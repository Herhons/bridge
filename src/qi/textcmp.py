"""比对用的输入归一与两层校验（CARD-06b 第 5 条 / CARD-09e 第 5 条 / CARD-v010-P0P1）。

## 它解决什么

ChatGPT / Grok 的编辑器会把行首的「- 」「* 」「1. 」「4. 」当成列表标记，
**渲染时把标记吃掉**。于是写进去 1195 字、读回来 1192/1193 字，
`input_mismatch` 整轮作废——2026-11-02 实测 Grok 吃掉「N. 」就是这么来的。

同一类问题还有三种（CARD-09e 第 5 条，都是 2026-11-02 真机现场）：

  · `# 标题` 被渲染成标题，`#` 和后面的空格一起消失（12:08 那次就是它）；
  · URL 被自动转成链接，读回来在链接前后多出零宽字符（16:07 那次：
    期望和实际肉眼一模一样，却在第 503 字判不一致）；
  · 行内 `**粗体**` / `` `代码` `` 被渲染，星号/反引号不剩。

## 只作用于比对，不改发出去的正文

发送走的是 `Screen.type_text(prompt)` 原串，本模块只在"读回来核对"时使用。
正文里的列表标记该发还是发，代码块里的 `-` 也不会被动。

## 2026-11-14（CARD-v010-P0P1）：从"一套归一"改成"两层校验"

旧实现有两个**假阳性**（把篡改放过去）：

  1. `_LINK` 把 `[文字](链接)` 变成 `[文字]`，URL 被整段丢掉 —— 于是
     `[文档](https://a.com)` 与 `[文档](https://b.com)` 归一后完全一样，判"一致"。
  2. `_ITALIC` 跑在 `_CODE` 之前 —— 行内 `` `*x*` `` 先被剥成 `` `x` `` 再被剥成 `x`，
     围栏代码块里的 `*x*` 同样被当斜体吃掉。

现在的分工：

  · `normalize()`：**只为比对服务**的规范形。默认 `link_mode="drop"`，
    语义与旧版一致（`equal()` 靠它保持向后兼容，`tests/test_card09e.py` 钉死了这点）。
  · `compare()`：**判定入口**。第一层严格（链接 target 参与比对），
    只在"可证明回读渠道丢了链接"时降级到第二层，并打 `link_target_unverifiable`。
  · 围栏感知：``` 与 ~~~ 围栏内的行**不做任何行首/行内变换**（只吃行尾空格），
    因为代码块里的缩进和星号是语义，不是格式。

归一规则（卡面原文 + 现场补的两条）：
  · 双方都去掉**行首**列表标记（`- ` / `* ` / `1. ` / `(1)` / 阿拉伯数字）；
    标记两侧的空白与 `_SPACES` 同一组（含 `\\u00a0` / `\\u3000`，R7 现场）；
  · 去掉行首标题 `#`、引用 `>` 的标记（渲染时会被吃掉）；
  · 行内 Markdown 取纯文本：`**x**`→`x`、`~~x~~`→`x`、`` `x` ``→`x`；
    `[文字](链接)` 视 `link_mode`：`drop` 只留文字，`strict` 留 `文字⟨链接⟩`；
  · 去掉零宽/不可见字符（`\\u200b` 一族、`\\ufeff`、软连字符），`\\u00a0` 当空格；
  · 统一换行（`\\r\\n` / `\\r` → `\\n`）、合并连续空白；
  · 去每行首尾空白，再去整串首尾空白。

`describe()` 给 `input_mismatch` 的 hint 用：第一处差异的行号 + 前后 20 字。
"第 3 行：期望…/实际…" 比"输入框读到 1192 字"有用得多。
"""

from __future__ import annotations

import re

