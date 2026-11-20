"""Unit tests for Bridge v0.2.2 master CLI (bridge.py) and local HTTP server."""
from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout

import bridge
from engine import __version__
from engine.server import create_server


class _FakeDriver:
    def __init__(self, name: str):
        self.name = name

    def ask(self, prompt: str, **kwargs):
        return {"ok": True, "endpoint": self.name, "reply": f"[{self.name}] echo: {prompt}"}

    def read(self):
        return {"ok": True, "endpoint": self.name, "reply": f"[{self.name}] latest"}

    def models(self):
        return {"ok": True, "endpoint": self.name, "models": ["default"]}

    def status(self):
        return {"ok": True, "endpoint": self.name, "window_found": True}

    def snapshot(self, **kwargs):
        return {"ok": True, "endpoint": self.name, "snapshot_id": "abc12345", "elements": []}


class TestBridgeCLIAndHTTP(unittest.TestCase):
    def test_cli_list_and_ask_full_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bridge.main(["list", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["version"], __version__)
        self.assertEqual(len(data["endpoints"]), 10)
        self.assertIn("hermes", data["endpoints"])
        self.assertIn("grokbot", data["endpoints"])

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = bridge.main(["ask", "hermes", "hello world"], driver_factory=_FakeDriver)
        self.assertEqual(rc2, 0)
        self.assertEqual(buf2.getvalue().strip(), "[hermes] echo: hello world")

    def test_http_server_health_endpoints_and_ask(self):
        srv = create_server(
            dispatcher=lambda act, ep, opts: bridge.dispatch(act, ep, opts, driver_factory=_FakeDriver),
            endpoint_names=list(bridge.ENDPOINT_NAMES),
            host="127.0.0.1",
            port=0,
        )
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(health, {"ok": True, "version": __version__})

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ask",
                data=json.dumps({"endpoints": ["grok", "grokbot"], "prompt": "hi"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(body["ok"])
            self.assertEqual(body["results"]["grok"]["reply"], "[grok] echo: hi")
            self.assertEqual(body["results"]["grokbot"]["reply"], "[grokbot] echo: hi")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
