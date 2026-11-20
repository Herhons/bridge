"""Regression tests for real-machine reconcile fixes (WorkBuddy sidebar, OpenCode CLI, Grok Bot, failure evidence, readiness)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endpoints import get_driver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo, find_window


class TestReconcileFixes(unittest.TestCase):
    def test_workbuddy_read_strips_left_sidebar_by_relative_x(self):
        # Simulate WorkBuddy window at x=1271, width=1254 (sidebar < 1271 + 0.23*1254 = 1559)
        kids = [
            UINode(role="TextControl", name="历史任务A", rect=(1300, 350, 200, 24)),
            UINode(role="TextControl", name="历史任务B", rect=(1300, 390, 200, 24)),
            UINode(role="TextControl", name="右侧正文回复第一行", rect=(1663, 600, 600, 24)),
            UINode(role="TextControl", name="右侧正文回复第二行", rect=(1663, 630, 600, 24)),
            UINode(role="EditControl", name="", rect=(1663, 1099, 740, 64)),
        ]
        root = UINode(role="PaneControl", name="WorkBuddy", rect=(1271, 204, 1254, 1050), children=kids)
        snap = Snapshot(
            snapshot_id="wb1",
            hwnd=67654,
            title="WorkBuddy",
            url="",
            nodes=[root],
            refs=assign_refs([root], "wb1"),
            window_rect=(1271, 204, 1254, 1050),
        )
        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "workbuddy",
                snapshot_fn=lambda _h: snap,
                window_fn=lambda: WindowInfo(hwnd=67654, title="WorkBuddy", process_name="WorkBuddy.exe", pid=10),
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.read()
            self.assertTrue(res["ok"])
            self.assertNotIn("历史任务A", res["reply"])
            self.assertNotIn("历史任务B", res["reply"])
            self.assertEqual(res["reply"], "右侧正文回复第一行\n右侧正文回复第二行")

    def test_opencode_cli_ask_returns_stdout_and_marks_ledger(self):
        captured_cmd = []

        def fake_runner(cmd, timeout):
            captured_cmd.extend(cmd)
            return 0, "OpenCode CLI 完整回复\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "opencode",
                cli_runner=fake_runner,
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("检查仓库")
            self.assertTrue(res["ok"])
            self.assertEqual(res["source"], "cli_stdout")
            self.assertEqual(res["reply"], "OpenCode CLI 完整回复")
            self.assertIn("--prompt", captured_cmd)
            self.assertIn("--auto", captured_cmd)
            self.assertIn("--continue", captured_cmd)
            self.assertEqual(drv.ledger.get("检查仓库", "opencode")["state"], "accepted")

    def test_grokbot_matches_real_process_name_with_space(self):
        drv = get_driver("grokbot")
        wins = [WindowInfo(hwnd=67514, title="Grok Bot", process_name="Grok Bot.exe", pid=99)]
        hit = find_window(
            process_names=drv.cfg["process_names"],
            title_contains=drv.cfg["title_contains"],
            windows=wins,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.hwnd, 67514)

    def test_login_screen_reports_not_ready_and_writes_failure_evidence(self):
        # Simulate ChatGPT window stuck on login screen (0 editors)
        root = UINode(role="WindowControl", name="ChatGPT", rect=(0, 0, 1200, 900), children=[])
        snap = Snapshot(snapshot_id="cg0", hwnd=1, title="ChatGPT", url="", nodes=[root], refs={}, window_rect=(0, 0, 1200, 900))
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / "fail"
            drv = get_driver(
                "chatgpt",
                snapshot_fn=lambda _h: snap,
                window_fn=lambda: WindowInfo(hwnd=1, title="ChatGPT - Google Chrome", process_name="chrome.exe", pid=1),
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=ev_dir,
            )
            st = drv.status()
            self.assertTrue(st["window_found"])
            self.assertFalse(st["ready"])

            res = drv.ask("hello")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "composer_not_found")
            self.assertTrue(res.get("evidence"))
            ev_path = Path(res["evidence"])
            self.assertTrue(ev_path.is_file())
            ev_data = json.loads(ev_path.read_text(encoding="utf-8"))
            self.assertEqual(ev_data["error"], "composer_not_found")


if __name__ == "__main__":
    unittest.main()
