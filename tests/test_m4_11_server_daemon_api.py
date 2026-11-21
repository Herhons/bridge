"""Offline coverage for M4-11: ``/daemon/status`` + ``/daemon/stop`` routes.

A real loopback server is started on port 0, but **both** daemon handlers are
injected: nothing here can write ``runs/STOP``, probe or kill a live PID. That
is the whole point of the ``status_fn`` / ``stop_fn`` seam — the routes are
exercised through HTTP while the control plane stays a mock.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

import bridge
from engine import server as server_mod
from engine.server import _default_status_fn, _default_stop_fn, create_server

STATUS_SNAPSHOT = {
    "ok": True,
    "action": "daemon-status",
    "pid": 4242,
    "running": True,
    "stop_requested": False,
    "state": "VERIFY_TESTS",
    "state_reason": "post-task check",
    "state_round": 4,
    "roadmap_pending": 1,
    "roadmap_total": 16,
    "dispatch_unfinished": 0,
    "dispatch_needs_review": 0,
}

STOP_RESULT = {
    "ok": True,
    "action": "stop",
    "pid": 4242,
    "running": False,
    "stop_written": True,
    "exited_gracefully": True,
    "killed": False,
    "detail": "stopped gracefully",
}


class _DaemonApiTest(unittest.TestCase):
    """Boots one server per test with injected handlers (never the real ones)."""

    def setUp(self):
        self.status_calls: list[bool] = []
        self.stop_calls: list[float] = []
        self.status_error: Exception | None = None
        self.stop_error: Exception | None = None

        def status_fn():
            self.status_calls.append(True)
            if self.status_error is not None:
                raise self.status_error
            return dict(STATUS_SNAPSHOT)

        def stop_fn(force_wait=5.0):
            self.stop_calls.append(force_wait)
            if self.stop_error is not None:
                raise self.stop_error
            return {**STOP_RESULT, "waited_s": 0.0}

        self.srv = create_server(
            dispatcher=lambda action, endpoint, opts: {"ok": True},
            endpoint_names=["grok"],
            host="127.0.0.1",
            port=0,
            status_fn=status_fn,
            stop_fn=stop_fn,
        )
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)

    def _call(self, path, *, payload=None, raw=None, method="GET"):
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


class DaemonStatusRouteTests(_DaemonApiTest):
    def test_status_returns_200_and_the_full_snapshot(self):
        code, body = self._call("/daemon/status")
        self.assertEqual(code, 200)
        self.assertEqual(body, STATUS_SNAPSHOT)
        for field in (
            "running", "pid", "state", "roadmap_pending", "roadmap_total",
            "dispatch_unfinished", "dispatch_needs_review",
        ):
            self.assertIn(field, body)
        self.assertEqual(self.status_calls, [True])
        self.assertEqual(self.stop_calls, [], "a GET must never stop anything")

    def test_status_failure_is_a_structured_500(self):
        self.status_error = RuntimeError("state file unreadable")
        code, body = self._call("/daemon/status")
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "status_failed")
        self.assertIn("state file unreadable", body["message"])


class DaemonStopRouteTests(_DaemonApiTest):
    def test_stop_defaults_to_five_seconds(self):
        code, body = self._call("/daemon/stop", payload={}, method="POST")
        self.assertEqual(code, 200)
        self.assertEqual(self.stop_calls, [5.0])
        self.assertEqual(body["action"], "stop")
        self.assertTrue(body["ok"])
        self.assertIn("waited_s", body)

    def test_stop_forwards_a_custom_force_wait(self):
        code, body = self._call(
            "/daemon/stop", payload={"force_wait": 12.5}, method="POST"
        )
        self.assertEqual(code, 200)
        self.assertEqual(self.stop_calls, [12.5])
        self.assertTrue(body["ok"])

    def test_stop_rejects_a_non_numeric_force_wait_without_calling_the_plane(self):
        for bad in ("soon", None, [1], {"x": 1}, -1):
            code, body = self._call(
                "/daemon/stop", payload={"force_wait": bad}, method="POST"
            )
            self.assertEqual(code, 400, bad)
            self.assertEqual(body["error"], "invalid_force_wait", bad)
            self.assertFalse(body["ok"])
        self.assertEqual(self.stop_calls, [], "a bad body must not reach the stop path")

    def test_stop_failure_is_a_structured_500(self):
        self.stop_error = OSError("taskkill unavailable")
        code, body = self._call("/daemon/stop", payload={}, method="POST")
        self.assertEqual(code, 500)
        self.assertEqual(body["error"], "stop_failed")
        self.assertEqual(len(self.stop_calls), 1)


class InvalidBodyTests(_DaemonApiTest):
    def test_malformed_json_is_400_not_500(self):
        code, body = self._call("/daemon/stop", raw=b"{not json", method="POST")
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "invalid_json")
        self.assertIn("message", body)
        self.assertEqual(self.stop_calls, [])

    def test_non_object_body_is_400_not_500(self):
        code, body = self._call(
            "/daemon/stop", raw=json.dumps([1, 2, 3]).encode("utf-8"), method="POST"
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "invalid_json")
        self.assertEqual(self.stop_calls, [])

    def test_empty_body_still_uses_the_defaults(self):
        code, body = self._call("/daemon/stop", raw=b"", method="POST")
        self.assertEqual(code, 200)
        self.assertEqual(self.stop_calls, [5.0])


class WiringTests(unittest.TestCase):
    """The routes must reuse ``bridge.py``'s control plane, not reimplement it."""

    def test_default_handlers_are_the_bridge_wrappers(self):
        srv = create_server(
            dispatcher=lambda action, endpoint, opts: {"ok": True},
            endpoint_names=["grok"],
            host="127.0.0.1",
            port=0,
        )
        try:
            self.assertIs(srv.status_fn, _default_status_fn)
            self.assertIs(srv.stop_fn, _default_stop_fn)
        finally:
            srv.server_close()

    def test_default_wrappers_point_at_the_cli_entry_points(self):
        import inspect

        status_src = inspect.getsource(_default_status_fn)
        stop_src = inspect.getsource(_default_stop_fn)
        self.assertIn("import bridge", status_src)
        self.assertIn("daemon_status", status_src)
        self.assertIn("import bridge", stop_src)
        self.assertIn("stop_scheduler", stop_src)
        # And the CLI really exposes both (same objects the routes call).
        self.assertTrue(callable(bridge.daemon_status))
        self.assertTrue(callable(bridge.stop_scheduler))
        self.assertTrue(hasattr(server_mod, "BridgeRequestHandler"))


if __name__ == "__main__":
    unittest.main()
