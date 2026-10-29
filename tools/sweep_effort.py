"""扫描强度档位：从最左开始，每次往右挪一格，Esc 关闭后截芯片。

目的：找出滑块位置和档位名的对应关系（吸附点在哪、几档）。
每轮：快捷键开弹层 -> 拖到目标 x -> Esc 关闭 -> 截芯片。
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
TRACK_L, TRACK_R = 914, 1279     # 橙色扫描窗口（比轨道宽一点）
LO, HI = 981, 1223               # 滑块圆心可移动范围（实测）


def knob(img, vy):
    b = img[:, :, 0].astype(int)
    r = img[:, :, 2].astype(int)
    om = ((r - b > 40) & (r > 140)).astype(np.uint8)
    band = om[vy + 60:vy + 140, TRACK_L:TRACK_R]
    ys, xs = np.where(band)
    if len(xs) < 80:
        return None
    return TRACK_L + int(xs.max()) - KNOB_R


def open_effort(s, vx, vy):
    """开弹层。返回是否成功（能找到滑块）。"""
    s.press("esc")
    time.sleep(0.7)
    if not C.focus_composer(s):
        return False
    time.sleep(0.5)
    s.hotkey(*C.CHIP_HOTKEY)
    time.sleep(1.4)
    img = s.grab(s.window_rect())
    return knob(img, vy) is not None


def main():
    s = Screen(verbose=False)
    s.bind(current())
    s.focus()
    win = s.window_rect()
    v = C._voice_center(s)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    ky = win[1] + vy + 94

    shots = []
    # 先按 5 个等分点扫（5 档假设），每点是 981 981+60.5*i
    targets = [LO + int(round((HI - LO) * i / 4)) for i in range(5)]
    print(f"扫描 {len(targets)} 个位置：{targets[0]}..{targets[-1]}")
    for tx in targets:
        # 探测脚本每轮都要点输入框聚焦，撞穿 safe_click 的 8 次预算，这里手工重置
        C._click["n"] = 0
        C._click["last"] = 0.0
        if not open_effort(s, vx, vy):
            print(f"  x={tx}: 弹层打不开，跳过")
            continue
        img = s.grab(win)
        k = knob(img, vy)
        if k is None:
            print(f"  x={tx}: 找不到滑块")
            continue
        if abs(k - tx) > 3:
            s.drag(win[0] + k, ky, win[0] + tx, ky, steps=14)
            time.sleep(1.0)
        # Esc 关闭，让设置生效
        s.press("esc")
        time.sleep(1.0)
        img2 = s.grab(win)
        chip = img2[vy - 34:vy + 8, vx - 300:vx + 4]
        shots.append((tx, chip))
        print(f"  x={tx}: 拖后={knob(s.grab(win), vy)}（已 Esc）")

    if not shots:
        print("没有数据")
        return 1

    wmax = max(sh.shape[1] for _t, sh in shots)
    rows = []
    for tx, sh in shots:
        pad = np.full((sh.shape[0], wmax - sh.shape[1] + 90, 3), 255, np.uint8)
        row = np.hstack([sh, pad])
        cv2.putText(row, f"x={tx}", (wmax + 6, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1)
        rows.append(row)
    out = cv2.resize(np.vstack(rows), None, fx=2.2, fy=2.2,
                     interpolation=cv2.INTER_NEAREST)
    cv2.imwrite("evidence/effort_sweep.png", out)
    print(f"\n拼图 evidence/effort_sweep.png  共 {len(shots)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
