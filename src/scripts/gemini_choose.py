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
from urllib.parse import urlsplit

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


DEBUG_DIR = os.path.join(_SRC, "..", "evidence", "fail")

# 芯片按钮的 UIA 名字（实测形态：`打开模式选择器，当前模式为“Argon”`）。
# Gemini 把"模型"叫"模式"，两个词都收，换文案时至少中一个。
CHIP_MARKS = ("当前模式为", "当前模型为")


def _chip_probe(s):
    """只读地找芯片按钮，并带回"为什么没找到"：(node, 名字, why, url)。

    why:
      · ok                —— 找到了
      · document_unbound  —— 连 Gemini 文档都没绑上（窗口里不是 Gemini 页 / 读不到）
      · no_chip           —— 文档绑上了，但页面上没有模型芯片

    2026-11-07 现场（05:17:27，hwnd=67552）：只回了 `menu_not_open/no_chip`，
    分不清"不是 Gemini 页"和"这一页本来就没有芯片"。两种原因要分开报，
    否则下一个人只能靠猜（本次只读复核：同一个 hwnd 在 `/app` 新对话页上
    芯片是 `打开模式选择器，当前模式为“Flash”`，read_model=flash/source=uia）。
    """
    try:
        from qi import ui
        control, url = ui.document(s.target_hwnd, "gemini")
        root = ui.tree(control)
    except Exception as exc:                # noqa: BLE001 - UIA 不可用就退回模板路径
        return None, "", "document_unbound", f"{type(exc).__name__}: {exc}"
    for node in root.walk():
        if node.kind != "ButtonControl":
            continue
        name = (node.name or "").strip()
        if name and any(mark in name for mark in CHIP_MARKS):
            return node, name, "ok", url
    return None, "", "no_chip", url


def _chip_node(s):
    """当前模型芯片的 UIA 按钮：(node, 名字) 或 None。

    为什么要有它：旧实现靠"麦克风往左 68px"这个**像素偏移**点芯片，
    芯片宽度/位置一变就点到空白，表现就是 menu_not_open。
    按钮名是**控制身份**，不受布局影响；点它自己也不会点错。
    """
    node, name, _why, _url = _chip_probe(s)
    return None if node is None else (node, name)


def _is_new_chat_url(url) -> bool:
    """模型芯片只在新对话页（`/app`）出现；会话页是 `/app/<id>`。"""
    try:
        return urlsplit(str(url or "")).path.rstrip("/") == "/app"
    except (TypeError, ValueError):
        return False


def _is_conversation_url(url) -> bool:
    """看起来就是一个 Gemini 会话页（`gemini.google.com/app/<id>`）。"""
    try:
        parts = urlsplit(str(url or ""))
    except (TypeError, ValueError):
        return False
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    return host.endswith("gemini.google.com") and path.startswith("/app/") and len(path) > 5


def _chip_missing_hint(why: str, url) -> str:
    """芯片找不到时的**准确**原因（不是"再试试"）。"""
    if why == "document_unbound":
        return f"当前窗口的活动文档不是 Gemini 页面（或读不到）：{url}"
    if _is_conversation_url(url):
        return (f"模型选择器只在新对话页（/app）出现，当前是 {url}："
                "先 --new 开新对话再选模型")
    return "找不到模型芯片（UIA 和麦克风锚点都没认出来）"


def _chip_tail(name: str) -> str:
    """从按钮名里取档位词：`…当前模式为“Argon”` -> `Argon`。"""
    for mark in CHIP_MARKS:
        if mark in name:
            tail = name.split(mark, 1)[1]
            return tail.strip().strip("“”\"'’‘›>").strip()
    return ""


def _model_from_uia(s, probe=None):
    """UIA 读模型：读不到芯片返回 None（交给菜单路径）；读到但认不出返回 unknown。"""
    node, name, _why, _url = probe or _chip_probe(s)
    if node is None:
        return None
    tail = _chip_tail(name) or name
    key = None
    try:
        from qi.chip import GEMINI_MODEL_NAMES
        key = GEMINI_MODEL_NAMES.get(tail.lower())
    except Exception:                       # noqa: BLE001
        key = None
    if key is None:
        low = tail.lower()
        key = next((k for k, label in CFG.options.items()
                    if k.replace("_", " ") in low or label.split("（")[0].strip().lower() in low),
                   None)
    if key in CFG.options:
        return {"model": key, "text": name, "chip": tail, "source": "uia"}
    return {"model": "unknown", "text": name, "chip": tail, "source": "uia",
            "hint": f"UIA 芯片读到 {tail!r}，不在已知模型清单里，不能确认"}


# 芯片右端的下拉箭头相对**麦克风**的偏移。
# 不能对输入框锚点用固定偏移：芯片左边界随模型名长短变（Flash-Lite 宽、Pro 短），
# 固定偏移在名字短的时候会点到空白，表现为"菜单打不开"。
# 麦克风是右对齐的，位置不变，用它当基准稳。
# **2026-11-07 起这只是兜底**：优先点 UIA 认出来的芯片按钮本身（见 _chip_node）。
MIC_TO_CARET = (-68, 0)


