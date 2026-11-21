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
    python bridge.py stop [--force-wait 5]
    python bridge.py daemon-status
    python bridge.py ledger list
    python bridge.py ledger clear [--key <hash>]
    python bridge.py serve [--port 18765]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
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


def ask_with_structured_errors(
    endpoint: str,
    opts: dict[str, Any],
    *,
    driver_factory=get_driver,
) -> dict[str, Any]:
    """``dispatch("ask")`` that turns an endpoint capability gap into JSON.

    ``EndpointDriver.ask`` raises ``NotImplementedError`` when ``--attach`` is
    passed to an endpoint without an ``attach_button`` selector. Letting that
    escape printed a raw traceback even under ``--json``, so it is mapped to
    the same ``attach_unsupported`` code the CLI-shaped endpoints return.
    """
    try:
        return dispatch("ask", endpoint, opts, driver_factory=driver_factory)
    except NotImplementedError as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "error": "attach_unsupported",
            "message": str(exc),
            "attachments": list(opts.get("attach") or []),
            "reply": "",
        }


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


# ---------------------------------------------------------------- scheduler control
STOP_FILE = "STOP"
PID_FILE = "overnight.pid"
STATE_FILE = "state_machine.json"
ROADMAP_PATH = "docs/ROADMAP.md"
ROADMAP_PENDING_RE = re.compile(r"^[ \t]*-[ \t]+\[ \]", re.M)
ROADMAP_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+\[[ xX]\]", re.M)
UNFINISHED_DISPATCH_STATES = ("dispatched", "timed_out")
STOP_WAIT_POLL = 0.2


def runs_dir() -> Path:
    """``runs/`` under the repo root (``ROOT`` is patched by offline tests)."""
    return ROOT / "runs"


def _unlink(path: Path) -> bool:
    """Remove ``path`` if present; True only when something was deleted."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def scheduler_pid(path: str | Path | None = None) -> int | None:
    """Read the daemon pid file; None when absent, empty or unparsable."""
    target = Path(path) if path is not None else runs_dir() / PID_FILE
    try:
        text = target.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """Liveness probe that can never kill the target.

    ``os.kill(pid, 0)`` terminates the process on Windows, so nt goes through
    ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess``.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
        except (AttributeError, OSError, ValueError):
            return False
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_scheduler_tree(pid: int) -> int:
    """``taskkill /F /T`` the scheduler tree; returns taskkill's rc (-1 on error)."""
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return int(proc.returncode)
    except (OSError, subprocess.SubprocessError):
        return -1


def stop_scheduler(
    force_wait: float = 5.0,
    *,
    runs: str | Path | None = None,
) -> dict[str, Any]:
    """Ask the overnight scheduler to stop, force-killing its tree if it will not.

    ``runs/STOP`` is written first: the loop polls it at round start, before
    every dispatch and inside its sleep slices. After ``force_wait`` seconds a
    still-living PID gets ``taskkill /F /T``. A stale pid file (no live process)
    is cleaned *without* writing STOP, so the next run is not stopped on arrival;
    STOP is removed once the scheduler is confirmed gone.
    """
    base = Path(runs) if runs is not None else runs_dir()
    base.mkdir(parents=True, exist_ok=True)
    stop_path = base / STOP_FILE
    pid_path = base / PID_FILE
    pid = scheduler_pid(pid_path)
    alive = bool(pid and pid_alive(pid))
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": True,
        "action": "stop",
        "pid": pid,
        "running": alive,
        "stop_file": str(stop_path),
        "pid_file": str(pid_path),
        "stop_written": False,
        "waited_s": 0.0,
        "exited_gracefully": False,
        "killed": False,
        "taskkill_rc": None,
        "pid_file_removed": False,
        "detail": "",
    }
    if not alive:
        result["pid_file_removed"] = _unlink(pid_path)
        result["detail"] = "no live scheduler; nothing to stop"
        return result

    try:
        stop_path.write_text(
            "pid=%d requested_at=%s\n"
            % (pid, time.strftime("%Y-%m-%dT%H:%M:%S")),
            encoding="utf-8",
        )
    except OSError as exc:
        result["ok"] = False
        result["detail"] = "cannot write STOP: %r" % exc
        return result
    result["stop_written"] = True

    deadline = started + max(0.0, float(force_wait))
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(min(STOP_WAIT_POLL, max(0.0, deadline - time.monotonic())))
    result["waited_s"] = round(time.monotonic() - started, 2)

    if pid_alive(pid):
        current = scheduler_pid(pid_path)
        if current not in (None, pid):
            result["ok"] = False
            result["detail"] = (
                "pid file now points at %s; refusing to kill the wrong tree"
                % current
            )
            return result
        result["killed"] = True
        result["taskkill_rc"] = kill_scheduler_tree(pid)

    still = pid_alive(pid)
    result["running"] = still
    result["ok"] = not still
    result["exited_gracefully"] = not still and not result["killed"]
    result["pid_file_removed"] = _unlink(pid_path)
    if still:
        result["detail"] = "scheduler still alive after taskkill rc=%s" % (
            result["taskkill_rc"]
        )
    else:
        result["detail"] = (
            "stopped via taskkill" if result["killed"] else "stopped gracefully"
        )
        _unlink(stop_path)
    return result


