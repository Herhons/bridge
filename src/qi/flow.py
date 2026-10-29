"""通用「提问 → 收回答」流程。四个网页端共用这一份，差异全部压进 Site 配置。

v0.0.8 的教训是同一套流程抄 8 遍，改一处要落 8 次。这里只有一份：
    找新对话（侧栏没开就先开）→ 点输入框 → 全选→输入 → 点发送
    → 核对新提问 → 等本轮回复完成 → 点击对应回复的复制控件
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

from . import guard
from .browser import ensure_site
from .screen import ForegroundLost, Match, Screen


@dataclass(frozen=True)
class Site:
    name: str                  # 端点名（用于日志）
    title_hint: str            # 窗口标题关键词，用来提到前台
    composer: str              # 输入框锚点模板名
    send: str                  # 发送键（可用态）模板名
    actions: str | tuple[str, ...]     # 回答动作条整排模板（对话/任务可能不是同一排，可给多个）
    copy: str                  # 动作条内复制键模板名
    composer_offset: tuple[int, int]   # 从锚点中心到文字区的偏移
    newchat: str | tuple[str, ...] | None = None   # 新对话按钮（可多个候选：整条 / 收起后的图标）
    sidebar: str | None = None         # 侧栏折叠/展开按钮（侧栏没开时先点它）
    stop: str | None = None            # 生成中的停止键
    send_mode: str = "color"           # color=靠颜色判断可用；exists=出现即可用
    send_blue_min: float = 100.0       # B-R 大于它算可点
    send_threshold: float = 0.92
    screen_reply: bool = False         # True=回答动作条不进控件树，只能在画面上找
    on_secondary: bool = True          # 只认副屏上那个浏览器窗口


def _win(s: Screen, site: Site):
    """目标窗口矩形。所有 find 都必须限定在它里面。

    不限定就是在整个虚拟桌面（-1440..2560）里找，主屏上开着同款页面时
    会先命中主屏那份，然后拿着主屏的坐标去点——点的是用户的窗口。
    """
    return s.window_rect(site.title_hint, on_secondary=site.on_secondary)


def _send_enabled(s: Screen, site: Site, m: Match) -> bool:
    if site.send_mode == "exists":
        return True
    b, _g, r = s.region_mean(m)
    return (b - r) > site.send_blue_min


def _open_new_chat(s: Screen, site: Site) -> dict:
    """点新对话。

    侧栏有两种形态：展开是一整条「新聊天」，收起只剩一个铅笔图标。
    所以这里按候选列表找：整条 → 图标 → 都不是就先点侧栏开关再找一遍。
    """
    if not site.newchat:
        return {"ok": True, "stage": "skipped"}
    if site.name == "deepseek":
        target = s.find("ds_newchat", region=s.window_rect())
        if target is None:
            return {"ok": False, "stage": "newchat_not_found"}
        s.click(target)
        time.sleep(0.5)
        return {"ok": True, "stage": "clicked"}
    from . import ui
    labels = {"deepseek": {"开启新对话", "新对话", "New chat"},
              "grok": {"聊天", "新聊天", "New chat"},
              "gemini": {"发起新对话", "新对话", "New chat"},
              "chatgpt": {"新聊天", "New chat"}}
    control, _ = ui.document(s.target_hwnd, site.name)
    nodes = [n for n in ui.tree(control).walk()
             if n.kind in ("ButtonControl", "HyperlinkControl")
             and n.name in labels[site.name] and not n.control.IsOffscreen]
    if not nodes:
        return {"ok": False, "stage": "newchat_not_found"}
    ui.click(s, nodes[0])
    time.sleep(0.9)
    return {"ok": True, "stage": "clicked"}

def _empty_probe(s: Screen, site: Site):
    """「输入框是空的」在网页端唯一可靠的判据是发送键不可用。

    读输入框文本不行：contenteditable 会把占位文字从 TextPattern 读出来
    （Gemini「问问 Gemini」、Grok 轮播、ChatGPT「使用 ChatGPT Work」），
    而占位文字和真输入在控件树里没有稳定区别——实测 Gemini 的占位文本节点
    就在输入框里面，和用户打的字同一个位置。

    发送键要两条都压低误判：
      1. 有「发送」名字的可用按钮 —— 空格子只有麦克风/听写，没有发送键；
      2. 模板命中且够高分（≥0.95）—— 麦克风在 gpt_send_on 上实测能匹到 0.911，
         按 0.90 判会把空输入框当成"有草稿"。
    """
    from .conversation import SEND

    limit = max(site.send_threshold, 0.95)

    def probe(view):
        if any(n.kind == "ButtonControl" and n.name in SEND and n.control.IsEnabled
               for n in view.root.walk()):
            return False
        m = s.find(site.send, threshold=limit, region=_win(s, site))
        if m is None:
            return True
        return not _send_enabled(s, site, m)

    return probe


def ask(site: Site, prompt: str, *, mode: str = "new", timeout: float = 180.0,
        verbose: bool = True, tab: int | None = None, prepare=None) -> dict:
    """prepare(s) -> dict：新对话之后、写输入之前要做的准备（比如切任务模式）。

    在新对话页明确设置写入器，再写入提问。
    """
    from .conversation import Conversation, ask as send_and_receive

    started = time.monotonic()
    screen = Screen(verbose=verbose)
    hwnd = ensure_site(site.name, tab=tab, continue_session=mode == "continue")
    if hwnd is None:
        return {"ok": False, "stage": "no_window", "submission": "not_sent", "sent": False}
    screen.bind(hwnd)
    if not screen.focus():
        return {"ok": False, "stage": "page_not_front", "submission": "not_sent", "sent": False}
    if mode == "new":
        opened = _open_new_chat(screen, site)
        if not opened["ok"]:
            return {**opened, "submission": "not_sent", "sent": False}
    if prepare is not None:
        prepared = prepare(screen)
        if not prepared["ok"]:
            return {"ok": False, "stage": "prepare_failed", "prepare": prepared,
                    "submission": "not_sent", "sent": False}
    images = (site.actions, site.copy) if site.screen_reply else None
    result = send_and_receive(Conversation(screen, site.name, empty=_empty_probe(screen, site),
                                           images=images), prompt, timeout)
    result["elapsed"] = round(time.monotonic() - started, 1)
    result["mode"] = mode
    return result


def cli(site: Site, argv: list[str], *, prepare=None, extra_help: str = "") -> int:
    import argparse
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    parser = argparse.ArgumentParser(prog=f"{site.name}_use.py", epilog=extra_help)
    parser.add_argument("prompt", help="完整提问，可包含换行")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--new", action="store_true", help="新对话（默认）")
    modes.add_argument("--continue", dest="continuing", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    args = parser.parse_args(argv)
    if not args.prompt.strip() or args.timeout <= 0:
        parser.error("提问不能为空，--timeout 必须大于 0")
    if not guard.guard(need, wait, force):
        return 4
    try:
        result = ask(site, args.prompt, mode="continue" if args.continuing else "new",
                     timeout=args.timeout, tab=args.tab, prepare=prepare)
    except RuntimeError as exc:
        result = {"ok": False, "stage": "preparation_failed", "submission": "not_sent",
                  "sent": False, "hint": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1
