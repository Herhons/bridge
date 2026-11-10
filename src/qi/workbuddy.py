"""WorkBuddy 的选档与收发（按控件名 / UIA 树定位，不用模板）。

## 本端两个独占行为（CARD-06d）

1. **`--new` 之后模型会被重置。** WorkBuddy 每次新建任务都把模型换回默认档，
   而回执（`tier_guard`）里记的是 choose 读回来的那个。闸门会在发送前读回并拒发
   （`tier_drift` / 5），可这一轮就白跑了。所以新对话之后先读一次：
   与回执不符就**按回执重选**、再读一次；仍不符 → `stage=model_reset_after_new` / 5，
   **不发送**。注意这不是"use 自己切档"：切的是回执上那个档，只是把它按回去。

2. **复制键可能不写剪贴板。** 06b/06c 验收实测卡在「复制回复后剪贴板未更新」
   （275 秒，`evidence\\accept_06b06c_hy3_reply.txt`）。剪贴板没更新时改读 UIA 树
   （`reply_text`），输出里标 `read_via=uia`；两条都失败才报 `interrupted` 并写 runlog。
   回退逻辑放在 `ReplyConversation` 子类里——`Conversation` 是五端共用的，
   本端的问题不许改到别端头上。

## CARD-07 第 1 条：页面已有回复却报 no_reply

Hy4 preview 下等到超时还说 no_reply，可页面上回复早写完了。2026-11-02 在真机上
把两个原因都复现出来了，**都在共享判据里**（`conversation.py` 不许动，本端自己等）：

1. `conversation.is_reply_copy` 要求复制键旁边还有赞/踩/重做这类兄弟按钮。
   这个页面上的回复动作条是 **ImageControl**（不在 ButtonControl 里），
   树里唯一叫「复制」的按钮挂在**提问气泡**上。于是 `view.copies` 恒为 0，
   `ask()` 的"出现新的复制键"那条永远不成立。
2. 提问气泡在控件树里是**截断**的（实测长提问只留到第 60 个字左右），
   而 `prompt_nodes` 要求整串精确相等，认领不到 → `accepted` 一直是 False。

所以本端把等待接过来（`ask()`，见文件后半）：发送仍然交给共享 `ask()`（预算 0，
写完就返回，写入/闸门/提交的语义一个字节没变），等待用本端判据——停止生成按钮
消失 + 回复正文连续 `STABLE_READS` 轮不变，超时前最后再纯只读地读一次树。
读到就算成功并标 `read_via=uia`，读不到才报 no_reply。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import time

from comtypes import COMError

from . import desk, exit_codes as ec, guard, textcmp, tier_guard, ui
from .conversation import Conversation, ask as send_and_wait, visible_editor
from .screen import Screen
from .ui import UiDocsLost
from .window import WindowMinimized

# 新对话之后模型被重置、按回执重选也按不回来时的 stage（第 1 条）
MODEL_RESET_STAGE = "model_reset_after_new"


def open_app():
    hwnd = desk.ensure_app("workbuddy")
    if hwnd is None:
        raise RuntimeError("WorkBuddy 没有可操作的窗口")
    screen = Screen(verbose=False)
    screen.bind(hwnd)
    if not screen.focus():
        raise RuntimeError("WorkBuddy 窗口无法获得焦点")
    return screen


def controls(screen):
    doc, _ = ui.document(screen.target_hwnd, "workbuddy")
    return list(ui.tree(doc).walk())


def model_control(nodes):
    return ui.unique((n for n in nodes if n.kind == "ComboBoxControl"
                      and n.name.startswith("Select model:")), "WorkBuddy 模型控件")


def current_model(nodes):
    return model_control(nodes).name.removeprefix("Select model:").strip()


def model_row(node, *, require_row=False):
    """命中的标签 → 它所在的那一行（菜单项/列表项/按钮）。找不到就返回 None 或标签自己。"""
    group = node.parent
    for _ in range(6):
        if group is None or group.kind == "DocumentControl":
            break
        if group.kind in ("MenuItemControl", "ListItemControl", "ButtonControl",
                          "HyperlinkControl"):
            return group
        group = group.parent
    return None if require_row else node


def model_rows(nodes):
    """同名标签里挑出"真的能点的那一行"。

    2026-11-02 实测（`--model Hy3`）：下拉打开时会有**两个** `Hy3`——一个是列表项里
    的标签，另一个是会话正文里的行内模型芯片（同一段对话里提过这个模型名就会有）。
    原来直接在标签上 `ui.unique` → "找到 2 个控件"，切档整条路走不通。

    判据：挂在菜单项/列表项/按钮下面的那个才是选项；没有这种祖先的是正文，不要。
    同一行里的重复标签再合成一个（一行一个选项）。
    """
    rows = [model_row(n, require_row=True) for n in nodes]
    rows = [row for row in rows if row is not None]
    if not rows:                            # 结构变了（选项不在列表项里）：退回标签本身
        rows = [model_row(n) for n in nodes]
    unique = []
    for row in rows:
        if not any(row is other for other in unique):
            unique.append(row)
    return unique


def select_model(screen, wanted):
    before = current_model(controls(screen))
    if before == wanted:
        return {"ok": True, "before": before, "after": before, "verified": True}

    # 第 4 条：点开下拉后轮询找目标项；下拉没打开 / 目标项没出现就重点下拉再试。
    # 目标项在可视区外（IsOffscreen）不排除，先 ScrollIntoView 再点。
    target = None
    for attempt in range(4):
        ui.click(screen, model_control(controls(screen)))
        time.sleep(0.3)
        items = [n for n in controls(screen) if n.name == wanted
                 and n.kind in ("MenuItemControl", "ListItemControl", "TextControl")]
        leaves = [n for n in items if not any(c in items for c in n.children)]
        rows = model_rows(leaves)
        if len(rows) == 1:
            target = rows[0]
            break
        if len(rows) > 1:
            # 结构变了：同名多行，宁可报错不瞎点（保持 CARD-06d 契约）
            raise RuntimeError(f"目标模型标签: 找到 {len(rows)} 个控件")
    if target is None:
        return {"ok": False, "verified": False, "before": before, "after": None,
                "want": wanted, "reason": "item_not_found",
                "hint": f"下拉里找不到模型项 {wanted!r}（重开下拉 4 次都没出现）"}
    try:
        offscreen = bool(target.control.IsOffscreen)
    except Exception:                       # noqa: BLE001 - 读不到就当可见，照点
        offscreen = False
    if offscreen:
        ui.scroll_into_view(target)
        time.sleep(0.2)
    ui.click(screen, target)
    time.sleep(0.4)
    after = current_model(controls(screen))
    return {"ok": after == wanted, "verified": after == wanted,
            "before": before, "after": after, "want": wanted}


def receipt_model(purpose, hwnd):
    """回执里登记的模型；回执本身不成立就返回 None。

    只**读**不动回执（`tier_guard.check` 不消费）。回执缺失/过期/身份不符一律 None，
    让后面的闸门按它自己的理由报错——这里不抢闸门的活，也不替它编一个模型。
    """
    try:
        verdict = tier_guard.check("workbuddy", purpose,
                                   live=tier_guard.fingerprint(hwnd))
    except Exception:                      # noqa: BLE001 - 读回执失败按"没有回执"处理
        return None
    if not verdict.get("ok"):
        return None
    receipt = verdict.get("receipt") or {}
    if str(receipt.get("dimension") or "model") != "model":
        return None                        # 不是模型维度，重选没有意义
    return receipt.get("target") or None


def restore_after_new(screen, purpose):
    """`--new` 之后把模型按回执按回来（CARD-06d 第 1 条）。

    返回 `(info, failure)`：
      · 没回执 → `(None, None)`，交给闸门报 `receipt_missing`；
      · 模型没被重置 → `({"reselect": False, ...}, None)`；
      · 重选后读回一致 → `({"reselect": True, ...}, None)`；
      · 重选后仍不一致（或重选本身失败）→ `(None, {stage: model_reset_after_new})`，
        调用方打印后按 5 退出，**不发送**。
    """
    want = receipt_model(purpose, screen.target_hwnd)
    if not want:
        return None, None
    try:
        before = current_model(controls(screen))
    except (RuntimeError, COMError):
        # 连当前模型都读不出来：不在这里编一个结论，交给闸门的 reread 去拒（tier_unreadable）
        return None, None
    if before == want:
        return {"model": want, "before": before, "after": before,
                "reselect": False}, None
    note = ""
    try:
        selected = select_model(screen, want)
        after = current_model(controls(screen))
    except (RuntimeError, COMError) as exc:
        selected, after = None, None
        note = f"重选时出错：{type(exc).__name__}: {exc}"
    if after == want:
        return {"model": want, "before": before, "after": after, "reselect": True,
                "selected": bool(selected and selected.get("ok"))}, None
    return None, {
        "ok": False, "stage": MODEL_RESET_STAGE, "reason": "reselect_failed",
        "code": ec.TIER_UNCONFIRMED, "model": want, "before": before, "after": after,
        "sent": False, "submission": "not_sent",
        "hint": (f"--new 之后 WorkBuddy 把模型重置成了 {before!r}，按回执重选 {want!r} "
                 f"后读回是 {after!r}：按卡面 fail-closed，未发送；"
                 f"先重跑 workbuddy_choose --model 再发"
                 + (f"（{note}）" if note else "")),
    }


# ---------------------------------------------------------------- 读回复的退路
def clipboard_seq():
    """剪贴板序列号：有人写一次剪贴板，Windows 就把它 +1。

    为什么要有它：`Conversation.copy_at` 用"内容变没变"判复制成没成，
    遇到"回复和上一次剪贴板内容一样"就会误判；序列号不会。
    这里只做**留证**，不拿它单独下结论——读不到返回 None，不猜。
    """
    try:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:                      # noqa: BLE001 - 纯诊断信息，不许带崩主流程
        return None


# 回复气泡里混着的状态行（WorkBuddy 把生成进度、工具条、转场话术都渲染成文本节点）。
# 读回退时逐块剔掉：**读不到正文可以报错，不能把「已消耗 1.97」当回答交出去**。
_STATUS_PREFIXES = ("预计消耗", "已消耗", "已处理", "已完成", "已停止", "思考中",
                    "正在思考", "正在生成", "正在处理", "内容由 AI 生成", "不再显示",
                    "引用来源")
_STATUS_EXACT = {"WorkBuddy", "分享", "复制", "重做", "重新生成", "朗读",
                 "好的回答", "有问题的回答", "差的回答", "赞", "踩"}
# 这些标签下一行跟着的是数值（「预计消耗」+「0.41 ~ 18.23」、「已消耗」+「1.97」），
# 标签和数值是两个独立的文本节点——只剔标签会留下一串裸数字。
_VALUE_LABELS = {"预计消耗", "已消耗", "已处理"}
_VALUE = re.compile(r"^[\d.,~\s\-]+$|^\d+[A-Za-z]+\d+[A-Za-z]+$")
_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
MAX_BUBBLE_CLIMB = 6           # 从复制键往上爬几层找气泡（实测气泡约 3 层）
MIN_PROMPT_FOR_PREFIX = 12     # 和 gemini.py 同一条约定：够长才按"包含"认领提问
_SKIP_KINDS = ("ButtonControl", "ComboBoxControl", "MenuControl", "MenuBarControl",
               "ToolBarControl", "StatusBarControl")


def _is_status(line):
    return (line in _STATUS_EXACT or bool(_CLOCK.match(line))
            or any(line.startswith(p) for p in _STATUS_PREFIXES))


def _leaf_texts(group, branch):
    """气泡里的叶子文本：[(文本, left, top, right, bottom)]，不含动作条那一支。

    为什么要矩形：状态区不只占一行——「已消耗」右面并排着数值和一句转场话术
    （「这题我会，让我组织下语言」），按行剔文本剔不干净，按**行高**连坐才干净；
    而且 WorkBuddy 会把一段文字拆成很多小文本节点（实测 `BR9-06D-LIVE-OK` 是
    7 个节点），按节点换行会读回一列单字母，只能靠矩形把它们拼回一行。
    """
    out = []

    def visit(node):
        if node is branch or node.kind in _SKIP_KINDS:
            return
        if not node.children:
            text = (node.name or "").strip()
            if text and node.kind in ("TextControl", "HyperlinkControl"):
                try:
                    rect = node.control.BoundingRectangle
                except COMError:
                    return
                out.append((text, rect.left, rect.top, rect.right, rect.bottom))
            return
        for child in node.children:
            visit(child)

    visit(group)
    return out


def _clean_bubble(lines):
    """剔状态块：状态文本本身，以及与状态文本**同一行**的其它叶子。

    同行的典型受害者：`预计消耗` 旁边的 `0.41 ~ 18.23`、`已消耗` 旁边的 `1.97`
    和转场话术。它们自己不像状态行，位置却说明它们属于状态区。
    """
    status = [(top, bottom) for text, _, top, _, bottom in lines if _is_status(text)]

    def same_row(top, bottom):
        return any(top < s_bottom and s_top < bottom for s_top, s_bottom in status)

    kept, expect_value = [], False
    for text, left, top, right, bottom in lines:
        if _is_status(text):
            expect_value = text in _VALUE_LABELS
            continue
        if expect_value and _VALUE.match(text):     # 兜底：没有矩形信息时按相邻行剔
            expect_value = False
            continue
        expect_value = False
        if same_row(top, bottom):
            continue
        kept.append((text, left, top, right, bottom))
    return kept


def _rows_to_text(items):
    """同一视觉行的叶子按 x 拼成一行，行与行之间换行。"""
    rows = []                                   # [[top, bottom, [(left, 文本)]]]
    for text, left, top, _, bottom in items:
        for row in rows:
            if top < row[1] and row[0] < bottom:        # 纵向有重叠 = 同一行
                row[2].append((left, text))
                row[0], row[1] = min(row[0], top), max(row[1], bottom)
                break
        else:
            rows.append([top, bottom, [(left, text)]])
    rows.sort(key=lambda row: row[0])
    return "\n".join("".join(text for _, text in sorted(row[2]))
                     for row in rows)


def reply_text(copy_node, prompt):
    """从回复动作条上的复制键往上，找出这一轮回复的正文。只读，不点任何控件。

    判据三层，缺一不可：
      · **结构**：取「同一层里除自己这一支以外的叶子」，越往上越宽；
      · **文本**：剔掉状态块（预计消耗/已完成/已消耗/时间戳/按钮名，及与它们同行的数值和话术），
        剩下的按矩形归行拼回去（一个词被拆成七个节点也还原得回来）；
      · **归属**：剩下的如果还是我们自己的提问（提问气泡上也有复制键），不算回复，
        继续往上；爬满 `MAX_BUBBLE_CLIMB` 层还拿不到就返回 None。
    返回 None 就是"读不到"——调用方按两条都失败处理，绝不猜一段文本冒充回答。

    已知代价（2026-11-02 真机实测，写在这里免得下一个人以为是 bug）：
      · 回复里的工具调用行（「运行校验、查看文件：ui.py」）也在正文层，剔不掉；
      · 行内代码是独立的小编辑框，位置信息会丢，可能挤到相邻文字里。
      所以它是**回退**：剪贴板能读就绝不走这里。
    """
    wanted = textcmp.normalize(prompt or "")
    branch, group = copy_node, copy_node.parent
    for _ in range(MAX_BUBBLE_CLIMB):
        if group is None:
            break
        text = _rows_to_text(_clean_bubble(_leaf_texts(group, branch)))
        lines = [line for line in text.splitlines() if line.strip()]
        if wanted and len(wanted) >= MIN_PROMPT_FOR_PREFIX:
            # 爬过了气泡边界：正文里夹进了我们自己的提问，把它那几行剔出去
            lines = [line for line in lines
                     if wanted not in textcmp.normalize(line)]
        text = "\n".join(lines).strip()
        if text and textcmp.normalize(text) != wanted:
            return text
        branch, group = group, group.parent
    return None


class ReplyConversation(Conversation):
    """本端的这一轮收发：剪贴板读不到时改读 UIA（CARD-06d 第 2 条）。

    `Conversation` 是五端共用的，允许改的文件里没有它，所以本端的问题在本端解决：
    只覆盖 `write`（记住这一轮的提问，回退时认领"这一条"）和 `copy`（先剪贴板，
    不成就读树）。别端的复制行为一个字节都没动。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompt = ""
        self.read_via = None       # "clipboard" / "uia"；两条都失败时保持 None
        self.copy_note = None      # 失败留证：剪贴板报错 + 前后序列号 + UIA 结果

    def write(self, view, prompt):
        self.prompt = prompt       # ask() 先 write 再找复制键，顺序是固定的
        return super().write(view, prompt)

    def copy(self, node):
        self.copy_note = None      # 留证只反映**这一次**尝试（候选可能试好几个）
        before = clipboard_seq()
        try:
            text = super().copy(node)
        except (RuntimeError, COMError) as exc:
            after = clipboard_seq()
            self.copy_note = {"clipboard": str(exc), "seq": [before, after]}
            answer = reply_text(node, self.prompt)
            if not answer:
                self.copy_note["uia"] = "unreadable"
                raise RuntimeError(
                    f"{exc}；UIA 也读不到本轮回复（剪贴板序列号 {before}→{after}）"
                ) from exc
            self.copy_note["uia"] = "ok"
            self.read_via = "uia"
            return answer
        self.read_via = "clipboard"
        return text


