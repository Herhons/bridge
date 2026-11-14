"""gemini 网页端专用：按控件树判「发出去了」和「答完了」。

2026-11-01 施工（CARD-GEM-fix-reply-detect）。11:4x 那次真机失败不是网络慢，是判据
在这个页面上根本不可能成立：

1. **提问气泡是折叠的。** 控件树里我们那条提问的文本，前面粘着标签「你说」，
   末尾是一个「…」（旁边有兄弟按钮「展开」）。`conversation.prompt_nodes` 要求
   整串精确相等，于是永远匹配 0 条 → `accepted` 一直是 False → `submission` 停在
   unknown → 240s 后报 timeout/no_reply，而页面上回复早就写完了。
   证据：`evidence\\gemini_bug_1215_uia.txt`，用旧判据跑这份树命中条数为 0。
2. **发送键模板过期。** 模式按钮现在叫「打开模式选择器，当前模式为“Argon”」，
   `gem_send_on.png` 最高只匹到 0.652（阈值 0.95）。拿它当「输入框有没有草稿」
   的判据就不可信了。
3. **动作条和「Gemini 说」都在控件树里**：答得好 / 答得不好 / 重做 / 复制 /
   显示更多选项。比模板稳，直接拿来当完成判据。

所以本模块只做三件事：
    · 按文档序把「你说 / Gemini 说」切成块（折叠的、重复的标签都容忍）；
    · 用「前 N 个字对得上」认领我们这条提问（截断尾部去掉再比）；
    · 用「回复动作条出现 ＋ 这条回复的文本稳定 3 秒」判完成。

发送仍先按控件名找按钮（页面上的真按钮），**模板只作兜底**——只在控件名这条路
彻底走不通时才去截图匹配。共享的 `qi/flow.py`、`qi/conversation.py` 一行不动：
另外三端还在用它们，改共享判据等于拿别人做试验。

2026-11-01 施工（CARD-WEB2）。三处现场失败，逐条对应：

1. **「发起新对话」不是 Button。** 控件树里它是 `H: 发起新对话` + `T: 发起新对话`
   （见 `evidence\\gemini_bug_1215_uia.txt`），旧代码只认 Button/Hyperlink **且只读一次树**，
   页面刚打开、侧栏还没渲染完就是 `newchat_not_found`（10:20、10:22 连续两次）。
   → `open_new_chat`：按名称在 Button/Hyperlink/Text 里找，读不到就隔 0.5s 再读（最多 8 次），
   最后才回落到 `gem_newchat` 模板。
2. **认领不到提问 ≠ 没有回复。** 10:35 那轮回复早就写完了（`--read` 一读就有），
   等待循环却因为 `prompt_at` 一直返回 None 白等到 300s，报 `no_reply`。
   → 归属加一条兜底：回复条数比发送前多，且最新那条自带动作条，也算本轮；
   → 超时前最后再读一次页面，读到就按成功返回（`_final_read`）。
3. **发送键模板过期**：`gem_send_on.png` 对现在的页面最高只到 0.65~0.69，阈值 0.95 永远不过。
   所以模板只在 UIA 找不到按钮时用，且报错里带上最高分 / 所在屏幕 / 缩放。
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from comtypes import COMError
import uiautomation as auto

from . import exit_codes as ec
from . import chip, evidence, failure, guard, heartbeat, human, precheck, sendgate, textcmp, tier_guard, ui, uilock
from . import zoom as zoom_mod
from .browser import ensure_site
# 2026-11-12（R3-6）：气泡认领规则与 grok 共用一处实现，见 qi/bubble.py
from .bubble import (MIN_PREFIX, TAIL_MARKS, already_answered as _already,
                     pick_reply as _pick, prompt_matches)
from .conversation import (SEND, STOP, Conversation, UserTookOver,
                           _looks_like_partial_write, path_of, session_id_of)
from .screen import ForegroundLost, Screen

# 真机类引用（test_cardweb2 会 patch `gemini.Screen` 这个名字，这里不受影响，
# concurrent_ui 检测用它判断「是不是真机屏幕」）。
_ScreenClass = Screen
from .ui import UiDocsLost
from .window import WindowMinimized

# 气泡标签。页面是中文界面，英文留一份给换语言时兜底。
PROMPT_LABELS = ("你说", "You said")
REPLY_LABELS = ("Gemini 说", "Gemini says")

# 只有回答动作条上才有的按钮：出现「答得好」或「复制」就算本轮已经在收尾。
DONE_BUTTONS = {"答得好", "复制", "Good response", "Copy"}
COPY_BUTTONS = {"复制", "Copy"}

# 任何一条动作条按钮（提问条和回答条都算）——出现即当前文本块结束。
BAR = {"答得好", "答得不好", "重做", "复制", "显示更多选项",
       "展开", "复制提示", "修改",
       "Good response", "Bad response", "Regenerate", "Copy", "Show more options",
       "Expand", "Edit", "More options"}

PLACEHOLDERS = {"问问 Gemini", "Ask Gemini"}

#: 写入后等编辑器把值刷进 UIA 的时长（秒）。旧版是"写→读→清→再写"，
#: 现在只读一次，这一拍必须留够；0.35 是 READBACK_SETTLE 实测值的下界。
WRITE_SETTLE = 0.35

# 「发起新对话」的可见名字。界面语言换过就加一条：找不到名称时才走模板。
NEWCHAT_NAMES = ("发起新对话", "新对话", "新聊天", "New chat", "New Chat")

# 模板兜底时试的缩放。本机 Chrome 的**页面缩放**会在 90%~125% 之间变
# （实测同一张 grok_send_on 在 100% 下截的图，换个缩放就只剩 0.490），
# 只按 1.0 匹配会稳定失配。
TEMPLATE_SCALES = (1.0, 0.9, 0.8, 1.1, 1.25)

# 折叠气泡的截断标记与最短认领前缀：定义在 `qi/bubble.py`（与 grok 共用）。
SETTLE = 3.0            # 卡面要求：回复文本稳定 3 秒
STABLE_READS = 2        # CARD-07 第 2 条：文本连续 2 次不变就算答完
# B19：点了发送之后，最多等这么久就要看到"落点"（提问块 / 新回复 / 生成中 / 会话 id）。
# 真机出现过"页面停在 /app、输入框已空、侧栏却新建了会话"，旧代码在这里空转 900 秒。
# 3 秒足够 SPA 换路由；没看到就先清空重发一次，再给 3 秒，仍没有才判死。
SUBMIT_LANDING_WAIT = 3.0
# B21（会审 P0）：3 秒内没有落点证据时，**先分清"端收没收到"再决定要不要再点**。
# 输入框已空 = 歧义态（可能已经发出去、只是提问块还没渲染），此时再点就是**二次发送**。
# 只有"框里还留着我们这段字"才是端确实没收下的正证据，才允许清空重写重点一次。
# 框空时改走静默窗：只等不点，覆盖"新建会话 + 首个 token"的慢路径。
SUBMIT_QUIET_WAIT = 6.0
SEP = (" ", "\u3000", ":", "：", "\n", "\t")   # 标签和正文之间的分隔符

# 页面全文兜底时要剔掉的尾部 UI 文案（输入框占位、按钮名、免责声明）。
# 只剔**整行完全相同**的，正文里出现同样的字不受影响。
DOM_NOISE = {"问问 Gemini", "Ask Gemini", "发送", "Send", "答得好", "答得不好",
             "重做", "复制", "显示更多选项", "展开", "修改", "复制提示",
             "Good response", "Bad response", "Regenerate", "Copy",
             "Show more options", "Expand", "Edit", "Gemini 可能会犯错，请核查",
             "Gemini can make mistakes, so double-check it"}


@dataclass
class Block:
    """一段气泡：label 是「你说」/「Gemini 说」，text 是**原样**正文。

    text 保留空格（回复要原样交给调用方），比较一律走 key（去掉所有空白的规范形）。
    早期版本直接存规范形，返回的回复就成了「仅限PLAN.md和」「GEM-2602OK」——
    中文看着还行，英文和代码会被糊成一片。
    """

    label: str
    text: str
    at: int                       # 块头在文档序里的下标（判归属用）
    bars: list[str] = field(default_factory=list)   # 紧跟其后的整排动作条按钮名

    @property
    def key(self) -> str:
        return ui.canonical(self.text)

    @property
    def closed_by(self) -> str | None:
        return self.bars[0] if self.bars else None


def _enabled(node) -> bool:
    try:
        return bool(node.control.IsEnabled)
    except (COMError, AttributeError):
        return bool(getattr(node, "enabled", True))


def _split_label(raw: str) -> tuple[str, str]:
    """把「你说 你是只读评审…」拆成 ("你说", "你是只读评审…")。

    必须以分隔符收尾才算标签：回复里出现「你说的对」时不能当成新提问的开头。
    """
    text = raw.strip()
    for label in PROMPT_LABELS + REPLY_LABELS:
        if text == label:
            return label, ""
        if text.startswith(label) and text[len(label):len(label) + 1] in SEP:
            return label, text[len(label):].strip()
    return "", ""


def parse(root) -> tuple[list[Block], int]:
    """按文档序切块。返回 (块列表, 动作条按钮数)。

    只认叶子节点：UIA 常把同一句文本在父节点上再挂一遍，父节点也收就会把
    每条回复读成两份。输入框（EditControl）一到就断块——气泡不可能跨过输入框，
    用户刚打的字也不会被当成回复。
    """
    blocks: list[Block] = []
    bar = 0
    cur: Block | None = None
    just_closed: Block | None = None    # 刚被动作条结束的块，后面几个按钮也归它
    last: str | None = None
    for at, node in enumerate(root.walk()):
        if node.kind in ("EditControl", "ComboBoxControl", "MenuControl"):
            cur = just_closed = last = None
            continue
        if node.kind == "ButtonControl":
            name = ui.canonical(node.name)
            if name in BAR:
                bar += 1
                if cur is not None:
                    cur.bars.append(name)
                    just_closed, cur = cur, None
                elif just_closed is not None:
                    just_closed.bars.append(name)   # 同一排的其余按钮
            continue
        if node.kind not in ("TextControl", "HyperlinkControl") or node.children:
            continue
        key = ui.canonical(node.name)
        if not key:
            continue
        label, body = _split_label(node.name)
        if label:
            # 同一个标签连出两个节点（UIA 的重复），第二个不算新块的开始
            if cur is not None and not cur.text.strip() and cur.label == label:
                continue
            cur = Block(label, body, at)
            just_closed = None
            last = ui.canonical(body) or None
            blocks.append(cur)
            continue
        if key in {ui.canonical(p) for p in PLACEHOLDERS}:
            cur = just_closed = last = None
            continue
        if cur is not None:
            # 相邻重复：真机上用户气泡把同一句话挂了两个节点（同屏、同文本），
            # 不挡掉就会把提问读成两遍，前缀比对直接失效。
            if key == last:
                continue
            cur.text += node.name        # 原样拼，空格是正文的一部分
            last = key
    return blocks, bar


# `prompt_matches` 与 `_squash` 已移到 `qi/bubble.py`，与 grok 共用同一份实现
# （2026-11-12 R3-6：两边原来逐字相同，改一边忘另一边就会出现"这端认领得到、
# 那端认不出"）。这里保留 `prompt_matches` 这个名字，调用方与测试不用改。


@dataclass
class Scan:
    blocks: list[Block] = field(default_factory=list)
    bar: int = 0                  # 页面上动作条按钮总数（只作诊断，不参与判定）

    def done(self, block: Block) -> bool:
        """这条回复自带回答动作条 = 它这一轮已经收尾。

        不用「页面上有动作条」当判据：接着聊的时候上一轮的条一直在，
        那样第一秒就会判完成，读回来的是半截话。也不只看第一个按钮：
        动作条的按钮顺序和数量会随界面变，整排里有「答得好」或「复制」就算。
        """
        return bool(DONE_BUTTONS.intersection(block.bars))

    def prompt_at(self, prompt: str) -> int | None:
        """我们这条提问的位置。取最后一个命中的块：折叠块和展开块会同时在对上。"""
        hits = [i for i, b in enumerate(self.blocks)
                if b.label in PROMPT_LABELS and prompt_matches(b.text, prompt)]
        return hits[-1] if hits else None

    def replies(self) -> list[Block]:
        return [b for b in self.blocks if b.label in REPLY_LABELS and b.text.strip()]

    def reply_after(self, at: int | None) -> Block | None:
        """我们这条提问之后的第一条回复——只认它，不去读别人的回答。"""
        if at is None:
            return None
        for i, b in enumerate(self.blocks):
            if i > at and b.label in REPLY_LABELS and b.text.strip():
                return b
        return None


def scan(root) -> Scan:
    return Scan(*parse(root))


def busy(root) -> bool:
    return any(n.kind == "ButtonControl" and n.name in STOP and _enabled(n)
               for n in root.walk())


def _composer_text(root) -> str | None:
    """读输入框里的真实文本；读不出来返回 None（交给调用方回退到旧判据）。"""
    for node in root.walk():
        if node.kind != "EditControl" or getattr(node, "offscreen", False):
            continue
        try:
            value = node.control.GetValuePattern().Value
        except (AttributeError, COMError, RuntimeError):
            return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        return "" if text in PLACEHOLDERS else text
    return None


def draft_present(root) -> bool:
    """输入框里有草稿。

    优先看**输入框真实文本**：挂了附件时发送键本来就可点（可以只发文件），
    拿"有没有发送键"当草稿证据会一直说"有草稿"——2026-11-12 实测：
    输入框只有占位符「问问 Gemini」、附件 chip 在，`draft_present` 仍返回 True，
    `gemini_use --continue` 因此拒发（draft_present），上传后的发送永远走不出去。

    读不出文本（离线桩/控件不可读）时回退到旧判据：有可点的「发送」就算有草稿。
    """
    text = _composer_text(root)
    if text is not None:
        return bool(text)
    return any(n.kind == "ButtonControl" and n.name in SEND and _enabled(n)
               for n in root.walk())


def _win(screen, site):
    return screen.window_rect(site.title_hint, on_secondary=site.on_secondary)


# ---------------------------------------------------------------- 只读档位复核
# 发送前"防漂移"用：只读芯片，不点菜单、不选任何一项。
# 为什么不用 gemini_choose.read_model：那个要**点开模型菜单**看勾，
# 虽然不会切档，但已经不是"只读"了——use 里多一次这样的点击，
# 就等于在发送前给页面多一次出错机会。
def read_tier_chip(screen) -> str | None:
    """只读当前模型芯片（模板优先、UIA 兜底，见 `qi/chip.py`）。

    读不到返回 None。**06b 起 None 不再等于"放行"**：`tier_guard` 会把它转成
    `tier_unreadable` 拒发（卡面第 1 条）。这里保留这个薄封装是给旧调用方用的。
    """
    try:
        got = chip.read(screen, "gemini", "model")
    except Exception:                       # noqa: BLE001 - 读失败不等于档位变了
        return None
    return got.get("value")



def new_chat_button(root):
    """页面上的「发起新对话」。按名称找，**不限角色**。

    实测（2026-11-01 现场树）：同一条侧栏项同时挂成 Hyperlink 和 Text，
    没有一个是 Button。只认 Button 就是 10:20/10:22 那两次 newchat_not_found。
    """
    return ui.prefer_clickables(ui.named(root, NEWCHAT_NAMES))


def _new_chat_landed(screen, site):
    """点完「发起新对话」之后，页面是不是真的回到了一个可写的新对话。

    B19：以前点完 `sleep(0.9)` 就直接返回 `ok:clicked`，根本不看落到哪。
    真机上出现过"侧栏都新建了会话、页面却停在 /app 空输入框"的死局。

    返回 True / False / None(=读不出来，**不判死**)。三个判据：
    地址里没有会话 id（回到 /app）、输入框干净、页面上没有上一轮的消息块。
    """
    try:
        control, url = ui.document(screen.target_hwnd, site.name)
        root = ui.tree(control)
    except (RuntimeError, COMError, UiDocsLost):
        return None
    if session_id_of(url) is not None:
        return False                       # 还挂在旧会话上
    if draft_present(root):
        return False                       # 输入框里还有上一轮的草稿
    # 只认真正的消息块：带「你说/Gemini 说」标签，或带动作条。
    # 侧栏的会话标题也是长文本叶子，`parse` 会把它们切成一堆**无标签**块，
    # 2026-11-12 实测因此把空的新会话误判成「仍有上一轮消息」（13 条全是标题）。
    if [b for b in scan(root).blocks if b.label or b.bars]:
        return False                       # 页面上还有上一轮的消息
    return True


def _drop_stale_draft(screen, site) -> None:
    """开新对话之前，清掉上一轮失败留在输入框里的草稿。

    为什么必须在这里清：`_new_chat_landed` 把「输入框里有草稿」当作
    「还停在旧会话」的证据，而我们自己上一次失败的发送（如 tier_unconfirmed
    发生在写入之后）就会把提问留在框里 —— 于是下一轮永远判 not_landed。
    2026-11-12 实测：框里就是上一轮的「只回复 OK」，两次点击后报
    `newchat_not_landed`，而地址其实早就是 `/app`。
    开新会话时旧草稿本来就作废，先清掉再点。清不掉就照原路走，不判死。
    """
    try:
        control, _url = ui.document(screen.target_hwnd, site.name)
        if not draft_present(ui.tree(control)):
            return
        screen.focus()
        conv = Conversation(screen, site.name)
        _clear_draft(conv, conv.read())
    except Exception:                     # noqa: BLE001 - 清不掉不影响主流程
        return


def open_new_chat(screen, site, *, tries: int = 8, pause: float = 0.5) -> dict:
    """点「发起新对话」：UIA 优先，读不到就等一拍再读，最后回落模板。

    为什么要重读：`ensure_site` 把窗口提到前台之后页面才开始渲染，
    第一次读到的树里侧栏可能还没有（10:20 新开窗口 4.4s 就报 not_found 就是这么来的）。

    B19：点完要校验落点（`_new_chat_landed`），没落地再点一次，两次都不行就
    返回 `newchat_not_landed`。**只在真机 Screen 上校验**：离线桩没有文档树，
    校验必然读不出来；用 `None` 表示"不可读"，按原行为直接返回，不判死。
    """
    last = None
    attempts = 2 if isinstance(screen, _ScreenClass) else 1
    if isinstance(screen, _ScreenClass):
        _drop_stale_draft(screen, site)   # 残草会被判成"还在旧会话"
    for _round in range(attempts):
        result = _open_new_chat_once(screen, site, tries=tries, pause=pause)
        if not result.get("ok"):
            return result
        if isinstance(screen, _ScreenClass):
            landed = _new_chat_landed(screen, site)
            result["verified"] = landed is True
            if landed is not False:
                return result
            last = result
            time.sleep(0.5)
            continue
        return result
    return {**(last or {}), "ok": False, "stage": "newchat_not_landed",
            "hint": "点了「发起新对话」，但页面仍在旧会话（地址带会话 id / 有草稿 / 有上一轮消息）"}


def _open_new_chat_once(screen, site, *, tries: int, pause: float) -> dict:
    control, _url = ui.document(screen.target_hwnd, site.name)
    for attempt in range(tries):
        nodes = new_chat_button(ui.tree(control))
        if nodes:
            ui.click(screen, nodes[0])
            time.sleep(0.9)
            return {"ok": True, "stage": "clicked", "basis": "uia",
                    "role": nodes[0].kind, "reads": attempt + 1}
        if attempt + 1 < tries:
            time.sleep(pause)

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
            "hint": f"控件树里没有「发起新对话」（读了 {tries} 次，约 {tries * pause:.1f}s）；"
                    f"模板 {site.newchat} 最高 {score}（{where}"
                    f"{f'；页面缩放 {zoom}' if zoom else ''}）"}


def _landing_action(*, now, landing_deadline, quiet_deadline, stuck, retried) -> str:
    """提交后没看到落点证据时，下一步该干什么。

    B21：把"要不要再点一次"和"再等一会儿"分开。判据只有一条——
    **输入框里还留着我们这段字，才说明端没接受这次提交**；框已空时
    可能只是渲染慢，此时再点是二次发送（会审指出的真 bug）。

    返回：
      "wait"   —— 还在窗口里，继续轮询
      "retry"  —— 框里有字且没重试过：清空重写、允许再点一次
      "extend" —— 框已空：开静默窗，只等不点
      "fail"   —— 该收工了（静默窗满，或重试后仍然堵着）
    """
    if quiet_deadline is not None:
        return "wait" if now < quiet_deadline else "fail"
    if now < landing_deadline:
        return "wait"
    if stuck:
        return "fail" if retried else "retry"
    return "extend"


def _submit(screen, site, conv, view) -> None:
    """点发送。先按控件名找真按钮，模板只作兜底（卡面要求）。"""
    if guard.user_moved_since(getattr(conv, "_input_mark", None)):
        raise UserTookOver("发送前检测到键鼠输入，已让位，未点发送")
    for attempt in range(4):
        buttons = [n for n in view.root.walk()
                   if n.kind == "ButtonControl" and n.name in SEND and _enabled(n)]
        if len(buttons) == 1:
            ui.click(screen, buttons[0])
            return
        if len(buttons) > 1:
            raise RuntimeError(f"可用发送按钮: 找到 {len(buttons)} 个控件")
        if attempt == 3:
            break
        time.sleep(0.5)                 # 文字刚打进去时按钮可能还没变可点
        view = conv.read()
    # 这里不传 scales：模板对 Gemini 现在的页面本来就匹不上（0.65 上下），
    # 多尺度只会多花几百毫秒。要的是把分数和所在屏幕打进报错里。
    match = screen.find(site.send, threshold=site.send_threshold, region=_win(screen, site))
    if match is None:
        best = getattr(screen, "last_best", None)
        score = "—" if best is None else f"{best:.3f}"
        where = screen.describe(_win(screen, site)) if hasattr(screen, "describe") else ""
        zoom = ui.page_zoom(getattr(screen, "target_hwnd", None))
        raise RuntimeError(
            f"发送键没找到：控件名里没有可用的「发送」，模板 {site.send} 最高 {score}"
            f"（阈值 {site.send_threshold}；{where}"
            f"{f'；页面缩放 {zoom}' if zoom else ''}）")
    screen.click(match)


class StagedUnsent(RuntimeError):
    """写入后读回不一致：**已落证据、按条件清稿，绝不点击发送**。

    CARD-v010-P0P1 之前这里是"读回不对就在同一个调用里再粘 2 次"，而每次重写前
    都会 `Ctrl+A` + `Delete`。写完之后页面若被切到别的会话，那两下删掉的是**别人的草稿**。
    现在把"清稿"从写入循环里彻底拿出来，交给 `clear_if_ours()` 的六条判据。
    """

    stage = "staged_unsent"
    delivery_state = "STAGED_UNSENT"

    def __init__(self, reason: str, hint: str = "", *, compare: dict | None = None,
                 draft_cleaned: bool = False, evidence_path: str | None = None,
                 checks: list | None = None):
        self.reason = str(reason or "text_mismatch")
        self.hint = str(hint or self.reason)
        self.compare = dict(compare or {})
        self.draft_cleaned = bool(draft_cleaned)
        self.evidence_path = evidence_path
        self.checks = list(checks or [])
        super().__init__(self.hint)

    def as_result(self) -> dict:
        out = {
            "ok": False,
            "stage": self.stage,
            "delivery_state": self.delivery_state,
            "attempt_consumed": False,
            "sent": False,
            "submission": "not_sent",
            "reason": self.reason,
            "draft_cleaned": self.draft_cleaned,
            "evidence_path": self.evidence_path,
            "link_target_unverifiable": bool(
                self.compare.get("link_target_unverifiable")),
            "hint": self.hint,
        }
        if self.compare:
            out["compare"] = {"ok": self.compare.get("ok"),
                              "reason": self.compare.get("reason"),
                              "stats": self.compare.get("stats")}
        if self.checks:
            out["checks"] = self.checks
        return out


def _runtime_id(view) -> list | None:
    """输入框控件的 RuntimeId。读不到返回 None（不猜）。"""
    try:
        rid = view.editor.control.GetRuntimeId()
        return list(rid) if rid else None
    except Exception:                        # noqa: BLE001 - 纯诊断，不许带崩主流程
        return None


def _overlap_ratio(a, b) -> float:
    """两个 box 的交集占较小者面积的比例。读不到 box 就给 0。"""
    try:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
    except (TypeError, ValueError):
        return 0.0
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    smaller = min(max(aw * ah, 1), max(bw * bh, 1))
    return ((right - left) * (bottom - top)) / smaller


def clear_if_ours(screen, conv, view, *, prompt: str, site: str,
                  stamp: dict | None = None, mark=None) -> dict:
    """只在**六条全过**时才按 Ctrl+A/Delete 清掉输入框。

    返回 {"cleared": bool, "reason": str|None, "checks": [{"name","ok","reason"}]}。
    reason 枚举：readback_failed / page_changed / session_drift /
    composer_replaced / not_ours / user_active / clear_unverified。
    """
    checks: list = []

    def add(name: str, ok: bool, reason: str = "", **detail) -> bool:
        item = {"name": name, "ok": bool(ok), "reason": reason or None}
        if detail:
            item["detail"] = detail
        checks.append(item)
        return bool(ok)

    def stop(reason: str) -> dict:
        return {"cleared": False, "reason": reason, "checks": checks}

    try:
        fresh = conv.read()
    except (RuntimeError, COMError) as exc:
        add("readback", False, "readback_failed", error=str(exc))
        return stop("readback_failed")

    raw = getattr(fresh, "value", "") or ""
    value = raw.strip()
    if not value or value in PLACEHOLDERS:
        add("draft_present", True, "already_empty")
        return {"cleared": True, "reason": "already_empty", "checks": checks}

    # 1) 页面还是写入时那一页
    if stamp is not None and isinstance(screen, _ScreenClass):
        verdict = sendgate.page_confirm(screen, site, stamp)
        if not add("page_stable", verdict.get("ok"),
                   "" if verdict.get("ok") else "page_changed",
                   changed=verdict.get("changed")):
            return stop("page_changed")
    else:
        add("page_stable", True, "unchecked")

    # 2) 会话锚点没有漂移（都没有 id 时比 path）
    try:
        now_sid = session_id_of(getattr(fresh, "url", None))
        was_sid = session_id_of(getattr(view, "url", None))
        same = (now_sid == was_sid) if (now_sid or was_sid) else (
            path_of(getattr(fresh, "url", "")) == path_of(getattr(view, "url", "")))
    except Exception:                        # noqa: BLE001
        same = False
    if not add("session_anchor", same, "" if same else "session_drift",
               send_sid=locals().get("was_sid"), now_sid=locals().get("now_sid")):
        return stop("session_drift")

    # 3) 还是同一个输入框（RuntimeId 优先，读不到退化为矩形重合）
    rid_now, rid_was = _runtime_id(fresh), _runtime_id(view)
    if rid_now and rid_was:
        same_editor = rid_now == rid_was
    else:
        same_editor = _overlap_ratio(getattr(getattr(fresh, "editor", None), "box", None),
                                     getattr(getattr(view, "editor", None), "box", None)) >= 0.8
    if not add("composer_same", same_editor,
               "" if same_editor else "composer_replaced"):
        return stop("composer_replaced")

    # 4) 框里那串字是我们的（等于 prompt，或是它的前缀）
    ours = textcmp.equal(prompt, raw) or _looks_like_partial_write(prompt, raw)
    if not add("draft_ours", ours, "" if ours else "not_ours",
               chars=len(raw), prompt_chars=len(str(prompt or ""))):
        return stop("not_ours")

    # 5) 期间没有真人动键鼠
    moved = guard.user_moved_since(mark)
    if not add("user_idle", not moved, "user_active" if moved else ""):
        return stop("user_active")

    # 6) 清完必须读回为空
    try:
        cleared = _clear_draft(conv, fresh)
    except Exception:                        # noqa: BLE001 - 清不掉只能记 False，不许带崩主流程
        cleared = False
    add("clear_verified", cleared, "" if cleared else "clear_unverified")
    return {"cleared": cleared,
            "reason": None if cleared else "clear_unverified",
            "checks": checks}


def write_prompt(screen, conv, view, prompt: str, *, tries: int = 1,
                 stamp: dict | None = None, mark=None, site: str = "gemini",
                 evidence_root=None):
    """把提问写进输入框：**单次写入 + 一次延迟重读**，不一致就停手。

    与旧实现的区别（CARD-v010-P0P1 第 4 条）：

      · 旧：读回不一致 → Ctrl+A/Delete → 重写，最多 3 轮。删的时候不校验页面身份。
      · 新：只写一次；不一致 → 落证据 → `clear_if_ours()` 六条全过才清 →
            抛 `StagedUnsent`，**绝不点击发送**。

    成功时仍返回读回的 view 对象（`tests/test_cardweb2.py` 与四处 mock 依赖这个契约）。
    `tries` 仅为向后兼容保留，传入值不再改变行为。
    """
    del tries
    conv.write(view, prompt)
    time.sleep(WRITE_SETTLE)
    written = conv.read()
    verdict = textcmp.compare(prompt, written.value)
    if verdict["ok"]:
        return written

    clear = clear_if_ours(screen, conv, written, prompt=prompt, site=site,
                          stamp=stamp, mark=mark)
    path = evidence.write_evidence(
        site or "gemini",
        {"delivery_state": "STAGED_UNSENT", "attempt_consumed": False,
         "compare": {"ok": verdict.get("ok"), "reason": verdict.get("reason"),
                     "stats": verdict.get("stats"), "diff": verdict.get("diff")},
         "clear": {"cleared": clear.get("cleared"), "reason": clear.get("reason"),
                   "checks": clear.get("checks")},
         "session": {"url_before": getattr(view, "url", None),
                     "url_after": getattr(written, "url", None)}},
        stage="input_mismatch", prompt=prompt, root=evidence_root,
    )
    raise StagedUnsent(verdict.get("reason") or "text_mismatch",
                       verdict.get("hint") or textcmp.describe(prompt, written.value),
                       compare=verdict, draft_cleaned=bool(clear.get("cleared")),
                       evidence_path=path, checks=clear.get("checks"))


def accept_evidence(*, send_sid, prompt_found: bool, replies: int, baseline: int,
                    busy: bool, url, mode: str = "new",
                    draft_present: bool = False) -> tuple[bool, str | None]:
    """点过发送之后，这一帧是否构成"端确实收下了"的硬证据。

    CARD-v010-P0P1 第 3 条：旧逻辑是
        `elif session_id_of(view.url) is not None: accepted = True`
    —— 只要地址里有会话 id 就当真。`--continue` 时那个 id 是**上一轮**的，
    于是一个字都没发出去也会被判 accepted 并把账本写成 accepted（永久禁发）。

    现在只认四种证据，会话 id 这条路要求"发车前没有 id、发车后有 id"：
      · prompt     —— 控件树里出现了本次提问；
      · new_reply —— 回复条数比发车前多；
      · busy       —— 页面正在生成；
      · session    —— 仅 mode=new 且 send_sid is None 且已有 id 且输入框已清空。
    """
    if prompt_found:
        return True, "prompt"
    if replies > baseline:
        return True, "new_reply"
    if busy:
        return True, "busy"
    if mode == "new" and send_sid is None and not draft_present:
        if session_id_of(url) is not None:
            return True, "session"
    return False, None



def _copy_fallback(conv, root, reply: Block) -> str | None:
    """回复被折叠截断时的退路：点这条回复的「复制」，拿剪贴板里的全文。"""
    for node in reversed([n for n in root.walk() if n.kind == "ButtonControl"
                          and ui.canonical(n.name) in COPY_BUTTONS]):
        try:
            text = conv.copy(node)
        except (RuntimeError, COMError):
            continue
        if text and ui.canonical(text) != reply.key:
            return text
    return None


def _page_text(view) -> str:
    """页面全文（DocumentControl 的 TextPattern）。读不到返回空串。

    **为什么需要它**：控件树偶尔一块都切不出来（气泡结构变了、渲染半截），
    这时按 DOM 全文取一次，比直接报 no_reply 强。只读，不点任何控件。
    """
    try:
        pattern = view.root.control.GetPattern(auto.PatternId.TextPattern)
        if pattern is not None:
            return pattern.DocumentRange.GetText(-1) or ""
    except (COMError, AttributeError):
        return ""
    return ""


def _text_after_prompt(text: str, prompt: str) -> str:
    """页面全文里，**最后一条**我们这条提问之后的那段文字。

    切不出来块时的兜底，所以只求"像回复"，别的不保证：
      · 认领提问走前缀（折叠气泡尾部会被截断），取最后一处命中；
      · 只剔**整行**等于按钮名/占位文字的尾部噪声；
      · 中间不做任何改写——原样交出去，是不是真的回复由调用方看 `read_via=dom` 判断。
    """
    lines = [line.strip() for line in text.splitlines()]
    want = ui.canonical(prompt)
    if not want:
        return ""
    at = None
    for index, line in enumerate(lines):
        got = ui.canonical(line)
        if not got:
            continue
        for mark in TAIL_MARKS:           # 折叠气泡：尾部省略号不算正文
            if got.endswith(mark):
                got = got[:-len(mark)]
                break
        if got == want or (len(got) >= min(MIN_PREFIX, len(want)) and want.startswith(got)):
            at = index
    if at is None:
        return ""
    noise = {ui.canonical(item) for item in DOM_NOISE}
    labels = {ui.canonical(item) for item in PROMPT_LABELS + REPLY_LABELS}
    tail = [line for line in lines[at + 1:]
            if line and ui.canonical(line) not in noise]
    # 提问后面紧跟的那一行常常是「Gemini 说」这种标签，它不是回复正文
    while tail and ui.canonical(tail[0]) in labels:
        tail.pop(0)
    while tail and ui.canonical(tail[-1]) in noise:
        tail.pop()
    return "\n".join(tail).strip()


def _pick_reply(page: Scan, at: int | None, baseline: int) -> Block | None:
    """本轮该读哪条回复（实现见 `qi/bubble.pick_reply`，与 grok 共用）。"""
    return _pick(page, at, baseline)


def _already_answered(root, prompt: str):
    """发送前检查：对话里是否已有本提问之后的回复。有则返回回复正文，否则 None。

    CARD-09f 第 6 条：中断后重跑、页面其实已答完时，绝不重发，只读已有回复。

    R9：**它只回答"页面上有没有"**。调用方必须先证明这是本事务、同一会话
    （见 `_prior_answer_proof`），证明不了就不能拿这串文字当本轮答案。
    """
    return _already(scan(root), prompt)


def _prior_answer_proof(site_name: str, prompt: str, view):
    """发送前认领旧回答前，先证明"本事务 + 同一会话"。

    返回 `(proved, reason, prior_state)`：
      · `no_journal`            没有本事务的提交记录（没发过 / 记录已清）；
      · `session_id_missing`    记录是旧的，没有与标题无关的会话 id；
      · `session_id_unreadable` 当前页面读不到会话 id（oc://、后台标签）；
      · `session_id_mismatch`   两者不相等（同文档另一个会话）。
    证明不了 -> 调用方 fail-closed：不认领、不发送、不清草稿。
    """
    from qi import submissions

    try:
        prior = submissions.pending(site_name, prompt)
    except Exception:                       # noqa: BLE001 - 读不出来按"状态不明"
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


def _clear_draft(conv, view) -> bool:
    """清空输入框并读回为空（CARD-09f 第 3/6 条）。读回为空返回 True，否则 False。

    第 6 条：一次 submission=accepted 之后，若提问被回填进输入框，要清掉并读回为空；
    第 3 条 concurrent_ui 清半截草稿也走这里。失败一律 False，不冒充"已清"。
    """
    try:
        conv.clear(view)
        recheck = conv.read()
        raw = getattr(recheck, "value", None)
        if not isinstance(raw, str):
            return False
        # B20：输入框空的时候，UIA 读回来的是占位文字（"问问 Gemini\n"），
        # 不是空串。以前这里直接和 "" 比，于是"本来就空"被判成"没清掉"，
        # 每轮 accepted 之后都会 abort 成 interrupted/draft_clear_failed
        # （B20 汇总轮 turn2 真机撞到：消息已经发出去了，却被判失败）。
        value = raw.strip()
        if value in PLACEHOLDERS:
            value = ""
        return textcmp.equal(value, "") and not draft_present(recheck.root)
    except (RuntimeError, COMError):
        return False


def _final_read(conv, prompt: str, baseline: int) -> dict | None:
    """超时前最后再看一眼页面。

    **「没认领到」不等于「没有回复」。** 只要页面不是还在生成、且确实多了一条
    本轮的新回复，就按成功返回，不再报 no_reply（卡面第 2 项）。
    仍然守住两条：还在生成（有可点的停止键）就不交卷——半截话不能当答案；
    回复条数没有增加也不交卷——那是上一轮的答案，不是这轮的。
    """
    try:
        view = conv.read(attempts=3, delay=0.4)
    except (RuntimeError, COMError):
        return None
    if view.busy:
        return None
    page = scan(view.root)
    at = page.prompt_at(prompt)
    if len(page.replies()) <= baseline:
        # CARD-07 第 2 条兜底：一条块都切不出来时，按页面全文再试一次。
        # 认领不到提问就不试——没有归属的整段文字不能当回答。
        if at is None:
            return None
        text = _text_after_prompt(_page_text(view), prompt)
        if not text:
            return None
        return {"ok": True, "stage": "answered", "reply": text,
                "submission": "accepted", "sent": True,
                "basis": "dom_text_after_prompt", "read_via": "dom",
                "criteria": "final_read+dom_text"}
    reply = _pick_reply(page, at, baseline)
    if reply is None or not reply.text.strip():
        return None
    result = {"ok": True, "stage": "answered", "reply": reply.text.strip(),
              "submission": "accepted", "sent": True, "basis": "final_read",
              "criteria": "final_read+text_stable"}
    if at is None:
        result["attribution"] = "new_reply"
    if not page.done(reply):
        result["hint"] = "最后一次读取时回复动作条还没出现，按「页面已停止生成 + 有新回复」判定"
    return result


def _send_and_wait(screen, site, prompt: str, timeout: float,
                   purpose: str | None = None, mode: str = "new") -> dict:
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
        # R9：旧回答可能来自同一文档的另一个会话（同文同答）。先证明本事务 +
        # 同一会话 id；证明不了就 fail-closed，不认领、不发送、不清草稿。
        proved, reason, prior_state = _prior_answer_proof(site.name, prompt, before)
        if not proved:
            submission = ("accepted" if prior_state in ("accepted", "answered")
                          else "unknown" if prior_state else "not_sent")
            return {**state, "stage": "session_unproven", "reason": reason,
                    "session": "unproven", "sent": False,
                    "submission": submission, "prior_submission": prior_state,
                    "hint": ("页面里已有同一条提问和回复，但证明不了是同一会话"
                             f"（{reason}）；按 fail-closed 不采信、不发送、不清草稿")}
        cleared = None
        if draft_present(before.root):
            cleared = _clear_draft(conv, before)
        return {**state, "ok": True, "stage": "already_answered", "reply": already,
                "submission": None, "sent": False, "basis": "already_answered",
                "cleared_draft": cleared}
    if purpose is not None:
        previous = submissions.pending("gemini", prompt)
        if previous:
            return submissions.refusal(previous)
    if draft_present(before.root):
        return {**state, "stage": "draft_present", "hint": "当前输入框已有草稿"}
    baseline = len(scan(before.root).replies())

    # CARD-v010-P1B-P2：守卫必须在**写稿之前**。
    # P0P1 把它放在 `write_prompt` 之后，一旦拒绝，输入框里已经留着我们的正文却直接
    # return —— 既没清稿也没落账，用户看到的是"没发但框里有字"。
    # 放在 uilock.section 之外：它只读，不需要持桌面锁。
    # `require=()` 是有意的：写前没有读回，`prompt_match` 无意义；`journal_free` 会与
    # 上面 `submissions.pending` 重复、并且改变 purpose=None 的既有行为。
    blocked = precheck.run(endpoint="gemini", site=site.name, prompt=prompt,
                           payload_sha256=precheck.digest(prompt))
    if blocked is not None:
        return {**state, **blocked}

    # 第 3 条：写入之前记页面指纹，写入之后、发送之前再比一次。
    # 只在真机（screen 是真 Screen 实例）时做——离线假件的 screen 是 StubScreen，跳过。
    # B13b：写入 → 读回 → 指纹 → 门禁 → 落账 → 发送，**一整段**（中途放锁必被抢前台）
    with uilock.section('send:gemini', timeout=uilock.SECTION_TIMEOUT):
        stamp_before_write = (sendgate.page_stamp(screen, site.name)
                              if isinstance(screen, _ScreenClass) else None)

        send_sid = session_id_of(getattr(before, "url", None))
        try:
            written = write_prompt(screen, conv, before, prompt,
                                   stamp=stamp_before_write,
                                   mark=getattr(conv, "_input_mark", None),
                                   site=site.name)
        except StagedUnsent as exc:
            # 读回不一致：已按条件清稿并落证据，**绝不点击**（CARD-v010-P0P1 第 4 条）
            return {**state, **exc.as_result()}
        state.update(submission="unknown", sent=None)
        # 第 3 条：发送阶段复核页面指纹，被换走 → 清掉半截文本 + concurrent_ui
        if stamp_before_write is not None:
            concurrent = sendgate.page_confirm(screen, site.name, stamp_before_write)
            if not concurrent["ok"]:
                cleared = _clear_draft(conv, written)
                return {**state, "stage": "concurrent_ui", "reason": "page_changed",
                        "changed": concurrent["changed"], "cleared_draft": cleared,
                        "submission": "not_sent", "sent": False}
        # 第 3 条：复核放在**写入之后、点击之前**；复核完再比一次页面指纹
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
        # CARD-v010-P1B-P2：守卫已前移到写稿之前（见 baseline 那一行下方）。
        try:
            if purpose is not None:
                from qi import submissions

                try:
                    submissions.reserve("gemini", prompt,
                                        scope=submissions.scope_from_view(
                                            written, site=site.name,
                                            hwnd=getattr(screen, "target_hwnd", None)))
                except submissions.TierUnconfirmed as exc:
                    return {**exc.as_result(), "submission": "not_sent", "sent": False}
                except OSError:
                    return {**state, "stage": "interrupted", "sent": None,
                            "submission": "unknown",
                            "reason": "submission_state_unwritable",
                            "hint": "无法持久化提交状态，已拒发"}
            try:
                _submit(screen, site, conv, written)
            except UserTookOver as exc:
                # `_submit` 明写"未点发送"：这一次**不算消耗**，退回可重试态。
                # 旧代码直接 return，submitting 永久留在盘上，同一提问再也发不出去。
                if purpose is not None:
                    submissions.release("gemini", prompt, reason="user_took_over")
                return {**state, "submission": "not_sent", "sent": False,
                        "stage": "user_took_over", "hint": str(exc)}
            except (RuntimeError, COMError):
                # 发送键一个都不可用 / 找到多个按钮：一个键都没点，同样是"没发出"
                if purpose is not None:
                    submissions.release("gemini", prompt,
                                        reason="send_button_unavailable")
                raise
            if purpose is not None:
                # 从这里开始状态是"可能已发"，只允许进 unknown/accepted，不许再 release
                submissions.mark_clicked("gemini", prompt)
            # 点过之后才核销票据：校验通过但被抢走的那一次不该烧掉用户的授权。
            # 必须带正文哈希——绑定了 payload_sha256 的票据，不传就永远核销失败。
            precheck.consume_grant_token(target="gemini",
                                         payload_sha256=precheck.digest(prompt))
        except (RuntimeError, COMError):
            raise

    deadline = time.monotonic() + timeout
    accepted = False
    attribution = None
    at = None
    stable_text, stable_since = None, None
    stable_reads = 0
    last_busy = False
    read_failures = 0
    # B19：提交后的落点窗口。提交那一刻起算，未 accepted 前每轮检查。
    landing_deadline = time.monotonic() + SUBMIT_LANDING_WAIT
    submit_retried = False
    #: B21：框已空时开的静默窗（只等不点）。None = 还没开过。
    quiet_deadline = None
    while time.monotonic() < deadline:
        try:
            view = conv.read(attempts=2, delay=0.4)
            heartbeat.beat_view("waiting", view)
        except (RuntimeError, COMError) as exc:
            # 已经发出去了，读不到只说明页面在重排/换路由（Gemini 答完才写会话 id）
            read_failures += 1
            if read_failures >= 12:
                if purpose is not None:
                    from qi import submissions

                    # 已经点过发送又连续读不到页面：状态不明，落 unknown（不重发）
                    submissions.finish("gemini", prompt,
                                       {**state, "stage": "interrupted",
                                        "submission": "unknown", "sent": None})
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
            # CARD-v010-P0P1 第 3 条：旧逻辑"地址里有会话 id 就算发成功"在
            # `--continue` 下会把上一轮的 id 当成本轮证据。判定收进纯函数，
            # 会话 id 只在"发车前没有、发车后有、且输入框已清空"时才算。
            accepted, attribution = accept_evidence(
                send_sid=send_sid, prompt_found=(at is not None),
                replies=len(page.replies()), baseline=baseline,
                busy=bool(getattr(view, "busy", False)), url=getattr(view, "url", None),
                mode=mode,
                draft_present=draft_present(view.root))
            if accepted:
                state.update(submission="accepted", sent=True)
            else:
                # B21：没有落点证据 → 先判"框里还有没有我们这段字"，再决定等还是重点。
                action = _landing_action(
                    now=time.monotonic(), landing_deadline=landing_deadline,
                    quiet_deadline=quiet_deadline, stuck=draft_present(view.root),
                    retried=submit_retried)
                if action == "retry":
                    submit_retried = True
                    state["submit_retried"] = True
                    cleared = _clear_draft(conv, view)
                    state["cleared_draft"] = cleared
                    try:
                        again = write_prompt(screen, conv, conv.read(), prompt,
                                             mark=getattr(conv, "_input_mark", None),
                                             site=site.name)
                    except StagedUnsent as exc:
                        # 先接住它：StagedUnsent 是 RuntimeError 子类，
                        # 放在下面的 except 里会被当成"重写失败"吞掉。
                        return {**state, **exc.as_result(),
                                "reason": f"retry_{exc.reason}"}
                    except (RuntimeError, COMError, UserTookOver) as exc:
                        return {**state, "stage": "submit_not_landed",
                                "reason": "retry_failed", "detail": str(exc),
                                "submission": "unknown", "sent": None,
                                "hint": "首轮无落点，重写阶段失败（未再点击）"}
                    _submit(screen, site, conv, again)
                    landing_deadline = time.monotonic() + SUBMIT_LANDING_WAIT
                    time.sleep(0.4)
                    continue
                if action == "extend":
                    quiet_deadline = time.monotonic() + SUBMIT_QUIET_WAIT
                    state["landing_extended"] = True
                    time.sleep(0.4)
                    continue
                if action == "fail":
                    stuck = draft_present(view.root)
                    if purpose is not None:
                        from qi import submissions

                        if stuck:
                            # 文字仍完整堵在输入框里 = 端根本没接受这次提交。
                            # force 是"点了但确定没发出去"的正证据专用开关。
                            submissions.release("gemini", prompt,
                                                reason="draft_stuck", force=True)
                        else:
                            # 框已空却没有落点证据：状态不明，落账 unknown（禁止自动重发）
                            submissions.finish("gemini", prompt,
                                               {**state, "stage": "submit_not_landed",
                                                "submission": "unknown", "sent": None})
                    return {**state, "stage": "submit_not_landed",
                            "reason": "draft_stuck" if stuck else "composer_empty",
                            "cleared_draft": state.get("cleared_draft"),
                            "submission": "not_sent" if stuck else "unknown",
                            "sent": False if stuck else None,
                            "retried": submit_retried,
                            "hint": ("点了发送，但限定时间内既没有提问块、也没有新回复、"
                                     "没有生成迹象、地址里也没有会话 id；"
                                     + ("文字还堵在输入框里" if stuck
                                        else "输入框已空：可能发出去了但没落地，按 fail-closed 不重发"))}

        if accepted:
            if purpose is not None:
                from qi import submissions

                # R5：第一次判定 accepted 的那一页，才代表"回答长在哪个会话里"。
                # 只写第一次（新会话发送后地址才带 id），后面不覆盖。
                submissions.finish("gemini", prompt, state,
                                   scope=submissions.scope_from_view(
                                       view, site=site.name,
                                       hwnd=getattr(screen, "target_hwnd", None)))
            if draft_present(view.root):
                state["cleared_draft"] = _clear_draft(conv, view)
                if not state["cleared_draft"]:
                    return {**state, "stage": "interrupted", "reason": "draft_clear_failed"}
                continue
        reply = _pick_reply(page, at, baseline)
        if reply is None or len(page.replies()) <= baseline:
            # CARD-07 第 2 条兜底：树里一块都切不出来时，读页面全文（TextPattern），
            # 取我们这条提问之后的那段。只在这条路上用，正常路径仍然以块为准。
            dom = "" if view.busy else _text_after_prompt(_page_text(view), prompt)
            if dom:
                if dom != stable_text:
                    stable_text, stable_reads = dom, 1
                    time.sleep(0.4)
                    continue
                stable_reads += 1
                if stable_reads < STABLE_READS:
                    time.sleep(0.4)
                    continue
                if len(page.replies()) > baseline:
                    # 树里其实有块，只是这一版没认领到：别用 DOM 的整段文字顶掉它
                    stable_text, stable_reads = None, 0
                    time.sleep(0.4)
                    continue
                result = {**state, "ok": True, "stage": "answered", "reply": dom,
                          "basis": "dom_text_after_prompt", "read_via": "dom",
                          "criteria": "stop_gone+dom_text_stable_2_reads"}
                if attribution == "new_reply":
                    result["attribution"] = "new_reply"
                if at is None:
                    result["attribution"] = "new_reply"
                return result
            stable_text, stable_reads = None, 0
            time.sleep(0.4)
            continue
        if view.busy:
            stable_text, stable_reads = None, 0
            time.sleep(0.4)
            continue
        bar = page.done(reply)
        if bar:
            # 原判据（动作条出现）：文本连续 SETTLE 秒没变 = 答完了
            if reply.key != stable_text:      # 比规范形：换行/空格抖动不算变化
                stable_text, stable_since = reply.key, time.monotonic()
                stable_reads = 1
                time.sleep(0.4)
                continue
            if stable_since is None:
                # 上一轮走的是"没有动作条"那条判据（只记了 stable_reads）：
                # 这里补一个起点，别拿 None 去算时间差。
                stable_since = time.monotonic()
                time.sleep(0.4)
                continue
            if time.monotonic() - stable_since < SETTLE:
                time.sleep(0.4)
                continue
        else:
            # CARD-07 第 2 条：动作条没出现（配额提示、换皮气泡…）时，
            # 「停止键消失 + 文本连续 2 次不变」就算完成——不再干等 3 秒或等到超时。
            if reply.key != stable_text:
                stable_text, stable_reads = reply.key, 1
                time.sleep(0.4)
                continue
            stable_reads += 1
            if stable_reads < STABLE_READS:
                time.sleep(0.4)
                continue
        answer = reply.text.strip()
        truncated = answer.endswith(TAIL_MARKS)
        if truncated:
            full = _copy_fallback(conv, view.root, reply)
            if full:
                answer, truncated = full, False
        result = {**state, "ok": True, "stage": "answered", "reply": answer,
                  "basis": ("tree_reply_bar_and_stable_text" if bar
                            else "stop_gone_and_stable_text"),
                  "criteria": ("reply_bar+text_stable_3s" if bar
                               else "stop_gone+text_stable_2_reads")}
        notes = []
        if attribution == "new_reply":
            result["attribution"] = "new_reply"
            notes.append("提问没在控件树里认领到，按“比发送前多出来的最新回复”归属")
        if truncated:
            result["truncated"] = True
            notes.append("回复在控件树里以省略号截断，复制键也没读到全文")
        if notes:
            result["hint"] = "；".join(notes)
        return result

    # 卡面第 2 项：超时前最后再读一次，读到回复就当成功返回
    final = _final_read(conv, prompt, baseline)
    if final is not None:
        return final
    kind = "still_generating" if state["sent"] and (last_busy or stable_text) else "no_reply"
    if purpose is not None:
        from qi import submissions

        # 超时：点过发送、但没有落点证据 → 账本必须落 unknown（阻断自动重发）。
        # 旧代码只 return，记录停在 submitting，没有 subtype，审计看不出"超时"。
        if state.get("submission") == "unknown" or state.get("sent") is None:
            submissions.finish("gemini", prompt,
                               {**state, "stage": "timeout", "submission": "unknown",
                                "sent": None})
    return {**state, "stage": "timeout", "timeout_kind": kind}


def _prior_submission(site, prompt: str) -> dict | None:
    """只读地看一眼持久化提交记录：这个端 + 这条提问以前提交过吗？

    CARD-09f 返工：这里**只读**，不 reserve/finish，不写任何文件。
    记录读坏了按 `unknown` 处理（宁可不发，也不把"上次可能已经发出去了"说成没发）。
    """
    from qi import submissions

    try:
        return submissions.pending(site.name, prompt)
    except Exception:                       # noqa: BLE001 - 读记录失败 = 状态不明
        return {"state": "unknown", "error": "submission journal unreadable"}


def _journal_submission(prior) -> str:
    state = str((prior or {}).get("state") or "unknown").lower()
    return state if state in ("accepted", "answered", "unknown") else "unknown"


def _prior_turn_recovery(site, prompt: str, prior, *, tab, verbose, ctx: dict):
    """准备动作**之前**的只读恢复：不点新对话、不写、不发。

    先续聊选中已有标签（`continue_session=True` 绝不打开首页），再只读扫一遍控件树，
    看这条提问在本页是不是已经有回复。有就按 already_answered 交回；
    没有就返回 None，让调用方按"已提交/状态不明"拒发（不重发）。
    读不到文档（UiDocsLost）**不在这里吞**：由调用方保留 accepted/unknown 事实。

    2026-11-07（R5 / CE2）：光"本页有同样的提问 + 一条旧回答"是**文本证据**，
    不是归属证据——另一个标签里恰好有一对同样的问答，就会被当成本轮结果。
    所以先比发送时记下的标签/文档身份（`submissions.reconcile`）：
    缺身份、读不到、对不上都返回 None（调用方按已提交拒发），绝不采信当前页。
    """
    from qi import submissions

    screen = Screen(verbose=verbose)
    hwnd = ensure_site(site.name, tab=tab, continue_session=True)
    ctx.update({"screen": screen, "hwnd": hwnd})
    if hwnd is None or not screen.bind(hwnd):
        return None
    screen.tab_site = site.name
    view = Conversation(screen, site.name).read(attempts=2, delay=0.4)
    current = submissions.scope_from_view(view, site=site.name, hwnd=hwnd)
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
        verbose: bool = True, tab: int | None = None, prepare=None,
        purpose: str | None = None, reread=None) -> dict:
    """发一条提问并等回复。prepare(s) 与新对话之后、写输入之前要做的准备（切模式）。

    准备阶段（开窗口/前台/新对话/切模式）的异常**不在这里吞**：和另外四端一样
    交给 cli 报 preparation_failed。只有"发出去之后"的异常按 interrupted 记。

    `purpose` 给了就在**真正发送之前**过档位闸（CARD-06a (a)）。
    """
    started = time.monotonic()
    result: dict = {"ok": False, "submission": "not_sent", "sent": False}
    screen = None
    hwnd = None
    # CARD-09f 返工（第 18/27 行）：**准备动作之前**先只读查一遍持久化提交记录。
    # 以前这一步在 `_send_and_wait` 里，而 `_send_and_wait` 在 `open_new_chat` 之后——
    # mode=new 会先把会话换掉，新会话/读文档一失败就把"仓库里明明 accepted"报成
    # not_sent，调用方会以为没发过而重发。
    prior = _prior_submission(site, prompt)
    if prior is not None:
        state = _journal_submission(prior)
        ctx: dict = {}
        recovered = None
        try:
            recovered = _prior_turn_recovery(site, prompt, prior, tab=tab,
                                             verbose=verbose, ctx=ctx)
        except (UiDocsLost, WindowMinimized) as exc:
            # 恢复自己也读不到：**保留**已知的 accepted/unknown，只说清恢复失败
            result = {**exc.as_result(), "submission": state, "sent": False,
                      "prior_submission": state,
                      "hint": (f"{exc}；已有该提问的提交记录（{state}），"
                               "按 CARD-09f 保持该状态，不重发")}
        except (ForegroundLost, RuntimeError, COMError) as exc:
            result = {**result, "stage": "interrupted",
                      "submission": state, "prior_submission": state,
                      "hint": (f"只读恢复失败：{exc}；已有该提问的提交记录（{state}），"
                               "保持该状态，不重发")}
        except Exception as exc:             # noqa: BLE001 - 恢复失败也必须保留既有事实
            result = {**result, "stage": "interrupted",
                      "submission": state, "prior_submission": state,
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
                # R5：说清是"身份没对上"而不是"页面没答案"——这是两件事。
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
                # 页面缩放预检（第 6 条）：非 100% 先 Ctrl+0，仍不是就退出
                zoom_mod.ensure(screen, verbose=verbose)
                # 本端自己的「发起新对话」：UIA 优先 + 重读 + 模板兜底（见 open_new_chat）
                opened = open_new_chat(screen, site) if mode == "new" else {"ok": True}
                prepared = prepare(screen) if (opened["ok"] and prepare is not None) else {"ok": True}
                if not opened["ok"]:
                    result = {**opened, "submission": "not_sent", "sent": False}
                elif not prepared["ok"]:
                    result = {"ok": False, "stage": "prepare_failed", "prepare": prepared,
                              "submission": "not_sent", "sent": False}
                else:
                    result = _send_and_wait(screen, site, prompt, timeout,
                                            purpose=purpose, mode=mode)
            except zoom_mod.ZoomNot100 as exc:
                result = {**exc.as_result(), "submission": "not_sent", "sent": False}
            except ForegroundLost as exc:
                result = {**result, "stage": "user_took_over", "hint": str(exc)}
            except UiDocsLost as exc:
                result = {**exc.as_result(), "submission": "not_sent", "sent": False}
            except WindowMinimized as exc:
                result = {**result, "stage": "window_minimized", "hint": str(exc)}
            except (RuntimeError, COMError) as exc:
                result = {**result, "stage": "interrupted", "hint": str(exc)}
    result["elapsed"] = round(time.monotonic() - started, 1)
    result["mode"] = mode
    if not result.get("ok"):
        failure.record(site.name, result, screen=screen, hwnd=hwnd)
    return result


def read_last(*, tab: int | None = None, verbose: bool = True) -> dict:
    """第 3 项：只读当前 Gemini 标签页最后一条回复，不发送、不点任何按钮。"""
    result: dict = {"ok": False, "submission": "not_sent", "sent": False}
    screen = Screen(verbose=verbose)
    try:
        hwnd = ensure_site("gemini", tab=tab)
        if hwnd is None:
            return {**result, "stage": "no_window"}
        if not (screen.bind(hwnd) and screen.focus()):
            return {**result, "stage": "page_not_front"}
        screen.tab_site = "gemini"
        view = Conversation(screen, "gemini").read()
        found = scan(view.root).replies()
        if not found:
            return {**result, "stage": "no_reply", "url": view.url, "replies": 0}
        return {"ok": True, "stage": "read", "submission": None, "sent": None,
                "reply": found[-1].text.strip(), "replies": len(found), "url": view.url}
    except ForegroundLost as exc:
        return {**result, "stage": "user_took_over", "hint": str(exc)}
    except (RuntimeError, COMError) as exc:
        return {**result, "stage": "interrupted", "hint": str(exc)}


def _log_criteria(endpoint: str, result: dict) -> None:
    """把这一轮的**完成判据**单独记一行（CARD-07 第 2 条）。

    `runlog.record` 只收固定字段（端/结果/耗时/错误），判据塞不进去；而
    `qi/runlog.py` 不在本卡允许改的范围里。所以这里按同一份格式、同一目录补一行，
    `result` 固定写 `reply_criteria`，和收发结果分开——读日志的脚本不用分叉，
    看日志的人一眼能知道"这轮是按停止键消失判的，还是按动作条判的"。

    只在真有判据时写（超时/没发出去的结果没有判据，不写空行）。
    写失败一律吞掉：判据是给人看的，不许影响主流程。
    """
    criteria = result.get("criteria")
    if not criteria:
        return
    try:
        from . import runlog
        folder = runlog.RUNS
        folder.mkdir(parents=True, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": endpoint,
                "ok": bool(result.get("ok")), "result": "reply_criteria",
                "timeout_kind": None, "submission": result.get("submission"),
                "elapsed": result.get("elapsed"),
                "error": None if result.get("ok") else (result.get("hint")
                                                        or f"stage={result.get('stage')}"),
                "criteria": criteria, "basis": result.get("basis"),
                "read_via": result.get("read_via")}
        with open(folder / time.strftime("%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                 # noqa: BLE001
        pass


def cli(site, argv: list[str], *, prepare=None, extra_help: str = "",
        reread=None) -> int:
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
    parser.add_argument("--purpose", choices=tier_guard.PURPOSES, default=None,
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
        parser.error("the following arguments are required: --purpose（test/review/accept，无默认）")
    # 第 6 条：高档（review/accept）等待上限下限 900s，不得 300s 就判超时
    args.timeout = tier_guard.floor_timeout(args.purpose, args.timeout)
    if not guard.guard(need, wait, force):
        return ec.BUSY
    try:
        if args.read:
            result = read_last(tab=args.tab)
        else:
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
        failure.record(site.name, result)
    runlog.record(site.name, result)
    _log_criteria(site.name, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else ec.code_for(result.get("stage"))
