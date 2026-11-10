"""「按页面标记取最后一条助手回复」——ChatGPT 的 UIA 兜底（CARD-09e 第 3 条）。

## 为什么要它

2026-11-02 两次真机现场（`runs\\20261102.jsonl`）：

  · 12:13 那轮页面早就答完了，`conversation.ask` 300 秒后仍报 `timeout/no_reply`
    （`submission=unknown`）——共享判据要"认领到我们这条提问"，而 ChatGPT 把长提问
    折叠了，认领不到，于是永远不 accepted；
  · 16:12 那轮已经 accepted，却在点"复制"时崩了：回复动作条在页面下方
    y=2014，副屏只到 y≈1906，`move_to` 挪不过去 → `interrupted`。

两次的共同点：**答案就在控件树里，只是复制不到/认领不到**。

## 标记从哪来（2026-11-02 现场树，`evidence\\card09e_probe_chatgpt.txt`）

页面自己给了两个现成的语义节点：

  · `TextControl '回答已完成'` —— aria-live 播报，只在生成结束时出现；
  · `TextControl '最新一条回复'` —— 指向最新一条回复的地标。

再加一个**结构**标记：回复动作条（复制 / 评价回复 / 朗读 / 分享 / 重新生成回复 /
更多操作）只在回答收尾后挂出来。三者任一成立就算"这一轮答完了"。

## 只读

全程 `IsEnabled/IsOffscreen` 只看不点，也不要求控件在视口内——回复经常在
可视区外，那正是要兜底的情况。取不到就返回 None，**绝不拿半截话冒充回答**。
"""
from __future__ import annotations

import re

from comtypes import COMError

from . import textcmp, ui
from .conversation import ACTION as REPLY_ACTIONS
from .conversation import COPY as COPY_NAMES

# 「答完了」的播报/地标（页面中文界面；英文留一份，换语言时兜底）
DONE_LABELS = {"回答已完成", "回复已完成", "Response complete", "Answer complete"}
LATEST_LABELS = {"最新一条回复", "Latest reply", "Last reply"}

# 从动作条往上爬几层找气泡（实测气泡 2~4 层）
MAX_CLIMB = 8
MIN_PROMPT_FOR_PREFIX = 12

# 这些支里的东西不是正文
SKIP_KINDS = ("ButtonControl", "ComboBoxControl", "MenuControl", "MenuBarControl",
              "ToolBarControl", "StatusBarControl", "ImageControl", "EditControl",
              "ProgressBarControl")

# 界面按钮名（万一被别的结构带进来，整行剔掉）
UI_LABELS = {ui.canonical(x) for x in
             COPY_NAMES | REPLY_ACTIONS |
             {"复制消息", "共享提示", "编辑消息", "更多操作", "显示更多", "滚动到底部",
              "添加文件等内容", "选择 ChatGPT 模型", "听写", "开始语音", "发送"}}

_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")

# 气泡的角色标签。ChatGPT 把「你说：」「ChatGPT 说：」渲染成**独立文本节点**，
# 而我们爬到的容器常常是整个回合（提问 + 回答）。只按标签切：取**最后一个**
# 角色标签之后的内容，就是这一轮的助手正文。
ROLE = re.compile(r"^(?:你说|ChatGPT\s*说|ChatGPT\s*says?|You\s*said|Gemini\s*说|"
                  r"Gemini\s*says?|Grok\s*说)[:：]?\s*")


def _drop_role_prefix(text: str) -> str | None:
    """把「最后一个角色标签」之前的整段切掉；没有标签返回 None。"""
    lines = text.splitlines()
    last = None
    for index, line in enumerate(lines):
        if ROLE.match(line.strip()):
            last = index
    if last is None:
        return None
    head = ROLE.sub("", lines[last].strip())
    tail = ([head] if head else []) + lines[last + 1:]
    return "\n".join(tail).strip()


def _enabled(node) -> bool:
    try:
        return bool(node.control is None or node.control.IsEnabled)
    except COMError:
        return False


def reply_markers(root) -> list:
    """回复动作条上的「复制」键，按文档序。

    判据：按钮名在 COPY 里，并且同组的兄弟里还有别的回答动作（赞/踩/重做/朗读/分享）。
    提问气泡上的「复制消息」名字不同，天然被排除。
    """
    found = []
    for node in root.walk():
        if node.kind != "ButtonControl" or node.name not in COPY_NAMES:
            continue
        group = node.parent
        peers = set()
        for _ in range(4):
            if group is None:
                break
            peers |= {n.name for c in group.children for n in c.walk()
                      if n.kind == "ButtonControl" and n.name != node.name}
            if peers & REPLY_ACTIONS:
                break
            if group.kind == "DocumentControl":
                break
            group = group.parent
        if peers & REPLY_ACTIONS and _enabled(node):
            found.append(node)
    return found