# ---------------------------------------------------------------- 等待判据（第 1 条）
# 判据（卡面原文）：停止生成按钮消失 / 复制按钮出现 / 回复正文连续 2 轮不变。
STABLE_READS = 2            # 正文连续几轮不变算答完
POLL = 0.4                  # 一轮多久
COPY_NAMES = {"复制", "Copy", "复制回复", "复制回答", "Copy response"}
REPLY_BASIS = "uia_stop_gone_and_stable_text"
# 这些 stage 表示"这一轮根本没发出去"，等下去没有意义，原样交回调用方
NOT_SENT = {"input_mismatch", "draft_present", "conversation_busy",
            "user_took_over", "interrupted", "tier_unconfirmed", "concurrent_ui"}


def reply_copy(node):
    """本端回复动作条上的复制键。

    共享的 `conversation.is_reply_copy` 要求同组里还有赞/踩/重做这类同伴按钮。
    2026-11-02 实测这个页面两件事都变了：
      · 回复动作条（赞/踩/重试/复制那一排）是 **ImageControl**，压根不在 ButtonControl 里；
      · 控件树里唯一叫「复制」的按钮挂在**提问气泡**的工具条上（`ToolBar[09:17, 复制]`）。
    于是共享判据一条都认不出来（`view.copies=0`，等到超时）。
    本端补一条：复制键挂在工具条下面也算。点它复制到的其实是提问，但不要紧——
    `reply_text` 会从它往上爬、跨过气泡边界拿到回复正文，`last_reply` 再把
    "等于提问"的那条剔掉。
    """
    if node.kind != "ButtonControl" or node.name not in COPY_NAMES:
        return False
    group = node.parent
    for _ in range(3):
        if group is None:
            return False
        if group.kind == "ToolBarControl":
            return True
        group = group.parent
    return False


