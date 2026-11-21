"""Offline unit coverage for scripts/auto_heal_loop.py.

No real bridge.py, git mutations, or GUI calls: subprocesses and bridge calls
are mocked. These tests are safe to run on a developer machine.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_heal_loop as ahl  # noqa: E402


class TestRunCmd(unittest.TestCase):
    def test_timeout_kills_whole_process_tree(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["slow.exe"], 1, output="partial output"),
            ("", None),
        ]
        with mock.patch.object(ahl.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(
                    ahl.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout="")
                ) as killer:
            rc, output = ahl.run_cmd(["slow.exe"], timeout=1, cwd="C:/tmp")

        self.assertEqual(rc, ahl.TIMEOUT_RC)
        self.assertIn("partial output", output)
        self.assertIn("taskkill rc=0", output)
        killer.assert_called_once()
        self.assertEqual(
            killer.call_args.args[0],
            ["taskkill", "/F", "/T", "/PID", "4242"],
        )
        self.assertEqual(popen.call_args.args[0], ["slow.exe"])
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["cwd"], "C:/tmp")


class TestCrashReport(unittest.TestCase):
    def test_report_keeps_full_traceback_git_state_and_test_source(self):
        failing = (
            "FAIL: test_broken (tests.test_demo.Demo)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_demo.py", line 3, in test_broken\n'
            "    self.fail('boom')\n"
            "AssertionError: boom\n"
        )
        source = (
            "import unittest\n"
            "\n"
            "class Demo(unittest.TestCase):\n"
            "    def test_broken(self):\n"
            "        self.fail('boom')\n"
            "\n"
            "    def test_next(self):\n"
            "        pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_demo.py").write_text(source, encoding="utf-8")
            replies = {
                "status": " M tests/test_demo.py",
                "diff": "diff --git a/tests/test_demo.py b/tests/test_demo.py\n+line",
            }
            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "head_commit", return_value="abcdef1234"), \
                    mock.patch.object(ahl, "log"), \
                    mock.patch.object(
                        ahl, "git_text",
                        side_effect=lambda *args: replies.get(args[0], "")
                    ):
                path = ahl.build_crash_report(
                    failing, root / "runs" / "crash_report.md"
                )
            content = path.read_text(encoding="utf-8")

        for needle in (
            "FAIL: test_broken (tests.test_demo.Demo)",
            "Traceback (most recent call last):",
            "AssertionError: boom",
            "git status --short",
            " M tests/test_demo.py",
            "git diff",
            "diff --git a/tests/test_demo.py",
            "def test_broken(self):",
            "self.fail('boom')",
            "abcdef1234",
        ):
            self.assertIn(needle, content)


class TestTaskCardExtraction(unittest.TestCase):
    def test_extracts_multiline_card_and_rejects_missing_markers(self):
        reply = (
            "analysis that must not be forwarded\n"
            "<<<WORKBUDDY_TASK_START>>>\n"
            "step 1: edit engine/base.py\n"
            "step 2: run full unittest\n"
            "<<<WORKBUDDY_TASK_END>>>\n"
            "trailing chatter\n"
        )
        self.assertEqual(
            ahl.extract_task_card(reply),
            "step 1: edit engine/base.py\nstep 2: run full unittest",
        )
        self.assertEqual(ahl.extract_task_card("no markers here"), "")
        self.assertTrue(ahl.TASK_RE.flags & re.S)


class TestGitGate(unittest.TestCase):
    BASE = "0123456789abcdef"

    def _fake_git(self, calls):
        def fake(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(
                    returncode=0, stdout=self.BASE + "\n", stderr=""
                )
            if args == ("status", "--porcelain"):
                return SimpleNamespace(returncode=0, stdout=" M engine/base.py", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake

    def test_three_red_rounds_reset_to_base_and_never_commit(self):
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "git", side_effect=self._fake_git(calls)), \
                    mock.patch.object(ahl, "log"), \
                    mock.patch.object(
                        ahl, "run_tests",
                        return_value=(1, "FAIL: test_broken (tests.test_demo.Demo)")
                    ) as tests, \
                    mock.patch.object(
                        ahl, "build_crash_report",
                        return_value=Path(tmp) / "crash_report.md"
                    ), \
                    mock.patch.object(ahl, "call_gemini", return_value="no markers") as gem, \
                    mock.patch.object(
                        ahl, "extract_task_card", return_value="task card"
                    ), \
                    mock.patch.object(ahl, "call_workbuddy", return_value="ok") as wb:
                rc = ahl.heal(max_retries=3, no_push=True, timeout=1)

        self.assertEqual(rc, 1)
        self.assertEqual(tests.call_count, 3)
        self.assertEqual(gem.call_count, 3)
        self.assertEqual(wb.call_count, 3)
        self.assertIn(("reset", "--hard", self.BASE), calls)
        self.assertFalse([item for item in calls if item[0] == "commit"])
        self.assertFalse([item for item in calls if item[0] == "push"])

    def test_green_suite_commits_and_respects_no_push(self):
        calls: list[tuple[str, ...]] = []
        # M4-7: run against a throwaway runs/ like every other test here, so a
        # green round cannot overwrite the real runs/state_machine.json of a
        # scheduler that is live at test time. Nothing about the assertions
        # changes — only where the state machine writes.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ahl, "RUNS", Path(tmp)), \
                mock.patch.object(ahl, "git", side_effect=self._fake_git(calls)), \
                mock.patch.object(ahl, "log"), \
                mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                mock.patch.object(ahl, "build_crash_report") as report:
            rc = ahl.heal(max_retries=3, no_push=True, timeout=1)
        self.assertEqual(rc, 0)
        report.assert_not_called()
        self.assertIn(("add", "-A"), calls)
        self.assertTrue([item for item in calls if item[0] == "commit"])
        self.assertFalse([item for item in calls if item[0] == "push"])


class TestOvernightLoop(unittest.TestCase):
    BASE = "abcdef0123456789"
    CARD = (
        "<<<WORKBUDDY_TASK_START>>>\n"
        "small step: fix one function and run full unittest\n"
        "<<<WORKBUDDY_TASK_END>>>"
    )

    def _fake_git(self, calls, *, dirty: bool = True):
        def fake(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(
                    returncode=0, stdout=self.BASE + "\n", stderr=""
                )
            if args == ("status", "--porcelain"):
                return SimpleNamespace(
                    returncode=0,
                    stdout=" M engine/base.py" if dirty else "",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return fake

    def test_multiple_green_rounds_schedule_tasks_and_commit(self):
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "ROOT", Path(tmp)), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(ahl, "git", side_effect=self._fake_git(calls)), \
                    mock.patch.object(ahl.time, "sleep") as sleeper, \
                    mock.patch.object(ahl, "clear_pid"), \
                    mock.patch.object(
                        ahl, "run_tests",
                        side_effect=[(0, "OK"), (0, "OK"), (0, "OK"), (0, "OK")]
                    ) as tests, \
                    mock.patch.object(
                        ahl, "load_backlog", return_value=("todo item", "test-todo")
                    ) as backlog, \
                    mock.patch.object(
                        ahl, "request_task_from_gemini",
                        side_effect=[self.CARD, self.CARD]
                    ) as ask, \
                    mock.patch.object(ahl, "call_workbuddy", return_value="done") as wb, \
                    mock.patch.object(ahl, "heal") as healer, \
                    mock.patch.object(ahl, "commit_and_push", return_value=0) as gate:
                rc = ahl.run_overnight(max_rounds=2, no_push=True, sleep=0)

            self.assertEqual(rc, 0)
            self.assertEqual(tests.call_count, 4)
            self.assertEqual(backlog.call_count, 2)
            self.assertEqual(ask.call_count, 2)
            self.assertEqual(wb.call_count, 2)
            self.assertEqual(gate.call_count, 2)
            healer.assert_not_called()
            self.assertEqual(sleeper.call_count, 1)
            self.assertEqual(
                (runs / ahl.PID_FILE).read_text(encoding="utf-8"),
                str(os.getpid()),
            )
            overnight_log = (runs / ahl.OVERNIGHT_LOG).read_text(encoding="utf-8")
            self.assertIn("overnight start", overnight_log)
            self.assertIn("BASE_COMMIT=" + self.BASE[:12], overnight_log)
            self.assertIn("gate green, committed", overnight_log)

    def test_red_round_is_rolled_back_and_next_round_still_runs(self):
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "ROOT", Path(tmp)), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(
                        ahl, "git",
                        side_effect=self._fake_git(calls, dirty=False)
                    ), \
                    mock.patch.object(ahl.time, "sleep") as sleeper, \
                    mock.patch.object(ahl, "clear_pid"), \
                    mock.patch.object(
                        ahl, "run_tests",
                        side_effect=[
                            (1, "FAIL: test_x (tests.test_demo.Demo)"),
                            (1, "FAIL: test_x (tests.test_demo.Demo)"),
                            (0, "OK"),
                            (0, "OK"),
                        ]
                    ), \
                    mock.patch.object(ahl, "heal", return_value=1) as healer, \
                    mock.patch.object(ahl, "reset_hard") as rollback, \
                    mock.patch.object(
                        ahl, "load_backlog", return_value=("todo", "test-todo")
                    ), \
                    mock.patch.object(
                        ahl, "request_task_from_gemini", return_value=self.CARD
                    ) as ask, \
                    mock.patch.object(ahl, "call_workbuddy", return_value="done"), \
                    mock.patch.object(ahl, "commit_and_push") as gate:
                rc = ahl.run_overnight(max_rounds=2, no_push=True, sleep=1)

            self.assertEqual(rc, 0)
            healer.assert_called_once()
            rollback.assert_called_once_with(self.BASE)
            ask.assert_called_once()
            gate.assert_not_called()
            sleeper.assert_called_once_with(1)
            overnight_log = (runs / ahl.OVERNIGHT_LOG).read_text(encoding="utf-8")
            self.assertIn("self-heal failed", overnight_log)
            self.assertIn("no commit", overnight_log)

    def test_three_idle_rounds_stop_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "ROOT", Path(tmp)), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(
                        ahl, "git", side_effect=self._fake_git([], dirty=False)
                    ), \
                    mock.patch.object(ahl.time, "sleep"), \
                    mock.patch.object(ahl, "clear_pid"), \
                    mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                    mock.patch.object(
                        ahl, "load_backlog", return_value=("todo", "test-todo")
                    ), \
                    mock.patch.object(
                        ahl, "request_task_from_gemini", return_value="no markers"
                    ) as ask, \
                    mock.patch.object(ahl, "call_workbuddy") as wb, \
                    mock.patch.object(ahl, "commit_and_push") as gate:
                rc = ahl.run_overnight(max_rounds=20, no_push=True, sleep=0)

            self.assertEqual(rc, 0)
            self.assertEqual(ask.call_count, 3)
            wb.assert_not_called()
            gate.assert_not_called()
            overnight_log = (runs / ahl.OVERNIGHT_LOG).read_text(encoding="utf-8")
            self.assertIn("THREE_IDLE_ROUNDS", overnight_log)


class TestSchedulerNotice(unittest.TestCase):
    def test_call_workbuddy_writes_notice_with_current_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(
                        ahl, "run_cmd",
                        return_value=(0, '{"ok": true, "reply": "ok"}'),
                    ), \
                    mock.patch.object(ahl, "log"):
                ahl.call_workbuddy("step one: patch code", 9, 60)
            text = (Path(tmp) / "workbuddy_task_round9.md").read_text(encoding="utf-8")

        self.assertTrue(text.startswith(ahl.scheduler_notice()))
        self.assertIn("PID=%d" % os.getpid(), text)
        self.assertIn("step one: patch code", text)


if __name__ == "__main__":
    unittest.main()
