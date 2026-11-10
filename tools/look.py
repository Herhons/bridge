"""截一个窗口（或一块区域）存到 evidence/。只读，不点击、不置前、不动鼠标。

    python tools/look.py                      # 默认找标题含 ChatGPT 的窗口
    python tools/look.py --title "Google Chrome"
    python tools/look.py --region -1440,-653,1440,2560
    python tools/look.py --top 240            # 只截顶部若干像素，放大 2 倍另存
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import Screen  # noqa: E402

user32 = ctypes.windll.user32


def find_window(hint: str):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if hint.lower() not in buf.value.lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((r.left, r.top, r.right - r.left, r.bottom - r.top, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="ChatGPT")
    ap.add_argument("--region")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--name", default="look")
    a = ap.parse_args()

    if a.region:
        region = tuple(int(v) for v in a.region.split(","))
        tag = a.name
    else:
        win = find_window(a.title)
        if win is None:
            print(f"没有标题含「{a.title}」的窗口")
            return 1
        region = win[:4]
        print(f"窗口: {win[4]}  rect={region}")
        tag = a.name or "look"

    s = Screen(verbose=False)
    img = s.grab(region)
    if img.size == 0:
        print("截不到")
        return 1
    ev = ROOT / "evidence"
    ev.mkdir(exist_ok=True)
    out = ev / f"{tag}.png"
    cv2.imwrite(str(out), img)
    print(f"{img.shape[1]}x{img.shape[0]} -> {out}")

    if a.top > 0:
        top = img[0:a.top, :]
        out2 = ev / f"{tag}_top_x2.png"
        cv2.imwrite(str(out2), cv2.resize(top, None, fx=2, fy=2,
                                          interpolation=cv2.INTER_NEAREST))
        print(f"顶部 {a.top}px 放大 2 倍 -> {out2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
