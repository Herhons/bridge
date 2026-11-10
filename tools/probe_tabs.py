"""诊断「聊天 / 工作」按钮：模板命中情况 + 两半格的墨色/底色。只读，不点击。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import Library, Screen, uilock  # noqa: E402


def main() -> int:
    s = Screen(verbose=False)
    lib = Library()
    win = s.window_rect("Google Chrome", on_secondary=True)
    print("win:", win)
    if win is None:
        raise SystemExit("副屏没有 Chrome")

    img = s.grab(win)
    print("窗口图:", img.shape)

    tpl = cv2.imread(str(lib.path_of("gpt_tab_chat_word")))
    print("模板 gpt_tab_chat_word:", tpl.shape, "min", tpl.min(), "mean", round(float(tpl.mean()), 1))

    res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
    print("最高分:", round(float(res.max()), 4))
    loc = np.unravel_index(int(res.argmax()), res.shape)
    print("位置(窗口内):", loc[1], loc[0], "-> 屏幕", win[0] + loc[1], win[1] + loc[0])

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    best = (loc[1], loc[0], tpl.shape[1], tpl.shape[0])
    print("bbox内最暗:", int(gray[best[1]:best[1] + best[3], best[0]:best[0] + best[2]].min()))

    # 在整条顶部带里扫"深色连续段"，看看按钮组到底多大、在哪
    band_y0, band_y1 = 80, 160
    sub = gray[band_y0:band_y1, :]
    dark = (sub < 140).astype(np.uint8)
    cols = dark.sum(axis=0)
    runs, start = [], None
    for i, v in enumerate(cols):
        if v >= 2 and start is None:
            start = i
        elif v < 2 and start is not None:
            if i - start >= 8:
                runs.append((start, i, int(cols[start:i].max())))
            start = None
    if start is not None:
        runs.append((start, len(cols), int(cols[start:].max())))
    print(f"顶部 y={band_y0}~{band_y1} 的深色列段（x起,x止,列内最多深色像素数）:")
    for r in runs:
        print("   ", r)
    return 0


if __name__ == "__main__":
    # UI 前台锁（CARD-09f）：真机探针也要全程持有 runs\ui.lock
    raise SystemExit(uilock.run("probe_tabs", main))
