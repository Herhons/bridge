#!/usr/bin/env python3
"""OpenCode endpoint driver for Bridge v0.2.1.

Reconciled against live Windows environment: ``opencode.exe`` is a pure CLI/TUI
program (``MainWindowHandle == 0``), so ``ask()`` invokes native non-interactive
``opencode --prompt "<prompt>" --auto`` directly (never steals focus, never hangs
``EnumWindows``), while ``read()`` uses the verified read-only SQLite fallback
(``engine/opencode_db.py``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import __version__, opencode_db  # noqa: E402
from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402
from engine.config import RUNS_DIR  # noqa: E402

#: Substrings that mean "this CLI will not answer again soon". Matched
#: case-insensitively against stderr+stdout, only on an unsuccessful attempt.
#: The bare ``quota`` / ``exhausted`` entries are the last resort so the
#: specific phrases win; a "context exhausted" reply is therefore classified as
#: a quota hit too, which is still the right *behaviour* (fall through now), and
#: ``matched`` keeps the original wording visible for diagnosis.
QUOTA_HINTS: tuple[str, ...] = (
    "insufficient_quota",
    "quota exhausted",
    "quota_exceeded",
    "quota exceeded",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "credits exhausted",
    "usage limit",
    "payment required",
    "out of credits",
    "quota",
    "exhausted",
)
#: Bare numeric codes are matched with word boundaries so a large reply that
#: merely contains "4290" or a token count never trips the detector.
QUOTA_CODES: tuple[str, ...] = ("429", "402")
QUOTA_COOLDOWN_SECONDS = 300.0
COOLDOWN_FILE = "opencode_cooldown.json"


def detect_quota_exhaustion(*texts: str) -> str:
    """Return the matched quota hint (``""`` when the output looks normal)."""
    blob = "\n".join(str(t) for t in texts if t).lower()
    if not blob:
        return ""
    for hint in QUOTA_HINTS:
        if hint in blob:
            return hint
    for code in QUOTA_CODES:
        if re.search(r"(?<![0-9])%s(?![0-9])" % code, blob):
            return code
    return ""



class OpenCodeDriver(EndpointDriver):
    name = "opencode"

    def __init__(
        self,
        *args,
        db_path: str | Path | None = None,
        cli_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
        cooldown_path: str | Path | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._db_path = db_path
        self._cli_runner = cli_runner
        self._cooldown_path = Path(cooldown_path) if cooldown_path else (RUNS_DIR / COOLDOWN_FILE)

    def cooldown(self) -> dict[str, Any]:
        """Read the last quota marker (``{}`` when absent or unreadable)."""
        try:
            data = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _mark_cooldown(self, hint: str) -> str:
        """Record a best-effort cooldown marker; return its ISO ``until`` stamp.

        Deliberately *not* enforced as a gate inside ``ask()``: a stale marker
        must never be able to silently suppress a legitimate retry, and a hidden
        on-disk gate would make the offline suite order-dependent. Callers that
        want to skip the endpoint read ``cooldown()`` themselves.
        """
        until = datetime.now() + timedelta(seconds=QUOTA_COOLDOWN_SECONDS)
        try:
            self._cooldown_path.parent.mkdir(parents=True, exist_ok=True)
            self._cooldown_path.write_text(
                json.dumps({
                    "endpoint": self.name,
                    "hint": hint,
                    "until": until.isoformat(timespec="seconds"),
                    "written_at": datetime.now().isoformat(timespec="seconds"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        return until.isoformat(timespec="seconds")

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
            # Always False: opencode is a headless CLI (MainWindowHandle == 0).
            # The field exists so `bridge.py status` keeps one schema for all
            # endpoints (UIA callers read window_found unconditionally).
            "window_found": False,
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
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            # Detach the CLI from the parent console so a fallback dispatch from
            # the overnight loop cannot flash a black window on the desktop.
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000
            )
        proc = subprocess.run(cmd, **kwargs)
        return int(proc.returncode), proc.stdout or "", proc.stderr or ""

    def ask(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        new_chat: bool = False,
        headed: bool | None = None,
        model: str | None = None,
        profile: str | None = None,
        free_only: bool = False,
        confirm: bool = False,
        force: bool = False,
        attach: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one non-interactive ``opencode --prompt`` turn.

        ``profile`` / ``free_only`` / ``headed`` are accepted so
        ``bridge.dispatch()`` can forward its fixed option set to every
        endpoint, but they are not applied: the opencode CLI exposes no model
        switch and always runs its own free local models. ``attach`` is
        rejected with a structured error instead of raising, because this
        endpoint has no attach surface at all.
        """
        started = time.monotonic()
        budget = float(timeout or self.cfg.get("timeout") or self.bridge_cfg.get("default_timeout", 180.0))
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return {"ok": False, "endpoint": self.name, "error": "empty_prompt", "reply": ""}

        raw_attach = [attach] if isinstance(attach, str) else list(attach or [])
        attach_paths = [
            part.strip()
            for entry in raw_attach
            for part in str(entry).split(",")
            if part.strip()
        ]
        if attach_paths:
            return {
                "ok": False,
                "endpoint": self.name,
                "error": "attach_unsupported",
                "message": "opencode is a CLI endpoint and has no attach surface",
                "attachments": attach_paths,
                "reply": "",
            }

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
                "profile": profile,
                "elapsed": round(time.monotonic() - started, 2),
            }

        hint = detect_quota_exhaustion(stderr, stdout)
        if hint:
            until = self._mark_cooldown(hint)
            self.ledger.mark(
                clean_prompt, self.name, "failed_retryable", note="quota_exhausted"
            )
            return {
                "ok": False,
                "endpoint": self.name,
                "error": "quota_exhausted",
                "matched": hint,
                "returncode": rc,
                "cooldown_until": until,
                "retry_after_s": int(QUOTA_COOLDOWN_SECONDS),
                "message": (
                    "opencode quota/rate limit detected (%s); failing fast so the "
                    "caller can fall through instead of waiting" % hint
                ),
                "stderr": stderr.strip()[:400],
                "reply": reply,
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
