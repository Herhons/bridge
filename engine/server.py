"""Minimal, clean local HTTP server (127.0.0.1:18765) for Bridge v0.2.5.

Shares the exact same endpoint registry and EndpointDriver execution path as
``bridge.py`` CLI, supports ``confirm: true`` two-step confirmation plus
``profile`` / ``free_only`` policy enforcement, and exposes ``/daemon/*``
control routes (``status``, ``start``, ``stop``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from engine import __version__
from engine.config import load_bridge_config

VALID_START_BRAINS = ("auto", "gemini", "deepseek", "grok")
VALID_START_EXECUTORS = ("auto", "workbuddy", "opencode", "antigravity")
VALID_START_PROFILES = ("none", "test", "routine", "council")


class BridgeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_cls,
        *,
        dispatcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        endpoint_names: list[str],
        status_fn: Callable[[], dict[str, Any]] | None = None,
        stop_fn: Callable[[float], dict[str, Any]] | None = None,
        start_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        super().__init__(server_address, handler_cls)
        self.dispatcher = dispatcher
        self.endpoint_names = endpoint_names
        # M4-11 / M4-13: daemon control plane. Defaults lazily import bridge to
        # avoid circular imports; injecting them keeps offline tests isolated.
        self.status_fn = status_fn or _default_status_fn
        self.stop_fn = stop_fn or _default_stop_fn
        self.start_fn = start_fn or _default_start_fn


def _default_status_fn() -> dict[str, Any]:
    """``bridge.py daemon-status`` snapshot, imported lazily on purpose."""
    import bridge

    return bridge.daemon_status()


def _default_stop_fn(force_wait: float = 5.0) -> dict[str, Any]:
    """``bridge.py stop`` semantics: write ``runs/STOP`` -> wait -> taskkill."""
    import bridge

    return bridge.stop_scheduler(force_wait)


def _detached_spawn_flags() -> int:
    """Windows detached + no-window creation flags (0 on POSIX)."""
    if os.name != "nt":
        return 0
    return (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        | int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    )


def _default_start_fn(options: dict[str, Any]) -> dict[str, Any]:
    """Spawn ``scripts/auto_heal_loop.py --daemon`` detached outside executor sandboxes."""
    import bridge

    current = bridge.daemon_status()
    if isinstance(current, dict) and current.get("running"):
        return {
            "ok": True,
            "action": "start",
            "started": False,
            "already_running": True,
            "pid": current.get("pid"),
            "status": current,
        }

    root = Path(__file__).resolve().parents[1]
    stop_file = root / "runs" / "STOP"
    try:
        stop_file.unlink(missing_ok=True)
    except OSError:
        pass

    cmd = [
        sys.executable,
        str(root / "scripts" / "auto_heal_loop.py"),
        "--daemon",
        "--brain", str(options.get("brain", "auto")),
        "--executor", str(options.get("executor", "workbuddy")),
        "--executor-profile", str(options.get("executor_profile", "none")),
        "--timeout", str(options.get("timeout", 600)),
        "--sleep", str(options.get("sleep", 30)),
        "--delay-start", str(options.get("delay_start", 0)),
        "--max-rounds", str(options.get("max_rounds", 20)),
    ]
    if options.get("stop_on_bug", True):
        cmd.append("--stop-on-bug")
    if options.get("no_push", False):
        cmd.append("--no-push")

    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    flags = _detached_spawn_flags()
    if flags:
        kwargs["creationflags"] = flags
    proc = subprocess.Popen(cmd, **kwargs)
    return {
        "ok": True,
        "action": "start",
        "started": True,
        "already_running": False,
        "pid": int(proc.pid),
        "cmd": cmd,
    }


def _parse_start_options(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize POST /daemon/start options."""
    brain = str(body.get("brain", "auto")).strip().lower()
    if brain not in VALID_START_BRAINS:
        raise ValueError("brain must be one of %s" % ", ".join(VALID_START_BRAINS))

    executor = str(body.get("executor", "workbuddy")).strip().lower()
    if executor not in VALID_START_EXECUTORS:
        raise ValueError("executor must be one of %s" % ", ".join(VALID_START_EXECUTORS))

    executor_profile = str(body.get("executor_profile", "none")).strip().lower()
    if executor_profile not in VALID_START_PROFILES:
        raise ValueError(
            "executor_profile must be one of %s" % ", ".join(VALID_START_PROFILES)
        )

    def _num(key: str, default: float, *, allow_zero: bool) -> float:
        raw = body.get(key, default)
        if isinstance(raw, bool):
            raise ValueError("%s must be a number" % key)
        try:
            val = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be a number" % key) from exc
        if allow_zero and val < 0:
            raise ValueError("%s must not be negative" % key)
        if not allow_zero and val <= 0:
            raise ValueError("%s must be positive" % key)
        return val

    timeout = _num("timeout", 600, allow_zero=False)
    sleep = _num("sleep", 30, allow_zero=True)
    delay_start = _num("delay_start", 0, allow_zero=True)

    raw_rounds = body.get("max_rounds", 20)
    if isinstance(raw_rounds, bool):
        raise ValueError("max_rounds must be a positive integer")
    try:
        max_rounds = int(raw_rounds)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_rounds must be a positive integer") from exc
    if max_rounds <= 0:
        raise ValueError("max_rounds must be a positive integer")

    return {
        "brain": brain,
        "executor": executor,
        "executor_profile": executor_profile,
        "timeout": timeout,
        "sleep": sleep,
        "delay_start": delay_start,
        "max_rounds": max_rounds,
        "stop_on_bug": bool(body.get("stop_on_bug", True)),
        "no_push": bool(body.get("no_push", False)),
    }


