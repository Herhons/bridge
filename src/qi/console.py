"""控制台的入口设置：UTF-8 输出 + 打印前去掉零宽字符（CARD-08 第 1 条）。

2026-11-02 09:22 真机：WorkBuddy 的回复里带了 \\u200b，`workbuddy_use` 其实已经读到
正文（同一轮的旁证是 evidence\\accept_07_hy4_reply_uia.txt），却在 `print` 那一步被
GBK 控制台打成 UnicodeEncodeError —— 活干完了，是"输出"把它弄崩的。

三条约定，全都只作用在"往流里写"这一层：

1. **编码**：`reconfigure(encoding="utf-8", errors="replace")`。
   流没有 reconfigure（被换成自定义对象、某些 IDE 的控制台）时退回 `FilteredStream`：
   有 `buffer` 就按 utf-8 写字节；连 buffer 都没有，就只能原地写，编不出来的字用 `?`
   顶替，绝不抛 UnicodeEncodeError。
2. **零宽字符**（\\u200b \\u200c \\u200d \\ufeff）在写之前去掉。
   去掉的只是**打印出去的那一份**：返回给调用方的结果对象、写进证据文件的正文
   一个字都不改（证据要的是原文）。
3. **不叠包装**：已经是 `FilteredStream` 就返回 "already"，调两次不会套两层。

入口怎么用（每个 `*_use.py` / `*_choose.py` / `workflow_use.py` 的 `__main__` 里）：

    from qi import console
    ...
    if __name__ == "__main__":
        console.setup()
        raise SystemExit(main())

放在 `__main__` 里而不是模块顶层：测试 import 这些脚本时不该动全局的 stdout。
"""
from __future__ import annotations

import sys

ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"
ENCODING = "utf-8"
ERRORS = "replace"
_TRANSLATION = {ord(char): None for char in ZERO_WIDTH}


def strip_zero_width(text):
    """去掉零宽字符；非 str 原样返回（`write()` 偶尔收到别的类型）。"""
    return text.translate(_TRANSLATION) if isinstance(text, str) else text


def _reconfigure(stream) -> bool:
    """能改就改成 utf-8；改不了返回 False（调用方据此决定要不要按字节写）。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return False
    try:
        reconfigure(encoding=ENCODING, errors=ERRORS)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


class FilteredStream:
    """stdout / stderr 的薄包装：写之前去零宽，必要时改走 utf-8 字节。

    其余属性（`buffer` / `fileno` / `isatty` / `closed` …）一律转给下面那个真流，
    所以 `logging`、进度条之类拿 `sys.stdout.buffer` 的代码照样能用。
    `close()` 不拦也不转：谁都不该关掉真的 stdout，真关了也是它自己的事。
    """

    def __init__(self, raw, *, bytes_out: bool = False):
        self._raw = raw
        self._bytes_out = bool(bytes_out) and getattr(raw, "buffer", None) is not None
        self.encoding = ENCODING if self._bytes_out else (getattr(raw, "encoding", None) or ENCODING)
        self.errors = ERRORS
        self.closed = False

    # -- 写 ------------------------------------------------------------
    def write(self, text):
        if not isinstance(text, str):
            return self._raw.write(text)
        cleaned = strip_zero_width(text)
        if not cleaned:
            return len(text)
        if self._bytes_out:
            # reconfigure 不可用：绕开它自己的编码，直接按 utf-8 落字节
            self._raw.buffer.write(cleaned.encode(ENCODING, ERRORS))
            return len(text)
        try:
            return self._raw.write(cleaned)
        except UnicodeEncodeError:
            # 既不能改编码、又编不出来：把编不出来的字换成 ?，宁可难看也不崩
            encoding = getattr(self._raw, "encoding", None) or "ascii"
            return self._raw.write(cleaned.encode(encoding, ERRORS).decode(encoding, ERRORS))

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    # -- 其余都转给真流 -------------------------------------------------
    def flush(self):
        self._raw.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._raw, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._raw.fileno()

    def __getattr__(self, name):
        return getattr(self._raw, name)


def setup(out=None, err=None) -> dict:
    """装好两个流，返回每个流走了哪条路：`already` / `reconfigure` / `wrapper`。

    参数只给测试用；平时不传，装的就是 `sys.stdout` / `sys.stderr`。
    """
    report = {}
    for name, stream in (("stdout", sys.stdout if out is None else out),
                         ("stderr", sys.stderr if err is None else err)):
        if isinstance(stream, FilteredStream):
            report[name] = "already"
            continue
        reconfigured = _reconfigure(stream)
        setattr(sys, name, FilteredStream(stream, bytes_out=not reconfigured))
        report[name] = "reconfigure" if reconfigured else "wrapper"
    return report


def utf8_open(path, mode: str = "a"):
    """写文件一律 utf-8（CARD-08）。`newline=""`：换行由调用方说了算，别偷偷转 CRLF。"""
    return open(path, mode, encoding=ENCODING, errors=ERRORS, newline="")
