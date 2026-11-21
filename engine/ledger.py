"""Persistent submission ledger keyed by sha256(endpoint:prompt).

Provides fail-closed duplicate submission protection while still allowing
explicit operator clearing or ``--force`` override.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from engine.config import RUNS_DIR

STATES = (
    "draft",
    "submitting",
    "accepted",
    "unknown",
    "failed_retryable",
    "failed_terminal",
)
BLOCKING_STATES = frozenset({"submitting", "accepted", "unknown"})
# ``unknown`` is recorded when a submit may or may not have landed. These two
# notes mean *our* side gave up before a verified delivery, so a retry cannot
# double-submit: blocking them turned one timeout into a permanent
# ``duplicate rc=6`` lock that no amount of retrying could clear.
RETRYABLE_UNKNOWN_NOTES = frozenset({"timeout", "submit_not_verified"})
# A ``submitting`` row left behind by a killed process would block the key
# forever; past this age it is treated as abandoned rather than in flight.
SUBMITTING_STALE_SECONDS = 900.0


class SubmissionLedger:
    """JSON-backed submission ledger with atomic writes."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (RUNS_DIR / "ledger.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({})

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def key(prompt: str, endpoint: str) -> str:
        raw = f"{endpoint.strip().lower()}:{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def check(
        self,
        prompt: str,
        endpoint: str,
        *,
        allow_retry_unknown: bool = True,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Return ``(allowed, previous_state)``.

        ``accepted`` always blocks: that prompt really was delivered, and
        resending it would be a genuine duplicate.

        ``unknown`` blocks *unless* it was recorded with a transient note
        (``timeout`` / ``submit_not_verified``) and ``allow_retry_unknown`` is
        left on. Those notes mean our side stopped waiting before a verified
        receipt, so the remote never confirmed anything; a bare ``unknown`` with
        no note (or any other note) keeps the fail-closed behaviour.

        ``submitting`` blocks only while the row is fresh. A process killed
        mid-submit cannot be distinguished from a live one, so after
        ``SUBMITTING_STALE_SECONDS`` the key is reclaimed instead of staying
        wedged forever.

        ``now`` exists for tests; production callers use the wall clock.
        """
        k = self.key(prompt, endpoint)
        entry = self._load().get(k) or {}
        state = str(entry.get("state") or "draft")
        if state not in BLOCKING_STATES:
            return True, state
        note = str(entry.get("note") or "")
        if state == "unknown":
            if allow_retry_unknown and note in RETRYABLE_UNKNOWN_NOTES:
                return True, state
            return False, state
        if state == "submitting":
            stamp = float(now if now is not None else time.time())
            try:
                age = stamp - float(entry.get("ts") or 0.0)
            except (TypeError, ValueError):
                age = 0.0
            if age >= SUBMITTING_STALE_SECONDS:
                return True, state
            return False, state
        return False, state

    def mark(self, prompt: str, endpoint: str, state: str, *, note: str = "") -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"Invalid ledger state: {state!r}. Must be one of {STATES}")
        k = self.key(prompt, endpoint)
        data = self._load()
        entry: dict[str, Any] = {
            "endpoint": endpoint.strip().lower(),
            "state": state,
            "ts": round(time.time(), 3),
            "preview": prompt[:80],
        }
        if note:
            entry["note"] = note
        data[k] = entry
        self._save(data)
        return entry

    def get(self, prompt: str, endpoint: str) -> dict[str, Any] | None:
        return self._load().get(self.key(prompt, endpoint))

    def list_entries(self) -> dict[str, dict[str, Any]]:
        return self._load()

    def clear(self, key: str | None = None) -> int:
        data = self._load()
        if key is None:
            removed = len(data)
            self._save({})
            return removed
        if key in data:
            del data[key]
            self._save(data)
            return 1
        return 0
