#!/usr/bin/env python3
"""Grokbot endpoint driver for Bridge v0.2.1.

Drives Grokbot via Windows UIA (primary) with screenshot fallback.
Run ``python bridge.py snapshot grokbot`` on Windows and tune
``process_names`` / ``title_contains`` / ``selectors`` in ``config/endpoints.json``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402


class GrokbotDriver(EndpointDriver):
    name = "grokbot"


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(GrokbotDriver))
