"""把「聊天/工作」那一条放大存图，并逐列打印墨色分布。只读。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import Screen, uilock  # noqa: E402


def main() -> int:
    s = Screen(verbose=False)
    win = s.window_rect("Google Chrome", on_secondary=True)
    img = s.grab(win)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    y0, y1 = 105, 165
    x0, x1 = 740, 1010
    crop = img[y0:y1, x0:x1]
    out = ROOT / "evidence" / "tabband_x4.png"
    cv2.imwrite(str(out), cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    print("存图:", out, crop.shape)

    sub = gray[y0:y1, x0:x1]
    dark = (sub < 90).astype(np.uint8)
    cols = dark.sum(axis=0)
    runs, start = [], None
    for i, v in enumerate(cols):
        if v >= 1 and start is None:
            start = i
        elif v < 1 and start is not None:
            runs.append((x0 + start, x0 + i))
            start = None
    if start is not None:
        runs.append((x0 + start, x0 + len(cols)))
    print("墨色(<90)列段:", runs)
    print("各段中心 x:", [(a + b) // 2 for a, b in runs])

    # 每段的 bbox 与原图最暗值
    for a, b in runs:
        if b - a < 6:
            continue
        seg = sub[:, a - x0:b - x0]
        ys = np.where((seg < 90).any(axis=1))[0]
        print(f"  段 x={a}..{b} 宽 {b-a}  y={y0+ys.min()}..{y0+ys.max()}  最暗 {int(seg.min())}")
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    raise SystemExit(uilock.run("probe_band", main))
