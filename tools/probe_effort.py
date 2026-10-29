"""量 ChatGPT 那个弹层的真实结构：强度层 和 模型列表 是两层。

用户指出的流程（2026-10-29）：
  点芯片 → 出「滑杆 + 当前模型名」这一层（叫它 effort 层）
  在这一层里再点模型名那一行 → 才进模型列表（models 层）
  选完模型 → 自动退回 effort 层，这时可以调强度
  调完点外面 → 整个收起

本脚本只做只读测量：打开弹层、截图、把连通块坐标打印出来。
不改任何设置，不发消息。
"""

import sys
import time

import cv2
import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "src/scripts")

from qi import Library, Screen  # noqa: E402
from qi.browser import current  # noqa: E402

import chatgpt_choose as C  # noqa: E402


def dump_blocks(img, tag, vx, vy):
    """打印窗口内所有非背景连通块，按 y 排序。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (g < 210).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(mask, 8)
    rows = []
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < 20:
            continue
        # 只关心橙钮附近这一片
        if not (vy - 180 <= y <= vy + 300):
            continue
        if not (vx - 560 <= x <= vx + 20):
            continue
        rows.append((y, x, w, h, a))
    rows.sort()
    print(f"--- {tag}  (橙钮窗口内 {vx},{vy}) 共 {len(rows)} 块 ---")
    for y, x, w, h, a in rows:
        kind = ""
        if h <= 22 and w >= 120:
            kind = "  <== 长横条(疑似滑杆)"
        print(f"  y={y:4d} x={x:4d} w={w:3d} h={h:2d} a={a:5d}{kind}")
    return rows


def main():
    lib = Library()
    miss = [n for n in (C.COMPOSER, C.CHECK) if not lib.has(n)]
    if miss:
        print("缺模板:", miss)

    s = Screen(verbose=False)
    h = current()
    if not h:
        print("没有记录的 Chrome 窗口，先 ensure_site")
        return 1
    s.bind(h)
    win = s.window_rect()
    print("窗口", win, "hwnd", h)

    # ---- 0. 先确认当前是干净的 ----
    print("page_kind =", C.page_kind(s))
    print("items(work) =", C._items_visible(s, "work"))

    # ---- 1. 打开 effort 层：聚焦输入框 + 快捷键 ----
    ok = C.focus_composer(s)
    print("focus_composer =", ok)
    time.sleep(0.6)
    s.hotkey(*C.CHIP_HOTKEY)
    time.sleep(1.4)

    img = s.grab(win)
    cv2.imwrite("evidence/effort_layer.png", img)
    v = C._voice_center(s)
    vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
    dump_blocks(img, "effort 层", vx, vy)

    print("_menu_entry_pos =", C._menu_entry_pos(s))
    print("items(work) =", C._items_visible(s, "work"))

    # ---- 2. 在 effort 层里点入口行，进 models 层 ----
    pos = C._menu_entry_pos(s)
    if pos:
        r = C.safe_click(s, pos)
        print("点入口行 ->", r)
        time.sleep(1.4)
    img2 = s.grab(win)
    cv2.imwrite("evidence/models_layer.png", img2)
    v = C._voice_center(s)
    if v:
        vx, vy = int(v[0]) - win[0], int(v[1]) - win[1]
        dump_blocks(img2, "models 层", vx, vy)
    print("items(work) =", C._items_visible(s, "work"))
    chk = s.find(C.CHECK, threshold=0.85, region=win)
    print("check =", None if chk is None else (round(chk.score, 3), chk.center))

    print("EA.png=effort层  MA.png=models层")
    return 0


if __name__ == "__main__":
    sys.exit(main())