def reply_copies(nodes):
    """树里所有回复复制键，按文档序。读不到的（对照组）不算。"""
    out = []
    for node in nodes:
        if not reply_copy(node):
            continue
        try:
            if node.control is not None and not node.control.IsEnabled:
                continue
        except COMError:
            continue
        out.append(node)
    return out


def prompt_at(nodes, prompt):
    """我们这条提问在文档序里的下标；认领不到返回 None。

    提问气泡在控件树里**会被截断**（实测长提问只留到第 60 个字），
    所以比的是前缀而不是全等——和 gemini.py 的 `prompt_matches` 同一条约定。
    """
    want = textcmp.normalize(prompt or "")
    if not want:
        return None
    found = None
    for index, node in enumerate(nodes):
        if node.kind not in ("TextControl", "HyperlinkControl") or node.children:
            continue
        got = textcmp.normalize(node.name or "")
        if not got:
            continue
        # 取**最后**一处命中：接着聊的时候同一个提问可能在树里出现过不止一次，
        # 认成第一条就会去读上一轮的回复。
        if got == want or (len(got) >= min(MIN_PROMPT_FOR_PREFIX, len(want))
                           and want.startswith(got)):
            found = index
    return found


def last_reply(nodes, prompt, at=None):
    """树里最后一条**本轮**回复：(正文, 复制键)。

    从最新的复制键往回试；`reply_text` 自己会把"读回来等于提问"的那条排除掉。
    认领到提问时只认排在它后面的复制键——不去读别人的回答（和共享判据同一条底线）。
    """
    for index in reversed(range(len(nodes))):
        node = nodes[index]
        if not reply_copy(node):
            continue
        if at is not None and index <= at:
            continue
        try:
            if node.control is not None and not node.control.IsEnabled:
                continue
        except COMError:
            continue
        text = reply_text(node, prompt)
        if not text:
            continue
        # `reply_text` 已经会把"读回来等于提问"的那条剔掉，这里再兜一次：
        # 提问认领失败时（prompt 传得不全、气泡被截断）它认不出归属，会原样返回提问。
        # 宁可继续往上找，也不能把提问当成回答交出去。
        want = textcmp.normalize(prompt or "")
        got = textcmp.normalize(text)
        if want and (got == want or (len(want) >= MIN_PROMPT_FOR_PREFIX
                                     and got.startswith(want[:MIN_PROMPT_FOR_PREFIX]))):
            continue
        return text, node
    return None, None


