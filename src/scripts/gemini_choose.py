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

import argparse
import json
import os
import sys
import time

import cv2

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import ForegroundLost, Library, Screen  # noqa: E402
from qi import guard  # noqa: E402
from qi.browser import ensure_site  # noqa: E402
from qi.choose import Chooser, choose_writer, read_writer  # noqa: E402
from qi.flow import Site, _open_new_chat  # noqa: E402

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

# 思考等级（--think）**未实现**，相关代码和模板已删除。
# 实测：子菜单只在鼠标真实悬停时展开，脚本点不开（点一下会把整个菜单关掉）。
# 之前这里留着一整套 _open_think / read_think / set_think，但 --think 分支直接返回
# not_supported，那些函数从来没被调用过，属于死代码，已清掉。


def _win(s):
    """目标窗口矩形。不限定就是在整个虚拟桌面里找，主屏上开着同款页面会先命中那边。"""
    return s.window_rect(CFG.title_hint, on_secondary=CFG.on_secondary)


def _items_visible(s) -> bool:
    return any(s.find(f"gem_item_{k}", threshold=0.85, region=_win(s)) is not None for k in CFG.options)


# 芯片右端的下拉箭头相对**麦克风**的偏移。
# 不能对输入框锚点用固定偏移：芯片左边界随模型名长短变（Flash-Lite 宽、Pro 短），
# 固定偏移在名字短的时候会点到空白，表现为"菜单打不开"。
# 麦克风是右对齐的，位置不变，用它当基准稳。
MIC_TO_CARET = (-68, 0)


def _open_model_menu(s) -> bool:
    """用麦克风（右对齐，位置固定）作基准，点芯片右端的下拉箭头。

    输入框空时右侧是麦克风；有文字时是发送键，两者同槽位，所以依次兜底。
    """
    if _items_visible(s):
        return True
    s.press("esc")
    time.sleep(0.3)
    base = s.find("gem_mic", threshold=0.85, region=_win(s)) or s.find("gem_send_on", threshold=0.85, region=_win(s))
    if base is None:
        return False
    s.click(base, offset=MIC_TO_CARET)
    time.sleep(1.1)
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
        m = s.find(f"gem_item_{k}", threshold=0.88, region=_win(s))
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
    m = s.find(f"gem_item_{key}", threshold=0.88, region=_win(s))
    if m is None:
        s.press("esc")
        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}
    s.click(m)
    time.sleep(1.2)
    _w = _win(s)
    if _w:
        s.move_to(_w[0] + 60, _w[1] + 60)   # 移开鼠标，别压在菜单/芯片上
    time.sleep(0.4)
    after = read_model(s)["model"]
    return {"ok": after == key, "stage": "done" if after == key else "unverified",
            "before": cur, "after": after}


REQUIRED = ([f"gem_item_{k}" for k in CFG.options]
            + ["gem_mic"]
            + list(CFG.writer_tabs))


def main() -> int:
    need, wait, force, argv = guard.parse_guard_args(list(sys.argv[1:]))
    parser = argparse.ArgumentParser(description="选择 Gemini 写入器和模型，不发送消息")
    parser.add_argument("--list", "-l", action="store_true", help="读取当前模式和模型")
    parser.add_argument("--new", action="store_true", help="先开新对话")
    parser.add_argument("--writer", choices=CFG.writers, type=str.lower)
    parser.add_argument("--model", choices=CFG.options, type=str.lower)
    parser.add_argument("--think", help="思考等级（当前未实现）")
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    args = parser.parse_args(argv)
    if args.list and (args.writer is not None or args.model is not None or args.think is not None):
        parser.error("--list 不能与 --writer、--model、--think 同用；--new --list 可以同用")
    if not (args.list or args.new or args.writer or args.model or args.think):
        parser.print_help()
        return 2
    if args.think is not None:
        print(json.dumps({"ok": False, "stage": "not_supported",
                          "hint": "Gemini 思考等级尚未实现"}, ensure_ascii=False))
        return 1

    lib = Library()
    required = REQUIRED + (["gem_newchat"] if args.new else [])
    print(lib.report(required))
    if lib.missing(required):
        return 3
    if not guard.guard(need, wait, force):
        return 4
    sc = Screen()
    hwnd = ensure_site("gemini", tab=args.tab)
    if hwnd is None:
        print("没有可操作的 Gemini 标签页")
        return 1
    sc.bind(hwnd)
    if not sc.focus():
        print("窗口提不到前台")
        return 1
    time.sleep(1.2)

    try:
        if args.new:
            site = Site(name="gemini", title_hint=CFG.title_hint, composer="gem_composer",
                        send="gem_send_on", actions="gem_actions", copy="gem_copy",
                        composer_offset=(200, 0), newchat="gem_newchat")
            result = _open_new_chat(sc, site)
            print(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return 1
        if args.list:
            writer = read_writer(sc, CFG)["writer"]
            out = {"writer": writer, **read_model(sc)}
            if writer in (None, "unknown"):
                out["note"] = "模式按钮只在新对话页出现；可用 --new --list 开新对话后读取。"
            print(json.dumps(out, ensure_ascii=False))
            # 模式读不出来也算失败：返回 0 会让验证器把"没读到"记成通过
            bad = out["model"] == "unknown" or writer in (None, "unknown")
            return 1 if bad else 0
        if args.writer is not None:
            result = choose_writer(sc, CFG, args.writer)
            print(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return 1
        if args.model is not None:
            result = set_model(sc, args.model)
            print(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return 1
    except ForegroundLost as exc:
        print(json.dumps({"ok": False, "stage": "user_took_over", "hint": str(exc)},
                         ensure_ascii=False))
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
