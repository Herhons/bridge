"""UI-BOOTSTRAP-2 / opencode：空白新会话的**居中** composer 也要能认出来。

2026-11-07 现场（1296x808 窗口，空白新会话）：
    发送键 (1051,421)+30x30、模型文字 (339,427)+131x20、⌄ (476,432)、
    占位文字 (225,387)+290x20 —— 全都在**页面中央**，
    而旧实现把搜索范围锁在"距底 170px"（y 638..808），于是
    `--list` 读到 model=unknown / chevrons=0。

这里不碰真实窗口：用**真实模板**拼一张 1296x808 的合成窗口图，
再让真正的 `Desk.find/find_all`（真模板、真 matchTemplate）在上面找。
换位置（贴底）也测一遍，证明判据与"离底多远"无关。
"""
from offline_guard import ensure_offline
ensure_offline()

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "src" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "scripts"))

import opencode_choose as oc  # noqa: E402

TEAM = "src/assets/library"
WIN = (0, 0, 1296, 808)
PAGE = 255          # 空白页的白


def tpl(name):
    img = cv2.imread(os.path.join(ROOT, TEAM, name + ".png"), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(name)
    return img


def paste(canvas, name, x, y):
    img = tpl(name)
    h, w = img.shape[:2]
    canvas[y:y + h, x:x + w] = img


def composer_canvas(*, top=387, model="oc_m_glm53free", chevron_x=476, send_x=1051):
    """空白新会话的一张合成窗口图；top = 占位文字的上沿。"""
    canvas = np.full((WIN[3], WIN[2], 3), PAGE, dtype=np.uint8)
    paste(canvas, "oc_placeholder", 225, top)
    paste(canvas, model, 339, top + 40)
    paste(canvas, "oc_chevron", chevron_x, top + 45)
    paste(canvas, "oc_send", send_x, top + 34)
    return canvas


class SyntheticDesk(oc.Desk):
    """真的 Desk（真模板匹配），只是把抓图换成内存里的合成图。"""

    def __init__(self, canvas, hwnd=4242):
        super().__init__(hwnd)
        self.canvas = canvas
        self.clicks = []
        self.keys = []

    def grab(self, region=None, *, fresh=False):
        if region is None:
            return self.canvas.copy()
        x, y, w, h = (int(v) for v in region)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.canvas.shape[1], x + w), min(self.canvas.shape[0], y + h)
        if x1 <= x0 or y1 <= y0:
            return np.zeros((0, 0, 3), np.uint8)
        return self.canvas[y0:y1, x0:x1].copy()

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))

    def press(self, key):
        self.keys.append(key)

    def hotkey(self, *keys):
        self.keys.append("+".join(keys))

    def type_text(self, text):
        self.keys.append("type:" + text)


def desk_for(canvas):
    return SyntheticDesk(canvas)


