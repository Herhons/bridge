"""Offline coverage for the M4-2 batch 1 fixes.

Three defects are pinned here, none of which needs a window, a clipboard or a
real CLI binary:

* ``engine/ledger.py`` no longer turns one timed-out submit into a permanent
  ``duplicate rc=6`` lock (``unknown`` + transient note is retryable, everything
  else stays fail-closed, and an abandoned ``submitting`` row is reclaimed);
* ``bridge.py models <endpoint> --json`` parses and behaves exactly like the
  flagless form (the single-endpoint scripts already accept ``--json``);
* ``endpoints/opencode.py`` starts the CLI without a console window on Windows
  and reports quota/rate-limit hits as a structured ``quota_exhausted`` error
  instead of waiting out the full budget.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
import endpoints.opencode as opencode_mod  # noqa: E402
from engine.ledger import (  # noqa: E402
    RETRYABLE_UNKNOWN_NOTES,
    SUBMITTING_STALE_SECONDS,
    SubmissionLedger,
)

PROMPT = "m4-2 账本与驱动探测"
BRIDGE_CFG = {
    "poll_interval": 0.0,
    "stable_seconds": 0.0,
    "default_timeout": 5.0,
    "lock_timeout": 5.0,
    "headless_default": True,
}


class LedgerTimeoutUnlockTests(unittest.TestCase):
    def _ledger(self, tmp: str) -> SubmissionLedger:
        return SubmissionLedger(Path(tmp) / "ledger.json")

    def test_transient_notes_are_the_only_retryable_unknowns(self):
        self.assertEqual(
            RETRYABLE_UNKNOWN_NOTES, frozenset({"timeout", "submit_not_verified"})
        )

    def test_unknown_after_timeout_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "unknown", note="timeout")
            allowed, state = ledger.check(PROMPT, "deepseek")

        self.assertTrue(allowed, "a timed-out submit must not lock the prompt")
        self.assertEqual(state, "unknown")

    def test_unknown_after_unverified_submit_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "gemini", "unknown", note="submit_not_verified")
            allowed, _ = ledger.check(PROMPT, "gemini")

        self.assertTrue(allowed)

    def test_bare_unknown_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "unknown")
            allowed, state = ledger.check(PROMPT, "deepseek")

        self.assertFalse(allowed, "an unqualified unknown must keep blocking")
        self.assertEqual(state, "unknown")

    def test_unknown_with_other_note_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "unknown", note="window_closed")
            allowed, _ = ledger.check(PROMPT, "deepseek")

        self.assertFalse(allowed)

    def test_accepted_always_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "accepted", note="returned_at_deadline")
            allowed, state = ledger.check(PROMPT, "deepseek")

        self.assertFalse(allowed)
        self.assertEqual(state, "accepted")

    def test_allow_retry_unknown_false_restores_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "unknown", note="timeout")
            allowed, _ = ledger.check(
                PROMPT, "deepseek", allow_retry_unknown=False
            )

        self.assertFalse(allowed)

    def test_fresh_submitting_blocks_but_an_abandoned_one_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.mark(PROMPT, "deepseek", "submitting")
            fresh_allowed, fresh_state = ledger.check(PROMPT, "deepseek")
            stamp = float(ledger.get(PROMPT, "deepseek")["ts"])
            stale_allowed, stale_state = ledger.check(
                PROMPT, "deepseek", now=stamp + SUBMITTING_STALE_SECONDS + 1
            )

        self.assertFalse(fresh_allowed)
        self.assertEqual(fresh_state, "submitting")
        self.assertTrue(stale_allowed, "a killed submit must not wedge the key")
        self.assertEqual(stale_state, "submitting")

    def test_failed_retryable_and_draft_are_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            self.assertTrue(ledger.check(PROMPT, "deepseek")[0])
            ledger.mark(PROMPT, "deepseek", "failed_retryable", note="quota_exhausted")
            allowed, state = ledger.check(PROMPT, "deepseek")

        self.assertTrue(allowed)
        self.assertEqual(state, "failed_retryable")


class BridgeModelsJsonTests(unittest.TestCase):
    class _FakeDriver:
        def __init__(self, name: str):
            self.name = name

        def models(self, **kwargs):
            return {
                "ok": True,
                "endpoint": self.name,
                "models": ["default"],
                "live": bool(kwargs.get("live")),
            }

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bridge.main(argv, driver_factory=self._FakeDriver)
        return rc, json.loads(buf.getvalue())

    def test_models_accepts_the_json_flag(self):
        args = bridge.build_parser().parse_args(["models", "opencode", "--json"])
        self.assertEqual(args.command, "models")
        self.assertEqual(args.endpoint, "opencode")
        self.assertTrue(args.as_json)
        self.assertFalse(bridge.build_parser().parse_args(["models", "opencode"]).as_json)

    def test_json_flag_does_not_change_the_payload(self):
        rc_json, payload_json = self._run(["models", "opencode", "--json"])
        rc_plain, payload_plain = self._run(["models", "opencode"])

        self.assertEqual(rc_json, 0)
        self.assertEqual(rc_plain, 0)
        self.assertEqual(payload_json, payload_plain)
        self.assertEqual(payload_json["endpoint"], "opencode")
        self.assertTrue(payload_json["ok"])

    def test_json_flag_still_forwards_live(self):
        rc, payload = self._run(["models", "grok", "--json", "--live"])
        self.assertEqual(rc, 0)
        self.assertTrue(payload["live"])


class OpenCodeWindowlessTests(unittest.TestCase):
    def _driver(self, tmp: str, **kwargs):
        kwargs.setdefault("bridge_config", dict(BRIDGE_CFG))
        kwargs.setdefault("ledger_path", Path(tmp) / "ledger.json")
        kwargs.setdefault("evidence_dir", Path(tmp) / "fail")
        kwargs.setdefault("sleep_fn", lambda _seconds: None)
        kwargs.setdefault("db_path", Path(tmp) / "opencode.db")
        kwargs.setdefault("cooldown_path", Path(tmp) / "cooldown.json")
        return opencode_mod.OpenCodeDriver(**kwargs)

    def test_windows_run_passes_create_no_window_and_devnull(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            proc = SimpleNamespace(returncode=0, stdout="reply\n", stderr="")
            with mock.patch.object(
                opencode_mod.subprocess, "run", return_value=proc
            ) as runner, mock.patch.object(
                opencode_mod, "os", SimpleNamespace(name="nt")
            ):
                rc, out, err = drv._run_cli(["opencode.exe", "--prompt", "x"], 5)

        kwargs = runner.call_args.kwargs
        self.assertEqual(rc, 0)
        self.assertEqual(out, "reply\n")
        self.assertEqual(err, "")
        self.assertEqual(
            kwargs["creationflags"], opencode_mod.subprocess.CREATE_NO_WINDOW
        )
        self.assertIs(kwargs["stdin"], opencode_mod.subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 5)

    def test_non_windows_run_has_no_creationflags(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            proc = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                opencode_mod.subprocess, "run", return_value=proc
            ) as runner, mock.patch.object(
                opencode_mod, "os", SimpleNamespace(name="posix")
            ):
                drv._run_cli(["opencode"], 5)

        self.assertNotIn("creationflags", runner.call_args.kwargs)

    def test_injected_runner_bypasses_subprocess_entirely(self):
        seen: list[list[str]] = []

        def fake_runner(cmd, budget):
            seen.append(list(cmd))
            return 0, "ok\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp, cli_runner=fake_runner)
            with mock.patch.object(
                opencode_mod.opencode_db, "current_max_seq", return_value=0
            ), mock.patch.object(
                opencode_mod.subprocess, "run"
            ) as runner:
                res = drv.ask(PROMPT, timeout=5, force=True)

        self.assertTrue(res["ok"])
        self.assertEqual(len(seen), 1)
        runner.assert_not_called()


class OpenCodeQuotaTests(unittest.TestCase):
    def _driver(self, tmp: str, runner, **kwargs):
        kwargs.setdefault("bridge_config", dict(BRIDGE_CFG))
        kwargs.setdefault("ledger_path", Path(tmp) / "ledger.json")
        kwargs.setdefault("evidence_dir", Path(tmp) / "fail")
        kwargs.setdefault("sleep_fn", lambda _seconds: None)
        kwargs.setdefault("db_path", Path(tmp) / "opencode.db")
        kwargs.setdefault("cooldown_path", Path(tmp) / "cooldown.json")
        return opencode_mod.OpenCodeDriver(cli_runner=runner, **kwargs)

    def test_hint_detection_ignores_unrelated_numbers(self):
        self.assertEqual(
            opencode_mod.detect_quota_exhaustion("Error: rate limit exceeded"),
            "rate limit",
        )
        self.assertEqual(opencode_mod.detect_quota_exhaustion("", "HTTP 429"), "429")
        self.assertEqual(opencode_mod.detect_quota_exhaustion("status 402"), "402")
        # A named phrase beats the numeric code it usually travels with.
        self.assertEqual(
            opencode_mod.detect_quota_exhaustion("402 payment required"),
            "payment required",
        )
        self.assertEqual(opencode_mod.detect_quota_exhaustion("token count 4290"), "")
        self.assertEqual(opencode_mod.detect_quota_exhaustion("normal answer"), "")

    def test_quota_failure_is_structured_and_marks_cooldown(self):
        def quota_runner(cmd, budget):
            return 1, "", "HTTP 429 Too Many Requests: rate limit reached"

        with tempfile.TemporaryDirectory() as tmp:
            cooldown = Path(tmp) / "cooldown.json"
            drv = self._driver(tmp, quota_runner)
            with mock.patch.object(
                opencode_mod.opencode_db, "current_max_seq", return_value=0
            ), mock.patch.object(
                opencode_mod.opencode_db, "read_latest_reply", return_value=None
            ):
                started = time.monotonic()
                res = drv.ask(PROMPT, timeout=300, force=True)
                elapsed = time.monotonic() - started
            entry = drv.ledger.get(PROMPT, "opencode")
            marker = json.loads(cooldown.read_text(encoding="utf-8"))
            retry_allowed, _ = drv.ledger.check(PROMPT, "opencode")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "quota_exhausted")
        self.assertIn(res["matched"], opencode_mod.QUOTA_HINTS + opencode_mod.QUOTA_CODES)
        self.assertEqual(res["returncode"], 1)
        self.assertEqual(res["retry_after_s"], 300)
        self.assertTrue(res["cooldown_until"])
        self.assertIn("quota", res["message"])
        self.assertLess(elapsed, 5.0, "a quota hit must fail fast, not burn the budget")
        self.assertEqual(marker["hint"], res["matched"])
        self.assertEqual(marker["endpoint"], "opencode")
        self.assertEqual(marker["until"], res["cooldown_until"])
        self.assertEqual(entry["state"], "failed_retryable")
        self.assertEqual(entry["note"], "quota_exhausted")
        self.assertNotEqual(entry["state"], "unknown", "quota means nothing was sent")
        self.assertTrue(retry_allowed)

    def test_cooldown_marker_is_readable_and_absent_when_never_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp, lambda cmd, budget: (0, "fine\n", ""))
            self.assertEqual(drv.cooldown(), {})

    def test_plain_cli_failure_keeps_the_old_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp, lambda cmd, budget: (2, "", "boom"))
            with mock.patch.object(
                opencode_mod.opencode_db, "current_max_seq", return_value=0
            ), mock.patch.object(
                opencode_mod.opencode_db, "read_latest_reply", return_value=None
            ):
                res = drv.ask(PROMPT, timeout=5, force=True)
            entry = drv.ledger.get(PROMPT, "opencode")

        self.assertEqual(res["error"], "cli_failed")
        self.assertEqual(res["returncode"], 2)
        self.assertEqual(entry["note"], "rc=2")


if __name__ == "__main__":
    unittest.main()
