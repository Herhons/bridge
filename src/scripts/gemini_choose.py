"""gemini 网页版：只做选择，不发提问。

用法：
    python gemini_choose.py --list
    python gemini_choose.py --model flash_lite|flash|pro|argon

实测菜单（深色主题）：
    3.5 Flash-Lite / 极速回答      ← 默认
    3.8 Flash      / 全方位帮助
    3.1 Pro        / 高级推理
    4 Argon        / 复杂任务
    思考等级 ›     （子菜单，本脚本没做）

芯片是右对齐的，宽度随模型名变；所以开菜单靠"点当前芯片"，不按固定偏移点。
"""

from __future__ import annotations

import json
import os
import sys
import time

import cv2

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library  # noqa: E402
from qi.choose import Chooser, cli, open_menu  # noqa: E402

CFG = Chooser(
    name="gemini",
    anchor="gem_composer",
    open_offset=(0, 0),
    prefix="gem_",
    options={
        "flash_lite": "3.5 Flash-Lite（极速回答，默认）",
        "flash": "3.8 Flash（全方位帮助）",
        "pro": "3.1 Pro（高级推理）",
        "argon": "4 Argon（复杂任务）",
    },
    has_chip=("flash_lite", "flash", "pro", "argon"),
    writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),
    writers={"dialog": 0, "task": 1},
    open_by_chip=True,
)

THINK = {"low": ("低 快速高效", "gem_tk_low"),
         "mid": ("中 深度适中", "gem_tk_mid"),
         "high": ("高 超级详尽", "gem_tk_high")}
THINK_ENTRY = "gem_tk_entry"


def _open_think(s) -> bool:
    """打开模型菜单 → 点「思考等级」→ 子菜单。"""
    if not any(s.find(t, threshold=0.85) for _d, t in THINK.values()):
        if s.find(THINK_ENTRY, threshold=0.85) is None:
            if not open_menu(s, CFG):
                return False
            time.sleep(0.6)
        e = s.find(THINK_ENTRY, threshold=0.85)
        if e is None:
            return False
        s.click(e)
        time.sleep(0.9)
    return any(s.find(t, threshold=0.85) for _d, t in THINK.values())


def read_think(s) -> dict:
    """读思考等级：子菜单里哪一行左边有勾。勾区墨量实测 34 / 0 / 0。"""
    if not _open_think(s):
        return {"think": "unknown", "hint": "打不开思考等级子菜单"}
    ink = {}
    for k, (_d, tpl) in THINK.items():
        m = s.find(tpl, threshold=0.85)
        if m is not None:
            band = s.grab((m.x, m.y, 40, m.h))
            g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            ink[k] = int((g > 140).sum())
    s.press("esc"); time.sleep(0.3)
    s.press("esc"); time.sleep(0.3)
    if not ink:
        return {"think": "unknown", "hint": "三个档位一个都没定位到"}
    best = max(ink, key=ink.get)
    return {"think": best if ink[best] > 10 else "unknown", "ink": ink}


def set_think(s, key: str) -> dict:
    if key not in THINK:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(THINK)}
    cur = read_think(s)["think"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    if not _open_think(s):
        return {"ok": False, "stage": "menu_not_open", "before": cur}
    m = s.find(THINK[key][1], threshold=0.85)
    if m is None:
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m); time.sleep(1.2)
    s.move_to(-1200, 700); time.sleep(0.4)
    after = read_think(s)["think"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


REQUIRED = ([f"gem_item_{k}" for k in CFG.options]
            + [f"gem_chip_{k}" for k in CFG.options]
            + list(CFG.writer_tabs)
            + [THINK_ENTRY] + [t for _d, t in THINK.values()])


def main() -> int:
    argv = sys.argv[1:]
    # --think 走单独的流程（子菜单维度），其余交给通用 cli
    if "--think" in argv:
        i = argv.index("--think")
        if i + 1 >= len(argv):
            print("--think 缺值；可选：", ", ".join(THINK))
            return 2
        key = argv[i + 1].strip().lower()
        rest = argv[:i] + argv[i + 2:]
        s = Screen()
        if not s.bring_to_front(CFG.title_hint, on_secondary=CFG.on_secondary):
            print("找不到浏览器窗口"); return 1
        if "--tab" in rest:
            j = rest.index("--tab")
            if j + 1 < len(rest):
                s.hotkey("ctrl", rest[j + 1]); time.sleep(1.2)
        r = set_think(s, key)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3
    return cli(CFG, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
