"""比对用的输入归一（CARD-06b 第 5 条 / CARD-09e 第 5 条）。

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

归一规则（卡面原文 + 现场补的两条）：
  · 双方都去掉**行首**列表标记（`- ` / `* ` / `1. ` / `(1)` / 阿拉伯数字）；
    标记两侧的空白与 `_SPACES` 同一组（含 `\u00a0` / `\u3000`，R7 现场）；
  · 去掉行首标题 `#`、引用 `>` 的标记（渲染时会被吃掉）；
  · 行内 Markdown 标记取纯文本：`**x**`→`x`、`~~x~~`→`x`、`` `x` ``→`x`、
    `[文字](链接)`→`文字`、`<链接>`→`链接`；
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

# 行内 Markdown：只吃**成对**的标记，且中间不跨行
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_BOLD_UNDER = re.compile(r"__(?!\s)(.+?)(?<!\s)__")
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")

# 零宽与不可见字符：U+200B..U+200F 一族 + U+FEFF + U+2060 + 软连字符
_INVISIBLE = re.compile(r"[\u200b-\u200f\ufeff\u2060\u00ad]")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def strip_marker(line: str) -> str:
    """去掉一行开头的列表标记。行内出现的标记不动（代码块/正文要保持原样）。"""
    return _MARKER.sub("", line, count=1)


def strip_block(line: str) -> str:
    """去掉行首的标题/引用标记（渲染时会被吃掉的那两个）。"""
    line = _HEADING.sub("", line, count=1)
    return _QUOTE.sub("", line, count=1)


def strip_inline(text: str) -> str:
    """行内 Markdown 取纯文本。顺序有讲究：图片→链接→自动链接→成对标记→代码。"""
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_UNDER.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    return text


def normalize(text) -> str:
    """比对用的规范形。空/None 一律给空串（调用方不用先判空）。

    每行**反复**去列表标记（最多 4 层）：编辑器的自动序号和自己的标记会叠在
    一起（`2. 2. 第二条`）。空行整行丢掉——真机上编辑器就是这么读回来的。
    """
    if not text:
        return ""
    lines = []
    for raw in _lines(str(text)):
        line = strip_inline(_INVISIBLE.sub("", raw))
        for _ in range(4):
            stripped = strip_block(strip_marker(line)).strip()
            if stripped == line.strip():
                break
            line = stripped
        line = _SPACES.sub(" ", strip_block(line)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def equal(expected, actual) -> bool:
    return normalize(expected) == normalize(actual)


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
