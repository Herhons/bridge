"""B17：dsh 的 composer 几何必须由「全窗找到的发送键」推导。

现场：首页 composer 居中（send 在窗口 y≈768/1340），会话页贴底（y≈1277）。
旧的 `_band()` 固定取底部 170px，首页里 `dsh_send` 全窗 1.000 却在带外 →
`chip_not_found`。这里钉住校准值、回退契约与「测试桩最小命名空间」兼容。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

CHOOSE = ROOT / "src" / "scripts" / "dsh_choose.py"
USE = ROOT / "src" / "scripts" / "dsh_use.py"
GEO_NAMES = ("_geo", "calibrate", "_click_x", "_effort_y0", "_band", "band_top")


def load(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    defs = [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names]
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + defs, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class FakeDesk:
    def __init__(self, hit):
        self.hit = hit
        self.regions = []

    def find(self, name, threshold, region=None):
        self.regions.append((name, threshold, region))
        return self.hit


def make(path=CHOOSE, names=GEO_NAMES):
    return load(path, names, {"TPL_SEND": "dsh_send", "BAND_H": 170})


class CalibrateTests(unittest.TestCase):
    WIN = (0, 0, 1380, 1340)

    def test_home_page_values_come_from_the_send_hit(self):
        ns = make()
        desk = FakeDesk((1146, 750, 36, 36, 1.0))
        self.assertTrue(ns["calibrate"](desk, self.WIN))
        geo = ns["_geo"](self.WIN)
        self.assertEqual(geo["send"], (1164, 768))
        self.assertEqual(geo["top"], 638)
        self.assertEqual((geo["menu_x0"], geo["menu_x1"]), (864, 1184))
        self.assertEqual((geo["menu_y0"], geo["menu_y1"]), (658, 748))
        self.assertEqual(geo["click_x"], 954)
        self.assertEqual(geo["effort_y0"], 508)
        # 全窗匹配，不是底部 band
        self.assertEqual(desk.regions[0][2], self.WIN)

    def test_band_and_helpers_follow_the_cache(self):
        ns = make()
        ns["calibrate"](FakeDesk((1146, 750, 36, 36, 1.0)), self.WIN)
        self.assertEqual(ns["_band"](self.WIN), (0, 638, 1380, 170))
        self.assertEqual(ns["band_top"](self.WIN), 638)
        self.assertEqual(ns["_click_x"](self.WIN), 954)
        self.assertEqual(ns["_effort_y0"](self.WIN), 508)

    def test_no_hit_falls_back_to_the_old_bottom_band(self):
        ns = make()
        self.assertFalse(ns["calibrate"](FakeDesk(None), self.WIN))
        self.assertEqual(ns["_band"](self.WIN), (0, 1170, 1380, 170))
        self.assertIsNone(ns["_geo"](self.WIN))

    def test_malformed_hit_is_rejected(self):
        for hit in ([1, 2, 3], (1, 2, 3, 4), (1, 2, 3, 4, 0.4),
                    object(), (1, 2, 3, 4, 5, 6)):
            with self.subTest(hit=hit):
                ns = make()
                self.assertFalse(ns["calibrate"](FakeDesk(hit), self.WIN))
                self.assertEqual(ns["_band"](self.WIN)[1], 1170)

    def test_find_exception_does_not_poison_geometry(self):
        class Broken:
            def find(self, *args):
                raise RuntimeError("grab failed")

        ns = make()
        self.assertFalse(ns["calibrate"](Broken(), self.WIN))
        self.assertEqual(ns["_band"](self.WIN)[1], 1170)

    def test_window_change_invalidates_the_cache(self):
        ns = make()
        ns["calibrate"](FakeDesk((1146, 750, 36, 36, 1.0)), self.WIN)
        self.assertIsNone(ns["_geo"]((0, 0, 1296, 828)))
        self.assertEqual(ns["_band"]((0, 0, 1296, 828))[1], 658)

    def test_session_page_still_looks_like_the_old_bottom_band(self):
        ns = make()
        ns["calibrate"](FakeDesk((1146, 1259, 36, 36, 1.0)), self.WIN)
        top = ns["_band"](self.WIN)[1]
        self.assertGreaterEqual(top, 1100)
        self.assertLessEqual(top, 1170)


class CompatibilityTests(unittest.TestCase):
    def test_minimal_namespace_has_no_new_global_dependency(self):
        """AST 桩测只给 BAND_H，不能因为新辅助函数就 NameError。"""
        for path in (CHOOSE, USE):
            with self.subTest(file=path.name):
                ns = load(path, ("_band", "band_top"), {"BAND_H": 170})
                self.assertEqual(ns["_band"]((0, 0, 1380, 1340)),
                                 (0, 1170, 1380, 170))
                self.assertEqual(ns["band_top"]((0, 0, 1380, 1340)), 1170)

    def test_both_scripts_calibrate_identically(self):
        a = make(CHOOSE)
        b = make(USE)
        hit = (1146, 750, 36, 36, 1.0)
        a["calibrate"](FakeDesk(hit), (0, 0, 1380, 1340))
        b["calibrate"](FakeDesk(hit), (0, 0, 1380, 1340))
        self.assertEqual(a["_band"]((0, 0, 1380, 1340)),
                         b["_band"]((0, 0, 1380, 1340)))
        self.assertEqual(a["_geo"]((0, 0, 1380, 1340)),
                         b["_geo"]((0, 0, 1380, 1340)))


class EvidenceReplayTests(unittest.TestCase):
    """用 R4 真机截图离线回放：校准后必须真的读得出档位与 ⌄。"""

    @classmethod
    def setUpClass(cls):
        import cv2
        import numpy as np

        cls.cv2, cls.np = cv2, np
        cls.image = cv2.imread(str(ROOT / "evidence" / "dsh_menu_not_open_model.png"))
        cls.menu_image = cv2.imread(str(ROOT / "evidence" / "batch17" /
                                        "dsh_menu_open_S1.png"))
        cls.win = (0, 0, 1380, 1340)
        names = GEO_NAMES + ("chip_chevron", "read_model", "read_effort",
                             "menu_rows")
        cls.ns = load(CHOOSE, names, {
            "cv2": cv2, "np": np, "TPL_SEND": "dsh_send",
            "TPL_CHEV": "dsh_chevron", "BAND_H": 170,
            "MODELS": {"deepseek_v41_flash": "dsh_m_deepseek",
                       "gpt6_luna": "dsh_m_luna"},
            "MODEL_THR": 0.90,
            "TPL_E": {"off": "dsh_e_0", "low": "dsh_e_1",
                      "high": "dsh_e_2", "max": "dsh_e_3"},
            "EFFORT_LABEL": {"off": "Off", "low": "Low",
                             "high": "High", "max": "Max"},
        })

        class Desk:
            def __init__(self, full):
                self.full = full
                self.tpls = {}

            def tpl(self, name):
                if name not in self.tpls:
                    self.tpls[name] = cv2.imread(
                        str(ROOT / "src" / "assets" / "library" / (name + ".png")),
                        cv2.IMREAD_GRAYSCALE)
                return self.tpls[name]

            def grab(self, region=None, *, fresh=False):
                if region is None:
                    return self.full.copy()
                x, y, w, h = (int(v) for v in region)
                return self.full[y:y + h, x:x + w].copy()

            def find(self, name, threshold=0.9, region=None):
                img = self.grab(region)
                hay = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                tpl = self.tpl(name)
                if tpl is None or tpl.shape[0] > hay.shape[0] or tpl.shape[1] > hay.shape[1]:
                    return None
                _, mx, _, loc = cv2.minMaxLoc(
                    cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED))
                if mx < threshold:
                    return None
                ox, oy = (region[0], region[1]) if region else (0, 0)
                return (ox + loc[0], oy + loc[1], tpl.shape[1], tpl.shape[0], float(mx))

        cls.desk = Desk(cls.image)

    def setUp(self):
        self.assertTrue(self.ns["calibrate"](self.desk, self.win))

    def test_band_covers_the_centered_composer(self):
        self.assertEqual(self.ns["_band"](self.win), (0, 638, 1380, 170))
        self.assertEqual(self.ns["_geo"](self.win)["send"], (1164, 768))

    def test_model_and_effort_are_readable_again(self):
        model = self.ns["read_model"](self.desk, self.win)
        self.assertEqual(model["model"], "deepseek_v41_flash")
        self.assertGreaterEqual(model["score"], 0.90)
        effort = self.ns["read_effort"](self.desk, self.win)
        self.assertEqual(effort["effort"], "max")

    def test_chevron_lands_on_the_real_arrow(self):
        x, y = self.ns["chip_chevron"](self.desk, self.win)
        self.assertTrue(1055 <= x <= 1085, x)
        self.assertTrue(750 <= y <= 790, y)

    def test_menu_rows_finds_both_rows_in_the_real_open_menu(self):
        desk = type(self.desk)(self.menu_image)
        self.assertTrue(self.ns["calibrate"](desk, self.win))
        rows = self.ns["menu_rows"](desk, self.win)
        self.assertGreaterEqual(len(rows), 2, rows)
        self.assertTrue(any(680 <= (a + b) // 2 <= 710 for a, b in rows), rows)
        self.assertTrue(any(715 <= (a + b) // 2 <= 745 for a, b in rows), rows)

    def test_closed_menu_does_not_look_open(self):
        rows = self.ns["menu_rows"](self.desk, self.win)
        self.assertFalse(any(680 <= (a + b) // 2 <= 745 for a, b in rows), rows)


if __name__ == "__main__":
    unittest.main()

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/dsh_menu_not_open_model.png', 'evidence/batch17/dsh_menu_open_S1.png') if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
