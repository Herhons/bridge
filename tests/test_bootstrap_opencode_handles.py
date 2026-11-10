"""Bootstrap 卡：`opencode_choose` 崩在 PrintWindow 的 64 位句柄上。

2026-11-06 执行方实测（协调方转述）：
    opencode normal choose claude_opus_55/max --idle180 --wait5
    → 失败在 PrintWindow：
      ctypes.ArgumentError argument2 OverflowError int too long to convert

根因：`qi.screen` 把 `gdi32.CreateCompatibleDC.restype` 声明成 64 位 `HDC`，
所以 `mdc` 是完整指针（可 > 2^31）；而 `opencode_choose.py` 从没声明过
`PrintWindow` 的参数类型，ctypes 按默认 32 位 int 传 → 第二个参数溢出。

这个测试不碰真实窗口：
  · 断言 Desk 用到的句柄/绘图函数都声明了指针类型的 argtypes/restype；
  · 用 `user32.IsWindow`（未被本仓库声明、且对任意句柄都安全）现场复现
    "未声明 → ArgumentError: int too long to convert；声明后 → 正常返回"，
    证明修的是这一类错误，而不是把异常吞掉。
"""
from offline_guard import ensure_offline
ensure_offline()

import ctypes
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "src"), str(ROOT / "src" / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import opencode_choose  # noqa: E402

BIG = 2 ** 40 + 1234        # 一个真实 64 位 HDC 的量级


class PrintWindowHandleTypes(unittest.TestCase):
    def test_handle_functions_used_by_desk_are_declared(self):
        user32, gdi32 = opencode_choose.user32, opencode_choose.gdi32
        expected = {
            (user32, "GetWindowDC"): ([wintypes.HWND], wintypes.HDC),
            (user32, "GetDC"): ([wintypes.HWND], wintypes.HDC),
            (user32, "ReleaseDC"): ([wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (user32, "PrintWindow"): ([wintypes.HWND, wintypes.HDC, ctypes.c_uint],
                                      wintypes.BOOL),
            (gdi32, "CreateCompatibleDC"): ([wintypes.HDC], wintypes.HDC),
            (gdi32, "CreateCompatibleBitmap"): ([wintypes.HDC, ctypes.c_int,
                                                 ctypes.c_int], wintypes.HBITMAP),
            (gdi32, "SelectObject"): ([wintypes.HDC, wintypes.HGDIOBJ],
                                      wintypes.HGDIOBJ),
            (gdi32, "DeleteObject"): ([wintypes.HGDIOBJ], wintypes.BOOL),
            (gdi32, "DeleteDC"): ([wintypes.HDC], wintypes.BOOL),
            (gdi32, "GetDIBits"): ([wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT], ctypes.c_int),
        }
        for (dll, name), (args, restype) in expected.items():
            with self.subTest(function=name):
                function = getattr(dll, name)
                # 没声明过时属性不存在（离线 guard 会把 PrintWindow 换成普通函数），
                # 用 getattr 默认值让失败信息是"None != 期望"，而不是 AttributeError
                self.assertEqual(getattr(function, "argtypes", None), args, name)
                self.assertEqual(getattr(function, "restype", None), restype, name)

    def test_bitblt_takes_three_handle_sized_arguments(self):
        bitblt = opencode_choose.gdi32.BitBlt
        self.assertEqual(bitblt.argtypes[:1], [wintypes.HDC])
        self.assertEqual(bitblt.argtypes[5], wintypes.HDC)
        self.assertEqual(bitblt.restype, wintypes.BOOL)

    def test_declaration_is_idempotent(self):
        before = list(opencode_choose.user32.PrintWindow.argtypes)
        opencode_choose._declare_win32()
        self.assertEqual(list(opencode_choose.user32.PrintWindow.argtypes), before)

    def test_undeclared_handle_is_the_reported_overflow_and_declared_one_is_not(self):
        """现场复现执行方看到的错误串，并证明声明后同样的值能传进去。"""
        is_window = ctypes.windll.user32.IsWindow
        saved = (is_window.argtypes, is_window.restype)
        try:
            is_window.argtypes = None          # 回到"未声明"：按默认 32 位 int 传参
            is_window.restype = ctypes.c_int
            with self.assertRaises(ctypes.ArgumentError) as caught:
                is_window(BIG)
            self.assertIn("int too long to convert", str(caught.exception))
            self.assertIn("argument 1", str(caught.exception))

            is_window.argtypes = [wintypes.HWND]
            is_window.restype = wintypes.BOOL
            self.assertEqual(is_window(BIG), 0, "不存在的窗口应当返回 0，而不是抛异常")
        finally:
            is_window.argtypes = saved[0]
            is_window.restype = saved[1]

    def test_desk_carries_the_full_width_hdc_into_printwindow(self):
        """用假 user32/gdi32 跑真实的 `Desk._printwindow`：
        `CreateCompatibleDC` 给的 64 位 HDC 必须原样交给 `PrintWindow`。"""
        calls = {}

        class FakeGdi:
            def CreateCompatibleDC(self, hdc):
                calls["create"] = hdc
                return BIG

            def CreateCompatibleBitmap(self, hdc, w, h):
                return 99

            def SelectObject(self, mdc, bmp):
                return 1

            def DeleteObject(self, bmp):
                return 1

            def DeleteDC(self, mdc):
                calls["delete_dc"] = mdc
                return 1

        class FakeUser:
            def GetWindowDC(self, hwnd):
                return 7

            def PrintWindow(self, hwnd, mdc, flags):
                calls["print"] = (hwnd, mdc, flags)
                return 0                      # 0 = 没画出来，正好不用真的取像素

            def ReleaseDC(self, hwnd, hdc):
                calls["release"] = (hwnd, hdc)
                return 1

        desk = opencode_choose.Desk(4242)
        with mock.patch.object(opencode_choose, "user32", FakeUser()), \
                mock.patch.object(opencode_choose, "gdi32", FakeGdi()), \
                mock.patch.object(opencode_choose, "rect_of",
                                  return_value=(0, 0, 640, 480)):
            self.assertIsNone(desk._printwindow())
        self.assertEqual(calls["create"], 7)
        self.assertEqual(calls["print"], (4242, BIG, 2), "HDC 不许被截成 32 位")
        self.assertEqual(calls["delete_dc"], BIG)
        self.assertEqual(calls["release"], (4242, 7))



if __name__ == "__main__":
    unittest.main()
