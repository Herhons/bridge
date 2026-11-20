"""Regression tests for Bridge v0.2.3 fixes and new capabilities."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from engine.base import EndpointDriver, exit_code_for, safe_print_json
from engine.config import classify_model_label, get_endpoint_config, resolve_profile
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(nodes: list[UINode], *, snap_id: str = "s1", rect=(0, 0, 1400, 900)) -> Snapshot:
    refs = assign_refs(nodes, snap_id)
    return Snapshot(snapshot_id=snap_id, hwnd=101, title="Test", url="", nodes=nodes, refs=refs, window_rect=rect)


class _StrictGBKStream(io.StringIO):
    encoding = "gbk"

    def write(self, s: str) -> int:
        s.encode("gbk")  # Raises UnicodeEncodeError on emojis like \u2705
        return super().write(s)


class TestV023Fixes(unittest.TestCase):
    def test_gbk_safe_print_json_handles_emoji(self):
        stream = _StrictGBKStream()
        payload = {"ok": True, "reply": "已完成 ✅ 测试"}
        safe_print_json(payload, stream=stream)
        parsed = json.loads(stream.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertIn("✅", parsed["reply"])

    def test_classify_model_label_pricing(self):
        self.assertEqual(classify_model_label("Hy4 flash 限时免费 0.00x"), "free")
        self.assertEqual(classify_model_label("DeepSeek-V4.1-Flash 0.11x"), "paid")
        self.assertEqual(classify_model_label("Hy4 0.29x"), "paid")
        self.assertEqual(classify_model_label("极致 1.00x"), "paid")
        self.assertEqual(classify_model_label("日常帮助 4 Hydrogen"), "free")
        self.assertEqual(classify_model_label("Neon"), "unknown")

    def test_resolve_profile_mappings(self):
        m, free_only = resolve_profile("workbuddy", "test")
        self.assertEqual(m, "free")
        self.assertTrue(free_only)
        m2, free2 = resolve_profile("gemini", "council")
        self.assertEqual(m2, "carbon")
        self.assertFalse(free2)

    def test_workbuddy_profile_test_auto_picks_free_000x(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        sel_btn = UINode(role="ButtonControl", name="Select model DeepSeek-V4.1-Flash 0.11x", rect=(400, 820, 220, 30))
        composer = UINode(role="EditControl", name="输入指令", rect=(400, 760, 800, 60))
        s_init = _snap([sel_btn, composer])
        s_menu = _snap([
            sel_btn,
            UINode(role="MenuItemControl", name="DeepSeek-V4.1-Flash 0.11x", rect=(400, 600, 220, 30)),
            UINode(role="MenuItemControl", name="Hy4 flash 限时免费 0.00x", rect=(400, 640, 220, 30)),
            UINode(role="MenuItemControl", name="极致 1.00x", rect=(400, 680, 220, 30)),
            composer,
        ], snap_id="s2")
        s_after = _snap([
            UINode(role="ButtonControl", name="Select model Hy4 flash 限时免费 0.00x", rect=(400, 820, 220, 30)),
            composer,
            UINode(role="TextControl", name="只回复：收到-wb", rect=(400, 300, 400, 30)),
            UINode(role="TextControl", name="收到-wb", rect=(400, 350, 400, 30)),
        ], snap_id="s3")

        snaps = [s_init, s_menu, s_init, s_init, s_after, s_after, s_after]
        idx = {"i": 0}
        clicked = []

        def fake_snap(_hwnd):
            out = snaps[min(idx["i"], len(snaps) - 1)]
            idx["i"] += 1
            return out

        def fake_invoke(node, _headed):
            clicked.append(node.name)
            return "stub:click"

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "workbuddy",
                snapshot_fn=fake_snap,
                window_fn=lambda: win,
                invoke_fn=fake_invoke,
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
                bridge_config={"stable_seconds": 0.0, "poll_interval": 0.0},
            )
            res = drv.ask("只回复：收到-wb", profile="test", headed=True, force=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["reply"], "收到-wb")
            self.assertIn("Hy4 flash 限时免费 0.00x", clicked)
            self.assertEqual(res["model_switch"]["tier"], "free")

    def test_workbuddy_profile_test_blocks_paid_fail_closed(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        sel_btn = UINode(role="ButtonControl", name="Select model DeepSeek-V4.1-Flash 0.11x", rect=(400, 820, 220, 30))
        composer = UINode(role="EditControl", name="输入指令", rect=(400, 760, 800, 60))
        s_init = _snap([sel_btn, composer])
        s_menu_paid_only = _snap([
            sel_btn,
            UINode(role="MenuItemControl", name="DeepSeek-V4.1-Flash 0.11x", rect=(400, 600, 220, 30)),
            UINode(role="MenuItemControl", name="极致 1.00x", rect=(400, 680, 220, 30)),
            composer,
        ], snap_id="s2")
        snaps = [s_init, s_menu_paid_only]
        idx = {"i": 0}
        esc_sent = []

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "workbuddy",
                snapshot_fn=lambda _h: snaps[min(idx.__setitem__("i", idx["i"] + 1) or idx["i"] - 1, len(snaps) - 1)],
                window_fn=lambda: win,
                invoke_fn=lambda _n, _h: "stub:click",
                write_fn=lambda _n, _t, _h, _c: "stub:write",
                hotkey_fn=lambda *k: esc_sent.append(k),
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
            )
            res = drv.ask("测试不烧钱", profile="test", headed=True, force=True)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "tier_unconfirmed")
            self.assertEqual(exit_code_for(res), 5)
            self.assertIn(("esc",), esc_sent)

    def test_models_live_probe(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        sel_btn = UINode(role="ButtonControl", name="Select model", rect=(400, 820, 200, 30))
        s1 = _snap([sel_btn])
        s2 = _snap([
            sel_btn,
            UINode(role="MenuItemControl", name="Hy4 flash 限时免费 0.00x", rect=(400, 640, 220, 30)),
            UINode(role="MenuItemControl", name="DeepSeek-V4.1-Flash 0.11x", rect=(400, 680, 220, 30)),
        ], snap_id="s2")
        snaps = [s1, s2]
        i = {"v": 0}
        esc_sent = []
        drv = EndpointDriver(
            "workbuddy",
            snapshot_fn=lambda _h: snaps[min(i.__setitem__("v", i["v"] + 1) or i["v"] - 1, 1)],
            window_fn=lambda: win,
            invoke_fn=lambda _n, _h: "stub:click",
            hotkey_fn=lambda *k: esc_sent.append(k),
            sleep_fn=lambda _s: None,
        )
        res = drv.models(live=True, headed=True)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["live_options"]), 2)
        self.assertEqual(res["live_options"][0]["tier"], "free")
        self.assertEqual(res["live_options"][1]["tier"], "paid")
        self.assertIn(("esc",), esc_sent)

    def test_gemini_model_option_ignores_history_listitem(self):
        cfg = get_endpoint_config("gemini")
        self.assertEqual(cfg["selectors"]["model_option"]["roles"], ["RadioButtonControl"])
        drv = EndpointDriver("gemini")
        s = _snap([
            UINode(role="ListItemControl", name="历史会话：切换 Hydrogen 测试", rect=(20, 100, 200, 30)),
            UINode(role="RadioButtonControl", name="日常帮助 4 Hydrogen", rect=(500, 600, 240, 40)),
        ])
        opt = drv._find_model_option(s, "hydrogen")
        self.assertIsNotNone(opt)
        self.assertEqual(opt.role, "RadioButtonControl")
        self.assertIn("Hydrogen", opt.name)

    def test_deepseek_extract_survives_bottom_composer_echo_and_nonleaf_text(self):
        drv = EndpointDriver("deepseek")
        lines = [
            "只回复：收到-ds-test",
            "已思考（用时 3 秒）",
            "收到-ds-test",
            "内容由 AI 生成",
            "只回复：收到-ds-test",  # e.g., composer echo at bottom of DOM
        ]
        cleaned = drv._extract_reply_lines(lines, prompt="只回复：收到-ds-test")
        self.assertEqual(cleaned, ["收到-ds-test"])

        # Non-leaf TextControl whose own name differs from child
        parent = UINode(role="TextControl", name="完整回复正文", children=[UINode(role="TextControl", name="子标签")])
        snap = _snap([parent])
        self.assertIn("完整回复正文", snap.text_lines())

    def test_grokbot_strips_timestamps_and_ui_chrome(self):
        drv = EndpointDriver("grokbot")
        lines = [
            "只回复：收到-v023-gb",
            "04:18",
            "收到-v023-gb",
            "昨天 19:30",
            "电脑",
            "Bot 的电脑",
            "语音聊天",
            "语言",
            "即将进行",
            "WorkBuddy每日领100积分",
        ]
        cleaned = drv._extract_reply_lines(lines, prompt="只回复：收到-v023-gb")
        self.assertEqual(cleaned, ["收到-v023-gb"])

    def test_hermes_composer_matches_xiaoxi_and_excludes_search(self):
        drv = EndpointDriver("hermes")
        s = _snap(
            [
                UINode(role="EditControl", name="搜索会话", rect=(-1400, 700, 200, 30)),
                UINode(role="EditControl", name="消息", rect=(-1122, 1500, 620, 30)),
            ],
            rect=(-1436, 622, 1400, 944),
        )
        comp = drv._find_composer(s)
        self.assertIsNotNone(comp)
        self.assertEqual(comp.name, "消息")


if __name__ == "__main__":
    unittest.main()
