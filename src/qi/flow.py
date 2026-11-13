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
from typing import Protocol, runtime_checkable

from . import exit_codes as ec
from . import guard, tier_guard, ui
from .browser import ensure_site
from .screen import ForegroundLost, Match, Screen
from .ui import UiDocsLost
from .window import WindowMinimized


@runtime_checkable
class SiteDriver(Protocol):
    """端驱动协议（原 src/qi/drivers/__init__.py，S4 骨架；本轮只定义接口，不迁实现）。

    目标架构：**一个总控 + 各端无状态驱动**。总控独占 UI 锁、会话锚点、账本、
    超时与硬停判定；各端驱动只做原子动作，不自己重试、不管锁、不写账本。
    """
    name: str

    def ensure_session(self, *, round_name: str, prompt_hash: str) -> dict:
        """确保当前页面是本轮会话；返回 {action, url}，不得自己新建。"""

    def choose_model(self, purpose: str) -> dict:
        """选档并返回回执摘要 {ok, model}。"""

    def upload(self, path: str) -> dict:
        """上传附件；返回 {ok, filename}。"""

    def submit(self, text: str) -> dict:
        """写入并提交；返回 {ok, stage, submission}。"""

    def read_reply(self) -> dict:
        """只读取回复；返回 {ok, reply}。"""



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
    # ---- CARD-09e：本端专用的三条（默认全关，别端行为一个字节不变）----
    composer_name: str | None = None   # 输入框的 UIA 名字（清草稿时精确认领，不碰地址栏）
    clear_draft_on_new: bool = False   # --new 之后有残留草稿就清空（第 4 条）
    paste_plain: bool = False          # 打字核对不上时改用粘贴纯文本（第 5 条）
    uia_fallback: bool = False         # 超时/被打断后按页面标记读最后一条回复（第 3 条）
    # ---- B17：本端已经有了「空的新对话页」时，`--new` 不要再点一次新聊天 ----
    # chatgpt 新版界面把模型选择做成**按会话**的：再点一次「新聊天」模型就回到
    # 默认（强度是账号级的，反而留着）。于是 choose 刚设好的模型会被 use 抹掉，
    # 档位闸报 tier_drift（2026-11-09 真机）。给一个 `screen -> bool` 判据，
    # 判得出来就不点；判不出来（None/异常）走老路，行为不变。
    blank_new: object = None
    # ---- B19：写入/判空的两处加固（默认全关，别端行为一个字节不变）----
    # 粘贴必须确认落地：没落地就抛 WriteNotLanded、清空重写，而不是静默返回后
    # 让 input_mismatch 顶锅（deepseek 2450 字只写进前 3 个字的根因）。
    write_verify: bool = False
    # 输入框里是"本提问的前缀"时，认成自己上一轮的残草清掉，不报 draft_present。
    adopt_self_draft: bool = False
    # 判空以输入框文本为准（文本明确为空就是空），不看发送键可用态。
    # deepseek 有草稿时按钮本来就深蓝，旧判据会把它自己的残草当成用户草稿。
    empty_by_text: bool = False
    # 本端的占位文字（gemini 的「问问 Gemini」不在公共 KNOWN_PLACEHOLDERS 里）。
    placeholders: tuple = ()
    # 单次粘贴的字数上限（0 = 不分块）。deepseek 实测：单次粘贴 >2000 字会被
    # 整段吞掉，分块之后总量可以远超 2000。
    paste_chunk: int = 0


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


NEWCHAT_LABELS = {
    "deepseek": {"开启新对话", "新对话", "New chat"},
    "grok": {"聊天", "新聊天", "New chat"},
    "gemini": {"发起新对话", "新对话", "New chat"},
    "chatgpt": {"新聊天", "New chat"},
}