def _read(conversation):
    """读一版树；读不到返回 None（页面在重排/换路由时的常态，不算失败）。"""
    try:
        return conversation.read(attempts=2, delay=0.4)
    except (RuntimeError, COMError):
        return None


def _already_replied(prompt) -> dict | None:
    """重启后、重跑发送前的只读检查 + 回填草稿清理（CARD-09f 第 6 条）。

    UiDocsLost 触发的重启可能发生在「已经 accepted、只是后续读不到」之后，
    此时绝不能把同一提问再发一遍。认领到本提问且其后有回复 → 返回
    already_answered 结果（顺手清掉回填进输入框的提问文本并读回为空）；
    认领不到 / 读不到 / 任何异常 → None（不拦，让 `_run` 按原样走）。
    """
    try:
        screen = open_app()
        conv = ReplyConversation(screen, "workbuddy")
        view = _read(conv)
        if view is None:
            return None
        nodes = list(view.root.walk())
        at = prompt_at(nodes, prompt)
        if at is None:
            return None
        text, _node = last_reply(nodes, prompt, at)
        if not text:
            return None
        cleared = None
        if not conv.empty(view):
            try:
                conv.clear(view)
                recheck = _read(conv)
                cleared = bool(recheck is not None and conv.empty(recheck))
            except (RuntimeError, COMError):
                cleared = False
        return {"ok": True, "stage": "already_answered", "reply": text,
                "submission": None, "sent": False, "basis": "already_answered",
                "cleared_draft": cleared}
    except (RuntimeError, COMError):
        return None


