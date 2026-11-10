
from offline_guard import ensure_offline
ensure_offline()

import json
import tempfile
import unittest
from pathlib import Path

from qi import runlog


class FailureCausePreservation(unittest.TestCase):
    def test_note_only_survives_actual_record_for_each_desktop_endpoint(self):
        for endpoint in ("codex", "dsh", "opencode"):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                runlog.record(endpoint, {"ok": False, "stage": "list", "note": "NOTE-MUST-SURVIVE"}, root=Path(tmp))
                rows = [json.loads(line) for path in Path(tmp).glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["error"], "NOTE-MUST-SURVIVE")

    def test_priority_is_hint_then_error_then_note(self):
        value = {"ok": False, "hint": "hint", "error": "error", "note": "note"}
        self.assertEqual(runlog.failure_note(value), "hint")
        value["hint"] = "  "
        self.assertEqual(runlog.failure_note(value), "error")
        value["error"] = None
        self.assertEqual(runlog.failure_note(value), "note")

    def test_success_never_carries_stale_failure_text(self):
        self.assertIsNone(runlog.failure_note({"ok": True, "hint": "old", "error": "old", "note": "old"}))

    def test_blank_nonstring_and_stage_fallback_are_explicit(self):
        self.assertEqual(runlog.failure_note({"ok": False, "note": 42}), "42")
        self.assertEqual(runlog.failure_note({"ok": False, "note": "  original  "}), "  original  ")
        self.assertIn("unknown", runlog.failure_note({"ok": False, "note": " "}))
