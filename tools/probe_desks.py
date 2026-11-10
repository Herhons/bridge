"""只读探测：枚举 dsh / opencode / workbuddy 的顶层窗口，各截一张图。

不改窗口、不点击、不抢前台。只是把画面存到 evidence/ 里看。
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import codex_use as C  # noqa: E402  # 复用 DPI 声明 / PrintWindow 截图 / 进程名
from qi import uilock  # noqa: E402

import cv2  # noqa: E402

u = C.user32

WANT = {
    "DeepSeek Harness.exe": "dsh",
    "opencode-cli.exe": "opencode",
    "OpenCode.exe": "opencode",
    "WorkBuddy.exe": "workbuddy",
}


def enum():
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = C._pid_of(hwnd)
        proc = C._proc_name(pid)
        if proc in WANT:
            r = C.rect_of(hwnd)
            out.append({"hwnd": int(hwnd), "proc": proc, "title": buf.value,
                        "rect": list(r) if r else None,
                        "minimized": bool(u.IsIconic(hwnd))})
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def main():
    wins = enum()
    print(json.dumps(wins, ensure_ascii=False, indent=1))
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")
    for w in wins:
        tag = WANT[w["proc"]]
        r = w["rect"]
        if not r or r[2] < 200:
            continue
        desk = C.Desk(w["hwnd"])
        img = desk.grab(r, fresh=True)
        if img.size == 0:
            print("grab failed", tag)
            continue
        p = os.path.abspath(os.path.join(d, f"probe_{tag}.png"))
        cv2.imwrite(p, img)
        print("saved", p, img.shape)


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    uilock.run("probe_desks", main)