def _answer(conversation, node, text, state, basis):
    """收尾：先按原来的路子复制（剪贴板优先、不成就 UIA），失败就用已经读到的正文。

    复制键点下去失败**不影响结论**——正文是刚从树里读出来的，不是猜的；
    但要把失败原因留在 `copy_note` 里，别让它悄悄消失。

    复制回来的东西不能直接当答案：2026-11-02 真机上每轮都出现「复制到的是提问本身」
    （树里那个复制键挂在提问气泡上）。这种情况一律改用树里读到的那一段，
    `read_via` 也据实写成 `uia`——说"剪贴板读到的"是假话。
    """
    answer, note, source = text, None, "uia"
    try:
        got = conversation.copy(node)
        wanted = textcmp.normalize(conversation.prompt or "")
        same_as_prompt = wanted and textcmp.normalize(got or "") == wanted
        if got and not same_as_prompt:
            answer, source = got, conversation.read_via or "clipboard"
        else:
            note = {"copy": "复制到的仍是提问本身，改用 UIA 正文"}
    except (RuntimeError, COMError) as exc:
        note = {"copy": str(exc), "uia": "ok"}
    if note:
        conversation.copy_note = note
    return {**state, "ok": True, "stage": "answered", "reply": answer,
            "read_via": source, "basis": basis}


