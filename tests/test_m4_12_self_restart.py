"""Offline coverage for M4-12: self-restart hot restart + brain --force.

Tests:
1. detect_self_modified_files hit on watched paths with path normalization.
2. detect_self_modified_files miss and edge cases (same commit, diff error, unwatched files).
3. build_self_restart_argv construction, flags and bounds clamping.
4. run_overnight triggers os.execv self-restart when self-modified and rounds remain.
5. run_overnight does not trigger self-restart on the final round.
6. _ask_brain_endpoint includes --force flag in the bridge CLI invocation.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402


class SelfRestartHelperTests(unittest.TestCase):
    def test_detect_self_modified_files_hit(self):
        diff_output = (
            "README.md\n"
            "scripts\\auto_heal_loop.py\n"
            "engine/daemon_state.py\n"
            "tests/test_foo.py\n"
        )
        with mock.patch.object(
            ahl, "git", return_value=SimpleNamespace(returncode=0, stdout=diff_output, stderr="")
        ) as mock_git:
            matched = ahl.detect_self_modified_files("0123456789ab", "abcdef012345")

        mock_git.assert_called_once_with("diff", "--name-only", "0123456789ab..abcdef012345")
        self.assertEqual(
            matched,
            ["scripts/auto_heal_loop.py", "engine/daemon_state.py"],
        )

    def test_detect_self_modified_files_ignore_and_edge_cases(self):
        # 1. base == head
        self.assertEqual(ahl.detect_self_modified_files("abcdef012345", "abcdef012345"), [])
        # 2. empty base or head
        self.assertEqual(ahl.detect_self_modified_files("", "abcdef012345"), [])
        self.assertEqual(ahl.detect_self_modified_files("abcdef012345", ""), [])
        # 3. git diff error
        with mock.patch.object(
            ahl, "git", return_value=SimpleNamespace(returncode=128, stdout="", stderr="fatal")
        ):
            self.assertEqual(ahl.detect_self_modified_files("0123456789ab", "abcdef012345"), [])
        # 4. diff has only unwatched files
        with mock.patch.object(
            ahl, "git", return_value=SimpleNamespace(returncode=0, stdout="engine/server.py\nbridge.py\n", stderr="")
        ):
            self.assertEqual(ahl.detect_self_modified_files("0123456789ab", "abcdef012345"), [])

    def test_build_self_restart_argv(self):
        argv = ahl.build_self_restart_argv(
            remaining_rounds=4,
            no_push=True,
            timeout=180.0,
            sleep=25.0,
            executor="workbuddy",
            brain="deepseek",
            executor_profile="routine",
            stop_on_bug=True,
        )
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], str(Path(ahl.__file__).resolve()))
        self.assertIn("--overnight", argv)
        self.assertIn("--no-push", argv)
        self.assertIn("--stop-on-bug", argv)

        def _val(flag: str) -> str:
            idx = argv.index(flag)
            return argv[idx + 1]

        self.assertEqual(_val("--max-rounds"), "4")
        self.assertEqual(_val("--timeout"), "180.0")
        self.assertEqual(_val("--sleep"), "25.0")
        self.assertEqual(_val("--delay-start"), "0")
        self.assertEqual(_val("--executor"), "workbuddy")
        self.assertEqual(_val("--brain"), "deepseek")
        self.assertEqual(_val("--executor-profile"), "routine")

        # Bounds: remaining_rounds <= 0 clamps to 1, no-push/stop-on-bug omitted when False
        argv_minimal = ahl.build_self_restart_argv(
            remaining_rounds=-2,
            no_push=False,
            timeout=60.0,
            sleep=10.0,
            executor="auto",
            brain="auto",
            executor_profile="none",
            stop_on_bug=False,
        )
        self.assertEqual(argv_minimal[argv_minimal.index("--max-rounds") + 1], "1")
        self.assertNotIn("--no-push", argv_minimal)
        self.assertNotIn("--stop-on-bug", argv_minimal)


class SelfRestartOvernightTests(unittest.TestCase):
    def test_run_overnight_triggers_self_restart_via_execv(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name)
        runs = root / "runs"
        runs.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "ROADMAP.md").write_text("# roadmap\n- [ ] Task 1\n", encoding="utf-8")

        with mock.patch.object(ahl, "ROOT", root), \
                mock.patch.object(ahl, "RUNS", runs), \
                mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                mock.patch.object(ahl, "head_commit", side_effect=["0123456789ab", "abcdef012345"]), \
                mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                mock.patch.object(ahl, "load_backlog", return_value=("- [ ] Task 1", "docs/ROADMAP.md")), \
                mock.patch.object(ahl, "request_task_from_gemini", return_value="<<<WORKBUDDY_TASK_START>>>\ndo something\n<<<WORKBUDDY_TASK_END>>>"), \
                mock.patch.object(ahl, "execute_brain_reply", return_value={"mode": "task", "applied": False, "task": "do something", "executor_reply": "done"}), \
                mock.patch.object(ahl, "has_changes", return_value=True), \
                mock.patch.object(ahl, "commit_and_push", return_value=0), \
                mock.patch.object(ahl, "detect_self_modified_files", return_value=["scripts/auto_heal_loop.py"]) as mock_detect, \
                mock.patch.object(ahl, "clear_pid") as mock_clear_pid, \
                mock.patch.object(ahl.os, "execv") as mock_execv:
            rc = ahl.run_overnight(
                max_rounds=3,
                no_push=True,
                timeout=120.0,
                sleep=5.0,
                executor="workbuddy",
                brain="gemini",
                executor_profile="test",
                stop_on_bug=True,
            )

        self.assertEqual(rc, 0)
        mock_detect.assert_called_once_with("0123456789ab", "abcdef012345")
        mock_execv.assert_called_once()
        exec_argv = mock_execv.call_args[0][1]
        self.assertEqual(exec_argv[0], sys.executable)
        self.assertEqual(exec_argv[exec_argv.index("--max-rounds") + 1], "2")
        self.assertIn("--no-push", exec_argv)
        self.assertIn("--stop-on-bug", exec_argv)

        mock_clear_pid.assert_called_once()
        state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "SELF_RESTART")
        self.assertIn("self-modified: scripts/auto_heal_loop.py", state["reason"])

    def test_run_overnight_does_not_trigger_restart_on_final_round(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name)
        runs = root / "runs"
        runs.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "ROADMAP.md").write_text("# roadmap\n- [ ] Task 1\n", encoding="utf-8")

        with mock.patch.object(ahl, "ROOT", root), \
                mock.patch.object(ahl, "RUNS", runs), \
                mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                mock.patch.object(ahl, "head_commit", return_value="0123456789ab"), \
                mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                mock.patch.object(ahl, "load_backlog", return_value=("- [ ] Task 1", "docs/ROADMAP.md")), \
                mock.patch.object(ahl, "request_task_from_gemini", return_value="<<<WORKBUDDY_TASK_START>>>\ndo something\n<<<WORKBUDDY_TASK_END>>>"), \
                mock.patch.object(ahl, "execute_brain_reply", return_value={"mode": "task", "applied": False, "task": "do something", "executor_reply": "done"}), \
                mock.patch.object(ahl, "has_changes", return_value=True), \
                mock.patch.object(ahl, "commit_and_push", return_value=0), \
                mock.patch.object(ahl, "detect_self_modified_files", return_value=["scripts/auto_heal_loop.py"]) as mock_detect, \
                mock.patch.object(ahl.os, "execv") as mock_execv:
            rc = ahl.run_overnight(
                max_rounds=1,
                no_push=True,
                timeout=120.0,
                sleep=0.0,
            )

        self.assertEqual(rc, 0)
        mock_execv.assert_not_called()
        mock_detect.assert_not_called()
        state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "STOPPED")
        self.assertEqual(state["reason"], "MAX_ROUNDS")


class BrainForceFlagTests(unittest.TestCase):
    def test_ask_brain_endpoint_includes_force_flag(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        runs = Path(tmpdir.name) / "runs"
        runs.mkdir()

        with mock.patch.object(ahl, "RUNS", runs), \
                mock.patch.object(ahl, "run_cmd", return_value=(0, '{"ok": true, "reply": "pong"}')) as mock_run:
            rc, reply = ahl._ask_brain_endpoint("gemini", "ping", 1, 60.0, attach=None)

        self.assertEqual(rc, 0)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--force", cmd)
        self.assertIn("--profile", cmd)
        self.assertIn("test", cmd)
        self.assertIn("--headed", cmd)
        self.assertIn("--timeout", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("--save", cmd)


if __name__ == "__main__":
    unittest.main()
