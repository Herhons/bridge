#!/usr/bin/env python3
"""Bridge v0.2.3 — Single master entry point to call any of the 10 AI endpoints.

Usage:
    python bridge.py ask deepseek "帮我审一下这段代码"
    python bridge.py ask workbuddy "只回复：收到" --profile test --headed
    python bridge.py ask deepseek "帮我审一下" --profile council --confirm --save reply.md
    python bridge.py ask grok,deepseek --file prompt.txt --json
    python bridge.py read opencode --save last_reply.txt
    python bridge.py models workbuddy [--live]
    python bridge.py status
    python bridge.py snapshot hermes
    python bridge.py ledger list
    python bridge.py ledger clear [--key <hash>]
    python bridge.py serve [--port 18765]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from endpoints import ENDPOINT_NAMES, get_driver  # noqa: E402
from engine import __version__  # noqa: E402
from engine.base import (  # noqa: E402
    configure_stdio_utf8,
    exit_code_for,
    safe_print,
    safe_print_json,
    save_reply_file,
)
from engine.config import run_doctor  # noqa: E402
from engine.ledger import SubmissionLedger  # noqa: E402
from engine.server import create_server  # noqa: E402


def dispatch(action: str, endpoint: str, opts: dict[str, Any] | None = None, *, driver_factory=get_driver) -> dict[str, Any]:
    opts = opts or {}
    try:
        driver = driver_factory(endpoint)
    except KeyError as exc:
        return {"ok": False, "endpoint": endpoint, "error": "unknown_endpoint", "message": str(exc)}

    if action == "ask":
        return driver.ask(
            str(opts.get("prompt") or ""),
            timeout=opts.get("timeout"),
            new_chat=bool(opts.get("new_chat", False)),
            headed=opts.get("headed"),
            model=opts.get("model"),
            profile=opts.get("profile"),
            free_only=bool(opts.get("free_only", False)),
            confirm=bool(opts.get("confirm", False)),
            force=bool(opts.get("force", False)),
            attach=opts.get("attach"),
        )
    if action == "read":
        return driver.read()
    if action == "models":
        return driver.models(live=bool(opts.get("live", False)), headed=bool(opts.get("headed", True)))
    if action == "status":
        return driver.status()
    if action == "snapshot":
        return driver.snapshot(
            interactive_only=bool(opts.get("interactive_only", True)),
            limit=int(opts.get("limit", 300)),
        )
    return {"ok": False, "endpoint": endpoint, "error": "unknown_action", "message": f"Unknown action: {action}"}


def normalize_attach_paths(
    values: list[str] | str | None,
    *,
    base_dir: str | Path | None = None,
) -> list[str]:
    """Expand, absolutize and validate attachment paths before any UI work.

    Comma-separated entries and repeated ``--attach`` flags are flattened.
    Raises FileNotFoundError as soon as any path does not resolve to a file.
    """
    if not values:
        return []
    entries = [values] if isinstance(values, str) else list(values)
    resolved: list[str] = []
    for entry in entries:
        for part in str(entry).split(","):
            if not part.strip():
                continue
            raw = os.path.expanduser(part.strip())
            candidate = raw if os.path.isabs(raw) else str((Path(base_dir) if base_dir else Path.cwd()) / raw)
            absolute = os.path.abspath(candidate)
            if not os.path.isfile(absolute):
                raise FileNotFoundError("Attachment not found: %s" % absolute)
            resolved.append(absolute)
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description=f"Bridge v{__version__} — Universal AI-to-AI caller (10 endpoints, UIA-first + screenshot fallback)",
    )
    parser.add_argument("--version", action="version", version=f"Bridge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="Send a prompt to one or more endpoints and print full reply")
    p_ask.add_argument("endpoint", help=f"Endpoint name or comma-separated list ({', '.join(ENDPOINT_NAMES)})")
    p_ask.add_argument("prompt", nargs="?", default="", help="Prompt text")
    p_ask.add_argument("--file", "-f", dest="prompt_file", help="Read prompt from UTF-8 file")
    p_ask.add_argument("--attach", "-a", action="append", default=None, help="Attach file path(s) before sending")
    p_ask.add_argument("--save", "-o", dest="save_path", help="Save full reply text to UTF-8 file")
    p_ask.add_argument("--new", action="store_true", help="Start a new chat before sending")
    p_ask.add_argument("--model", default=None, help="Switch to this model before sending")
    p_ask.add_argument("--profile", choices=("test", "routine", "council"), default=None, help="Scene strength profile from config/model_policy.json")
    p_ask.add_argument("--free-only", action="store_true", help="Refuse sending if a paid model multiplier is detected or free tier cannot be confirmed")
    p_ask.add_argument("--confirm", action="store_true", help="Wait for user confirmation before sending")
    p_ask.add_argument("--force", action="store_true", help="Bypass duplicate submission ledger check")
    p_ask.add_argument("--timeout", type=float, default=None, help="Wait timeout in seconds")
    p_ask.add_argument("--headed", action="store_true", help="Allow physical mouse/keyboard immediately")
    p_ask.add_argument("--json", action="store_true", dest="as_json", help="Print full JSON response")

    p_read = sub.add_parser("read", help="Read the latest reply from an endpoint without sending")
    p_read.add_argument("endpoint", choices=ENDPOINT_NAMES)
    p_read.add_argument("--save", "-o", dest="save_path", help="Save read reply to UTF-8 file")
    p_read.add_argument("--json", action="store_true", dest="as_json")

    p_models = sub.add_parser("models", help="List configured (or --live probed) models & policy tiers for an endpoint")
    p_models.add_argument("endpoint", choices=ENDPOINT_NAMES)
    p_models.add_argument("--live", action="store_true", help="Open UIA model dropdown on Windows to probe live options & pricing")

    p_status = sub.add_parser("status", help="Show window & readiness status (one or all endpoints)")
    p_status.add_argument("endpoint", nargs="?", choices=ENDPOINT_NAMES, default=None)

    p_snap = sub.add_parser("snapshot", help="Capture live UIA accessibility tree for an endpoint")
    p_snap.add_argument("endpoint", choices=ENDPOINT_NAMES)
    p_snap.add_argument("--all", action="store_true", help="Include non-interactive nodes")
    p_snap.add_argument("--limit", type=int, default=300)

    p_list = sub.add_parser("list", help="List all 10 registered endpoints")
    sub.add_parser("doctor", help="Validate configs, model policy, ratio boxes, and template PNGs")
    p_list.add_argument("--json", action="store_true", dest="as_json")

    p_ledger = sub.add_parser("ledger", help="Inspect or clear the duplicate-submission ledger")
    p_ledger.add_argument("ledger_action", choices=("list", "clear"))
    p_ledger.add_argument("--key", default=None, help="Specific ledger hash key to clear")

    p_serve = sub.add_parser("serve", help="Run optional local HTTP server (127.0.0.1:18765)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    return parser


def main(argv: list[str] | None = None, *, driver_factory=get_driver) -> int:
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        res = {"version": __version__, **run_doctor(ENDPOINT_NAMES)}
        safe_print_json(res)
        return 0 if res["ok"] else 1

    if args.command == "list":
        if args.as_json:
            safe_print_json({"version": __version__, "endpoints": list(ENDPOINT_NAMES)})
        else:
            for name in ENDPOINT_NAMES:
                safe_print(name)
        return 0

    if args.command == "ledger":
        ledger = SubmissionLedger()
        if args.ledger_action == "list":
            safe_print_json(ledger.list_entries())
            return 0
        removed = ledger.clear(args.key)
        safe_print_json({"ok": True, "removed": removed}, indent=None)
        return 0

    if args.command == "ask":
        try:
            attach_paths = normalize_attach_paths(args.attach)
            prompt_file = (
                os.path.abspath(os.path.expanduser(str(args.prompt_file)))
                if args.prompt_file else None
            )
            prompt = args.prompt
            if prompt_file:
                prompt = Path(prompt_file).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            safe_print_json(
                {"ok": False, "error": "missing_resource", "message": str(exc), "reply": ""},
                indent=None, stream=sys.stderr,
            )
            return 3
        targets = [t.strip().lower() for t in args.endpoint.split(",") if t.strip()]
        opts = {
            "prompt": prompt,
            "timeout": args.timeout,
            "new_chat": args.new,
            "headed": True if args.headed else None,
            "model": args.model,
            "profile": args.profile,
            "free_only": args.free_only,
            "confirm": args.confirm,
            "force": args.force,
            "attach": attach_paths or None,
        }
        if len(targets) == 1:
            res = dispatch("ask", targets[0], opts, driver_factory=driver_factory)
            if res.get("ok") and args.save_path:
                res["saved_to"] = save_reply_file(args.save_path, res["reply"])
            if args.as_json:
                safe_print_json(res)
            elif res.get("ok"):
                safe_print(res["reply"])
            else:
                safe_print_json(res, indent=None, stream=sys.stderr)
            return exit_code_for(res)

        results = {t: dispatch("ask", t, opts, driver_factory=driver_factory) for t in targets}
        all_ok = all(r.get("ok") for r in results.values())
        if args.save_path and all_ok:
            combined = "\n\n".join(f"=== {k} ===\n{v['reply']}" for k, v in results.items())
            save_reply_file(args.save_path, combined)
        if args.as_json:
            safe_print_json({"ok": all_ok, "results": results})
        else:
            for name, res in results.items():
                safe_print(f"=== {name} ===")
                safe_print(res["reply"] if res.get("ok") else f"[ERROR] {res.get('error')}: {res.get('message', '')}")
        return 0 if all_ok else 1

    if args.command == "read":
        res = dispatch("read", args.endpoint, driver_factory=driver_factory)
        if res.get("ok") and args.save_path:
            res["saved_to"] = save_reply_file(args.save_path, res["reply"])
        if args.as_json:
            safe_print_json(res)
        elif res.get("ok"):
            safe_print(res["reply"])
        else:
            safe_print_json(res, indent=None, stream=sys.stderr)
        return exit_code_for(res)

    if args.command == "models":
        res = dispatch("models", args.endpoint, {"live": args.live, "headed": True}, driver_factory=driver_factory)
        safe_print_json(res)
        return exit_code_for(res)

    if args.command == "status":
        if args.endpoint:
            res = dispatch("status", args.endpoint, driver_factory=driver_factory)
            safe_print_json(res)
            return exit_code_for(res)
        out = {name: dispatch("status", name, driver_factory=driver_factory) for name in ENDPOINT_NAMES}
        safe_print_json({"version": __version__, "endpoints": out})
        return 0

    if args.command == "snapshot":
        res = dispatch(
            "snapshot",
            args.endpoint,
            {"interactive_only": not args.all, "limit": args.limit},
            driver_factory=driver_factory,
        )
        safe_print_json(res)
        return exit_code_for(res)

    if args.command == "serve":
        srv = create_server(
            dispatcher=lambda act, ep, o: dispatch(act, ep, o, driver_factory=driver_factory),
            endpoint_names=list(ENDPOINT_NAMES),
            host=args.host,
            port=args.port,
        )
        host, port = srv.server_address
        safe_print_json({"ok": True, "version": __version__, "listening": f"http://{host}:{port}"}, indent=None)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
