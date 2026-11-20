"""Minimal, clean local HTTP server (127.0.0.1:18765) for Bridge v0.2.3.

Shares the exact same endpoint registry and EndpointDriver execution path as
``bridge.py`` CLI, and supports ``confirm: true`` two-step confirmation plus
``profile`` / ``free_only`` policy enforcement.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from engine import __version__
from engine.config import load_bridge_config


class BridgeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, *, dispatcher: Callable[[str, str, dict[str, Any]], dict[str, Any]], endpoint_names: list[str]):
        super().__init__(server_address, handler_cls)
        self.dispatcher = dispatcher
        self.endpoint_names = endpoint_names


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
) -> BridgeHTTPServer:
    cfg = load_bridge_config()
    bind_host = host or str(cfg.get("host", "127.0.0.1"))
    bind_port = int(cfg.get("port", 18765)) if port is None else int(port)
    return BridgeHTTPServer(
        (bind_host, bind_port),
        BridgeRequestHandler,
        dispatcher=dispatcher,
        endpoint_names=endpoint_names,
    )