# 行首列表标记：- * + • · / 1. 1) 1、 / (1) （1）
# 只吃**行首**，且要求标记后面有空白（`1.5` 这种小数不是列表）。
# 注意 `\d{1,2}[ \t]*[.)、．]` 后面**必须**有空白（或行尾）：否则 `1.5` 会被当成
# 列表标记，把小数点后的数字吃掉。
#
# 2026-11-07（R7）：标记两侧的"空白"必须和 `_SPACES` 用同一组字符。
# Gemini 读回来的行首 `·` 后面是 U+00A0（&nbsp;）：旧正则只认 `[ \t]`，于是
# 期望里被吃掉的 `· ` 在实际里留了下来（第 19 行第 1 字起不一致）；
# 同一行其余字符完全一致。把 `\u00a0`/`\u3000` 并进来即可，不改发出去的正文。
_GAP = r"[ \t\u00a0\u3000]"
_MARKER = re.compile(
    r"^" + _GAP + r"*(?:[-*+•·]" + _GAP + r"+"
    r"|\d{1,2}" + _GAP + r"*[.)、．]" + _GAP + r"+"
    r"|[（(]" + _GAP + r"*\d{1,2}" + _GAP + r"*[)）]" + _GAP + r"*)"
)

# 行首标题 / 引用：渲染后 `#`、`>` 都不剩
_HEADING = re.compile(r"^" + _GAP + r"*#{1,6}" + _GAP + r"+")
_QUOTE = re.compile(r"^" + _GAP + r"*>+" + _GAP + r"?")

#: ``` 或 ~~~ 围栏（CommonMark：关闭围栏必须同字符、长度不小于开启）。
_FENCE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<mark>`{3,}|~{3,})(?P<info>.*)$")

# 行内 Markdown：只吃**成对**的标记，且中间不跨行
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_BOLD_UNDER = re.compile(r"__(?!\s)(.+?)(?<!\s)__")
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
#: 行内代码：等长反引号配对，避免 ``a`` 与 `b` 交叉匹配。
_CODE_SPAN = re.compile(r"(?<!`)(`+)(.+?)\1(?!`)")

# 零宽与不可见字符：U+200B..U+200F 一族 + U+FEFF + U+2060 + 软连字符
_INVISIBLE = re.compile(r"[\u200b-\u200f\ufeff\u2060\u00ad]")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")

#: 行内代码占位哨兵。NUL 不是编辑器能产出的字符，进来时先清掉，冲突概率为零。
_SENTINEL = "\x00"

#: 链接 target 的包裹符（strict 层）。用 ⟨⟩ 而不是括号，避免与正文括号混淆。
_LTAG = "\u27e8"
_RTAG = "\u27e9"

#: 截断判据（都作用在**归一后**长度上，合法归一化因此被对称抵消）。
MIN_TRUNC_CHECK = 48
RATIO_HARD = 0.85
RATIO_SOFT = 0.97
DELTA_MIN = 16
EDGE_CHARS = 24
EDGE_MIN_LEN = 120


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


# ---------------------------------------------------------------- 围栏感知
class _FenceState:
    """``` / ~~~ 围栏状态机。`feed()` 返回 (kind, token)。

    kind:
      · `"fence"`  —— 这一行是围栏行本身；token 是它的规范形
      · `"inside"` —— 围栏内的普通行
      · `"text"`   —— 围栏外（含未闭合后紧跟的正文）
    """

    __slots__ = ("char", "length", "opened", "count", "closed")

    def __init__(self) -> None:
        self.char: str | None = None
        self.length = 0
        self.opened = 0
        self.count = 0
        self.closed = 0

    def feed(self, line: str) -> tuple[str, str]:
        match = _FENCE.match(line)
        if self.char is None:
            if match:
                mark = match.group("mark")
                self.char = mark[0]
                self.length = len(mark)
                self.opened += 1
                return "fence", _fence_token(mark, match.group("info"))
            return "text", ""
        # 已打开：只有同字符且不短于开启长度的围栏行能关闭
        if (match and match.group("mark")[0] == self.char
                and len(match.group("mark")) >= self.length
                and not match.group("info").strip()):
            token = _fence_token(match.group("mark"), "")
            self.char, self.length = None, 0
            self.closed += 1
            return "fence", token
        return "inside", ""