# 侧栏折叠时，「新对话」那一项可能整个不在控件树里，得先把侧栏打开。
# 只收**明确的侧栏开关**：裸的「展开」在 Gemini 页面上还有别的同名按钮
# （实测 1739,476 那个是展开回答面板），不能点。
SIDEBAR_TOGGLES = (
    "打开边栏", "打开侧边栏", "展开边栏", "展开侧边栏", "显示边栏", "显示侧边栏",
    "Show sidebar", "Expand sidebar", "Open sidebar",
    "主菜单", "打开主菜单", "Open menu",
)


def _newchat_nodes(root, site: Site) -> list:
    """按名字找「新对话」入口。侧栏折叠/展开两态都认（CARD-06c 第 5 条）。

    Gemini 折叠态那一项是 41px 宽的小图标、展开态是整条 340px 的链接，
    名字都叫「发起新对话」；角色还可能是 Hyperlink / Button / Text，
    所以走 `ui.named` 的 CLICKABLE 集合，不限定角色。
    """
    labels = NEWCHAT_LABELS.get(site.name)
    if not labels:
        return []
    return ui.prefer_clickables(ui.named(root, labels))


def _sidebar_nodes(root) -> list:
    return ui.prefer_clickables(ui.named(root, set(SIDEBAR_TOGGLES)))


def _open_new_chat(s: Screen, site: Site) -> dict:
    """点新对话。

    侧栏有两种形态：展开是一整条「新聊天」，收起只剩一个图标——甚至整项都不渲染。
    所以按这个顺序试（CARD-06c 第 5 条「两态都能找到」）：

        找入口 -> 找不到就先点侧栏开关（先模板、再 UIA 名字）-> 再找 -> 还不行才报错

    每一步都写进返回值的 `tried` 里，失败时看日志就知道卡在哪一态。
    """
    if not site.newchat:
        return {"ok": True, "stage": "skipped"}
    if callable(getattr(site, "blank_new", None)):
        try:
            if site.blank_new(s):
                return {"ok": True, "stage": "already_new", "basis": "blank_new"}
        except Exception:                    # noqa: BLE001 - 判不出来就走老路
            pass
    if site.name == "deepseek":
        target = s.find("ds_newchat", region=s.window_rect())
        if target is None:
            return {"ok": False, "stage": "newchat_not_found"}
        s.click(target)
        time.sleep(0.5)
        return {"ok": True, "stage": "clicked"}
    control, _ = ui.document(s.target_hwnd, site.name)
    tried: list[str] = []
    attempts = 0
    for attempt in range(3):
        attempts = attempt + 1
        view = ui.tree(control)          # 每轮只取一次快照，两个判断用同一份
        nodes = _newchat_nodes(view, site)
        if nodes:
            ui.click(s, nodes[0])
            time.sleep(0.9)
            return {"ok": True, "stage": "clicked", "basis": "uia",
                    "role": nodes[0].kind, "attempt": attempts, "tried": tried}
        if not tried:
            # 先试本端登记的侧栏模板（按钮位置固定的那几端），再试 UIA 名字
            if site.sidebar:
                match = s.find(site.sidebar, threshold=0.85,
                               region=s.window_rect(site.title_hint,
                                                    on_secondary=site.on_secondary))
                if match is not None:
                    s.click(match)
                    tried.append(f"sidebar_template:{site.sidebar}")
                    time.sleep(0.9)
                    continue
            toggles = _sidebar_nodes(view)
            if toggles:
                ui.click(s, toggles[0])
                tried.append(f"sidebar_uia:{toggles[0].name}")
                time.sleep(0.9)
                continue
        time.sleep(0.6)
    return {"ok": False, "stage": "newchat_not_found",
            "hint": f"控件树里没有「新对话」入口（试了 {attempts} 轮；"
                    f"用过的侧栏开关：{tried or '没有找到'}）；"
                    "侧栏折叠/展开两态都没找到，可能页面还没渲染完或改版了"}

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
        # B19：文本优先。文本判据说"空"就是空——不看发送键。
        #
        # 为什么需要这一条：deepseek 有草稿时发送键本来就是深蓝，粘失败留下的
        # "提问前 3 个字"于是被旧判据当成用户草稿，之后每次 draft_present
        # （真机连挂 3 次）。反过来不会更严：文本判非空或读不出来时，仍旧落回
        # 下面的按钮判据，其它端行为不变。
        if site.empty_by_text:
            try:
                from .conversation import composer_empty
                if composer_empty(view, placeholders=site.placeholders):
                    return True
            except Exception:                          # noqa: BLE001 - 读不出来就走老路
                pass
        if any(n.kind == "ButtonControl" and n.name in SEND and n.control.IsEnabled
               for n in view.root.walk()):
            return False
        m = s.find(site.send, threshold=limit, region=_win(s, site))
        if m is None:
            return True
        return not _send_enabled(s, site, m)

    return probe


