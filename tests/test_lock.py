"""Unit tests for Bridge v0.2.1 cross-process UI lock and dead-PID stale recovery."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.lock import LockBusyError, clear_stale_lock, hold_ui_lock


class TestUILock(unittest.TestCase):
    def test_reentrant_same_thread_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "ui.lock"
            with hold_ui_lock("outer", lock_path=lp):
                self.assertTrue(lp.exists())
                with hold_ui_lock("inner", lock_path=lp):
                    self.assertTrue(lp.exists())
                self.assertTrue(lp.exists())
            self.assertFalse(lp.exists())

    def test_stale_dead_pid_lock_is_reclaimed_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "ui.lock"
            lp.write_text(json.dumps({"pid": 99999999, "owner": "crashed"}), encoding="utf-8")
            self.assertTrue(clear_stale_lock(lp))
            self.assertFalse(lp.exists())

            lp.write_text(json.dumps({"pid": 99999999, "owner": "crashed"}), encoding="utf-8")
            with hold_ui_lock("new-caller", timeout=1.0, lock_path=lp):
                meta = json.loads(lp.read_text(encoding="utf-8"))
                self.assertEqual(meta["owner"], "new-caller")
            self.assertFalse(lp.exists())

    def test_live_other_pid_blocks_until_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            lp = Path(tmp) / "ui.lock"
            # PID 1 is alive on POSIX; simulate another live owner via monkeypatch
            lp.write_text(json.dumps({"pid": 1, "owner": "other"}), encoding="utf-8")
            with unittest.mock.patch("engine.lock.pid_alive", return_value=True):
                with self.assertRaises(LockBusyError):
                    with hold_ui_lock("me", timeout=0.05, poll=0.01, lock_path=lp):
                        pass


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
