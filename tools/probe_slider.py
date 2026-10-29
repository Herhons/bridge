"""精确量滑杆：轨道端点、4 个刻度点、滑块中心。

用法：先让 effort 层开着（Ctrl+Shift+M），再跑本脚本。
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
    img = s.grab(win)

    # 滑杆在橙钮下方 70~120px 之间，横跨橙钮左侧一大段
    y0, y1 = vy + 60, vy + 130
    x0, x1 = vx - 300, vx + 10
    sub = img[y0:y1, x0:x1]
    b = sub[:, :, 0].astype(int)
    r = sub[:, :, 2].astype(int)
    orange = ((r - b > 40) & (r > 140)).astype(np.uint8)

    ys, xs = np.where(orange)
    if len(xs) == 0:
        print("这一带没有橙色，弹层可能没开")
        return 1
    print(f"橙色像素 x 范围 {x0 + xs.min()}..{x0 + xs.max()}  "
          f"y 范围 {y0 + ys.min()}..{y0 + ys.max()}")
    cy = y0 + int(np.median(ys))
    print("橙色带中位 y =", cy)

    # 逐行找最长连续橙色段，确定轨道纵向范围
    print("\n每行橙色像素数:")
    for yy in range(y0, y1):
        row = orange[yy - y0]
        n = int(row.sum())
        if n:
            idx = np.where(row)[0]
            print(f"  y={yy:4d} n={n:4d} x={x0 + idx.min()}..{x0 + idx.max()}")

    # 灰轨道：找那一行里颜色接近 #E5E5E5 的连续段
    print("\n轨道整行（灰度 < 250 的非背景）:")
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for yy in range(cy - 6, cy + 7):
        row = g[yy, x0:x1]
        idx = np.where(row < 248)[0]
        if len(idx) > 50:
            print(f"  y={yy:4d} 范围 {x0 + idx.min()}..{x0 + idx.max()} "
                  f"长 {idx.max() - idx.min() + 1}")

    # 刻度点：在轨道行里找比轨道略深的孤立小块
    print("\n滑块（橙色圆环）:")
    n, lb, st, c = cv2.connectedComponentsWithStats(orange, 8)
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < 30:
            continue
        print(f"  x={x0 + x:4d} y={y0 + y:4d} w={w:3d} h={h:2d} a={a:5d} "
              f"中心=({x0 + x + w // 2},{y0 + y + h // 2})")

    cv2.imwrite("evidence/slider_probe.png",
                cv2.resize(sub, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    print(f"\n放大图 evidence/slider_probe.png  (原图左上 {x0},{y0})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
