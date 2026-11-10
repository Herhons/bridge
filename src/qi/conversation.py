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

import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from comtypes import COMError

from . import guard, human, sendgate, textcmp, tier_guard, ui
from .screen import ForegroundLost, Screen


class UserTookOver(ForegroundLost):
    """发送前发现用户动了键鼠：没点发送就让位。（2026-10-30 第3项）"""


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
    def __init__(self, screen, site, empty=None, images=None):
        self.screen = screen
        self.site = site
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
        control, url = ui.document(self.screen.target_hwnd, self.site)
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
        return View(url, root, editor, value, busy, copies)

    def write(self, view, prompt):
        """点进输入框，键入前几个字符激活编辑器，其余走剪贴板粘贴（CARD-09f 第 5 条）。

        不再逐键键入整段提问：行首 `#`/`* `/`1. ` 会被编辑器当 Markdown 渲染掉标记，
        粘贴走 paste 事件，只插文本节点、不做 Markdown 转换。元素失效就重读一版再试。
        """
        error = None
        for _ in range(3):
            try:
                ui.click(self.screen, view.editor)
                prefix = prompt[:PREFIX_CHARS]
                rest = prompt[PREFIX_CHARS:]
                if prefix:
                    self.screen.type_text(prefix)
                if rest:
                    self._paste_via_clipboard(rest)
                # 打字/粘贴本身是 SendInput，也会刷新"最后输入时间"；记下打完那一刻，
                # 发送前时间变了才算用户动过（直接用 user_active 会被自己的输入误触发）
                time.sleep(0.15)
                self._input_mark = guard.last_input_tick()
                return
            except (RuntimeError, COMError) as exc:
                error = exc
                time.sleep(0.3)
                view = self.read()
        raise error

    def _paste_via_clipboard(self, text):
        """把 text 写入剪贴板（用序列号确认确实更新）再 Ctrl+V；结束恢复原剪贴板。

        CARD-09f 第 5 条：粘贴前保存原剪贴板；写入后按剪贴板序列号确认内容真的
        更新了再 Ctrl+V；无论成败都要恢复原剪贴板（恢复失败也尽力）。
        序列号没变 = 剪贴板没更新 → 不粘贴、直接报错。
        """
        previous = self.screen.clipboard()
        seq_before = self.screen.clipboard_seq()
        try:
            self.screen.set_clipboard(text)
            seq_after = self.screen.clipboard_seq()
            if (seq_before is not None and seq_after is not None
                    and seq_after == seq_before):
                raise RuntimeError("剪贴板序列号没变：剪贴板未更新，不粘贴")
            self.screen.hotkey("ctrl", "v")
            time.sleep(0.35)
        finally:
            try:
                self.screen.set_clipboard(previous)
            except Exception:                # noqa: BLE001 - 恢复失败也要尽力吞掉
                pass

    def clear(self, view):
        """清空输入框：点进去 → Ctrl+A → Delete（CARD-09e 第 4/5 条复用）。"""
        ui.click(self.screen, view.editor)
        self.screen.hotkey("ctrl", "a")
        self.screen.press("delete")
        time.sleep(0.2)

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
            buttons = [n for n in view.root.walk() if n.kind == "ButtonControl"
                       and n.name in SEND and n.control.IsEnabled]
            if len(buttons) == 1:
                ui.click(self.screen, buttons[0])
                return
            if len(buttons) > 1:
                raise RuntimeError(f"可用发送按钮: 找到 {len(buttons)} 个控件")
            if attempt == 3:
                raise RuntimeError("可用发送按钮: 找到 0 个控件")
            time.sleep(0.5)
            view = self.read()

    def copy(self, node):
        return self.copy_at(*node.center) if hasattr(node, "center") else self._copy_node(node)

    def _copy_node(self, node):
        rect = node.control.BoundingRectangle
        return self.copy_at((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

    def copy_at(self, x, y):
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
            deadline = time.monotonic() + 5
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


def composer_empty(view) -> bool:
    """输入框里没有用户输入。

    空 contenteditable 的 TextPattern 会返回占位文字，而且和控件名不是同一个串
    （Gemini：name「为 Gemini 输入提示」/ 文本「问问 Gemini」；Grok 的占位还轮播）。
    结构判据：值恰好等于**输入框框内**渲染出来的那个文本节点（它是输入框的兄弟节点，
    不是输入框的后代——有内容时文字在后者里面）。
    """
    text = ui.canonical(view.value)
    if not text:
        return True
    if text in {ui.canonical(p) for p in KNOWN_PLACEHOLDERS}:
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


def _rewrite_plain(conversation, prompt):
    """清空后改用粘贴再写一次；任何异常都当作"没成"（返回 None）。

    只在"打字版本核对不上"时才走这条路——正常一轮不多花一次剪贴板往返。
    """
    try:
        view = conversation.read()
        conversation.clear(view)
        conversation.paste(view, prompt)
        time.sleep(0.2)
        return conversation.read()
    except (RuntimeError, COMError):
        return None


def _existing_reply(conversation, view, prompt):
    """对话里已有「本提问」且它之后有回复时，只读返回该回复正文；否则 None。

    CARD-09f 第 6 条：中断后重跑、页面其实已答完时，绝不能重发。
    判据：认领到本提问（`prompt_nodes`），并且它后面有回复复制键（`view.copies`）。
    从最新一条往回试，复制到「提问本身」就跳过——宁可读不到也不冒充回答。
    """
    prompts = prompt_nodes(view, prompt)
    if not prompts:
        return None
    nodes = list(view.root.walk())
    prompt_at = {i for i, n in enumerate(nodes) if n in prompts}
    for node in reversed(view.copies):
        try:
            at = nodes.index(node)
        except (ValueError, COMError):
            continue
        if any(at > i for i in prompt_at):
            try:
                text = conversation.copy(node)
            except (RuntimeError, COMError):
                continue
            if text and not textcmp.equal(prompt, text):
                return text
    return None


def _clear_draft_verified(conversation, view, is_empty) -> bool:
    """清空输入框并读回为空（CARD-09f 第 3 条）。读回为空返回 True，否则 False。"""
    try:
        conversation.clear(view)
        recheck = conversation.read()
        return bool(is_empty(recheck))
    except (RuntimeError, COMError):
        return False


def ask(conversation, prompt, timeout=180.0, pre_send=None, plain=False, watch=None):
    """Timeout is a single budget. A submitted/unknown turn is never retried.

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
        before = conversation.read()
        if before.busy:
            return {**state, "stage": "conversation_busy"}
        # 第 6 条：发送前先看对话里是否已有「本提问」且它之后有回复（防重发）。
        # 中断后重跑、页面其实已答完时，绝不重发，只读已有回复返回 already_answered。
        existing = _existing_reply(conversation, before, prompt)
        if existing:
            # 第 6 条：已提交后提问被回填进输入框时，要清掉并读回为空（失败也如实记，
            # 不冒充"已清"）。already_answered 命中即命中，清草稿只是顺手收尾。
            cleared = None
            if not is_empty(before):
                cleared = _clear_draft_verified(conversation, before, is_empty)
            return {**state, "ok": True, "stage": "already_answered", "reply": existing,
                    "submission": None, "sent": False, "basis": "already_answered",
                    "cleared_draft": cleared}
        if not is_empty(before):
            return {**state, "stage": "draft_present", "hint": "当前输入框已有草稿"}
        sent_prompts = len(prompt_nodes(before, prompt))
        baseline = len(before.copies)
        anchor_path = path_of(before.url)

        # 第 3 条：写入之前记页面指纹，写入之后、发送之前再比一次；
        # 前台标签/文档被换走就立即停手、清草稿、以 concurrent_ui 退出。
        # 只在真机（screen 是真 Screen 实例）时做——离线假件的 screen 是 mock/假件，
        # 不参与并发检测。
        screen = getattr(conversation, "screen", None)
        site = getattr(conversation, "site", None)
        stamp_before_write = (sendgate.page_stamp(screen, site)
                              if isinstance(screen, Screen) and isinstance(site, str)
                              else None)

        conversation.write(before, prompt)
        written = conversation.read()
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
        if pre_send is not None:
            try:
                door = pre_send() or {}
            except (tier_guard.TierUnconfirmed, human.HumanCheck) as exc:
                return {**exc.as_result(), "submission": "not_sent", "sent": False}
            stamp = door.get("stamp")
            if stamp is not None:
                verdict = sendgate.page_confirm(conversation.screen, conversation.site, stamp)
                if not verdict["ok"]:
                    return {**state, "stage": "tier_unconfirmed",
                            "reason": "page_changed", "code": tier_guard.TierUnconfirmed.code,
                            "changed": verdict["changed"], "page": verdict["now"],
                            "hint": "档位复核之后页面发生了导航/新对话/窗口切换，"
                                    "按卡面拒发；请重新 choose 再发"}
        try:
            conversation.submit(written)
        except UserTookOver as exc:
            return {**state, "submission": "not_sent", "sent": False,
                    "stage": "user_took_over", "hint": str(exc)}
        # 第5项：同一个候选连续 settle 秒（两次读取）没变、且期间不忙才复制
        settle = getattr(conversation, "settle", 1.5)
        stable_sig = stable_since = None
        last_busy = False

        accepted = False
        read_failures = 0
        while time.monotonic() < deadline:
            try:
                view = conversation.read(attempts=2, delay=0.4)
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
            if watch is not None:
                early = watch(view)
                if early:
                    return {**state, **early}
            prompts = prompt_nodes(view, prompt)
            if len(prompts) <= sent_prompts:
                if accepted:
                    return {**state, "stage": "conversation_changed"}
                time.sleep(0.4)
                continue
            if not accepted:
                if not is_empty(view):
                    time.sleep(0.4)
                    continue
                accepted = True
                state.update(submission="accepted", sent=True)
            if path_of(view.url) != anchor_path:
                # 地址变了（首轮新建会话）：以这一刻为基线，已有回复不算本轮
                anchor_path = path_of(view.url)
                baseline = len(view.copies)
                time.sleep(0.4)
                continue
            # 有图片兜底的端（Grok）动作条不进控件树，copies 永远是空，不能拿条数当门
            if view.busy or (len(view.copies) <= baseline and not getattr(conversation, "images", None)):
                stable_sig = None
                time.sleep(0.4)
                continue
            ordered = list(view.root.walk())
            prompt_at = {i for i, n in enumerate(ordered) if n in prompts}
            candidates = []
            for node in reversed(view.copies[baseline:]):
                try:
                    if not node.control.IsEnabled:
                        continue
                    at = ordered.index(node)
                except (ValueError, COMError):
                    continue
                if any(at > i for i in prompt_at):
                    candidates.append(node)
            if not candidates:
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
            sig, now = ("copies", len(view.copies), len(candidates)), time.monotonic()
            if sig != stable_sig:
                stable_sig, stable_since = sig, now
            if now - stable_since < settle:
                time.sleep(0.4)
                continue
            # 从最新的那条往回试：复制到"我们自己的提问"就换下一个（提问气泡上也有复制键）
            copied_prompt = False
            for node in candidates:
                answer = conversation.copy(node)
                if textcmp.equal(prompt, answer):
                    copied_prompt = True
                    continue
                return {**state, "ok": True, "stage": "answered", "reply": answer,
                        "basis": "new_user_message_and_reply_control"}
            if copied_prompt:
                return {**state, "stage": "copied_prompt",
                        "hint": "复制到的是提问本身，不是回答"}   # 宁可失败也不冒充回答
        # 第6项：stage 仍是 timeout（兼容旧调用方），另给 timeout_kind 区分
        kind = "still_generating" if state["sent"] and (last_busy or stable_sig is not None) else "no_reply"
        return {**state, "stage": "timeout", "timeout_kind": kind}
    except ForegroundLost as exc:
        return {**state, "stage": "user_took_over", "hint": str(exc)}
    except (RuntimeError, COMError) as exc:
        return {**state, "stage": "interrupted", "hint": str(exc)}
