"""启动一个桌面程序，把它的新窗口挪到副屏，截图，然后关掉。

只用于观察界面形态（写脚本前先看清楚有什么控件），不发任何消息。
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

import cv2

sys.path.insert(0, "src")
from qi import Screen, uilock  # noqa: E402

u = ctypes.windll.user32
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_CLOSE = 0x0010


def wins():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                r = wintypes.RECT()
                u.GetWindowRect(h, ctypes.byref(r))
                out.append((int(h), b.value, r.left, r.top,
                            r.right - r.left, r.bottom - r.top))
        return True

    u.EnumWindows(cb, 0)
    return out


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else r"F:\dsh-latest-windows-x64.exe"
    tag = sys.argv[2] if len(sys.argv) > 2 else "dsh"
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

    before = {w[0] for w in wins()}
    print("启动:", exe)
    p = subprocess.Popen([exe])
    time.sleep(wait)

    new = [w for w in wins() if w[0] not in before and w[4] > 300 and w[5] > 200]
    print(f"新增窗口 {len(new)} 个:")
    for w in new:
        print("  ", w)

    if not new:
        print("没抓到新窗口（可能启动慢或被 UWP 包装）")
        return 1

    # 取面积最大的
    h, title, x, y, w_, h_ = max(new, key=lambda t: t[4] * t[5])
    print(f"目标: hwnd={h} {title!r} {w_}x{h_}")
    # 挪到副屏（不激活）
    u.SetWindowPos(h, 0, -1440, -560, 1380, 1340,
                   SWP_NOACTIVATE | SWP_SHOWWINDOW)
    time.sleep(1.5)

    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
    print("挪后:", rect)

    s = Screen(verbose=False)
    img = s.grab(rect)
    out = f"evidence/desk_{tag}.png"
    cv2.imwrite(out, img)
    print("截图:", out, img.shape)

    # 关掉
    u.PostMessageW(h, WM_CLOSE, 0, 0)
    time.sleep(1.5)
    if p.poll() is None:
        p.terminate()
    print("已关闭")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    sys.exit(uilock.run("probe_desktop", main))
