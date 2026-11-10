"""B17：dsh_choose / dsh_use 的几何与读选函数必须逐字一致。

B16 只改了 choose 一侧（四态 open_menu / _menu_fail / 双形状兼容），
use 侧仍是旧形状；几何再各改一遍必然走样。`test_r3_choose_use_lockstep.py`
已经给 opencode 立过这个规矩，这里照样搬到 dsh。
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

SHARED = (
    "_geo", "calibrate", "_click_x", "_effort_y0", "_band", "chip_chevron",
    "menu_rows", "menu_open", "open_menu", "_menu_fail", "panel_bbox",
    "close_all", "read_effort", "read_model", "set_model", "set_effort",
    "list_state", "band_top", "send_state", "probe",
)


def functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return {node.name: ast.dump(ast.Module(body=[node], type_ignores=[]))
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in SHARED}


class LockstepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.choose = functions(CHOOSE)
        cls.use = functions(USE)
        cls.choose_text = CHOOSE.read_text(encoding="utf-8-sig")
        cls.use_text = USE.read_text(encoding="utf-8-sig")

    def test_every_shared_function_is_present_in_both(self):
        self.assertEqual(set(SHARED), set(self.choose))
        self.assertEqual(set(SHARED), set(self.use))

    def test_shared_functions_are_identical(self):
        for name in SHARED:
            with self.subTest(function=name):
                self.assertEqual(self.choose[name], self.use[name])

    def test_tier_gate_is_not_weakened(self):
        """几何修好后 read 应能读到真值，不许靠关 readback 蒙混过关。"""
        self.assertNotIn("require_readback=False", self.use_text)
        self.assertIn('tier_guard.gate("dsh", purpose', self.use_text)
        self.assertIn("_reread_tier", self.use_text)

    def test_both_mains_calibrate_before_use(self):
        for text in (self.choose_text, self.use_text):
            self.assertIn('globals().get("calibrate")', text)
            self.assertIn("win = rect_of(hwnd)", text)

    def test_choose_keeps_its_legacy_entry_shape(self):
        # test_b16_dsh_choose.EntryGuardTests 钉着这些字面串
        self.assertIn('code = _lock.entry("dsh_choose", main)', self.choose_text)
        self.assertIn("idle_seconds()", self.choose_text)
        self.assertNotIn("def _main(", self.choose_text)


if __name__ == "__main__":
    unittest.main()