def _open_model_menu(s):
    """打开模型菜单。返回 "open" / "clicked_unverified" / "no_chip"。

    顺序：已经是开的 -> 点 UIA 芯片按钮 -> 老办法（麦克风往左 68px）。
    点完一律用"菜单项能不能匹到"确认，**不确认就不往下点**。
    """
    if _items_visible(s):
        return "open"
    s.press("esc")
    time.sleep(0.3)
    got = _chip_node(s)
    if got is not None:
        node, _name = got
        clicked = False
        try:
            from qi import ui
            ui.click(s, node)
            time.sleep(1.1)
            clicked = True
        except Exception:                   # noqa: BLE001 - 控件失效就退回偏移
            clicked = False
        if _items_visible(s):
            return "open"
        if clicked:
            # 芯片点到了但菜单项模板一个都没匹上：模板过期的典型症状，别再乱点
            return "clicked_unverified"
    base = s.find("gem_mic", threshold=0.85, region=_win(s)) or s.find(
        "gem_send_on", threshold=0.85, region=_win(s))
    if base is None:
        return "no_chip"
    s.click(base, offset=MIC_TO_CARET)
    time.sleep(1.1)
    return "open" if _items_visible(s) else "clicked_unverified"


def _evidence(s, tag: str) -> str | None:
    """菜单/选项读不出来时留一张现场图，方便重截模板。失败不影响主流程。"""
    try:
        win = _win(s)
        if not win:
            return None
        img = s.grab(win)
        if img is None or not getattr(img, "size", 0):
            return None
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"gemini_{tag}_{time.strftime('%Y%m%d-%H%M%S')}.png")
        cv2.imwrite(path, img)
        return path
    except Exception:                       # noqa: BLE001
        return None


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
    """读当前模型：优先 UIA 芯片按钮（精确文本 + 控制身份），再退回"开菜单看勾"。

    2026-11-07：旧实现只会开菜单看勾，而开菜单靠"麦克风往左 68px"这个像素偏移；
    偏移一失效就同时得到 menu_not_open 和 model=unknown（执行方看到的就是这两个）。
    UIA 按钮名（`…当前模式为“Argon”`）不依赖布局，读到就是读到，读不到才走老路。
    """
    probe = _chip_probe(s)
    uia = _model_from_uia(s, probe)
    if uia is not None:
        return uia
    node, _name, why, url = probe
    # 文档都没绑上 / 是会话页：**先把原因报清楚，不去点**（老实现会去点麦克风
    # 左边 68px 那块空白，既证明不了什么，也可能点到别的东西）。
    if node is None and why in ("document_unbound", "no_chip") \
            and (why == "document_unbound" or _is_conversation_url(url)):
        return {"model": "unknown", "source": "none", "detail": why,
                "stage": "menu_not_open", "url": url, "clicks": 0,
                "hint": _chip_missing_hint(why, url)}
    st = _open_model_menu(s)
    if st != "open":
        no_chip = st == "no_chip"
        return {"model": "unknown", "source": "none",
                "detail": (why or "no_chip") if no_chip else st,
                "stage": "menu_not_open" if no_chip else "menu_items_unreadable",
                "url": url,
                "hint": (_chip_missing_hint(why, url) if no_chip
                         else "芯片点到了，但菜单项模板一个都没匹到：模板可能过期，"
                              "按卡面不下调阈值、不猜着点")}
    ink = {}
    dx, dy, w, h = CHECK_INK_BOX
    for k in CFG.options:
        m = s.find(f"gem_item_{k}", threshold=0.88, region=_win(s))
        if m is not None:
            ink[k] = _check_ink(s.grab((m.x + dx, m.y + dy, w, h)))
    s.press("esc")
    time.sleep(0.3)
    if not ink:
        return {"model": "unknown", "source": "menu",
                "hint": "模型项一个都没定位到"}
    best = max(ink, key=ink.get)
    return {"model": best if ink[best] > 5 else "unknown", "ink": ink, "source": "menu"}


def set_model(s, key: str) -> dict:
    if key not in CFG.options:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(CFG.options)}
    # 先做**只读**判断：页面不对就一个东西都不点（连 Esc 都不按）。
    # 会话页（/app/<id>）没有模型选择器；老实现会去点"麦克风左边 68px"那块空白。
    node, _name, why, url = _chip_probe(s)
    if node is None and why == "document_unbound":
        return {"ok": False, "stage": "document_unbound", "before": None, "want": key,
                "detail": url, "clicks": 0,
                "hint": "当前窗口的活动文档不是 Gemini 页面（或读不到）：先切到 Gemini 标签页；"
                        "本次没有点任何东西"}
    if node is None and why == "no_chip" and _is_conversation_url(url):
        return {"ok": False, "stage": "page_not_new_chat", "before": None, "want": key,
                "url": url, "clicks": 0,
                "hint": "模型选择器只在新对话页（/app）出现：先 --new 开新对话再选；"
                        "本次没有点任何东西"}
    cur = read_model(s)["model"]
    if cur == key:
        return {"ok": True, "stage": "already", "before": cur, "after": cur}
    st = _open_model_menu(s)
    if st != "open":
        shot = _evidence(s, "menu" if st == "no_chip" else "items")
        return {"ok": False,
                "stage": "menu_not_open" if st == "no_chip" else "menu_items_unreadable",
                "before": cur, "want": key, "detail": st, "evidence_shot": shot,
                "hint": (_chip_missing_hint(why, url) if st == "no_chip" else
                         "芯片点到了但菜单项模板全部失配（模板可能过期）："
                         "已留现场图，未点任何选项、未下调阈值")}
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
    sc.tab_site = "gemini"
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
    raise SystemExit(uilock.entry("gemini_choose", main))