# ---------------------------------------------------------------- 草稿（CARD-09e 第 4 条）
def composer_node(root, site: Site):
    """消息输入框。有 UIA 名字就按名字精确认领，否则退回 `visible_editor` 那套。

    为什么按名字：ChatGPT 的输入框叫「询问 ChatGPT」，而 Chrome 的地址栏
    也是 EditControl（在某些树里会一起被枚举出来）。名字精确相等是唯一能
    把两者分开的判据——点错到地址栏，Ctrl+A / Delete 会改掉 URL。
    """
    from .conversation import visible_editor

    if site.composer_name:
        hits = [n for n in root.walk()
                if n.kind == "EditControl" and n.name == site.composer_name
                and n.control.IsEnabled and not n.control.IsOffscreen]
        if len(hits) == 1:
            return hits[0]
    return visible_editor(root, site.name)


def looks_empty(text, editor, placeholders=()) -> bool:
    """输入框文本是不是"空的"（含占位文字这一态）。

    B19：多接一个 `placeholders`，让各端把自己那串占位文字传进来。
    gemini 的「问问 Gemini」不在公共 `KNOWN_PLACEHOLDERS` 里，于是"本来就空"会
    被判成"还有 10 字"，`clear_composer` 每次都误报 `draft_clear_failed`。
    """
    from .conversation import KNOWN_PLACEHOLDERS

    key = ui.canonical(text or "")
    if not key:
        return True
    if editor is not None and key == ui.canonical(editor.name or ""):
        return True
    known = set(KNOWN_PLACEHOLDERS) | {p for p in (placeholders or ()) if p}
    return str(text or "").strip() in known


def clear_composer(s: Screen, site: Site) -> dict:
    """把输入框清空并读回确认（CARD-09e 第 4 条）。

    ChatGPT 的「新聊天」会**保留上一条没发出去的草稿**：`--new` 之后
    `use` 一读就是 `draft_present`，整轮白跑（2026-11-02 10:11、16:08 两次）。

    顺序：按名字认领输入框 → 读一次确认不是空的 → 点进去 → Ctrl+A → Delete → 读回。
    **空的时候一下都不按**：Ctrl+A 在空 contenteditable 上会选中整页，
    紧接着的 Delete 有可能被页面当成别的快捷键。
    """
    try:
        control, _url = ui.document(s.target_hwnd, site.name)
        editor = composer_node(ui.tree(control), site)
        before = ui.edit_value(editor.control)
    except (RuntimeError, COMError, UiDocsLost) as exc:
        return {"ok": False, "stage": "composer_not_found", "hint": str(exc)}
    if looks_empty(before, editor, site.placeholders):
        return {"ok": True, "stage": "already_empty"}
    try:
        ui.click(s, editor)
        s.hotkey("ctrl", "a")
        s.press("delete")
        time.sleep(0.3)
        control, _url = ui.document(s.target_hwnd, site.name)
        editor = composer_node(ui.tree(control), site)
        after = ui.edit_value(editor.control)
    except (RuntimeError, COMError, UiDocsLost) as exc:
        return {"ok": False, "stage": "draft_clear_failed", "hint": str(exc),
                "before": str(before)[:80]}
    if not looks_empty(after, editor, site.placeholders):
        return {"ok": False, "stage": "draft_clear_failed",
                "hint": f"清空后读回仍有 {len(after)} 字",
                "before": str(before)[:80], "after": str(after)[:80]}
    return {"ok": True, "stage": "cleared", "before_len": len(before)}


