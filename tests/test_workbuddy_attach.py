"""Offline WorkBuddy --attach flow tests (no GUI, no focus, no real dialog)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine.base as base
from engine.base import EndpointDriver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(nodes: list[UINode], snap_id: str) -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id, hwnd=101, title="WorkBuddy", url="", nodes=nodes,
        refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1200, 900),
    )


class TestWorkBuddyAttach(unittest.TestCase):
    def _drive(self, attach_value: str, *, comma_multi: bool):
        win = WindowInfo(hwnd=101, pid=4242, process_name="WorkBuddy.exe", title="WorkBuddy")
        attach_btn = UINode(role="ButtonControl", name="添加附件", rect=(300, 720, 60, 30))
        menu_item = UINode(role="MenuItemControl", name="上传文件", rect=(300, 640, 160, 30))
        composer = UINode(role="EditControl", name="输入指令", rect=(360, 720, 600, 60))
        send_btn = UINode(role="ButtonControl", name="发送", rect=(900, 760, 60, 30))

        initial = _snap([attach_btn, composer], "s1")
        menu = _snap([menu_item, composer], "s2")
        ready = _snap([
            UINode(role="TextControl", name="附件: uploaded.txt", rect=(360, 690, 200, 30)),
            composer,
        ], "s3")
        send = _snap([composer, send_btn], "s4")
        reply = _snap([
            composer,
            UINode(role="TextControl", name="收到附件", rect=(360, 300, 300, 30)),
        ], "s5")
        snaps = [initial, menu, ready, ready, send, reply, reply]
        idx = {"i": 0}
        events: list[str] = []

        def fake_snap(_hwnd):
            snap = snaps[min(idx["i"], len(snaps) - 1)]
            idx["i"] += 1
            return snap

        def fake_invoke(node, _headed):
            events.append("click:" + node.name)
            return "stub:click"

        def fake_write(_node, text, _headed, _chunk):
            events.append("write:" + text)
            return "stub:write"

        with tempfile.TemporaryDirectory() as td:
            paths = []
            for name in ("a.txt", "b.txt") if comma_multi else ("a.txt",):
                path = Path(td) / name
                path.write_text(name, encoding="utf-8")
                paths.append(str(path))
            value = ",".join(paths) if comma_multi else paths[0]

            with mock.patch.object(
                base, "fill_windows_file_dialog",
                side_effect=lambda p, **_kw: events.append("dialog:" + ",".join(p)) or "#32770:stub",
            ), mock.patch.object(base, "find_existing_file_dialog", return_value=0):
                drv = EndpointDriver(
                    "workbuddy",
                    snapshot_fn=fake_snap,
                    window_fn=lambda: win,
                    invoke_fn=fake_invoke,
                    write_fn=fake_write,
                    sleep_fn=lambda _s: None,
                    lock_path=Path(td) / "ui.lock",
                    ledger_path=Path(td) / "ledger.json",
                )
                drv.cfg["stable_seconds"] = 0.0
                res = drv.ask("请确认收到附件", attach=[value], force=True, headed=False)

        return paths, events, res

    def test_upload_dialog_then_write_and_send_in_order(self):
        paths, events, res = self._drive("unused", comma_multi=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reply"], "收到附件")
        self.assertEqual(
            events[:5],
            [
                "click:添加附件",
                "click:上传文件",
                "dialog:" + paths[0],
                "write:请确认收到附件",
                "click:发送",
            ],
        )
        self.assertEqual(res["attach_methods"], ["#32770:stub"])
        self.assertEqual(res["attached_files"], paths)

    def test_multiple_files_go_through_one_quoted_dialog(self):
        paths, events, res = self._drive("unused", comma_multi=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(paths), 2)
        dialog_events = [e for e in events if e.startswith("dialog:")]
        self.assertEqual(dialog_events, ["dialog:" + ",".join(paths)])
        self.assertEqual(res["attached_files"], paths)
        self.assertLess(events.index("dialog:" + ",".join(paths)), events.index("write:请确认收到附件"))


if __name__ == "__main__":
    unittest.main()
