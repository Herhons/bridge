"""选择类脚本的通用流程：打开菜单 → 点某一项 → 复核。

没有 OCR 就读不出任意文字，所以每个档位靠**两张图**代表：
    <prefix>item_<key>.png   菜单里那一行的样子（点它）
    <prefix>chip_<key>.png   选中后收起时芯片的样子（读它）
有 chip 的档位才能被 --list 读出来；没有的只能"设"，不能"读"。

三个端的差异（锚点、偏移、模板前缀）全在配置里，流程只有这一份。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

from .screen import Screen


@dataclass(frozen=True)
class Chooser:
    name: str
    anchor: str                       # 输入框锚点模板（用来定位菜单位置）
    open_offset: tuple[int, int]      # 从锚点点到"模型芯片"的偏移
    prefix: str                       # 模板前缀，如 "grok_"
    options: dict[str, str] = field(default_factory=dict)  # key -> 中文说明
    has_chip: tuple[str, ...] = ()    # 哪些 key 有 chip 图（可读）
    open_by_chip: bool = False        # True=点当前芯片开菜单（菜单宽度随名字变时更稳）
    title_hint: str = "Google Chrome"  # 提到前台的窗口标题关键词
    on_secondary: bool = True          # 只认副屏上那个浏览器窗口


def _item(cfg: Chooser, key: str) -> str:
    return f"{cfg.prefix}item_{key}"


def _chip(cfg: Chooser, key: str) -> str:
    return f"{cfg.prefix}chip_{key}"


def read_current(s: Screen, cfg: Chooser) -> dict:
    """当前选的是哪个档：靠芯片图认。认不出就报 unknown，不猜。"""
    for key in cfg.has_chip:
        m = s.find(_chip(cfg, key))
        if m is not None:
            return {"state": key, "score": round(m.score, 3)}
    return {"state": "unknown",
            "hint": "当前芯片和任何一张 chip 图都对不上（可能是没截过的档位，或主题/缩放变了）"}


def open_menu(s: Screen, cfg: Chooser) -> bool:
    if cfg.open_by_chip:
        # 点当前芯片本体：芯片宽度随模型名变，但它是右对齐的，用图定位最稳
        for key in cfg.has_chip:
            m = s.find(_chip(cfg, key), threshold=0.90)
            if m is not None:
                s.click(m)
                time.sleep(0.8)
                break
        else:
            return False
    else:
        a = s.find(cfg.anchor, threshold=0.85)
        if a is None:
            return False
        s.click(a, offset=cfg.open_offset)
        time.sleep(0.7)
    # 菜单开了会有任意一个 item 图出现
    for key in cfg.options:
        if s.find(_item(cfg, key), threshold=0.88) is not None:
            return True
    return False


def choose(s: Screen, cfg: Chooser, key: str) -> dict:
    if key not in cfg.options:
        return {"ok": False, "stage": "bad_arg", "want": key,
                "options": list(cfg.options)}
    before = read_current(s, cfg)["state"]
    if not open_menu(s, cfg):
        return {"ok": False, "stage": "menu_not_open", "before": before,
                "hint": "点不开菜单：确认页面在前台、芯片位置没变（换主题/缩放要重截模板）"}
    hit = s.find(_item(cfg, key), threshold=0.88)
    if hit is None:
        return {"ok": False, "stage": "item_not_found", "before": before, "want": key,
                "hint": f"菜单里找不到 {_item(cfg, key)}.png"}
    s.click(hit)
    time.sleep(0.9)

    if key in cfg.has_chip:
        after = read_current(s, cfg)
        ok = after["state"] == key
        return {"ok": ok, "stage": "done" if ok else "unverified",
                "before": before, "after": after["state"], "chip_score": after.get("score")}
    # 没有 chip 图：只能确认菜单已经关了
    still = any(s.find(_item(cfg, k), threshold=0.90) is not None for k in cfg.options)
    return {"ok": not still, "stage": "done_no_chip" if not still else "menu_still_open",
            "before": before, "after": key, "verified_by": "menu_closed"}


def cli(cfg: Chooser, argv: list[str]) -> int:
    name = f"{cfg.name}_choose.py"
    do_list, want, bad, help_, tab = False, None, [], False, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-l", "--list"):
            do_list = True
        elif a == "--tab":
            if i + 1 >= len(argv):
                bad.append("--tab 缺值")
            else:
                i += 1
                try:
                    tab = int(argv[i])
                except ValueError:
                    bad.append(f"--tab 要数字，收到 {argv[i]!r}")
        elif a in ("--model", "--effort", "--mode"):
            if i + 1 >= len(argv):
                bad.append(f"{a} 缺值")
            else:
                i += 1
                want = argv[i].strip().lower()
        elif a in ("-h", "--help"):
            help_ = True
        else:
            bad.append(a)
        i += 1

    if help_ or (not do_list and want is None):
        print("用法：")
        print(f"  python {name} --list [--tab N]")
        print(f"  python {name} --model <{'|'.join(cfg.options)}> [--tab N]")
        print("  --tab N   先按 Ctrl+N 切到第 N 个标签页（左边数）")
        for k, desc in cfg.options.items():
            mark = "" if k in cfg.has_chip else "  （只能设，不能读）"
            print(f"    {k:8} {desc}{mark}")
        return 0 if help_ else 2
    if bad:
        print("参数有问题：", bad)
        return 2
    if want is not None and want not in cfg.options:
        print(f"没有这个档位：{want}")
        print("可选：", ", ".join(cfg.options))
        return 2

    s = Screen()
    if not s.bring_to_front(cfg.title_hint, on_secondary=cfg.on_secondary):
        print("找不到浏览器窗口")
        return 1
    if tab:
        s.hotkey("ctrl", str(tab))
        time.sleep(1.2)
    if do_list:
        print(json.dumps(read_current(s, cfg), ensure_ascii=False))
        return 0
    r = choose(s, cfg, want)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1
