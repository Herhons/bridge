#!/usr/bin/env python3
"""OpenCode endpoint driver for Bridge v0.2.1.

Reconciled against live Windows environment: ``opencode.exe`` is a pure CLI/TUI
program (``MainWindowHandle == 0``), so ``ask()`` invokes native non-interactive
``opencode --prompt "<prompt>" --auto`` directly (never steals focus, never hangs
``EnumWindows``), while ``read()`` uses the verified read-only SQLite fallback
(``engine/opencode_db.py``).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import __version__, opencode_db  # noqa: E402
from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402


class OpenCodeDriver(EndpointDriver):
    name = "opencode"

    def __init__(
        self,
        *args,
        db_path: str | Path | None = None,
        cli_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._db_path = db_path
        self._cli_runner = cli_runner

    def _exe(self) -> str | None:
        cmd = str(self.cfg.get("cli_command") or "opencode")
        return shutil.which(cmd)

    def status(self) -> dict[str, Any]:
        exe = self._exe()
        db_exists = (Path(self._db_path) if self._db_path else opencode_db.DEFAULT_DB).is_file()
        return {
            "ok": bool(exe or db_exists),
            "version": __version__,
            "endpoint": self.name,
            "kind": "cli",
            "cli_path": exe,
            "db_exists": db_exists,
            "ready": bool(exe),
            "models": list(self.cfg.get("models") or []),
        }

    def read(self) -> dict[str, Any]:
        try:
            hit = opencode_db.read_latest_reply(title=None, after_seq=0, db_path=self._db_path)
            if hit:
                return {
                    "ok": True,
                    "endpoint": self.name,
                    "source": "sqlite",
                    "session_id": hit["session_id"],
                    "reply": hit["reply"],
                }
        except Exception as exc:
            return {"ok": False, "endpoint": self.name, "error": "sqlite_error", "message": str(exc), "reply": ""}
        return {"ok": False, "endpoint": self.name, "error": "no_reply", "reply": ""}

    def _run_cli(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        if self._cli_runner is not None:
            return self._cli_runner(cmd, timeout)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return int(proc.returncode), proc.stdout or "", proc.stderr or ""

    def ask(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        new_chat: bool = False,
        headed: bool | None = None,
        model: str | None = None,
        confirm: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        budget = float(timeout or self.cfg.get("timeout") or self.bridge_cfg.get("default_timeout", 180.0))
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return {"ok": False, "endpoint": self.name, "error": "empty_prompt", "reply": ""}

        if not force:
            allowed, prev_state = self.ledger.check(clean_prompt, self.name)
            if not allowed:
                return {
                    "ok": False,
                    "endpoint": self.name,
                    "error": "duplicate",
                    "previous_state": prev_state,
                    "ledger_key": self.ledger.key(clean_prompt, self.name),
                    "reply": "",
                }

        if confirm and not self._confirm_fn(self.name, clean_prompt, model):
            self.ledger.mark(clean_prompt, self.name, "failed_retryable", note="cancelled_by_user")
            return {"ok": False, "endpoint": self.name, "error": "cancelled_by_user", "reply": ""}

        exe = self._exe() if self._cli_runner is None else "opencode"
        if not exe:
            return {
                "ok": False,
                "endpoint": self.name,
                "error": "cli_not_found",
                "message": "opencode executable not found in PATH",
                "reply": "",
            }

        before_seq = 0
        try:
            before_seq = opencode_db.current_max_seq(db_path=self._db_path)
        except Exception:
            pass

        cmd = [exe, "--prompt", clean_prompt, "--auto"]
        if not new_chat:
            cmd.append("--continue")

        self.ledger.mark(clean_prompt, self.name, "submitting")
        try:
            rc, stdout, stderr = self._run_cli(cmd, budget)
        except subprocess.TimeoutExpired:
            self.ledger.mark(clean_prompt, self.name, "unknown", note="timeout")
            return {
                "ok": False,
                "endpoint": self.name,
                "error": "timeout",
                "message": f"opencode CLI timed out after {budget:.0f}s",
                "reply": "",
                "elapsed": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            self.ledger.mark(clean_prompt, self.name, "failed_retryable", note=type(exc).__name__)
            return {"ok": False, "endpoint": self.name, "error": "cli_error", "message": str(exc), "reply": ""}

        reply = stdout.strip()
        source = "cli_stdout"
        if not reply:
            try:
                hit = opencode_db.read_latest_reply(after_seq=before_seq, db_path=self._db_path)
                if hit:
                    reply = hit["reply"]
                    source = "sqlite_fallback"
            except Exception:
                pass

        if rc == 0 and reply:
            self.ledger.mark(clean_prompt, self.name, "accepted")
            return {
                "ok": True,
                "endpoint": self.name,
                "reply": reply,
                "source": source,
                "model_requested": model,
                "elapsed": round(time.monotonic() - started, 2),
            }

        self.ledger.mark(clean_prompt, self.name, "failed_retryable", note=f"rc={rc}")
        return {
            "ok": False,
            "endpoint": self.name,
            "error": "cli_failed",
            "returncode": rc,
            "stderr": stderr.strip()[:400],
            "reply": reply,
            "elapsed": round(time.monotonic() - started, 2),
        }


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(OpenCodeDriver))
