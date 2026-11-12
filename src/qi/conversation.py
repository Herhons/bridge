"""One submission, bound to its newly observed user message and assistant reply.

2026-10-30 重写（v0.0.9 首轮真实测试暴露的问题，逐条对应）：

1. **不能用 UIA RuntimeId 当"这条消息"的标识。**
   Chrome/Electron 在重排或 SPA 换路由后会给同一个气泡换 id（实测 ChatGPT
   同一个提问节点 56888 → 56784）。原来拿 key 比对，一换就把"已经收到回复"
   误判成"会话被切走"（DeepSeek/dsh 就是这么失败的）。
   → 改成**按同文本节点出现次数**判断：发送前记下 n，看到第 n+1 个才算发出去。

2. **首轮发送时网页端会改 URL**（`/` → `/c/<id>`，四个网页端全都如此），
   而且时机不可预测——Gemini 是回答完才把会话 id 写进地址栏。
   所以地址变化**不中止也不放行**：只把"当时已经在屏幕上的动作条"划成旧回复，
   继续等新的那条。真正判"会话被切走"的是：我们这条提问从树里消失了。

3. **归属**：回复动作条必须是发送之后新出现的，且排在"我们这条提问"之后。
   两条都满足才复制；否则宁可超时，也不读别人的回答。

4. **空输入框**：contenteditable 会把自己的占位文字从 TextPattern 读出来
   （Gemini「问问 Gemini」、Grok「畅所欲问」），和控件名不是一个串，
   原样比较就成了"已有草稿"。判据：值 == 输入框里渲染出来的那个占位文本节点。

5. **元素失效**：持有控件到点击之间，页面一重绘就抛 UIA_E_ELEMENTNOTAVAILABLE
   （0x80040201，comtypes 报成 "事件无法调用任何订户"）。read/write/submit/copy
   都带一次重读重试，用户抢走前台（ForegroundLost）则立即中止，绝不重试。
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from comtypes import COMError

from . import guard, heartbeat, human, reject, sendgate, textcmp, tier_guard, ui, uilock
from .browser import TabUnbound
from .screen import ForegroundLost, Screen


class UserTookOver(ForegroundLost):
    """发送前发现用户动了键鼠：没点发送就让位。（2026-10-30 第3项）"""


class SendUnavailable(RuntimeError):
    """发送键**存在**，但一个都点不了：一个字都没发出去。

    必须与"找不到发送键"分开：找不到是脚本/UI 问题（页面改版、DOM 没渲染出来），
    "存在但全部 disabled"是端不接受输入（额度耗尽后 Send 常年是灰的，实测见
    `docs/完成矩阵.md:15`）。两者混成同一句 `找到 0 个控件`，正是当初那句
    "无法区分是脚本问题还是额度问题"的代码来源。
    """

    stage = "send_unavailable"

    def __init__(self, message: str, *, present: int = 0):
        self.present = present
        super().__init__(message)


SEND = {"发送", "发送消息", "发送提示", "提交", "Send", "Send message", "Send prompt"}
STOP = {"停止", "停止生成", "停止回答", "停止响应", "停止回复", "停止任务", "Stop",
        "Stop generating", "Stop response", "Cancel", "取消生成"}
COPY = {"Copy", "复制", "Copy response", "复制回复", "复制回答"}
RATING = {"Rate response", "Read aloud", "Good response", "Bad response", "朗读",
          "点赞", "点踩", "赞", "踩", "有帮助", "没帮助", "好回答", "差回答",
          "答得好", "答得不好", "评价回复",
          "好的回答", "有问题的回答", "差的回答"}
REGENERATE = {"重做", "重新生成", "重新生成回复", "重试", "Regenerate", "Regenerate response", "Retry"}
# 回答动作条上的同伴按钮（提问气泡上没有这些）
ACTION = RATING | REGENERATE | {"分享", "Share"}

# 实测过的占位文字：控件名和它不是一个串，结构上又找不到对应文本节点时兜底。
KNOWN_PLACEHOLDERS = {"今天帮你做些什么？@ 添加上下文，/调用技能与指令"}


@dataclass
class View:
    url: str
    root: ui.Node
    editor: ui.Node
    value: str
    busy: bool
    copies: list[ui.Node]
    #: 每个"发送"键的可用态（名字命中 `SEND` 的那些）。带默认值是为了不打断
    #: 既有按位置构造的调用方与测试。`reject.sends_blocked` 靠它区分
    #: "找不到发送键"（脚本/UI 问题）与"发送键不可用"（端不接受输入）。
    sends: tuple[bool, ...] = ()


def is_reply_copy(node):
    """回答动作条里的那个复制键。

    判据：同组里还有别的"回答动作"（赞/踩/重做/朗读/分享）。
    提问气泡上的复制键名字不同（ChatGPT「复制消息」、Gemini「复制提示」），
    而且它那一组里没有这些动作——实测 Gemini 的提问条是 [复制提示, 修改]，
    回答条是 [答得好, 答得不好, 重做, 复制, 显示更多选项]。
    """
    if node.kind != "ButtonControl" or node.name not in COPY:
        return False
    if node.name in ("Copy response", "复制回复", "复制回答"):
        return True
    group = node.parent
    for _ in range(4):
        if group is None:
            return False
        buttons = [n for c in group.children for n in c.walk() if n.kind == "ButtonControl"]
        if len(buttons) >= 2:
            peers = {n.name for n in buttons if n.name in ACTION and n.name not in COPY}
            return len(peers) >= 1
        if group.kind == "DocumentControl":
            return False
        group = group.parent
    return False


MIN_EDITOR = 40     # 宽或高小于这个像素值的可见输入框，不当成消息输入框
PREFIX_CHARS = 3    # CARD-09f 第 5 条：键入前几个字符激活编辑器，其余走粘贴
# 2026-11-09（B17）：写入后的读回要**等一拍**。
# grok 新界面把输入框换成 ComboBoxControl 之后，UIA 的 Value 比编辑器慢约 1s：
# 写完 0.15s 就读，读到的是上一帧（只有开头 PREFIX_CHARS 个字），于是把
# "已经写进去了"判成 input_mismatch（真机连续复现，见
# evidence/batch17/grok_write_probe2.txt：同一段文本 1.3s 后读回才是全文）。
# 只对"值是提问的前缀"这种滞后形态等，值完全对不上就不等——原判据不变。
READBACK_SETTLE = 3.0
READBACK_INTERVAL = 0.35
# 2026-11-09（B17）：粘贴后**不能立刻还原剪贴板**。
# grok 新界面实测：Ctrl+V 之后约 1.3s，文本才进 UIA 值（`AFTER_PASTE` 还是旧值、
# `AFTER_WAIT` 才是全文）。旧代码固定 0.35s 就还原用户剪贴板，粘贴于是落空或
# 粘到旧内容——外面看到的现象就是"只写进了前 3 个字"。改成有界轮询：编辑器
# 吃到就立刻还原，最多等这么久。
PASTE_SETTLE = 2.5
PASTE_POLL = 0.25
# 2026-11-10（B19）：长文本的粘贴确认要给够时间，而且**没落地必须报错**。
# deepseek 实测：2450 字提问只写进了前 3 个字（`你是资`）。原因是每轮确认都要
# 读一次输入框（旧实现走 `read()`，一次要截屏+多次模板匹配），2.5s 只够 2–4 轮，
# 正文常常还没进 UIA 值；而 `_paste_via_clipboard` 到点后**不抛错**，`finally`
# 立刻还原剪贴板就正常返回。于是"没写进去"被后面的 `input_mismatch` 顶了锅。
# 超过 PASTE_LONG_CHARS 的文本用更长的窗口，并且只在确认落地后才算成功。
PASTE_LONG_CHARS = 600
PASTE_SETTLE_LONG = 8.0


class WriteNotLanded(RuntimeError):
    """粘贴没落地：剪贴板写过、Ctrl+V 也发过，但编辑器里始终没有这段文字。

    与 `input_mismatch` 的区别：那个是"写完之后比对不符"，这个是"根本没写进去"。
    真机上两者要分开看，否则会把粘贴失败当成模型/页面把文字改了。
    """

    stage = "input_not_landed"

    def __init__(self, message: str, *, attempts: int = 0):
        super().__init__(message)
        self.attempts = attempts

# 2026-11-09（B17）：只复制到"提问"不等于端只会复读。
# dsh 实测：回答还在生成时，能复制的只有提问气泡上的那颗复制键，收集循环
# 一读到"稳定文本"就去点它，把 `copied_prompt` 当成终局（R4c 真机）。
# 这里给一段有界等待：复制键条数一变就重来，过了这段时间才认这个结论。
PROMPT_COPY_GRACE = 20.0
PROMPT_COPY_POLL = 0.4

# 2026-11-07（R5）：接受之后"提问从树里消失了"不等于"对话被换掉了"。
# 实测 opencode 的提问气泡在长任务跑到一半时就读不到了（回答还在生成）。
# 所以：连续这么多次读不到、且页面**不在生成中**，才认定对话真的变了。
MISSING_STABLE_READS = 5
# 接受之后允许一次"换路由"（新会话首轮发送才会发生），且必须还没出现回复。
ACCEPT_NAV_GRACE_READS = 2
# 2026-11-07（B10）：**「读不出」不等于「变了」。**
# 实测：opencode 长任务滚到底时正文污染标签带 → `proven` 从 true 掉成 false
# （`header_mismatch`），而会话标题从头到尾没变。旧代码把这种情况当成
# `session_changed`，在 13:12:52 主动放弃了本该拿到的终答复（594.6s）。
# 现在：只有"两边都读得出、且标题真的不同"才判换会话；读不出就等，
# 连续这么多次仍读不出才按 `identity_unreadable` 退出（与换会话分开记账）。
IDENTITY_UNREADABLE_READS = 10

# 2026-11-12：**用量硬墙**要快速失败，不能当"还在生成"等满整档预算。
# 实测 chatgpt Work 额度用完时页面写着「你暂时已用完工作用量 / 升级订阅或添加额度…」，
# 收卷侧看不出区别 —— 没有回复键、也不 busy，于是白等到 `REVIEW_TIMEOUT`（1500s）。
# 这不是绕过额度（一个字节都不碰），只是把"等 25 分钟"换成"当场认输"。
# 词条取**整句**而不是关键词：审查内容里出现"额度""升级"是常态，短词必误判。
QUOTA_WALL = {
    "chatgpt": ("你暂时已用完工作用量",
                "升级订阅或添加额度",
                "You've reached your limit",
                "You’ve reached your limit"),
}


def quota_wall(site, view):
    """页面是不是「用量已用完」的硬墙；命中返回那一条文案，否则 None。

    只在**没有回复键**时判：有回复就说明这一轮确实有产出，额度提示可能是别的
    残留（比如侧栏历史里的同类会话标题），不能拿它盖掉真答案。
    """
    markers = QUOTA_WALL.get(str(site or "").strip().lower())
    if not markers:
        return None
    try:
        if list(getattr(view, "copies", None) or ()):
            return None
        text = ui.text_of(view.root) or ""
    except (RuntimeError, COMError, AttributeError):
        return None
    for marker in markers:
        if marker in text:
            return marker
    return None


# 2026-11-07（R6）：**同一个文档、同一个 URL** 的 SPA 会话切换。
# opencode 的每个会话都是应用自己的一个标签页：切会话时 document runtime id 和
# URL（`oc://renderer/index.html`）都不变，变的只有应用画出来的**会话名和标签条**。
# 所以文档身份之外再收一份"会话戳"；读不到/不唯一/对不上，一律 fail-closed。
TAB_BAND_H = 48        # 文档顶端往下 48px：应用自己的标签条
HEADER_BAND_H = 96     # 48~96px：当前会话标题
#: 标签带里只认这么长的文本。实测 opencode 长任务滚到底时，正文残片会落进
#: 0~48px 这条带子（`'——与施工方自述一致。'`、`'Ran 1159 tests / OK / EXIT=0'`），
#: 于是 tabs 里混进正文 → `tabs.count(title) != 1` → `proven=False`。
#: 这是**传感器噪声**，不是会话变了。
TAB_TEXT_MAX = 60
#: 以这些字符收尾的，是句子不是标签。
_SENTENCE_TAIL = "。！？…；;.!?"


def _looks_like_tab(text) -> bool:
    """这段文字像不像应用自己的标签/标题，而不是正文残片。

    只做保守过滤（换行 / 超长 / 句子收尾），**不猜语义**：
    宁可漏掉一个真标签（那只会让 `proven` 变 false，由下面的宽限兜住），
    也不能把正文当成标签（那会让 `proven` 变成"读得出但读错"）。
    """
    value = str(text or "").strip()
    if not value or len(value) > TAB_TEXT_MAX:
        return False
    if "\n" in value or "\r" in value:
        return False
    return value[-1] not in _SENTENCE_TAIL
# 应用给新会话的占位名（还没按首条消息自动命名）。只用来允许**一次**改名。
PROVISIONAL_TITLES = {"会话", "新会话", "新对话", "新建会话", "未命名",
                      "new chat", "new session", "untitled", "untitled session"}

# 2026-11-07（R7）：标题**不是**身份。
# R6 用"会话名 + 标签条唯一"当证明，R6 自己也承认同名会话分不出来。标题可变、
# 可重复，不能拿它证明"还是同一个会话"。能证明会话的只有**与标题无关**的会话 id：
# 地址里的会话路径（Gemini `/app/<id>`、Grok `/chat/<id>`、ChatGPT `/c/<id>`）。
# 桌面端（oc://、app://、file://）没有这个结构 -> 读不到 id -> 一律 fail-closed。
SESSION_PATH_ROOTS = ("app", "chat", "c")
SESSION_ID_PLACEHOLDERS = {"", "new", "home", "index", "app", "chat"}


def session_id_of(url):
    """从地址里取**与标题无关**的会话 id；没有就返回 None（不猜、不拿标题顶替）。

    例：`https://gemini.google.com/app/abc123` -> `abc123`；
        `https://grok.com/chat/xyz` -> `xyz`；`oc://renderer/index.html` -> None。
    """
    try:
        parts = urlsplit(str(url or ""))
    except (TypeError, ValueError):
        return None
    if parts.scheme not in ("http", "https"):
        return None
    segments = [s for s in (parts.path or "").split("/") if s]
    if len(segments) < 2 or segments[0].lower() not in SESSION_PATH_ROOTS:
        return None
    sid = segments[1].strip()
    return None if sid.lower() in SESSION_ID_PLACEHOLDERS else sid


def _area(node):
    """控件面积；读不到矩形就返回 -1（多框时自然排在最后）。"""
    try:
        r = node.control.BoundingRectangle
    except (COMError, AttributeError):
        return -1
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return -1
    return w * h


def _big_enough(node):
    try:
        r = node.control.BoundingRectangle
    except (COMError, AttributeError):
        return False
    return (r.right - r.left) >= MIN_EDITOR and (r.bottom - r.top) >= MIN_EDITOR


def visible_editor(root, site):
    """消息输入框。页面里有多个可见 EditControl 时按面积挑，不再直接报错。

    2026-10-31 实测：WorkBuddy 页面上可见的 EditControl 有 **12 个**
    （侧栏和列表里的行内重命名框都是 19px 高，还有一个 30x21 的空框），
    真正的输入框是 740x64。`ui.unique` 撞上多框直接抛"找到 N 个控件"，
    workbuddy_use 连读都读不到。
    判据改成：先去掉宽或高 < 40px 的（行内小框、图标框），再取面积最大的那个；
    只有过滤后一个都不剩才报错。**单框页面行为不变**，仍然原样返回。
    """
    editors = [n for n in root.walk() if n.kind == "EditControl"
               and n.control.IsEnabled and not n.control.IsOffscreen]
    if site == "grok":
        editors += [n for n in root.walk() if n.kind == "ComboBoxControl"
                    and n.name in ("向 Grok 提任何问题", "Ask Grok anything")]
    # Search boxes have their own names and are outside the message composer.
    editors = [n for n in editors if not any(s in n.name.lower() for s in ("搜索", "search"))]
    if len(editors) > 1:
        usable = [n for n in editors if _big_enough(n)]
        if not usable:
            raise RuntimeError(f"消息输入框: 找到 0 个控件"
                               f"（另有 {len(editors)} 个可见输入框都小于 {MIN_EDITOR}px）")
        return max(usable, key=_area)
    return ui.unique(editors, "消息输入框")


def path_of(url):
    return urlsplit(url).path


class Conversation:
    def __init__(self, screen, site, empty=None, images=None, wake=None,
                 write_verify=False, adopt_self_draft=False, paste_chunk=0):
        self.screen = screen
        self.site = site
        #: 并发模式下不许抢前台（`BRIDGE_UI_WAKE=0`）；None = 按环境变量走。
        self.wake = wake
        # B19：粘贴必须确认落地（默认关，只有 deepseek 打开）。
        #   False = 旧行为：到点没确认也照常返回，交给调用方比对。
        #   True  = 到点没确认抛 WriteNotLanded，write() 会清空重试。
        self.write_verify = bool(write_verify)
        # B19：输入框里若是"本提问的前缀"，认成自己上一轮留下的残草并清掉，
        # 而不是当成用户草稿拒发。只对打开了这个开关的端生效。
        self.adopt_self_draft = bool(adopt_self_draft)
        # B19：单次粘贴的字数上限（0 = 不分块）。deepseek 的输入框实测在
        # **2000 字**：2000 能进，2001 整段被吞掉（不截断、不报错）。分两次
        # 各 1500 字粘进去则总共 3000 字都在，所以这是"单次粘贴"的上限，
        # 不是输入框总长上限。
        self.paste_chunk = int(paste_chunk or 0)
        # B15：网页端在 tabs 模式下要"每次读之前把标签切回自己"。
        # 只对 4 个网页端设（桌面端没有标签概念）；per_site 模式下 _ensure_tab 是空操作。
        if site in ui.HOSTS:
            try:
                screen.tab_site = site
            except AttributeError:
                pass                       # 假 screen 不支持就算了（测试桩）
        # 网页端传"发送键不可用即空输入框"的探针；桌面端用默认的文本判据。
        self.empty = empty or composer_empty
        # (动作条模板, 排内复制键模板)：有些端的动作条不进控件树，只能在画面上找
        self.images = images

    def read(self, attempts: int = 4, delay: float = 0.4):
        """快照一次。SPA 换路由的瞬间文档可能暂时数不出来，重读几次再看。"""
        error = None
        for i in range(attempts):
            try:
                return self._read()
            except (RuntimeError, COMError) as exc:
                error = exc
                if i + 1 < attempts:
                    time.sleep(delay)
        raise error

    def _read(self):
        # tabs 模式：先切标签再取文档（否则读到的是别的席位那一页）。
        from . import browser
        control, url = browser.acquire_document(
            self.site, self.screen.target_hwnd, wake=self.wake)
        root = ui.tree(control)
        editor = visible_editor(root, self.site)
        value = ui.edit_value(editor.control)
        busy = any(n.kind == "ButtonControl" and n.name in STOP and n.control.IsEnabled
                   for n in root.walk())
        copies = [n for n in root.walk() if is_reply_copy(n)]
        if self.site == "deepseek":
            win = self.screen.window_rect()
            busy = self.screen.find("ds_stop", threshold=0.92, region=win) is not None
            copies = []
            for row in self.screen.find_all("ds_actions", threshold=0.88, region=win):
                match = self.screen.find("ds_copy", region=(row.x, row.y, row.w, row.h))
                if match is not None:
                    x, y = match.center
                    for n in root.walk():
                        if n.kind == "ButtonControl":
                            r = n.control.BoundingRectangle
                            if r.left <= x < r.right and r.top <= y < r.bottom:
                                copies.append(n)
        # 发送键的可用态（R11）：`ui.tree` 已把 CachedIsEnabled 写进 node.enabled，
        # 读它是纯本地属性，不额外走 UIA。注意与 `busy` 的区别：`busy` 用 live
        # `IsEnabled` 判断"此刻能不能点停止"，这里只需要快照里的可用态。
        sends = tuple(bool(n.enabled) for n in root.walk()
                      if n.kind == "ButtonControl" and n.name in SEND)
        return View(url, root, editor, value, busy, copies, sends)

    def write(self, view, prompt):
        """点进输入框，键入前几个字符激活编辑器，其余走剪贴板粘贴（CARD-09f 第 5 条）。

        不再逐键键入整段提问：行首 `#`/`* `/`1. ` 会被编辑器当 Markdown 渲染掉标记，
        粘贴走 paste 事件，只插文本节点、不做 Markdown 转换。元素失效就重读一版再试。
        """
        error = None
        for attempt in range(3):
            try:
                ui.click(self.screen, view.editor)
                prefix = prompt[:PREFIX_CHARS]
                rest = prompt[PREFIX_CHARS:]
                if prefix:
                    self.screen.type_text(prefix)
                if rest:
                    # expect=整段提问：判"落地"时用它做归一化比较（编辑器会合并空行）
                    self._paste_rest(rest, prompt, attempts=attempt)
                # 打字/粘贴本身是 SendInput，也会刷新"最后输入时间"；记下打完那一刻，
                # 发送前时间变了才算用户动过（直接用 user_active 会被自己的输入误触发）
                time.sleep(0.15)
                self._input_mark = guard.last_input_tick()
                return
            except WriteNotLanded as exc:
                # B19：没落地不要立刻退出——先清空，再从空框重写一次。
                # 不清的话下一轮会在"前缀"后面再贴一遍，越写越乱。
                error = exc
                if self.write_verify:
                    try:
                        self.clear(self.read())
                    except (RuntimeError, COMError):
                        pass
                view = self.read()
            except (RuntimeError, COMError) as exc:
                error = exc
                time.sleep(0.3)
                view = self.read()
        raise error

    def _paste_rest(self, rest, prompt, *, attempts: int = 0):
        """把正文粘进去；设了 `paste_chunk` 就分块粘（每块都单独确认落地）。

        为什么分块：deepseek 单次粘贴超过 2000 字会被整段吞掉，但分块之后总量
        可以远超 2000（实测 1500+1500=3000 全在）。中间块只校验本块是否出现，
        最后一块才用整段提问做归一化比较。
        """
        size = self.paste_chunk
        if not size or len(rest) <= size:
            self._paste_via_clipboard(rest, attempts=attempts, expect=prompt)
            return
        chunks = [rest[i:i + size] for i in range(0, len(rest), size)]
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            self._paste_via_clipboard(chunk, attempts=attempts,
                                      expect=prompt if last else None)

    def read_value(self):
        """只读输入框文本，比 `read()` 轻得多。

        B19：粘贴确认要在一个几秒的窗口里轮询好几次，而 `read()` 每轮都要
        截屏 + 多次模板匹配（deepseek 长文本时 2.5s 只够 2–4 轮）。这里只取
        文档树和输入框的 Value，不做任何找图。
        """
        from . import browser

        control, _url = browser.acquire_document(
            self.site, self.screen.target_hwnd, wake=self.wake)
        root = ui.tree(control)
        editor = visible_editor(root, self.site)
        if editor is None:
            raise RuntimeError("输入框不见了")
        return ui.edit_value(editor.control) or ""

    def _clipboard_now(self):
        """读当前剪贴板；测试用的假 screen 没有这个方法时返回 None（不阻断）。"""
        try:
            return self.screen.clipboard()
        except Exception:                       # noqa: BLE001
            return None

    def _paste_via_clipboard(self, text, *, attempts: int = 0, expect=None):
        """把 text 写入剪贴板（用序列号确认确实更新）再 Ctrl+V；结束恢复原剪贴板。

        CARD-09f 第 5 条：粘贴前保存原剪贴板；写入后按剪贴板序列号确认内容真的
        更新了再 Ctrl+V；无论成败都要恢复原剪贴板（恢复失败也尽力）。
        序列号没变 = 剪贴板没更新 → 不粘贴、直接报错。

        B19：打开 `write_verify` 后，**超时没确认也算失败**（抛 `WriteNotLanded`）。
        旧行为是到点静默返回，于是"只写进前 3 个字"要等到后面的 `input_mismatch`
        才暴露，而那时已经分不清是粘贴没落地还是页面改了文本。

        `expect` 是整段提问（含前面键入的那几个字）。判"落地"用它做归一化比较，
        不是拿正文做原始子串：多行提问里的连续空行会被编辑器合并，原始子串永远
        比不中，会把"其实写进去了"误判成没落地（2285 字真机一次）。
        """
        previous = self.screen.clipboard()
        seq_before = self.screen.clipboard_seq()
        needle = (text or "").strip()
        want = (expect or "").strip() or needle
        landed = not needle
        limit = PASTE_SETTLE
        if self.write_verify and len(needle) >= PASTE_LONG_CHARS:
            limit = PASTE_SETTLE_LONG

        def arrived(value: str) -> bool:
            if not needle:
                return True
            if expect is not None:
                return textcmp.equal(want, value or "")
            return needle in (value or "")

        try:
            self.screen.set_clipboard(text)
            seq_after = self.screen.clipboard_seq()
            if (seq_before is not None and seq_after is not None
                    and seq_after == seq_before):
                raise RuntimeError("剪贴板序列号没变：剪贴板未更新，不粘贴")
            # 序列号变了不等于内容是我们的（别的进程可能同时写剪贴板）。
            # 读回逐字比一次；不一致就再写一次，仍不一致才拒发——绝不把
            # 别人的内容 Ctrl+V 进提问框。
            if not clipboard_matches(text, self._clipboard_now()):
                self.screen.set_clipboard(text)
                if not clipboard_matches(text, self._clipboard_now()):
                    raise RuntimeError("剪贴板内容与写入不一致：不粘贴")
            self.screen.hotkey("ctrl", "v")
            # 等编辑器真的吃掉这次粘贴再还原（见 PASTE_SETTLE）：还原早了等于把
            # 用户剪贴板换回去的同时把这次粘贴打断。
            deadline = time.monotonic() + limit
            while needle and time.monotonic() < deadline:
                time.sleep(PASTE_POLL)
                try:
                    value = self.read_value() if self.write_verify else (self.read().value or "")
                except (RuntimeError, COMError):
                    break
                if arrived(value):
                    landed = True
                    break
        finally:
            try:
                # **只恢复自己写进去的那一份**：粘贴期间用户可能已经复制了别的东西，
                # 无条件写回会把他的新内容冲掉（2026-11-12 会审：按归属恢复）。
                if clipboard_matches(text, self._clipboard_now()):
                    self.screen.set_clipboard(previous)
            except Exception:                # noqa: BLE001 - 恢复失败也要尽力吞掉
                pass
        if self.write_verify and not landed:
            raise WriteNotLanded(
                f"粘贴没落地：{limit:.1f}s 内输入框里没有这段文字（{len(needle)} 字）",
                attempts=attempts)

    def clear(self, view):
        """清空输入框：点进去 → Ctrl+A → Delete（CARD-09e 第 4/5 条复用）。

        B17：grok 新界面把输入框换成 `ComboBoxControl` 之后，Ctrl+A/Delete 打
        不干净（实测清完读回来还是原文，下一轮就 `draft_present`）。它同时带
        可写的 ValuePattern，所以读回非空时用它兜底一次。只在"没清掉"时才走，
        其他端一个字节不变。
        """
        ui.click(self.screen, view.editor)
        self.screen.hotkey("ctrl", "a")
        self.screen.press("delete")
        time.sleep(0.2)
        try:
            if not (self.read().value or "").strip():
                return
        except (RuntimeError, COMError):
            return
        try:
            view.editor.control.GetValuePattern().SetValue("")
            time.sleep(0.3)
        except Exception:                            # noqa: BLE001 - 没有就维持原状
            pass

    def paste(self, view, prompt):
        """点击输入框后用剪贴板贴**纯文本**（CARD-09e 第 5 条 / CARD-09f 第 5 条）。

        为什么需要它：逐字 SendInput 在 ChatGPT / Grok 的编辑器里等于"手打"，
        行首 `#`、`* `、`1. ` 会被 Markdown 自动格式化，标记当场被吃掉，
        读回来就和提问对不上（`input_mismatch`）。粘贴走的是 paste 事件，
        编辑器只插文本节点，不做 Markdown 转换。

        剪贴板用完要还原：这是用户的剪贴板，不是我们的。序列号确认 + 恢复
        都收在 `_paste_via_clipboard` 里。
        """
        error = None
        for _ in range(3):
            try:
                ui.click(self.screen, view.editor)
                self._paste_via_clipboard(prompt)
                time.sleep(0.15)
                self._input_mark = guard.last_input_tick()
                return
            except (RuntimeError, COMError) as exc:
                error = exc
                time.sleep(0.3)
                view = self.read()
        raise error

    def submit(self, view):
        error = None
        for _ in range(3):
            try:
                self._submit(view)
                return
            except COMError as exc:
                error = exc
                time.sleep(0.3)
                view = self.read()
        raise error

    def _submit(self, view):
        # 持锁期间别的席位打不了字（它们的输入也在各自的 section 里），
        # 所以这里读到的输入抖动只可能是"我们自己的输入"或"真用户"，判据仍然成立。
        if guard.user_moved_since(getattr(self, "_input_mark", None)):
            raise UserTookOver("发送前检测到键鼠输入，已让位，未点发送")
        if self.site == "deepseek":
            target = self.screen.find("ds_send_on", threshold=0.92, region=self.screen.window_rect())
            if target is None:
                raise RuntimeError("DeepSeek 发送按钮未出现")
            blue, _, red = self.screen.region_mean(target)
            if blue - red <= 100:
                raise RuntimeError("DeepSeek 发送按钮尚不可用")
            self.screen.click(target)
            return
        # 文字刚打进去时 Electron 端可能还没把发送键设成可用（实测 Codex 桌面端
        # Send 一直是 disabled），等几轮再判死，别把"慢半拍"报成"没有发送键"。
        for attempt in range(4):
            named = [n for n in view.root.walk() if n.kind == "ButtonControl"
                     and n.name in SEND]
            buttons = [n for n in named if n.control.IsEnabled]
            if len(buttons) == 1:
                ui.click(self.screen, buttons[0])
                return
            if len(buttons) > 1:
                raise RuntimeError(f"可用发送按钮: 找到 {len(buttons)} 个控件")
            if attempt == 3:
                # R11：把"找不到发送键"和"发送键不可用"分开。
                # 只有"存在但一个都点不了"才说明端不接受输入；找不到仍走原样报错。
                if named and reject.sends_blocked(
                        reject.profile(self.site),
                        reject.Facts(sends_total=len(named), sends_on=0,
                                     busy=bool(getattr(view, "busy", False)))):
                    raise SendUnavailable(
                        f"发送键存在但全部不可用（{len(named)} 个）：端不接受输入",
                        present=len(named))
                # 2026-11-12：**点不了发送**也可以是额度问题 —— 前提是页面自己说了。
                # chatgpt 的 Work 额度用完后，输入区被额度卡替换，发送键要么不存在、
                # 要么常年灰着；旧代码报 `interrupted`，账本记 unknown（BLOCKING），
                # 这条提问就被永久拦住（要人工清账），而实际上一个字都没发。
                # 有额度墙证据时归到 `send_unavailable`（可重试态），额度恢复即可重发。
                # 判据用 `not buttons`（一个能点的都没有），不是 `not named` ——
                # 实测额度墙下"发送"按钮还在，只是 disabled。
                wall = quota_wall(self.site, view)
                if wall and not buttons:
                    raise SendUnavailable(
                        f"发送键不可用（{len(named)} 个存在 / 0 个可用），"
                        f"且页面提示用量已用完：{wall}", present=len(named))
                raise RuntimeError("可用发送按钮: 找到 0 个控件")
            time.sleep(0.5)
            view = self.read()

    def copy(self, node):
        return self.copy_at(*node.center) if hasattr(node, "center") else self._copy_node(node)

    def _copy_node(self, node):
        rect = node.control.BoundingRectangle
        return self.copy_at((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

    def copy_at(self, x, y):
        # B13：复制是**整段桌面操作**（抢焦点 + 点按钮 + 改剪贴板 + 轮询）。
        # 两个端同时进来会互相踩：A 写完 marker、B 把自己的正文写进剪贴板，
        # A 的 `value != marker` 判定就会把 B 的内容当成自己的答案。
        # 所以它必须整段持短锁，而不是"点之前锁一下"。
        with uilock.section(f"copy:{self.site}"):
            return self._copy_at_locked(x, y)

    def _copy_at_locked(self, x, y):
        # 并发时点复制键前也要把自己的窗口抢回前台：否则上一个聚焦的席位
        # 会让我们抛 ForegroundLost，而这一下通常发生在"答案已经出来了"之后，
        # 白丢一整轮（2026-11-08 真机 codex：stage=user_took_over）。
        if uilock.sectioned():
            focus = getattr(self.screen, "focus", None)
            if focus is not None and getattr(self.screen, "target_hwnd", None):
                focus()
        old = self.screen.clipboard()
        marker = "bridge-copy-" + uuid.uuid4().hex
        self.screen.set_clipboard(marker)
        try:
            error = None
            for _ in range(3):
                try:
                    self.screen.click_xy(int(x), int(y))
                    break
                except COMError as exc:
                    error = exc
                    time.sleep(0.3)
            else:
                raise error
            # 并发下最坏值是"四席同时收尾"，把上限定在 3s 而不是 5s。
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = self.screen.clipboard()
                if value != marker and value.strip():
                    return value
                time.sleep(0.1)
            raise RuntimeError("复制回复后剪贴板未更新")
        finally:
            self.screen.set_clipboard(old)


def image_reply(screen, images, prompts):
    """动作条只在画面上、不进控件树时的退路（Grok 实测如此）。

    取提问下方最靠下的那一排动作条，再在排内找复制键。
    """
    if not images:
        return None
    actions, copy = images
    win = screen.window_rect()
    if win is None:
        return None
    rows = []
    for name in (actions if isinstance(actions, (list, tuple)) else (actions,)):
        rows += screen.find_all(name, threshold=0.86, region=win)
    if not rows:
        return None
    bottoms = []
    for node in prompts:
        try:
            bottoms.append(node.control.BoundingRectangle.bottom)
        except COMError:
            continue
    if bottoms:
        rows = [r for r in rows if r.y >= max(bottoms)]
    if not rows:
        return None
    row = max(rows, key=lambda r: r.y)
    return screen.find(copy, region=(row.x, row.y, row.w, row.h))


def prompt_nodes(view, prompt):
    # 比对走归一（去行首列表标记），否则「1. xxx」被页面吃掉标记后永远认领不到
    wanted = textcmp.normalize(prompt)
    found = []

    def visit(node):
        if node.kind in ("EditControl", "ComboBoxControl", "ButtonControl", "MenuControl"):
            return
        if textcmp.normalize(ui.text_of(node)) == wanted:
            found.append(node)
            return
        for child in node.children:
            visit(child)

    visit(view.root)
    return found


def merged_blocks(texts):
    """把所有块拼成一整串，并记录每块占的字符区间。

    **提问定位与答案取回共用这一处**（2026-11-12 Round 3 会审：两边各写一套，
    改一边忘另一边）。markdown 强调符号要去掉——grok 把 `**` 渲染成独立节点，
    留在串里会把提问切成两半。
    """
    parts, spans, offset = [], [], 0
    for text in texts:
        squashed = "".join(str(text or "").split()).replace("*", "")
        parts.append(squashed)
        spans.append((offset, offset + len(squashed)))
        offset += len(squashed)
    return "".join(parts), spans


def prompt_span(texts, needle: str, start_at: int = 0):
    """在拼接文本里找 `needle`，返回 `(首块, 末块, 结束偏移)`；找不到返回 None。"""
    if not needle:
        return None
    merged, spans = merged_blocks(texts)
    pos = merged.find(needle, start_at)
    if pos < 0:
        return None
    end = pos + len(needle)
    first = next((i for i, (_a, b) in enumerate(spans) if b > pos), None)
    last = next((i for i, (_a, b) in enumerate(spans) if b >= end), None)
    if first is None or last is None:
        return None
    return first, last, end


def prompt_fragments(view, prompt, *, min_chars: int = 12, limit: int = 12):
    """提问被渲染成多块时，找出属于它的叶子文本节点。

    grok 实测：一条提问 = 9~10 个节点（`**`、`只依据第一章`、`**` 各自成块），
    `prompt_nodes()` 按整段相等匹配会返回 0，于是 5 次读不到就判 `prompt_gone`，
    明明答案还在生成中就中止了（2026-11-12 两轮都这样）。
    这里按「整块是提问的连续片段、且 ≥12 字」认领，**只用于判断提问还在不在**。
    """
    wanted = "".join(textcmp.normalize(prompt).split())
    if not wanted:
        return []
    out = []
    for node in view.root.walk():
        if node.children or node.kind not in ("TextControl", "HyperlinkControl"):
            continue
        squashed = "".join(textcmp.normalize(ui.text_of(node)).split())
        if len(squashed) >= min_chars and squashed in wanted:
            out.append(node)
            if len(out) >= limit:
                break
    return out


def composer_empty(view, placeholders=()) -> bool:
    """输入框里没有用户输入。

    空 contenteditable 的 TextPattern 会返回占位文字，而且和控件名不是同一个串
    （Gemini：name「为 Gemini 输入提示」/ 文本「问问 Gemini」；Grok 的占位还轮播）。
    结构判据：值恰好等于**输入框框内**渲染出来的那个文本节点（它是输入框的兄弟节点，
    不是输入框的后代——有内容时文字在后者里面）。

    B19：加了 `placeholders`，让各端把自己那串占位文字传进来
    （gemini 的 `问问 Gemini` 不在 `KNOWN_PLACEHOLDERS` 里，于是被当成 10 字草稿，
    `clear_composer` 每次都误报 `draft_clear_failed`）。
    """
    text = ui.canonical(view.value)
    if not text:
        return True
    known = set(KNOWN_PLACEHOLDERS) | {p for p in (placeholders or ()) if p}
    if text in {ui.canonical(p) for p in known}:
        return True
    if view.editor is None:
        return False
    if text == ui.canonical(view.editor.name):
        return True
    try:
        box = view.editor.control.BoundingRectangle
    except COMError:
        return False
    inside = list(view.editor.walk())
    for node in view.root.walk():
        if node.kind != "TextControl" or ui.canonical(node.name) != text:
            continue
        if any(node is n for n in inside):
            continue
        try:
            r = node.control.BoundingRectangle
        except COMError:
            continue
        if box.left <= r.left and r.right <= box.right and box.top <= r.top and r.bottom <= box.bottom:
            return True
    return False


def _looks_like_partial_write(prompt, value) -> bool:
    """输入框里的值是不是"只写进去一截"（UIA 值逐段刷新的滞后形态）。

    grok 实测：整段写完后 0.15s 读到的还是开头 3 个字（提问的前缀），1.3s 后
    才是全文。前缀是**滞后**，值得等一拍；值完全对不上（比如 Markdown 把行首
    标记吃掉了）就不是滞后，不等，交给原判据。
    """
    v = (value or "").strip()
    p = (prompt or "").strip()
    return p.startswith(v) and len(v) < len(p)


def _read_written(conversation, prompt):
    """写入后读回输入框，允许 UIA 慢一拍（见 READBACK_SETTLE）。

    对上就立刻返回——正常一轮不额外 sleep。超时返回最后一版，调用方仍按
    严格比对判 `input_mismatch`（这里不替它下结论）。
    """
    got = conversation.read()
    if textcmp.equal(prompt, got.value) or not _looks_like_partial_write(prompt, got.value):
        return got
    deadline = time.monotonic() + READBACK_SETTLE
    while time.monotonic() < deadline:
        time.sleep(READBACK_INTERVAL)
        got = conversation.read()
        if textcmp.equal(prompt, got.value):
            break
        if not _looks_like_partial_write(prompt, got.value):
            break
    return got


def _rewrite_plain(conversation, prompt):
    """清空后改用粘贴再写一次；任何异常都当作"没成"（返回 None）。

    只在"打字版本核对不上"时才走这条路——正常一轮不多花一次剪贴板往返。
    """
    try:
        view = conversation.read()
        conversation.clear(view)
        conversation.paste(view, prompt)
        time.sleep(0.2)
        return _read_written(conversation, prompt)
    except (RuntimeError, COMError):
        return None


def _session_proof(conversation, view, prompt):
    """发送前认领"页面已有本提问+回复"时，先证明**同一会话**。

    R8（Hy4 E.3 / R7 §6.4）：`_existing_reply` 发生在采集会话 id 之前，原来只按
    文本 + 当前文档认领；同一文档同一 URL 的 SPA 换到另一个会话时，里面恰好有
    同一条提问和旧回答，会被当成本轮答案交回去。

    唯一认的是**与标题无关的会话 id**（`session_id_of`，地址里的会话路径）：
      · 记录里没有 id（老记录/桌面端） -> 证明不了；
      · 当前页面读不到 id（oc://、app://、后台标签） -> 证明不了；
      · 两者不相等 -> 证明不了。
    证明不了时返回 (False, 原因, 记录状态)，调用方必须 fail-closed：不认领、
    不发送、不清草稿；accepted/unknown 的事实原样保留。
    """
    site = getattr(conversation, "site", None)
    if not isinstance(site, str) or not site.strip():
        return False, "site_unreadable", None
    try:
        from . import submissions

        prior = submissions.pending(site.strip(), prompt)
    except Exception:                       # noqa: BLE001 - 记录读不出来按"状态不明"
        return False, "journal_unreadable", "unknown"
    if not prior:
        return False, "no_journal", None
    state = str(prior.get("state") or "unknown").lower()
    scope = submissions.recovery_scope(prior) or {}
    recorded = scope.get("session_id")
    current = session_id_of(getattr(view, "url", None))
    if not isinstance(recorded, str) or not recorded.strip():
        return False, "session_id_missing", state
    if not current:
        return False, "session_id_unreadable", state
    if recorded.strip() != current:
        return False, "session_id_mismatch", state
    return True, "matched", state


def _existing_reply(conversation, view, prompt):
    """对话里已有「本提问」且它之后有回复时，只读返回该回复正文。

    CARD-09f 第 6 条：中断后重跑、页面其实已答完时，绝不能重发。
    判据：认领到本提问（`prompt_nodes`），并且它后面有回复复制键（`view.copies`）。
    从最新一条往回试，复制到「提问本身」就跳过——宁可读不到也不冒充回答。

    R8：**先证明同一会话，再复制**。返回 `(正文, 原因, 记录状态)`：
      · `(text, None, state)`   身份已证明，可交卷（already_answered）；
      · `(None, None, None)`    页面上根本没有本提问+回复，走正常发送；
      · `(None, reason, state)` 有候选但证明不了 -> 调用方 fail-closed。
    证明不了时**连复制都不做**（不读旧回答、不碰剪贴板）。
    """
    prompts = prompt_nodes(view, prompt)
    if not prompts:
        return None, None, None
    nodes = list(view.root.walk())
    prompt_at = {i for i, n in enumerate(nodes) if n in prompts}
    candidates = []
    for node in reversed(view.copies):
        try:
            at = nodes.index(node)
        except (ValueError, COMError):
            continue
        if any(at > i for i in prompt_at):
            candidates.append(node)
    if not candidates:
        return None, None, None
    proved, reason, state = _session_proof(conversation, view, prompt)
    if not proved:
        return None, reason, state
    for node in candidates:
        try:
            text = conversation.copy(node)
        except (RuntimeError, COMError):
            continue
        if text and not textcmp.equal(prompt, text):
            return text, None, state
    # 身份已证明、页面上也确实有"本提问+回复"，但复制回来的是提问本身/读不到：
    # 不能当作"没有已答"去重发（那会违反 no-resend），同样 fail-closed。
    return None, "reply_unreadable", state


def _clear_draft_verified(conversation, view, is_empty) -> bool:
    """清空输入框并读回为空（CARD-09f 第 3 条）。读回为空返回 True，否则 False。"""
    try:
        conversation.clear(view)
        recheck = conversation.read()
        return bool(is_empty(recheck))
    except (RuntimeError, COMError):
        return False


# T2-2：PRE_SUBMIT 探测期与提交前受控重试。只用于"点完发送之后、还没证明确实进去了"
# 那一段；一旦证明已提交（POST_SUBMIT），后续任何情况下都**不许**再发（严禁重发）。
PRE_SUBMIT_PROBE = 5.0          # 施工单：PRE_SUBMIT ≤5s
SUBMIT_RETRY_TOTAL = 10.0       # 施工单：允许重试 1 次，总 ≤10s

#: 回复容器最多往上爬几层去找"有文字的那个祖先"
MAX_TEXT_CLIMB = 6

#: 稳定判据的上限。有候选回复、但文本长度一直在变（计时器 / 百分比这类 UI 噪声）时，
#: 到了这个时限就按当前内容交卷并打标记 —— 宁可能力受限地交一次，也不因噪声超时把答案丢掉。
SETTLE_MAX = 40.0

#: B16：`copies <= baseline` 持续这么久、且不在生成中，就改用树内位置判候选。
#: 45s 远大于正常出答案的时间（实测 20~50s），不会影响快路径；只救「长会话
#: 里 baseline 重锚错、明明有回复却永远不交卷」这一类。
POSITIONAL_AFTER = 45.0


def positional_ready(*, busy: bool, accepted: bool, since: float | None,
                     now: float, after: float = POSITIONAL_AFTER,
                     evidence: bool = False) -> bool:
    """纯函数：是不是该放弃 `copies > baseline`、改用树内位置判候选。

    **不能在生成中放行**（半句话会被交卷）；提问还没被收下也不放行。
    单独拎出来是为了能离线钉住这三条门槛，不靠 300s 真机超时去验。

    `evidence`：树里已出现本轮回答的动作条（`answer_action_after`）。
    deepseek 实测「停止」图标在回答结束后仍留在画面上，`busy` 恒为真，
    原来这条永不放行 → 每轮都 25 分钟空转到 timeout（2026-11-12 三轮
    `stage=text_extracted`）。动作条是完成证据，可以替代 `not busy`；
    交卷前仍有文本长度签名稳定这道闸，不会交半句话。
    """
    if not accepted or since is None or now - since < after:
        return False
    return bool(not busy or evidence)


# B17：dsh 首页的回复复制键要 hover 才出现，`view.copies` 一直为空，
# 连「位置候选」也无从下手。用户授权的只读兜底：只认「末个提问之后、
# 编辑器之前」的叶子文本，且必须在 settle 稳定后才交卷。
# B19：deepseek 也加进来。长回答把回答动作条顶出视口后，`ds_actions`/`ds_copy`
# 模板在画面上找不到 → `copies` 恒为 0 → 等待循环每轮 continue，25 分钟空转到
# `no_reply`，而页面上回答早就完整了（真机 job_review4）。这条兜底只在
# `positional`（已接受、不忙、>45s）就绪且 copies 为空时生效。
UIA_TEXT_SITES = frozenset({"dsh", "deepseek", "chatgpt"})
UIA_TEXT_EXACT = frozenset({
    "探索未至之境", "新会话", "新建会话", "复制", "重新生成", "点赞", "点踩",
    "朗读", "分享", "Off", "Low", "High", "Max", "最新一条回复",
    "DeepSeek-V4.1-Flash", "GPT-6Luna",
})
UIA_TEXT_PREFIX = ("描述你想要构建的内容", "搜索模型", "输入消息", "问DeepSeek",
                   # deepseek 的联网检索抬头与页脚（2026-11-12 实测混进答案尾部）
                   "搜索到", "已深度思考", "深度思考", "内容由 AI 生成", "以上内容",
                   # 回答耗时是 UI 计数，不是正文（chatgpt 实测「用时4分钟41秒」）
                   "用时")
def is_count_text(text: str) -> bool:
    """「-1-11」「15个网页」「2s」这类计数/时间戳，不可能是正文。

    不用正则：本模块不导入 `re`（保持依赖面最小），而且这类判据就三行。
    """
    probe = (text or "").strip()
    if not probe:
        return False
    if probe.endswith("个网页"):
        return probe[:-3].strip().isdigit()
    if probe[-1] in "sS" and probe[:-1].isdigit():
        return True
    body = probe.lstrip("-")
    # 长度 ≤ 8 才算计数：`2026-11-12` 这种日期在正文里是合法内容，不能删。
    return bool(body) and len(body) <= 8 and all(ch.isdigit() or ch in "-/." for ch in body)


#: 回答动作条上的按钮名。**不依赖 hover 的完成证据**：deepseek 的动作条
#: 要悬停才出图片模板，但按钮本身在 UIA 树里有名字，回答一生成完就出现
#: （2026-11-12 实测：完成页 2 个「朗读」，生成中 0 个）。
ANSWER_ACTION_NAMES = frozenset({"朗读", "复制", "重新生成", "重新生成回复",
                                 "点赞", "点踩", "分享"})


def rect_in_window(node, window) -> bool:
    """动作条是否落在窗口可见区内。

    长回答会把动作条顶到视口下方（chatgpt 实测：复制键 y=2061，窗口只到 1340），
    这种候选**点了也点不到**，继续按候选处理只会把整轮拖到 timeout。
    """
    if window is None:
        return True
    try:
        rect = node.control.BoundingRectangle
    except Exception:                                 # noqa: BLE001
        return True
    if rect is None:
        return True
    try:
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except AttributeError:
        try:
            left, top, right, bottom = rect
        except Exception:                             # noqa: BLE001
            return True
    try:
        wl, wt, wr, wb = window
    except Exception:                                 # noqa: BLE001
        wl, wt, wr, wb = window.left, window.top, window.right, window.bottom
    # 只要求「有一部分在窗口内」：完全在窗口外的才判不可达
    return not (right < wl or left > wr or bottom < wt or top > wb)


def clipboard_matches(written: str, actual) -> bool:
    """剪贴板内容是不是我们刚写进去的那一段。

    读不出来（None）时按"无法核对"放行——不能因为环境不提供读取就拒发；
    能读到时按归一化文本比（不同应用会把 `\r\n` 换成 `\n`）。
    """
    if actual is None:
        return True
    left = "\n".join((written or "").replace("\r\n", "\n").split("\n"))
    right = "\n".join((actual or "").replace("\r\n", "\n").split("\n"))
    return left == right


def _prompt_indices(ordered, prompts, editor) -> tuple[list[int], int]:
    """本轮提问在树里的下标，以及「答案区」的右边界。

    **必须排除编辑器之后的提问**：deepseek 把历史提问预览排在 composer 之后
    （实测 2026-11-12：prompt 在 1000 与 1217，编辑器在 1177），取「最后一个」
    会把起点放到 1218、终点留在 1177，切片为空——于是每轮都判 no_reply。
    """
    end = len(ordered)
    if editor is not None:
        try:
            end = ordered.index(editor)
        except ValueError:
            end = len(ordered)
    inside = [i for i, node in enumerate(ordered) if i < end and node in prompts]
    if inside:
        return inside, end
    outside = [i for i, node in enumerate(ordered) if node in prompts]
    return outside, len(ordered)


def answer_action_after(root, prompts, editor=None) -> int:
    """末个提问之后出现了几个回答动作按钮。历史回答的动作条不算本轮。"""
    ordered = list(root.walk())
    prompt_at, end = _prompt_indices(ordered, prompts, editor)
    if not prompt_at:
        return 0
    start = max(prompt_at) + 1
    return sum(1 for node in ordered[start:end]
               if node.kind == "ButtonControl"
               and (node.name or "").strip() in ANSWER_ACTION_NAMES)


def _rect_of(node):
    """控件矩形，统一成 `(left, top, right, bottom)`。

    UIA 有时给 RECT（有属性），有时给四元组（真机 2026-11-12 实测两种都出现过），
    这里统一，避免调用处再判类型。
    """
    try:
        rect = node.control.BoundingRectangle
    except Exception:                                 # noqa: BLE001
        return None
    if rect is None:
        return None
    try:
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except AttributeError:
        try:
            left, top, right, bottom = rect
        except Exception:                             # noqa: BLE001
            return None
    if bottom <= top or right <= left:
        return None
    return (left, top, right, bottom)


def uia_text_join(nodes) -> str:
    """把多段叶子文本按顺序拼成回复：同一视觉行接在一起，跨行才换行。

    deepseek 的长回答既「一段一个 TextControl」（实测 50+ 段），也会把一句话
    拆成多个行内片段；只取 `nodes[-1]` 等于只交最后一段（2026-11-12 修）。
    相邻重复只留一份——同一段会被父子各报一次。
    """
    lines: list[dict] = []
    for node in nodes:
        try:
            text = ui.canonical(ui.text_of(node))
        except Exception:                             # noqa: BLE001
            continue
        if not text or (lines and lines[-1]["parts"][-1] == text):
            continue
        rect = _rect_of(node)
        if rect is not None:
            left, top, right, bottom = rect
            center, height = (top + bottom) / 2, bottom - top
        if rect is not None and lines and lines[-1]["center"] is not None:
            last = lines[-1]
            if (abs(last["center"] - center) <= max(4.0, height * 0.45)
                    and left - last["right"] <= 40):
                last["parts"].append(text)
                last["right"] = max(last["right"], right)
                continue
        lines.append({"center": center if rect is not None else None,
                      "parts": [text],
                      "right": right if rect is not None else 0})
    return "\n".join("".join(line["parts"]) for line in lines).strip()


def uia_text_candidates(view, prompts, editor, prompt_text=""):
    """收集「末个提问节点之后、编辑器节点之前」的叶子文本节点。

    纯读 `Node` 属性，不 deref UIA、不碰剪贴板、不写盘。侧栏与 composer
    天然落在这个区间之外；再用黑名单和「与提问相等」两道过滤挡 UI 文案。
    """
    ordered = list(view.root.walk())
    prompt_at, end = _prompt_indices(ordered, prompts, editor)
    if not prompt_at:
        return []
    start = max(prompt_at) + 1
    # 提问气泡的**子节点**是回声：deepseek 的提问是 GroupControl，正文在子节点里，
    # 按「节点等于提问」挡不住（实测 1004 是「请分 8 段…」的截断版）。
    # 用父子关系判，而不是「文本是提问的子串」——后者会误删**答案本身**
    # （夹具实测：答案恰好是提问里的那个 token）。
    echo_ids = {id(child) for prompt_node in prompts
                for child in prompt_node.walk()}
    out = []
    for node in ordered[start:max(start, end)]:
        if id(node) in echo_ids:
            continue
        if node.kind not in ("TextControl", "HyperlinkControl") or node.children:
            continue
        try:
            text = ui.canonical(ui.text_of(node))
        except Exception:                             # noqa: BLE001
            continue
        if not text or text in UIA_TEXT_EXACT:
            continue
        if any(text.startswith(word) for word in UIA_TEXT_PREFIX):
            continue
        if prompt_text and textcmp.equal(prompt_text, text):
            continue
        if is_count_text(text):
            continue
        out.append(node)
    return out


def uia_text_len(nodes) -> int:
    """文本长度签名，供 settle 判「还在流式输出」用。"""
    total = 0
    for node in nodes:
        try:
            total += len(ui.canonical(ui.text_of(node)))
        except Exception:                             # noqa: BLE001
            continue
    return total


def reply_text_len(view, candidates) -> int:
    """最新那条回复所在容器里的叶子文本长度。

    **只用来判断"这一轮文本变了没有"**，不参与任何内容判定、不与提问比较。

    为什么不用整页：整页含侧栏/列表/计数，噪声大。为什么从复制键往上爬：
    复制键所在的容器就是这条回复；`ui.text_of` 会跳过按钮名，所以"复制/赞/踩"
    这些不会污染长度。
    """
    node = candidates[-1] if candidates else None
    for _ in range(MAX_TEXT_CLIMB):
        if node is None:
            break
        node = getattr(node, "parent", None)
        if node is None:
            break
        try:
            text = ui.canonical(ui.text_of(node))
        except Exception:                              # noqa: BLE001 - 读不到就当没有
            text = ""
        if text:
            return len(text)
    return 0


def _collection_identity(view):
    """收集阶段用来判断"还是不是同一个页面"的身份：地址路径 + document runtime id。

    读不到的项留 None（不猜）。`_identity_broken` 只在**两边都读得到**时下结论，
    和 `sendgate.diff_stamp` 同一条口径：读不到不冒充"没变"。
    """
    try:
        rid = list(view.root.control.GetRuntimeId()) or None
    except Exception:                       # noqa: BLE001 - 读不到就没有这一项
        rid = None
    url = getattr(view, "url", None)
    return {"path": (path_of(url) or None) if url else None, "rid": rid}


def _identity_broken(before, now):
    """身份有没有变（只比两边都读得到的字段）。"""
    if not before or not now:
        return False
    if before.get("path") and now.get("path") and before["path"] != now["path"]:
        return True
    if before.get("rid") and now.get("rid") and list(before["rid"]) != list(now["rid"]):
        return True
    return False


def _local_chat_persisted(before, now) -> bool:
    """是不是「临时会话落库」：`/c/local-…` → `/c/<uuid>`（B17）。

    新版 chatgpt 用 `/c/local-chatgpt:<uuid>` 表示还没落库的新对话，首条消息
    一提交就换成真实 id（2026-11-09 实测：发送成功、回复也读得到，却因为路径
    变了被判 `conversation_changed`）。只认这一条窄路径，别的变化照旧拒绝。
    """
    old = (before or {}).get("path") or ""
    new = (now or {}).get("path") or ""
    if not old or not new:
        return False
    return old.startswith("/c/local-") and new.startswith("/c/") \
        and not new.startswith("/c/local-")


def _first_turn_landed(before, now) -> bool:
    """首轮新会话「落库」：容器地址 -> 该容器下的具体会话地址。

    与 `_local_chat_persisted` 同一类，但按**路径形状**判，**不看第几次读**。

    为什么必须按形状：gemini 换地址发生在**答案出现的那一刻**。2026-11-12 实测
    新会话问「请只回复两个字：收到」：发送后地址一直是 `/app`，到第 16 秒答案
    才出来，地址同时变成 `/app/1bd58577…`。旧判据是"接受后 2 次读之内"，而答案
    出来时已经读到第 40 次上下 —— 明明发成功、答案也在页面上，却被判成
    `conversation_changed`（`reason=document_changed`），一次会审就这样白跑。

    只认「容器路径 -> 容器下的一段 id」：
      · gemini   `/app`       -> `/app/<id>`
      · chatgpt  `/c/local-…` -> `/c/<uuid>`（原 B17 路径，保留）
    反例必须继续拒：`/app/<id1>` -> `/app/<id2>`（真的换了会话）。
    """
    old = ((before or {}).get("path") or "").rstrip("/")
    new = (now or {}).get("path") or ""
    if not old or not new:
        return False
    if _local_chat_persisted(before, now):
        return True
    if old != "/app":
        return False
    rest = new[len("/app/"):] if new.startswith("/app/") else ""
    return bool(rest) and "/" not in rest


def _rect_of(node):
    """控件的屏幕矩形；读不到返回 None（不当成 0，也不猜）。"""
    try:
        rect = node.control.BoundingRectangle
    except (COMError, AttributeError):
        return None
    try:
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except (TypeError, ValueError, AttributeError):
        return None


def _window_title(name):
    """文档标题 -> 会话名。Electron 把窗口标题拼成 `<会话名> · <应用名>`。"""
    raw = (name or "").strip()
    if not raw:
        return None
    head, sep, tail = raw.rpartition(" · ")
    if sep and head.strip() and 0 < len(tail.strip()) <= 32:
        return head.strip()
    return raw


def _band_texts(root, top, low, high):
    """文档顶端 `low..high` 像素带里的叶子文本：[(x, 文本)]，按 x 排序。

    只收"像标签"的短文本（`_looks_like_tab`）。这条带子会被正文污染：
    opencode 滚到底时，消息正文会落进 0~48px，混进 tabs 里。
    """
    out = []
    for node in root.walk():
        if node.kind != "TextControl" or node.children:
            continue
        name = (node.name or "").strip()
        if not name or not _looks_like_tab(name):
            continue
        box = _rect_of(node)
        if box is None or not (low <= box[1] - top < high):
            continue
        out.append((box[0], name))
    out.sort()
    return out


def session_stamp(view):
    """当前会话在**应用 UI 上的名字**：会话名 + 标签条证据。

    **2026-11-07（R7）起它只是"变化探测器"，不是身份证明。**
    名字可变（自动命名、手动改名）、可重复（两个同名会话），所以它只能用来
    **发现变化并拒绝**；任何"可以交卷"的判断都不得依赖它，必须依赖
    `session_id_of(url)`（与标题无关的会话 id）。这个函数保留是为了：
      · 换会话时能更早、更有信息量地拒（session_changed 带 before/now）；
      · 应用自动命名（占位名 -> 真名）时不要误判成换会话。
    """
    root = view.root
    title = _window_title(getattr(root, "name", ""))
    box = _rect_of(root)
    if title is None or box is None:
        return {"title": title, "tabs": [], "proven": False, "why": "no_title"}
    tabs = [text for _x, text in _band_texts(root, box[1], 0, TAB_BAND_H)]
    if len(tabs) < 2:
        return {"title": title, "tabs": tabs, "proven": False, "why": "no_tab_strip"}
    if tabs.count(title) != 1:
        return {"title": title, "tabs": tabs, "proven": False, "why": "ambiguous_title"}
    in_band = set(tabs)
    heads = {text for _x, text in _band_texts(root, box[1], TAB_BAND_H, HEADER_BAND_H)
             if text in in_band}
    # B10：原来要求 `heads == {title}`（页头带**只有**标题）。
    # 实测这个判据太脆：长任务滚到底时正文残片也会落进 48~96px，
    # 于是 heads 里多出无关文本 → header_mismatch → proven 从 true 掉 false
    # → 被当成"换了会话"。改成"标题**必须出现**在页头带里"：
    # 仍然证明"页头显示的就是这个会话"，但不再被无关文本一票否决。
    if title not in heads:
        return {"title": title, "tabs": tabs, "proven": False, "why": "header_mismatch"}
    return {"title": title, "tabs": tabs, "proven": True, "why": "ok"}


def _session_same(before, now):
    """两份**名字**戳是否自洽（只用于"没变"的探测，绝不用于放行）。"""
    return bool(before and now and before.get("proven") and now.get("proven")
                and before.get("title") == now.get("title"))


def _session_provisional(stamp):
    """这份会话戳是不是应用的"新会话占位名"（还没自动命名）。"""
    return bool(stamp) and str(stamp.get("title") or "").strip().lower() in PROVISIONAL_TITLES


def _has_reply_after(nodes, prompts, copies):
    """提问节点后面有没有已经存在的回复键（用来辨认"历史问答对"）。"""
    if not prompts or not copies:
        return False
    wanted = {id(c) for c in copies}
    last_prompt = max(i for i, n in enumerate(nodes) if any(n is p for p in prompts))
    return any(i > last_prompt and id(n) in wanted for i, n in enumerate(nodes))


def _single_new_reply(conversation, view, prompt, baseline):
    """提问已经从树里消失时的兜底：只认"比发送前多出来的**唯一**一条回复"。

    调用方必须先确认文档身份没变、页面不在生成中。多一条、复制回来等于提问本身、
    或者控件不可用 —— 一律返回 None（宁可不交卷，也不交错的）。
    """
    try:
        fresh = list(view.copies[baseline:])
    except Exception:                       # noqa: BLE001
        return None
    if len(fresh) != 1:
        return None
    node = fresh[0]
    try:
        if not node.control.IsEnabled:
            return None
        answer = conversation.copy(node)
    except (RuntimeError, COMError):
        return None
    if not answer or textcmp.equal(prompt, answer):
        return None
    return answer


def _ask_impl(conversation, prompt, timeout=180.0, pre_send=None, plain=False,
              watch=None, ledger=None):
    """Timeout is a single budget. A submitted/unknown turn is never retried.

    `ledger`：T2-1 的提交账本句柄（`_Ledger`）。None = 不记账，行为与改造前一致。
    本函数有 19 个 return 点，账本收尾不在这里做——由外层 `ask()` 统一包，避免漏点。

    `pre_send()`：**写入文本之后、点发送之前**跑的门（CARD-06b 第 3 条）。
    它负责档位只读复核 + 人机验证，并返回 {"stamp": 页面指纹}；
    本函数在真正点击之前再比一次指纹，变了就拒发。

    `plain=True`：打字版本核对不上时，清空改用粘贴纯文本再写一次
    （CARD-09e 第 5 条，见 `Conversation.paste`）。

    `watch(view)`：每轮读完树之后调一次（CARD-09e 第 3 条）。返回 dict 就**提前交卷**，
    返回 None 继续按原判据等。给 ChatGPT 用：它的提问气泡会被折叠，共享判据永远
    认领不到，只能一路等到超时（2026-11-02 实测 240s）；而页面自己会说"回答已完成"。
    默认 None —— 另外几端的行为一个字节不变。
    """
    is_empty = getattr(conversation, "empty", composer_empty)
    state = {"ok": False, "submission": "not_sent", "sent": False}
    deadline = time.monotonic() + timeout
    try:
        # ---- S 段（B13b）：首读 → 已有回复 → 草稿 → 写入 → 读回 → 指纹 → 门禁 → 落账 → 发送
        # **必须是一整段**，中途放锁就必然失败（不是偶发）：
        #   ① write 用的是这一帧的控件坐标，被抢前台 → _assert_foreground 抛 ForegroundLost
        #   ② pre_send 的 page_stamp 含 foreground，True→False 会被自己的门禁判成 concurrent_ui
        #   ③ _submit 的 4 轮重试里会 read()，必须读到自己那张页面
        before = written = None
        sent_prompts = 0
        baseline = 0
        anchor_path = None
        screen = site = None
        stamp_before_write = None
        #: B25：发车时这一页的会话 id。提前交卷（watch）必须先证明还是同一会话，
        #: 否则页面被切到别的对话时，会把别人的回答当成本轮结果。
        send_sid = None

        def _prepare_and_send():
            nonlocal before, written, sent_prompts, baseline, anchor_path
            nonlocal screen, site, stamp_before_write, send_sid
            # 并发时"前台"是共享资源：`section` 只保证**同一时刻只有我在动桌面**，
            # 不保证**我的窗口在前台**（2026-11-08 真机：codex 一抢前台，workbuddy
            # 点输入框就抛 ForegroundLost，还会被误记成 user_took_over）。
            # 所以进段先把自己的窗口抢回来；抢不到就干净失败，不伪装成"用户接管"。
            _screen = getattr(conversation, "screen", None)
            _focus = getattr(_screen, "focus", None)
            if _focus is not None and getattr(_screen, "target_hwnd", None):
                if not _focus():
                    time.sleep(0.3)
                    if not _focus():
                        return {**state, "stage": "focus_lost", "reason": "not_foreground",
                                "submission": "not_sent", "sent": False,
                                "hint": "目标窗口拿不到前台，本轮没发送（可能被别的席位占着）"}
            before = conversation.read()
            if before.busy:
                return {**state, "stage": "conversation_busy"}
            # 第 6 条：发送前先看对话里是否已有「本提问」且它之后有回复（防重发）。
            # 中断后重跑、页面其实已答完时，绝不重发，只读已有回复返回 already_answered。
            # R8：必须先证明是同一会话；证明不了就 fail-closed（不认领、不发送、不清草稿）。
            existing, unproven, prior_state = _existing_reply(conversation, before, prompt)
            if existing:
                # 第 6 条：已提交后提问被回填进输入框时，要清掉并读回为空（失败也如实记，
                # 不冒充"已清"）。already_answered 命中即命中，清草稿只是顺手收尾。
                cleared = None
                if not is_empty(before):
                    cleared = _clear_draft_verified(conversation, before, is_empty)
                return {**state, "ok": True, "stage": "already_answered", "reply": existing,
                        "submission": None, "sent": False, "basis": "already_answered",
                        "cleared_draft": cleared}
            if unproven:
                submission = ("accepted" if prior_state in ("accepted", "answered")
                              else "unknown" if prior_state else "not_sent")
                return {**state, "stage": "session_unproven", "reason": unproven,
                        "session": "unproven", "sent": False,
                        "submission": submission, "prior_submission": prior_state,
                        "hint": "页面里已有同一条提问和回复，但证明不了是同一会话"
                                f"（{unproven}）；按 fail-closed 不采信、不发送、不清草稿"}
            if not is_empty(before):
                # B19：deepseek 粘贴没落地时会留下"提问的前 3 个字"。下一轮看见它
                # 就报 draft_present，整轮永远走不动（真机连挂 3 次）。打开
                # `adopt_self_draft` 的端：值是本提问的前缀 → 认成自己的残草清掉；
                # 其它任何内容仍然按用户草稿拒发。
                adopt = bool(getattr(conversation, "adopt_self_draft", False))
                if adopt and _looks_like_partial_write(prompt, getattr(before, "value", "")):
                    cleared = _clear_draft_verified(conversation, before, is_empty)
                    state["adopted_self_draft"] = True
                    if not cleared:
                        return {**state, "stage": "draft_present",
                                "hint": "输入框里是本提问的前缀（自己上一轮的残草），但没清掉",
                                "cleared_draft": cleared}
                    before = conversation.read()
                else:
                    return {**state, "stage": "draft_present", "hint": "当前输入框已有草稿"}
            sent_prompts = len(prompt_nodes(before, prompt))
            baseline = len(before.copies)
            anchor_path = path_of(before.url)
            # B25：记下发车时的会话 id。提前交卷只认这个 id，不认"回复条数变多"。
            send_sid = session_id_of(before.url)

            # 第 3 条：写入之前记页面指纹，写入之后、发送之前再比一次；
            # 前台标签/文档被换走就立即停手、清草稿、以 concurrent_ui 退出。
            # 只在真机（screen 是真 Screen 实例）时做——离线假件的 screen 是 mock/假件，
            # 不参与并发检测。
            screen = getattr(conversation, "screen", None)
            site = getattr(conversation, "site", None)
            stamp_before_write = (sendgate.page_stamp(screen, site)
                                  if isinstance(screen, Screen) and isinstance(site, str)
                                  else None)

            try:
                conversation.write(before, prompt)
            except WriteNotLanded as exc:
                # B19：把"没写进去"和"写进去了但对不上"分开。旧代码这里没有分支，
                # 粘贴失败会一路走到 input_mismatch，看日志分不清是粘贴没落地
                # 还是页面把文字改了。
                cleared = _clear_draft_verified(conversation, conversation.read(), is_empty)
                return {**state, "stage": "input_not_landed",
                        "attempts": getattr(exc, "attempts", None),
                        "submission": "not_sent", "sent": False,
                        "cleared_draft": cleared, "hint": str(exc)}
            written = _read_written(conversation, prompt)
            if plain and not textcmp.equal(prompt, written.value):
                # 第 5 条：行首 `#`/`* `/`1. ` 被编辑器当 Markdown 吃掉，打字这条路走不通，
                # 清空改用粘贴纯文本（只再试一次，不无限重来）
                retried = _rewrite_plain(conversation, prompt)
                if retried is not None:
                    written, state["write_via"] = retried, "paste"
            if not textcmp.equal(prompt, written.value):
                # 第 5 条：归一只用于比对；hint 要给出第一处差异的行号与前后 20 字
                return {**state, "stage": "input_mismatch",
                        "hint": textcmp.describe(prompt, written.value) or
                                f"写进去的文本和提问对不上（{textcmp.lengths(prompt, written.value)}）"}
            state.update(submission="unknown", sent=None)

            # 第 3 条：发送阶段复核页面指纹，被换走 → 清掉已写入的半截文本 + concurrent_ui
            if stamp_before_write is not None:
                concurrent = sendgate.page_confirm(screen, site, stamp_before_write)
                if not concurrent["ok"]:
                    cleared = _clear_draft_verified(conversation, written, is_empty)
                    return {**state, "stage": "concurrent_ui", "reason": "page_changed",
                            "changed": concurrent["changed"], "cleared_draft": cleared,
                            "submission": "not_sent", "sent": False}

            # 最后一刻门禁：复核（消费回执）+ 记指纹，然后立刻再比一次
            stamp = None
            reserved_by_door = False
            if pre_send is not None:
                try:
                    door = pre_send() or {}
                except (tier_guard.TierUnconfirmed, human.HumanCheck) as exc:
                    return {**exc.as_result(), "submission": "not_sent", "sent": False}
                # B26：有的端（workbuddy）在门禁里已经自己落过账了。它必须回报
                # `reserved=True`，否则这里再 reserve 一次会把"本轮刚写的
                # submitting"当成"上一次"，把合法的首发拦掉。
                reserved_by_door = bool(door.get("reserved"))
                stamp = door.get("stamp")
                if stamp is not None:
                    verdict = sendgate.page_confirm(conversation.screen, conversation.site, stamp)
                    if not verdict["ok"]:
                        return {**state, "stage": "tier_unconfirmed",
                                "reason": "page_changed", "code": tier_guard.TierUnconfirmed.code,
                                "changed": verdict["changed"], "page": verdict["now"],
                                "hint": "档位复核之后页面发生了导航/新对话/窗口切换，"
                                        "按卡面拒发；请重新 choose 再发"}
            # T2-1：提交动作**之前**落账。命中旧记录（accepted/submitting/unknown/
            # failed_terminal）就在这里拒发，绝不点击。没给 purpose 时 ledger=None，
            # 这段完全跳过，老调用方行为不变。
            if ledger is not None:
                if reserved_by_door:
                    refused = ledger.adopt(written)
                    if refused is not None:
                        return refused
                else:
                    refused = ledger.reserve(written)
                    if refused is not None:
                        return refused
            try:
                conversation.submit(written)
            except UserTookOver as exc:
                return {**state, "submission": "not_sent", "sent": False,
                        "stage": "user_took_over", "hint": str(exc)}

        try:
            with uilock.section(f"send:{getattr(conversation, 'site', None)}",
                                timeout=uilock.SECTION_TIMEOUT):
                early = _prepare_and_send()
        except uilock.Busy as exc:
            # 并发模式下拿不到桌面锁：一个字都没发出去，按可重试处理
            return {**state, "stage": "ui_busy", "reason": "send_section",
                    "submission": "not_sent", "sent": False,
                    "hint": f"拿不到桌面锁，本轮没发送：{exc}"}
        if early:
            return early

        # T2-2：PRE_SUBMIT 探测期（≤5s）+ 提交前受控重试（至多 1 次，总 ≤10s）。
        #
        # 判据只有一条：**拿到"没提交成功"的正证据**才允许重试 —— 输入框里还留着
        # 我们这段文字，且提问节点数量没变。读不到、框已空、或条数变了，一律不重试，
        # 直接按 POST_SUBMIT 交给下面的主循环（POST_SUBMIT 严禁重发）。
        # T2-2：PRE_SUBMIT 探测期（≤5s）+ 提交前受控重试（至多 1 次，总 ≤10s）。
        #
        # **不额外读一次树**：判定就挂在主循环已有的"输入框还非空"分支上（那里每轮
        # 本来就有 `view`）。多插读会打乱所有假件测试的读序列，也会让真机上多一轮 UIA。
        submit_started = time.monotonic()
        retried = False

        def _maybe_retry(current_view):
            """PRE_SUBMIT 阶段允许的**受控重试**（至多 1 次，总 ≤10s）。

            正证据只有一条：框里还留着我们这段文字（发送成功会被清空）。
            窗口外、框已空、或已经重试过，一律不动 —— POST_SUBMIT 严禁重发。
            """
            nonlocal retried
            if retried:
                return None
            # B25：提问已经渲染到控件树上 = 这一下确实发出去了，比"框非空"更硬。
            # opencode 每轮结束会把提问回填输入框，只看"框非空"会在 5–10s 窗口
            # 里再点一次，同一提问发两遍。
            if len(prompt_nodes(current_view, prompt)) > sent_prompts:
                return None
            waited = time.monotonic() - submit_started
            if not (PRE_SUBMIT_PROBE <= waited <= SUBMIT_RETRY_TOTAL):
                return None
            if is_empty(current_view):
                return None
            retried = True
            state["submit_retried"] = True
            try:
                with uilock.section(f"retry:{getattr(conversation, 'site', None)}",
                                    timeout=uilock.SECTION_TIMEOUT):
                    conversation.submit(current_view)
            except UserTookOver as exc:
                return {**state, "submission": "not_sent", "sent": False,
                        "stage": "user_took_over", "hint": str(exc)}
            return None

        # 第5项：同一个候选连续 settle 秒（两次读取）没变、且期间不忙才复制
        settle = getattr(conversation, "settle", 1.5)
        stable_sig = stable_since = None
        unsettled_since = None      # R11：文本第一次开始变化/持续变化起点
        last_busy = False

        accepted = False
        read_failures = 0
        # R11：跨轮的判据状态（提交时刻 / 连续回填计数 / 无声起点 / 基线条数）
        reject_state = reject.new_state()
        # B26：判据没开的端也要留现场。每轮记一次"看到了什么"，
        # 超时/超时类失败时带回去，攒够证据才能给 PROFILES 加端（否则是猜）。
        last_signals: dict = {}
        reject_state["submit_at"] = submit_started
        reject_state["baseline_copies"] = baseline
        # R5：接受那一刻的页面身份 + "连续读不到提问"的计数
        accepted_ident = None
        # R6：接受那一刻的**名字**戳（只用于发现变化，R7 起不作身份证明）
        accepted_stamp = None
        # R7：接受那一刻的**会话 id**（与标题无关；没有就是 None）
        accepted_sid = None
        misses = 0
        since_accept = 0
        identity_misses = 0     # B10：连续读不出会话戳的次数（≠ 换会话）
        hb_reads = 0            # B11：本轮读了几次（只喂心跳）
        # B15：有些端（codex）的"停止"键会常驻，busy 恒为真 → settle 永远触发不了。
        # 服务端按端下发秒数（`stall_busy_ignore`），0 = 关闭（其它端与今天一致）。
        try:
            stall_limit = float(os.environ.get("BRIDGE_STALL_BUSY") or 0)
        except ValueError:
            stall_limit = 0.0
        busy_since = None
        busy_last_sig = None
        busy_ignored_once = False
        no_copies_since = None
        copies_max = 0          # 诊断：本轮见过的最大复制键条数
        busy_reads = 0          # 诊断：读到 busy=True 的次数
        evidence_seen = False   # 诊断：是否见过回答动作条（完成证据）
        # B17：只复制到提问时用（见下面的 PROMPT_COPY_GRACE）：(当时的复制键条数, 起算时刻)
        prompt_only = None
        loop_started = time.monotonic()
        while time.monotonic() < deadline:
            try:
                # 读也要持锁：后台窗口的 URL 是空串（ui.py 头注释实测），
                # 未聚焦时读到的 url 不可信；DeepSeek/Grok 更是要截图。
                # 20s：4 席共用一把锁，单次"切标签+读树+URL 复核"约 0.5–1.5s，
                # 第 4 席最坏要等 6s；5s 会常态 Busy（虽能自愈，但读率被压到很低）。
                with uilock.section(f"poll:{getattr(conversation, 'site', None)}",
                                    timeout=20.0):
                    view = conversation.read(attempts=2, delay=0.4)
            except uilock.Busy:
                # 别人正占着桌面。不判死、不计失败，下一轮再来。
                time.sleep(0.4)
                continue
            except (RuntimeError, COMError) as exc:
                # 已经发出去了，读不到只是页面在换路由/重绘（Gemini 答完才写会话 id）。
                # 别把"暂时读不到"当成"这一轮失败了"。
                read_failures += 1
                if read_failures >= 12:
                    return {**state, "stage": "interrupted", "hint": str(exc)}
                time.sleep(1.0)
                continue
            read_failures = 0
            last_busy = view.busy
            # B11：让面板在等回答期间看得见（1.5s 节流，没有 job 环境变量时 no-op）
            hb_reads += 1
            # B16：把「为什么一直不交卷」变成可采样的量。以前只有 reads 一个计数，
            # 300s 超时后只剩 `stage=None`，分不清是没读到回复、还是读到了但签名不稳。
            heartbeat.beat_view("waiting", view, reads=hb_reads,
                                accepted=accepted, baseline=baseline,
                                composer_len=len(view.value or ""))
            # 用量硬墙：页面明说额度用完，就别按"还在生成"等到整档预算。
            # 只读判断，不改任何页面状态；命中即收工，让上层去决定等额度还是换端。
            wall = quota_wall(getattr(conversation, "site", None), view)
            if wall:
                # 额度墙是页面上的硬事实：无论有没有被判过接受，都别再等下去。
                # 但两种情形要分开记 —— 判过接受的那条记录**不许改口**：
                # 改成 `not_sent` 会让账本降成可重发（B24 只拦旧记录，不拦本轮
                # 刚 reserve 的 submitting），额度一恢复就会真的再发一遍。
                if accepted:
                    return {**state, "stage": "quota_exhausted",
                            "reason": "quota_wall", "quota_text": wall,
                            "hint": "页面提示用量已用完（硬墙）。这条 accepted 是在额度墙"
                                    "期间写的，可能并没有真的发出去；额度恢复后若要重发，"
                                    "先用 tools/submission_clear.py 清掉这条记录"}
                return {**state, "stage": "quota_exhausted",
                        "reason": "quota_wall", "quota_text": wall,
                        "sent": False, "submission": "not_sent",
                        "hint": "页面提示用量已用完（硬墙）：未发送，不重试、不绕过，"
                                "等额度恢复或换端"}
            prompts = prompt_nodes(view, prompt)
            if not prompts:
                # 整段匹配失败时退回「碎片认领」：只影响"提问还在不在"的判断，
                # 不会把碎片当身份证据（身份仍由会话 id / 标题戳负责）。
                fragments = prompt_fragments(view, prompt)
                if len(fragments) >= 2:
                    prompts = fragments
            ident = _collection_identity(view)
            stamp = session_stamp(view)
            sid = session_id_of(view.url)
            prompt_here = len(prompts) > sent_prompts

            # B25：提前交卷要先把身份算出来再判。`watch`（chatgpt 的 UIA 兜底）
            # 只看"回复条数变多 + 文本两次相同"，页面被切到别的会话时会认错人。
            # 只认发车时那个会话 id；取不到 id 就退回正常等待（少 1–2 秒提前量，换正确性）。
            if watch is not None and send_sid is not None and sid == send_sid:
                early = watch(view)
                if early:
                    return {**state, **early}

            # R11：端点拒绝这一轮的结构判据。**必须放在下面所有分支之前**——
            # `len(prompts) <= sent_prompts` 那条分支会 `continue` 走掉，放后面就永远轮不到。
            # 只有拿到了实测结构证据的端才会命中（`reject.PROFILES`），其余端一个字节不变。
            reject_state["accepted"] = accepted
            facts = reject.observe(view, prompt, prompt_here=prompt_here)
            last_signals = reject.signals(facts)
            refused = reject.verdict(reject.profile(site), facts, reject_state,
                                     time.monotonic())
            if refused:
                return {**state, **refused}

            # 接受之后身份一变，就是换了文档/换了路由：绝不再从这个页面收集答案。
            if accepted:
                if accepted_ident is None:
                    accepted_ident = ident
                since_accept += 1
                if _identity_broken(accepted_ident, ident):
                    nodes_now = list(view.root.walk())
                    # 唯一例外：刚接受、页面上有我们这条提问、且**还没有任何回复**
                    # （新会话首轮换路由）。这时以新身份重新锚定一次。
                    if (since_accept <= ACCEPT_NAV_GRACE_READS and misses == 0
                            and prompts
                            and not _has_reply_after(nodes_now, prompts, view.copies)):
                        accepted_ident = ident
                        anchor_path = path_of(view.url)
                        baseline = len(view.copies)
                        time.sleep(0.4)
                        continue
                    # B17：chatgpt 新界面把「还没落库的新对话」放在
                    # `/c/local-chatgpt:<uuid>`，第一条消息一提交就换成 `/c/<uuid>`
                    # （实测 2026-11-09：发送成功、回复也读得到，却在这里被判成
                    # conversation_changed）。这**不是**换会话，是同一段对话在落库，
                    # 所以允许重新锚定一次——但只认"我们的提问还在页面上"，且只认
                    # 这条临时 id → 真实 id 的窄路径，别的路径变化照旧拒。
                    #
                    # 2026-11-12 扩成 `_first_turn_landed`：gemini 的落库发生在
                    # **答案出现那一刻**（实测第 16 秒、读到第 40 次上下），
                    # "接受后 2 次读之内"的窗口根本覆盖不到，于是判成换会话。
                    if prompts and _first_turn_landed(accepted_ident, ident):
                        accepted_ident = ident
                        anchor_path = path_of(view.url)
                        # 落库迟到时答案可能已经上屏：**别把它划进基线**，
                        # 否则重新锚定等于把刚到的答案当成历史，白等到超时。
                        # 只有"确实还没有回复"才重设基线（原来那一支的语义）。
                        if not _has_reply_after(nodes_now, prompts, view.copies):
                            baseline = len(view.copies)
                        time.sleep(0.4)
                        continue
                    return {**state, "stage": "conversation_changed",
                            "reason": "document_changed",
                            "identity": {"before": accepted_ident, "now": ident}}

                # R6：文档和 URL 都没变时，名字戳只能用来**发现**"换没换会话"并拒绝；
                # R7：它仍然不是身份证明（标题可变可重复），所以这里只产生拒绝，
                # 从不产生"可以交卷"。提问还在树上时允许一次占位名自动命名。
                if prompt_here:
                    if sid:
                        accepted_sid = sid
                    if stamp["proven"]:
                        if accepted_stamp is None:
                            accepted_stamp = stamp
                        elif not _session_same(accepted_stamp, stamp):
                            if _session_provisional(accepted_stamp) \
                                    and not _session_provisional(stamp):
                                accepted_stamp = stamp
                            else:
                                return {**state, "stage": "conversation_changed",
                                        "reason": "session_changed",
                                        "session": "changed",
                                        "session_identity": {"before": accepted_stamp,
                                                             "now": stamp}}
                elif accepted_stamp is not None:
                    if stamp["proven"]:
                        identity_misses = 0
                        if not _session_same(accepted_stamp, stamp):
                            # 提问已经读不到，名字又**确实**变了：换过会话，停手。
                            return {**state, "stage": "conversation_changed",
                                    "reason": "session_changed", "session": "changed",
                                    "session_identity": {"before": accepted_stamp,
                                                         "now": stamp}}
                    else:
                        # 传感器读不出（标签带被正文污染等），**不是**事实变化。
                        # 继续等；连续读不出足够多次才承认"身份不可信"，
                        # 而且用另一个 reason 记账，别和"真的换会话"混在一起。
                        identity_misses += 1
                        if identity_misses >= IDENTITY_UNREADABLE_READS:
                            return {**state, "stage": "conversation_changed",
                                    "reason": "identity_unreadable",
                                    "session": "unreadable",
                                    "session_identity": {"before": accepted_stamp,
                                                         "now": stamp},
                                    "hint": ("连续 %d 次读不出会话标题/标签带（不是换了会话）："
                                             "页面结构可能变了，先人工看一眼"
                                             % identity_misses)}

            if len(prompts) <= sent_prompts:
                if accepted:
                    misses += 1
                    # 还在生成（停止键可点）时，绝不因为"读不到提问"就交卷：
                    # 实测 opencode 长任务中途会把提问气泡整个从树里丢掉。
                    if not view.busy and misses >= MISSING_STABLE_READS:
                        # 提问没了但身份没变、也不在生成：只认"比发送前多出来的
                        # 唯一一条回复"（多一条或读回提问本身都不认）。
                        fallback = None
                        # 两份身份都要读得到（接受时一份、现在一份），才允许用"新回复"兜底；
                        # 任何一份读不出来都可能是换到了看不见的文档上。
                        stable_doc = bool((accepted_ident or {}).get("rid")
                                          and (ident or {}).get("rid"))
                        # R7：**会话身份只认与标题无关的 id**。标题一致不算证明
                        # （同名/改名都能骗过它）；没有 id 就永远不交卷。
                        stable_session = bool(accepted_sid) and accepted_sid == sid
                        if stable_doc and stable_session:
                            fallback = _single_new_reply(conversation, view, prompt, baseline)
                        if fallback is not None:
                            return {**state, "ok": True, "stage": "answered",
                                    "reply": fallback,
                                    "basis": "new_reply_after_baseline",
                                    "attribution": "new_reply",
                                    "identity": "stable",
                                    "session": "proven",
                                    "session_id": accepted_sid,
                                    "session_title": (accepted_stamp or {}).get("title")}
                        # B26：只加观测，不改判定。会审指出这条分支只有 `misses`
                        # 一个量，事后无法判断"当时页面上到底有没有回复动作条"。
                        evidence = {"misses": misses,
                                    "copies": len(view.copies or []),
                                    "baseline_copies": baseline,
                                    "prompts": len(prompts),
                                    "sent_prompts": sent_prompts,
                                    "reads": hb_reads,
                                    "busy": bool(view.busy)}
                        try:
                            heartbeat.beat_view("prompt_gone", view, force=True, **evidence)
                        except Exception:          # noqa: BLE001 - 观测失败不影响返回
                            pass
                        return {**state, "stage": "conversation_changed",
                                "reason": "prompt_gone", **evidence,
                                "session": ("proven" if stable_session else "unproven"),
                                "session_id": sid,
                                "session_title": (accepted_stamp or {}).get("title"),
                                "identity": ("stable" if (ident or {}).get("rid")
                                             else "unreadable"),
                                "hint": (None if stable_session else
                                         "没有与标题无关的会话 id（或它变了），"
                                         "无法证明是同一会话；不使用'新回复'兜底")}
                    time.sleep(0.4)
                    continue
                # 还没接受、提问条数也没变：这就是"点了但没落下去"的主现场。
                early = _maybe_retry(view)
                if early:
                    return early
                time.sleep(0.4)
                continue
            misses = 0
            if not accepted:
                if not is_empty(view):
                    early = _maybe_retry(view)
                    if early:
                        return early
                    time.sleep(0.4)
                    continue
                accepted = True
                state.update(submission="accepted", sent=True)
                accepted_ident = ident
                # 接受这一帧提问就在树上：这一页确实有我们这条消息。
                # 名字戳只作变化探测；会话 id（与标题无关）才是身份。
                accepted_stamp = stamp if stamp.get("proven") else None
                accepted_sid = sid
                since_accept = 0
            if path_of(view.url) != anchor_path:
                # 地址变了（首轮新建会话）：以这一刻为基线，已有回复不算本轮
                anchor_path = path_of(view.url)
                baseline = len(view.copies)
                time.sleep(0.4)
                continue
            # B15：busy 停滞判据。只有"签名长时间不变 + 已经有复制候选 +
            # 从起跑算已过 max(60, 2*stall)"三条同时成立，才把 busy 当成误报。
            # 文本长度参与签名？这里没有正文，所以用 (copies, 输入框长度)；
            # 真正防"半句话"的是下面 settle 签名里的 reply_text_len，它一直在变。
            busy_now = bool(view.busy)
            copies_max = max(copies_max, len(view.copies))
            busy_reads += 1 if busy_now else 0
            busy_ignored = False
            if busy_now and stall_limit > 0:
                sig_now = (len(view.copies), len(view.value or ""))
                now_b = time.monotonic()
                if sig_now != busy_last_sig:
                    busy_last_sig, busy_since = sig_now, now_b
                if (busy_since is not None
                        and now_b - busy_since >= stall_limit
                        and now_b - loop_started >= max(60.0, 2 * stall_limit)
                        and len(view.copies) > baseline):
                    busy_ignored = True
                    busy_ignored_once = True
                    state.setdefault("warnings", [])
                    if "busy_ignored" not in state["warnings"]:
                        state["warnings"].append("busy_ignored")
            # 有图片兜底的端（Grok）动作条不进控件树，copies 永远是空，不能拿条数当门
            positional = False
            short_circuit = (busy_now and not busy_ignored) or (
                    len(view.copies) <= baseline and not getattr(conversation, "images", None))
            if short_circuit:
                stable_sig = None
                now_gate = time.monotonic()
                if no_copies_since is None:
                    no_copies_since = now_gate
                # B16：长会话里 `baseline` 会被重锚到「已经含新回复」的 copies 数，
                # 于是 `copies <= baseline` 永远成立 —— R4 实测 codex 卡了 300s，
                # `copies=9 baseline=9` 一动不动，而窗口里回复明明已经在了。
                # 卡够 45s 且不在生成中，就改用「树里位置在提问之后」判候选，
                # 不再把 copies 的条数差当唯一门槛。快路径（几十秒就出答案）不变。
                # 只在「copies 一直为空且已过 45s」时才多走一次树，量很小；
                # 动作条也算完成证据（busy 可能是停止图标的误报）。
                evidence = False
                if (accepted and no_copies_since is not None
                        and now_gate - no_copies_since >= POSITIONAL_AFTER):
                    evidence = answer_action_after(view.root, prompts,
                                                   getattr(view, "editor", None)) > 0
                    evidence_seen = evidence_seen or evidence
                positional = positional_ready(busy=busy_now, accepted=accepted,
                                              since=no_copies_since, now=now_gate,
                                              evidence=evidence)
                heartbeat.beat("gate", reads=hb_reads, busy=busy_now,
                               copies=len(view.copies), baseline=baseline,
                               positional=positional,
                               why=("busy" if busy_now and not busy_ignored else "no_copies"))
                if not positional:
                    time.sleep(0.4)
                    continue
            else:
                no_copies_since = None
            ordered = list(view.root.walk())
            prompt_at = {i for i, n in enumerate(ordered) if n in prompts}
            candidates = []
            for node in reversed(view.copies if positional else view.copies[baseline:]):
                try:
                    if not node.control.IsEnabled:
                        continue
                    at = ordered.index(node)
                except (ValueError, COMError):
                    continue
                if any(at > i for i in prompt_at):
                    candidates.append(node)
            if candidates:
                window = None
                owner = getattr(conversation, "screen", None)
                if owner is not None:
                    try:
                        window = owner.window_rect()
                    except Exception:                 # noqa: BLE001
                        window = None
                reachable = [n for n in candidates if rect_in_window(n, window)]
                if window is not None and not reachable:
                    # 动作条全在视口外：复制键点不到。当作「没有候选」，
                    # 让下面的 UIA 文本兜底接手（否则会一直重试到 timeout）。
                    candidates = []
                    positional = True
                    warns = list(state.get("warnings") or [])
                    if "actions_offscreen" not in warns:
                        warns.append("actions_offscreen")
                    state["warnings"] = warns
            if not candidates:
                # B17：dsh 专属、用户授权的只读 UIA 文本兜底。只在「已放开
                # copies 条数门槛」(positional) 之后生效，避免拿到半句话。
                helper = globals().get("uia_text_candidates")
                if (getattr(conversation, "site", None) in UIA_TEXT_SITES
                        and positional and callable(helper)):
                    nodes = helper(view, prompts, getattr(view, "editor", None), prompt)
                    if nodes:
                        sig, now = ("uia_text", len(view.copies), len(nodes),
                                    uia_text_len(nodes)), time.monotonic()
                        heartbeat.beat("uia_text", reads=hb_reads, nodes=len(nodes),
                                       text_len=sig[3], busy=bool(view.busy))
                        if sig != stable_sig:
                            stable_sig, stable_since = sig, now
                        if now - stable_since >= settle:
                            text = uia_text_join(nodes)
                            screen = getattr(conversation, "screen", None)
                            shot, warns = None, list(state.get("warnings") or [])
                            if screen is not None:
                                try:
                                    shot = str(screen.snapshot("dsh_uia_text"))
                                except Exception:      # noqa: BLE001
                                    warns.append("uia_screenshot_failed")
                            return {**state, "ok": True, "stage": "answered",
                                    "reply": text, "basis": "uia_text_after_prompt",
                                    "source": "uia_text", "screenshot": shot,
                                    "warnings": warns}
                        time.sleep(0.4)
                        continue
                screen = getattr(conversation, "screen", None)
                images = getattr(conversation, "images", None)
                match = image_reply(screen, images, prompts) if screen else None
                if match is not None:
                    sig, now = ("image", tuple(match.center)), time.monotonic()
                    if sig != stable_sig:
                        stable_sig, stable_since = sig, now
                    if now - stable_since < settle:
                        time.sleep(0.4)
                        continue
                    answer = conversation.copy_at(*match.center)
                    if not textcmp.equal(prompt, answer):
                        return {**state, "ok": True, "stage": "answered", "reply": answer,
                                "basis": "new_user_message_and_screen_copy"}
                time.sleep(0.4)
                continue
            # R11：签名里加**文本长度**。只数控件个数时，流式输出期间
            # `copies == 1 && candidates == 1` 从头到尾不变，1.5 秒就被判"稳定"
            # 并复制走半句话。加上长度之后，只有正文真的不再增长才会稳定。
            sig, now = ("copies", len(view.copies), len(candidates),
                        reply_text_len(view, candidates)), time.monotonic()
            heartbeat.beat("settle", reads=hb_reads, text_len=sig[3], sig=str(sig),
                           copies=len(view.copies), baseline=baseline,
                           busy=bool(view.busy), candidates=len(candidates))
            if sig != stable_sig:
                stable_sig, stable_since = sig, now
                if unsettled_since is None:
                    unsettled_since = now
            if (now - stable_since < settle
                    and now - unsettled_since < SETTLE_MAX):
                time.sleep(0.4)
                continue
            if now - stable_since < settle:
                # 有回复候选、但文本一直在变：判定为 UI 噪声，按当前内容交卷并标记。
                state["settle_basis"] = "capped"
                state["warnings"] = ["text_never_settled"]
            else:
                state["settle_basis"] = "text_stable"
            unsettled_since = None
            # 从最新的那条往回试：复制到"我们自己的提问"就换下一个（提问气泡上也有复制键）
            copied_prompt = False
            copy_error = None
            # B17：这一批复制键刚刚已经试过、只复制到提问——说明回答的动作条还没
            # 出来（dsh 实测：边生成边判，第一版能复制的只有提问，于是直接判死）。
            # 条数没变就别再点一遍，等它变（或等过 grace）再来。
            if (prompt_only is not None and prompt_only[0] == len(view.copies)
                    and time.monotonic() - prompt_only[1] < PROMPT_COPY_GRACE
                    and time.monotonic() < deadline):
                time.sleep(PROMPT_COPY_POLL)
                continue
            for node in candidates:
                try:
                    answer = conversation.copy(node)
                except RuntimeError as exc:
                    # 点空了（动作条刚出现、坐标还在动画里）：不当成终局，
                    # 和「只复制到提问」一起走下面的有界等待。
                    copy_error = exc
                    continue
                if textcmp.equal(prompt, answer):
                    copied_prompt = True
                    continue
                out = {**state, "ok": True, "stage": "answered", "reply": answer,
                       "basis": "new_user_message_and_reply_control"}
                # 诊断（不改 ok / stage）：交卷时输入框里还是我们这段文字，
                # 说明端可能是在中途收手的。opencode 正常每轮结束也会回填，
                # 所以这里**只标记、不否决**。
                if facts.echo:
                    out["warnings"] = ["composer_echo_after_answer"]
                return out
            if copied_prompt or copy_error is not None:
                # 还没到点就再给一次机会：只要还在生成、或者刚试过的这一批
                # 复制键还没变，就继续等（有界）——宁可多等，也不把"动作条还没
                # 渲染出来 / 刚渲染出来点空了"当成终局。
                if prompt_only is None or prompt_only[0] != len(view.copies):
                    prompt_only = (len(view.copies), time.monotonic())
                    time.sleep(PROMPT_COPY_POLL)
                    continue
                if (view.busy or time.monotonic() - prompt_only[1] < PROMPT_COPY_GRACE) \
                        and time.monotonic() < deadline:
                    time.sleep(PROMPT_COPY_POLL)
                    continue
                if copy_error is not None:
                    raise copy_error
                return {**state, "stage": "copied_prompt",
                        "hint": "复制到的是提问本身，不是回答"}   # 宁可失败也不冒充回答
        # 第6项：stage 仍是 timeout（兼容旧调用方），另给 timeout_kind 区分
        if busy_ignored_once:
            kind = "stalled_busy"          # busy 误报已忽略，但到点仍未交出可读回复
        else:
            kind = ("still_generating"
                    if state["sent"] and (last_busy or stable_sig is not None) else "no_reply")
        out = {**state, "stage": "timeout", "timeout_kind": kind,
               "diagnostics": {"reads": hb_reads, "copies_max": copies_max,
                               "busy_reads": busy_reads, "accepted": bool(accepted),
                               "evidence_seen": evidence_seen}}
        if last_signals:
            out["reject_facts"] = dict(last_signals)
        return out
    except SendUnavailable as exc:
        # 一个字都没发出去。放在 `RuntimeError` 之前接：它是 RuntimeError 子类，
        # 落到下面那行会被压成 `interrupted`，"端不接受输入"就丢了。
        return {**state, "stage": "send_unavailable", "submission": "not_sent",
                "sent": False, "present": getattr(exc, "present", 0),
                "hint": str(exc)}
    except ForegroundLost as exc:
        return {**state, "stage": "user_took_over", "hint": str(exc)}
    except TabUnbound as exc:
        # 认不出"哪个标签是我的"：**不猜**，如实报（它是 RuntimeError 子类，必须抢在前面）
        return {**state, "stage": getattr(exc, "stage", "tab_unbound"),
                "submission": "not_sent", "sent": False,
                "hint": str(exc), **(getattr(exc, "facts", None) or {})}
    except (RuntimeError, COMError) as exc:
        return {**state, "stage": "interrupted", "hint": str(exc)}


# ---------------------------------------------------------------- 提交账本（T2-1）
#: "没发出去、且有可信证据" —— 放行重试。
_RETRYABLE_STAGES = frozenset({
    "input_mismatch", "draft_present", "concurrent_ui", "user_took_over",
    "tier_unconfirmed", "session_unproven", "conversation_busy",
    # R11：发送键存在但全部不可用 —— 一个字都没发出去，允许重试。
    "send_unavailable",
})

#: "重试没有意义" —— 一旦命中就永久拦住同一 (endpoint, prompt)。
#:
#: 2026-11-07（R11）：原来的 `{"login_required","quota","limit","unavailable"}` 是**死值**：
#: 全仓没有任何发射点（`_backup_batch2/3/7` 三份快照逐字相同可证）。
#: 按原因命名却拿不到原因，等于对读代码的人撒谎，也让"注册即生效"的假象
#: 盖住了真正的缺口。现在只收**结构事实**能证明的那一条。
#:
#: 任何按原因命名的 stage 想回来，必须先同时满足三条：
#:   ① 该端有实测结构证据  ② 有真实发射点
#:   ③ 通过 tests/test_r11_reject_signals.py 的防死值测试
#: 否则不许加 —— 这条规矩是被上面那四个值教出来的。
_TERMINAL_STAGES = frozenset({
    "submit_refused",
})


class _Ledger:
    """把 `ask()` 的一次调用接到提交账本上。

    **只在 `purpose` 给定时启用。** 现有调用方（尤其 1023 条既有测试）绝大多数
    不带 purpose，行为必须一个字节不变。

    挂在这里而不是挂五个 `*_use.py`，是因为 chatgpt / deepseek / codex / dsh /
    opencode **五个端全部汇聚到 `conversation.ask`**（前两个经 `flow.ask`）。
    改一处覆盖五端，以后新端复用 `ask` 也自动被覆盖。
    """

    def __init__(self, conversation, prompt, purpose):
        import os
        from . import submissions          # 局部导入：避免模块级循环依赖

        self._sub = submissions
        self.prompt = prompt
        self.purpose = purpose
        site = getattr(conversation, "site", None)
        self.site = site.strip() if isinstance(site, str) and site.strip() else None
        self.enabled = bool(purpose) and self.site is not None
        self.root = None
        self.scope = None
        #: B24：本轮是否**成功落过账**。只有落过账，收尾才有资格判定这一轮的成败。
        #: 没落账却写终态，会把上一轮已经 accepted 的记录改写成"没发出去"。
        self.reserved = False
        self.job_id = (os.environ.get("BRIDGE_JOB_ID") or "").strip() or None
        self.request_id = (os.environ.get("BRIDGE_REQUEST_ID") or "").strip() or None

    # ------------------------------------------------------------ 提交前
    def adopt(self, written=None):
        """门禁已经替本调用落过账（`pre_send` 回报 `reserved=True`）。

        **不再读 pending**——再读一次就会把刚写的那条 submitting 当成上一次，
        把合法首发拦掉（B26）。但要核对这条预约**是不是本次调用的**：
        只信门禁的布尔值，过期回执会被当成本次预约，终态写账就会改掉旧记录。
        """
        scope = None
        if written is not None:
            try:
                scope = self._sub.scope_from_view(written, site=self.site)
            except Exception:               # noqa: BLE001 - 读不到身份就不比对
                scope = None
        owned = self._sub.adopt_owned(self.site, self.prompt, scope=scope, root=self.root)
        if not owned.get("ok"):
            return {"ok": False, "stage": "tier_unconfirmed",
                    "reason": owned.get("reason"), "code": tier_guard.TierUnconfirmed.code,
                    "submission": "unknown", "sent": False,
                    "hint": "预约不属于本次调用：不点击、不记账"}
        self.reserved = True
        return None

    def reserve(self, written):
        """落账。返回 None = 放行；返回 dict = 就地拒发（原样交回调用方）。"""
        sub = self._sub
        try:
            prior = sub.pending(self.site, self.prompt, root=self.root)
        except Exception:                   # noqa: BLE001 - 账本读不出来按"状态不明"
            prior = {"state": "unknown"}
        if prior:
            out = sub.refusal(prior)
            out["prior_submission"] = (sub.normalize(prior) or {}).get("v2_state")
            return out
        # 身份：拿刚写完的那份视图（与 gemini/grok 的写法一致）
        try:
            self.scope = sub.scope_from_view(written, site=self.site)
        except Exception:                   # noqa: BLE001 - 读不到就留空，恢复时 fail-closed
            self.scope = None
        try:
            sub.reserve(self.site, self.prompt, root=self.root, scope=self.scope,
                        job_id=self.job_id, request_id=self.request_id)
        except sub.TierUnconfirmed as exc:
            return {"ok": False, "stage": "interrupted", "sent": None,
                    "submission": "unknown", "reason": exc.reason,
                    "hint": str(exc)}
        except OSError:
            return {"ok": False, "stage": "interrupted", "sent": None,
                    "submission": "unknown",
                    "reason": "submission_state_unwritable",
                    "hint": "无法持久化提交状态，已拒发"}
        self.reserved = True                # B24：落过账，收尾才有资格写这一轮的终态
        return None

    # ------------------------------------------------------------ 收尾
    def finish(self, result):
        sub = self._sub
        stage = result.get("stage")
        try:
            if stage == "already_answered":
                self._mark_existing_l3(result)
                return
            if stage in _TERMINAL_STAGES:
                sub.fail(self.site, self.prompt, state=sub.STATE_FAILED_TERMINAL,
                         subtype=stage, root=self.root,
                         evidence={"stage": stage, "hint": result.get("hint")})
                return
            # B24：这一轮根本没走到 `reserve`（busy / 草稿没清 / 身份没证成…），
            # 就没资格给账本写"本轮失败"。否则会把上一轮已 accepted 的记录
            # 改写成 failed_retryable，而 pending() 不拦可重试态 —— 同一提问能再发一次。
            if stage in _RETRYABLE_STAGES and not self.reserved:
                return
            payload = dict(result)
            if stage in _RETRYABLE_STAGES and payload.get("sent") is not True:
                payload.setdefault("submission", "not_sent")
                payload["sent"] = False
            sub.finish(self.site, self.prompt, payload, root=self.root,
                       scope=self.scope)
        except Exception:                   # noqa: BLE001
            # 账本写失败**绝不影响收发结果**：用户已经把消息发出去了，
            # 不能因为记不上账就让整次调用失败。
            pass

    def _mark_existing_l3(self, result):
        """already_answered：**没有记录就不建记录**（那等于补造一次外部提交）。"""
        sub = self._sub
        if not sub.read_state(self.site, self.prompt, root=self.root):
            return
        sub.mark_level(self.site, self.prompt, "L3",
                       {"basis": "already_answered",
                        "chars": len(result.get("reply") or "")},
                       root=self.root)

    def fail_on_exception(self):
        """调用过程中抛了没被接住的异常：状态不明，按 unknown 记（不重发）。"""
        try:
            self._sub.finish(self.site, self.prompt,
                             {"stage": "interrupted"}, root=self.root)
        except Exception:                   # noqa: BLE001
            pass


def ask(conversation, prompt, timeout=180.0, pre_send=None, plain=False,
        watch=None, purpose=None):
    """`_ask_impl` 的账本包装层。

    `purpose` 给了才记账（T2-1）；不给就等于原来的 `ask`，一个字节都不变。

    为什么用包装而不是在 19 个 return 点上各插一句：漏一个就是"发出去了但没记账"，
    而这种漏洞在测试里很难被抓到——统一收口才可靠。
    """
    ledger = _Ledger(conversation, prompt, purpose)
    if not ledger.enabled:
        try:
            return _ask_impl(conversation, prompt, timeout, pre_send, plain, watch)
        finally:
            # B11：命令行直跑时也要给最后那一帧留个终态（服务端会读走再删）
            heartbeat.beat("finished", force=True)
    try:
        result = _ask_impl(conversation, prompt, timeout, pre_send, plain, watch,
                           ledger=ledger)
    except BaseException:
        ledger.fail_on_exception()
        heartbeat.beat("finished", force=True, outcome="interrupted")
        raise
    ledger.finish(result)
    heartbeat.beat("finished", force=True, outcome=result.get("stage"))
    return result