def _fence_token(mark: str, info: str) -> str:
    """围栏行的规范形：容忍 5 个反引号写成 3 个、Python/python 大小写。"""
    return f"fence:{mark[0]}{min(len(mark), 3)}:{info.strip().lower()}"


def scan_fences(text) -> dict:
    """围栏统计，给 `compare()` 的 stats 与 `fence_unbalanced` 用。"""
    state = _FenceState()
    for raw in _lines(str(text or "")):
        state.feed(raw)
    return {"opened": state.opened, "closed": state.closed,
            "unbalanced": state.opened != state.closed}


# ---------------------------------------------------------------- 行归一
def strip_marker(line: str) -> str:
    """去掉一行开头的列表标记。行内出现的标记不动（代码块/正文要保持原样）。"""
    return _MARKER.sub("", line, count=1)


def strip_block(line: str) -> str:
    """去掉行首的标题/引用标记（渲染时会被吃掉的那两个）。"""
    line = _HEADING.sub("", line, count=1)
    return _QUOTE.sub("", line, count=1)


def _extract_code(text: str) -> tuple[str, list[str]]:
    """把行内代码换成哨兵占位，返回 (占位后的文本, 代码原文列表)。"""
    spans: list[str] = []

    def repl(match: re.Match) -> str:
        spans.append(match.group(2))
        return f"{_SENTINEL}{len(spans) - 1}{_SENTINEL}"

    return _CODE_SPAN.sub(repl, text), spans


def _restore_code(text: str, spans: list[str]) -> str:
    for index, span in enumerate(spans):
        text = text.replace(f"{_SENTINEL}{index}{_SENTINEL}", span)
    return text


def strip_inline(text: str, *, link_mode: str = "drop") -> str:
    """行内 Markdown 取纯文本。

    顺序有讲究：**先抽行内代码**（占位）→ 图片/链接/自动链接 → 成对标记 → 还原代码。
    旧版把 `_ITALIC` 排在 `_CODE` 前面，于是 `` `*x*` `` 会被剥成 `x`（假阳性）。
    """
    text, spans = _extract_code(text)
    if link_mode == "strict":
        text = _IMAGE.sub(lambda m: f"{m.group(1)}{_LTAG}{m.group(2)}{_RTAG}", text)
        text = _LINK.sub(lambda m: f"{m.group(1)}{_LTAG}{m.group(2)}{_RTAG}", text)
    else:
        text = _IMAGE.sub(r"\1", text)
        text = _LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_UNDER.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    return _restore_code(text, spans)


def _normalize_line(raw: str, *, link_mode: str) -> str:
    line = strip_inline(_INVISIBLE.sub("", raw), link_mode=link_mode)
    for _ in range(4):
        stripped = strip_block(strip_marker(line)).strip()
        if stripped == line.strip():
            break
        line = stripped
    return _SPACES.sub(" ", strip_block(line)).strip()


def normalize(text, *, link_mode: str = "drop", fences: bool = True) -> str:
    """比对用的规范形。空/None 一律给空串（调用方不用先判空）。

    每行**反复**去列表标记（最多 4 层）：编辑器的自动序号和自己的标记会叠在
    一起（`2. 2. 第二条`）。空行整行丢掉——真机上编辑器就是这么读回来的。

    围栏内（`fences=True`）**只**吃行尾空格：行首缩进与内部星号是语义。
    """
    if not text:
        return ""
    body = str(text).replace(_SENTINEL, "")
    lines: list[str] = []
    state = _FenceState() if fences else None
    for raw in _lines(body):
        if state is not None:
            kind, token = state.feed(raw)
            if kind == "fence":
                lines.append(token)
                continue
            if kind == "inside":
                lines.append(raw.rstrip())
                continue
        line = _normalize_line(raw, link_mode=link_mode)
        if line:
            lines.append(line)
    return "\n".join(lines)


def equal(expected, actual, *, link_mode: str = "drop") -> bool:
    """向后兼容的布尔判等：语义与 CARD-09e 完全一致（默认丢链接 target）。"""
    return normalize(expected, link_mode=link_mode) == normalize(actual, link_mode=link_mode)


