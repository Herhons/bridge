"""Offline unit tests for Bridge v0.2.5 generic --attach primitives."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.base import EndpointDriver, exit_code_for
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(nodes: list[UINode], snap_id: str = "s1") -> Snapshot:
    return Snapshot(snapshot_id=snap_id, hwnd=101, title="T", url="", nodes=nodes, refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1000, 800))


class TestV025Attach(unittest.TestCase):
    def test_missing_attachment_returns_exit_code_3(self):
        drv = EndpointDriver("gemini")
        res = drv.ask("test", attach=["/nonexistent/file_xyz.txt"], force=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "missing_resource")
        self.assertEqual(exit_code_for(res), 3)

    def test_two_step_menu_attach_runs_before_composer_write_and_reports_metadata(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="chrome.exe", title="Gemini")
        s1 = _snap([
            UINode(role="ButtonControl", name="上传和工具", rect=(300, 720, 40, 30)),
            UINode(role="EditControl", name="输入 Google 提示", rect=(360, 720, 400, 40)),
        ], "s1")
        s2 = _snap([UINode(role="MenuItemControl", name="上传文件. 文档、数据、代码文件", rect=(300, 640, 220, 30))], "s2")
        s3 = _snap([
            UINode(role="EditControl", name="输入 Google 提示", rect=(360, 720, 400, 40)),
            UINode(role="TextControl", name="已收到附件", rect=(360, 300, 200, 30)),
        ], "s3")
        snaps = [s1, s2, s1, s1, s1, s3, s3]
        idx = {"i": 0}
        events = []
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "demo.txt"
            f.write_text("hello", encoding="utf-8")
            drv = EndpointDriver(
                "gemini",
                snapshot_fn=lambda _h: snaps[min(idx.__setitem__("i", idx["i"] + 1) or idx["i"] - 1, len(snaps) - 1)],
                window_fn=lambda: win,
                invoke_fn=lambda n, _h: events.append(f"click:{n.name}") or "stub",
                write_fn=lambda _n, _t, _h, _c: events.append("write") or "stub",
                dialog_fn=lambda p: events.append(f"dialog:{Path(p).name}") or "stub:dialog",
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
                bridge_config={"stable_seconds": 0.0, "poll_interval": 0.0},
            )
            res = drv.ask("请看附件", attach=[str(f)], force=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(events[:4], ["click:上传和工具", "click:上传文件. 文档、数据、代码文件", "dialog:demo.txt", "write"])
            self.assertEqual(res["attached_files"], [str(f)])
            self.assertEqual(res["attach_methods"], ["stub:dialog"])

    def test_one_step_direct_attach_and_comma_separated_multi_files(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="chrome.exe", title="DeepSeek")
        s1 = _snap([
            UINode(role="ButtonControl", name="上传附件", rect=(300, 720, 40, 30)),
            UINode(role="EditControl", name="输入你的问题", rect=(360, 720, 400, 40)),
        ], "s1")
        s_after = _snap([
            UINode(role="EditControl", name="输入你的问题", rect=(360, 720, 400, 40)),
            UINode(role="TextControl", name="两份文件已读", rect=(360, 300, 200, 30)),
        ], "s2")
        snaps = [s1, s1, s1, s1, s1, s_after, s_after]
        idx = {"i": 0}
        dialogs = []
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "a.txt"
            f2 = Path(td) / "b.txt"
            f1.write_text("1", encoding="utf-8")
            f2.write_text("2", encoding="utf-8")
            drv = EndpointDriver(
                "deepseek",
                snapshot_fn=lambda _h: snaps[min(idx.__setitem__("i", idx["i"] + 1) or idx["i"] - 1, len(snaps) - 1)],
                window_fn=lambda: win,
                invoke_fn=lambda _n, _h: "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                dialog_fn=lambda p: dialogs.append(Path(p).name) or "stub:dialog",
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
                bridge_config={"stable_seconds": 0.0, "poll_interval": 0.0},
            )
            res = drv.ask("读两份", attach=[f"{f1},{f2}"], force=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(dialogs, ["a.txt", "b.txt"])
            self.assertEqual(len(res["attached_files"]), 2)

    def test_missing_menu_item_sends_esc_and_returns_missing_resource(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="Grok Bot.exe", title="Grok Bot")
        s1 = _snap([UINode(role="ButtonControl", name="输入框操作", rect=(100, 720, 40, 30))], "s1")
        s_empty = _snap([], "s2")
        snaps = [s1, s_empty]
        idx = {"i": 0}
        esc = []
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.txt"
            f.write_text("x", encoding="utf-8")
            drv = EndpointDriver(
                "grokbot",
                snapshot_fn=lambda _h: snaps[min(idx.__setitem__("i", idx["i"] + 1) or idx["i"] - 1, 1)],
                window_fn=lambda: win,
                invoke_fn=lambda _n, _h: "stub:click",
                hotkey_fn=lambda *k: esc.append(k),
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
            )
            res = drv.ask("发附件", attach=[str(f)], force=True)
            self.assertFalse(res["ok"])
            self.assertEqual(exit_code_for(res), 3)
            self.assertIn(("esc",), esc)


if __name__ == "__main__":
    unittest.main()
