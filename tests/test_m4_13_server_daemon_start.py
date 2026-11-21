"""Offline coverage for M4-13: HTTP POST /daemon/start daemon control route.

Tests:
1. start defaults returns 200 and forwards normalized default options.
2. start custom options returns 200 and forwards validated options.
3. start already running returns 200 and reports already_running: True.
4. start rejects invalid brain, executor, or executor_profile with 400.
5. start rejects invalid numbers (timeout, sleep, delay_start, max_rounds) with 400.
6. start_fn exception returns structured 500 without crashing the server.
7. _default_start_fn detects running daemon and returns idempotent status.
8. _default_start_fn spawns detached auto_heal_loop.py process with detached flags.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import bridge  # noqa: E402
from engine import server as server_mod  # noqa: E402
from engine.server import _default_start_fn, _detached_spawn_flags, create_server  # noqa: E402


class DaemonStartRouteTests(unittest.TestCase):
    def setUp(self):
        self.start_calls: list[dict[str, Any]] = []
        self.start_error: Exception | None = None
        self.start_return: dict[str, Any] | None = None

        def start_fn(opts: dict[str, Any]) -> dict[str, Any]:
            self.start_calls.append(opts)
            if self.start_error is not None:
                raise self.start_error
            if self.start_return is not None:
                return dict(self.start_return)
            return {
                "ok": True,
                "action": "start",
                "started": True,
                "already_running": False,
                "pid": 54321,
            }

        self.srv = create_server(
            dispatcher=lambda action, endpoint, opts: {"ok": True},
            endpoint_names=["gemini"],
            host="127.0.0.1",
            port=0,
            start_fn=start_fn,
        )
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)

    def _call(self, path: str, *, payload: Any = None, raw: bytes | None = None, method: str = "POST"):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = raw if raw is not None else (
            None if payload is None else json.dumps(payload).encode("utf-8")
        )
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_start_defaults_returns_200_and_forwards_default_options(self):
        code, body = self._call("/daemon/start", payload={})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "start")
        self.assertTrue(body["started"])
        self.assertEqual(body["pid"], 54321)

        self.assertEqual(len(self.start_calls), 1)
        opts = self.start_calls[0]
        self.assertEqual(opts["brain"], "auto")
        self.assertEqual(opts["executor"], "workbuddy")
        self.assertEqual(opts["executor_profile"], "none")
        self.assertEqual(opts["timeout"], 600.0)
        self.assertEqual(opts["sleep"], 30.0)
        self.assertEqual(opts["delay_start"], 0.0)
        self.assertEqual(opts["max_rounds"], 20)
        self.assertTrue(opts["stop_on_bug"])
        self.assertFalse(opts["no_push"])

    def test_start_custom_options_returns_200_and_forwards_normalized_options(self):
        custom = {
            "brain": "grok",
            "executor": "antigravity",
            "executor_profile": "council",
            "timeout": 300,
            "sleep": 15,
            "delay_start": 5,
            "max_rounds": 10,
            "stop_on_bug": False,
            "no_push": True,
        }
        code, body = self._call("/daemon/start", payload=custom)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

        self.assertEqual(len(self.start_calls), 1)
        opts = self.start_calls[0]
        self.assertEqual(opts["brain"], "grok")
        self.assertEqual(opts["executor"], "antigravity")
        self.assertEqual(opts["executor_profile"], "council")
        self.assertEqual(opts["timeout"], 300.0)
        self.assertEqual(opts["sleep"], 15.0)
        self.assertEqual(opts["delay_start"], 5.0)
        self.assertEqual(opts["max_rounds"], 10)
        self.assertFalse(opts["stop_on_bug"])
        self.assertTrue(opts["no_push"])

    def test_start_already_running_idempotent(self):
        self.start_return = {
            "ok": True,
            "action": "start",
            "started": False,
            "already_running": True,
            "pid": 8888,
            "status": {"running": True, "pid": 8888},
        }
        code, body = self._call("/daemon/start", payload={})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["started"])
        self.assertTrue(body["already_running"])
        self.assertEqual(body["pid"], 8888)
        self.assertEqual(body["status"]["pid"], 8888)

    def test_start_rejects_invalid_brain_and_executor(self):
        invalid_cases = [
            ({"brain": "chatgpt"}, "brain must be one of"),
            ({"executor": "vscode"}, "executor must be one of"),
            ({"executor_profile": "expert"}, "executor_profile must be one of"),
        ]
        for payload, expected_hint in invalid_cases:
            code, body = self._call("/daemon/start", payload=payload)
            self.assertEqual(code, 400)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "invalid_start_option")
            self.assertIn(expected_hint, body["message"])

        self.assertEqual(self.start_calls, [], "start_fn must never be called on invalid option")

    def test_start_rejects_invalid_timeout_sleep_delay_and_rounds(self):
        invalid_numbers = [
            {"timeout": 0},
            {"timeout": -10},
            {"timeout": "not-a-number"},
            {"timeout": True},
            {"sleep": -1},
            {"sleep": "invalid"},
            {"delay_start": -1},
            {"delay_start": "invalid"},
            {"max_rounds": 0},
            {"max_rounds": -5},
            {"max_rounds": "abc"},
            {"max_rounds": True},
            {"max_rounds": [1]},
        ]
        for payload in invalid_numbers:
            code, body = self._call("/daemon/start", payload=payload)
            self.assertEqual(code, 400, "payload %r should return 400" % payload)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "invalid_start_option")

        self.assertEqual(self.start_calls, [], "start_fn must never be called on invalid number")

    def test_start_failure_returns_structured_500(self):
        self.start_error = RuntimeError("subsystem failed to spawn")
        code, body = self._call("/daemon/start", payload={})
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "start_failed")
        self.assertIn("subsystem failed to spawn", body["message"])


class DefaultStartFnUnitTests(unittest.TestCase):
    def test_default_start_fn_already_running(self):
        with mock.patch("bridge.daemon_status", return_value={"running": True, "pid": 1111}):
            with mock.patch("subprocess.Popen") as mock_popen:
                result = _default_start_fn({})

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "start")
        self.assertFalse(result["started"])
        self.assertTrue(result["already_running"])
        self.assertEqual(result["pid"], 1111)
        mock_popen.assert_not_called()

    def test_default_start_fn_spawns_detached_process(self):
        mock_proc = SimpleNamespace(pid=2222)
        with mock.patch("bridge.daemon_status", return_value={"running": False, "pid": None}):
            with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                opts = {
                    "brain": "grok",
                    "executor": "workbuddy",
                    "executor_profile": "routine",
                    "timeout": 300,
                    "sleep": 10,
                    "delay_start": 0,
                    "max_rounds": 5,
                    "stop_on_bug": True,
                    "no_push": True,
                }
                result = _default_start_fn(opts)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "start")
        self.assertTrue(result["started"])
        self.assertFalse(result["already_running"])
        self.assertEqual(result["pid"], 2222)

        mock_popen.assert_called_once()
        cmd, kwargs = mock_popen.call_args[0][0], mock_popen.call_args[1]

        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("auto_heal_loop.py"))
        self.assertIn("--daemon", cmd)
        self.assertIn("--brain", cmd)
        self.assertEqual(cmd[cmd.index("--brain") + 1], "grok")
        self.assertIn("--executor", cmd)
        self.assertEqual(cmd[cmd.index("--executor") + 1], "workbuddy")
        self.assertIn("--executor-profile", cmd)
        self.assertEqual(cmd[cmd.index("--executor-profile") + 1], "routine")
        self.assertIn("--timeout", cmd)
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "300")
        self.assertIn("--sleep", cmd)
        self.assertEqual(cmd[cmd.index("--sleep") + 1], "10")
        self.assertIn("--delay-start", cmd)
        self.assertEqual(cmd[cmd.index("--delay-start") + 1], "0")
        self.assertIn("--max-rounds", cmd)
        self.assertEqual(cmd[cmd.index("--max-rounds") + 1], "5")
        self.assertIn("--stop-on-bug", cmd)
        self.assertIn("--no-push", cmd)

        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        expected_flags = _detached_spawn_flags()
        if expected_flags:
            self.assertEqual(kwargs["creationflags"], expected_flags)


if __name__ == "__main__":
    unittest.main()
