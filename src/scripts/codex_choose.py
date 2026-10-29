"""codex 桌面端：选模型 / 读当前模型。

## 界面结构（2026-10-29 实测，窗口 1380x1340）

和网页版 ChatGPT 是**同一套两层结构**：

    点芯片「GPT-6 Astra Ultra ∨」
       └─> 强度层：`Ultra`（档位字）
                   `GPT-6 Astra ›`（点它进模型列表）
                   ━━━━━━━ 紫色滑杆
            点模型行
               └─> 模型列表：`Select model`
                            Default / Recommended set of models
                            GPT-6.1 Sol
                            GPT-6 Astra  ✓
                            GPT-6.1 Terra
                            GPT-6 Sol
                            GPT-6 Luna
                            GPT-5.6 Sol
                            （GPT-5.6 Terra / GPT-5.6 Luna 看不到，见下）

## 关键：弹层位置随页面变，不能用固定偏移

新聊天页弹层贴在芯片上方 134px，会话页在 320px。所以**用滑杆当锚点**：
滑杆是弹层里唯一有颜色的东西（紫色 BGR≈[255,122,185]），找到它以后
模型行 = 滑杆上边 - 45px。

## 已知限制

模型列表有**固定最大高度**，一屏最多 7 项 + 第 8 项半截。
最后两项（GPT-5.6 Terra / GPT-5.6 Luna）露不出来，而且**滚轮对它无效**
（实测滚 9 格纹丝不动，把窗口从 1340 拉到 1900 也一样）。
所以这两项在 codex 上选不到，会明确报 item_not_visible。
"""

from __future__ import annotations

import json
import os
import sys
import time

import cv2
import numpy as np

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import ForegroundLost, Library, Screen  # noqa: E402
from qi import desk_flow, guard  # noqa: E402
from qi.desk_sites import SITES  # noqa: E402

TITLE = "ChatGPT"

# 列表项：key -> 模板名。**只看得到 7 项**，后两项留空并给出明确报错。
MODELS = {
    "default":     "codex_m_default",
    "gpt61_sol":   "codex_m_gpt61_sol",
    "gpt6_astra":  "codex_m_gpt6_astra",
    "gpt61_terra": "codex_m_gpt61_terra",
    "gpt6_sol":    "codex_m_gpt6_sol",
    "gpt6_luna":   "codex_m_gpt6_luna",
}
UNREACHABLE = {
    "gpt56_sol": "列表第 8 项，只能露出半截，不可靠",
    "gpt56_terra": "列表第 9 项，被固定高度截断，露不出来",
    "gpt56_luna": "列表第 10/9 项，被固定高度截断，露不出来",
}

CHECK = "codex_check"
SLIDER_ABOVE = 45          # 模型行在滑杆上边上方 45px（实测）
ITEM_GAP = 31              # 列表项行距（实测 1053->1084->1115）
CLICK_BUDGET = 10
_click = {"n": 0, "last": 0.0}


def _guard_click(s: Screen, x: int, y: int) -> None:
    if _click["n"] >= CLICK_BUDGET:
        raise RuntimeError(f"本次运行已点 {_click['n']} 下，达到上限")
    gap = 0.4 - (time.time() - _click["last"])
    if gap > 0:
        time.sleep(gap)
    s.click_xy(int(x), int(y))
    _click["n"] += 1
    _click["last"] = time.time()


def _find_slider(s: Screen, win, chip):
    """找强度层里的紫色滑杆。返回窗口内 (x, y, w, h) 或 None。"""
    img = s.grab(win)
    if img.size == 0:
        return None
    b = img[:, :, 0].astype(int)
    g = img[:, :, 1].astype(int)
    r = img[:, :, 2].astype(int)
    # 紫色：B 很高、R 中高、G 明显低
    m = ((b > 200) & (r > 140) & (r < 225) & (g < 165)).astype(np.uint8)
    cy = chip[1]
    cx = chip[0]
    m[: max(0, cy - 340), :] = 0
    m[max(0, cy - 25):, :] = 0
    m[:, : max(0, cx - 330)] = 0
    m[:, cx + 240:] = 0
    n, _lb, st, _c = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = (int(st[i][0]), int(st[i][1]), int(st[i][2]),
                         int(st[i][3]), int(st[i][4]))
        if a > 700 and w > 110:
            if best is None or a > best[4]:
                best = (x, y, w, h, a)
    return best[:4] if best else None


def _items_visible(s: Screen, win) -> bool:
    """模型列表开着吗：命中 ≥2 个模型项就算开着。"""
    hit = 0
    for tpl in MODELS.values():
        if s.find(tpl, threshold=0.86, region=win) is not None:
            hit += 1
            if hit >= 2:
                return True
    return False


def _list_band(s: Screen, win):
    """列表的纵向范围。用能匹配到的项聚成一组，排除页面别处的同名文字。

    芯片上就写着「GPT-6 Astra」——选中它时芯片上也是这个词，
    所以整窗搜索一定会在芯片上先命中，必须把范围收进列表。
    """
    ys = []
    for tpl in MODELS.values():
        m = s.find(tpl, threshold=0.86, region=win)
        if m is not None:
            ys.append(int(m.center[1]))
    if len(ys) < 2:
        return None
    ys.sort()
    groups = [[ys[0]]]
    for y in ys[1:]:
        if y - groups[-1][-1] <= 60:
            groups[-1].append(y)
        else:
            groups.append([y])
    gp = max(groups, key=len)
    if len(gp) < 2:
        return None
    return (win[0], gp[0] - 28, win[2], gp[-1] - gp[0] + 56)


