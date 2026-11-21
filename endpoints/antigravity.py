#!/usr/bin/env python3
"""Antigravity (反重力) desktop GUI endpoint driver for Bridge v0.2.5.

Drives the Electron app ``Antigravity.exe`` via Windows UIA (primary) with
screenshot fallback. The window is pinned by process name, WNDCLASS
(``Chrome_WidgetWin_1``) and title so the OpenCode window whose title merely
contains 反重力 is never matched.

Run ``python bridge.py snapshot antigravity`` on Windows to inspect live UIA
controls and adjust ``config/endpoints.json`` if needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402


class AntigravityDriver(EndpointDriver):
    name = "antigravity"


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(AntigravityDriver))
