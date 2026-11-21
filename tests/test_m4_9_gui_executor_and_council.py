"""Offline coverage for M4-9: executor picker + one-click three-way council.

Everything here is pure logic: the GUI module is loaded without importing
``tkinter`` (no window, no event loop), the council runner is injected, and the
only subprocess use is a mocked ``git`` behind ``build_council_brief``.

Pinned contracts: the executor default is ``workbuddy`` and reaches argv, the
brief carries the unfinished roadmap items, and one failing endpoint never
starves the other two of their bundle section.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "bridge_gui.pyw"


def _load_gui(name: str = "bridge_gui_m49"):
    """Load ``bridge_gui.pyw`` under a private name; no Tk window is created."""
    loader = importlib.machinery.SourceFileLoader(name, str(GUI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class ExecutorChoiceTests(unittest.TestCase):
    def setUp(self):
        self.gui = _load_gui()

    def test_default_executor_is_workbuddy(self):
        self.assertEqual(self.gui.EXECUTOR_CHOICES, ("workbuddy", "auto", "antigravity"))
        self.assertEqual(self.gui.EXECUTOR_DEFAULT, "workbuddy")
        self.assertIn(self.gui.EXECUTOR_DEFAULT, self.gui.EXECUTOR_CHOICES)

    def test_daemon_cmd_carries_the_selected_executor(self):
        cmd = self.gui.build_daemon_cmd(".", python_exe="py.exe", executor="workbuddy")
        self.assertEqual(cmd[cmd.index("--executor") + 1], "workbuddy")
        # The builder default matches the dropdown, so an omitted value cannot
        # silently fall back to a different worker.
        default_cmd = self.gui.build_daemon_cmd(".", python_exe="py.exe")
        self.assertEqual(
            default_cmd[default_cmd.index("--executor") + 1],
            self.gui.EXECUTOR_DEFAULT,
        )

    def test_detached_launch_honours_the_dropdown_without_respawning_argv(self):
        cmd = self.gui.build_daemon_cmd(".", python_exe="py.exe", executor="workbuddy")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    self.gui.subprocess, "Popen",
                    return_value=SimpleNamespace(pid=4242),
                ) as popen:
            result = self.gui.launch_daemon_detached(
                cmd, root=tmp, executor="antigravity"
            )
        self.assertTrue(result["ok"])
        argv = result["command"]
        self.assertEqual(argv[argv.index("--executor") + 1], "antigravity")
        self.assertEqual(popen.call_args.args[0][popen.call_args.args[0].index("--executor") + 1],
                         "antigravity")
        # The caller's list is never mutated in place.
        self.assertEqual(cmd[cmd.index("--executor") + 1], "workbuddy")


class CouncilBriefTests(unittest.TestCase):
    def setUp(self):
        self.gui = _load_gui()

    def test_brief_lists_branch_head_and_pending_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ROADMAP.md").write_text(
                "# roadmap\n"
                "- [x] M4-8 已完成\n"
                "- [ ] **M4-9（GUI 执行器选择与一键会审）**：做下拉框\n"
                "- [ ] M4-10（base 瘦身）：拆 ask\n",
                encoding="utf-8",
            )

            def fake_run(cmd, **kwargs):
                args = [str(part) for part in cmd]
                if "rev-parse" in args:
                    return SimpleNamespace(returncode=0, stdout="auto/v0.2.5\n", stderr="")
                return SimpleNamespace(
                    returncode=0, stdout="14cc27d chore(auto): round 1\n", stderr=""
                )

            with mock.patch.object(self.gui.subprocess, "run", side_effect=fake_run):
                brief = self.gui.build_council_brief(root)

            text = brief.read_text(encoding="utf-8")

        self.assertEqual(brief, Path(tmp) / "runs" / "council_brief_auto.md")
        self.assertIn("auto/v0.2.5", text)
        self.assertIn("14cc27d chore(auto): round 1", text)
        # Only the unfinished items belong in the review material.
        self.assertIn("M4-9（GUI 执行器选择与一键会审）", text)
        self.assertIn("M4-10（base 瘦身）", text)
        self.assertNotIn("M4-8 已完成", text)
        self.assertIn("未勾选 2 项 / 共 3 项", text)
        self.assertIn("会审要求", text)

    def test_missing_roadmap_still_writes_a_usable_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                self.gui.subprocess, "run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
            ):
                brief = self.gui.build_council_brief(Path(tmp))
            text = brief.read_text(encoding="utf-8")
            self.assertTrue(brief.is_file())
        self.assertIn("(unknown)", text)
        self.assertIn("未勾选 0 项 / 共 0 项", text)


class ThreeWayCouncilTests(unittest.TestCase):
    def setUp(self):
        self.gui = _load_gui()

    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "ROADMAP.md").write_text("- [ ] M4-9\n", encoding="utf-8")
        (root / "runs").mkdir()
        brief = root / "runs" / "brief.md"
        brief.write_text("会审材料\n", encoding="utf-8")
        return root

    def test_all_success_bundles_every_raw_reply(self):
        replies = {
            "deepseek": "DS: 风险在于超时口径",
            "grok": "GROK: 建议先补单测",
            "gemini": "GEMINI: 无阻塞问题",
        }
        calls: list[list[str]] = []

        def runner(cmd, timeout):
            calls.append([str(part) for part in cmd])
            endpoint = cmd[3]
            return 0, json.dumps(
                {"ok": True, "reply": replies[endpoint]}, ensure_ascii=False
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            result = self.gui.run_three_way_council(
                root, brief_path=root / "runs" / "brief.md", runner=runner
            )
            bundle = Path(result["bundle_path"]).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["succeeded"], ["deepseek", "grok", "gemini"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(calls), 3)
        for cmd in calls:
            self.assertIn("--profile", cmd)
            self.assertEqual(cmd[cmd.index("--profile") + 1], "council")
            self.assertIn("--headed", cmd)
            self.assertIn("--file", cmd)
        self.assertEqual([cmd[3] for cmd in calls], ["deepseek", "grok", "gemini"])
        for endpoint, reply in replies.items():
            self.assertIn("## %s — OK" % endpoint, bundle)
            self.assertIn(reply, bundle)
        self.assertIn("全部成功", bundle)

    def test_one_failing_endpoint_does_not_starve_the_others(self):
        def runner(cmd, timeout):
            endpoint = cmd[3]
            if endpoint == "grok":
                return 1, "rate limited"
            return 0, json.dumps({"ok": True, "reply": "reply from %s" % endpoint})

        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            result = self.gui.run_three_way_council(
                root, brief_path=root / "runs" / "brief.md", runner=runner
            )
            bundle = Path(result["bundle_path"]).read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertEqual(result["succeeded"], ["deepseek", "gemini"])
        self.assertEqual(result["failed"], ["grok"])
        self.assertIn("reply from deepseek", bundle)
        self.assertIn("reply from gemini", bundle)
        self.assertIn("## grok — FAILED", bundle)
        self.assertIn("rc_1", bundle)
        self.assertIn("失败：grok", bundle)

    def test_runner_exception_is_recorded_not_raised(self):
        def runner(cmd, timeout):
            if cmd[3] == "deepseek":
                raise RuntimeError("browser blew up")
            return 0, json.dumps({"ok": True, "reply": "ok-%s" % cmd[3]})

        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            result = self.gui.run_three_way_council(
                root, brief_path=root / "runs" / "brief.md", runner=runner
            )
            bundle = Path(result["bundle_path"]).read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], ["deepseek"])
        self.assertEqual(result["succeeded"], ["grok", "gemini"])
        self.assertIn("runner_error", result["results"]["deepseek"]["error"])
        self.assertIn("未取得回复", bundle)


class NoTkAtImportTests(unittest.TestCase):
    def test_import_never_builds_a_window(self):
        gui = _load_gui("bridge_gui_m49_import")
        self.assertIsNone(gui.tk)
        self.assertIsNone(gui.ttk)


if __name__ == "__main__":
    unittest.main()
