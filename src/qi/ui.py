"""Named UI controls for binding, input and reply ownership.

Coordinates come only from the currently bound control. No image/position fallback.

2026-11-01 CARD-WEB：**"URL 读不到"不等于"这个窗口没有文档"。**
实测（本机 Chrome 132 / OpenCode / WorkBuddy）：
  · Chrome 窗口在后台时，页面 DocumentControl 照样存在，但 ValuePattern 的值是空串；
    提到前台立刻变成 `https://gemini.google.com/app`。
  · 同理 OpenCode 后台是 ''，前台是 `oc://renderer/index.html`。
旧实现把空值当成"没有文档"直接跳过，于是绑定报「目标文档数量为 0」——
巡检里 gemini/grok 的 no_window、opencode --new 的失败都是这一个原因。
现在：有 ValuePattern 的 DocumentControl 一律算候选，URL 空串表示"未知"；
只有一个候选且 URL 未知时按它绑定（并在失败时把诊断信息带出去）。

2026-11-02 CARD-06a (b)：**"读到 0 个文档"是一次性的可恢复故障，不是终局。**
实测（WorkBuddy 空闲一夜、OpenCode 后台放久）：
  · 本进程这一侧的 UIA 会话会失效，重建客户端就好（`reset_client`）；
  · Electron/Chromium 的渲染进程休眠后，光读缓存根节点唤醒不了 AXTree，
    必须先 `SetForegroundWindow` 拿真焦点，再 `NotifyWinEvent` + 根节点 `FindAll`
    触发重建；必要时鼠标在窗口内小幅移动一下。
所以 `candidates()` 现在是"最多 3 轮、每轮间隔 2 秒、总限时"的唤醒循环：
每轮**重新取 Root**、不复用上一轮的元素、每轮写一行 runlog。
仍然读到 0 个文档 → 抛 `UiDocsLost`（stage=uia_docs_lost / 退出码 6），
**不杀进程、不自动重启**——重启是用户的事，脚本只如实报告。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urlsplit

import uiautomation as auto
from uiautomation.uiautomation import _AutomationClient

from . import exit_codes as ec
from . import uilock

user32 = ctypes.windll.user32

SW_RESTORE = 9

# 桌面端渲染文档可能出现的 URL scheme（Codex=app、OpenCode=oc、WorkBuddy=file …）
DESKTOP_SCHEMES = ("file", "oc", "dsh-app", "app")

# UIA 唤醒策略（CARD-06a (b)）：最多 3 轮、每轮间隔 2 秒、总限时 8 秒
MAX_ROUNDS = 3
ROUND_INTERVAL = 2.0
TOTAL_LIMIT = 8.0

# 最近一次 candidates() 的每轮记录（诊断用；测试也读它）
LAST_ROUNDS: list[dict] = []


class UiDocsLost(RuntimeError):
    """UIA 读不到任何文档（已唤醒重试）。CLI 层转成 stage=uia_docs_lost / 退出码 6。"""

    stage = "uia_docs_lost"
    code = ec.UIA_DOCS_LOST

    def __init__(self, message: str, rounds: list | None = None, facts: str = ""):
        super().__init__(message)
        self.rounds = list(rounds or [])
        self.facts = facts

    def as_result(self) -> dict:
        return {"ok": False, "stage": self.stage, "code": self.code,
                "hint": str(self), "rounds": self.rounds}



def _declare_win32():
    """声明句柄参数类型：64 位句柄可能大于 2^31，不声明会抛
    `OverflowError: int too long to convert`（2026-11-01 同批修掉的）。"""
    from ctypes import POINTER, c_int

    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    user32.GetWindowTextW.restype = c_int
    user32.GetForegroundWindow.restype = wintypes.HWND


_declare_win32()


@dataclass(eq=False)
class Node:
    kind: str
    name: str
    control: object = None
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @cached_property
    def key(self):
        return tuple(self.control.GetRuntimeId())

    @cached_property
    def enabled(self):
        return bool(self.control.IsEnabled)

    @cached_property
    def offscreen(self):
        return bool(self.control.IsOffscreen)


def tree(control):
    """Fetch one UIA snapshot instead of dereferencing old nodes after a render."""
    client = _AutomationClient.instance().IUIAutomation
    request = client.CreateCacheRequest()
    request.TreeScope = 7  # TreeScope_Subtree
    request.TreeFilter = client.RawViewCondition
    for prop in (auto.PropertyId.ControlTypeProperty, auto.PropertyId.NameProperty,
                 auto.PropertyId.RuntimeIdProperty, auto.PropertyId.IsEnabledProperty,
                 auto.PropertyId.IsOffscreenProperty):
        request.AddProperty(prop)

    def build(element, parent=None):
        kind = auto.ControlTypeNames[element.CachedControlType]
        node = Node(kind, element.CachedName, getattr(auto, kind)(element=element), parent)
        node.key = tuple(element.GetCachedPropertyValue(auto.PropertyId.RuntimeIdProperty))
        node.enabled = bool(element.CachedIsEnabled)
        node.offscreen = bool(element.CachedIsOffscreen)
        children = element.GetCachedChildren()
        if children and node.kind != "MenuBarControl":
            node.children = [build(children.GetElement(i), node) for i in range(children.Length)]
        return node

    return build(control.Element.BuildUpdatedCache(request))


# ---------------------------------------------------------------- 窗口诊断
def _title(hwnd) -> str:
    try:
        n = user32.GetWindowTextLengthW(int(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(int(hwnd), buf, n + 1)
        return buf.value
    except Exception:                       # noqa: BLE001 - 诊断信息不许把主流程带崩
        return ""


def title_of(hwnd) -> str:
    """窗口标题（公开版）。人机验证/错误横幅的标题也算一条证据。"""
    return _title(hwnd) if hwnd else ""


def window_facts(hwnd) -> str:
    """一行窗口现状，专给失败信息用（排查时不用再去猜窗口在哪）。"""
    try:
        h = int(hwnd)
        if not user32.IsWindow(h):
            return f"hwnd={h} 窗口已不存在"
        foreground = int(user32.GetForegroundWindow()) == h
        iconic = bool(user32.IsIconic(h))
        return (f"hwnd={h} 标题={_title(h)!r} "
                f"前台={'是' if foreground else '否'} "
                f"最小化={'是' if iconic else '否'}")
    except Exception as exc:                # noqa: BLE001
        return f"hwnd={hwnd} 读窗口信息失败（{type(exc).__name__}: {exc}）"


# ---------------------------------------------------------------- 文档
def documents(hwnd):
    """窗口里的内容文档：[(控件, URL)]。

    URL 可能是空串——**那是"这个窗口不在前台，Chrome/Electron 没给出地址"，
    不是"没有这个文档"**。只有拿到 ValuePattern 的才算文档；纯外壳
    DocumentControl（Chrome 里一层套一层那种，没有 ValuePattern）继续往下找。
    """
    out = []

    def visit(control):
        if control.ControlTypeName == "DocumentControl":
            pattern = control.GetValuePattern()
            if pattern is not None:
                try:
                    out.append((control, pattern.Value or ""))
                except Exception:           # noqa: BLE001 - 元素失效：按 URL 未知收下
                    out.append((control, ""))
                return
        for child in control.GetChildren():
            visit(child)

    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return []
    visit(root)
    return out


def reset_client() -> None:
    """丢掉缓存的 UIA 客户端，下次调用重建。

    2026-11-01 巡检：WorkBuddy 长时间空闲后读不到文档（目标文档数量为 0），
    重启 WorkBuddy 才恢复。那是本进程这一侧的 UIA 会话放久了失效，
    重建客户端就能恢复，不必动目标应用。
    """
    try:
        _AutomationClient._instance = None
    except Exception:                       # noqa: BLE001 - 清不掉也不能影响读取
        pass


def _declare_wake() -> None:
    """唤醒用到的几个 Win32 入口。句柄一律声明成 HWND：
    64 位句柄可能大于 2^31，不声明会抛 `OverflowError: int too long to convert`。"""
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.BringWindowToTop.argtypes, user32.BringWindowToTop.restype = [wintypes.HWND], wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.NotifyWinEvent.argtypes = [wintypes.DWORD, wintypes.HWND, ctypes.c_long, ctypes.c_long]
    user32.NotifyWinEvent.restype = None
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL


_declare_wake()

EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_REORDER = 0x8004
OBJID_WINDOW = 0
OBJID_CLIENT = -4
CHILDID_SELF = 0


def activate(hwnd) -> bool:
    """把窗口提到前台，**拿真焦点**。

    为什么不做成"能省就省"：Chromium 的渲染进程休眠后，UIA 读缓存根节点
    拿不到 AXTree，必须窗口真的到前台才会重建。SetForegroundWindow 会被
    前台锁拒（返回 0 但不报错），所以附加到前台线程再设一次。
    """
    h = int(hwnd)
    try:
        if int(user32.GetForegroundWindow()) == h:
            return True
        fg = int(user32.GetForegroundWindow() or 0)
        tid_cur = int(ctypes.windll.kernel32.GetCurrentThreadId())
        tid_fg = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        if tid_fg and tid_fg != tid_cur:
            attached = bool(user32.AttachThreadInput(tid_cur, tid_fg, True))
        try:
            user32.SetForegroundWindow(h)
            user32.BringWindowToTop(h)
        finally:
            if attached:
                user32.AttachThreadInput(tid_cur, tid_fg, False)
        time.sleep(0.2)
        return int(user32.GetForegroundWindow()) == h
    except Exception:                       # noqa: BLE001 - 唤醒失败不算致命，后面还有别的招
        return False


def notify(hwnd) -> bool:
    """广播两条 WinEvent，催渲染进程重建可访问性树。返回值只是"调用没炸"。"""
    try:
        h = int(hwnd)
        user32.NotifyWinEvent(EVENT_OBJECT_SHOW, h, OBJID_WINDOW, CHILDID_SELF)
        user32.NotifyWinEvent(EVENT_OBJECT_REORDER, h, OBJID_CLIENT, CHILDID_SELF)
        return True
    except Exception:                       # noqa: BLE001
        return False


def findall(hwnd) -> int:
    """在**新取的根节点**上做一次 FindAll（UIA 只在有查询时才会去拉 AXTree）。

    只用来触发重建，返回命中的 DocumentControl 数量（诊断用）。
    """
    try:
        root = auto.ControlFromHandle(int(hwnd))
        if root is None:
            return 0
        found = root.FindAll(searchDepth=8, ControlType=auto.ControlType.DocumentControl)
        return len(found)
    except Exception:                       # noqa: BLE001
        return 0


def nudge(hwnd) -> bool:
    """鼠标在窗口内小幅移动一下（并尽量挪回原处）。

    有些渲染进程要"收到一次真实鼠标事件"才肯重建 AXTree。只在窗口内动，
    不点任何东西；窗口矩形不可用时不动（不看 -32000 哨兵，交给 window 模块）。
    """
    from . import window as window_mod
    rect = window_mod.restored_rect(hwnd)
    if not rect:
        return False
    try:
        x, y, w, h = rect
        start = wintypes.POINT()
        had = bool(user32.GetCursorPos(ctypes.byref(start)))
        px, py = x + min(60, max(10, w // 2)), y + min(40, max(10, h // 4))
        user32.SetCursorPos(px, py)
        time.sleep(0.05)
        user32.SetCursorPos(px + 6, py + 4)
        time.sleep(0.05)
        if had:
            user32.SetCursorPos(start.x, start.y)
        return True
    except Exception:                       # noqa: BLE001
        return False


def msaa_poke(hwnd) -> bool:
    """向窗口要一次 MSAA 对象（`AccessibleObjectFromWindow(hwnd, OBJID_CLIENT)`）。

    **这是真机上唯一确实把 Chromium 可访问性打开的那一步。**
    2026-11-02 实测 WorkBuddy：整棵树只有 8 个空 PaneControl、0 个 DocumentControl，
    `NotifyWinEvent` / `FindAll` / 挪鼠标跑了 4 轮都没用；调用本函数之后过一会儿再读，
    DocumentControl 就出现了（随后 `workbuddy_use --list` 一次读出
    `DeepSeek-V4.1-Flash`）。原理：Chromium 只在"检测到辅助技术客户端"时才构建
    AXTree，而 MSAA 的 `AccessibleObjectFromWindow` 就是那个检测信号。

    只读：拿一个 IAccessible 指针就丢掉，不点、不改任何状态。
    """
    try:
        import ctypes
        from ctypes import byref, c_void_p, wintypes

        oleacc = ctypes.windll.oleacc
        ole32 = ctypes.windll.ole32

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        iid = GUID()
        # {618736E0-3C3D-11CF-810C-00AA00389B71} = IID_IAccessible
        if ole32.CLSIDFromString(ctypes.c_wchar_p("{618736E0-3C3D-11CF-810C-00AA00389B71}"),
                                 byref(iid)) != 0:
            return False
        oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                      c_void_p, ctypes.POINTER(c_void_p)]
        oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long
        ptr = c_void_p()
        rc = oleacc.AccessibleObjectFromWindow(int(hwnd), 0xFFFFFFFC,   # OBJID_CLIENT
                                               ctypes.cast(byref(iid), c_void_p),
                                               byref(ptr))
        return rc == 0 and bool(ptr)
    except Exception:                       # noqa: BLE001 - 拿不到就当没这一步
        return False


def _recover(hwnd) -> dict:
    """读到 0 个文档时的**一整轮**自愈。

    五件事按顺序做，任何一步失败都不影响后面的：
      1. 最小化就先恢复（最小化的窗口读不到 AXTree）；
      2. 重建本进程这一侧的 UIA 客户端（放久的会话会失效）；
      3. 提到前台 + NotifyWinEvent（让渲染进程知道"有人要看你的树"）；
      4. 在新根节点上 FindAll 一次 + 向窗口要一次 MSAA 对象（Chromium 的开关）；
      5. 鼠标在窗口内小幅移动。
    返回 facts，写进每轮日志里——失败时看日志就知道是哪一步没成。
    """
    facts = {"iconic": False, "reset": False, "activated": False,
             "notified": False, "findall": 0, "msaa": False, "nudged": False,
             "window": False}
    real = False
    try:
        real = bool(user32.IsWindow(int(hwnd)))
    except Exception:                       # noqa: BLE001 - 假句柄（离线测试）走静态分支
        real = False
    facts["window"] = real

    from . import window as window_mod
    if real:
        try:
            facts["iconic"] = bool(window_mod.is_minimized(hwnd))
            if facts["iconic"]:
                window_mod.restore(hwnd)
        except Exception:                   # noqa: BLE001
            pass
    reset_client()
    facts["reset"] = True
    if real:
        facts["activated"] = bool(activate(hwnd))
        facts["notified"] = bool(notify(hwnd))
        facts["findall"] = int(findall(hwnd))
        facts["msaa"] = bool(msaa_poke(hwnd))
        facts["nudged"] = bool(nudge(hwnd))
    time.sleep(0.2)
    return facts


def wake_enabled() -> bool:
    """并发模式下不许抢前台（`BRIDGE_UI_WAKE=0`）。

    为什么：`_recover` 会 `activate()` + 动鼠标。4 个端同时跑时会互相把对方
    挤到后台 —— A 抢回来、B 变后台、B 再抢回来……每秒都在切前台，还白烧 8 秒。
    `wake` 只在"读到 0 个文档"时才起作用，默认开着，单跑行为不变。
    """
    return os.environ.get("BRIDGE_UI_WAKE", "1") != "0"


def candidates(hwnd, attempts: int = MAX_ROUNDS, *, site: str | None = None,
               read=None, sleep=None, clock=None, wake=None) -> list:
    """带唤醒循环的文档读取（CARD-06a (b)）。

    顺序：
      · **先直接读一次**（正常路径零打扰，不提前台、不动鼠标）；
      · 有文档但 URL 全是空串 → 隔 0.3s 重读一次（accessibility 树首次访问的延迟）；
      · 一个文档都没有 → 进入唤醒循环：最多 `attempts` 轮、每轮间隔 2 秒、
        总耗时不超过 `TOTAL_LIMIT`；每轮重新取 Root、不复用旧元素、写一行 runlog。

    `read` / `sleep` / `clock` 可注入，离线测试靠它们覆盖"第 N 轮才读到 / 一直读不到"。
    仍然 0 个文档时返回 []（要不要报错由 `document()` 决定）。
    """
    read = read or documents
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    # 直接往模块级的 LAST_ROUNDS 里写：`_last[:] = records` 只复制元素，
    # 之后往 records 里 append 是追加到另一个列表上，外面读到的永远是空的。
    LAST_ROUNDS.clear()
    records = LAST_ROUNDS
    started = clock()

    def one_read(index: int, kind: str, facts: dict | None = None) -> list:
        error = None
        try:
            found = list(read(hwnd) or [])
        except Exception as exc:            # noqa: BLE001 - 元素失效/COM 抽风：记下来继续
            found, error = [], f"{type(exc).__name__}: {exc}"
        record = {"round": index, "kind": kind, "docs": len(found),
                  "urls": sum(1 for _c, url in found if url),
                  "elapsed": round(clock() - started, 2), "error": error}
        if facts:
            record["wake"] = facts
        records.append(record)
        if site:
            from . import runlog
            runlog.record_probe(site, round=index, rounds=max(1, attempts),
                                docs=record["docs"], urls=record["urls"],
                                elapsed=record["elapsed"],
                                note=error or ("" if found else "no_document_control"))
        return found

    found = one_read(0, "warm")
    if any(url for _c, url in found):
        return found
    if found:
        sleep(0.3)
        again = one_read(0, "warm_retry")
        return again or found

    for index in range(1, max(1, attempts) + 1):
        if clock() - started >= TOTAL_LIMIT:
            records.append({"round": index, "kind": "skipped", "reason": "total_limit",
                            "elapsed": round(clock() - started, 2)})
            break
        if index > 1:
            sleep(min(ROUND_INTERVAL, max(0.0, TOTAL_LIMIT - (clock() - started))))
            if clock() - started >= TOTAL_LIMIT:      # 睡醒就超时了，别再折腾一轮
                records.append({"round": index, "kind": "skipped",
                                "reason": "total_limit",
                                "elapsed": round(clock() - started, 2)})
                break
        if wake is None:
            wake = wake_enabled()
        if not wake:
            records.append({"round": index, "kind": "skipped", "reason": "wake_off"})
            break
        facts = _recover(hwnd)
        found = one_read(index, "wake", facts)
        if found:
            return found
    return found


def _pick(site, docs):
    """从候选里挑出本站点的那一个；挑不出来返回 None（不猜）。"""
    known = [(control, url) for control, url in docs if url]
    unknown = [(control, url) for control, url in docs if not url]

    if site in HOSTS:
        host = HOSTS[site]
        matched = [(control, url) for control, url in known
                   if urlsplit(url).hostname == host]
        if len(matched) == 1:
            return matched[0]
        # URL 全读不到但窗口里只有一个文档：那就是当前标签页，按它绑。
        if not known and len(unknown) == 1:
            return unknown[0]
        return None

    # 桌面端（WorkBuddy / OpenCode / dsh / Codex）：一个窗口只有一个渲染文档。
    if len(docs) == 1:
        return docs[0]
    matched = [(control, url) for control, url in known
               if urlsplit(url).scheme in DESKTOP_SCHEMES]
    return matched[0] if len(matched) == 1 else None


def _found(docs) -> str:
    """候选清单，一行说清每个文档的名字和 URL。"""
    if not docs:
        return "没有可用的 DocumentControl"
    parts = []
    for index, (control, url) in enumerate(docs, 1):
        try:
            name = control.Name
        except Exception:                   # noqa: BLE001
            name = "(名读取失败)"
        parts.append(f"#{index} name={name!r} url={url or '(URL 未知)'}")
    return "；".join(parts)


HOSTS = {"chatgpt": "chatgpt.com", "deepseek": "chat.deepseek.com",
         "gemini": "gemini.google.com", "grok": "grok.com"}


def document(hwnd, site, *, wake=None):
    """绑定本站点的文档控件，返回 (控件, URL)。

    **tabs 模式下这一整段要进锁**：切标签和读文档必须是原子的，
    否则"切过去 → 别人又切走 → 读到别人的页面"（2026-11-08 真机：
    gemini 的 `open_new_chat` 就栽在这上面，报"读到 1 个文档 url=chat.deepseek.com"）。
    单跑 / per_site 模式不加锁，行为与今天逐字节一致。
    """
    from . import browser, uilock as _uilock

    if browser.window_mode() != "tabs":
        return _document_plain(hwnd, site, wake=wake)
    with _uilock.section(f"doc:{site}", timeout=_uilock.SECTION_TIMEOUT):
        browser.acquire_tab(site, hwnd)
        return _document_plain(hwnd, site, wake=wake)

def _document_plain(hwnd, site, *, wake=None):
    """绑定本站点的文档控件，返回 (控件, URL)。URL 空串表示"读不到但确实绑上了"。

    失败时的报错要能直接看出发生了什么：窗口在不在前台、最小化没有、
    到底读到了几个文档、每个文档的 URL 是什么。巡检只看得到这一行输出。

    **一个文档都读不到**（已唤醒重试 3 轮）→ 抛 `UiDocsLost`（退出码 6）：
    不杀进程、不重启客户端，只如实报告试了几轮、每轮看到了什么。
    """
    docs = candidates(hwnd, site=site, wake=wake)
    picked = _pick(site, docs)
    if picked is not None:
        return picked
    same_site = [d for d in docs if d[1] and urlsplit(d[1]).hostname == HOSTS.get(site)]
    count = len(same_site) if site in HOSTS else len(docs)
    message = (f"{site}: 目标文档数量为 {count}，无法绑定"
               f"（{window_facts(hwnd)}；读到 {len(docs)} 个文档：{_found(docs)}）")
    if not docs:
        rounds = list(LAST_ROUNDS)
        tried = "；".join(f"#{r.get('round')}{r.get('kind')} docs={r.get('docs')}"
                          f" urls={r.get('urls')}" for r in rounds) or "没有记录"
        raise UiDocsLost(
            f"{message}；已唤醒重试 {MAX_ROUNDS} 轮仍为 0（{tried}）",
            rounds=rounds, facts=window_facts(hwnd))
    raise RuntimeError(message)



def text_of(node):
    """Leaf text only: UIA often repeats a heading's text in its child."""
    if node.kind in ("ButtonControl", "EditControl", "ComboBoxControl", "MenuControl"):
        return ""
    if not node.children:
        return node.name if node.kind in ("TextControl", "HyperlinkControl") else ""
    return "\n".join(part for c in node.children if (part := text_of(c)))


