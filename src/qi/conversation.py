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

from . import ui
from .screen import ForegroundLost


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


def visible_editor(root, site):
    editors = [n for n in root.walk() if n.kind == "EditControl"
               and n.control.IsEnabled and not n.control.IsOffscreen]
    if site == "grok":
        editors += [n for n in root.walk() if n.kind == "ComboBoxControl"
                    and n.name in ("向 Grok 提任何问题", "Ask Grok anything")]
    # Search boxes have their own names and are outside the message composer.
    editors = [n for n in editors if not any(s in n.name.lower() for s in ("搜索", "search"))]
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
        """点进输入框并打字。元素失效就重新读一版再试，最多三次。"""
        error = None
        for _ in range(3):
            try:
                ui.click(self.screen, view.editor)
                self.screen.type_text(prompt)
                return
            except COMError as exc:
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
    wanted = ui.canonical(prompt)
    found = []

    def visit(node):
        if node.kind in ("EditControl", "ComboBoxControl", "ButtonControl", "MenuControl"):
            return
        if ui.canonical(ui.text_of(node)) == wanted:
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


def ask(conversation, prompt, timeout=180.0):
    """Timeout is a single budget. A submitted/unknown turn is never retried."""
    is_empty = getattr(conversation, "empty", composer_empty)
    state = {"ok": False, "submission": "not_sent", "sent": False}
    deadline = time.monotonic() + timeout
    try:
        before = conversation.read()
        if before.busy:
            return {**state, "stage": "conversation_busy"}
        if not is_empty(before):
            return {**state, "stage": "draft_present", "hint": "当前输入框已有草稿"}
        sent_prompts = len(prompt_nodes(before, prompt))
        baseline = len(before.copies)
        anchor_path = path_of(before.url)

        conversation.write(before, prompt)
        written = conversation.read()
        if ui.canonical(written.value) != ui.canonical(prompt):
            return {**state, "stage": "input_mismatch"}
        state.update(submission="unknown", sent=None)
        conversation.submit(written)

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
                    answer = conversation.copy_at(*match.center)
                    if ui.canonical(answer) != ui.canonical(prompt):
                        return {**state, "ok": True, "stage": "answered", "reply": answer,
                                "basis": "new_user_message_and_screen_copy"}
                time.sleep(0.4)
                continue
            # 从最新的那条往回试：复制到"我们自己的提问"就换下一个（提问气泡上也有复制键）
            copied_prompt = False
            for node in candidates:
                answer = conversation.copy(node)
                if ui.canonical(answer) == ui.canonical(prompt):
                    copied_prompt = True
                    continue
                return {**state, "ok": True, "stage": "answered", "reply": answer,
                        "basis": "new_user_message_and_reply_control"}
            if copied_prompt:
                return {**state, "stage": "copied_prompt",
                        "hint": "复制到的是提问本身，不是回答"}   # 宁可失败也不冒充回答
        return {**state, "stage": "timeout"}
    except ForegroundLost as exc:
        return {**state, "stage": "user_took_over", "hint": str(exc)}
    except (RuntimeError, COMError) as exc:
        return {**state, "stage": "interrupted", "hint": str(exc)}
