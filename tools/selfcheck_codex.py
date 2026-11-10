"""codex_use 自检：模板在多种窗口尺寸下的命中情况 + PrintWindow 抗遮挡。

只读（不点击），但会改窗口尺寸，跑完自动还原。
用法：python tools/selfcheck_codex.py
"""
import ctypes
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

sys.path.insert(0, "src")
sys.path.insert(0, "src/scripts")

import codex_use as C  # noqa: E402

u = ctypes.windll.user32

SIZES = [
    ("1380x1340", (1380, 1340)),      # 原始尺寸
    ("1150x1050", (1150, 1050)),      # 缩小
    ("1050x950", (1050, 950)),        # 接近 MIN_W 下界
]
POS = (-1440, -560)
NAMES = ("cdx_send", "cdx_arrow", "cdx_mic", "cdx_plus", "cdx_composer")


def run_probe(tag):
    """按 CLI 的 --probe 走一遍：不 activate、不点击。"""
    h = C.find_window()
    d = C.Desk(h)
    win = C.rect_of(h)
    t0 = time.time()
    pm = C.locate(d, win)
    cost = (time.time() - t0) * 1000
    rows = C.probe(d, win)
    return {
        "tag": tag,
        "win": win,
        "pw": d._pw_ok,
        "ms": round(cost),
        "missing": pm["missing"],
        "tpl": {n: (None if v is None else (round(v[0], 4), v[1])) for n, v in rows},
    }


def main():
    h = C.find_window()
    if h is None:
        print("找不到 codex 窗口")
        return 1
    orig = C.rect_of(h)
    print(f"原始窗口 {orig}\n")

    results = []
    try:
        for tag, size in SIZES:
            C.place(h, POS, size)
            time.sleep(2.0)
            r = run_probe(tag)
            results.append(r)
    finally:
        C.place(h, (orig[0], orig[1]), (orig[2], orig[3]))
        time.sleep(1.0)
        print(f"已还原窗口 {orig}\n")

    # 表格
    hdr = f"{'尺寸':<12}{'PrintWindow':<13}{'耗时':<8}"
    for n in NAMES:
        hdr += f"{n.replace('cdx_', ''):<11}"
    print(hdr)
    print("-" * len(hdr))
    bad = 0
    for r in results:
        line = f"{r['tag']:<12}{str(r['pw']):<13}{r['ms']:>4}ms  "
        for n in NAMES:
            v = r["tpl"].get(n)
            if v is None:
                line += f"{'MISS':<11}"
                bad += 1
            else:
                score = v[0]
                mark = "" if score >= C.TPL_THR else "!"
                line += f"{score:.3f}{mark:<6}"
                if score < C.TPL_THR:
                    bad += 1
        print(line)
    print()
    print(f"阈值 {C.TPL_THR}，低于阈值的项：{bad}")
    print("（! 表示低于阈值）")

    # 对比：同样三档，但用旧的屏幕抓取，看遮挡时会不会失真
    print("\n--- 遮挡测试：把 codex 沉到 z-order 底部 ---")
    h = C.find_window()
    d = C.Desk(h)
    win = C.rect_of(h)
    d.grab(win, fresh=True)
    scr_before = d._screen_grab(win)

    # HWND_BOTTOM=1，不动位置不动大小、不激活 —— 只是让它被别的窗口盖住
    u.SetWindowPos(h, 1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
    time.sleep(1.2)

    r = run_probe("被遮挡")
    scr_after = d._screen_grab(win)
    diff_scr = float(np.abs(scr_before.astype(int) - scr_after.astype(int)).mean())
    print(f"屏幕抓取：遮挡前后平均差 = {diff_scr:.2f}"
          f"（>0 说明屏幕那份已经变了 —— 截到的是压在它上面的窗口）")
    line = f"PrintWindow probe：{str(r['pw']):<6} 耗时 {r['ms']:>3}ms  "
    for n in NAMES:
        v = r["tpl"].get(n)
        line += f"{'MISS' if v is None else format(v[0], '.3f'):<11}"
    print(line)
    if r["pw"] and all((r["tpl"].get(n) or [0])[0] >= C.TPL_THR for n in NAMES):
        print("结论：窗口被完全遮挡时 PrintWindow 仍然全部命中 —— 抗遮挡成立")
    else:
        print("结论：遮挡下有一项没命中，PrintWindow 可能退回了屏幕抓取")
        bad += 1

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
