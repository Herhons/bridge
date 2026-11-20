"""Unit tests for the --confirm send gate (CLI and HTTP)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import bridge
from endpoints import get_driver
from engine.base import exit_code_for
from engine.server import create_server
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(snap_id: str, lines: list[str]) -> Snapshot:
    kids = [
        UINode(role="EditControl", name="输入框", rect=(10, 400, 300, 40)),
        UINode(role="ButtonControl", name="发送", rect=(320, 400, 50, 40)),
    ] + [UINode(role="TextControl", name=t) for t in lines]
    root = UINode(role="WindowControl", name="DeepSeek", children=kids)
    return Snapshot(snapshot_id=snap_id, hwnd=1, title="DeepSeek", url="", nodes=[root], refs=assign_refs([root], snap_id))


class TestConfirmGate(unittest.TestCase):
    def test_confirm_cancelled_blocks_send_and_returns_exit_code_4(self):
        invoked = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "deepseek",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=lambda _h: _snap("s0", ["旧消息"]),
                window_fn=lambda: WindowInfo(hwnd=1, title="DeepSeek", process_name="chrome.exe", pid=10),
                invoke_fn=lambda node, _h: invoked.append(node.name) or "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                confirm_fn=lambda _ep, _p, _m: False,
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("不要发出去", confirm=True)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "cancelled_by_user")
            self.assertEqual(exit_code_for(res), 4)
            self.assertEqual(invoked, [])

    def test_confirm_accepted_sends_normally(self):
        snaps = [_snap("s0", []), _snap("s1", []), _snap("s2", ["收到"]), _snap("s3", ["收到"])]
        idx = {"i": 0}

        def next_snap(_h):
            i = min(idx["i"], len(snaps) - 1)
            idx["i"] += 1
            return snaps[i]

        invoked = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "deepseek",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=next_snap,
                window_fn=lambda: WindowInfo(hwnd=1, title="DeepSeek", process_name="chrome.exe", pid=10),
                invoke_fn=lambda node, _h: invoked.append(node.name) or "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                confirm_fn=lambda _ep, _p, _m: True,
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("确认发送", confirm=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["reply"], "收到")
            self.assertEqual(invoked, ["发送"])

    def test_http_confirm_returns_confirmation_required(self):
        srv = create_server(
            dispatcher=lambda act, ep, opts: {"ok": True},
            endpoint_names=list(bridge.ENDPOINT_NAMES),
            host="127.0.0.1",
            port=0,
        )
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ask",
                data=json.dumps({"endpoint": "deepseek", "prompt": "测试确认", "confirm": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(req, timeout=3) as resp:
                self.assertEqual(resp.status, 202)
                body = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "confirmation_required")
            self.assertEqual(body["preview"], "测试确认")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
