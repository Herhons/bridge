"""B29：动作条被顶出视口时，不能死等复制键。

真机（2026-11-12，chatgpt 长回答）：复制键 y=2061，窗口只到 1340，
`conversation.copy()` 直接抛「光标没能移到…」，于是整轮重试到 timeout，
最后靠 `text_extracted` 兜底——STOP_20261111_2145 未决清单第 1 条就是这个。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation                            # noqa: E402

WINDOW = (-1440, -560, 1380, 1340)      # 双屏：副屏在左，主屏到 x=1380


class Node:
    def __init__(self, rect):
        self.control = type("C", (), {"BoundingRectangle": rect})()


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class OffscreenTests(unittest.TestCase):
    def test_below_window_is_unreachable(self):
        self.assertFalse(conversation.rect_in_window(Node(Rect(-954, 2061, -921, 2094)), WINDOW))

    def test_inside_window_is_reachable(self):
        self.assertTrue(conversation.rect_in_window(Node(Rect(-954, 800, -921, 833)), WINDOW))

    def test_partially_visible_counts_as_reachable(self):
        self.assertTrue(conversation.rect_in_window(Node(Rect(-954, 1320, -921, 1360)), WINDOW))

    def test_tuple_rect_is_supported(self):
        self.assertFalse(conversation.rect_in_window(Node((-954, 2061, -921, 2094)), WINDOW))

    def test_missing_window_or_rect_means_unknown_but_allowed(self):
        self.assertTrue(conversation.rect_in_window(Node(Rect(0, 9999, 1, 9999)), None))
        self.assertTrue(conversation.rect_in_window(object(), WINDOW))


class WiringTests(unittest.TestCase):
    def test_chatgpt_uses_uia_text_fallback(self):
        self.assertIn("chatgpt", conversation.UIA_TEXT_SITES)

    def test_offscreen_candidates_are_dropped(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("reachable = [n for n in candidates if rect_in_window(n, window)]", source)
        self.assertIn('"actions_offscreen"', source)


if __name__ == "__main__":
    unittest.main()
