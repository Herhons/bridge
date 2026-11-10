"""Bootstrap 卡：Codex 强度选择报 stops_unknown，但 --list 明明读到 Max。

2026-11-06 执行方实测（协调方转述）：
    Codex normal choose gpt61_sol/max：模型已经是 gpt61_sol；
    强度选择被拒，stage=stops_unknown（文字读数是 Max）。
    之后普通 --list 也读到 gpt61_sol Max。

根因：`_effort_now()` 用**档位词**（英文模板，精确）读到名字后就直接 return，
返回里没有 `stops`/`index`；而 `set_effort()` 必须靠 `STOP_SETS[stops]` 把目标
换算成"第几格"。于是"读到了 Max"却因为"档数未知"拒动滑杆。

修法：文字仍然是档位的唯一权威，但补一次刻度点数**只为补几何**
（`stops`/`index`），并标出两者是否一致（`ticks_agree`）。证据打架时不动滑杆。

本测试不碰真实窗口：档位词与刻度都用假件，Desk 只记录按键/点击。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "scripts"))

import codex_choose  # noqa: E402

WIN = (100, 100, 1400, 1000)
SLIDER = (1000, 1200, 300, 1150)          # left, right, row_y, knob


class FakeDesk:
    def __init__(self):
        self.presses = []
        self.clicks = []

    def click(self, *args):
        self.clicks.append(args)

    def press(self, key):
        self.presses.append(key)

    def grab(self, win):
        raise AssertionError("离线用例不该真的截图")


def five_stop_grid(knob, offset=0):
    """5 档卡片的刻度：low..max，档距 50；offset 用来看"点在别的档上"。"""
    points = [950.0 + 50 * i for i in range(5)]
    return points, 50.0


class EffortFromTextStillNeedsGeometry(unittest.TestCase):
    def read(self, *, text, grid, knob=1150):
        with mock.patch.object(codex_choose, "_effort_text", return_value=text), \
                mock.patch.object(codex_choose, "tick_grid", return_value=grid):
            return codex_choose._effort_now(FakeDesk(), WIN, (1000, 1200, 300, knob))

    def test_text_reading_also_fills_stops_and_index(self):
        got = self.read(text="max", grid=five_stop_grid(1150))
        self.assertEqual(got["effort"], "max")
        self.assertEqual(got["from"], "text")
        self.assertEqual(got["stops"], 5)
        self.assertEqual(got["index"], 4)
        self.assertTrue(got["ticks_agree"], got)
        self.assertTrue(got["at_max"])

    def test_text_reading_without_ticks_keeps_working_but_has_no_geometry(self):
        got = self.read(text="max", grid=None)
        self.assertEqual(got["effort"], "max")
        self.assertNotIn("stops", got)
        self.assertNotIn("ticks_agree", got)

    def test_disagreeing_ticks_are_flagged(self):
        got = self.read(text="max", grid=five_stop_grid(1100), knob=1100)
        self.assertEqual(got["effort"], "max")
        self.assertEqual(got["index"], 3)
        self.assertFalse(got["ticks_agree"], got)


class SetEffortGate(unittest.TestCase):
    def run_set(self, *, text, grid, key="max"):
        desk = FakeDesk()
        send = SimpleNamespace(center=(0, 0))
        with mock.patch.object(codex_choose, "_popup_state", return_value="effort"), \
                mock.patch.object(codex_choose, "_ensure_effort", return_value=True), \
                mock.patch.object(codex_choose, "locate",
                                  return_value={"send": send}), \
                mock.patch.object(codex_choose, "find_slider",
                                  return_value=(1000, 1200, 300, 1150)), \
                mock.patch.object(codex_choose, "_effort_text", return_value=text), \
                mock.patch.object(codex_choose, "tick_grid", return_value=grid), \
                mock.patch.object(codex_choose.time, "sleep"):
            got = codex_choose.set_effort(desk, WIN, key)
        return got, desk

    def test_max_is_no_longer_refused_when_the_label_was_read(self):
        got, desk = self.run_set(text="max", grid=five_stop_grid(1150))
        self.assertNotEqual(got["stage"], "stops_unknown", got)
        self.assertEqual(got["stage"], "done", got)
        self.assertEqual(got["before"], "max")
        self.assertTrue(desk.presses, "应当真的动过滑杆")
        self.assertIn("esc", desk.presses, "最后必须收卡片")

    def test_text_without_ticks_still_refuses_but_says_what_it_read(self):
        got, desk = self.run_set(text="max", grid=None)
        self.assertEqual(got["stage"], "stops_unknown")
        self.assertIn("档位词读到了 max", got["hint"])
        self.assertFalse(desk.presses, "没有几何就不许动滑杆")

    def test_conflicting_evidence_does_not_move_the_slider(self):
        # 滑块停在 1150，但刻度网格从 1150 起（= 第 0 档 low），文字却说 max
        got, desk = self.run_set(text="max", grid=([1150.0, 1200.0, 1250.0,
                                                    1300.0, 1350.0], 50.0))
        self.assertEqual(got["stage"], "stops_disagree", got)
        self.assertIn("max", got["hint"])
        self.assertFalse(desk.presses, "证据打架时不许动滑杆")
        self.assertFalse(desk.clicks, "也不许点圆点")


if __name__ == "__main__":
    unittest.main()