def uia_answer(s: Screen, site: Site, prompt: str, result: dict, baseline: int) -> dict | None:
    """超时/被打断之后的 UIA 兜底（CARD-09e 第 3 条）。读不到返回 None。

    只在**确实可能已经答完**的情况下出手：
      · `site.uia_fallback` 打开（目前只有 ChatGPT）；
      · 结果不是"没发出去"（`submission` 是 unknown/accepted）；
      · 页面没有可点的停止键（还在生成就不交卷，半截话不能当答案）；
      · 回复动作条比发送前**多**了（否则读到的就是上一轮的回答）。
    """
    from . import lastreply
    from .conversation import STOP

    if not site.uia_fallback:
        return None
    if result.get("stage") not in ("timeout", "interrupted"):
        return None
    if result.get("submission") not in ("accepted", "unknown"):
        return None
    try:
        control, url = ui.document(s.target_hwnd, site.name)
        root = ui.tree(control)
    except (RuntimeError, COMError, UiDocsLost):
        return None
    if any(n.kind == "ButtonControl" and n.name in STOP and n.control.IsEnabled
           for n in root.walk()):
        return None
    if lastreply.count(root) <= baseline:
        return None
    text, _node = lastreply.last_answer(root, prompt)
    if not text:
        return None
    return {**result, "ok": True, "stage": "answered", "reply": text,
            "read_via": "uia", "basis": "uia_last_reply_marker",
            "done_marker": lastreply.done_signal(root), "url": url,
            "fallback_from": result.get("stage")}


def ask(site: Site, prompt: str, *, mode: str = "new", timeout: float = 180.0,
        verbose: bool = True, tab: int | None = None, prepare=None,
        purpose: str | None = None, reread=None) -> dict:
    """prepare(s) -> dict：新对话之后、写输入之前要做的准备（比如切任务模式）。

    在新对话页明确设置写入器，再写入提问。

    `purpose` 给了就在**写入文本之后、点发送之前**过档位闸（CARD-06b 第 3 条）：
    校验回执 → 只读复核（读不到就拒发）→ 原子消费 → 再比一次页面指纹。
    没过就返回 stage=tier_unconfirmed（退出码 5），一个键都没敲。
    """
    from . import failure, human, sendgate
    from . import lastreply
    from . import zoom as zoom_mod
    from .conversation import Conversation, ask as send_and_receive

    started = time.monotonic()
    screen = Screen(verbose=verbose)
    result: dict = {"ok": False, "submission": "not_sent", "sent": False}
    opened: dict = {}
    hwnd = ensure_site(site.name, tab=tab, continue_session=mode == "continue")
    if hwnd is None:
        return {**result, "stage": "no_window"}
    screen.bind(hwnd)
    if not screen.focus():
        return {**result, "stage": "page_not_front"}
    screen.tab_site = site.name
    try:
        zoom_mod.ensure(screen, verbose=verbose)
        if mode == "new":
            opened = _open_new_chat(screen, site)
            if not opened["ok"]:
                return {**opened, "submission": "not_sent", "sent": False}
            # 第 4 条：新对话可能把上一条草稿带过来，有就清掉（读回为空才算清掉）
            if site.clear_draft_on_new:
                cleared = clear_composer(screen, site)
                if not cleared["ok"]:
                    return {"ok": False, "stage": "draft_clear_failed",
                            "submission": "not_sent", "sent": False,
                            "detail": cleared,
                            "hint": cleared.get("hint", "新对话后清不掉残留草稿")}
                opened["draft"] = cleared.get("stage")
        if prepare is not None:
            prepared = prepare(screen)
            if not prepared["ok"]:
                return {"ok": False, "stage": "prepare_failed", "prepare": prepared,
                        "submission": "not_sent", "sent": False}
        door = None
        if purpose is not None:
            def door():
                return sendgate.pre_send(screen, site.name, purpose, reread=reread)
        images = (site.actions, site.copy) if site.screen_reply else None
        # 第 3 条的归属基线：发送前树里有几条回复动作条
        baseline = 0
        watch = None
        if site.uia_fallback:
            try:
                control, _url = ui.document(hwnd, site.name)
                baseline = lastreply.count(ui.tree(control))
            except (RuntimeError, COMError, UiDocsLost):
                baseline = 0
            watch = _watch_for(lastreply, prompt, baseline)
        result = send_and_receive(
            Conversation(screen, site.name, empty=_empty_probe(screen, site),
                         images=images,
                         # B19：两个开关默认关，只有 deepseek 打开
                         write_verify=site.write_verify,
                         adopt_self_draft=site.adopt_self_draft,
                         paste_chunk=site.paste_chunk), prompt, timeout, pre_send=door,
            plain=site.paste_plain, watch=watch,
            # T2-1：把用途透传下去，`conversation.ask` 据此决定是否接提交账本。
            # 不给 purpose 就等于不记账（老行为）；chatgpt/deepseek 走这条路。
            purpose=purpose)
        if not result.get("ok"):
            fallback = uia_answer(screen, site, prompt, result, baseline)
            if fallback is not None:
                result = fallback
    except zoom_mod.ZoomNot100 as exc:
        result = {**exc.as_result(), "submission": "not_sent", "sent": False}
    except (human.HumanCheck, tier_guard.TierUnconfirmed) as exc:
        result = {**exc.as_result(), "submission": "not_sent", "sent": False}
    except WindowMinimized as exc:
        result = {**result, "stage": "window_minimized", "hint": str(exc)}
    except UiDocsLost as exc:
        result = {**exc.as_result(), "submission": "not_sent", "sent": False}
    except ForegroundLost as exc:
        result = {**result, "stage": "user_took_over", "hint": str(exc)}
    result["elapsed"] = round(time.monotonic() - started, 1)
    result["mode"] = mode
    if opened.get("draft") and mode == "new":
        # 第 4 条：把"新对话带过来的草稿清掉了"写进结果，证据里能直接查到
        result["draft_on_new"] = opened["draft"]
    if not result.get("ok"):
        failure.record(site.name, result, screen=screen, hwnd=hwnd)
    return result