class CenteredComposer(unittest.TestCase):
    def setUp(self):
        # UIA 在合成图里没有；显式关掉，走模板路径（另有用例单测 UIA）
        patcher = mock.patch.object(oc, "_uia_root", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_composer_row_is_found_in_the_middle_of_the_page(self):
        d = desk_for(composer_canvas())
        row = oc.composer_row(d, WIN)
        self.assertIsNotNone(row, "居中 composer 必须能被找到")
        self.assertEqual(row["source"], "send")
        self.assertGreater(row["y"], 400)
        self.assertLess(row["y"], 500, "这是页面中央，不是底部带")

    def test_chevrons_are_found_next_to_the_model_text(self):
        d = desk_for(composer_canvas())
        got = oc.chevrons(d, WIN)
        self.assertEqual(len(got), 1, got)
        x, y, _score = got[0]
        self.assertAlmostEqual(x, 481, delta=2)
        self.assertAlmostEqual(y, 436, delta=2)

    def test_model_is_read_with_control_identity(self):
        d = desk_for(composer_canvas())
        got = oc.read_model(d, WIN)
        self.assertEqual(got["model"], "glm_53_flash_free", got)
        self.assertEqual(got["source"], "template")
        self.assertGreater(got["score"], 0.90)

    def test_same_composer_at_the_bottom_still_works(self):
        d = desk_for(composer_canvas(top=560))
        row = oc.composer_row(d, WIN)
        self.assertIsNotNone(row)
        self.assertGreater(row["y"], 600)
        self.assertEqual(oc.read_model(d, WIN)["model"], "glm_53_flash_free")

    def test_home_without_a_composer_fails_closed(self):
        canvas = np.full((WIN[3], WIN[2], 3), PAGE, dtype=np.uint8)
        paste(canvas, "oc_m_glm53free", 420, 13)      # 会话列表里的历史标题
        d = desk_for(canvas)
        self.assertIsNone(oc.composer_row(d, WIN))
        self.assertEqual(oc.chevrons(d, WIN), [])
        got = oc.read_model(d, WIN)
        self.assertEqual(got["model"], "unknown")
        self.assertIn("会话", got["hint"])

    def test_model_text_far_from_any_chevron_is_not_confirmed(self):
        canvas = composer_canvas()
        # 把 ⌄ 挪远：文字还在，但控制身份不成立
        canvas[432:440, 476:486] = PAGE
        paste(canvas, "oc_chevron", 800, 432)
        d = desk_for(canvas)
        got = oc.read_model(d, WIN)
        self.assertEqual(got["model"], "unknown", got)
        self.assertIn("控制身份", got["hint"])

    def test_send_state_reads_brightness_not_only_score(self):
        # 模板本身是深色（启用态）箭头；CCOEFF 对亮度不敏感，所以两种状态分数都高，
        # 只能看这块的平均亮度 —— 这正是 send_state 存在的理由。
        d = desk_for(composer_canvas())
        self.assertEqual(oc.send_state(d, WIN), "ready", "深色=启用")

        canvas = composer_canvas()
        y, x = 421, 1051
        patch = canvas[y:y + 30, x:x + 30].astype(np.float32)
        # 保结构、抬亮度（CCOEFF 对 a*x+b 不敏感，所以模板仍能匹上，但平均亮度变了）
        gray = patch.mean(axis=2, keepdims=True)
        canvas[y:y + 30, x:x + 30] = np.clip(150 + (gray - gray.mean()) * 0.6, 0, 255)
        self.assertEqual(oc.send_state(desk_for(canvas), WIN), "disabled")

    def test_list_state_reports_composer_source(self):
        d = desk_for(composer_canvas())
        out = oc.list_state(d, WIN)
        self.assertEqual(out["model"], "glm_53_flash_free")
        self.assertEqual(out["composer"], "send")
        self.assertEqual(out["chevrons"], 1)


class UiaChip(unittest.TestCase):
    """UIA 芯片按钮（实测 `OpenCode / GLM-5.3-Flash Free`）。"""

    class Node:
        def __init__(self, kind, name, rect=None):
            self.kind = kind
            self.name = name
            self.control = SimpleNamespace(
                BoundingRectangle=SimpleNamespace(**(rect or dict(
                    left=0, top=0, right=10, bottom=10))))

    class Root:
        def __init__(self, nodes):
            self.nodes = nodes

        def walk(self):
            return iter(self.nodes)

    def test_button_text_gives_exact_model_and_identity(self):
        root = self.Root([self.Node("DocumentControl", "OpenCode"),
                          self.Node("ButtonControl", "OpenCode / GLM-5.3-Flash Free")])
        with mock.patch.object(oc, "_uia_root", return_value=root):
            got = oc.read_model(SimpleNamespace(hwnd=1), WIN)
        self.assertEqual(got["model"], "glm_53_flash_free", got)
        self.assertEqual(got["source"], "uia")
        self.assertEqual(got["text"], "OpenCode / GLM-5.3-Flash Free")

    def test_unknown_button_text_is_not_guessed(self):
        root = self.Root([self.Node("ButtonControl", "OpenCode / Some New Model")])
        with mock.patch.object(oc, "_uia_root", return_value=root):
            got = oc.read_model(SimpleNamespace(hwnd=1), WIN)
        self.assertEqual(got["model"], "unknown")
        self.assertIn("Some New Model", got["hint"])

    def test_open_model_panel_clicks_the_uia_chip_first(self):
        node = self.Node("ButtonControl", "OpenCode / GLM-5.3-Flash Free",
                         dict(left=300, top=420, right=500, bottom=452))
        root = self.Root([node])
        d = SyntheticDesk(np.full((WIN[3], WIN[2], 3), PAGE, dtype=np.uint8))
        with mock.patch.object(oc, "_uia_root", return_value=root):
            ok = oc.open_model_panel(d, WIN)
        self.assertFalse(ok, "合成图里没有面板，打不开是预期")
        self.assertEqual(d.clicks, [(400, 436)], "必须先点 UIA 认出来的芯片按钮中心")


if __name__ == "__main__":
    unittest.main()
