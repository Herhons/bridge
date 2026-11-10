"""在副屏开一个网页窗口，**不抢焦点**。

做法：ShellExecuteW 用 SW_SHOWMINNOACTIVE 起 Chrome（窗口最小化、不激活），
等进程出来以后再用 SetWindowPos + SWP_NOACTIVATE 把它挪到副屏并恢复。
这样用户主屏上的东西一直不会被盖住，也不会被抢键盘。

    python tools/open_web.py https://chatgpt.com/ [--size 1390,1300]
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def windows():
    out = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out[int(hwnd)] = (buf.value, r.left, r.top, r.right - r.left, r.bottom - r.top)
        return True

    user32.EnumWindows(cb, 0)
    return out


def monitor_rect(hwnd):
    h = user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(h, ctypes.byref(mi)):
        r = mi.rcMonitor
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--size", default="1390,1240")
    ap.add_argument("--wait", type=float, default=6.0)
    a = ap.parse_args()

    before = set(windows())
    args = f'--new-window --no-first-run --no-default-browser-check "{a.url}"'
    rc = shell32.ShellExecuteW(None, "open", CHROME, args, None, SW_SHOWMINNOACTIVE)
    if rc <= 32:
        print(f"启动失败 rc={rc}")
        return 1
    print("已启动（最小化、未激活），等窗口…")

    new_hwnd = None
    for _ in range(int(a.wait * 4)):
        time.sleep(0.5)
        for hwnd, (title, x, y, w, h) in windows().items():
            if hwnd in before or w < 300 or h < 300:
                continue
            new_hwnd = hwnd
            break
        if new_hwnd:
            break
    if new_hwnd is None:
        print("没等到新窗口")
        return 1

    mon = monitor_rect(new_hwnd) or (-1440, -653, 1440, 2560)
    sw, sh = (int(v) for v in a.size.split(","))
    # 只在它自己那块屏里挪，绝不往主屏放
    x = mon[0] + 20
    y = mon[1] + 40
    sw = min(sw, mon[2] - 40)
    sh = min(sh, mon[3] - 80)
    user32.ShowWindow(new_hwnd, SW_RESTORE)
    time.sleep(0.2)
    user32.SetWindowPos(new_hwnd, 0, x, y, sw, sh, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    time.sleep(0.4)
    print("窗口:", windows().get(new_hwnd))
    print("目标:", (x, y, sw, sh), "monitor:", mon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
