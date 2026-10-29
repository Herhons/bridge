"""标定强度滑杆：拖到 5 个等分位置，各截芯片看档位名。

滑块圆心 = 橙色块右端 - 16（实测统一公式）：
  · 低档位时橙色块就是滑块本身（32px）
  · 中档位时是「填充段 + 滑块」连成一块，右端仍是滑块右缘
  · 高档位时填满整条，右端仍是滑块右缘
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

KNOB_R = 16
KNOB_Y_DY = 94          # 滑块圆心 y = 橙钮 y + 94
BAND_DY = (60, 140)     # 滑杆纵向搜索带
TRACK_L, TRACK_R = 954, 1239   # 轨道两端（实测）


def orange_mask(img):
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    return ((r - b > 40) & (r > 140)).astype(np.uint8)


def knob_x(img, vy):
    """滑块圆心 x = 橙色块右端 - 16。找不到橙色返回 None。"""
    om = orange_mask(img)
    # x 必须限定在轨道附近：全宽扫会抓到窗口右边外面的橙色（实测返回 1363）
    band = om[vy + BAND_DY[0]:vy + BAND_DY[1], TRACK_L - 40:TRACK_R + 40]
    ys, xs = np.where(band)
    if len(xs) < 80:
        return None
    return TRACK_L - 40 + int(xs.max()) - KNOB_R


def main():
    s = Screen(verbose=False)
    s.bind(current())
    s.focus()
    win = s.window_rect()
    v = C._voice_center(s)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]

    s.press("esc")
    time.sleep(0.7)
    print("focus:", C.focus_composer(s))
    time.sleep(0.6)
    s.hotkey(*C.CHIP_HOTKEY)
    time.sleep(1.5)

    img = s.grab(win)
    k = knob_x(img, vy)
    print("开弹层后滑块 x =", k, " 圆心 y =", vy + KNOB_Y_DY)
    if k is None:
        print("弹层没开或找不到滑杆")
        return 1

    # 5 档等分：实测最左 981、中间 1103、最右 1223
    LO, HI = 981, 1223
    ky_screen = win[1] + vy + KNOB_Y_DY

    shots = []
    for i in range(5):
        tx = int(round(LO + (HI - LO) * i / 4))
        kx = knob_x(s.grab(win), vy)
        if kx is None:
            print(f"  [{i}] 找不到滑块")
            continue
        if abs(kx - tx) > 2:
            s.drag(win[0] + kx, ky_screen, win[0] + tx, ky_screen, steps=14)
            time.sleep(1.1)
        img2 = s.grab(win)
        real = knob_x(img2, vy)
        print(f"  [{i}] 目标 {tx} -> 实际 {real}")
        chip = img2[vy - 36:vy + 10, vx - 340:vx + 6]
        shots.append((i, tx, real, chip))

    if shots:
        wmax = max(sh.shape[1] for *_x, sh in shots)
        rows = []
        for i, tx, real, sh in shots:
            pad = np.full((sh.shape[0], wmax - sh.shape[1] + 120, 3), 255, np.uint8)
            row = np.hstack([sh, pad])
            cv2.putText(row, f"{i}: t={tx} r={real}", (wmax + 6, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            rows.append(row)
        out = cv2.resize(np.vstack(rows), None, fx=3, fy=3,
                         interpolation=cv2.INTER_NEAREST)
        cv2.imwrite("evidence/chip_5levels.png", out)
        print("\n拼图 evidence/chip_5levels.png")

    # 回到中间档再关，别把用户设置留在极端
    kx = knob_x(s.grab(win), vy)
    mid = int((LO + HI) / 2)
    if kx is not None and abs(kx - mid) > 2:
        s.drag(win[0] + kx, ky_screen, win[0] + mid, ky_screen, steps=12)
        time.sleep(0.8)
    s.press("esc")
    time.sleep(0.7)
    print("已按 Esc 关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
