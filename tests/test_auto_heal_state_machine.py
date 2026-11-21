"""Offline coverage for the v0.2.5 self-iteration state machine.

Everything here is mocked: no bridge.py, no git mutation, no GUI call. The
suite pins the new contracts (brain fallback, attach degradation, executor
profile, stop-on-bug, direct patches, persisted state) without touching a
real desktop.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_heal_loop as ahl  # noqa: E402

BASE = "0123456789abcdef0123456789abcdef01234567"
CARD = (
    "<<<WORKBUDDY_TASK_START>>>\n"
    "step 1: patch engine/base.py\n"
    "step 2: run the full unittest suite\n"
    "<<<WORKBUDDY_TASK_END>>>"
)
PATCH = (
    "<<<BRIDGE_PATCH_START>>>\n"
    "diff --git a/engine/base.py b/engine/base.py\n"
    "--- a/engine/base.py\n"
    "+++ b/engine/base.py\n"
    "@@ -1 +1 @@\n"
    "-x\n"
    "+y\n"
    "<<<BRIDGE_PATCH_END>>>"
)
FENCED_PATCH = (
    "<<<BRIDGE_PATCH_START>>>\n"
    "```diff\n"
    "diff --git a/engine/base.py b/engine/base.py\n"
    "+y\n"
    "```\n"
    "<<<BRIDGE_PATCH_END>>>"
)


def _payload(reply: str) -> str:
    return json.dumps({"ok": True, "reply": reply}, ensure_ascii=False)


def _endpoint_of(cmd: list[str]) -> str:
    """bridge.py ask <endpoint> '' --file ... -> index 3."""
    return cmd[3]


class BrainChainTests(unittest.TestCase):
    def _fake_run_cmd(self, replies: dict[str, tuple[int, str]]):
        calls: list[list[str]] = []

        def fake(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return replies.get(_endpoint_of([str(c) for c in cmd]), (1, ""))

        return fake, calls

    def test_auto_brain_falls_back_gemini_deepseek_grok(self):
        fake, calls = self._fake_run_cmd({
            "gemini": (1, ""),
            "deepseek": (124, "timeout"),
            "grok": (0, _payload(CARD)),
        })
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                endpoint, reply = ahl.call_brain("prompt", 1, 120)

        self.assertEqual(endpoint, "grok")
        self.assertIn("<<<WORKBUDDY_TASK_START>>>", reply)
        self.assertEqual(
            [_endpoint_of(call) for call in calls],
            ["gemini", "deepseek", "grok"],
        )

    def test_empty_reply_without_marker_also_falls_back(self):
        fake, calls = self._fake_run_cmd({
            "gemini": (0, _payload("just chatter, no markers")),
            "deepseek": (0, _payload("")),
            "grok": (0, _payload(PATCH)),
        })
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                endpoint, reply = ahl.call_brain("prompt", 2, 120)

        self.assertEqual(endpoint, "grok")
        self.assertEqual(len(calls), 3)
        self.assertIn("<<<BRIDGE_PATCH_START>>>", reply)

    def test_explicit_brain_never_falls_back(self):
        fake, calls = self._fake_run_cmd({
            "gemini": (0, _payload(CARD)),
            "deepseek": (0, _payload("")),
            "grok": (0, _payload(PATCH)),
        })
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                endpoint, reply = ahl.call_brain("prompt", 3, 120, brain="deepseek")

        self.assertEqual(endpoint, "")
        self.assertEqual(reply, "")
        self.assertEqual([_endpoint_of(call) for call in calls], ["deepseek"])

    def test_unknown_brain_is_rejected(self):
        with self.assertRaises(ValueError):
            ahl.call_brain("prompt", 1, 30, brain="chatgpt")


class AttachDegradeTests(unittest.TestCase):
    def test_grok_has_no_attach_button_and_inlines_text(self):
        self.assertFalse(ahl.endpoint_supports_attach("grok"))
        self.assertTrue(ahl.endpoint_supports_attach("gemini"))
        self.assertTrue(ahl.endpoint_supports_attach("deepseek"))

    def test_degraded_endpoint_gets_no_attach_flag_but_inlined_body(self):
        calls: list[list[str]] = []

        def fake(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return 0, _payload(CARD)

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            report = runs / "crash_report.md"
            report.write_text("STACK-TRACE-MARKER\n", encoding="utf-8")
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                endpoint, reply = ahl.call_brain(
                    "please fix", 4, 120, brain="grok", attach=report
                )
            cmd = calls[0]
            prompt_file = Path(cmd[cmd.index("--file") + 1])
            body = prompt_file.read_text(encoding="utf-8")

        self.assertEqual(endpoint, "grok")
        self.assertTrue(reply)
        self.assertEqual(_endpoint_of(cmd), "grok")
        self.assertNotIn("--attach", cmd)
        self.assertIn("please fix", body)
        self.assertIn("STACK-TRACE-MARKER", body)

    def test_capable_endpoint_still_uses_attach_flag(self):
        calls: list[list[str]] = []

        def fake(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return 0, _payload(CARD)

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            report = runs / "crash_report.md"
            report.write_text("x\n", encoding="utf-8")
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                ahl.call_brain("please fix", 5, 120, brain="gemini", attach=report)

        cmd = calls[0]
        self.assertIn("--attach", cmd)
        self.assertEqual(cmd[cmd.index("--attach") + 1], str(report))


class ExecutorProfileTests(unittest.TestCase):
    def _run(self, executor_profile):
        calls: list[list[str]] = []

        def fake(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return 0, _payload("done")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake), \
                    mock.patch.object(ahl, "log"):
                ahl.call_workbuddy(
                    "do the thing", 6, 60, executor_profile=executor_profile
                )
        return calls[0]

    def test_profile_none_omits_profile_flag(self):
        cmd = self._run("none")
        self.assertNotIn("--profile", cmd)
        self.assertIn("--headed", cmd)

    def test_named_profile_is_forwarded(self):
        cmd = self._run("routine")
        self.assertEqual(cmd[cmd.index("--profile") + 1], "routine")

    def test_default_profile_is_test(self):
        cmd = self._run("test")
        self.assertEqual(cmd[cmd.index("--profile") + 1], "test")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            ahl._ask_endpoint("workbuddy", "card", 1, 30, executor_profile="paid")


class StopOnBugTests(unittest.TestCase):
    def test_red_suite_reports_rolls_back_and_stops(self):
        calls: list[tuple[str, ...]] = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(returncode=0, stdout=BASE + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "git", side_effect=fake_git), \
                    mock.patch.object(ahl, "log"), \
                    mock.patch.object(
                        ahl, "run_tests",
                        return_value=(1, "FAIL: test_x (tests.test_demo.Demo)")
                    ) as tests, \
                    mock.patch.object(
                        ahl, "build_crash_report",
                        return_value=runs / "crash_report.md"
                    ), \
                    mock.patch.object(ahl, "build_handoff_bundle") as handoff, \
                    mock.patch.object(ahl, "call_gemini", return_value=PATCH) as brain, \
                    mock.patch.object(ahl, "call_executor") as executor, \
                    mock.patch.object(ahl, "reset_hard") as rollback:
                rc = ahl.heal(max_retries=3, no_push=True, timeout=1, stop_on_bug=True)

            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(tests.call_count, 1)
        brain.assert_called_once()
        handoff.assert_called_once()
        rollback.assert_called_once_with(BASE)
        executor.assert_not_called()
        self.assertEqual(state["state"], "STOPPED_ON_BUG")
        self.assertIn("stop-on-bug", state["reason"])
        self.assertIn("STOPPED_ON_BUG", [item["state"] for item in state["history"]])
        self.assertFalse([item for item in calls if item[0] == "commit"])


class PatchTests(unittest.TestCase):
    def test_extract_patch_strips_markers_and_fences(self):
        self.assertEqual(
            ahl.extract_patch(PATCH),
            "diff --git a/engine/base.py b/engine/base.py\n"
            "--- a/engine/base.py\n"
            "+++ b/engine/base.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y",
        )
        self.assertEqual(
            ahl.extract_patch(FENCED_PATCH),
            "diff --git a/engine/base.py b/engine/base.py\n+y",
        )
        self.assertEqual(ahl.extract_patch("no markers at all"), "")

    def test_direct_patch_wins_over_task_card(self):
        with mock.patch.object(ahl, "apply_unified_patch", return_value=(True, "applied")), \
                mock.patch.object(ahl, "call_executor") as executor, \
                mock.patch.object(ahl, "log"):
            result = ahl.execute_brain_reply(PATCH + "\n" + CARD, 1, 30)

        self.assertEqual(result["mode"], "patch")
        self.assertTrue(result["applied"])
        executor.assert_not_called()

    def test_rejected_patch_falls_back_to_executor(self):
        with mock.patch.object(
            ahl, "apply_unified_patch", return_value=(False, "does not apply")
        ), mock.patch.object(ahl, "call_executor", return_value="done") as executor, \
                mock.patch.object(ahl, "log"):
            result = ahl.execute_brain_reply(PATCH + "\n" + CARD, 2, 30)

        self.assertEqual(result["mode"], "task")
        self.assertFalse(result["applied"])
        self.assertEqual(result["executor_reply"], "done")
        self.assertIn("run the full unittest suite", result["task"])
        executor.assert_called_once()

    def test_apply_runs_check_before_apply(self):
        calls: list[tuple[str, ...]] = []

        def fake_git(*args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "git", side_effect=fake_git), \
                    mock.patch.object(ahl, "log"):
                ok, detail = ahl.apply_unified_patch(ahl.extract_patch(PATCH))

        self.assertTrue(ok)
        self.assertEqual(detail, "applied")
        self.assertEqual(calls[0][:2], ("apply", "--check"))
        self.assertEqual(calls[1][:2], ("apply", "--whitespace=nowarn"))


class StateMachineTests(unittest.TestCase):
    def test_transitions_are_persisted_with_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs):
                machine = ahl.StateMachine()
                machine.set("PICK_TASK", reason="todo", round_no=1)
                machine.set("BRAIN_PLAN", reason="ask gemini", round_no=1)
                machine.set("STOPPED_ON_BUG", reason="boom", round_no=2)
                machine_path = machine.path
            self.assertEqual(machine_path, runs / ahl.STATE_FILE)
            data = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))

        self.assertEqual(data["state"], "STOPPED_ON_BUG")
        self.assertEqual(data["reason"], "boom")
        self.assertEqual(data["round"], 2)
        self.assertEqual(
            [item["state"] for item in data["history"]],
            ["PICK_TASK", "BRAIN_PLAN", "STOPPED_ON_BUG"],
        )
        self.assertIn("updated_at", data)

    def test_unknown_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)):
                machine = ahl.StateMachine()
                with self.assertRaises(ValueError):
                    machine.set("NOT_A_STATE")


class HandoffBundleTests(unittest.TestCase):
    def test_bundle_holds_everything_a_brain_needs(self):
        commits = "\n".join(
            "%07x feat: commit %d" % (0xabc000 + i, i) for i in range(8)
        )

        def fake_git_text(*args):
            key = args[:2]
            if key == ("rev-parse", "--abbrev-ref"):
                return "auto/v0.2.5\n"
            if key == ("log", "-n"):
                return commits + "\n"
            if key == ("status", "--short"):
                return " M scripts/auto_heal_loop.py\n"
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "git_text", side_effect=fake_git_text), \
                    mock.patch.object(ahl, "head_commit", return_value=BASE), \
                    mock.patch.object(
                        ahl, "load_backlog",
                        return_value=("[待办] split file_dialog", "runs/overnight_todo.md")
                    ), \
                    mock.patch.object(ahl, "log"):
                path = ahl.build_handoff_bundle("unittest red rc=1")
            content = path.read_text(encoding="utf-8")

        self.assertEqual(path, runs / ahl.HANDOFF_FILE)
        for needle in (
            str(root),
            "auto/v0.2.5",
            BASE,
            "unittest red rc=1",
            "严禁删除、跳过或弱化任何既有单测断言。",
            "feat: commit 0",
            "feat: commit 7",
            " M scripts/auto_heal_loop.py",
            "[待办] split file_dialog",
            "最近 8 条 commit",
        ):
            self.assertIn(needle, content)


class CliTests(unittest.TestCase):
    def test_new_flags_are_parsed(self):
        args = ahl.build_parser().parse_args([
            "--overnight",
            "--brain", "deepseek",
            "--executor-profile", "none",
            "--stop-on-bug",
            "--delay-start", "30",
            "--sleep", "60",
            "--timeout", "600",
        ])
        self.assertTrue(args.overnight)
        self.assertEqual(args.brain, "deepseek")
        self.assertEqual(args.executor_profile, "none")
        self.assertTrue(args.stop_on_bug)
        self.assertEqual(args.delay_start, 30)
        self.assertEqual(args.sleep, 60)
        self.assertEqual(args.timeout, 600)

    def test_defaults_stay_compatible(self):
        args = ahl.build_parser().parse_args([])
        self.assertEqual(args.brain, "auto")
        self.assertEqual(args.executor_profile, "test")
        self.assertFalse(args.stop_on_bug)
        self.assertEqual(args.max_retries, 3)

    def test_overnight_forwards_new_settings_to_heal(self):
        calls: list[tuple[str, ...]] = []

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(returncode=0, stdout=BASE + "\n", stderr="")
            if args == ("status", "--porcelain"):
                return SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "ROOT", Path(tmp)), \
                    mock.patch.object(ahl, "git", side_effect=fake_git), \
                    mock.patch.object(ahl, "log"), \
                    mock.patch.object(ahl, "run_tests", return_value=(1, "boom")), \
                    mock.patch.object(ahl, "heal", return_value=1) as healer, \
                    mock.patch.object(ahl, "reset_hard") as rollback:
                rc = ahl.run_overnight(
                    max_rounds=1, no_push=True, sleep=0,
                    brain="deepseek", executor_profile="none", stop_on_bug=True,
                )
            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        kwargs = healer.call_args.kwargs
        self.assertEqual(kwargs["brain"], "deepseek")
        self.assertEqual(kwargs["executor_profile"], "none")
        self.assertTrue(kwargs["stop_on_bug"])
        self.assertEqual(kwargs["executor"], "auto")
        self.assertIs(kwargs["machine"].__class__, ahl.StateMachine)
        rollback.assert_not_called()
        self.assertEqual(state["state"], "STOPPED_ON_BUG")


if __name__ == "__main__":
    unittest.main()