def canonical(text):
    return "".join(text.replace("\u200b", "").replace("\ufeff", "").split())


def edit_value(control):
    value = control.GetPattern(auto.PatternId.ValuePattern)
    if value:
        return value.Value
    text = control.GetPattern(auto.PatternId.TextPattern)
    if text:
        return text.DocumentRange.GetText(-1)
    raise RuntimeError("输入控件没有可读的文本模式")


def unique(nodes, description):
    nodes = list(nodes)
    if len(nodes) != 1:
        raise RuntimeError(f"{description}: 找到 {len(nodes)} 个控件")
    return nodes[0]


def click(screen, node):
    rect = node.control.BoundingRectangle
    point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    if uilock.sectioned():
        # 并发时"前台"是共享资源。这里的点击大量发生在**S 段之外**（新建会话、
        # 选模型…），所以每次点击自己拿一段短锁，并先把自己的窗口抢回前台；
        # 否则别的席位一抢前台，这里就抛 ForegroundLost（真机 2026-11-08）。
        # 单跑（未开分段）保持原样：用户抢走前台时仍然立即中止，不替他抢回来。
        with uilock.section("ui:click", timeout=uilock.SECTION_TIMEOUT):
            if getattr(screen, "target_hwnd", None):
                screen.focus()
            screen.click_xy(*point)
        return
    screen.click_xy(*point)