# ---------------------------------------------------------------- 两层校验
def _link_skeleton(text: str) -> list[tuple[str, str]]:
    """按出现顺序抽出 [(链接文字, target)]，含图片与自动链接。"""
    found: list[tuple[int, str, str]] = []
    for pattern in (_IMAGE, _LINK):
        for match in pattern.finditer(str(text or "")):
            found.append((match.start(), match.group(1), match.group(2)))
    for match in _AUTOLINK.finditer(str(text or "")):
        found.append((match.start(), match.group(1), match.group(1)))
    found.sort(key=lambda item: item[0])
    return [(text, target) for _, text, target in found]


def _head_tail(text: str) -> tuple[str, str]:
    return text[:EDGE_CHARS], text[-EDGE_CHARS:]


def compare(expected, actual, *, link_mode: str = "auto", fences: bool = True,
            truncation: bool = True, around: int = 20) -> dict:
    """两层校验。返回的 `ok` 是唯一判定。

    第一层：`link_mode="strict"`（链接 target 参与比对）。
    第二层：**只在**「期望有链接、实际一个都没有、两侧链接文字序列相同」时
            降级到 `drop` 重比，并置 `link_target_unverifiable=True`。

    因此 `[文档](https://a.com)` vs `[文档](https://b.com)` 两侧都有 target，
    第二层条件不成立 → 判**不一致**（修掉旧版的假阳性）。
    """
    want = "" if expected is None else str(expected)
    got = "" if actual is None else str(actual)
    fences_seen = scan_fences(want) if fences else {"opened": 0, "closed": 0,
                                                    "unbalanced": False}

    stats: dict = {
        "expected_chars": len(want),
        "actual_chars": len(got),
        "expected_norm": 0,
        "actual_norm": 0,
        "ratio": 0.0,
        "lines_expected": 0,
        "lines_actual": 0,
        "fence_lines": fences_seen["opened"] + fences_seen["closed"],
        "links_expected": 0,
        "links_actual": 0,
        "link_mode_used": "strict" if link_mode in ("auto", "strict") else link_mode,
    }

    def out(ok: bool, reason: str | None, *, degraded: bool = False,
            diff: dict | None = None, hint: str = "") -> dict:
        stats["link_mode_used"] = "drop" if degraded or link_mode == "drop" else "strict"
        return {"ok": ok, "reason": reason, "link_target_unverifiable": degraded,
                "diff": diff, "hint": hint, "stats": stats}

    if not want.strip():
        return out(False, "expected_empty", hint="期望正文为空，不能判一致")
    if not got.strip():
        return out(False, "readback_empty", hint="读回来是空的（编辑器没吃进任何文字）")

    strict = link_mode != "drop"
    mode = "strict" if strict else "drop"
    w_norm = normalize(want, link_mode=mode, fences=fences)
    g_norm = normalize(got, link_mode=mode, fences=fences)
    stats.update(expected_norm=len(w_norm), actual_norm=len(g_norm),
                 lines_expected=w_norm.count("\n") + 1,
                 lines_actual=g_norm.count("\n") + 1,
                 ratio=(len(g_norm) / len(w_norm)) if w_norm else 0.0,
                 links_expected=len(_link_skeleton(want)),
                 links_actual=len(_link_skeleton(got)))

    if w_norm == g_norm:
        return out(True, None)

    # ---- 第二层：只在可证明"通道丢了链接"时降级
    degraded = False
    if link_mode == "auto":
        links_w, links_g = _link_skeleton(want), _link_skeleton(got)
        # 触发条件必须同时成立：
        #   · 期望里有链接；
        #   · 回读里**一个链接都没有**（这才像"纯文本渠道丢了 target"）；
        #   · 用 drop 归一后两边完全一致（链接文字仍在正确位置）。
        # 反例：`[文档](a)` vs `[文档](b)` —— 回读也有链接，条件不成立，
        # 于是 strict 层的不一致被保留下来（不再静默放行）。
        if links_w and not links_g:
            w_drop = normalize(want, link_mode="drop", fences=fences)
            g_drop = normalize(got, link_mode="drop", fences=fences)
            if w_drop == g_drop:
                stats.update(expected_norm=len(w_drop), actual_norm=len(g_drop),
                             ratio=(len(g_drop) / len(w_drop)) if w_drop else 0.0)
                return out(True, "link_targets_lost", degraded=True,
                           hint="回读渠道没有链接 target，只比对了链接文字（未验证目标地址）")
            w_norm, g_norm = w_drop, g_drop
            degraded = True

    # ---- 截断检测（归一后长度，两侧同一套规则 → 合法变换被对称抵消）
    w_drop = normalize(want, link_mode="drop", fences=fences)
    g_drop = normalize(got, link_mode="drop", fences=fences)
    w_len, g_len = len(w_drop), len(g_drop)
    if truncation and w_len >= MIN_TRUNC_CHECK:
        ratio = g_len / w_len if w_len else 0.0
        if ratio < RATIO_HARD:
            return out(False, "truncated", degraded=degraded,
                       diff=first_diff(want, got, around=around),
                       hint=f"读回来的文本明显短了（{lengths(want, got)}）")
        if (w_len - g_len) >= DELTA_MIN and ratio < RATIO_SOFT:
            if _head_tail(w_drop)[0] != _head_tail(g_drop)[0] or \
                    _head_tail(w_drop)[1] != _head_tail(g_drop)[1]:
                return out(False, "truncated", degraded=degraded,
                           diff=first_diff(want, got, around=around),
                           hint=f"读回来的首尾片段对不上（{lengths(want, got)}）")
    if truncation and w_len >= EDGE_MIN_LEN and w_drop != g_drop:
        if _head_tail(w_drop) != _head_tail(g_drop):
            return out(False, "truncated", degraded=degraded,
                       diff=first_diff(want, got, around=around),
                       hint=f"首尾关键片段被改动或整体错位（{lengths(want, got)}）")

    reason = "text_mismatch"
    return out(False, reason, degraded=degraded,
               diff=first_diff(want, got, around=around),
               hint=describe(want, got, around=around))