def _watch_for(lastreply, prompt: str, baseline: int):
    """等回复时每轮看一眼页面标记，答完了就提前交卷（CARD-09e 第 3 条）。

    为什么需要它：ChatGPT 把长提问折叠了，共享判据认领不到「我们这条提问」，
    于是一直等到超时（2026-11-02 16:07 那轮实测 241.8 秒才由兜底救回来）。
    页面自己在生成结束时挂出「回答已完成」，没理由不用。

    守住三条，缺一不可：
      · 停止键不在（还在生成就不交卷，半截话不能当答案）；
      · 回复动作条比发送前多了（多出来的才是这一轮）；
      · 取到的正文**连续两轮完全一样**（防"正在打字"的中间态）。
    """
    seen = {"key": None, "count": 0}

    def watch(view):
        if view.busy or lastreply.count(view.root) <= baseline:
            seen["key"], seen["count"] = None, 0
            return None
        text, _node = lastreply.last_answer(view.root, prompt)
        if not text:
            seen["key"], seen["count"] = None, 0
            return None
        key = ("uia", text)
        if key != seen["key"]:
            seen["key"], seen["count"] = key, 1
            return None
        seen["count"] += 1
        if seen["count"] < 2:
            return None
        return {"ok": True, "stage": "answered", "reply": text, "submission": "accepted",
                "sent": True, "read_via": "uia", "basis": "uia_last_reply_marker",
                "done_marker": lastreply.done_signal(view.root), "early": True}

    return watch