def scroll_into_view(node) -> bool:
    """把控件滚进可视区（CARD-09f 第 2/4 条）。成功返回 True，失败返回 False。

    走 UIA 的 ScrollItemPattern.ScrollIntoView；控件不支持该 pattern（或元素
    失效）就返回 False，由调用方决定是否降级。这里只滚、不点、不抛。
    """
    control = node.control
    try:
        pattern = control.GetPattern(auto.PatternId.ScrollItemPattern)
        if pattern is not None:
            pattern.ScrollIntoView()
            return True
    except Exception:                       # noqa: BLE001 - 不支持/失效就当没滚
        pass
    return False


# 网页端「能点的东西」不止按钮。Chromium 把同一条侧栏项同时挂成
# Hyperlink + Text（Gemini 实测 `H: 发起新对话` + `T: 发起新对话`），
# 只认 ButtonControl 就会整片漏掉。按名称找控件时默认这些都算。
CLICKABLE = ("ButtonControl", "HyperlinkControl", "TextControl", "MenuItemControl")


def named(root, names, kinds=CLICKABLE) -> list:
    """按可访问性名称找控件，按文档序返回。

    **UIA 优先**的落点：先把页面上真实存在的控件按名字/角色找一遍，
    找不到才轮到截图匹配模板（模板会随缩放、主题、版本变，名字不会）。
    屏幕外的节点不算——侧栏收起时那一排链接仍在树里，但点不到。
    """
    wanted = {name for name in names}
    found = []
    for node in root.walk():
        if node.kind not in kinds or node.name not in wanted:
            continue
        try:
            if node.control is not None and node.control.IsOffscreen:
                continue
        except Exception:                   # noqa: BLE001 - 元素失效：当作不可见
            continue
        found.append(node)
    return found


