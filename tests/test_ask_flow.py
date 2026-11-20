"""End-to-end offline flow test verifying exact call order of ask()."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endpoints import get_driver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(snap_id: str, lines: list[str], *, menu_open: bool = False) -> Snapshot:
    kids = [
        UINode(role="ButtonControl", name="Fast", rect=(80, 350, 80, 28)),
        UINode(role="EditControl", name="畅所欲问", rect=(10, 400, 300, 40)),
        UINode(role="ButtonControl", name="提交", rect=(320, 400, 50, 40)),
    ]
    if menu_open:
        kids.append(UINode(role="MenuItemControl", name="Expert", rect=(80, 300, 100, 26)))
    for t in lines:
        kids.append(UINode(role="TextControl", name=t))
    root = UINode(role="WindowControl", name="Grok", children=kids)
    return Snapshot(snapshot_id=snap_id, hwnd=88, title="Grok", url="", nodes=[root], refs=assign_refs([root], snap_id))


class TestAskFlowOrder(unittest.TestCase):
    def test_full_ask_sequence_and_duplicate_block_on_second_call(self):
        events: list[str] = []
        snaps = [
            _snap("s0", ["旧对话"]),
            _snap("s1", ["旧对话"], menu_open=True),
            _snap("s2", ["旧对话"]),
            _snap("s3", ["旧对话"]),
            _snap("s4", ["旧对话", "测试全链路", "完整回复正文"]),
            _snap("s5", ["旧对话", "测试全链路", "完整回复正文"]),
        ]
        idx = {"i": 0}

        def fake_snap(_h):
            i = min(idx["i"], len(snaps) - 1)
            idx["i"] += 1
            return snaps[i]

        def fake_invoke(node, _headed):
            events.append(f"click:{node.name}")
            return "stub:click"

        def fake_write(node, text, _headed, _chunk):
            events.append(f"write:{node.name}:{text}")
            return "stub:write"

        def fake_confirm(ep, prompt, model):
            events.append(f"confirm:{ep}:{model}")
            return True

        with tempfile.TemporaryDirectory() as tmp:
            ledger_file = Path(tmp) / "ledger.json"
            drv = get_driver(
                "grok",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=fake_snap,
                window_fn=lambda: WindowInfo(hwnd=88, title="Grok - Google Chrome", process_name="chrome.exe", pid=55),
                invoke_fn=fake_invoke,
                write_fn=fake_write,
                confirm_fn=fake_confirm,
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=ledger_file,
            )
            res1 = drv.ask("测试全链路", model="expert", confirm=True)
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["reply"], "完整回复正文")
            self.assertEqual(
                events,
                [
                    "click:Fast",
                    "click:Expert",
                    "write:畅所欲问:测试全链路",
                    "confirm:grok:expert",
                    "click:提交",
                ],
            )
            self.assertEqual(drv.ledger.get("测试全链路", "grok")["state"], "accepted")

            # Second identical call must be blocked by ledger
            res2 = drv.ask("测试全链路")
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["error"], "duplicate")
            self.assertEqual(res2["previous_state"], "accepted")


if __name__ == "__main__":
    unittest.main()
