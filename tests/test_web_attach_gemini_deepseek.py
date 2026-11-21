"""Offline --attach coverage for Gemini (two-step) and DeepSeek (one-step).

No real GUI, clipboard, file dialog, or bridge subprocess: snapshots, the
#32770 dialog primitive and the existing-dialog probe are mocked. Safe to run
unattended on any machine.
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

import engine.base as base  # noqa: E402
from engine.base import EndpointDriver  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402


def _snap(nodes: list[UINode], snap_id: str, *, title: str = "Web") -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id, hwnd=101, title=title, url="", nodes=nodes,
        refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1200, 900),
    )


class _DriverCase(unittest.TestCase):
    def _driver(self, name: str, snaps: list[Snapshot], win: WindowInfo,
                events: list[str], hotkeys: list[tuple[str, ...]],
                td: str, *, start: int = 0) -> EndpointDriver:
        idx = {"i": start}

        def fake_snap(_hwnd):
            snap = snaps[min(idx["i"], len(snaps) - 1)]
            idx["i"] += 1
            return snap

        drv = EndpointDriver(
            name,
            snapshot_fn=fake_snap,
            window_fn=lambda: win,
            invoke_fn=lambda node, _headed: events.append("click:" + node.name) or "stub:click",
            write_fn=lambda _node, text, _headed, _chunk: events.append("write:" + text) or "stub:write",
            hotkey_fn=lambda *keys: hotkeys.append(keys),
            sleep_fn=lambda _seconds: None,
            lock_path=Path(td) / "ui.lock",
            ledger_path=Path(td) / "ledger.json",
            bridge_config={"stable_seconds": 0.0, "poll_interval": 0.0},
        )
        drv.cfg["stable_seconds"] = 0.0
        return drv


class TestGeminiTwoStepAttach(_DriverCase):
    def test_upload_tools_button_then_menu_item_before_dialog(self):
        win = WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini")
        attach_btn = UINode(role="ButtonControl", name="上传和工具", rect=(60, 800, 40, 30))
        menu_item = UINode(
            role="MenuItemControl", name="上传文件. 文档、数据、代码文件",
            rect=(60, 700, 240, 30),
        )
        composer = UINode(role="EditControl", name="输入 Google 提示", rect=(120, 800, 600, 30))
        snaps = [_snap([attach_btn, composer], "s1"), _snap([menu_item, composer], "s2")]
        events: list[str] = []
        hotkeys: list[tuple[str, ...]] = []

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "gemini.txt"
            f.write_text("hello", encoding="utf-8")
            drv = self._driver("gemini", snaps, win, events, hotkeys, td, start=1)
            with mock.patch.object(base, "find_existing_file_dialog", return_value=0) as finder, \
                    mock.patch.object(
                        base, "fill_windows_file_dialog", return_value="#32770:stub"
                    ) as filler:
                methods = drv.attach_files(win, snaps[0], [str(f)], headed=False)

        self.assertEqual(
            events,
            ["click:上传和工具", "click:上传文件. 文档、数据、代码文件"],
        )
        finder.assert_called_once_with(4242)
        filler.assert_called_once_with([str(f)], owner_pid=4242, timeout=12.0)
        self.assertEqual(methods, ["#32770:stub"])
        self.assertEqual(hotkeys, [])  # no clipboard, no Esc, no foreground key

    def test_menu_is_skipped_when_the_button_already_opened_the_dialog(self):
        win = WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini")
        attach_btn = UINode(role="ButtonControl", name="上传和工具", rect=(60, 800, 40, 30))
        composer = UINode(role="EditControl", name="输入 Google 提示", rect=(120, 800, 600, 30))
        snaps = [_snap([attach_btn, composer], "s1"), _snap([composer], "s2")]
        events: list[str] = []
        hotkeys: list[tuple[str, ...]] = []

        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "gemini.txt"
            f.write_text("hello", encoding="utf-8")
            drv = self._driver("gemini", snaps, win, events, hotkeys, td)
            with mock.patch.object(base, "find_existing_file_dialog", return_value=777), \
                    mock.patch.object(
                        base, "fill_windows_file_dialog", return_value="#32770:direct"
                    ) as filler:
                methods = drv.attach_files(win, snaps[0], [str(f)], headed=False)

        self.assertEqual(events, ["click:上传和工具"])
        filler.assert_called_once_with([str(f)], owner_pid=4242, timeout=12.0)
        self.assertEqual(methods, ["#32770:direct"])


class TestDeepSeekOneStepAttach(_DriverCase):
    def test_single_click_carries_multiple_files_in_one_dialog(self):
        win = WindowInfo(hwnd=101, pid=99, process_name="chrome.exe", title="DeepSeek")
        attach_btn = UINode(role="ButtonControl", name="上传附件", rect=(200, 800, 30, 30))
        composer = UINode(role="EditControl", name="输入你的问题", rect=(260, 800, 600, 30))
        snaps = [_snap([attach_btn, composer], "s1")]
        events: list[str] = []
        hotkeys: list[tuple[str, ...]] = []

        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "a.txt"
            f2 = Path(td) / "b.txt"
            f1.write_text("1", encoding="utf-8")
            f2.write_text("2", encoding="utf-8")
            drv = self._driver("deepseek", snaps, win, events, hotkeys, td)
            with mock.patch.object(base, "find_existing_file_dialog") as finder, \
                    mock.patch.object(
                        base, "fill_windows_file_dialog", return_value="#32770:stub"
                    ) as filler:
                methods = drv.attach_files(
                    win, snaps[0], [str(f1), str(f2)], headed=False
                )

        self.assertEqual(events, ["click:上传附件"])
        finder.assert_not_called()  # DeepSeek has no attach_menu_item
        filler.assert_called_once_with([str(f1), str(f2)], owner_pid=99, timeout=12.0)
        self.assertEqual(methods, ["#32770:stub"])
        self.assertEqual(hotkeys, [])

    def test_ask_without_attach_never_touches_the_dialog_primitive(self):
        win = WindowInfo(hwnd=101, pid=99, process_name="chrome.exe", title="DeepSeek")
        composer = UINode(role="EditControl", name="输入你的问题", rect=(260, 800, 600, 30))
        send_btn = UINode(role="ButtonControl", name="发送", rect=(900, 800, 40, 30))
        echo = UINode(role="TextControl", name="请回复", rect=(260, 700, 200, 24))
        reply = UINode(role="TextControl", name="ATTACH-NONE-OK", rect=(260, 640, 240, 24))
        initial = _snap([composer, send_btn], "n1")
        final = _snap([composer, echo, reply], "n2")
        snaps = [initial, initial, final, final]
        events: list[str] = []
        hotkeys: list[tuple[str, ...]] = []

        with tempfile.TemporaryDirectory() as td:
            drv = self._driver("deepseek", snaps, win, events, hotkeys, td)
            with mock.patch.object(base, "find_existing_file_dialog") as finder, \
                    mock.patch.object(base, "fill_windows_file_dialog") as filler:
                res = drv.ask("请回复", force=True)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reply"], "ATTACH-NONE-OK")
        self.assertNotIn("attached_files", res)
        self.assertNotIn("attach_methods", res)
        finder.assert_not_called()
        filler.assert_not_called()
        self.assertEqual(events, ["write:请回复", "click:发送"])


class TestAttachConfig(_DriverCase):
    def test_gemini_exposes_two_step_and_deepseek_exposes_one_step(self):
        cfg = json.loads((ROOT / "config" / "endpoints.json").read_text(encoding="utf-8"))
        endpoints = cfg["endpoints"]

        gem = endpoints["gemini"]["selectors"]
        self.assertTrue(gem["attach_button"]["names"])
        self.assertTrue(gem["attach_button"]["name_contains"])
        self.assertTrue(gem["attach_menu_item"]["name_contains"])
        self.assertLess(gem["attach_button"]["anchor_box_ratio"][1],
                        gem["attach_button"]["anchor_box_ratio"][3])

        ds = endpoints["deepseek"]["selectors"]
        self.assertTrue(ds["attach_button"]["names"])
        self.assertTrue(ds["attach_button"]["name_contains"])
        self.assertNotIn("attach_menu_item", ds)

        for name in ("gemini", "deepseek"):
            self.assertGreaterEqual(
                endpoints[name]["attach_dialog_timeout"], 10.0
            )
            self.assertNotIn("SetForegroundWindow", json.dumps(endpoints[name]))


if __name__ == "__main__":
    unittest.main()
