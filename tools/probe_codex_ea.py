"""量 codex 弹层：档位字 / 模型入口行 / 滑杆 / 模型列表。

只打开看和截图，**不选任何模型项**，最后 Esc。
"""

import sys
import time

import cv2
import numpy as np

sys.path.insert(0, "src")
from qi import desk, desk_flow, Screen, uilock  # noqa: E402
from qi.desk_sites import SITES  # noqa: E402


def blocks(img, x0, x1, y0, y1, thr=200, minarea=15):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sub = (g[y0:y1, x0:x1] < thr).astype(np.uint8)
    n, _lb, st, _c = cv2.connectedComponentsWithStats(sub, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < minarea:
            continue
        out.append((y0 + y, x0 + x, w, h, a))
    out.sort()
    return out


def main():
    site = SITES["codex"]
    s = desk_flow.open_bind("codex")
    if s is None:
        print("没窗口")
        return 1
    win = s.window_rect()
    pos = desk_flow.locate(s, site)
    print("控件:", {k: v for k, v in pos.items() if k != "win"})
    sx, sy = pos["send"]
    print(f"发送键 屏幕({sx},{sy}) = 窗口内({sx - win[0]},{sy - win[1]})")

    # 点芯片开弹层
    s.click_xy(*pos["chip"])
    time.sleep(1.6)
    img = s.grab(win)
    cv2.imwrite("evidence/codex_ea.png", img)

    # 弹层应该在芯片正上方 200~350px，横向覆盖芯片左侧
    cx, cy = pos["chip"][0] - win[0], pos["chip"][1] - win[1]
    # 弹层位置随页面变：新聊天页贴在芯片上方 134px，会话页在 320px。
    # 所以范围放宽到「芯片上方 40~320」，靠滑杆的彩色特征定位。
    y0, y1 = cy - 320, cy - 40
    x0, x1 = cx - 320, cx + 220
    print(f"\n扫描区 窗口内 x={x0}..{x1} y={y0}..{y1}")
    rows = blocks(img, x0, x1, y0, y1, thr=215, minarea=20)
    print("非背景块（y,x,w,h,area）:")
    for y, x, w, h, a in rows[:25]:
        print(f"  y={y:4d} x={x:4d} w={w:3d} h={h:2d} a={a:5d}")

    # 橙色/紫色滑杆：找彩色横条
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    sat = np.maximum(np.maximum(b, g), r) - np.minimum(np.minimum(b, g), r)
    cm = ((sat > 45) & (np.maximum(np.maximum(b, g), r) > 80)).astype(np.uint8)
    cm[:y0, :] = 0
    cm[y1:, :] = 0
    cm[:, :x0] = 0
    cm[:, x1:] = 0
    n, _lb, st, _c = cv2.connectedComponentsWithStats(cm, 8)
    print("\n弹层里的彩色块（滑杆填充）:")
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a < 200:
            continue
        px = img[y + h // 2, x + w // 2]
        print(f"  y={y} x={x} {w}x{h} a={a} BGR={px.tolist()} 中心=({x + w // 2},{y + h // 2})")

    # 放大弹层区域
    sub = img[max(0, y0 - 20):min(img.shape[0], y1 + 20), max(0, x0 - 20):min(img.shape[1], x1 + 20)]
    cv2.imwrite("evidence/codex_ea_zoom.png",
                cv2.resize(sub, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
    print(f"\n放大图 evidence/codex_ea_zoom.png 原图左上 ({max(0, x0 - 20)},{max(0, y0 - 20)})")

    s.press("esc")
    time.sleep(0.6)
    print("已 Esc")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    sys.exit(uilock.run("probe_codex_ea", main))
