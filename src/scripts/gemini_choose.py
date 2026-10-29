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

from qi import Library, Screen  # noqa: E402
from qi.choose import Chooser, cli, open_menu, read_writer  # noqa: E402

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
# 模型芯片在输入框锚点右边固定的位置（输入框有最大宽度且居中，所以这个偏移稳定）
MENU_DX, MENU_DY = 600, -3


def _items_visible(s) -> bool:
    return any(s.find(f"gem_item_{k}", threshold=0.85) is not None for k in CFG.options)


def _open_model_menu(s) -> bool:
    """用输入框锚点 + 偏移打开模型菜单。

    不用芯片图定位：芯片会因聚焦（蓝框）/悬停变样式，模板经常对不上。
    锚点 gem_composer 一直是 1.000，稳定得多。
    """
    if _items_visible(s):
        return True
    s.press("esc")
    time.sleep(0.25)
    a = s.find("gem_composer", threshold=0.85)
    if a is None:
        return False
    s.click(a, offset=(MENU_DX, MENU_DY))
    time.sleep(1.0)
    return _items_visible(s)


def read_model(s) -> dict:
    """读当前模型：看菜单项**左侧**有没有勾。

    实测（2026-10-29 新菜单）：勾在标题左边，采样窗 = (m.x-45, m.y+10, 26, 24)，
    选中 42 个亮像素，未选 0。之前判成"勾位置会变"，是因为整行模板把定位点带偏了；
    现在模板只截标题文字（125x19），定位稳定到 1.000，勾区就跟着稳了。
    """
    if not _open_model_menu(s):
        return {"model": "unknown", "hint": "打不开模型菜单"}
    ink = {}
    for k in CFG.options:
        m = s.find(f"gem_item_{k}", threshold=0.88)
        if m is not None:
            band = s.grab((m.x - 45, m.y + 10, 26, 24))
            g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            ink[k] = int((g > 110).sum())
    s.press("esc")
    time.sleep(0.3)
    if not ink:
        return {"model": "unknown", "hint": "模型项一个都没定位到"}
    best = max(ink, key=ink.get)
    return {"model": best if ink[best] > 5 else "unknown", "ink": ink}


def set_model(s, key: str) -> dict:
    if key not in CFG.options:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(CFG.options)}
    cur = read_model(s)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    if not _open_model_menu(s):
        return {"ok": False, "stage": "menu_not_open", "before": cur}
    m = s.find(f"gem_item_{key}", threshold=0.88)
    if m is None:
        s.press("esc")
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m)
    time.sleep(1.2)
    s.move_to(-1300, 700)
    time.sleep(0.4)
    after = read_model(s)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


def _open_think(s) -> bool:
    """打开模型菜单 → 点「思考等级」→ 子菜单。"""
    if not any(s.find(t, threshold=0.85) for _d, t in THINK.values()):
        if s.find(THINK_ENTRY, threshold=0.85) is None:
            if not _open_model_menu(s):
                return False
            time.sleep(0.4)
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
            + list(CFG.writer_tabs)
            + [THINK_ENTRY] + [t for _d, t in THINK.values()])


def main() -> int:
    argv = sys.argv[1:]
    # --think 走单独的流程（子菜单维度），其余交给通用 cli
    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 >= len(argv):
            print("--model 缺值；可选：", ", ".join(CFG.options))
            return 2
        key = argv[i + 1].strip().lower()
        rest = argv[:i] + argv[i + 2:]
        sc = Screen()
        if not sc.bring_to_front(CFG.title_hint, on_secondary=CFG.on_secondary):
            print("找不到浏览器窗口"); return 1
        if "--tab" in rest:
            j = rest.index("--tab")
            if j + 1 < len(rest):
                sc.hotkey("ctrl", rest[j + 1]); time.sleep(1.2)
        r = set_model(sc, key)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if "--think" in argv:
        # 实测：思考等级子菜单点一下会把整个菜单关掉，悬停也不触发（鼠标瞬移不产生 hover）。
        # 与其留一个永远失败的开关，不如明确说不支持。
        print(json.dumps({"ok": False, "stage": "not_supported",
                          "hint": "Gemini 思考等级未支持：子菜单只在鼠标真实悬停时展开，脚本点不开。"
                                  "需要的话请在网页上手动选。"}, ensure_ascii=False))
        return 1

    if "--list" in argv:
        sc = Screen()
        if not sc.bring_to_front(CFG.title_hint, on_secondary=CFG.on_secondary):
            print("找不到浏览器窗口"); return 1
        if "--tab" in argv:
            j = argv.index("--tab")
            if j + 1 < len(argv):
                sc.hotkey("ctrl", argv[j + 1]); time.sleep(1.2)
        # 读状态不改页面。模式按钮只在新对话页出现，会话页里读不到是正常的。
        w = read_writer(sc, CFG).get("writer")
        out = {"writer": w}
        out.update(read_model(sc))
        if w in (None, "unknown"):
            out["note"] = "模式按钮只在新对话页出现（当前在会话页）。用 --new 先开新对话再读；本命令不替你重置会话。"
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if "--new" in argv:
        # 显式要求：先开新对话，再读/切模式
        sc = Screen()
        if not sc.bring_to_front(CFG.title_hint, on_secondary=CFG.on_secondary):
            print("找不到浏览器窗口"); return 1
        if "--tab" in argv:
            j = argv.index("--tab")
            if j + 1 < len(argv):
                sc.hotkey("ctrl", argv[j + 1]); time.sleep(1.2)
        from qi.flow import Site, _open_new_chat
        site = Site(name="gemini", title_hint=CFG.title_hint, composer="gem_composer",
                    send="gem_send_on", actions="gem_actions", copy="gem_copy",
                    composer_offset=(200, 0), newchat="gem_newchat")
        r = _open_new_chat(sc, site)
        time.sleep(1.2)
        rest = [a for a in argv if a != "--new"]
        if not any(a.startswith("--model") or a.startswith("--writer") for a in rest):
            print(json.dumps({"newchat": r, "writer": read_writer(sc, CFG).get("writer")},
                             ensure_ascii=False))
            return 0 if r.get("ok") else 1
        return cli(CFG, rest)

    lib = Library()
    print(lib.report(REQUIRED))
    if lib.missing(REQUIRED):
        return 3
    return cli(CFG, sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
