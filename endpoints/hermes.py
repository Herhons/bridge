#!/usr/bin/env python3
"""Hermes desktop GUI (Hermes.exe) endpoint driver for Bridge v0.2.1.

Drives Hermes via Windows UIA (primary) with screenshot fallback.
Run ``python bridge.py snapshot hermes`` on Windows to inspect live UIA controls
and adjust ``config/endpoints.json`` if needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402


class HermesDriver(EndpointDriver):
    name = "hermes"


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(HermesDriver))
