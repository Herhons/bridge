"""S1：安全点击/按键原语的判定（离线，纯函数）。"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import safe_ui                              # noqa: E402

RECT = (0, 0, 1000, 800)


class PointTests(unittest.TestCase):
    def test_inside_target_is_allowed(self):
        v = safe_ui.decide_point(x=10, y=10, client_rect=RECT, hit_pid=7,
                                 hit_class="Chrome_WidgetWin_1", target_pid=7)
        self.assertTrue(v.ok, v)

    def test_desktop_window_is_refused(self):
        v = safe_ui.decide_point(x=10, y=10, client_rect=RECT, hit_pid=7,
                                 hit_class="WorkerW", target_pid=7)
        self.assertFalse(v.ok)
        self.assertIn("forbidden_window", v.reason)

    def test_other_process_is_refused(self):
        v = safe_ui.decide_point(x=10, y=10, client_rect=RECT, hit_pid=8,
                                 hit_class="Chrome_WidgetWin_1", target_pid=7)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "other_process")

    def test_outside_client_is_refused(self):
        v = safe_ui.decide_point(x=1200, y=10, client_rect=RECT, hit_pid=7,
                                 hit_class="Chrome_WidgetWin_1", target_pid=7)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "outside_client")


class KeyTests(unittest.TestCase):
    def test_dangerous_key_needs_editable_focus(self):
        v = safe_ui.decide_keys(keys=("ctrl", "a"), foreground_hwnd=1, target_hwnd=1,
                                focused_role="ButtonControl")
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "focused_not_editable")

    def test_dangerous_key_ok_in_edit(self):
        v = safe_ui.decide_keys(keys=("ctrl", "a"), foreground_hwnd=1, target_hwnd=1,
                                focused_role="EditControl")
        self.assertTrue(v.ok, v)

    def test_foreground_mismatch_is_refused(self):
        v = safe_ui.decide_keys(keys=("a",), foreground_hwnd=2, target_hwnd=1,
                                focused_role="EditControl")
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "foreground_not_target")

    def test_focus_moved_after_keys_is_detected(self):
        v = safe_ui.decide_keys(keys=("a",), foreground_hwnd=1, target_hwnd=1,
                                focused_role="EditControl", after_foreground=2)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "focus_moved_after_keys")

    def test_dangerous_set(self):
        self.assertTrue(safe_ui.is_dangerous(["Ctrl", "A"]))
        self.assertTrue(safe_ui.is_dangerous(["delete"]))
        self.assertFalse(safe_ui.is_dangerous(["Enter"]))


if __name__ == "__main__":
    unittest.main()