def wait_reply(conversation, prompt, timeout, *, baseline=0):
    """等到这一轮答完（第 1 条）。返回 `ask()` 的结果字典。

    `baseline` 是发送前树里的复制键个数——只为"超时前最后一次读"的归属兜底：
    认领不到提问、复制键也没变多，那条回复就是上一轮的，不能交卷。
    """
    state = {"ok": False, "submission": "unknown", "sent": None}
    deadline = time.monotonic() + max(0.0, timeout)
    stable_text, stable_reads = None, 0
    last_busy = False
    read_failures = 0
    while time.monotonic() < deadline:
        view = _read(conversation)
        if view is None:
            read_failures += 1
            if read_failures >= 12:
                return {**state, "stage": "interrupted",
                        "hint": "连续 12 次读不到控件树"}
            time.sleep(1.0)
            continue
        read_failures = 0
        nodes = list(view.root.walk())
        at = prompt_at(nodes, prompt)
        if at is not None and not state["sent"]:
            # 提问气泡出现在树里 = 这一轮确实发出去了（截断也能认）
            state.update(submission="accepted", sent=True)
        last_busy = bool(view.busy)
        if last_busy:
            # 停止键还在：还在生成，正文再稳也不算答完
            stable_text, stable_reads = None, 0
            time.sleep(POLL)
            continue
        text, node = last_reply(nodes, prompt, at)
        if not text:
            stable_text, stable_reads = None, 0
            time.sleep(POLL)
            continue
        if text != stable_text:
            stable_text, stable_reads = text, 1
            time.sleep(POLL)
            continue
        stable_reads += 1
        if stable_reads < STABLE_READS:
            time.sleep(POLL)
            continue
        return _answer(conversation, node, text, state, REPLY_BASIS)
    # 超时前最后再看一眼：**纯只读**，不点复制键
    final = final_read(conversation, prompt, state, baseline)
    if final is not None:
        return final
    kind = "still_generating" if state["sent"] and last_busy else "no_reply"
    return {**state, "stage": "timeout", "timeout_kind": kind}


def final_read(conversation, prompt, state, baseline=0):
    """超时前的最后一次 UIA 读（卡面要求）。

    守住两条底线：还在生成（停止键可点）不交卷——半截话不能当答案；
    认领不到提问、复制键个数也没比发送前多，也不交卷——那是上一轮的答案。
    """
    view = None
    for _ in range(3):
        view = _read(conversation)
        if view is not None:
            break
        time.sleep(0.4)
    if view is None or view.busy:
        return None
    nodes = list(view.root.walk())
    at = prompt_at(nodes, prompt)
    if at is None and len(reply_copies(nodes)) <= baseline:
        return None
    text, _node = last_reply(nodes, prompt, at)
    if not text:
        return None
    result = {**state, "ok": True, "stage": "answered", "reply": text,
              "read_via": "uia", "basis": "uia_final_read"}
    if at is None:
        result["attribution"] = "new_reply"
    return result


def ask(conversation, prompt, timeout=180.0):
    """发送 + 等待（CARD-07 第 1 条）。

    发送仍然走共享的 `conversation.ask`，但只给它 0 秒预算：它会把
    「读一版 → 写入 → 核对 → 提交」做完就返回，写入/闸门/拒发的语义一个字节没变，
    只是不再用它那套在 WorkBuddy 上永远不成立的等待判据。等由 `wait_reply` 负责。

    发送阶段出问题（写歪、有草稿、用户抢键鼠）就把那个结果原样交回，
    绝不继续等一个根本没发出去的提问——那会把"没发出去"报成"没回复"。
    """
    baseline = 0
    before = _read(conversation)
    if before is not None:
        baseline = len(reply_copies(list(before.root.walk())))
    sent = send_and_wait(conversation, prompt, 0.0)
    if sent.get("ok") or sent.get("stage") == "already_answered":
        return sent
    if sent.get("stage") in NOT_SENT:
        return sent
    result = wait_reply(conversation, prompt, timeout, baseline=baseline)
    # 复制键/剪贴板的留证要带出去（read_via 由 `_answer` 决定）
    if conversation.copy_note and "copy_note" not in result:
        result["copy_note"] = conversation.copy_note
    return result


