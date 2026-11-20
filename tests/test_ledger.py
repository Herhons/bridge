"""Unit tests for engine/ledger.py: sha256 dedup, 6 states, fail-closed blocking, and clear."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.base import exit_code_for
from engine.ledger import STATES, SubmissionLedger


class TestSubmissionLedger(unittest.TestCase):
    def test_six_states_and_fail_closed_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SubmissionLedger(Path(tmp) / "ledger.json")
            self.assertEqual(
                STATES,
                ("draft", "submitting", "accepted", "unknown", "failed_retryable", "failed_terminal"),
            )

            allowed, state = ledger.check("hello", "deepseek")
            self.assertTrue(allowed)
            self.assertEqual(state, "draft")

            # Blocking states: submitting, accepted, unknown
            for blocking in ("submitting", "accepted", "unknown"):
                ledger.mark("hello", "deepseek", blocking)
                allowed, state = ledger.check("hello", "deepseek")
                self.assertFalse(allowed, f"{blocking} must block")
                self.assertEqual(state, blocking)

            # Retryable / terminal failure allows retry or new check
            ledger.mark("hello", "deepseek", "failed_retryable")
            allowed, state = ledger.check("hello", "deepseek")
            self.assertTrue(allowed)
            self.assertEqual(state, "failed_retryable")

            # Different endpoint with same prompt has distinct key
            ledger.mark("hello", "deepseek", "accepted")
            allowed_grok, _ = ledger.check("hello", "grok")
            self.assertTrue(allowed_grok)

    def test_clear_single_and_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SubmissionLedger(Path(tmp) / "ledger.json")
            ledger.mark("p1", "deepseek", "accepted")
            ledger.mark("p2", "grok", "unknown")
            k1 = ledger.key("p1", "deepseek")
            self.assertEqual(ledger.clear(k1), 1)
            self.assertTrue(ledger.check("p1", "deepseek")[0])
            self.assertFalse(ledger.check("p2", "grok")[0])
            self.assertEqual(ledger.clear(), 1)
            self.assertEqual(ledger.list_entries(), {})

    def test_duplicate_exit_code_is_6(self):
        self.assertEqual(exit_code_for({"ok": False, "error": "duplicate"}), 6)


if __name__ == "__main__":
    unittest.main()