def normalize_request_attach(value: Any) -> list[str]:
    """Normalize ``attach`` / ``files`` request fields to validated abs paths."""
    if value is None:
        return []
    entries = [value] if isinstance(value, str) else value
    if not isinstance(entries, list):
        raise ValueError("attach/files must be a string or a list of paths")
    resolved: list[str] = []
    for entry in entries:
        for part in str(entry).split(","):
            if not part.strip():
                continue
            raw = os.path.expanduser(part.strip())
            absolute = os.path.abspath(raw)
            if not os.path.isfile(absolute):
                raise FileNotFoundError("Attachment not found: %s" % absolute)
            resolved.append(absolute)
    return resolved


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = f"Bridge/{__version__}"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "version": __version__})
            return
        if self.path == "/daemon/status":
            try:
                payload = self.server.status_fn()
            except Exception as exc:  # noqa: BLE001 - never kill the server
                self._send_json(
                    500,
                    {"ok": False, "error": "status_failed", "message": str(exc)},
                )
                return
            self._send_json(200, payload if isinstance(payload, dict) else {"ok": False, "error": "bad_status_payload"})
            return
        if self.path == "/endpoints":
            self._send_json(200, {"ok": True, "version": __version__, "endpoints": self.server.endpoint_names})
            return
        if self.path.startswith("/status/"):
            ep = self.path.split("/status/", 1)[1].strip("/")
            res = self.server.dispatcher("status", ep, {})
            self._send_json(200 if res.get("ok") else 404, res)
            return
        self._send_json(404, {"ok": False, "error": "not_found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": "invalid_json", "message": str(exc)})
            return
        if not isinstance(body, dict):
            self._send_json(
                400,
                {"ok": False, "error": "invalid_json", "message": "body must be a JSON object"},
            )
            return

        if self.path == "/daemon/start":
            try:
                opts = _parse_start_options(body)
            except ValueError as exc:
                self._send_json(
                    400,
                    {"ok": False, "error": "invalid_start_option", "message": str(exc)},
                )
                return
            try:
                payload = self.server.start_fn(opts)
            except Exception as exc:  # noqa: BLE001 - report, never 500-crash
                self._send_json(
                    500,
                    {"ok": False, "error": "start_failed", "message": str(exc)},
                )
                return
            self._send_json(200, payload if isinstance(payload, dict) else {"ok": False, "error": "bad_start_payload"})
            return

        if self.path == "/daemon/stop":
            raw_wait = body.get("force_wait", 5)
            try:
                force_wait = float(raw_wait)
            except (TypeError, ValueError):
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_force_wait",
                        "message": "force_wait must be a number of seconds",
                    },
                )
                return
            if force_wait < 0:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "invalid_force_wait",
                        "message": "force_wait must not be negative",
                    },
                )
                return
            try:
                payload = self.server.stop_fn(force_wait)
            except Exception as exc:  # noqa: BLE001 - report, never 500-crash
                self._send_json(
                    500,
                    {"ok": False, "error": "stop_failed", "message": str(exc)},
                )
                return
            self._send_json(200, payload if isinstance(payload, dict) else {"ok": False, "error": "bad_stop_payload"})
            return

        if self.path == "/ask":
            ep = str(body.get("endpoint") or "").strip().lower()
            endpoints = body.get("endpoints")
            prompt = str(body.get("prompt") or "")
            if not prompt.strip():
                self._send_json(400, {"ok": False, "error": "empty_prompt"})
                return
            raw_attach = body.get("attach")
            if raw_attach is None:
                raw_attach = body.get("files")

            if bool(body.get("confirm", False)):
                targets = [ep] if ep else [str(t).strip().lower() for t in (endpoints or [])]
                if not targets:
                    self._send_json(400, {"ok": False, "error": "missing_endpoint"})
                    return
                preview = prompt[:200] + ("..." if len(prompt) > 200 else "")
                self._send_json(
                    202,
                    {
                        "ok": False,
                        "error": "confirmation_required",
                        "endpoints": targets,
                        "model": body.get("model"),
                        "profile": body.get("profile"),
                        "attach": raw_attach,
                        "preview": preview,
                    },
                )
                return

            try:
                attach_paths = normalize_request_attach(raw_attach)
            except FileNotFoundError as exc:
                self._send_json(400, {"ok": False, "error": "missing_resource", "message": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "invalid_attach", "message": str(exc)})
                return

            opts = {
                "timeout": body.get("timeout"),
                "new_chat": bool(body.get("new_chat", False)),
                "headed": body.get("headed"),
                "model": body.get("model"),
                "profile": body.get("profile"),
                "free_only": bool(body.get("free_only", False)),
                "force": bool(body.get("force", False)),
                "attach": attach_paths or None,
                "prompt": prompt,
            }
            if ep:
                res = self.server.dispatcher("ask", ep, opts)
                code = 200 if res.get("ok") else (409 if res.get("error") == "duplicate" else 500)
                self._send_json(code, res)
                return
            if isinstance(endpoints, list) and endpoints:
                results = {}
                for target in endpoints:
                    name = str(target).strip().lower()
                    results[name] = self.server.dispatcher("ask", name, opts)
                all_ok = all(r.get("ok") for r in results.values())
                self._send_json(200 if all_ok else 207, {"ok": all_ok, "results": results})
                return
            self._send_json(400, {"ok": False, "error": "missing_endpoint", "hint": "Pass 'endpoint' or 'endpoints'"})
            return

        if self.path == "/read":
            ep = str(body.get("endpoint") or "").strip().lower()
            if not ep:
                self._send_json(400, {"ok": False, "error": "missing_endpoint"})
                return
            res = self.server.dispatcher("read", ep, {})
            self._send_json(200 if res.get("ok") else 500, res)
            return

        if self.path == "/shutdown":
            self._send_json(200, {"ok": True, "stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        self._send_json(404, {"ok": False, "error": "not_found", "path": self.path})


def create_server(
    *,
    dispatcher: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    endpoint_names: list[str],
    host: str | None = None,
    port: int | None = None,
    status_fn: Callable[[], dict[str, Any]] | None = None,
    stop_fn: Callable[[float], dict[str, Any]] | None = None,
    start_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> BridgeHTTPServer:
    cfg = load_bridge_config()
    bind_host = host or str(cfg.get("host", "127.0.0.1"))
    bind_port = int(cfg.get("port", 18765)) if port is None else int(port)
    return BridgeHTTPServer(
        (bind_host, bind_port),
        BridgeRequestHandler,
        dispatcher=dispatcher,
        endpoint_names=endpoint_names,
        status_fn=status_fn,
        stop_fn=stop_fn,
        start_fn=start_fn,
    )