# 同一个名字常常同时挂在「链接 + 文字」两种角色上（Chromium 的可访问性树如此）。
# 点链接比点文字稳，所以按角色排个优先序；同角色内保持文档序。
ROLE_ORDER = {"HyperlinkControl": 0, "ButtonControl": 1, "MenuItemControl": 2,
              "TextControl": 3}


def prefer_clickables(nodes) -> list:
    """同名的多个候选里，先点真正的可点控件（链接 > 按钮 > 菜单项 > 文字）。"""
    return sorted(nodes, key=lambda node: ROLE_ORDER.get(node.kind, 9))


def page_zoom(hwnd) -> str | None:
    """Chrome 的**页面缩放**（工具栏那个按钮的名字里就写着「缩放比例：125%」）。

    模板是在某个缩放下截的：页面缩放一变，同一个按钮的像素尺寸就跟着变，
    于是「图明明在屏幕上却匹不上」（2026-11-01：grok_send_on 最高 0.490，
    而当时 Chrome 的页面缩放是 125%）。这是找图失败最常见的原因，
    比显示器 DPI 更值得写进报错。读不到就返回 None——不猜。
    """
    if not hwnd:
        return None
    try:
        root = tree(auto.ControlFromHandle(int(hwnd)))
    except Exception:                       # noqa: BLE001 - 诊断信息不许把主流程带崩
        return None
    for node in root.walk():
        if node.kind == "ButtonControl" and node.name.startswith("缩放比例"):
            return node.name.split("：", 1)[-1].strip()
    return None


def tabs(hwnd):
    root = tree(auto.ControlFromHandle(hwnd))
    groups = [n for n in root.walk() if n.kind == "TabControl"
              and any(c.kind == "TabItemControl" for c in n.children)]
    return max(([c for c in g.children if c.kind == "TabItemControl"] for g in groups),
               key=len, default=[])
