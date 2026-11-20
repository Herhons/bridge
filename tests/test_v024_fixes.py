"""Regression tests for Bridge v0.2.4 clean engine primitives."""
from __future__ import annotations

import unittest

from engine.base import EndpointDriver
from engine.config import get_endpoint_config
from engine.uia import Snapshot, UINode, assign_refs, snapshot_delta


def _snap(nodes: list[UINode], *, snap_id: str = "s1", rect=(0, 0, 1000, 800)) -> Snapshot:
    refs = assign_refs(nodes, snap_id)
    return Snapshot(snapshot_id=snap_id, hwnd=101, title="Test", url="", nodes=nodes, refs=refs, window_rect=rect)


class TestV024CleanPrimitives(unittest.TestCase):
    def test_hermes_repeated_bubble_and_stripped_prefix_receipt(self):
        before = _snap([UINode(role="TextControl", name="旧对话", rect=(200, 100, 300, 30))], snap_id="s1")
        # UI echoes prompt without "只回复：" prefix, and assistant replies with identical text
        after = _snap(
            [
                UINode(role="TextControl", name="旧对话", rect=(200, 100, 300, 30)),
                UINode(role="TextControl", name="收到-v024-hermes ✅", rect=(200, 200, 300, 30)),
                UINode(role="TextControl", name="收到-v024-hermes ✅", rect=(200, 260, 300, 30)),
            ],
            snap_id="s2",
        )
        drv = EndpointDriver("hermes")
        self.assertTrue(drv._has_send_receipt(before, after, "只回复：收到-v024-hermes ✅"))
        delta = snapshot_delta(before, after)
        self.assertEqual(delta["added_lines"], ["收到-v024-hermes ✅", "收到-v024-hermes ✅"])
        cleaned = drv._extract_reply_lines(delta["added_lines"], prompt="只回复：收到-v024-hermes ✅")
        self.assertEqual(cleaned, ["收到-v024-hermes ✅", "收到-v024-hermes ✅"])
        self.assertEqual(drv.cfg.get("timeout"), 120)

    def test_deepseek_virtual_list_reordered_old_messages_excluded(self):
        # Before send: old conversation lines exist
        before = _snap(
            [
                UINode(role="TextControl", name="旧问题A", rect=(200, 100, 300, 30)),
                UINode(role="TextControl", name="旧长回答B（5000字）", rect=(200, 140, 300, 30)),
                UINode(role="TextControl", name="内容由 AI 生成", rect=(200, 700, 200, 20)),
            ],
            snap_id="s1",
        )
        # After send: virtual list places old nodes AFTER footer, while new prompt + reply appear at top
        after = _snap(
            [
                UINode(role="TextControl", name="只回复：收到-v024-ds", rect=(200, 100, 300, 30)),
                UINode(role="TextControl", name="收到-v024-ds", rect=(200, 140, 300, 30)),
                UINode(role="TextControl", name="内容由 AI 生成", rect=(200, 700, 200, 20)),
                UINode(role="TextControl", name="旧问题A", rect=(200, 740, 300, 30)),
                UINode(role="TextControl", name="旧长回答B（5000字）", rect=(200, 780, 300, 30)),
            ],
            snap_id="s2",
        )
        delta = snapshot_delta(before, after)
        self.assertEqual(delta["added_lines"], ["只回复：收到-v024-ds", "收到-v024-ds"])
        drv = EndpointDriver("deepseek")
        cleaned = drv._extract_reply_lines(delta["added_lines"], prompt="只回复：收到-v024-ds")
        self.assertEqual(cleaned, ["收到-v024-ds"])

    def test_grokbot_right_sidebar_clipped_by_max_rel_x_and_status_cleaned(self):
        cfg = get_endpoint_config("grokbot")
        self.assertEqual(cfg.get("content_max_rel_x"), 0.72)
        snap = _snap(
            [
                UINode(role="TextControl", name="收到-v024-gb", rect=(120, 200, 300, 30)),
                UINode(role="TextControl", name="助手 已回复", rect=(120, 240, 200, 20)),
                UINode(role="TextControl", name="右侧任务列表项", rect=(780, 200, 180, 30)),
                UINode(role="TextControl", name="已暂停", rect=(800, 250, 100, 20)),
                UINode(role="TextControl", name="Chinese (Simplified)", rect=(800, 300, 160, 20)),
            ],
            rect=(0, 0, 1000, 800),
        )
        lines = snap.text_lines(min_rel_x=0.0, max_rel_x=0.72)
        self.assertEqual(lines, ["收到-v024-gb", "助手 已回复"])
        drv = EndpointDriver("grokbot")
        cleaned = drv._extract_reply_lines(lines, prompt="只回复：收到-v024-gb")
        self.assertEqual(cleaned, ["收到-v024-gb"])


if __name__ == "__main__":
    unittest.main()
