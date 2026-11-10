"""R6 次要项：Gemini 的"有芯片/没芯片"必须分开报，页面不对时一个东西都不点。

2026-11-07 05:17:27 现场（hwnd=67552）：`gemini_choose --model argon` 只回
`menu_not_open / model=unknown`，所有模板分数 0.4~0.52；分不清是"不是 Gemini 页"
还是"这一页本来就没有模型芯片"。

只读复核（同一 hwnd）：地址是 `https://gemini.google.com/app`（新对话页），
UIA 按钮 `打开模式选择器，当前模式为“Flash”`，`read_model` 直接读回
`model=flash / source=uia`——**芯片在，读法也对**。缺的是"报清楚为什么没找到"。

这里钉住：
  1. 读得到芯片 -> 直接出结果，不开菜单、不点、不按键；
  2. 文档根本没绑上 -> `detail=document_unbound`，hint 带真实错误；
  3. 会话页（`/app/<id>`）没有芯片 -> hint 指明"只在新对话页出现"；
  4. 选模型时页面不对 -> **零点击**拒（老实现会去点麦克风左边 68px 的空白）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "src" / "scripts"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import gemini_choose as gc                      # noqa: E402
from qi import ui                               # noqa: E402
from test_r2_gemini_menu import FakeNode, FakeRoot, FakeScreen  # noqa: E402

NEW_CHAT = "https://gemini.google.com/app"
CONVERSATION = "https://gemini.google.com/app/abc123"


def with_doc(nodes, url):
    """绑定一份假 UIA 树，并把地址固定成 url。"""
    root = FakeRoot(nodes)
    return mock.patch.multiple(ui,
                               document=mock.Mock(return_value=(object(), url)),
                               tree=mock.Mock(return_value=root))


class ChipReadback(unittest.TestCase):
    def test_live_flash_chip_is_read_without_any_click(self):
        """真机现在的形态：`当前模式为“Flash”` -> flash，且不点任何东西。"""
        screen = FakeScreen()
        with with_doc([FakeNode("ButtonControl", "打开模式选择器，当前模式为“Flash”")],
                      NEW_CHAT):
            got = gc.read_model(screen)
        self.assertEqual(got["model"], "flash", got)
        self.assertEqual(got["source"], "uia")
        self.assertEqual(screen.clicks, [], "读得到就不许点")
        self.assertEqual(screen.keys, [], "也不许按 Esc")

    def test_unbound_document_is_reported_with_the_real_error(self):
        screen = FakeScreen()
        with mock.patch.object(ui, "document",
                               side_effect=RuntimeError("gemini: 目标文档数量为 0")):
            got = gc.read_model(screen)
        self.assertEqual(got["model"], "unknown")
        self.assertEqual(got["stage"], "menu_not_open", got)
        self.assertEqual(got["detail"], "document_unbound", got)
        self.assertIn("不是 Gemini 页面", got["hint"])
        self.assertIn("目标文档数量为 0", got["hint"], "要把真实错误带出来")
        self.assertEqual(screen.clicks, [], "读不到文档时不许乱点")

    def test_conversation_page_points_at_new_chat(self):
        screen = FakeScreen()          # 没有芯片，但麦克风在（老实现会去点偏移）
        with with_doc([FakeNode("TextControl", "Gemini")], CONVERSATION):
            got = gc.read_model(screen)
        self.assertEqual(got["stage"], "menu_not_open", got)
        self.assertEqual(got["detail"], "no_chip", got)
        self.assertIn("只在新对话页", got["hint"])
        self.assertIn("abc123", got["hint"])


class SetModelRefusesBeforeClicking(unittest.TestCase):
    def test_conversation_page_is_refused_with_zero_clicks(self):
        screen = FakeScreen()          # mic=True：老路径会点 (1100-68, 800)
        with with_doc([], CONVERSATION):
            got = gc.set_model(screen, "argon")
        self.assertFalse(got["ok"], got)
        self.assertEqual(got["stage"], "page_not_new_chat", got)
        self.assertEqual(screen.clicks, [], "页面上没有选择器，一个像素都不许点")
        self.assertEqual(screen.keys, [], "连 Esc 都不按")

    def test_unbound_document_is_refused_with_zero_clicks(self):
        screen = FakeScreen()
        with mock.patch.object(ui, "document", side_effect=RuntimeError("no docs")):
            got = gc.set_model(screen, "argon")
        self.assertFalse(got["ok"], got)
        self.assertEqual(got["stage"], "document_unbound", got)
        self.assertEqual(screen.clicks, [])
        self.assertEqual(screen.keys, [])

    def test_new_chat_page_with_a_chip_still_goes_through_the_normal_path(self):
        """正例对照：新对话页 + 认得出的芯片 -> 照常点芯片（不是一律拒）。"""
        class Screen(FakeScreen):
            def __init__(self):
                super().__init__(items=())
                self.moves = []

            def move_to(self, x, y):
                self.moves.append((x, y))

        screen = Screen()
        node = FakeNode("ButtonControl", "打开模式选择器，当前模式为“Flash”")

        def opens(_screen, _node):
            _screen.items = {f"gem_item_{k}" for k in gc.CFG.options}

        with with_doc([node], NEW_CHAT), \
                mock.patch.object(ui, "click", side_effect=opens) as clicker, \
                mock.patch.object(gc, "_evidence", return_value=None):
            got = gc.set_model(screen, "argon")
        clicker.assert_called_once()
        self.assertIs(clicker.call_args.args[1], node, "点的必须是芯片按钮本身")
        self.assertIn(got["stage"], ("done", "unverified"), got)
        self.assertTrue(screen.clicks, "菜单项该点的还是要点（不是一律拒）")


if __name__ == "__main__":
    unittest.main()