def daemon_status(
    *,
    runs: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only scheduler snapshot: liveness, state machine, roadmap backlog."""
    base = Path(runs) if runs is not None else runs_dir()
    repo = Path(root) if root is not None else ROOT
    pid = scheduler_pid(base / PID_FILE)
    alive = bool(pid and pid_alive(pid))

    state_path = base / STATE_FILE
    state_doc: dict[str, Any] = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state_doc = loaded
    except (OSError, ValueError):
        state_doc = {}

    roadmap = repo / ROADMAP_PATH
    roadmap_present = roadmap.is_file()
    pending: int | None = None
    total: int | None = None
    if roadmap_present:
        try:
            text = roadmap.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        pending = len(ROADMAP_PENDING_RE.findall(text))
        total = len(ROADMAP_ITEM_RE.findall(text))

    unfinished = 0
    needs_review = 0
    dispatched_dir = base / "dispatched"
    if dispatched_dir.is_dir():
        for item in sorted(dispatched_dir.glob("*.json")):
            try:
                doc = json.loads(item.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("status") in UNFINISHED_DISPATCH_STATES:
                unfinished += 1
            if doc.get("needs_review"):
                needs_review += 1

    return {
        "ok": True,
        "action": "daemon-status",
        "pid": pid,
        "running": alive,
        "pid_file": str(base / PID_FILE),
        "stop_requested": (base / STOP_FILE).is_file(),
        "state": state_doc.get("state"),
        "state_reason": state_doc.get("reason"),
        "state_round": state_doc.get("round"),
        "state_pid": state_doc.get("pid"),
        "state_updated_at": state_doc.get("updated_at"),
        "state_file": str(state_path),
        "roadmap": ROADMAP_PATH,
        "roadmap_present": roadmap_present,
        "roadmap_pending": pending,
        "roadmap_total": total,
        "dispatch_unfinished": unfinished,
        "dispatch_needs_review": needs_review,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description=f"Bridge v{__version__} — Universal AI-to-AI caller (11 endpoints, UIA-first + screenshot fallback)",
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
    p_models.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Accepted for parity with the single-endpoint scripts "
             "(models output is always JSON)",
    )

    p_status = sub.add_parser("status", help="Show window & readiness status (one or all endpoints)")
    p_status.add_argument("endpoint", nargs="?", choices=ENDPOINT_NAMES, default=None)

    p_snap = sub.add_parser("snapshot", help="Capture live UIA accessibility tree for an endpoint")
    p_snap.add_argument("endpoint", choices=ENDPOINT_NAMES)
    p_snap.add_argument("--all", action="store_true", help="Include non-interactive nodes")
    p_snap.add_argument("--limit", type=int, default=300)

    p_list = sub.add_parser("list", help="List all 11 registered endpoints")
    sub.add_parser("doctor", help="Validate configs, model policy, ratio boxes, and template PNGs")
    p_list.add_argument("--json", action="store_true", dest="as_json")

    p_ledger = sub.add_parser("ledger", help="Inspect or clear the duplicate-submission ledger")
    p_ledger.add_argument("ledger_action", choices=("list", "clear"))
    p_ledger.add_argument("--key", default=None, help="Specific ledger hash key to clear")

    p_serve = sub.add_parser("serve", help="Run optional local HTTP server (127.0.0.1:18765)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    p_stop = sub.add_parser(
        "stop",
        help="Ask the overnight scheduler to stop gracefully, then taskkill /F /T if it will not",
    )
    p_stop.add_argument(
        "--force-wait",
        type=float,
        default=5.0,
        help="Seconds to wait for a graceful exit before force-killing the tree (default 5)",
    )

    sub.add_parser(
        "daemon-status",
        help="Read-only status: scheduler PID liveness, state machine, ROADMAP backlog",
    )

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
            res = ask_with_structured_errors(
                targets[0], opts, driver_factory=driver_factory
            )
            if res.get("ok") and args.save_path:
                res["saved_to"] = save_reply_file(args.save_path, res["reply"])
            if args.as_json:
                safe_print_json(res)
            elif res.get("ok"):
                safe_print(res["reply"])
            else:
                safe_print_json(res, indent=None, stream=sys.stderr)
            return exit_code_for(res)

        results = {
            t: ask_with_structured_errors(t, opts, driver_factory=driver_factory)
            for t in targets
        }
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
        # ``--json`` is accepted for CLI parity with the single-endpoint scripts
        # (``models`` there also defaults to JSON); the payload shape is
        # identical either way so no flagless caller changes behaviour.
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

    if args.command == "stop":
        res = stop_scheduler(args.force_wait)
        safe_print_json(res)
        return 0 if res.get("ok") else 1

    if args.command == "daemon-status":
        safe_print_json(daemon_status())
        return 0

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
