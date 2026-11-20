"""Regression tests for Bridge v0.2.2 (BUG-01 through BUG-10 + --save)."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import bridge
from endpoints import get_driver
from engine.uia import Snapshot, UINode, assign_refs, resolve_automation_client
from engine.window import WindowInfo


class TestV022Bugfixes(unittest.TestCase):
    def test_bug01_resolve_automation_client_from_submodule(self):
        sentinel = object()
        fake_cls = SimpleNamespace(instance=lambda: SimpleNamespace(IUIAutomation=sentinel))
        # Simulate uiautomation 2.0.29 where _AutomationClient is ONLY on uiautomation.uiautomation
        fake_pkg = SimpleNamespace(uiautomation=SimpleNamespace(_AutomationClient=fake_cls))
        self.assertIs(resolve_automation_client(fake_pkg), sentinel)

    def test_bug02_bug08_exclude_address_bar_and_sidebar_search(self):
        kids = [
            UINode(role="EditControl", name="地址和搜索栏", rect=(100, 40, 800, 30)),
            UINode(role="EditControl", name="搜索会话", rect=(20, 100, 200, 30)),
        ]
        root = UINode(role="WindowControl", name="Hermes", rect=(0, 0, 1400, 900), children=kids)
        snap = Snapshot(snapshot_id="h1", hwnd=1, title="Hermes", url="", nodes=[root], refs=assign_refs([root], "h1"), window_rect=(0, 0, 1400, 900))
        drv = get_driver("hermes")
        self.assertIsNone(drv._find_composer(snap))
        self.assertEqual(drv.cfg.get("fallback_composer_ratio"), [0.446, 0.946])

    def test_bug05_bug07_skip_default_model_and_esc_on_missing_option(self):
        # WorkBuddy --model default must skip without clicking menu
        wb = get_driver("workbuddy")
        root = UINode(role="WindowControl", name="WorkBuddy", children=[UINode(role="ButtonControl", name="Select model: Hy4")])
        snap = Snapshot(snapshot_id="w1", hwnd=1, title="WorkBuddy", url="", nodes=[root], refs=assign_refs([root], "w1"))
        res_wb = wb.switch_model(WindowInfo(1, "WorkBuddy", "WorkBuddy.exe", 1), snap, "default", headed=True)
        self.assertFalse(res_wb["attempted"])
        self.assertEqual(res_wb["reason"], "default_skipped")

        # Grok --model fast must NOT mistake selector button itself for option and must press Esc
        keys = []
        sel_btn = UINode(role="ButtonControl", name="型号: Fast", rect=(100, 500, 80, 28))
        g_root = UINode(role="WindowControl", name="Grok", children=[sel_btn])
        g_snap = Snapshot(snapshot_id="g1", hwnd=2, title="Grok", url="", nodes=[g_root], refs=assign_refs([g_root], "g1"))
        grok = get_driver(
            "grok",
            snapshot_fn=lambda _h: g_snap,
            invoke_fn=lambda _n, _h: "stub:click",
            hotkey_fn=lambda k: keys.append(k),
            sleep_fn=lambda _t: None,
        )
        res_g = grok.switch_model(WindowInfo(2, "Grok", "chrome.exe", 2), g_snap, "fast", headed=True)
        self.assertFalse(res_g["switched"])
        self.assertEqual(res_g["reason"], "option_not_found")
        self.assertIn("esc", keys)

    def test_bug06_grokbot_busy_and_old_history_never_false_succeed(self):
        before_root = UINode(
            role="WindowControl",
            name="Grok Bot",
            rect=(0, 0, 1000, 800),
            children=[
                UINode(role="EditControl", name="", rect=(100, 700, 600, 50)),
                UINode(role="ButtonControl", name="发送", rect=(720, 700, 50, 50)),
                UINode(role="TextControl", name="11:42 旧历史消息"),
            ],
        )
        before_snap = Snapshot(snapshot_id="b0", hwnd=3, title="Grok Bot", url="", nodes=[before_root], refs=assign_refs([before_root], "b0"), window_rect=(0, 0, 1000, 800))

        # After send, only stale history + "助手 正在工作" appears, no prompt echo or new copy button
        after_root = UINode(
            role="WindowControl",
            name="Grok Bot",
            rect=(0, 0, 1000, 800),
            children=[
                UINode(role="EditControl", name="", rect=(100, 700, 600, 50)),
                UINode(role="ButtonControl", name="发送", rect=(720, 700, 50, 50)),
                UINode(role="TextControl", name="11:42 旧历史消息"),
                UINode(role="TextControl", name="17:40 另一条旧历史"),
                UINode(role="TextControl", name="助手 正在工作"),
            ],
        )
        after_snap = Snapshot(snapshot_id="b1", hwnd=3, title="Grok Bot", url="", nodes=[after_root], refs=assign_refs([after_root], "b1"), window_rect=(0, 0, 1000, 800))

        calls = {"n": 0}

        def fake_snap(_h):
            calls["n"] += 1
            return before_snap if calls["n"] <= 2 else after_snap

        with tempfile.TemporaryDirectory() as tmp:
            drv = get_driver(
                "grokbot",
                bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 0.05},
                snapshot_fn=fake_snap,
                window_fn=lambda: WindowInfo(3, "Grok Bot", "Grok Bot.exe", 3),
                invoke_fn=lambda _n, _h: "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                sleep_fn=lambda _t: None,
                lock_path=Path(tmp) / "ui.lock",
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=Path(tmp) / "fail",
            )
            res = drv.ask("只回复：收到-grokbot-test", timeout=0.05)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "timeout")
            self.assertEqual(res["reason"], "submit_not_verified")

    def test_bug09_deepseek_strips_reasoning_header_and_footers(self):
        drv = get_driver("deepseek")
        raw_lines = [
            "旧对话标题",
            "只回复：收到-ds-001",
            "已思考（用时 2 秒）",
            "收到-ds-001",
            "智能搜索",
            "内容由 AI 生成，请仔细甄别",
            "chat.deepseek.com",
            "创建拆分视图",
        ]
        cleaned = drv._extract_reply_lines(raw_lines, prompt="只回复：收到-ds-001")
        self.assertEqual(cleaned, ["收到-ds-001"])

    def test_bug10_gemini_maps_hydrogen_alias(self):
        drv = get_driver("gemini")
        root = UINode(role="WindowControl", name="Gemini", children=[UINode(role="RadioButtonControl", name="4 Hydrogen 日常帮助")])
        snap = Snapshot(snapshot_id="gm1", hwnd=4, title="Gemini", url="", nodes=[root], refs=assign_refs([root], "gm1"))
        opt = drv._find_model_option(snap, "flash_lite")
        self.assertIsNotNone(opt)
        self.assertIn("Hydrogen", opt.name)

    def test_cli_save_writes_full_reply_to_file(self):
        class _Stub:
            def __init__(self, name):
                self.name = name

            def ask(self, prompt, **kw):
                return {"ok": True, "endpoint": self.name, "reply": f"FULL:{prompt}"}

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "saved_reply.md"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = bridge.main(["ask", "gemini", "hello", "--save", str(out_file)], driver_factory=_Stub)
            self.assertEqual(rc, 0)
            self.assertEqual(out_file.read_text(encoding="utf-8"), "FULL:hello")


if __name__ == "__main__":
    unittest.main()
