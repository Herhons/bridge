"""TEST-ISOLATION: normal discovery must be safe without an external wrapper."""
from offline_guard import ensure_offline, expected_denial

ensure_offline()

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from qi import failure, guard, progress, runlog, tier_guard, uilock, workflow
from qi.screen import Screen


class OfflineDefaultIsolation(unittest.TestCase):
    def test_default_failure_capture_log_and_input_mark_stay_in_run(self):
        fence = ensure_offline()
        for path in (runlog.RUNS, failure.FAIL_DIR, guard.MARK_FILE, tier_guard.ROOT,
                     progress.ROOT, uilock.ROOT, workflow.progress_path()):
            self.assertTrue(path == fence.EVENTS or fence.EVENTS in path.parents, path)
        self.assertEqual(Path(tempfile.gettempdir()), fence.OUT / "_tmp")
        image = Screen(verbose=False).grab()
        self.assertEqual(image.shape, (2, 2, 3))
        self.assertFalse(image.any())
        result = {"ok": False, "stage": "tier_unconfirmed", "reason": "isolation-" + uuid.uuid4().hex,
                  "hint": "intentional synthetic offline failure"}
        evidence = failure.record("isolation", result)
        self.assertTrue(Path(evidence["png"]).is_file())
        self.assertTrue(Path(evidence["json"]).is_file())
        self.assertIn(fence.EVENTS, Path(evidence["png"]).parents)
        # Retain the real mark function as the formerly leaking fixture does;
        # only the physical keystroke is mocked.
        with mock.patch("qi.screen._send"), mock.patch("qi.screen.time.sleep"):
            Screen(verbose=False).press("enter")
        self.assertTrue(guard.MARK_FILE.is_file())
        self.assertIsInstance(json.loads(guard.MARK_FILE.read_text())["tick"], int)

    def test_real_python_child_inherits_guard_without_test_import(self):
        code = ("import json,sys\nfrom qi import guard\n"
                "print(json.dumps({'active':sys._bridge_offline_guard_active,"
                "'mark':str(guard.MARK_FILE)}))")
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(Path(data["active"]), ensure_offline().OUT)
        self.assertIn(ensure_offline().EVENTS, Path(data["mark"]).parents)

    def test_real_native_write_network_and_launch_attempts_are_denied(self):
        fence = ensure_offline()
        outside = Path(r"F:\codex\2026-11-06\bridge-v0-0-9-codex-draft\work") / (
            "guard_probe_" + uuid.uuid4().hex + ".txt")
        with expected_denial("outside_write"), self.assertRaises(PermissionError):
            outside.write_text("must not be created")
        self.assertFalse(outside.exists())
        for library, name, args in (("user32", "SendInput", (0, None, 0)),
                                    ("user32", "OpenClipboard", (0,)),
                                    ("user32", "PrintWindow", (0, 0, 0)),
                                    ("gdi32", "BitBlt", (0,) * 9)):
            with self.subTest(name=name), expected_denial("real_ui_api"):
                with self.assertRaises(PermissionError):
                    getattr(getattr(ctypes.windll, library), name)(*args)
        with socket.socket() as sock, expected_denial("external_network"):
            with self.assertRaises(PermissionError):
                sock.connect(("203.0.113.1", 443))
        with socket.socket() as sock, expected_denial("unregistered_loopback"):
            with self.assertRaises(PermissionError):
                sock.connect(("127.0.0.1", 18765))
        with expected_denial("non_test_process"), self.assertRaises(PermissionError):
            subprocess.Popen(["bridge-forbidden-app-must-not-run.exe"])
        env = dict(os.environ, PYTHONPATH=str(fence.OUT / "src"))
        with expected_denial("child_guard_missing"), self.assertRaises(PermissionError):
            subprocess.Popen([sys.executable, "-c", "pass"], env=env)

    def test_caught_unexpected_denial_still_fails_child_at_exit(self):
        fence = ensure_offline()
        run = fence.EVENTS.name + "-expected-fail-child"
        env = dict(os.environ, BRIDGE_WIP_RUN=run)
        outside = fence.OUT / ("source_must_remain_unchanged_" + uuid.uuid4().hex + ".txt")
        code = ("from pathlib import Path\n"
                "try:\n    Path(" + repr(str(outside)) + ").write_text('deny')\n"
                "except PermissionError:\n    pass\n"
                "print('caught, but must exit nonzero',flush=True)")
        result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertIn("OFFLINE_GUARD_UNEXPECTED_EVENTS=1", result.stderr)
        self.assertFalse(outside.exists())
        events = list((fence.OUT / "_selftest" / run).glob("guard_events_*.jsonl"))
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].read_text())["kind"], "outside_write")