def read_last(site: Site, *, tab: int | None = None, verbose: bool = True) -> dict:
    """只读最后一条回复，不发送、不点任何控件（CARD-09e 第 3 条）。

    输出沿用 `gemini_use --read` / `grok_use --read` 的形状：`stage=read` + `reply`。
    """
    from . import lastreply

    screen = Screen(verbose=verbose)
    hwnd = ensure_site(site.name, tab=tab)
    if hwnd is None:
        return {"ok": False, "stage": "no_window", "submission": None, "sent": None}
    screen.bind(hwnd)
    if not screen.focus():
        return {"ok": False, "stage": "page_not_front", "submission": None, "sent": None}
    screen.tab_site = site.name
    try:
        control, url = ui.document(hwnd, site.name)
        root = ui.tree(control)
    except UiDocsLost as exc:
        return {**exc.as_result(), "submission": None, "sent": None}
    except (RuntimeError, COMError) as exc:
        return {"ok": False, "stage": "interrupted", "hint": str(exc),
                "submission": None, "sent": None}
    text, _node = lastreply.last_answer(root, "")
    if not text:
        return {"ok": False, "stage": "no_reply", "submission": None, "sent": None,
                "hint": "控件树里没有带动作条的回复；可能这一页还没有回答",
                "url": url, "done_marker": lastreply.done_signal(root)}
    return {"ok": True, "stage": "read", "submission": None, "sent": None,
            "reply": text, "url": url, "read_via": "uia",
            "basis": "uia_last_reply_marker", "done_marker": lastreply.done_signal(root)}


def cli(site: Site, argv: list[str], *, prepare=None, extra_help: str = "",
        reread=None) -> int:
    import argparse
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    parser = argparse.ArgumentParser(prog=f"{site.name}_use.py", epilog=extra_help +
                                     "\n  --purpose   test=最低档 / review|accept=最高档"
                                     "（发送必填，无默认；要先用 *_choose 选出该档）")
    parser.add_argument("prompt", nargs="?", help="完整提问，可包含换行")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--new", action="store_true", help="新对话（默认）")
    modes.add_argument("--continue", dest="continuing", action="store_true")
    # 第 3 条：只读最后一条回复，不发送（和 gemini_use / grok_use 的 --read 同形）
    parser.add_argument("--read", action="store_true",
                        help="只读当前标签页最后一条回复，不发送")
    parser.add_argument("--purpose", choices=tier_guard.PURPOSES, default=None,
                        help="test=最低档，review/accept=最高档（必填，无默认）")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    args = parser.parse_args(argv)
    if args.read:
        if not site.uia_fallback:
            parser.error(f"本端不支持 --read：{site.name} 还没有「按页面标记取最后一条回复」的判据")
        if args.prompt and args.prompt.strip():
            parser.error("--read 是只读，不接受提问")
        if not guard.guard(need, wait, force):
            return ec.BUSY
        from . import runlog
        result = read_last(site, tab=args.tab)
        runlog.record(site.name, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else ec.code_for(result.get("stage"))
    if not args.prompt or not args.prompt.strip() or args.timeout <= 0:
        parser.error("提问不能为空，--timeout 必须大于 0")
    if not args.purpose:
        parser.error("the following arguments are required: --purpose（test/review/accept，无默认）")
    # 第 6 条：高档（review/accept）等待上限下限 900s，不得 300s 就判超时
    args.timeout = tier_guard.floor_timeout(args.purpose, args.timeout)
    if not guard.guard(need, wait, force):
        return ec.BUSY
    try:
        result = ask(site, args.prompt, mode="continue" if args.continuing else "new",
                     timeout=args.timeout, tab=args.tab, prepare=prepare,
                     purpose=args.purpose, reread=reread)
    except WindowMinimized as exc:
        result = {"ok": False, "stage": "window_minimized", "submission": "not_sent",
                  "sent": False, "hint": str(exc)}
    except UiDocsLost as exc:
        result = {**exc.as_result(), "submission": "not_sent", "sent": False}
    except RuntimeError as exc:
        result = {"ok": False, "stage": "preparation_failed", "submission": "not_sent",
                  "sent": False, "hint": str(exc)}
    from . import runlog
    if not result.get("ok") and not result.get("evidence"):
        from . import failure
        failure.record(site.name, result)
    runlog.record(site.name, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else ec.code_for(result.get("stage"))
