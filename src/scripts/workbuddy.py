#!/usr/bin/env python3
"""workbuddy 独立全功能脚本（包含 WorkBuddy 桌面端的模型查询/切换、消息发送与续推）。

用法：
    python src/scripts/workbuddy.py choose --list
    python src/scripts/workbuddy.py choose --model <模型名>
    python src/scripts/workbuddy.py use "任务内容"
    python src/scripts/workbuddy.py use --continue "继续按卡施工"
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qi import uilock  # noqa: E402
from qi.workbuddy import *  # noqa: F401,F403,E402
from qi.workbuddy import main as _wb_main  # noqa: E402


def _detect_mode(argv: list[str]) -> tuple[str, list[str]]:
    if argv and argv[0] in ("choose", "use"):
        return argv[0], argv[1:]
    if "--list" in argv or "-l" in argv:
        return "choose", argv
    if "--continue" in argv or "--read" in argv or "--purpose" in argv or "--timeout" in argv:
        return "use", argv
    i = 0
    pos = []
    while i < len(argv):
        if argv[i] in ("--model", "-m", "--effort", "-e", "--tab"):
            i += 2
            continue
        if argv[i].startswith("-"):
            i += 1
            continue
        pos.append(argv[i])
        i += 1
    return ("use", argv) if pos else ("choose", argv)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    mode, rest = _detect_mode(raw_argv)
    saved = sys.argv[:]
    try:
        sys.argv = [sys.argv[0]] + rest
        if mode == "choose":
            return int(uilock.entry("workbuddy_choose", lambda: _wb_main(sending=False)))
        return int(uilock.entry("workbuddy_use", lambda: _wb_main(sending=True)))
    finally:
        sys.argv = saved


if __name__ == "__main__":
    raise SystemExit(main())
