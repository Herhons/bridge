"""探测副屏 Chrome 的标签页：逐个 Ctrl+N，读窗口标题。

只做"切换标签 + 读标题"，不点击页面。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import Screen, uilock  # noqa: E402

user32 = ctypes.windll.user32


def foreground_title() -> str:
    h = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, b, n + 1)
    return b.value


def find_secondary_chrome() -> int | None:
    pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        if "google chrome" not in b.value.lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        if 0 <= cx < pw and 0 <= cy < ph:
            return True
        if r.right - r.left < 200:
            return True
        found.append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def main() -> int:
    hwnd = find_secondary_chrome()
    print("副屏 Chrome hwnd:", hwnd)
    if hwnd is None:
        return 1
    print("当前前台:", foreground_title())

    s = Screen(verbose=False)
    if not s.bring_to_front("Google Chrome", on_secondary=True):
        print("置前失败")
        return 1
    time.sleep(0.5)

    for i in range(1, 9):
        s.hotkey("ctrl", str(i))
        time.sleep(0.9)
        print(f"  Ctrl+{i}: {foreground_title()}")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    raise SystemExit(uilock.run("probe_tabs_chrome", main))
