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

from qi import ForegroundLost, Library, Screen, console, uilock  # noqa: E402
from qi import failure, guard  # noqa: E402
from qi import zoom as zoom_mod  # noqa: E402
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


CHECK_INK_BOX = (-26, 3, 22, 22)      # 勾的取样窗：相对标题模板左上角 (dx, dy, w, h)


def _check_ink(band) -> int:
    """采样窗里"勾"的亮像素数；**不是勾的形状就返回 0**。

    为什么不能只数亮像素：菜单刚打开时**第一项可能带一个白色焦点框**（2026-11-02 实测），
    框的左边线正好擦着采样窗的左边，能数出几十个亮像素，比真勾（30）还多——
    只数像素会把"焦点停在第一项"误读成"选的就是第一项"，而它恰恰不是。
    勾是 11 列 × 8 行的一小坨弯线；焦点框的边是 2~3 列、二十来行的直线。
    所以要求列数 ≥5：直线过不了，勾过得去。
    """
    g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    mask = g > 100
    cols = int(mask.any(axis=0).sum())
    rows = int(mask.any(axis=1).sum())
    if cols < 5 or rows < 3:
        return 0
    return int(mask.sum())


def read_model(s) -> dict:
    """读当前模型：看菜单项**左侧**有没有勾。

    CARD-07 第 4 条：2026-11-02 的菜单换了排版，旧采样窗 (m.x-45, m.y+10, 26, 24)
    从 m.y+10 起、到 m.x-19 止，把勾（垂直居中、离标题左边缘 12~18px）整个错过去，
    四个档位全读成 0、model=unknown。现在采「标题左侧紧邻的一小块」，
    并且用 `_check_ink` 按形状认勾（顺手排掉第一项的焦点框）。
    """
    if not _open_model_menu(s):
        return {"model": "unknown", "hint": "打不开模型菜单"}
    ink = {}
    dx, dy, w, h = CHECK_INK_BOX
    for k in CFG.options:
        m = s.find(f"gem_item_{k}", threshold=0.88, region=_win(s))
        if m is not None:
            ink[k] = _check_ink(s.grab((m.x + dx, m.y + dy, w, h)))
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


def _record_receipt(hwnd, dimension, target, readback, raw):
    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**

    读回为空/读不回原名时传 None：use 会因此拒发——那是实话。
    """
    try:
        from qi import tier_guard
        tier_guard.record_choice("gemini", target=target, readback=readback,
                                 dimension=dimension, hwnd=hwnd, raw=raw)
    except Exception:                                  # noqa: BLE001 - 回执不是主流程
        pass


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
        # 页面缩放预检（CARD-06b 第 6 条）：非 100% 先 Ctrl+0，仍不是就退出
        zoom_mod.ensure(sc, verbose=True)
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
            rec = globals().get("_record_receipt")
            if callable(rec):
                rec(hwnd, "model", args.model,
                    None if result.get("after") in (None, "unknown") else result.get("after"),
                    result)
    except ForegroundLost as exc:
        print(json.dumps({"ok": False, "stage": "user_took_over", "hint": str(exc)},
                         ensure_ascii=False))
        return 4
    except zoom_mod.ZoomNot100 as exc:
        result = {**exc.as_result(), "ok": False}
        failure.record("gemini", result, screen=sc, hwnd=hwnd)
        print(json.dumps(result, ensure_ascii=False))
        return exc.code
    return 0


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(uilock.run("gemini_choose", main))
