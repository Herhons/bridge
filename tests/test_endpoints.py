"""Unit tests covering all 10 Bridge v0.2.1 endpoints, chunking, Grok normalization, templates, and OpenCode DB."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from endpoints import ENDPOINT_NAMES, get_driver
from engine.config import TEMPLATES_DIR, load_endpoints_config
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _build_snap(snap_id: str, lines: list[str], *, busy: bool = False) -> Snapshot:
    kids = [
        UINode(role="EditControl", name="输入框", rect=(10, 400, 300, 40)),
        UINode(role="ButtonControl", name="停止" if busy else "发送", rect=(320, 400, 50, 40)),
    ]
    for line in lines:
        kids.append(UINode(role="TextControl", name=line))
    root = UINode(role="WindowControl", name="AI", children=kids)
    return Snapshot(snapshot_id=snap_id, hwnd=42, title="AI", url="", nodes=[root], refs=assign_refs([root], snap_id))


class TestAllTenEndpoints(unittest.TestCase):
    def test_exactly_ten_registered_endpoints(self):
        expected = (
            "chatgpt", "gemini", "grok", "deepseek",
            "codex", "dsh", "opencode", "workbuddy",
            "hermes", "grokbot",
        )
        self.assertEqual(ENDPOINT_NAMES, expected)
        for name in expected:
            drv = get_driver(name)
            self.assertEqual(drv.name, name)
            self.assertTrue(drv.models()["ok"])

    def test_every_configured_template_png_exists(self):
        cfg = load_endpoints_config()
        for ep, ec in cfg.items():
            for role, tpl_name in (ec.get("templates") or {}).items():
                path = TEMPLATES_DIR / f"{tpl_name}.png"
                self.assertTrue(path.is_file(), f"Missing template PNG for {ep}.{role}: {path}")

    def test_full_reply_returned_without_purpose_or_truncation(self):
        long_reply = "A" * 500
        snaps = [
            _build_snap("s0", ["旧消息"]),
            _build_snap("s1", ["旧消息"]),
            _build_snap("s2", ["旧消息", "你好", long_reply], busy=True),
            _build_snap("s3", ["旧消息", "你好", long_reply], busy=False),
            _build_snap("s4", ["旧消息", "你好", long_reply], busy=False),
        ]
        idx = {"i": 0}

        def fake_snap(_hwnd):
            i = min(idx["i"], len(snaps) - 1)
            idx["i"] += 1
            return snaps[i]

        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "deepseek",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
                snapshot_fn=fake_snap,
                window_fn=lambda: WindowInfo(hwnd=42, title="DeepSeek - Google Chrome", process_name="chrome.exe", pid=100),
                invoke_fn=lambda _n, _h: "stub:click",
                write_fn=lambda _n, text, _h, chunk: f"stub:chunk={chunk}:len={len(text)}",
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.ask("你好")
            self.assertTrue(res["ok"])
            self.assertEqual(res["reply"], long_reply)
            self.assertEqual(len(res["reply"]), 500)
            self.assertIn("chunk=1500", res["write_method"])

    def test_grok_normalizes_multiline_prompt(self):
        drv = get_driver("grok")
        self.assertEqual(drv.prepare_prompt("第一行\n\n第二行"), "第一行 第二行")

    def test_opencode_sqlite_readonly_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE session_v2 (id TEXT, title TEXT, time_updated INTEGER)")
            con.execute("CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            con.execute("INSERT INTO session_v2 VALUES ('sess1', 'MyTask', 100)")
            payload = {
                "content": [
                    {"type": "reasoning", "text": "internal thought"},
                    {"type": "text", "text": "最终完整回复内容"},
                ],
                "time": {"completed": 123456},
            }
            con.execute("INSERT INTO session_message VALUES ('sess1', 'assistant', 2, ?)", (json.dumps(payload),))
            con.commit()
            con.close()

            drv = get_driver(
                "opencode",
                db_path=db,
                window_fn=lambda: WindowInfo(hwnd=77, title="MyTask · OpenCode", process_name="OpenCode.exe", pid=200),
                ledger_path=Path(tmp) / "ledger.json",
            )
            res = drv.read()
            self.assertTrue(res["ok"])
            self.assertEqual(res["source"], "sqlite")
            self.assertEqual(res["reply"], "最终完整回复内容")


if __name__ == "__main__":
    unittest.main()
