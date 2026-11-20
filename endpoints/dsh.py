#!/usr/bin/env python3
"""DeepSeek Harness (DSH) desktop endpoint driver for Bridge v0.2.1."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.base import EndpointDriver, run_endpoint_cli  # noqa: E402


class DSHDriver(EndpointDriver):
    name = "dsh"


if __name__ == "__main__":
    raise SystemExit(run_endpoint_cli(DSHDriver))
