"""量 effort 层：轨道、橙色填充、滑块、刻度点。只读，不改设置。

用法：python tools/probe_slider2.py
会自己开弹层（Esc 清场 -> 点输入框 -> Ctrl+Shift+M），量完按 Esc 关掉。
"""

import sys
import time

import cv2
import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "src/scripts")

from qi import Screen  # noqa: E402
from qi.browser import current  # noqa: E402

import chatgpt_choose as C  # noqa: E402


def main():
    s = Screen(verbose=False)
    s.bind(current())
    s.focus()
    win = s.window_rect()
    v = C._voice_center(s)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    print(f"窗口 {win}")
    print(f"橙钮 窗口内 ({vx},{vy})")

    # 清场
    s.press("esc")
    time.sleep(0.8)
    print("focus_composer:", C.focus_composer(s))
    time.sleep(0.6)
    s.hotkey(*C.CHIP_HOTKEY)
    time.sleep(1.5)

    img = s.grab(win)
    cv2.imwrite("evidence/S0_full.png", img)

    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    orange = ((r - b > 40) & (r > 140)).astype(np.uint8)

    # 只在橙钮左下方找
    y0, y1 = vy + 40, vy + 160
    x0, x1 = vx - 500, vx - 30
    print(f"\n搜索区 窗口内 x={x0}..{x1} y={y0}..{y1}")

    n, _lb, st, _c = cv2.connectedComponentsWithStats(orange, 8)
    print("橙色块:")
    found = []
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < 40:
            continue
        if not (x0 <= x and x + w <= x1 and y0 <= y and y + h <= y1):
            continue
        print(f"  x={x:4d} y={y:4d} w={w:3d} h={h:2d} a={a:5d}")
        found.append((x, y, w, h, a))

    if not found:
        print("没有橙色块，弹层没开")
        cv2.imwrite("evidence/S0_miss.png", img[y0 - 60:y1 + 40, x0:x1])
        return 1

    # 最大那块 = 橙色填充 + 滑块
    big = max(found, key=lambda t: t[2] * t[3])
    ox0, oy0, ow, oh, _ = big
    print(f"\n填充/滑块块: x={ox0}..{ox0 + ow} y={oy0}..{oy0 + oh}")

    # 滑块圆心：取该块顶部几行的 x 中心
    top_rows = orange[oy0:oy0 + 3, ox0:ox0 + ow]
    idx = np.where(top_rows.any(axis=0))[0]
    knob_x = ox0 + (int(idx.min()) + int(idx.max())) // 2
    knob_y = oy0 + oh // 2
    print(f"滑块圆心 ≈ ({knob_x},{knob_y})  半径≈{oh // 2}")

    # 轨道整行：在滑块圆心那一行找 <250 的连续段
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    row = g[knob_y, x0:x1]
    idx = np.where(row < 250)[0]
    tx0, tx1 = x0 + int(idx.min()), x0 + int(idx.max())
    print(f"轨道 x={tx0}..{tx1} 长={tx1 - tx0 + 1}")

    # 刻度点：轨道上比轨道底色深的孤立小块（灰度差 3~40）
    print("\n轨道行灰度分布（相对 x 的突变）:")
    prof = row[idx.min():idx.max() + 1].astype(int)
    base = int(np.median(prof))
    print(f"  轨道底色(中位) = {base}")
    dark = prof < base - 4
    segs = []
    stt = None
    for i, d in enumerate(dark):
        if d and stt is None:
            stt = i
        elif not d and stt is not None:
            segs.append((tx0 + stt, tx0 + i - 1))
            stt = None
    if stt is not None:
        segs.append((tx0 + stt, tx1))
    for a_, b_ in segs:
        print(f"  深色段 x={a_}..{b_} 宽={b_ - a_ + 1} 中心={(a_ + b_) // 2}")

    # 保存放大图（只截滑杆这一带）
    pad = 10
    sub = img[oy0 - 40:oy0 + oh + pad, tx0 - 10:tx1 + 10]
    cv2.imwrite("evidence/S0_slider.png",
                cv2.resize(sub, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    print(f"\n放大图 evidence/S0_slider.png 原图左上 ({tx0 - 10},{oy0 - 40})")

    # 关掉
    s.press("esc")
    time.sleep(0.8)
    print("已按 Esc 关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
