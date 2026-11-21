"""Offline tests for the Antigravity endpoint and the 3-level executor pool.

No real GUI, bridge subprocess or window activation: UIA/window enumeration is
not touched, ``run_cmd`` is mocked, and only the config/window matching code is
exercised. Safe to run unattended.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402
from endpoints import ENDPOINT_NAMES, get_driver  # noqa: E402
from engine.window import WindowInfo, find_window  # noqa: E402


class TestAntigravityConfig(unittest.TestCase):
    def _cfg(self) -> dict:
        data = json.loads((ROOT / "config" / "endpoints.json").read_text(encoding="utf-8"))
        return data["endpoints"]["antigravity"]

    def test_endpoint_config_is_complete_and_name_based(self):
        cfg = self._cfg()
        self.assertEqual(cfg["kind"], "desktop")
        self.assertEqual(cfg["process_names"], ["Antigravity.exe"])
        self.assertEqual(cfg["window_class"], "Chrome_WidgetWin_1")
        self.assertTrue(any("antigravity" in t.lower() for t in cfg["title_contains"]))

        sel = cfg["selectors"]
        composer = sel["composer"]
        self.assertIn("Message input", composer["names"])
        self.assertIn("ComboBoxControl", composer["roles"])

        attach = sel["attach_button"]
        self.assertIn("Add context", attach["names"])
        self.assertEqual(attach["roles"], ["ButtonControl"])

        self.assertTrue(sel["send_button"]["names"])
        self.assertTrue(cfg["models"])

        # Copy is required for reply read-back; keep both UI languages.
        self.assertIn("Copy", sel["copy_button"]["names"])
        self.assertIn("复制", sel["copy_button"]["names"])

        dumped = json.dumps(sel, ensure_ascii=False)
        self.assertNotIn("base-ui-", dumped)
        self.assertNotIn("automation_ids", dumped)

    def test_registry_and_window_class_filtering(self):
        self.assertIn("antigravity", ENDPOINT_NAMES)
        drv = get_driver("antigravity")
        self.assertEqual(drv.name, "antigravity")
        self.assertEqual(drv.cfg["window_class"], "Chrome_WidgetWin_1")

        wins = [
            # Wrong WNDCLASS (hidden helper style).
            WindowInfo(hwnd=1, title="Antigravity", process_name="Antigravity.exe",
                       pid=10, class_name="Chrome_WidgetWin_0"),
            # Correct main window.
            WindowInfo(hwnd=2, title="Antigravity", process_name="Antigravity.exe",
                       pid=11, class_name="Chrome_WidgetWin_1"),
            # OpenCode window whose title contains 反重力 must never match.
            WindowInfo(hwnd=3, title="任务 反重力 UIA 探测 · OpenCode",
                       process_name="OpenCode.exe", pid=12,
                       class_name="Chrome_WidgetWin_1"),
        ]
        picked = find_window(
            process_names=["Antigravity.exe"],
            title_contains=["Antigravity"],
            window_class="Chrome_WidgetWin_1",
            windows=wins,
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.hwnd, 2)


class TestThreeLevelExecutorFallback(unittest.TestCase):
    def test_auto_falls_back_to_antigravity_when_first_two_fail(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            if endpoint == "workbuddy":
                return ahl.TIMEOUT_RC, "TIMEOUT"
            if endpoint == "opencode":
                return 0, ""
            if endpoint == "antigravity":
                return 0, json.dumps({"ok": True, "reply": "antigravity ok"})
            return 1, ""

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "log") as logger:
                reply = ahl.call_executor("step: patch code", 4, 120, "auto")

            task_text = (runs / "antigravity_task_round4.md").read_text(encoding="utf-8")

        self.assertEqual(reply, "antigravity ok")
        self.assertEqual(calls, ["workbuddy", "opencode", "antigravity"])
        logs = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("executor_fallback -> opencode", logs)
        self.assertIn("executor_fallback -> antigravity", logs)
        self.assertIn("executor_hit=antigravity", logs)
        self.assertTrue(task_text.startswith(ahl.scheduler_notice()))
        self.assertIn("step: patch code", task_text)
        self.assertIn("[executor] mode=auto endpoint=antigravity", task_text)

    def test_explicit_antigravity_never_touches_earlier_levels(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            return 0, json.dumps({"ok": True, "reply": "agy only"})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step", 1, 60, "antigravity")

        self.assertEqual(reply, "agy only")
        self.assertEqual(calls, ["antigravity"])

    def test_parser_lists_antigravity_and_default_is_auto(self):
        parser = ahl.build_parser()
        self.assertEqual(parser.parse_args([]).executor, "auto")
        self.assertEqual(
            parser.parse_args(["--executor", "antigravity"]).executor,
            "antigravity",
        )
        self.assertEqual(ahl.EXECUTOR_CHAIN, ("workbuddy", "opencode", "antigravity"))


if __name__ == "__main__":
    unittest.main()