def enter_list(s: Screen, win, chip) -> bool:
    """点芯片 → 找滑杆 → 点模型行 → 确认列表开着。"""
    if _items_visible(s, win):
        return True
    # chip 是**窗口内**坐标（_find_slider 要拿它当参考点），点击要换成屏幕坐标
    _guard_click(s, win[0] + chip[0], win[1] + chip[1])
    time.sleep(1.4)
    sl = _find_slider(s, win, chip)
    if sl is None:
        return False
    x, y, w, _h = sl
    row_y = win[1] + y - SLIDER_ABOVE
    row_x = win[0] + x + w // 2
    _guard_click(s, row_x, row_y)
    time.sleep(1.3)
    return _items_visible(s, win)


def read_model(s: Screen, win, *, verbose: bool = False) -> dict:
    """读当前模型：找勾 + 各项，取离勾最近的那一项。"""
    band = _list_band(s, win)
    if band is None:
        return {"model": "unknown", "hint": "列表里一项都没匹配到"}
    chk = s.find(CHECK, threshold=0.80, region=band)
    dists = {}
    for key, tpl in MODELS.items():
        m = s.find(tpl, threshold=0.86, region=band)
        if m is None:
            continue
        dists[key] = None if chk is None else abs(int(m.center[1] - chk.center[1]))
    if chk is None:
        return {"model": "unknown", "hint": "找不到勾", "seen": list(dists)}
    near = {k: d for k, d in dists.items() if d is not None and d <= 16}
    if not near:
        return {"model": "unknown", "hint": "勾不在任何一行的位置上", "dist": dists}
    best = min(near, key=lambda k: near[k])
    return {"model": best, "dist": near[best], "seen": list(dists)}


def set_model(s: Screen, win, chip, key: str) -> dict:
    """切模型。"""
    if key in UNREACHABLE:
        return {"ok": False, "stage": "unreachable",
                "hint": UNREACHABLE[key] + "；codex 列表是固定高度、且不响应滚轮"}
    if key not in MODELS:
        return {"ok": False, "stage": "bad_arg", "want": key,
                "options": list(MODELS) + list(UNREACHABLE)}
    if not enter_list(s, win, chip):
        return {"ok": False, "stage": "list_not_open"}

    before = read_model(s, win)
    if before.get("model") == key:
        return {"ok": True, "stage": "already", "before": key, "after": key}

    band = _list_band(s, win)
    if band is None:
        return {"ok": False, "stage": "list_band_none"}
    m = s.find(MODELS[key], threshold=0.86, region=band)
    if m is None:
        return {"ok": False, "stage": "item_not_visible", "want": key,
                "before": before.get("model")}
    _guard_click(s, *m.center)
    # 等久一点：点完模型项后弹层要收起来，这时候立刻点芯片会被"关闭动画"吃掉，
    # 表现是复核阶段打不开列表（实测 reverify_failed）
    time.sleep(2.4)

    # 复核：再点开一次看勾
    if not enter_list(s, win, chip):
        return {"ok": False, "stage": "reverify_failed", "before": before.get("model")}
    after = read_model(s, win)
    return {"ok": after.get("model") == key, "before": before.get("model"),
            "after": after.get("model"), "want": key,
            "stage": "done" if after.get("model") == key else "unverified"}


def main() -> int:
    name = os.path.basename(__file__)
    idle_need, idle_wait, force, args = guard.parse_guard_args(list(sys.argv[1:]))
    do_list = "--list" in args or "-l" in args
    want = None
    if "--model" in args:
        i = args.index("--model")
        if i + 1 >= len(args):
            print("--model 缺值")
            return 2
        want = args[i + 1].strip().lower()

    if not do_list and want is None:
        print("用法：")
        print(f"  python {name} --list")
        print(f"  python {name} --model <名字>")
        print("  可选：" + ", ".join(MODELS))
        print("  看不到（列表固定高度截断）：" + ", ".join(UNREACHABLE))
        return 2
    if want is not None and want not in MODELS and want not in UNREACHABLE:
        print(f"没有这个模型：{want}")
        print("  可选：" + ", ".join(MODELS))
        return 2

    # 只查模板文件在不在。不要用 lib.report —— 它会在**当前屏幕**上试匹配，
    # 弹层没开时必然一堆"没找到"，看着像失败其实只是没开列表。
    lib = Library()
    need = [CHECK] + list(MODELS.values())
    missing = lib.missing(need)
    if missing:
        print(f"缺模板：{missing}")
        return 3

    if not guard.guard(idle_need, idle_wait, force):
        return 4

    s = desk_flow.open_bind("codex")
    if s is None:
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1
    win = s.window_rect()
    pos = desk_flow.locate(s, SITES["codex"])
    if pos is None:
        print(json.dumps({"ok": False, "stage": "no_send_button"}, ensure_ascii=False))
        return 1
    chip = (pos["chip"][0] - win[0], pos["chip"][1] - win[1])

    try:
        if do_list:
            if not enter_list(s, win, chip):
                print(json.dumps({"model": "unknown", "hint": "打不开模型列表"},
                                 ensure_ascii=False))
                return 0
            out = read_model(s, win)
            s.press("esc")
            print(json.dumps(out, ensure_ascii=False))
            return 0
        r = set_model(s, win, chip, want)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    except ForegroundLost as e:
        print(json.dumps({"ok": False, "stage": "user_took_over", "hint": str(e)},
                         ensure_ascii=False))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