def done_signal(root) -> str | None:
    """页面有没有说"答完了"。返回命中的标记原文，没有就 None。"""
    for node in root.walk():
        name = (node.name or "").strip()
        if name in DONE_LABELS:
            return name
    for node in root.walk():
        name = (node.name or "").strip()
        if name in LATEST_LABELS:
            return name
    return None


def _leaf_texts(group, branch) -> list:
    """气泡里的叶子文本：[(文本, left, top, right, bottom)]，不含动作条那一支。"""
    out = []

    def visit(node):
        if node is branch or node.kind in SKIP_KINDS:
            return
        if not node.children:
            text = (node.name or "").strip()
            if text and node.kind in ("TextControl", "HyperlinkControl"):
                control = getattr(node, "control", None)
                if control is None:
                    return          # B21：离线夹具允许只给名字，没有 control 就当没有几何
                try:
                    rect = control.BoundingRectangle
                except (COMError, AttributeError):
                    return
                if rect is None or rect.right - rect.left <= 0:
                    return
                out.append((text, rect.left, rect.top, rect.right, rect.bottom))
            return
        for child in node.children:
            visit(child)

    visit(group)
    return out


def _rows_to_text(items) -> str:
    """同一视觉行的叶子按**文档序**拼成一行，行与行之间换行。

    B21：两处修正，都是拿真实 chatgpt 气泡（`evidence/card09e_probe_chatgpt.txt`）
    复算出来的：

    1. 行归属不能用"任意纵向重叠"——列表项的 `•` 和上一行会连成一句
       （`token。•`）。改成"重叠 ≥ 较矮那个高度的 50%"才算同一行。
    2. **不能按 x 排序**。真实树里同一行的叶子文档序就是阅读序，按 x 排会把
       中文（尤其是数字开头的行）搅乱，实测字符集都不再一致。只分组、不重排。
    """
    rows = []                                    # [[top, bottom, [(left, 文本)]]]
    for text, left, top, _right, bottom in items:
        height = max(1, bottom - top)
        for row in rows:
            overlap = min(bottom, row[1]) - max(top, row[0])
            shorter = min(height, max(1, row[1] - row[0]))
            if overlap >= 0.5 * shorter:             # 纵向重叠过半 = 同一行
                row[2].append((left, text))
                row[0], row[1] = min(row[0], top), max(row[1], bottom)
                break
        else:
            rows.append([top, bottom, [(left, text)]])
    rows.sort(key=lambda row: row[0])
    return "\n".join("".join(text for _left, text in row[2]) for row in rows)


def bubble_text(marker, prompt: str = "") -> str | None:
    """从回复动作条上的复制键往上，取出这一条回复的正文。读不到返回 None。

    逐层往上试：第一层通常只有动作条（按钮全被 SKIP 剔掉，读出来是空的），
    再上一层才是正文。读到的文本等于我们自己的提问就继续往上——
    宁可读不到，也不能把提问当回答交出去。
    """
    wanted = textcmp.normalize(prompt or "")
    branch, group = marker, marker.parent
    for _ in range(MAX_CLIMB):
        if group is None:
            break
        kept = []
        for text, left, top, right, bottom in _leaf_texts(group, branch):
            key = ui.canonical(text)
            if not key or key in UI_LABELS or _CLOCK.match(key):
                continue
            kept.append((text, left, top, right, bottom))
        # B21：按视觉行拼，纯文本节点（没有几何）才退回整段 join。
        text = _rows_to_text(kept).strip() if kept else ""
        if not text:
            text = "\n".join(item[0] for item in kept).strip()
        if text:
            # 容器是整个回合时，正文里会带上「你说：」那一段；按最后一个角色标签切
            trimmed = _drop_role_prefix(text)
            if trimmed:
                text = trimmed
        if text and textcmp.normalize(text) != wanted:
            return text
        branch, group = group, group.parent
    return None


def last_answer(root, prompt: str = "", *, after: int | None = None) -> tuple[str | None, object]:
    """树里**最后一条**回复：(正文, 复制键)。没有就 (None, None)。

    `after` 给了就只认文档序排在它后面的复制键（认领到提问时的归属判据）。
    """
    nodes = list(root.walk())
    index_of = {id(node): index for index, node in enumerate(nodes)}
    order = sorted((index_of.get(id(node), -1) for node in reply_markers(root)), reverse=True)
    for index in order:
        if index < 0 or (after is not None and index <= after):
            continue
        text = bubble_text(nodes[index], prompt)
        if text:
            return text, nodes[index]
    return None, None


def count(root) -> int:
    """回复动作条个数——发送前后各数一次，多出来的才是这一轮。"""
    return len(reply_markers(root))