def read_state(screen):
    """--list 的只读快照：当前模型 + 输入框状态。全程不点任何控件。"""
    doc, _ = ui.document(screen.target_hwnd, "workbuddy")
    root = ui.tree(doc)
    return {"ok": True, "model": current_model(list(root.walk())),
            "editor": editor_state(root)}


def editor_state(root):
    """输入框的宽高。读不到就给 None——这是参考信息，不能拖垮 --list 的退出码。"""
    try:
        node = visible_editor(root, "workbuddy")
        rect = node.control.BoundingRectangle
    except Exception:      # 没有输入框 / COMError / 控件读完就失效，都算读不到
        return None
    return {"name": node.name, "width": rect.right - rect.left,
            "height": rect.bottom - rect.top}


def parse_args(argv=None, *, sending=False):
    """解析参数，并补回"不带 --list 就必须给 prompt"这条闸。

    2026-10-31：`prompt` 原来是必填位置参数，`workbuddy_use.py --list` 在 argparse
    阶段就退 2（`the following arguments are required: prompt`），根本走不到只读逻辑。
    改成 `nargs="?"` 先收下，再把"不带 --list 又没给 prompt"的报错手工补回来，
    措辞和退出码 2 与改前完全一致。返回 (parser, args)，parser 留着报组合不对的错。
    """
    parser = argparse.ArgumentParser(description="WorkBuddy 模型选择 / 提问并读取本轮回复")
    if sending:
        parser.add_argument("prompt", nargs="?")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--new", action="store_true")
        group.add_argument("--continue", dest="continuing", action="store_true")
        parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--purpose", choices=tier_guard.PURPOSES, default=None,
                        help="test=最低档，review/accept=最高档（发送必填，无默认）")
    parser.add_argument("--idle", type=float, default=2)
    parser.add_argument("--wait", type=float, default=120)
    parser.add_argument("--force", action="store_true")
    # CARD-09e 第 2 条：读不到文档时默认自动重启一次；--no-restart 只用来演练/排障
    parser.add_argument("--no-restart", dest="no_restart", action="store_true",
                        help="UIA 读不到文档时不重启 WorkBuddy（排障用）")
    args = parser.parse_args(argv)
    if sending and not args.list and not args.prompt:
        parser.error("the following arguments are required: prompt")
    # 发送必须有 --purpose（无默认）；--list 只读、不发消息，不要求
    if sending and not args.list and not args.purpose:
        parser.error("the following arguments are required: --purpose（test/review/accept，无默认）")
    return parser, args


def main(argv=None, *, sending=False):
    parser, args = parse_args(argv, sending=sending)
    if args.list and (args.model or getattr(args, "prompt", None)):
        parser.error("--list 不能与模型修改或提问同用")
    if not sending and not (args.list or args.model):
        parser.print_help()
        return 2
    if not guard.guard(args.idle, args.wait, args.force):
        return ec.BUSY
    try:
        return _run(args, sending=sending)
    except UiDocsLost as exc:
        return _restart_and_retry(args, sending=sending, lost=exc)


def _restart_and_retry(args, *, sending: bool, lost) -> int:
    """UIA 读不到文档 → 重启 WorkBuddy 一次 → 动作再来一遍（CARD-09e 第 2 条）。

    只重启**一次**：第二次还读不到就按 6 退出，不循环重启（卡面原文）。
    每一次重启都写一行 runlog（`result=uia_restart`），成功失败都要留痕。
    """
    from . import restart, runlog

    if getattr(args, "no_restart", False):
        print(json.dumps({**lost.as_result(), "restart": {"skipped": "--no-restart"}},
                         ensure_ascii=False))
        return lost.code
    info = restart.restart("workbuddy")
    runlog.record_restart("workbuddy", info)
    # 触发重启的那次失败也要留一行：否则日志里只剩"重启过"，看不出为什么重启
    runlog.record("workbuddy", lost.as_result())
    if not info.get("ok"):
        print(json.dumps({**lost.as_result(), "restart": info,
                          "hint": f"{lost}；自动重启一次也没恢复（{info.get('error')}），"
                                  "按 6 退出，等人工看一眼 WorkBuddy"},
                         ensure_ascii=False))
        return lost.code
    try:
        # 第 6 条：重启后、重跑发送前，先只读确认本提问还没被答过。已经答完就
        # 只读返回 already_answered，绝不因 UiDocsLost 重启而把同一提问再发一遍。
        if sending and getattr(args, "prompt", None):
            replied = _already_replied(args.prompt)
            if replied is not None:
                replied["restarted"] = {"docs": info.get("docs"),
                                        "steps": info.get("steps")}
                runlog.record("workbuddy", replied)
                print(json.dumps(replied, ensure_ascii=False))
                return 0
        return _run(args, sending=sending, restarted=info)
    except UiDocsLost as again:
        runlog.record("workbuddy", again.as_result())
        print(json.dumps({**again.as_result(), "restart": info,
                          "hint": f"重启成功（{info.get('docs')} 个文档）但动作仍然读不到文档，"
                                  "按 6 退出，不第二次重启"},
                         ensure_ascii=False))
        return again.code
    except WindowMinimized as exc:
        print(json.dumps({"ok": False, "stage": "window_minimized", "hint": str(exc),
                          "restart": info, **exc.facts}, ensure_ascii=False))
        return exc.code


