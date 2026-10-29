"""只量 effort 层（强度层）：打开后就停，把结构打出来。

不做任何点击以外的操作，不切模型，不改强度。
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


def rows_of(img, x0, x1, y0, y1):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (g < 215).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < 15:
            continue
        if not (x0 <= x and x + w <= x1 and y0 <= y and y + h <= y1):
            continue
        out.append((y, x, w, h, a))
    out.sort()
    return out


def main():
    s = Screen(verbose=False)
    h = current()
    s.bind(h)
    s.focus()
    win = s.window_rect()
    v = C._voice_center(s)
    if v is None:
        print("找不到橙钮")
        return 1
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    print(f"窗口 {win}")
    print(f"橙钮 窗口内 ({vx},{vy})")

    # 先清场，再开
    s.press("esc")
    time.sleep(0.8)
    print("focus:", C.focus_composer(s))
    time.sleep(0.7)
    s.hotkey(*C.CHIP_HOTKEY)
    time.sleep(1.5)

    img = s.grab(win)
    cv2.imwrite("evidence/EA.png", img)
    print(f"\n弹层区域 窗口内 x={vx-560}..{vx+20} y={vy-20}..{vy+320}")
    for y, x, w, h_, a in rows_of(img, vx - 560, vx + 20, vy - 20, vy + 320):
        kind = "  <== 长横条" if (h_ <= 24 and w >= 110) else ""
        print(f"  y={y:4d} x={x:4d} w={w:3d} h={h_:2d} a={a:5d}{kind}")

    print("\n_menu_entry_pos =", C._menu_entry_pos(s))
    print("items(work) =", C._items_visible(s, "work"))

    # 橙色块单独列一遍
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    om = ((r - b > 40) & (r > 140)).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(om, 8)
    print("\n橙色块:")
    for i in range(1, n):
        x, y, w, h_, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                          int(st[i][3]), int(st[i][4]))
        if a < 20:
            continue
        if not (vy - 20 <= y <= vy + 320):
            continue
        print(f"  y={y:4d} x={x:4d} w={w:3d} h={h_:2d} a={a:5d}"
              f" 中心=({x + w // 2},{y + h_ // 2})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
