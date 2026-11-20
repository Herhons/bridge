"""Tests for v0.2.5 CLI/HTTP --attach forwarding and WorkBuddy 3-pane safety."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import bridge
from engine.base import EndpointDriver
from engine.server import create_server
from engine.uia import Snapshot, UINode, assign_refs


def _snap(nodes: list[UINode], snap_id: str = "s1") -> Snapshot:
    return Snapshot(snapshot_id=snap_id, hwnd=101, title="WorkBuddy", url="", nodes=nodes, refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1200, 900))


class _StubDriver:
    def __init__(self, ep: str):
        self.ep = ep
        self.last_attach = None

    def ask(self, prompt: str, **kwargs):
        self.last_attach = kwargs.get("attach")
        return {"ok": True, "endpoint": self.ep, "reply": f"ok:{prompt}", "attached_files": list(self.last_attach or [])}


class TestV025CliHttpAndWorkBuddyPane(unittest.TestCase):
    def test_cli_forwards_multiple_attach_flags(self):
        created = {}

        def factory(ep: str):
            d = _StubDriver(ep)
            created[ep] = d
            return d

        with tempfile.TemporaryDirectory() as td:
            f1 = str(Path(td) / "a.txt")
            f2 = str(Path(td) / "b.txt")
            Path(f1).write_text("a", encoding="utf-8")
            Path(f2).write_text("b", encoding="utf-8")
            rc = bridge.main(["ask", "gemini", "hi", "--attach", f1, "--attach", f2, "--json"], driver_factory=factory)
            self.assertEqual(rc, 0)
            self.assertEqual(created["gemini"].last_attach, [f1, f2])

    def test_http_confirm_includes_attach_field(self):
        srv = create_server(dispatcher=lambda a, e, o: {"ok": True}, endpoint_names=["gemini"], host="127.0.0.1", port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ask",
                data=json.dumps({"endpoint": "gemini", "prompt": "check", "attach": ["demo.txt"], "confirm": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                status = e.code
                body = json.loads(e.read().decode("utf-8"))
            self.assertEqual(status, 202)
            self.assertEqual(body["attach"], ["demo.txt"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_workbuddy_ignores_right_preview_editor_when_open_and_still_matches_when_closed(self):
        drv = EndpointDriver("workbuddy")
        # 1. Right preview pane open: chat composer center x=0.415, right preview editor center x=0.805
        s_open = _snap([
            UINode(role="EditControl", name="今天帮你做些什么？", rect=(280, 760, 430, 80)),
            UINode(role="EditControl", name="overnight_summary.md", rect=(740, 760, 440, 80)),
            UINode(role="TextControl", name="左栏历史", rect=(50, 300, 150, 20)),
            UINode(role="TextControl", name="中间真实回复", rect=(320, 300, 300, 20)),
            UINode(role="TextControl", name="右侧文档表格", rect=(820, 300, 300, 20)),
        ])
        comp_open = drv._find_composer(s_open)
        self.assertIsNotNone(comp_open)
        self.assertEqual(comp_open.name, "今天帮你做些什么？")
        lines = s_open.text_lines(min_rel_x=0.23, max_rel_x=0.62)
        self.assertEqual(lines, ["中间真实回复"])

        # 2. Right preview pane closed: chat composer spans 0.23..0.98 (center x=0.605)
        s_closed = _snap([UINode(role="EditControl", name="今天帮你做些什么？", rect=(280, 760, 890, 80))])
        comp_closed = drv._find_composer(s_closed)
        self.assertIsNotNone(comp_closed)
        self.assertEqual(comp_closed.name, "今天帮你做些什么？")


if __name__ == "__main__":
    unittest.main()
