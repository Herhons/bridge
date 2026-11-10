"""grok 网页版：提问 → 收回答；--read 只读最新回复。

用法：
    python grok_use.py "只回复两个字：收到"
    python grok_use.py "..." --continue
    python grok_use.py --read                 ← 只读当前标签页最后一条回复，不发送
    python grok_use.py --read --tab 2

本端要点：
- 占位文案是**轮播**的（"将文件夹拖放到聊天中" / "切换到构建模式以创建应用" …），
  不能当锚点。锚点用左下角固定的「+」，按偏移 (250,-27) 点进文字行。
- 空输入时右下角是**语音**按钮（浅蓝波形）；输入文字后才变成发送箭头，
  控件树里它的名字是「提交」（2026-11-01 实测），发送键优先按名字点。
- 侧栏「聊天」＝新对话；侧栏收起时先点右上角的侧栏开关。

2026-11-01 施工（CARD-WEB2）。10:22 那轮 `grok_use --new` 报
`stage=input_mismatch`，日志里唯一一句诊断是「没找到 grok_send_on（最高 0.490 / 阈值 0.95）」——
模板分数低到 0.49，说明图根本对不上（Chrome 缩放 125%、窗口在左侧竖屏、坐标是负的），
这条路上再调阈值也没有意义。改成：

1. **发送键 UIA 优先**：按可访问性名称找「提交/发送」（`SEND`），点真按钮；
   找不到才回落模板，且模板按 (1.0, 0.8, 1.25) 三个缩放各匹一次。
2. **完成判据也走控件树**：本端动作条（复制响应 / Like / Dislike / 重新生成）
   其实在树里（旧代码以为只在画面上，`screen_reply=True` 就是在赌这一点，
   10:2x 那次就是赌输了：图像兜底没匹上，回复明明写完了也判 no_reply）。
   判据改成「我们这条提问之后的回复 + 自带动作条 + 文本稳定 3 秒 + 页面不忙」。
3. **`--read`**：只读，不发送、不点任何按钮，输出格式和 `gemini_use --read` 一致
   （`stage=read`，带 reply / replies / url）。
4. **认领不到提问时也收**：回复条数比发送前多，且最新那条已收尾，同样算本轮；
   超时前最后再读一次页面，读到就按成功返回。

2026-11-02 施工（CARD-06b）。发送链路改成"写入 → 复核 → 点击"：

- 页面缩放预检（非 100% 只对浏览器发 Ctrl+0；仍不是 100% 退出 `zoom_not_100`）；
- 写完之后、点发送之前才过档位闸：读不回芯片 → `tier_unreadable`（退出码 5），
  读到但与回执不符 → `tier_drift`，**use 绝不自己点菜单重选**；
- 复核之后立刻再比一次页面指纹（URL / 文档 runtime id / 前台窗口），
  变了就拒发（`reason=page_changed`）；
- 人机验证 / Unknown error → `human_check`，不点、不重试；
- `input_mismatch` 的比对走归一（行首列表标记、编辑器自动序号、空行），
  正文原样发送；失败统一留证到 `evidence\fail\`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

from comtypes import COMError

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import Library, console, failure, guard, heartbeat, human, sendgate, textcmp, tier_guard, ui, uilock  # noqa: E402
from qi import zoom as zoom_mod  # noqa: E402
from qi.browser import ensure_site  # noqa: E402
from qi.conversation import SEND, Conversation, UserTookOver  # noqa: E402
from qi.flow import Site  # noqa: E402
from qi.screen import ForegroundLost, Screen  # noqa: E402

SITE = Site(
    name="grok",
    title_hint="Google Chrome",
    composer="grok_composer",
    send="grok_send_on",
    actions="grok_actions",
    copy="grok_copy",
    composer_offset=(250, -27),
    newchat="grok_newchat",
    sidebar="grok_sidebar",
    send_mode="exists",
    screen_reply=True,   # 保留旧配置：本文件已不靠它判定，见下方 scan()
    send_threshold=0.90,
)

REQUIRED = ["grok_composer", "grok_send_on", "grok_actions", "grok_copy"]

# 气泡标签。页面是中文，英文留一份给换语言时兜底。
PROMPT_LABELS = ("你", "You")
REPLY_LABELS = ("Grok", "Assistant", "Grok 说", "Grok says")

# 动作条上的按钮（提问条是 [Edit, 复制]，回答条是 [复制响应, 创建共享链接, Like, …]）
BAR = {"Edit", "编辑", "复制", "复制提示", "复制响应", "Copy", "Copy response",
       "Like", "Dislike", "重新生成", "Regenerate", "More actions", "更多操作",
       "创建共享链接"}
# 只有回答动作条才有的按钮：出现即这一轮已经收尾。
DONE = {"复制响应", "Copy response", "Like", "Dislike", "重新生成", "Regenerate"}

# 「聊天」＝新对话。侧栏收起时它不在树里，先点侧栏开关再找。
NEWCHAT_NAMES = ("聊天", "新聊天", "新对话", "New chat", "New Chat")

# 模板兜底试的缩放：限定 0.8–1.25（CARD-06b 第 6 条），1.0 永远排第一。
# 超出这个范围的重采样既慢又容易误配。
TEMPLATE_SCALES = zoom_mod.SCALES

SETTLE = 3.0        # 回复文本稳定 3 秒才算收完
TAIL_MARKS = ("…", "...")
MIN_PREFIX = 12


@dataclass
class Block:
    """一段气泡。text 是**原样**正文（回复要原样交出去），比较走 key。"""

    label: str          # "prompt" / "reply"
    text: str
    at: int
    bars: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return ui.canonical(self.text)

    @property
    def closed_by(self):
        return self.bars[0] if self.bars else None


def _enabled(node) -> bool:
    try:
        return bool(node.control.IsEnabled)
    except (COMError, AttributeError):
        return bool(getattr(node, "enabled", True))


def _label(raw: str):
    """整串等于标签才算标签：「你说的对」不能当成新提问的开头。"""
    text = raw.strip()
    if text in PROMPT_LABELS:
        return "prompt"
    if text in REPLY_LABELS:
        return "reply"
    return None


def parse(root):
    """按文档序切块。返回 Block 列表。

    只认叶子文本节点：UIA 会把同一句文本在父节点上再挂一遍，父节点也收就会
    把每条回复读成两份。输入框一到就断块——用户刚打的字不算回复。

    真机结构（2026-11-01）：
        T: 你            ← 提问标签
        T: 只回复 OK-WEB2
        G: 你 / B: Edit / B: 复制      ← 提问动作条（Group 名字不算标签）
        T: Grok          ← 回答标签（只出现一次，末尾那个是 Group，不是文本）
        T: OK-WEB2
        G: Grok / B: 复制响应 / B: Like …
    """
    blocks: list[Block] = []
    cur = None
    last = None
    for at, node in enumerate(root.walk()):
        if node.kind in ("EditControl", "ComboBoxControl", "MenuControl"):
            cur = last = None
            continue
        if node.kind == "ButtonControl":
            name = ui.canonical(node.name)
            if name in BAR and cur is not None:
                cur.bars.append(name)
                cur, last = None, None
            continue
        if node.kind != "TextControl" or node.children:
            continue
        key = ui.canonical(node.name)
        if not key:
            continue
        label = _label(node.name)
        if label:
            if cur is not None and cur.label == label and cur.text.strip():
                # 同一作者的标签又来一次、且上一块已经有正文：这是尾部重复，不另起块
                cur = last = None
                continue
            cur = Block(label, "", at)
            blocks.append(cur)
            last = None
            continue
        if cur is not None:
            if key == last:
                continue
            cur.text += ("\n" if cur.text else "") + node.name
            last = key
    return blocks


def _squash(text: str) -> str:
    """整段重复两遍的收紧成一遍（真机上用户气泡会挂内外两层节点）。"""
    half = len(text) // 2
    if len(text) % 2 == 0 and half and text[:half] == text[half:]:
        return text[:half]
    return text


def prompt_matches(text: str, prompt: str) -> bool:
    """控件树里的这段文字是不是我们发出去的那条提问（容忍折叠截断）。"""
    # 归一后再比：页面会把行首「1.」吃掉，用 ui.canonical 会认领不到
    got, want = _squash(textcmp.normalize(text)), textcmp.normalize(prompt)
    if not got or not want:
        return False
    if got == want:
        return True
    for mark in TAIL_MARKS:
        if got.endswith(mark):
            got = got[: -len(mark)]
            break
    if len(got) < min(MIN_PREFIX, len(want)):
        return False
    return want.startswith(got)


@dataclass
class Scan:
    blocks: list = field(default_factory=list)

    def replies(self):
        return [b for b in self.blocks if b.label == "reply" and b.text.strip()]

    def prompt_at(self, prompt):
        hits = [i for i, b in enumerate(self.blocks)
                if b.label == "prompt" and prompt_matches(b.text, prompt)]
        return hits[-1] if hits else None

    def reply_after(self, at):
        if at is None:
            return None
        for i, b in enumerate(self.blocks):
            if i > at and b.label == "reply" and b.text.strip():
                return b
        return None

    def done(self, block) -> bool:
        """这条回复自带回答动作条 = 这一轮收尾了。

        不拿「页面上有动作条」当判据：续聊时上一轮的条一直在，那样第一秒就会
        判完成，读回来的是半截话。
        """
        return bool(DONE.intersection(block.bars))


def scan(root) -> Scan:
    return Scan(parse(root))


def draft_present(root) -> bool:
    """输入框里有草稿：空输入时右下角是语音键，没有「提交/发送」这个名字的按钮。"""
    return any(n.kind == "ButtonControl" and n.name in SEND and _enabled(n)
               for n in root.walk())


def _win(screen, site):
    return screen.window_rect(site.title_hint, on_secondary=site.on_secondary)


def new_chat_button(root):
    """页面上的「聊天」（＝新对话）。按名称找，不限角色（Chromium 会挂成 L/T/H）。

    注意别点到历史列表顶上那个同名的分区标题（它是个 Button），
    所以候选先按角色排序：链接 > 按钮 > 文字。
    """
    return ui.prefer_clickables(ui.named(root, NEWCHAT_NAMES))


def open_new_chat(screen, site, *, tries: int = 6, pause: float = 0.5) -> dict:
    """点新对话：UIA 优先，读不到就等一拍再读，侧栏收起时先开侧栏，最后回落模板。"""
    control, _url = ui.document(screen.target_hwnd, site.name)
    for attempt in range(tries):
        nodes = new_chat_button(ui.tree(control))
        if nodes:
            ui.click(screen, nodes[0])
            time.sleep(0.9)
            return {"ok": True, "stage": "clicked", "basis": "uia", "reads": attempt + 1}
        if attempt + 1 < tries:
            time.sleep(pause)

    # 侧栏收起时「聊天」不在树里：先把侧栏打开再找一次
    if site.sidebar:
        toggle = screen.find(site.sidebar, threshold=0.88, region=_win(screen, site),
                             scales=TEMPLATE_SCALES)
        if toggle is not None:
            screen.click(toggle)
            time.sleep(0.8)
            nodes = new_chat_button(ui.tree(control))
            if nodes:
                ui.click(screen, nodes[0])
                time.sleep(0.9)
                return {"ok": True, "stage": "clicked", "basis": "sidebar+uia"}

    match = screen.find(site.newchat, threshold=0.88, region=_win(screen, site),
                        scales=TEMPLATE_SCALES)
    if match is not None:
        screen.click(match)
        time.sleep(0.9)
        return {"ok": True, "stage": "clicked", "basis": "template",
                "score": round(match.score, 3)}

    score = "—" if getattr(screen, "last_best", None) is None else f"{screen.last_best:.3f}"
    where = screen.describe(_win(screen, site)) if hasattr(screen, "describe") else ""
    zoom = ui.page_zoom(getattr(screen, "target_hwnd", None))
    return {"ok": False, "stage": "newchat_not_found", "reads": tries,
            "hint": f"控件树里没有「聊天」（读了 {tries} 次，约 {tries * pause:.1f}s）；"
                    f"模板 {site.newchat} 最高 {score}（{where}"
                    f"{f'；页面缩放 {zoom}' if zoom else ''}）"}


def _submit(screen, site, conv, view) -> str:
    """点发送。**UIA 优先**（名字「提交/发送」），模板只作兜底（卡面第 1 项）。"""
    if guard.user_moved_since(getattr(conv, "_input_mark", None)):
        raise UserTookOver("发送前检测到键鼠输入，已让位，未点发送")
    for attempt in range(4):
        buttons = [n for n in view.root.walk()
                   if n.kind == "ButtonControl" and n.name in SEND and _enabled(n)]
        if len(buttons) == 1:
            ui.click(screen, buttons[0])
            return "uia"
        if len(buttons) > 1:
            raise RuntimeError(f"可用发送按钮: 找到 {len(buttons)} 个控件")
        if attempt == 3:
            break
        time.sleep(0.5)                 # 文字刚打进去时按钮可能还没变可点
        view = conv.read()
    match = screen.find(site.send, threshold=site.send_threshold, region=_win(screen, site),
                        scales=TEMPLATE_SCALES)
    if match is None:
        best = getattr(screen, "last_best", None)
        score = "—" if best is None else f"{best:.3f}"
        where = screen.describe(_win(screen, site)) if hasattr(screen, "describe") else ""
        zoom = ui.page_zoom(getattr(screen, "target_hwnd", None))
        raise RuntimeError(
            f"发送键没找到：控件名里没有可用的「提交/发送」，模板 {site.send} 最高 {score}"
            f"（阈值 {site.send_threshold}；{where}"
            f"{f'；页面缩放 {zoom}' if zoom else ''}）")
    screen.click(match)
    return "template"


def write_prompt(screen, conv, view, prompt: str, *, tries: int = 3):
    """把提问写进输入框，并**核对读回来的值**，不对就清空重来。

    10:22 那轮报的其实是 `input_mismatch`（不是找不到发送键）：点进输入框/打字
    偶尔落空，旧逻辑判一次就整轮作废。这里每次重读一版树再点，写歪了先
    Ctrl+A + Delete 清干净；三次还不对就原样返回，交给调用方报 input_mismatch。
    """
    written = None
    for attempt in range(tries):
        try:
            conv.write(view, prompt)
        except (RuntimeError, COMError):
            if attempt + 1 >= tries:
                raise
            time.sleep(0.4)
            view = conv.read()
            continue
        written = conv.read()
        if textcmp.equal(prompt, written.value):
            return written
        screen.hotkey("ctrl", "a")
        screen.press("delete")
        time.sleep(0.3)
        view = conv.read()
    return written


def _pick_reply(page: Scan, at, baseline: int):
    """本轮该读哪条回复：认领到提问就取它后面那条，否则只认「比发送前多出来的最新一条」。"""
    if at is not None:
        return page.reply_after(at)
    replies = page.replies()
    if len(replies) > baseline:
        return replies[-1]
    return None


def _already_answered(root, prompt: str):
    """发送前检查：对话里是否已有本提问之后的回复。有则返回回复正文，否则 None。

    CARD-09f 第 6 条：中断后重跑、页面其实已答完时，绝不重发，只读已有回复。
    """
    page = scan(root)
    at = page.prompt_at(prompt)
    if at is None:
        return None
    reply = page.reply_after(at)
    if reply is not None and reply.text.strip():
        return reply.text.strip()
    return None


def _clear_draft(conv, view) -> bool:
    """清空输入框并读回为空（CARD-09f 第 3/6 条）。读回为空返回 True，否则 False。"""
    try:
        conv.clear(view)
        recheck = conv.read()
        return (isinstance(getattr(recheck, "value", None), str)
                and textcmp.equal(recheck.value, "") and not draft_present(recheck.root))
    except (RuntimeError, COMError):
        return False


def _final_read(conv, prompt: str, baseline: int):
    """超时前最后再看一眼页面：读到本轮回复就按成功返回，不报 no_reply。"""
    try:
        view = conv.read(attempts=3, delay=0.4)
    except (RuntimeError, COMError):
        return None
    if view.busy:
        return None                      # 还在生成：半截话不能当答案
    page = scan(view.root)
    if len(page.replies()) <= baseline:
        return None                      # 没有新回复：那是上一轮的答案
    at = page.prompt_at(prompt)
    reply = _pick_reply(page, at, baseline)
    if reply is None or not reply.text.strip():
        return None
    result = {"ok": True, "stage": "answered", "submission": "accepted", "sent": True,
              "reply": reply.text.strip(), "basis": "final_read"}
    if at is None:
        result["attribution"] = "new_reply"
    if not page.done(reply):
        result["hint"] = "最后一次读取时回复动作条还没出现，按「页面已停止生成 + 有新回复」判定"
    return result


def _send_and_wait(screen, site, prompt: str, timeout: float,
                   purpose: str | None = None) -> dict:
    from qi import submissions
    state = {"ok": False, "submission": "not_sent", "sent": False}
    conv = Conversation(screen, site.name)
    before = conv.read()
    if before.busy:
        return {**state, "stage": "conversation_busy"}

    # 第 6 条：发送前先看对话里是否已有本提问之后的回复（防重发），只读不发送。
    # 必须放在 draft_present 之前：中断后重跑、页面已答完时，提问会被回填进输入框，
    # 先判 draft_present 会卡住，永远走不到 already_answered。
    already = _already_answered(before.root, prompt)
    if already:
        cleared = None
        if draft_present(before.root):
            cleared = _clear_draft(conv, before)
        return {**state, "ok": True, "stage": "already_answered", "reply": already,
                "submission": None, "sent": False, "basis": "already_answered",
                "cleared_draft": cleared}
    if purpose is not None:
        previous = submissions.pending("grok", prompt)
        if previous:
            return submissions.refusal(previous)
    if draft_present(before.root):
        return {**state, "stage": "draft_present", "hint": "当前输入框已有草稿"}
    baseline = len(scan(before.root).replies())

    # 第 3 条：写入之前记页面指纹，写入之后、发送之前再比一次。
    # 只在真机（screen 是真 Screen 实例）时做——离线假件的 screen 是 StubScreen，跳过。
    # B13b：写入 → 读回 → 指纹 → 门禁 → 落账 → 发送，**一整段**（中途放锁必被抢前台）
    with uilock.section('send:grok', timeout=uilock.SECTION_TIMEOUT):
        stamp_before_write = (sendgate.page_stamp(screen, site.name)
                              if isinstance(screen, Screen) else None)

        written = write_prompt(screen, conv, before, prompt)
        if not textcmp.equal(prompt, written.value):
            # 第 5 条：正文原样发送，只有比对走归一；hint 带第一处差异的行号和前后 20 字
            return {**state, "stage": "input_mismatch",
                    "hint": textcmp.describe(prompt, written.value) or
                            f"写进去的文本和提问对不上（{textcmp.lengths(prompt, written.value)}）"}
        state.update(submission="unknown", sent=None)
        # 第 3 条：发送阶段复核页面指纹，被换走 → 清掉半截文本 + concurrent_ui
        if stamp_before_write is not None:
            concurrent = sendgate.page_confirm(screen, site.name, stamp_before_write)
            if not concurrent["ok"]:
                cleared = _clear_draft(conv, written)
                return {**state, "stage": "concurrent_ui", "reason": "page_changed",
                        "changed": concurrent["changed"], "cleared_draft": cleared,
                        "submission": "not_sent", "sent": False}
        # 第 3 条：复核放在写入之后、点击之前；复核完再比一次页面指纹
        stamp = None
        if purpose is not None:
            try:
                door = sendgate.pre_send(screen, site.name, purpose, root=written.root)
                stamp = door.get("stamp")
            except (tier_guard.TierUnconfirmed, human.HumanCheck) as exc:
                return {**exc.as_result(), "submission": "not_sent", "sent": False}
            if stamp is not None:
                verdict = sendgate.page_confirm(screen, site.name, stamp)
                if not verdict["ok"]:
                    return {**state, "stage": "tier_unconfirmed", "reason": "page_changed",
                            "code": tier_guard.TierUnconfirmed.code,
                            "changed": verdict["changed"], "page": verdict["now"],
                            "hint": "档位复核之后页面发生了导航/新对话/窗口切换，按卡面拒发"}
        try:
            if purpose is not None:
                from qi import submissions

                # R5：把发送时所在的标签/文档身份一起写进记录（恢复要拿它比对）
                submissions.reserve("grok", prompt,
                                    scope=submissions.scope_from_view(
                                        written, site=site.name,
                                        hwnd=getattr(screen, "target_hwnd", None)))
            basis_of_submit = _submit(screen, site, conv, written)
        except UserTookOver as exc:
            return {**state, "submission": "not_sent", "sent": False,
                    "stage": "user_took_over", "hint": str(exc)}

    deadline = time.monotonic() + timeout
    accepted = False
    attribution = None
    at = None
    stable_text = stable_since = None
    last_busy = False
    read_failures = 0
    while time.monotonic() < deadline:
        try:
            view = conv.read(attempts=2, delay=0.4)
            heartbeat.beat_view("waiting", view)
        except (RuntimeError, COMError) as exc:
            read_failures += 1
            if read_failures >= 12:
                return {**state, "stage": "interrupted", "hint": str(exc)}
            time.sleep(1.0)
            continue
        read_failures = 0
        last_busy = view.busy
        page = scan(view.root)

        found = page.prompt_at(prompt)
        if found is not None:
            at = found
        if not accepted:
            if at is not None:
                accepted, attribution = True, "prompt"
                state.update(submission="accepted", sent=True)
            elif len(page.replies()) > baseline:
                # 认领不到提问但页面多了回复：同样算发出去了
                accepted, attribution = True, "new_reply"
                state.update(submission="accepted", sent=True)

        if accepted:
            if purpose is not None:
                from qi import submissions

                # R5：第一次判定 accepted 的那一页才是"回答所在会话"的身份，只写一次。
                submissions.finish("grok", prompt, state,
                                   scope=submissions.scope_from_view(
                                       view, site=site.name,
                                       hwnd=getattr(screen, "target_hwnd", None)))
            if draft_present(view.root):
                state["cleared_draft"] = _clear_draft(conv, view)
                if not state["cleared_draft"]:
                    return {**state, "stage": "interrupted", "reason": "draft_clear_failed"}
                continue
        reply = _pick_reply(page, at, baseline)
        if reply is None or len(page.replies()) <= baseline or view.busy:
            stable_text = stable_since = None
            time.sleep(0.4)
            continue
        if not page.done(reply):
            stable_text = stable_since = None
            time.sleep(0.4)
            continue
        if reply.key != stable_text:
            stable_text, stable_since = reply.key, time.monotonic()
            time.sleep(0.4)
            continue
        if time.monotonic() - stable_since < SETTLE:
            time.sleep(0.4)
            continue
        result = {**state, "ok": True, "stage": "answered", "reply": reply.text.strip(),
                  "basis": "tree_reply_bar_and_stable_text", "submit": basis_of_submit}
        notes = []
        if attribution == "new_reply":
            result["attribution"] = "new_reply"
            notes.append("提问没在控件树里认领到，按“比发送前多出来的最新回复”归属")
        if notes:
            result["hint"] = "；".join(notes)
        return result

    final = _final_read(conv, prompt, baseline)
    if final is not None:
        final["submit"] = basis_of_submit
        return final
    kind = "still_generating" if state["sent"] and (last_busy or stable_text) else "no_reply"
    return {**state, "stage": "timeout", "timeout_kind": kind}


def _prior_submission(prompt: str) -> dict | None:
    """只读地看一眼持久化提交记录（CARD-09f 返工）。不 reserve/finish，不写文件。"""
    from qi import submissions

    try:
        return submissions.pending("grok", prompt)
    except Exception:                       # noqa: BLE001 - 读记录失败 = 状态不明
        return {"state": "unknown", "error": "submission journal unreadable"}


def _journal_submission(prior) -> str:
    state = str((prior or {}).get("state") or "unknown").lower()
    return state if state in ("accepted", "answered", "unknown") else "unknown"


def _prior_turn_recovery(prompt: str, prior, *, tab, verbose, ctx: dict):
    """准备动作**之前**的只读恢复：不点新对话、不写、不发。

    续聊选中已有标签（`continue_session=True` 绝不打开首页），只读扫一遍控件树：
    这条提问在本页已经有回复就按 already_answered 交回，否则 None（按已提交拒发）。
    读不到文档时不在这里吞，由调用方保留 accepted/unknown 事实。

    2026-11-07（R5 / CE2）：先比发送时记下的标签/文档身份。缺身份、读不到、
    对不上都返回 None——"本页有一对同样的问答"不是归属证据。
    """
    from qi import submissions

    screen = Screen(verbose=verbose)
    hwnd = ensure_site("grok", tab=tab, continue_session=True)
    ctx.update({"screen": screen, "hwnd": hwnd})
    if hwnd is None or not screen.bind(hwnd):
        return None
    screen.tab_site = "grok"
    view = Conversation(screen, "grok").read(attempts=2, delay=0.4)
    current = submissions.scope_from_view(view, site="grok", hwnd=hwnd)
    matched, why = submissions.reconcile(submissions.recovery_scope(prior), current)
    ctx["identity"] = why
    if not matched:
        return None
    text = _already_answered(view.root, prompt)
    if not text:
        return None
    return {"ok": True, "stage": "already_answered", "reply": text,
            "submission": None, "sent": False, "basis": "prior_submission_recovery",
            "identity": "matched", "cleared_draft": None}


def ask(site, prompt: str, *, mode: str = "new", timeout: float = 180.0,
        verbose: bool = True, tab: int | None = None, purpose: str | None = None) -> dict:
    """发一条提问并等回复。准备阶段的异常交给 cli 报 preparation_failed。

    `purpose` 给了就在 `_send_and_wait` 里、**写入文本之后、点发送之前**过档位闸
    （CARD-06b 第 3 条）：校验回执 → 只读复核（模板优先，读不到即拒发）→
    原子消费 → 再比一次页面指纹。

    CARD-09f 返工：在**任何准备动作之前**先只读查持久化提交记录，避免 mode=new
    先把会话换掉，再用 newchat/读文档的失败把"已经 accepted"说成 not_sent。
    """
    started = time.monotonic()
    result: dict = {"ok": False, "submission": "not_sent", "sent": False}
    screen = None
    hwnd = None
    prior = _prior_submission(prompt)
    if prior is not None:
        state = _journal_submission(prior)
        ctx: dict = {}
        recovered = None
        try:
            recovered = _prior_turn_recovery(prompt, prior, tab=tab,
                                             verbose=verbose, ctx=ctx)
        except RuntimeError as exc:
            if hasattr(exc, "as_result"):          # UiDocsLost=6 / WindowMinimized=7
                result = {**exc.as_result(), "submission": state, "sent": False,
                          "prior_submission": state,
                          "hint": (f"{exc}；已有该提问的提交记录（{state}），"
                                   "按 CARD-09f 保持该状态，不重发")}
            else:
                result = {**result, "stage": "interrupted", "submission": state,
                          "prior_submission": state,
                          "hint": (f"只读恢复失败：{exc}；已有该提问的提交记录（{state}），"
                                   "保持该状态，不重发")}
        except Exception as exc:             # noqa: BLE001 - 恢复失败也必须保留既有事实
            result = {**result, "stage": "interrupted", "submission": state,
                      "prior_submission": state,
                      "hint": (f"只读恢复失败：{type(exc).__name__}: {exc}；"
                               f"已有该提问的提交记录（{state}），保持该状态，不重发")}
        if recovered is not None:
            recovered.update({"elapsed": round(time.monotonic() - started, 1),
                              "mode": mode, "prior_submission": state})
            return recovered
        screen, hwnd = ctx.get("screen"), ctx.get("hwnd")
        if result.get("submission") == "not_sent":
            from qi import submissions

            result = {**submissions.refusal(prior), "prior_submission": state,
                      "hint": (f"仓库里已有该提问的提交记录（{state}）："
                               "按 CARD-09f 先只读查答，确认前不重发；"
                               "要重发请人工核对后清掉 runs/submissions 里这条记录")}
            if ctx.get("identity"):
                result["identity"] = ctx["identity"]
                result["hint"] += f"；本次未采信当前页面（identity={ctx['identity']}）"
    else:
        screen = Screen(verbose=verbose)
        hwnd = ensure_site(site.name, tab=tab, continue_session=mode == "continue")
        if hwnd is None:
            result = {**result, "stage": "no_window"}
        elif not (screen.bind(hwnd) and screen.focus()):
            result = {**result, "stage": "page_not_front"}
        else:
            screen.tab_site = site.name
            try:
                # 页面缩放预检（第 6 条）：非 100% 先 Ctrl+0，仍不是就退出（stage=zoom_not_100）
                zoom_mod.ensure(screen, verbose=verbose)
                opened = open_new_chat(screen, site) if mode == "new" else {"ok": True}
                if not opened["ok"]:
                    result = {**opened, "submission": "not_sent", "sent": False}
                else:
                    result = _send_and_wait(screen, site, prompt, timeout, purpose=purpose)
            except zoom_mod.ZoomNot100 as exc:
                result = {**exc.as_result(), "submission": "not_sent", "sent": False}
            except ForegroundLost as exc:
                result = {**result, "stage": "user_took_over", "hint": str(exc)}
            except (RuntimeError, COMError) as exc:
                if hasattr(exc, "as_result"):          # UiDocsLost=6 / WindowMinimized=7
                    result = {**exc.as_result(), "submission": "not_sent", "sent": False}
                else:
                    result = {**result, "stage": "interrupted", "hint": str(exc)}
    result["elapsed"] = round(time.monotonic() - started, 1)
    result["mode"] = mode
    if not result.get("ok"):
        failure.record(site.name, result, screen=screen, hwnd=hwnd)
    return result


def read_last(*, tab: int | None = None, verbose: bool = True) -> dict:
    """只读当前标签页最后一条回复，不发送、不点任何按钮。

    输出格式和 `gemini_use --read` 一致：stage=read / reply / replies / url。
    """
    result: dict = {"ok": False, "submission": "not_sent", "sent": False}
    screen = Screen(verbose=verbose)
    try:
        hwnd = ensure_site("grok", tab=tab)
        if hwnd is None:
            return {**result, "stage": "no_window"}
        if not (screen.bind(hwnd) and screen.focus()):
            return {**result, "stage": "page_not_front"}
        screen.tab_site = "grok"
        view = Conversation(screen, "grok").read()
        found = scan(view.root).replies()
        if not found:
            return {**result, "stage": "no_reply", "url": view.url, "replies": 0}
        return {"ok": True, "stage": "read", "submission": None, "sent": None,
                "reply": found[-1].text.strip(), "replies": len(found), "url": view.url}
    except ForegroundLost as exc:
        return {**result, "stage": "user_took_over", "hint": str(exc)}
    except (RuntimeError, COMError) as exc:
        return {**result, "stage": "interrupted", "hint": str(exc)}


def cli(site, argv: list, *, extra_help: str = "") -> int:
    need, wait, force, argv = guard.parse_guard_args(list(argv))
    parser = argparse.ArgumentParser(prog=f"{site.name}_use.py", epilog=extra_help +
                                     "\n  --purpose   test=最低档 / review|accept=最高档"
                                     "（发送必填，无默认；要先用 *_choose 选出该档）")
    parser.add_argument("prompt", nargs="?", default=None, help="完整提问，可包含换行")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--new", action="store_true", help="新对话（默认）")
    modes.add_argument("--continue", dest="continuing", action="store_true")
    parser.add_argument("--read", action="store_true",
                        help="只读当前标签页最后一条回复，不发送（不需要提问）")
    parser.add_argument("--purpose", choices=("test", "review", "accept"), default=None,
                        help="test=最低档，review/accept=最高档（发送必填，无默认）")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--tab", type=int, choices=range(1, 10))
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if not args.read and not (args.prompt or "").strip():
        parser.error("提问不能为空")
    if args.read and args.prompt:
        parser.error("--read 是只读，不接受提问")
    # --read 不发送，不需要档位回执；真发送时 --purpose 必填（无默认）
    if not args.read and not args.purpose:
        parser.error("the following arguments are required: --purpose"
                     "（test/review/accept，无默认）")
    # 第 6 条：高档（review/accept）等待上限下限 900s，不得 300s 就判超时
    args.timeout = tier_guard.floor_timeout(args.purpose, args.timeout)
    if not guard.guard(need, wait, force):
        return 4
    try:
        if args.read:
            result = read_last(tab=args.tab)
        else:
            result = ask(site, args.prompt, mode="continue" if args.continuing else "new",
                         timeout=args.timeout, tab=args.tab, purpose=args.purpose)
    except RuntimeError as exc:
        result = {"ok": False, "stage": "preparation_failed", "submission": "not_sent",
                  "sent": False, "hint": str(exc)}
    from qi import exit_codes, runlog
    # 失败留证（第 9 条）：ask() 里已经带着 screen/hwnd 记过一次的，别再记一遍——
    # 第二次没有 screen，会把"窗口截图"换成"整屏截图"，还丢掉 hwnd/rect。
    if not result.get("ok") and not result.get("evidence"):
        failure.record(site.name, result)
    runlog.record(site.name, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else exit_codes.code_for(result.get("stage"))


def main() -> int:
    argv = list(sys.argv[1:])
    reading = "--read" in argv          # 只读模式：不发送，也不需要模板
    if not reading:
        lib = Library()
        need = list(REQUIRED)
        if "--new" in argv or not any(a.startswith("--") for a in argv):
            need.append("grok_newchat")
        print(lib.report(need))
        if lib.missing(need):
            return 3
    return cli(SITE, argv)


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.entry("grok_use", main))
