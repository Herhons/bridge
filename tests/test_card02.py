"""任务卡 02：visible_editor 在多个可见输入框时按面积挑，不再直接报错。

全部用假节点，不碰真窗口、不 import uiautomation 以外的东西。
"""

from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi import ui
from qi.conversation import MIN_EDITOR, visible_editor


def editor(name, box, key=1, enabled=True, offscreen=False):
    """一个假 EditControl：宽高取自 box=(left, top, right, bottom)。"""
    control = SimpleNamespace(IsEnabled=enabled, IsOffscreen=offscreen,
                              BoundingRectangle=SimpleNamespace(
                                  left=box[0], top=box[1], right=box[2], bottom=box[3]))
    return ui.Node("EditControl", name, control)


def root(*children):
    node = ui.Node("DocumentControl", "", SimpleNamespace(IsEnabled=True, IsOffscreen=False))
    node.children = list(children)
    for child in children:
        child.parent = node
    return node


class VisibleEditorTests(unittest.TestCase):
    def test_two_editors_takes_the_larger_one(self):
        """WorkBuddy 实测：一个 900x120 的输入框 + 一个 30x20 的空框。"""
        small = editor("", (1000, 800, 1030, 820))
        big = editor("", (200, 700, 1100, 820))
        self.assertIs(visible_editor(root(small, big), "workbuddy"), big)
        self.assertIs(visible_editor(root(big, small), "workbuddy"), big)

    def test_small_box_is_dropped_even_if_it_comes_last(self):
        """顺序不能影响结果：小框在前在后，都取大框。"""
        small = editor("", (0, 0, 39, 500))          # 宽 39 < 40
        tall = editor("", (0, 0, 40, 500))           # 宽 40 = 门槛，保留
        self.assertIs(visible_editor(root(small, tall), "dsh"), tall)

    def test_height_below_threshold_counts_as_small(self):
        small = editor("", (0, 0, 600, 39))
        big = editor("", (0, 0, 600, 40))
        self.assertIs(visible_editor(root(small, big), "workbuddy"), big)

    def test_area_decides_not_the_width(self):
        """面积最大的那个赢，不是最宽或最高的那个。"""
        wide_short = editor("", (0, 0, 900, 60))     # 54000
        narrow_tall = editor("", (0, 0, 300, 200))   # 60000
        self.assertIs(visible_editor(root(wide_short, narrow_tall), "workbuddy"), narrow_tall)

    def test_single_editor_is_returned_as_is(self):
        """只有一个输入框时行为不变，不做任何过滤。"""
        only = editor("", (0, 0, 900, 120))
        self.assertIs(visible_editor(root(only), "workbuddy"), only)
        tiny = editor("", (0, 0, 10, 10))             # 单框就算小也照旧返回
        self.assertIs(visible_editor(root(tiny), "workbuddy"), tiny)

    def test_no_editor_raises(self):
        with self.assertRaises(RuntimeError):
            visible_editor(root(), "workbuddy")

    def test_all_editors_too_small_raises_with_counts(self):
        """多框但全被过滤掉：报错，并且说清有几个被排除。"""
        a = editor("", (0, 0, 30, 20))
        b = editor("", (0, 0, 20, 30))
        with self.assertRaises(RuntimeError) as ctx:
            visible_editor(root(a, b), "workbuddy")
        self.assertIn("找到 0 个控件", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_hidden_and_disabled_editors_are_ignored_first(self):
        """不可见/禁用的框先出局，剩下的只有一个时照旧返回。"""
        hidden = editor("", (0, 0, 900, 120), offscreen=True)
        disabled = editor("", (0, 0, 900, 120), enabled=False)
        real = editor("", (0, 0, 900, 120))
        self.assertIs(visible_editor(root(hidden, disabled, real), "workbuddy"), real)

    def test_search_box_is_still_excluded(self):
        search = editor("搜索", (0, 0, 900, 120))
        composer = editor("", (0, 0, 900, 120))
        self.assertIs(visible_editor(root(search, composer), "workbuddy"), composer)

    def test_grok_combo_box_still_counts(self):
        """grok 的输入框是 ComboBox，改动不能把它弄丢。"""
        combo = ui.Node("ComboBoxControl", "向 Grok 提任何问题",
                        SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                        BoundingRectangle=SimpleNamespace(
                                            left=0, top=0, right=900, bottom=120)))
        self.assertIs(visible_editor(root(combo), "grok"), combo)
        other = editor("", (0, 0, 900, 120))
        self.assertIs(visible_editor(root(combo, other), "grok"), other)

    def test_threshold_is_forty_pixels(self):
        self.assertEqual(MIN_EDITOR, 40)


if __name__ == "__main__":
    unittest.main()
