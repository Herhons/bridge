"""Unit tests for --model switching via config/endpoints.json model_selector & model_option."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endpoints import get_driver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap_with_model_menu(snap_id: str, *, menu_open: bool = False, extra_lines: list[str] | None = None) -> Snapshot:
    kids = [
        UINode(role="ButtonControl", name="选择 ChatGPT 模型", rect=(100, 350, 120, 30)),
        UINode(role="EditControl", name="询问 ChatGPT", rect=(10, 400, 300, 40)),
        UINode(role="ButtonControl", name="发送", rect=(320, 400, 50, 40)),
    ]
    if menu_open:
        kids.append(UINode(role="RadioButtonControl", name="GPT-6.1 Sol", rect=(100, 260, 150, 28)))
        kids.append(UINode(role="RadioButtonControl", name="GPT-5.6", rect=(100, 290, 150, 28)))
    for line in (extra_lines or []):
        kids.append(UINode(role="TextControl", name=line))
    root = UINode(role="WindowControl", name="ChatGPT", children=kids)
    return Snapshot(snapshot_id=snap_id, hwnd=5, title="ChatGPT", url="", nodes=[root], refs=assign_refs([root], snap_id))


class TestModelSwitch(unittest.TestCase):
    def test_model_switch_clicks_selector_and_target_option(self):
        snaps = [
            _snap_with_model_menu("s0", menu_open=False),
            _snap_with_model_menu("s1", menu_open=True),
            _snap_with_model_menu("s2", menu_open=False),
            _snap_with_model_menu("s3", menu_open=False),
            _snap_with_model_menu("s4", menu_open=False, extra_lines=["切换后回复"]),
            _snap_with_model_menu("s5", menu_open=False, extra_lines=["切换后回复"]),
        ]
        idx = {"i": 0}

        def next_snap(_h):
            i = min(idx["i"], len(snaps) - 1)
            idx["i"] += 1
            return snaps[i]

        clicked = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "chatgpt",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=next_snap,
                window_fn=lambda: WindowInfo(hwnd=5, title="ChatGPT - Google Chrome", process_name="chrome.exe", pid=11),
                invoke_fn=lambda node, _h: clicked.append(node.name) or "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("测试切模型", model="gpt61_sol")
            self.assertTrue(res["ok"])
            self.assertTrue(res["model_switch"]["switched"])
            self.assertEqual(clicked, ["选择 ChatGPT 模型", "GPT-6.1 Sol", "发送"])

    def test_endpoint_without_model_selector_skips_cleanly(self):
        snaps = [
            _snap_with_model_menu("s0"),
            _snap_with_model_menu("s1"),
            _snap_with_model_menu("s2", extra_lines=["你好", "hermes ok"]),
            _snap_with_model_menu("s3", extra_lines=["你好", "hermes ok"]),
        ]
        idx = {"i": 0}

        def next_snap(_h):
            i = min(idx["i"], len(snaps) - 1)
            idx["i"] += 1
            return snaps[i]

        clicked = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "grokbot",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=next_snap,
                window_fn=lambda: WindowInfo(hwnd=9, title="Grok Bot", process_name="Grok Bot.exe", pid=12),
                invoke_fn=lambda node, _h: clicked.append(node.name) or "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("你好", model="freellm")
            self.assertTrue(res["ok"])
            self.assertEqual(res["model_switch"]["reason"], "not_supported")
            self.assertEqual(clicked, ["发送"])


if __name__ == "__main__":
    unittest.main()
