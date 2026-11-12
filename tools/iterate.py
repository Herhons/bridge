"""自迭代循环的驱动器（规格 §4 的十步）。

真机动作只在这里：发问、上传、读回复。判定、材料、匿名、验真全在
`src/qi/iteration.py`（可离线测）。

用法：
    py -3.10 -B tools/iterate.py status --round round-0
    py -3.10 -B tools/iterate.py snapshot --round round-0
    py -3.10 -B tools/iterate.py review --round round-0 --site chatgpt
    py -3.10 -B tools/iterate.py verify --round round-0
    py -3.10 -B tools/iterate.py anonymize --round round-0
    py -3.10 -B tools/iterate.py summarize --round round-0
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import iteration as it                      # noqa: E402

CONFIG_PATH = ROOT / "docs" / "iteration.json"
DEFAULT_CONFIG = {
    # 投喂方式：upload=网页端上传文件；paste=分段粘贴。按厂商分别设定。
    "feed": {"chatgpt": "upload", "grok": "paste", "gemini": "paste", "deepseek": "paste"},
    "chunk_chars": 3600,
    "seats": ["chatgpt", "grok", "gemini", "deepseek"],
    "review_timeout": 900,
    "upload_timeout": 120,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {})
    return cfg


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def round_dir(name: str) -> Path:
    return ROOT / "docs" / "iterations" / name


# ---------------------------------------------------------------- 1. 快照
def cmd_snapshot(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    sha = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    remote = git("remote", "get-url", "origin")
    snapshot = {"repo": remote, "branch": branch, "sha": sha,
                "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "orchestrator": "tools/iterate.py@1"}
    it.write_archive(round_dir(args.round), {"snapshot.json": snapshot})
    state.set("snapshot", snapshot)
    state.mark("snapshot", {"sha": sha})
    print(json.dumps(snapshot, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 2. 发问
def _screen_for(site: str, *, new: bool = False):
    from qi.flow import ensure_site
    from qi.screen import Screen

    hwnd = ensure_site(site, tab=None, continue_session=not new, verbose=False)
    screen = Screen(verbose=False)
    screen.bind(hwnd)
    if not screen.focus():
        raise RuntimeError(f"{site}: 拿不到前台")
    screen.tab_site = site
    return screen


def _upload_settled(site: str, screen, timeout: float = 90.0) -> bool:
    """等附件传完。

    注意：**不能**等"发送键可用"——空输入框本来就不启用发送键，
    那样会一直等到超时（Round 0 第一枪就是这么空等 20 分钟的）。
    判据改成页面上不再出现「正在上传」。
    """
    from qi import ui

    end = time.time() + timeout
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                     # noqa: BLE001
            time.sleep(1.0)
            continue
        if not any("正在上传" in (node.name or "") for node in root.walk()):
            return True
        time.sleep(1.0)
    return False


#: 各端「新开会话」的入口名。取不到就退回站点根。
NEW_CHAT = {
    "chatgpt": ("新聊天", "新对话"),
    "gemini": ("发起新对话",),
    "grok": ("聊天",),
    "deepseek": ("开启新对话", "新建对话"),
}
ROOT_URL = {
    "chatgpt": "https://chatgpt.com/",
    "grok": "https://grok.com/",
    "gemini": "https://gemini.google.com/app",
    "deepseek": "https://chat.deepseek.com/",
}


def conversation_session_id(url: str):
    """从地址里取与标题无关的会话 id；没有就是新会话页。"""
    from qi.conversation import session_id_of
    return session_id_of(url)


def start_new_chat(screen, site: str, timeout: float = 20.0) -> bool:
    """真的新开一个会话，并**验证 URL 已经变**。

    上一轮的教训：`ensure_site(continue_session=False)` 并不会替 chatgpt 新开，
    材料被贴进了旧会话，取回的"回复"是别的话题。发错地方的代价比不发大得多，
    所以这里必须验证；验证不过就返回 False，调用方**不许发送**。
    """
    from qi import ui

    def url_now() -> str:
        try:
            control, url = ui.document(screen.target_hwnd, site)
            return url or ""
        except Exception:                     # noqa: BLE001
            return ""

    before = url_now()
    if before and conversation_session_id(before) is None:
        return True                      # 已经在新会话页（例如站点根）
    end = time.time() + timeout
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                     # noqa: BLE001
            time.sleep(0.5)
            continue
        target = None
        for node in root.walk():
            if node.kind not in ("ButtonControl", "HyperlinkControl") or node.offscreen:
                continue
            name = (node.name or "").strip()
            if name and any(name == want for want in NEW_CHAT.get(site, ())):
                target = node
                break
        if target is not None:
            rect = target.control.BoundingRectangle
            screen.click_xy((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            time.sleep(2.0)
            after = url_now()
            if after and conversation_session_id(after) is None:
                # 「新会话页」= 地址里没有会话 id。已经在 `/` 时点一下不会变地址，
                # 那也算成功（再发一条自然就是新会话）。
                return True
        time.sleep(1.0)
    return False


def _is_prompt_echo(block: str, prompt: str, squashed_prompt: str) -> bool:
    """这一块是不是提问回声（整块落在提问里）。"""
    squashed = "".join(block.split())
    # 「可访问=是」是答案抬头，即使和提问重合也要留；
    # 含「」/章节号/文件:行号 的块是**合规引文**，同样豁免（2026-11-12 会审：
    # 按重合长度一刀切会把逐字引用删掉，验真直接归零）。
    looks_like_citation = any(mark in block for mark in ("「", "」", "§")) \
        or re.search(r"[\w./-]+\.(?:py|md|json|yml|yaml):\d+", block) is not None
    return bool(len(squashed) >= 8 and not squashed.startswith("可访问")
                and not looks_like_citation and squashed in squashed_prompt)


def _echo_head(blocks: list[str]) -> int:
    """前序区到哪结束：第一条正式意见的行首。"""
    for index, block in enumerate(blocks):
        stripped = block.strip()
        if (re.match(r"^\s*(?:\d+[）).、]|[①-⑥]|[A-E][\s　])", stripped)
                or "章节号" in stripped[:12]):
            return index
    return 0


def _drop_prompt_echo(blocks: list[str], prompt: str) -> list[str]:
    """剔掉与提问重合的块。

    chatgpt 实测会在回答尾部把自己收到的提问复述 3 段（2026-11-12 Round 1），
    如果留着，会被验真当成"引用"或拉低命中率。判据：整块是提问的连续子串。
    """
    squashed_prompt = "".join(prompt.split())
    if not squashed_prompt:
        return blocks
    # 只在前序区剥（第一条正式意见之前）。正文里引用提问原句是合法的，
    # 一旦进入意见区就停止剥离（2026-11-12 会审建议）。
    head = _echo_head(blocks)
    return [block for position, block in enumerate(blocks)
            if (position >= head and head > 0)
            or not _is_prompt_echo(block, prompt, squashed_prompt)]


def _rect_tuple(rect):
    """UIA 矩形统一成 `(left, top, right, bottom)`；无效返回 None。"""
    if rect is None:
        return None
    try:
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except AttributeError:
        try:
            left, top, right, bottom = rect
        except Exception:                     # noqa: BLE001
            return None
    if bottom <= top or right <= left:
        return None
    return (left, top, right, bottom)


def _join_by_lines(items) -> str:
    """把同一视觉行的碎片拼回一行，跨行才换行。

    为什么：chatgpt 的行内代码（`safe_ui.py` / `_ask_upload_body`）在控件树里是
    **独立节点**，按节点换行会把一句「引用」拆成 3 行（2026-11-12 实测 Round 2：
    5 处引句里 4 处被拆断，验真只认出 1 处，把一个引句齐全的回复判成 unread）。
    这里按控件矩形回拼：垂直中心相差 ≤ 半行高、且水平相邻（间隔 ≤ 40px）的算同一行。
    """
    lines: list[dict] = []
    for text, rect in items:
        box = _rect_tuple(rect)
        if box is None:
            lines.append({"center": None, "parts": [text], "left": 0, "right": 0})
            continue
        left, top, right, bottom = box
        center = (top + bottom) / 2
        height = bottom - top
        if lines:
            last = lines[-1]
            same_line = (
                last["center"] is not None
                and abs(last["center"] - center) <= max(4.0, height * 0.45)
                and left - last["right"] <= 40
            )
            if same_line:
                last["parts"].append(text)
                last["left"] = min(last["left"], left)
                last["right"] = max(last["right"], right)
                continue
        lines.append({"center": center, "parts": [text],
                      "left": left, "right": right})
    return "\n".join("".join(line["parts"]) for line in lines).strip()


def _merged_blocks(texts):
    """把所有块拼成一整串，并记录每块占的字符区间。

    为什么要拼：grok 把一条提问拆成 9~10 个节点（`**`、`只依据第一章`、`**`…），
    任何"单块匹配"的判据都会在气泡中间断掉。拼起来才能确定气泡的**起止块**。
    """
    parts, spans, offset = [], [], 0
    for text in texts:
        # 去掉 markdown 强调符号：grok 把 `**` 渲染成**独立节点**，
        # 留在拼接串里会把提问切成两半，needle 永远匹配不上（2026-11-12 实测）。
        squashed = "".join(text.split()).replace("*", "")
        parts.append(squashed)
        spans.append((offset, offset + len(squashed)))
        offset += len(squashed)
    return "".join(parts), spans


def _prompt_span(texts, needle: str, start_at: int = 0):
    """在拼接文本里找 `needle`，返回 `(首块, 末块, 结束偏移)`；找不到返回 None。"""
    if not needle:
        return None
    merged, spans = _merged_blocks(texts)
    pos = merged.find(needle, start_at)
    if pos < 0:
        return None
    end = pos + len(needle)
    first = next((i for i, (_a, b) in enumerate(spans) if b > pos), None)
    last = next((i for i, (_a, b) in enumerate(spans) if b >= end), None)
    if first is None or last is None:
        return None
    return first, last, end


def is_progress_noise(text: str) -> bool:
    """端自己的过程行/品牌名，不是答案。

    grok 会把「Analyzing the specification document」这类思考步骤和「工作了 57s」
    一起挂进控件树（2026-11-12 实测 6 条），留着会被验真算成一条没有引句的意见。
    """
    probe = (text or "").strip()
    if not probe:
        return True
    if probe in ("Grok", "ChatGPT", "DeepSeek", "Gemini") or probe.endswith("已完成回复"):
        return True
    if probe.startswith("工作了") and probe.endswith("s"):
        return True
    # 纯英文的进行时短句（首字母大写、无句末标点）——本项目的会审都用中文
    if (probe.isascii() and probe[:1].isupper() and len(probe) < 80
            and not probe.endswith((".", "。", "!", "?", ":", ";", "；"))):
        return True
    return False


def read_answer_text(screen, site: str, prompt: str) -> str:
    """从控件树里按顺序读答案文本（复制键点不到时的兜底）。

    两步，先准后广：

    1. **锚在提问回声上**。树序常见形状是「侧栏标题 … 提问回声 … 答案 … 提问回声」，
       取**第一个**回声之后、下一个回声（或「已完成回复」标记）之前的块。
       grok 实测就是这样：0–6 是侧栏、7 是回声、8–49 是答案、50 是「Grok 已完成回复」、
       51 又是回声。按关键字猜起点会把侧栏标题一起带进来。
    2. 找不到回声时，退回按合同首行「可访问=…」找起点。
    """
    from qi import ui

    squash = lambda x: "".join(x.split())          # noqa: E731
    sp = squash(prompt)
    key = sp[:36]
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                     # noqa: BLE001
        return ""

    noise = {"回答已完成", "最新一条回复", "复制", "编辑", "朗读", "分享", "重新生成回复"}
    done = re.compile(r"(已完成回复|回答已完成|已完成)$")
    seen, blocks = set(), []
    for node in root.walk():
        if node.kind != "TextControl":
            continue
        text = (node.name or "").strip()
        if not text or text in noise or is_progress_noise(text):
            continue
        # 输入框占位符会作为独立文本节点出现（grok「畅所欲问」、gemini「问问 Gemini」）
        if text in SITE_PLACEHOLDERS.get(site, ()):  # noqa: F821 - 定义在本文件后段
            continue
        try:
            rect = node.control.BoundingRectangle
        except Exception:                 # noqa: BLE001 - 拿不到矩形就按独立行处理
            rect = None
        # **不能按长度丢**：chatgpt 把「」渲染成独立节点，一句引用的收尾就一个字符；
        # 原来 `len(text) < 8` 把它丢了，于是每条引句都缺右括号、验真全判不中
        # （2026-11-12 实测：5 条引句只有 1 条能配上对）。
        # 去重也改成按「文本 + 位置」——单字符节点会重复出现多次。
        dedupe_key = (text, getattr(rect, "top", None), getattr(rect, "left", None)) \
            if rect is not None else (text, None, None)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        blocks.append((text, rect))

    def is_echo(text: str) -> bool:
        """这一块是不是提问（或提问的一段）。

        grok 会把一条提问拆成 3 个 TextControl 渲染（2026-11-12 实测），
        只认「整段包含前缀」会判成 prompt_gone；这里把「是提问的连续片段」
        也算进来（≥12 字，短片段太容易撞上正文）。
        """
        squashed = squash(text)
        if not squashed:
            return False
        if key and key in squashed:
            return True
        return len(squashed) >= 12 and squashed in sp

    texts = [text for text, _rect in blocks]
    # 提问可能被拆成多块，所以用**拼接后的整段**定位气泡起止：先找到它占的最后一块，
    # 答案从下一块开始；再找一次同样的一段（grok 会在答案末尾再回显一遍）作为结束。
    # 注意提问里的 markdown 强调（`**`）不会出现在渲染文本里，比较前先去掉。
    stripped_prompt = sp.replace("*", "")
    needle = stripped_prompt[:60]
    first = next((i for i, t in enumerate(texts) if is_echo(t)), -1)

    def collect(start_index: int, stop_index=None):
        out = []
        for index, (text, rect) in enumerate(blocks[start_index:], start=start_index):
            if stop_index is not None and index >= stop_index:
                break                       # 末尾那条回声 = 已经走过答案了
            if stop_index is None and is_echo(text):
                break
            if done.search(text):
                break
            out.append((text, rect))
        return out

    if first >= 0:
        body = collect(first + 1)
        # 旧判据优先（多轮真机验证过）。只有它取到的正文短得不像答案时，才用
        # 「拼接后定位气泡起止」——那条路能处理提问被拆成 9~10 块的情况，
        # 但也更容易误锚到侧栏标题，所以不放在主路径上。
        if len(_join_by_lines(body)) < 80:
            # 用**能匹配上的最长前缀**定位气泡：前缀越短，落点越可能停在气泡中段
            # （实测 60 字只覆盖到提问的第三块，答案会从半句提问开始）。
            span = None
            for size in (len(stripped_prompt), 200, 160, 120, 90, 60):
                if size < 20:
                    continue
                candidate = stripped_prompt[:size]
                span = _prompt_span(texts, candidate)
                if span:
                    needle = candidate
                    break
            if span:
                stop = _prompt_span(texts, needle, start_at=span[2])
                start_at = span[1] + 1
                # 余下的提问碎片（`**`、`一句`…）继续跳过，直到出现不属于提问的块。
                while start_at < len(blocks):
                    probe = "".join(blocks[start_at][0].split()).replace("*", "")
                    if len(probe) >= 2 and probe in stripped_prompt:
                        start_at += 1
                        continue
                    break
                alt = collect(start_at, stop[0] if stop else None)
                if len(_join_by_lines(alt)) > len(_join_by_lines(body)):
                    body = alt
        if body:
            # 与 `_drop_prompt_echo` 同一套规则：只在前序区剥回声，进入意见区就停。
            head = _echo_head([t for t, _r in body])
            out = []
            for position, (text, rect) in enumerate(body):
                if not (position >= head and head > 0) and _is_prompt_echo(text, prompt, sp):
                    continue
                if out and out[-1][0] == text:
                    continue
                out.append((text, rect))
            return _join_by_lines(out)

    # 退回：只认「可访问」开头的块（chatgpt 的答案就是这个形状）。
    out, started = [], False
    for text, rect in blocks:
        if is_echo(text):
            continue
        if not started:
            if text.startswith("可访问") or text.startswith("NO_ACCESS"):
                started = True
            else:
                continue
        out.append((text, rect))
    kept = []
    for text, rect in out:
        if _is_prompt_echo(text, prompt, sp):
            continue
        if kept and kept[-1][0] == text:
            continue
        kept.append((text, rect))
    return _join_by_lines(kept)


#: 各端的输入框占位文字。`composer_empty` 的结构判据要求把它显式传进去，
#: 否则空输入框会被当成草稿（gemini 实测：值就是「问问 Gemini」）。
SITE_PLACEHOLDERS = {"gemini": ("问问 Gemini",), "grok": ("畅所欲问",)}


def _conversation(screen, site: str):
    from qi import conversation

    return conversation.Conversation(
        screen, site,
        empty=lambda view: conversation.composer_empty(
            view, placeholders=SITE_PLACEHOLDERS.get(site, ())))


def _clear_composer(screen, site: str) -> bool:
    """点输入框 → Ctrl+A → Delete 清空。

    **必须把点击点夹进窗口矩形**：chatgpt 的编辑区 UIA 矩形会超出窗口下边界
    （实测 402..1286，而窗口只到 780），点它的中心等于点在桌面上，
    焦点立刻被桌面抢走，后面的按键全部 ForegroundLost。
    """
    from qi import ui
    from qi.upload import _raw_click

    try:
        import uiautomation as auto

        conv_ctl = auto.ControlFromHandle(screen.target_hwnd)
        win = conv_ctl.BoundingRectangle
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
        editors = [n for n in root.walk() if n.kind == "EditControl" and not n.offscreen]
        if not editors:
            return True
        rect = editors[0].control.BoundingRectangle
        # 取「控件矩形 ∩ 窗口矩形」的中心：
        # · gemini 的输入框很小且在窗口内 —— 中心就是框里；
        # · chatgpt 的编辑区会超出窗口下沿 —— 交集中心仍在窗口内。
        left, right = max(rect.left, win.left), min(rect.right, win.right)
        top, bottom = max(rect.top, win.top), min(rect.bottom, win.bottom)
        if right - left < 8 or bottom - top < 8:
            left, right, top, bottom = win.left, win.right, win.top, win.bottom
        x = (left + right) // 2
        y = (top + bottom) // 2
        from qi import safe_ui

        verdict = safe_ui.safe_click(screen.target_hwnd, x, y)
        if not verdict.ok:
            return False          # 点位不安全（桌面/他窗/客户区外）：一个键都不发
        time.sleep(0.3)
        # S1：发键前双检——前台是目标窗口 + 焦点在可编辑控件上；
        # 2026-11-12 就是这样把 Ctrl+A/Delete 发到桌面、选中 155 个图标的。
        import ctypes

        import uiautomation as auto

        try:
            role = auto.GetFocusedControl().ControlTypeName
        except Exception:                     # noqa: BLE001
            role = None
        key_verdict = safe_ui.decide_keys(
            keys=("ctrl", "a"), foreground_hwnd=ctypes.windll.user32.GetForegroundWindow(),
            target_hwnd=screen.target_hwnd, focused_role=role)
        if not key_verdict.ok:
            return False
        screen.hotkey("ctrl", "a")
        time.sleep(0.1)
        screen.press("delete")
        time.sleep(0.3)
        return True
    except Exception:                     # noqa: BLE001
        return False


def harvest_reply(screen, site: str, wait: float = 420.0) -> dict:
    """等「停止」消失、最后一条消息可复制且内容稳定，再复制回来。

    为什么需要它：`conversation.ask` 只看动作条上的复制键数量，
    chatgpt 在长回答**仍在生成**时复制键是 0，于是判超时；等它真生成完，
    这里按「停止键消失 + 复制内容两次相同」收卷。
    """
    from qi import ui

    def scroll_bottom(times: int = 6) -> None:
        """把页面滚到底。动作条常在可见区之外（仓库文档记过 y≈2014 的坑），
        不滚到底根本点不到复制键。"""
        import ctypes

        u32 = ctypes.windll.user32
        u32.mouse_event(0x0800, 0, 0, -120 * times, 0)
        time.sleep(0.6)

    end = time.time() + wait
    last_text, stable_since = "", None
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                 # noqa: BLE001
            time.sleep(2.0)
            continue
        busy = any((n.name or "").strip() in ("停止", "Stop") for n in root.walk()
                   if n.kind == "ButtonControl")
        copies = [n for n in root.walk()
                  if n.kind == "ButtonControl" and (n.name or "").strip() in ("复制", "Copy")]
        if busy or not copies:
            stable_since = None
            time.sleep(2.0)
            continue
        scroll_bottom()
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
            copies = [n for n in root.walk()
                      if n.kind == "ButtonControl"
                      and (n.name or "").strip() in ("复制", "Copy")]
        except Exception:                 # noqa: BLE001
            time.sleep(2.0)
            continue
        if not copies:
            time.sleep(2.0)
            continue
        rect = copies[-1].control.BoundingRectangle
        seq_before = screen.clipboard_seq()
        try:
            screen.focus()            # 长等待里焦点可能掉到桌面，先抢回来
            screen.click_xy((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        except Exception:             # noqa: BLE001 - 焦点抢不回来就用裸点击
            __import__("qi.upload", fromlist=["x"])._raw_click(
                (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        # **必须**等剪贴板序号变化：点空时读到的是用户自己的剪贴板内容
        # （实测抓到过一段地址+电话），那不是回答。
        for _ in range(20):
            if screen.clipboard_seq() != seq_before:
                break
            time.sleep(0.15)
        if screen.clipboard_seq() == seq_before:
            time.sleep(2.0)
            continue
        text = screen.clipboard() or ""
        if text and text == last_text:
            return {"ok": True, "stage": "harvested", "reply": text}
        last_text, stable_since = text, time.time()
        time.sleep(3.0)
    return {"ok": False, "stage": "harvest_timeout", "reply": last_text}


#: 账本里这些状态意味着「可能已经发出去过」——只对账，不重发。
RECONCILE_STATES = ("accepted", "submitting", "unknown")


def _ledger_state(site: str, prompt: str) -> str:
    from qi import submissions

    try:
        return submissions.read_state(site, prompt) or "none"
    except Exception:                     # noqa: BLE001 - 读不出账本按"未知"处理更安全
        return "unknown"


def _page_has(screen, site: str, needle: str) -> bool:
    """页面上有没有这段文字（续跑时核对"上一段到底发出去没有"）。"""
    from qi import ui

    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                     # noqa: BLE001
        return False
    return any(needle in (node.name or "") for node in root.walk())


def _reconcile_gate(screen, site: str, ask_text: str) -> dict | None:
    """发之前的账本闸门：有记录就只对账，不重发。

    返回 None 表示可以发；返回 dict 表示接管本次结果。
    `accepted/submitting/unknown` 一律先只读查答：
      · 页面已有答案 → 直接采用（不重发）；
      · 页面没有答案 → `needs_manual_clear`，交人工确认后清账本，**绝不自动清、不重发**。
    """
    state = _ledger_state(site, ask_text)
    if state not in RECONCILE_STATES:
        return None
    text = read_answer_text(screen, site, ask_text)
    if len(text) >= 300:
        return {"ok": True, "stage": "already_answered", "reply": text,
                "feed": "reconcile", "ledger": state}
    return {"ok": False, "stage": "needs_manual_clear", "ledger": state,
            "hint": "账本里已有该提问的提交记录，但页面找不到答案；按「只对账不重发」停手。"
                    "人工核对后用 tools/submission_clear.py --file <记录> --state "
                    f"{state} --reason ... --confirm 清掉再跑"}


def _url_now(screen, site: str) -> str:
    from qi import ui

    try:
        _control, url = ui.document(screen.target_hwnd, site)
        return url or ""
    except Exception:                     # noqa: BLE001
        return ""


def _navigate(screen, url: str, wait: float = 3.0) -> bool:
    """把当前标签导航到 `url` 并确认地址真的变了。"""
    if not url:
        return False
    try:
        screen.hotkey("ctrl", "l")
        time.sleep(0.3)
        screen.type_text(url)
        time.sleep(0.2)
        screen.press("enter")
        time.sleep(wait)
    except Exception:                     # noqa: BLE001 - 前台被抢等
        return False
    return _url_now(screen, getattr(screen, "tab_site", "")) .split("?")[0] == url.split("?")[0]


def _session_plan(screen, site: str, state=None) -> dict:
    """S3：按锚点决定 stay / navigate / new_allowed / session_lost。"""
    from qi import session_anchor as sa

    anchor = None
    if state is not None:
        raw = (state.data.get("anchors") or {}).get(site)
        if raw:
            anchor = sa.SessionAnchor.from_dict(raw)
    decision = sa.resolve(anchor, _url_now(screen, site))
    if decision["action"] == "navigate":
        decision["ok"] = _navigate(screen, decision["url"])
        if not decision["ok"] and anchor is not None and anchor.accepted:
            return {"action": "session_lost", "url": decision["url"],
                    "reason": "navigate_failed_after_accepted"}
    return decision


def _remember_anchor(state, site: str, round_name: str, prompt_hash: str,
                    accepted: bool) -> None:
    """发送成功后把本轮会话钉住（下次续跑不再新开）。"""
    from qi import session_anchor as sa

    if state is None:
        return
    url = _url_now(_screen_for(site), site)
    if not url:
        return
    anchors = state.data.setdefault("anchors", {})
    anchors[site] = sa.SessionAnchor(site=site, round_name=round_name, url=url,
                                     prompt_hash=prompt_hash,
                                     accepted=bool(accepted)).to_dict()
    state.save()


def _clear_stale_lock(state=None) -> dict:
    """开跑前清掉「持有者已死」的残留锁，并把结果记进 state。

    只改写**确认已死**的元数据；活着的锁一律不动（真并发时这里只会返回 held/alive）。
    """
    from qi import uilock

    info = uilock.clear_stale()
    if state is not None:
        state.set("lock", {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           **{k: info.get(k) for k in ("cleared", "reason", "pid", "owner")}})
    return info


def _ask_upload(site: str, prompt: str, material: Path, cfg, state=None,
                force_new: bool = False) -> dict:
    """上传 + 短提问。**整段持 UI 锁**：这段时间全是网页端动作。

    离线步骤（验真、匿名、归档）一律不取锁——锁只在"要动网页"的时候存在。
    """
    from qi import uilock

    with uilock.hold(f"iterate:upload:{site}",
                     timeout=float(cfg.get("ui_lock_timeout", 180))):
        return _ask_upload_body(site, prompt, material, cfg, state, force_new)


def _ask_upload_body(site: str, prompt: str, material: Path, cfg, state=None,
                     force_new: bool = False) -> dict:
    from qi import conversation, upload

    screen = _screen_for(site)
    _clear_composer(screen, site)
    # S3：先看锚点再决定要不要新开。漂移就回跳；已 accepted 还漂移就硬停，绝不新建。
    # 汇总必须**每次**新开对话：同一个会话里既有原意见又有汇总，匿名就白做了。
    # 锚点机制是为"续跑同一次会审"设计的，不能用在汇总上（2026-11-12 实测：
    # 第二次汇总复用了第一次的会话，session_id 完全没变）。
    plan = ({"action": "new_allowed", "url": ""} if force_new
            else _session_plan(screen, site, state))
    if plan["action"] == "session_lost":
        return {"ok": False, "stage": "session_lost", "feed": "upload",
                "hint": "锚点漂移且已 accepted；按 §10 不新建、不重发，保留账本"}
    if plan["action"] == "new_allowed":
        if not start_new_chat(screen, site):
            return {"ok": False, "stage": "new_chat_failed", "feed": "upload"}
    elif plan["action"] == "navigate" and not plan.get("ok"):
        return {"ok": False, "stage": "session_navigate_failed", "feed": "upload",
                "hint": "锚点回跳失败，且未 accepted；未发送"}
    up = upload.upload(screen, site, str(material), timeout=cfg["upload_timeout"])
    if not up.ok:
        return {"ok": False, "stage": "upload_failed", **up.as_dict()}
    if not _upload_settled(site, screen):
        return {"ok": False, "stage": "upload_not_settled", "upload": up.as_dict()}
    # 上传模式材料在附件里，提问用一句短指令即可：长多行提问在部分端会写不进去。
    # 每端可覆盖提问：deepseek 看到 `docs/...` 会去仓库找，然后回 NO_ACCESS
    # （2026-11-12 实测），所以给它一条明确「只读附件」的提问。
    ask_text = str((cfg.get("upload_prompt_by_site") or {}).get(site)
                   or cfg.get("upload_prompt") or prompt)
    # 账本优先：已有 accepted/submitting/unknown 就先只读对账，**不重发**。
    gated = _reconcile_gate(screen, site, ask_text)
    if gated is not None:
        gated["upload"] = up.as_dict()
        return gated
    prompt_id = it.prompt_identity(site, getattr(state, "round_dir", None) and state.round_dir.name or "",
                                   ask_text)
    if state is not None:
        state.mark_chunk(prompt_id, "sending")     # 发送**之前**落盘，崩溃后不留空白
    _refresh_tier(site)                  # 回执一次性；上一次失败也会把它用掉
    conv = _conversation(screen, site)
    result = conversation.ask(conv, ask_text, timeout=cfg["review_timeout"],
                              purpose="review")
    if state is not None:
        state.mark_chunk(prompt_id, "sent" if result.get("ok") else "unknown",
                         chars=len(result.get("reply") or ""))
    _remember_anchor(state, site, getattr(state, "round_dir", None) and state.round_dir.name or "",
                     prompt_id, accepted=bool(result.get("ok")
                                              or result.get("submission") == "accepted"))
    if not result.get("ok") and result.get("stage") in ("timeout", "interrupted", "harvest_timeout"):
        harvested = harvest_reply(screen, site, wait=float(cfg.get("harvest_wait", 420)))
        if harvested["ok"]:
            result = harvested
        else:
            # 复制键点不到时，直接从控件树读（答案确实在，只是动作条不可点）。
            text = read_answer_text(screen, site, ask_text)
            if len(text) >= 300:
                result = {"ok": True, "stage": "text_extracted", "reply": text}
    result["feed"] = "upload"
    result["upload"] = up.as_dict()
    return result


def _ask_upload_script(site: str, prompt: str, material: Path, cfg, state=None) -> dict:
    """上传附件，再用该端自己的 `*_use.py` 写短指令并发送。

    为什么不用 `conversation.ask`：gemini 在 upload 模式下会把「写了字、根本没
    发出去」判成 accepted（2026-11-12 两次实测：账本 accepted，但地址仍是 `/app`、
    输入框里还留着提问、0 个消息块），随后永远被 `submission_pending` 拦住。
    各端 `*_use.py` 自带落点校验（`submit_not_landed`）与收卷逻辑，更可靠。
    """
    from qi import upload

    screen = _screen_for(site)
    _clear_composer(screen, site)
    if not start_new_chat(screen, site):
        return {"ok": False, "stage": "new_chat_failed", "feed": "upload-script"}
    from qi import uilock

    # 只包上传这一段：后面的 `*_use.py` 是子进程，它自己会 uilock.run，
    # 父进程若也持锁就直接自锁死（2026-11-12 设计时排除）。
    with uilock.hold(f"iterate:upload:{site}",
                     timeout=float(cfg.get("ui_lock_timeout", 180))):
        up = upload.upload(screen, site, str(material), timeout=cfg["upload_timeout"])
        settled = _upload_settled(site, screen)
    if not up.ok:
        return {"ok": False, "stage": "upload_failed", "feed": "upload-script",
                **up.as_dict()}
    if not settled:
        return {"ok": False, "stage": "upload_not_settled", "feed": "upload-script",
                "upload": up.as_dict()}

    ask_text = str((cfg.get("upload_prompt_by_site") or {}).get(site)
                   or cfg.get("upload_prompt") or prompt)
    gated = _reconcile_gate(screen, site, ask_text)   # 账本优先，只对账不重发
    if gated is not None:
        gated["upload"] = up.as_dict()
        return gated
    prompt_id = it.prompt_identity(site, getattr(state, "round_dir", None) and state.round_dir.name or "",
                                   ask_text)
    if state is not None:
        state.mark_chunk(prompt_id, "sending")
    _refresh_tier(site)                  # 回执一次性，发送前重拿一条
    script = ROOT / "src" / "scripts" / f"{site}_use.py"
    budget = int(cfg["review_timeout"])
    proc = subprocess.run(
        [sys.executable, str(script), ask_text, "--purpose", "review",
         "--continue", "--timeout", str(budget)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        timeout=budget + 180)
    parsed = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
    result = dict(parsed or {"ok": False, "stage": "bad_output",
                             "exit": proc.returncode})
    if state is not None:
        state.mark_chunk(prompt_id, "sent" if result.get("ok") else "unknown",
                         chars=len(result.get("reply") or ""))
    result["feed"] = "upload-script"
    result["upload"] = up.as_dict()
    if not result.get("ok") and result.get("stage") in ("timeout", "interrupted"):
        text = read_answer_text(screen, site, ask_text)
        if len(text) >= 300:
            result = {"ok": True, "stage": "text_extracted", "reply": text,
                      "feed": "upload-script", "upload": up.as_dict()}
    return result


def _site_object(name: str):
    """复用各端 `*_use.py` 里打磨过的 Site 配置（新会话、输入框、发送键）。"""
    import importlib.util

    path = ROOT / "src" / "scripts" / f"{name}_use.py"
    spec = importlib.util.spec_from_file_location(f"{name}_use_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SITE


def _choose_tier(site: str, purpose: str = "review") -> dict:
    """跑该端的 `*_choose.py` 选档位。

    `purpose=review` 需要档位回执，否则 `conversation.ask` 直接 `tier_unconfirmed`
    （deepseek 实测）。这一步本来就是会审流程的一部分。
    """
    import subprocess

    script = ROOT / "src" / "scripts" / f"{site}_choose.py"
    if not script.is_file():
        return {"ok": False, "reason": "no_choose_script"}
    proc = subprocess.run([sys.executable, str(script), "--purpose", purpose],
                          cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=180)
    return {"ok": proc.returncode == 0, "exit": proc.returncode,
            "tail": (proc.stdout or "")[-200:]}


#: 各端 review 档位回执怎么刷新。回执**一次性**：发一条就作废，
#: 第二条必须重新 choose，否则 `tier_unconfirmed`（deepseek/gemini 都实测到）。
#: gemini 的 choose 不认 `--think`（文档写着"当前未实现"），只能重设模型；
#: review 要最高档，gemini 的档位序是 flash_lite→flash→pro→argon。
TIER_RECIPE = {"gemini": ("--model", "argon"), "deepseek": ("--think", None)}


def _refresh_tier(site: str) -> None:
    """写一条新的档位回执。不支持的端直接跳过（它们不校验回执）。"""
    import subprocess

    script = ROOT / "src" / "scripts" / f"{site}_choose.py"
    if not script.is_file() or site not in TIER_RECIPE:
        return
    flag, value = TIER_RECIPE[site]
    if value is None:                     # deepseek：先关再开，否则"已是 on"不写回执
        rounds = [("--think", "off"), ("--think", "on")]
    else:
        rounds = [(flag, value)]
    for key, val in rounds:
        try:
            subprocess.run([sys.executable, str(script), key, val],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", timeout=180)
        except Exception:                 # noqa: BLE001
            pass


def _ask_via_script(site: str, prompt: str, material_text: str, cfg,
                    state=None) -> dict:
    """走各端自己的 `*_use.py`（它们管新会话、档位、粘贴保真）。

    为什么不自己拼 `flow.ask`：deepseek 的 review 档位回执由 **use 脚本**内部
    `tier_guard.gate` 处理，`*_choose.py` 只认 `--think/--search`，接不上。

    续跑：每段有固定身份（`chunk:<sha>`），已 `sent` 的跳过；`sending` 是
    "上次发送途中被杀"，先对着页面核对这一段在不在，在就记账跳过、不在才补发。
    """
    import subprocess

    script = ROOT / "src" / "scripts" / f"{site}_use.py"
    limit = int((cfg.get("chunk_chars_by_site") or {}).get(site, cfg["chunk_chars"]))
    chunks = it.pack_chunks_with_ids(material_text, limit, head=prompt, site=site)
    total = len(chunks)
    if chunks:
        _clear_composer(_screen_for(site), site)      # 残草会让脚本直接拒发
    pending = it.resume_chunks(chunks, state)
    skipped = total - len(pending)
    if chunks and not pending:
        text = read_answer_text(_screen_for(site), site, prompt)
        return {"ok": bool(text), "stage": "resumed_read" if text else "needs_manual_clear",
                "reply": text, "feed": "paste", "chunks": total,
                "resumed_skip": skipped}

    last = {"ok": False, "stage": "no_chunks"}
    for chunk in pending:
        index, body = chunk["index"], chunk["text"]
        record = state.chunk(chunk["id"]) if state is not None else {}
        if record.get("status") == "sending":
            # 上次发送途中被杀：只核对，不盲目重发。
            if _page_has(_screen_for(site), site, f"第 {index}/{total} 段"):
                state.mark_chunk(chunk["id"], "sent", reconciled=True)
                continue
        _refresh_tier(site)              # 档位回执是一次性的：每段都要重拿
        budget = cfg["review_timeout"] if index == total else int(
            cfg.get("chunk_timeout", 300))
        cmd = [sys.executable, str(script), body, "--purpose", "review",
               "--timeout", str(int(budget))]
        if index > 1:
            cmd.append("--continue")
        if state is not None:
            state.mark_chunk(chunk["id"], "sending")
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=budget + 120)
        parsed = None
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except ValueError:
                    continue
        last = parsed or {"ok": False, "stage": "bad_output", "exit": proc.returncode}
        if state is not None:
            state.mark_chunk(chunk["id"], "sent" if last.get("ok") else "unknown",
                             chars=len(last.get("reply") or ""))
        if not last.get("ok"):
            return {**last, "feed": "paste", "chunks": total, "at": index,
                    "resumed_skip": skipped, "stderr": (proc.stderr or "")[-300:]}
    return {**last, "feed": "paste", "chunks": total, "resumed_skip": skipped}


def _ask_paste(site: str, prompt: str, material_text: str, cfg, state=None) -> dict:
    """分段粘贴：优先走各端 `*_use.py`。"""
    if (ROOT / "src" / "scripts" / f"{site}_use.py").is_file():
        return _ask_via_script(site, prompt, material_text, cfg, state)
    from qi import flow

    site_obj = _site_object(site)
    chosen = _choose_tier(site)
    limit = int((cfg.get("chunk_chars_by_site") or {}).get(site, cfg["chunk_chars"]))
    if site in (cfg.get("single_line_sites") or []):
        # deepseek / grok 的输入框折叠换行，读回必然 input_mismatch。
        material_text = material_text.replace("\n", " ⏎ ")
        prompt = prompt.replace("\n", " ⏎ ")
    chunks = it.pack_chunks(material_text, limit, head=prompt)
    last = {"ok": False, "stage": "no_chunks"}
    if chunks:
        _clear_composer(_screen_for(site), site)      # 上一轮残草会判 draft_present
    for index, chunk in enumerate(chunks, start=1):
        budget = cfg["review_timeout"] if index == len(chunks) else int(
            cfg.get("chunk_timeout", 300))
        last = flow.ask(site_obj, chunk, mode=("new" if index == 1 else "continue"),
                        timeout=budget, purpose="review", verbose=False)
        if not last.get("ok"):
            return {**last, "feed": "paste", "chunks": len(chunks), "at": index}
    return {**last, "feed": "paste", "chunks": len(chunks), "choose": chosen}


def cmd_review(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    _clear_stale_lock(state)             # 续跑前先处理上一条命令的死进程残留
    done = state.done(f"review:{args.site}")
    if done and not args.force:
        print(json.dumps({"skipped": "already_done", **done}, ensure_ascii=False))
        return 0

    prompt_path = round_dir(args.round) / "prompt.md"
    if not prompt_path.is_file():
        print(json.dumps({"ok": False, "error": "prompt_missing",
                          "hint": f"先写 {prompt_path}"}, ensure_ascii=False))
        return 2
    prompt = prompt_path.read_text(encoding="utf-8")
    material = round_dir(args.round) / "materials" / "material.md"
    mode = str(cfg["feed"].get(args.site, "paste"))

    started = time.time()
    if mode in ("upload", "upload-script"):
        if not material.is_file():
            print(json.dumps({"ok": False, "error": "material_missing"}, ensure_ascii=False))
            return 2
        ask = _ask_upload_script if mode == "upload-script" else _ask_upload
        result = ask(args.site, prompt, material, cfg, state)
    else:
        text = material.read_text(encoding="utf-8") if material.is_file() else ""
        result = _ask_paste(args.site, prompt, text, cfg, state)

    reply = result.get("reply") or ""
    replies_dir = round_dir(args.round) / "replies"
    replies_dir.mkdir(parents=True, exist_ok=True)
    (replies_dir / f"{args.site}.md").write_text(reply, encoding="utf-8")
    payload = {"ok": bool(result.get("ok")), "stage": result.get("stage"),
               "feed": result.get("feed", mode), "chars": len(reply),
               "elapsed": round(time.time() - started, 1)}
    state.mark(f"review:{args.site}", payload)
    print(json.dumps(payload, ensure_ascii=False))

    # 材料上传/粘贴的原始证据留档（不入库）
    evidence = ROOT / "evidence" / "iterations" / args.round
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{args.site}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------- 3. 验真
def _tree_paths(sha: str, rdir: Path | None = None) -> list:
    """真值表：优先读本轮的 `tree.txt`（离线可测），否则问 git。"""
    if rdir is not None:
        cached = rdir / "tree.txt"
        if cached.is_file():
            return [line.strip() for line in cached.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    try:
        out = git("ls-tree", "-r", "--name-only", sha)
    except Exception:                    # noqa: BLE001 - 离线测试守卫会拦 subprocess
        return []
    return [line for line in out.splitlines() if line.strip()]


def cmd_verify(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    snapshot = state.data.get("snapshot") or {}
    sha = str(snapshot.get("sha") or "")
    if not sha:
        cached = round_dir(args.round) / "tree.txt"
        if cached.is_file():
            sha = "unknown"              # 有真值表就够，不必惊动 git（离线干跑）
        else:
            try:
                sha = git("rev-parse", "HEAD")
            except Exception:            # noqa: BLE001 - 没有 git 也能降级
                sha = "unknown"
    tree = _tree_paths(sha, round_dir(args.round))
    spec_text = (ROOT / it.SPEC_PATH).read_text(encoding="utf-8")
    # 本轮材料（Round 1 起含架构与已知问题）也算"可引用的正文"；
    # 但"核心命中"仍只认规格本身。
    material_path = round_dir(args.round) / "materials" / "material.md"
    corpus = spec_text + ("\n\n" + material_path.read_text(encoding="utf-8")
                          if material_path.is_file() else "")

    replies_dir = round_dir(args.round) / "replies"
    verdicts, report = {}, {}
    for path in sorted(replies_dir.glob("*.md")):
        site = path.stem
        text = path.read_text(encoding="utf-8")
        if "NO_ACCESS" in text[:200]:
            verdict = {"hits": 0, "total": 0, "rate": 0.0, "core": False,
                       "verdict": "no_access", "rows": []}
        else:
            verdict = it.verify_reply(text, tree_paths=tree, spec_text=corpus,
                                      core_text=spec_text,
                                      core_paths=(it.SPEC_PATH,))
        verdicts[site] = verdict
        report[site] = {"chars": len(text), **{k: verdict[k] for k in
                                              ("hits", "total", "rate", "core", "verdict")}}

    valid = [s for s, v in verdicts.items() if v["verdict"] == "true_read"]
    summary = {"sha": sha, "valid_seats": valid, "valid_count": len(valid),
               "meets_three_vendors": len(valid) >= 3, "seats": report}

    it.write_archive(round_dir(args.round), {
        "quote_check.json": {"summary": summary, "detail": verdicts},
        "quote_check.md": _quote_md(summary, report),
    })
    state.mark("verify", {"valid": valid, "count": len(valid)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["meets_three_vendors"] else 1


def _quote_md(summary, report) -> str:
    lines = ["# 引用验真（Round 0 变体）", "",
             f"快照 SHA：`{summary['sha']}`", "",
             "| 端 | 字数 | 命中 | 总数 | 命中率 | 核心命中 | 判定 |",
             "|---|---:|---:|---:|---:|---|---|"]
    for site, row in sorted(report.items()):
        lines.append(f"| {site} | {row['chars']} | {row['hits']} | {row['total']} | "
                     f"{row['rate']} | {'是' if row['core'] else '否'} | {row['verdict']} |")
    lines += ["", f"有效（真读）：{', '.join(summary['valid_seats']) or '无'}；"
                  f"门槛 3 家：{'满足' if summary['meets_three_vendors'] else '未满足'}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 4. 匿名 + 5. 汇总
def cmd_anonymize(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    qc = json.loads((round_dir(args.round) / "quote_check.json").read_text(encoding="utf-8"))
    detail = qc["detail"]
    replies = {p.stem: p.read_text(encoding="utf-8")
               for p in sorted((round_dir(args.round) / "replies").glob("*.md"))}
    pack = it.anonymize(replies, verdicts=detail)
    (round_dir(args.round) / "anonymous_pack.md").write_text(pack, encoding="utf-8")
    state.mark("anonymize", {"entries": pack.count("## F")})
    print(json.dumps({"ok": True, "entries": pack.count("## F"),
                      "chars": len(pack)}, ensure_ascii=False))
    return 0


def _summarize_via_site(args, cfg, state, site: str) -> int:
    """用非 Gemini 的端做汇总（附件 + 短指令），并归档会话 id 与汇总方。"""
    pack_file = round_dir(args.round) / "anonymous_pack.md"
    ask = str((cfg.get("summary_prompt_by_site") or {}).get(site)
              or cfg.get("summary_prompt")
              or "附件是匿名审查意见。请按四点汇总：1) 按主题聚类并注明合并了哪几条；"
                 "2) 标出冲突；3) 按对本项目的好处排序；4) 你要补充的新缺陷单独列在「汇总者新增」。"
                 "不要猜来源，1500 字内。")
    local = dict(cfg)
    local.pop("upload_prompt", None)
    local["upload_prompt_by_site"] = {}
    result = _ask_upload(site, ask, pack_file, local, state, force_new=True)
    reply = result.get("reply") or ""
    url = _url_now(_screen_for(site), site)
    session_id = conversation_session_id(url) if url else None
    payload = {"summarizer": site, "session_id": session_id, "url": url,
               "chars": len(reply), "stage": result.get("stage"),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (round_dir(args.round) / "gemini_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (round_dir(args.round) / "gemini_summary.md").write_text(reply, encoding="utf-8")
    state.mark("summarize", {"chars": len(reply), "session_id": session_id,
                             "summarizer": site})
    if not result.get("ok") or session_id is None:
        print(json.dumps({"ok": False, "stage": result.get("stage") or "summary_not_isolated",
                          "summarizer": site, "session_id": session_id,
                          "chars": len(reply)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "summarizer": site, "chars": len(reply),
                      "session_id": session_id}, ensure_ascii=False))
    return 0


def cmd_summarize(args, cfg) -> int:
    """匿名包 → Gemini 汇总。**必须先开新对话**，并把会话 ID 记进归档。

    为什么必须新开：汇总者与四审里都有 gemini，同一个会话等于让它"认出自己"，
    匿名就白做了（规格 §9.2，chatgpt/grok 都点过这条）。
    会话 ID 是"确实换了会话"的唯一客观证据——只写"我开了新对话"不算。
    """
    from qi import conversation, ui, uilock

    state = it.State.load(ROOT, args.round)
    _clear_stale_lock(state)
    site = (getattr(args, "site", None) or "gemini").strip().lower()
    if site != "gemini":
        # 换汇总方（规格默认 Gemini）。gemini 拒答时的退路：同样必须新开对话、
        # 同样把会话 id 写进归档，只把"谁来汇总"如实记下来。
        return _summarize_via_site(args, cfg, state, site)
    pack = (round_dir(args.round) / "anonymous_pack.md").read_text(encoding="utf-8")
    prompt = it.anonymize_prompt(pack)
    screen = _screen_for("gemini")
    _clear_composer(screen, "gemini")
    if str(cfg.get("summary_via") or "") == "upload":
        # 匿名包动辄上万字，gemini 的输入框塞不下（实测 input_mismatch）。
        # 改走附件 + 一条短指令；前提仍是**先新开对话**。
        from qi import upload as upload_mod

        pack_file = round_dir(args.round) / "anonymous_pack.md"
        with uilock.hold("iterate:summarize:gemini",
                         timeout=float(cfg.get("ui_lock_timeout", 180))):
            if not start_new_chat(screen, "gemini"):
                print(json.dumps({"ok": False, "stage": "new_chat_failed",
                                  "hint": "汇总必须新开对话；没开成就不发"},
                                 ensure_ascii=False))
                return 1
            up = upload_mod.upload(screen, "gemini", str(pack_file),
                                   timeout=cfg["upload_timeout"])
            if not up.ok:
                print(json.dumps({"ok": False, "stage": "upload_failed",
                                  **up.as_dict()}, ensure_ascii=False))
                return 1
            _upload_settled("gemini", screen)
        _refresh_tier("gemini")
        short = str(cfg.get("summary_prompt") or
                    "附件是匿名审查意见。请按四点汇总：1) 按主题聚类并注明合并了哪几条；"
                    "2) 标出冲突；3) 按对本项目的好处排序；4) 你要补充的新缺陷单独列在「汇总者新增」。"
                    "不要猜来源，1500 字内。")
        script = ROOT / "src" / "scripts" / "gemini_use.py"
        budget = int(cfg["review_timeout"])
        proc = subprocess.run([sys.executable, str(script), short, "--purpose", "review",
                               "--continue", "--timeout", str(budget)],
                              cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=budget + 180)
        parsed = None
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except ValueError:
                    continue
        result = dict(parsed or {"ok": False, "stage": "bad_output",
                                 "exit": proc.returncode})
    else:
        with uilock.hold("iterate:summarize:gemini",
                         timeout=float(cfg.get("ui_lock_timeout", 180))):
            if not start_new_chat(screen, "gemini"):
                print(json.dumps({"ok": False, "stage": "new_chat_failed",
                                  "hint": "汇总必须新开对话；没开成就不发"},
                                 ensure_ascii=False))
                return 1
            _refresh_tier("gemini")          # 回执一次性
            conv = _conversation(screen, "gemini")
            result = conversation.ask(conv, prompt, timeout=cfg["review_timeout"],
                                      purpose="review")

    url = ""
    try:
        _control, url = ui.document(screen.target_hwnd, "gemini")
    except Exception:                     # noqa: BLE001 - 读不到就记 None，由判定兜底
        pass
    session_id = conversation_session_id(url) if url else None
    reply = result.get("reply") or ""
    payload = {"session_id": session_id, "url": url, "chars": len(reply),
               "stage": result.get("stage"), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (round_dir(args.round) / "gemini_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (round_dir(args.round) / "gemini_summary.md").write_text(reply, encoding="utf-8")
    state.mark("summarize", {"chars": len(reply), "session_id": session_id})

    if not result.get("ok"):
        print(json.dumps({"ok": False, "stage": result.get("stage"),
                          "session_id": session_id}, ensure_ascii=False))
        return 1
    if session_id is None:
        # 页面没带会话 id = 没有真正进入新会话，汇总结果不可信。
        print(json.dumps({"ok": False, "stage": "summary_not_isolated",
                          "session_id": None, "chars": len(reply)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "chars": len(reply), "session_id": session_id},
                     ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 上传自检
def cmd_upload_check(args, cfg) -> int:
    """新会话 → 上传一个小文件 → 只问一句 → 答案必须来自文件内容。

    规格 §8 要求「文件已落到该端、对方按文件内容作答」，这一步就是那个自检。
    """
    from qi import conversation, upload

    site = args.site or "chatgpt"
    sample = ROOT / "docs" / "iterations" / args.round / "materials" / "upload_check.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    token = "BRIDGE-UPLOAD-CHECK-20261111"
    sample.write_text(f"# 上传自检\n\n本文件只有一行关键内容：{token}\n", encoding="utf-8")

    screen = _screen_for(site, new=True)
    if not start_new_chat(screen, site):
        print(json.dumps({"ok": False, "stage": "new_chat_failed"}, ensure_ascii=False))
        return 1
    _clear_composer(screen, site)
    up = upload.upload(screen, site, str(sample), timeout=cfg["upload_timeout"])
    if not up.ok:
        print(json.dumps({"ok": False, "stage": "upload_failed", **up.as_dict()},
                         ensure_ascii=False))
        return 1
    _upload_settled(site, screen)
    conv = _conversation(screen, site)
    result = conversation.ask(conv, "附件里那行关键内容是什么？只回答那一行，不要解释。",
                              timeout=cfg["review_timeout"], purpose="review")
    reply = (result.get("reply") or "").strip()
    ok = token.lower() in reply.lower()
    payload = {"ok": bool(result.get("ok")) and ok, "upload": up.as_dict(),
               "stage": result.get("stage"), "answer": reply[:200],
               "token_found": ok}
    (ROOT / "docs" / "iterations" / args.round / "upload_check.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


# ---------------------------------------------------------------- status
def cmd_status(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    print(json.dumps(state.data, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="自迭代循环驱动器")
    parser.add_argument("command", choices=("status", "snapshot", "review", "verify",
                                            "anonymize", "summarize", "upload-check"))
    parser.add_argument("--round", default="round-0")
    parser.add_argument("--site", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "review" and not args.site:
        parser.error("review 需要 --site")
    return {"status": cmd_status, "snapshot": cmd_snapshot, "review": cmd_review,
            "verify": cmd_verify, "anonymize": cmd_anonymize,
            "summarize": cmd_summarize, "upload-check": cmd_upload_check}[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