# ---------------------------------------------------------------- 诊断
def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def first_diff(expected, actual, *, around: int = 20) -> dict | None:
    """第一处差异。完全一致返回 None。

    返回 {"line": 行号(1 起), "index": 行内字符下标, "expected": 前后 N 字,
          "actual": 前后 N 字}
    行号按**归一后**的行算——发给用户看的是"第几行对不上"，不是原始字符偏移。
    """
    want, got = normalize(expected), normalize(actual)
    if want == got:
        return None
    wl, gl = want.split("\n"), got.split("\n")
    for i in range(max(len(wl), len(gl))):
        w = wl[i] if i < len(wl) else ""
        g = gl[i] if i < len(gl) else ""
        if w == g:
            continue
        at = _common_prefix(w, g)
        lo, hi = max(0, at - around), at + around
        return {"line": i + 1, "index": at,
                "expected": w[lo:hi], "actual": g[lo:hi]}
    return None


def describe(expected, actual, *, around: int = 20) -> str:
    """一行字说清第一处差异（给 hint 用）。一致时返回空串。"""
    diff = first_diff(expected, actual, around=around)
    if diff is None:
        return ""
    head = f"第 {diff['line']} 行第 {diff['index'] + 1} 字起不一致"
    return (f"{head}：期望…{diff['expected']}… / 实际…{diff['actual']}…"
            f"（已按「去列表/标题标记 + 行内 Markdown 取纯文本 + 去零宽字符 + 合并空白」"
            f"归一后比对）")


def lengths(expected, actual) -> str:
    """字数诊断：`期望 1195 字 / 实际 1192 字（差 -3）`。"""
    w, g = len(str(expected or "")), len(str(actual or ""))
    return f"期望 {w} 字 / 实际 {g} 字（差 {g - w:+d}）"
