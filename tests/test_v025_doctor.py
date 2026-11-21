"""Tests for Bridge v0.2.5 doctor health check and version sync."""
from __future__ import annotations

import unittest

import bridge
from endpoints import ENDPOINT_NAMES
from engine import __version__
from engine.config import run_doctor


class TestV025Doctor(unittest.TestCase):
    def test_doctor_passes_cleanly_on_current_repo(self):
        res = run_doctor(ENDPOINT_NAMES)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["endpoint_count"], 11)
        self.assertEqual(res["issues"], [])

    def test_cli_doctor_returns_zero(self):
        self.assertEqual(__version__, "0.2.5")
        rc = bridge.main(["doctor"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
