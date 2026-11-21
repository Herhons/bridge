#!/usr/bin/env python3
"""DeepSeek web endpoint driver for Bridge v0.2.5.

Preserves the verified 1500-char chunked paste (DeepSeek web drops single
pastes exceeding 2000 characters).

M4-3 adds thinking-process (CoT) stripping. The base driver already prefers the
``copy_button`` payload, which is the final answer only; this override covers the
``uia_delta`` fallback, where an expanded reasoning block lands in the same
delta as the answer.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402

#: Headers DeepSeek renders above the reasoning body. Matched after the base
#: class has already applied its own noise filter, so only a surviving header
#: starts a strip.
THINKING_HEADER_RE = re.compile(
    r"^(?:已(?:深度)?思考|思考过程|深度思考|推理过程|分析过程|"
    r"thinking(?: process)?|reasoning|chain of thought)\b",
    re.IGNORECASE,
)
#: An explicit "final answer" label closes a reasoning block deterministically.
THINKING_END_RE = re.compile(
    r"^(?:最终(?:回答|答案)|正式回答|答案|回答|final answer|answer)\s*[:：]?$",
    re.IGNORECASE,
)


def _paragraphs(lines: list[str]) -> list[list[str]]:
    """Group lines into blank-line separated paragraphs (leading blanks dropped)."""
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def strip_thinking_sections(lines: list[str]) -> list[str]:
    """Drop a leading reasoning block, keeping only the final answer.

    The block is only removed when its extent is knowable:

    1. a ``最终回答``-style label closes it — everything before is dropped;
    2. otherwise the body is the first blank-line separated paragraph, so it is
       dropped and the remaining paragraphs are the answer.

    A single indistinguishable blob is returned untouched: returning nothing
    would turn a readable reply into a timeout, which is worse than a CoT line
    leaking into the answer. When every rule would leave nothing behind, the
    original lines win.
    """
    if not lines:
        return []

    header_at = -1
    for idx, line in enumerate(lines):
        if THINKING_HEADER_RE.match(line.strip()):
            header_at = idx
    if header_at < 0:
        return list(lines)

    head = list(lines[:header_at])
    tail = list(lines[header_at + 1:])

    for idx, line in enumerate(tail):
        if THINKING_END_RE.match(line.strip()):
            closed = head + tail[idx + 1:]
            return [line for line in closed if line.strip()] or list(lines)

    groups = _paragraphs(tail)
    if len(groups) >= 2:
        kept = [line for group in groups[1:] for line in group]
        if kept:
            return head + kept
    return head + tail


class DeepSeekDriver(EndpointDriver):
    name = "deepseek"

    def _extract_reply_lines(self, lines: list[str], prompt: str = "") -> list[str]:
        """Strip CoT before *and* after the base class' noise/anchor filtering.

        Stripping first sees the header the base noise regex would otherwise
        delete (``已思考（用时 N 秒）``), so an expanded reasoning body is
        recognisable; stripping again catches headers that survived the base
        filter (``思考过程``, ``Thinking``). Both passes are no-ops when no
        header is present, and neither can return an empty reply on its own.
        """
        trimmed = strip_thinking_sections(list(lines))
        cleaned = super()._extract_reply_lines(trimmed, prompt=prompt)
        return strip_thinking_sections(cleaned)


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(DeepSeekDriver))
