"""Verifier protocol tests. Every subprocess call is mocked; no UI is opened."""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import ast
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_all", ROOT / "tools" / "verify_all.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def invoke_main(self, rows, folder, random_count=0):
        args = ["verify_all.py", "--only", "grok", "--output-dir", str(folder),
                "--random", str(random_count)]
        with patch.object(sys, "argv", args), \
                patch.dict(verifier.VERIFIERS, {"grok": lambda: list(rows)}), \
                contextlib.redirect_stdout(io.StringIO()):
            return verifier.main()

    def test_failed_mode_never_runs_dependent_model_or_effort(self):
        for name, check in (("gemini", verifier.verify_gemini),
                            ("chatgpt", verifier.verify_chatgpt)):
            with self.subTest(site=name), patch.object(verifier, "run") as run:
                run.return_value = (1, {"ok": False, "stage": "writer_not_found"}, "")
                rows = check()
                self.assertTrue(rows)
                self.assertFalse(any(row["ok"] for row in rows))
                self.assertEqual([call.args[1][0] for call in run.call_args_list],
                                 ["--writer", "--writer"])
                self.assertTrue(all(row["raw"]["stage"] == "writer_not_selected"
                                    for row in rows if row["kind"] != "writer"))

    def test_wrong_mode_readback_stops_combination(self):
        with patch.object(verifier, "run", return_value=(0, {"ok": True, "after": "dialog"}, "")) as run:
            rows = verifier.verify_mode("gemini", "task", ["argon"])
        self.assertEqual(run.call_count, 1)
        self.assertFalse(any(row["ok"] for row in rows))

    def test_wrong_effort_readback_fails(self):
        with patch.object(verifier, "run", side_effect=[
            (0, {"ok": True, "after": "work"}, ""),
            (0, {"ok": True, "after": "low"}, ""),
        ]):
            rows = verifier.verify_mode("chatgpt", "work", [], ["max"])
        self.assertTrue(rows[0]["ok"])
        self.assertFalse(rows[1]["ok"])
        self.assertEqual(rows[1]["target"], "work+max")

    def test_failed_suite_returns_nonzero_and_records_failure(self):
        rows = [verifier.rec("model", "fast", 1, {}, False)]
        with tempfile.TemporaryDirectory() as folder:
            rc = self.invoke_main(rows, folder)
            summary = json.loads((Path(folder) / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(summary["grok"]["failed"], ["fast"])

    def test_successful_suite_returns_zero(self):
        rows = [verifier.rec("model", "fast", 0, {"ok": True, "after": "fast"}, True)]
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.invoke_main(rows, folder), 0)

    def test_random_rerun_includes_failed_cases(self):
        rows = [verifier.rec("model", "fast", 1, {}, False)]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(verifier, "run", return_value=(0, {"ok": True, "after": "fast"}, "")) as run:
            rc = self.invoke_main(rows, folder, random_count=1)
            stored = json.loads((Path(folder) / "verify-grok.json").read_text(encoding="utf-8"))
        self.assertEqual(run.call_args.args[1], ["--model", "fast"])
        self.assertEqual([row["kind"] for row in stored], ["model", "random"])
        self.assertEqual(rc, 1)  # A rerun does not erase the original failed result.

    def test_random_effort_uses_effort_flag_and_establishes_mode(self):
        row = verifier.rec("effort", "work+max", 0, {"ok": True, "after": "max"}, True)
        with tempfile.TemporaryDirectory() as folder:
            args = ["verify_all.py", "--only", "chatgpt", "--random", "1", "--output-dir", folder]
            with patch.object(sys, "argv", args), \
                    patch.dict(verifier.VERIFIERS, {"chatgpt": lambda: [row]}), \
                    patch.object(verifier, "run", side_effect=[
                        (0, {"ok": True, "after": "work"}, ""),
                        (0, {"ok": True, "after": "max"}, ""),
                    ]) as run, contextlib.redirect_stdout(io.StringIO()):
                rc = verifier.main()
        self.assertEqual([call.args[1] for call in run.call_args_list],
                         [["--writer", "work"], ["--effort", "max"]])
        self.assertEqual(rc, 0)

    def test_default_run_keeps_historical_json(self):
        row = verifier.rec("model", "fast", 0, {"ok": True, "after": "fast"}, True)
        with tempfile.TemporaryDirectory() as folder:
            deliverables = Path(folder) / "deliverables"
            deliverables.mkdir()
            historical = deliverables / "verify-grok.json"
            historical.write_text("historical evidence", encoding="utf-8")
            with patch.object(verifier, "ROOT", folder), \
                    patch.object(sys, "argv", ["verify_all.py", "--only", "grok"]), \
                    patch.dict(verifier.VERIFIERS, {"grok": lambda: [row]}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verifier.main(), 0)
            self.assertEqual(historical.read_text(encoding="utf-8"), "historical evidence")
            self.assertEqual(len(list(deliverables.glob("verification/*/summary.json"))), 1)

    def test_malformed_success_output_is_protocol_failure(self):
        process = SimpleNamespace(returncode=0, stdout="not JSON")
        with patch.object(verifier.subprocess, "run", return_value=process):
            rc, data, _ = verifier.run("unused.py", [], 1)
        self.assertNotEqual(rc, 0)
        self.assertEqual(data["stage"], "invalid_output")

    def test_effort_cases_match_current_script_options(self):
        tree = ast.parse((ROOT / "src/scripts/chatgpt_choose.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "EFFORTS"
                            for target in node.targets))
        actual = ast.literal_eval(node.value)
        self.assertEqual(verifier.CHATGPT_EFFORTS,
                         {writer: list(options) for writer, options in actual.items()})


if __name__ == "__main__":
    unittest.main()
