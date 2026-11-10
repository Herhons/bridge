"""UI-BOOTSTRAP-2 / Gemini：菜单识别要认**控制身份**，认不出就 fail-closed。

2026-11-07 现场：`gemini_choose --model argon` 报 menu_not_open + model=unknown，
旧模板分数 0.4~0.52。旧实现开菜单靠"麦克风往左 68px"这个像素偏移，芯片一挪就点空。

这里全部离线：假 Screen + 假 UIA 树。
  · 有 UIA 芯片按钮时，`read_model` 直接读文本（**不开菜单**）、`_open_model_menu`
    点的是那个按钮本身；
  · 芯片点到了但菜单项模板全失配 → `menu_items_unreadable`（模板过期的信号），
    绝不点任何选项、绝不下调阈值；
  · 连芯片都认不出来 → `menu_not_open`，同样不点。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "src" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "scripts"))

import gemini_choose as gc  # noqa: E402
from qi import ui  # noqa: E402

WIN = (100, 100, 1200, 900)


class Match:
    def __init__(self, x, y, w=20, h=20, score=0.99):
        self.x, self.y, self.width, self.height, self.score = x, y, w, h, score

    @property
    def center(self):
        return (self.x + self.width // 2, self.y + self.height // 2)


class FakeScreen:
    """只实现 gemini_choose 用到的那几个方法。"""

    def __init__(self, matches=None, items=(), mic=True, opens_on_click=False):
        self.matches = dict(matches or {})
        self.items = set(items)
        self.mic = mic
        self.opens_on_click = opens_on_click
        self.clicks = []
        self.keys = []
        self.target_hwnd = 77

    def window_rect(self, hint, on_secondary=False):
        return WIN

    def find(self, name, threshold=0.85, region=None):
        if name.startswith("gem_item_"):
            return Match(300, 500) if name in self.items else None
        if name == "gem_mic":
            return Match(1100, 800) if self.mic else None
        return self.matches.get(name)

    def click(self, m, *, offset=(0, 0)):
        cx, cy = m.center
        self.clicks.append((cx + offset[0], cy + offset[1]))
        if self.opens_on_click:                      # 点一下菜单真的开出来了
            self.items = {f"gem_item_{k}" for k in gc.CFG.options}

    def press(self, key):
        self.keys.append(key)

    def grab(self, region=None):
        return np.full((20, 20, 3), 30, dtype=np.uint8)


class FakeNode:
    def __init__(self, kind, name, rect=(0, 0, 10, 10)):
        self.kind = kind
        self.name = name
        self.control = SimpleNamespace(BoundingRectangle=SimpleNamespace(
            left=rect[0], top=rect[1], right=rect[2], bottom=rect[3]))


class FakeRoot:
    def __init__(self, nodes):
        self.nodes = nodes

    def walk(self):
        return iter(self.nodes)


def with_tree(nodes):
    root = FakeRoot(nodes)
    return mock.patch.multiple(ui, document=mock.Mock(return_value=(object(), "u")),
                               tree=mock.Mock(return_value=root))


class ChipIdentity(unittest.TestCase):
    def test_chip_button_is_found_by_name(self):
        node = FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")
        with with_tree([FakeNode("TextControl", "Gemini"), node]):
            got = gc._chip_node(FakeScreen())
        self.assertIsNotNone(got)
        self.assertIs(got[0], node)

    def test_no_chip_button_returns_none(self):
        with with_tree([FakeNode("ButtonControl", "发送"), FakeNode("TextControl", "hi")]):
            self.assertIsNone(gc._chip_node(FakeScreen()))

    def test_tree_failure_is_not_fatal(self):
        with mock.patch.object(ui, "document", side_effect=RuntimeError("no docs")):
            self.assertIsNone(gc._chip_node(FakeScreen()))

    def test_tail_parses_both_wording_variants(self):
        self.assertEqual(gc._chip_tail("打开模式选择器，当前模式为“Argon”"), "Argon")
        self.assertEqual(gc._chip_tail("打开模型选择器，当前模型为“3.5 Flash-Lite”"),
                         "3.5 Flash-Lite")

    def test_uia_text_maps_to_known_key_or_unknown(self):
        with with_tree([FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")]):
            got = gc._model_from_uia(FakeScreen())
        self.assertEqual(got["model"], "argon", got)
        self.assertEqual(got["source"], "uia")

        with with_tree([FakeNode("ButtonControl", "打开模式选择器，当前模式为“5 Nova”")]):
            got = gc._model_from_uia(FakeScreen())
        self.assertEqual(got["model"], "unknown")
        self.assertIn("5 Nova", got["hint"])


class ReadModel(unittest.TestCase):
    def test_reads_from_uia_without_opening_any_menu(self):
        screen = FakeScreen()
        with with_tree([FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")]):
            got = gc.read_model(screen)
        self.assertEqual(got["model"], "argon")
        self.assertEqual(screen.clicks, [], "UIA 读得到时不许点任何东西")
        self.assertEqual(screen.keys, [], "也不许按 Esc 开关菜单")

    def test_falls_back_to_menu_items_when_uia_missing(self):
        screen = FakeScreen(items=[f"gem_item_{k}" for k in gc.CFG.options])
        with with_tree([]):
            got = gc.read_model(screen)
        self.assertIn(got["model"], ("unknown", *gc.CFG.options))

    def test_unreadable_menu_reports_items_unreadable(self):
        # 没有 UIA 芯片 -> 走菜单路径 -> 点了但菜单项模板一个也没匹到
        screen = FakeScreen(items=[])
        with with_tree([FakeNode("TextControl", "Gemini")]):
            got = gc.read_model(screen)
        self.assertEqual(got["model"], "unknown")
        self.assertEqual(got["stage"], "menu_items_unreadable")
        self.assertIn("模板", got["hint"])
        self.assertTrue(screen.clicks, "该点的芯片还是要点的")


class OpenMenu(unittest.TestCase):
    def test_clicks_the_uia_chip_and_confirms_items(self):
        screen = FakeScreen(items=(), opens_on_click=True)
        node = FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”",
                        rect=(400, 700, 480, 730))
        def opens(_screen, _node):
            screen.items = {f"gem_item_{k}" for k in gc.CFG.options}

        with with_tree([node]), mock.patch.object(ui, "click", side_effect=opens) as clicker:
            self.assertEqual(gc._open_model_menu(screen), "open")
        clicker.assert_called_once()
        self.assertIs(clicker.call_args.args[1], node, "点的必须是芯片按钮本身")

    def test_mic_offset_is_only_a_fallback(self):
        screen = FakeScreen(items=(), opens_on_click=True)
        with with_tree([]):
            self.assertEqual(gc._open_model_menu(screen), "open")
        self.assertEqual(screen.clicks, [(1110 + gc.MIC_TO_CARET[0], 810)])

    def test_no_chip_and_no_mic_is_no_chip(self):
        screen = FakeScreen(mic=False)
        with with_tree([]):
            self.assertEqual(gc._open_model_menu(screen), "no_chip")
        self.assertEqual(screen.clicks, [])


class SetModelFailsClosed(unittest.TestCase):
    def test_clicked_chip_but_items_unreadable_never_clicks_an_item(self):
        screen = FakeScreen(items=[])
        node = FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")
        with with_tree([node]), mock.patch.object(ui, "click"), \
                mock.patch.object(gc, "_evidence", return_value="shot.png"):
            got = gc.set_model(screen, "pro")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "menu_items_unreadable", got)
        self.assertEqual(got["evidence_shot"], "shot.png")
        self.assertEqual(screen.clicks, [], "一个选项都不许点")

    def test_no_chip_at_all_is_menu_not_open(self):
        screen = FakeScreen(mic=False)
        with with_tree([]), mock.patch.object(gc, "_evidence", return_value=None):
            got = gc.set_model(screen, "pro")
        self.assertEqual(got["stage"], "menu_not_open")
        self.assertEqual(screen.clicks, [])

    def test_already_on_the_requested_model_is_a_noop(self):
        screen = FakeScreen()
        with with_tree([FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")]):
            got = gc.set_model(screen, "argon")
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "already")
        self.assertEqual(screen.clicks, [])


if __name__ == "__main__":
    unittest.main()