def _run(args, *, sending: bool, restarted: dict | None = None) -> int:
    """一次完整的动作。UIA 读不到文档时**不在这里吞**：交给 `main` 去重启重试。"""
    try:
        screen = open_app()
        after_new = None
        # --list 是只读的：绝不能顺手新建任务，否则读一次状态就把当前会话换掉。
        if sending and not args.list and not args.continuing:
            new_chat(screen, "workbuddy")
            # 第 1 条：新对话把模型重置成默认了没有？重置了就按回执按回来。
            # 给了 --model 就不插手——那是调用方明确点的档，马上会重新写一张回执。
            if not args.model:
                after_new, reset = restore_after_new(screen, args.purpose)
                if reset is not None:
                    print(json.dumps(reset, ensure_ascii=False))
                    return ec.TIER_UNCONFIRMED
        if args.list:
            state = read_state(screen)
            if restarted:
                state["restarted"] = {"docs": restarted.get("docs"),
                                      "steps": restarted.get("steps")}
            print(json.dumps(state, ensure_ascii=False))
            return 0
        if args.model:
            result = select_model(screen, args.model)
            print(json.dumps(result, ensure_ascii=False))
            if not result["ok"]:
                return 1
            # 读回确认了才写档位回执（CARD-06a (a)）
            tier_guard.record_choice(
                "workbuddy", target=args.model,
                readback=result.get("after") if result.get("verified") else None,
                dimension="model", hwnd=screen.target_hwnd, raw=result)
        if sending:
            # 发送前过档位闸：回执校验 → 只读复核当前模型 → 原子消费
            fail = tier_guard.gate_or_exit(
                "workbuddy", args.purpose,
                reread=lambda dimension: (current_model(controls(screen))
                                          if dimension == "model" else None),
                hwnd=screen.target_hwnd)
            if fail is not None:
                return fail
            started = time.monotonic()
            # 第 2 条：剪贴板读不到就改读 UIA，读法记在 read_via 里
            conversation = ReplyConversation(screen, "workbuddy")
            # 第 6 条：高档（review/accept）等待上限下限 900s
            result = ask(conversation, args.prompt,
                         tier_guard.floor_timeout(args.purpose, args.timeout))
            result.setdefault("elapsed", round(time.monotonic() - started, 1))
            # 读法以 ask 里判定的为准（它知道最后到底用了剪贴板还是树）；
            # 这里只补字段，不覆盖——`ReplyConversation.read_via` 记的是"复制调用了没失败"，
            # 而复制到的可能是提问本身（真机 2026-11-02 就是这样），那不是这一轮的答案。
            if conversation.read_via:
                result.setdefault("read_via", conversation.read_via)
            if conversation.copy_note:
                result["copy_note"] = conversation.copy_note
            if after_new and after_new.get("reselect"):
                result["model_after_new"] = after_new
            if restarted:
                # 这一轮是重启之后跑成的：把重启过程带进结果，别让看日志的人以为没发生
                result["restarted"] = {"docs": restarted.get("docs"),
                                       "steps": restarted.get("steps"),
                                       "elapsed": restarted.get("elapsed")}
            from . import runlog
            runlog.record("workbuddy", result)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else ec.code_for(result.get("stage"))
    except WindowMinimized as exc:
        print(json.dumps({"ok": False, "stage": "window_minimized", "hint": str(exc),
                          **exc.facts}, ensure_ascii=False))
        return exc.code
    return 0


def new_chat(screen, site):
    labels = {"workbuddy": {"新建任务"}, "opencode": {"新建会话", "New session"},
              "dsh": {"新建会话", "New session"}, "codex": {"New chat", "新聊天"}}
    doc, _ = ui.document(screen.target_hwnd, site)
    nodes = [n for n in ui.tree(doc).walk() if n.name in labels[site]
             and n.kind in ("ButtonControl", "TabItemControl") and not n.control.IsOffscreen]
    # Some clients expose the same action in both sidebar and header.
    if not nodes:
        raise RuntimeError(f"{site}: 找不到新对话控件")
    ui.click(screen, nodes[0])
    time.sleep(0.5)
