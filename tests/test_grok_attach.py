"""Offline GrokBot --attach and executor-fallback coverage.

No real GUI, clipboard, file dialog, or bridge subprocess: UIA snapshots and
the #32770 primitive are mocked. Safe to run unattended on any machine.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _path in (ROOT, SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402
import engine.base as base  # noqa: E402
from engine.base import EndpointDriver  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402


def _snap(nodes: list[UINode], snap_id: str) -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id, hwnd=101, title="Grok Bot", url="", nodes=nodes,
        refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1200, 900),
    )


class TestGrokbotAttach(unittest.TestCase):
    """GrokBot uses the generic attach_files() + #32770 dialog primitive."""

    def _drive(self, paths: list[str], *, with_attach: bool):
        win = WindowInfo(hwnd=101, pid=4242, process_name="Grok Bot.exe", title="Grok Bot")
        # Rectangles chosen to sit inside the configured anchor_box_ratio
        # [0.06, 0.90, 0.22, 0.99] of a 1200x900 window.
        attach_btn = UINode(role="ButtonControl", name="输入框操作", rect=(100, 810, 28, 28))
        menu_item = UINode(role="MenuItemControl", name="添加附件", rect=(100, 740, 140, 30))
        composer = UINode(role="EditControl", name="提示词", rect=(160, 810, 462, 20))
        send_btn = UINode(role="ButtonControl", name="发送", rect=(900, 805, 60, 30))
        prompt = "请确认收到附件"
        echo = UINode(role="TextControl", name=prompt, rect=(160, 700, 300, 24))
        reply = UINode(role="TextControl", name="ATTACH-TOKEN-V025-OK", rect=(160, 640, 300, 24))

        initial = _snap([attach_btn, composer], "s1")
        menu = _snap([menu_item, composer], "s2")
        # The prompt echo must be a *new* line after send so GrokBot's
        # require_send_receipt gate can be satisfied by the snapshot delta.
        ready = _snap([composer], "s3")
        send = _snap([composer, send_btn], "s4")
        final = _snap([echo, reply, composer], "s5")
        no_attach_send = _snap([composer, send_btn], "n4")
        no_attach_final = _snap([echo, reply, composer], "n5")
        # GrokBot has no attach_ready_patterns, so attach_files() consumes one
        # fewer snapshot than WorkBuddy (no chip-readiness poll).
        snaps = (
            [initial, menu, ready, send, final, final]
            if with_attach else [initial, no_attach_send, no_attach_final,
                                 no_attach_final]
        )
        idx = {"i": 0}
        events: list[str] = []

        def fake_snap(_hwnd):
            snap = snaps[min(idx["i"], len(snaps) - 1)]
            idx["i"] += 1
            return snap

        def fake_invoke(node, _headed):
            events.append("click:" + node.name)
            return "stub:click"

        def fake_write(_node, text, _headed, _chunk):
            events.append("write:" + text)
            return "stub:write"

        with tempfile.TemporaryDirectory() as td:
            dialized: list[list[str]] = []

            def fake_dialog(p, **_kw):
                dialized.append(list(p))
                events.append("dialog:" + ",".join(p))
                return "#32770:stub"

            with mock.patch.object(base, "fill_windows_file_dialog", side_effect=fake_dialog), \
                    mock.patch.object(base, "find_existing_file_dialog", return_value=0):
                drv = EndpointDriver(
                    "grokbot",
                    snapshot_fn=fake_snap,
                    window_fn=lambda: win,
                    invoke_fn=fake_invoke,
                    write_fn=fake_write,
                    sleep_fn=lambda _s: None,
                    lock_path=Path(td) / "ui.lock",
                    ledger_path=Path(td) / "ledger.json",
                )
                drv.cfg["stable_seconds"] = 0.0
                kwargs = {"attach": paths} if with_attach else {}
                res = drv.ask(prompt, force=True, headed=False, **kwargs)

        return events, res, dialized

    def test_single_file_upload_precedes_write_and_send(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("ATTACH-TOKEN-V025-OK", encoding="utf-8")
            events, res, dialogs = self._drive([str(p)], with_attach=True)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reply"], "ATTACH-TOKEN-V025-OK")
        self.assertEqual(
            events[:5],
            ["click:输入框操作", "click:添加附件",
             "dialog:" + str(p), "write:请确认收到附件", "click:发送"],
        )
        self.assertEqual(dialogs, [[str(p)]])
        self.assertEqual(res["attach_methods"], ["#32770:stub"])
        self.assertEqual(res["attached_files"], [str(p)])

    def test_multiple_files_share_one_dialog_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for name in ("a.txt", "b.txt"):
                path = Path(td) / name
                path.write_text(name, encoding="utf-8")
                paths.append(str(path))
            events, res, dialogs = self._drive(paths, with_attach=True)

        self.assertTrue(res["ok"], res)
        self.assertEqual(dialogs, [paths])
        self.assertEqual(res["attached_files"], paths)
        self.assertLess(events.index("dialog:" + ",".join(paths)),
                        events.index("write:请确认收到附件"))

    def test_no_attach_has_zero_regression(self):
        events, res, dialogs = self._drive([], with_attach=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reply"], "ATTACH-TOKEN-V025-OK")
        self.assertNotIn("attach_methods", res)
        self.assertNotIn("attached_files", res)
        self.assertEqual(dialogs, [])
        self.assertEqual(events, ["write:请确认收到附件", "click:发送"])

    def test_config_exposes_attach_selectors_and_ratio(self):
        cfg = json.loads((ROOT / "config" / "endpoints.json").read_text(encoding="utf-8"))
        grok = cfg["endpoints"]["grokbot"]
        sel = grok["selectors"]
        self.assertTrue(sel["attach_button"]["name_contains"])
        self.assertTrue(sel["attach_menu_item"]["name_contains"])
        self.assertEqual(len(grok["fallback_attach_ratio"]), 2)


class TestExecutorFallback(unittest.TestCase):
    """auto_heal_loop falls back to desktop opencode exactly once."""

    def test_auto_falls_back_when_workbuddy_reply_is_empty(self):
        seen: list[list[str]] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            seen.append(list(cmd))
            if "workbuddy" in cmd:
                # Empty stdout -> parse_reply() yields "", which is the real
                # empty-reply signal exec/fallback logic reacts to.
                return 0, ""
            if "opencode" in cmd:
                return 0, json.dumps({"ok": True, "reply": "opencode done"})
            return 1, ""

        def fake_read_reply(endpoint, saved, timeout):
            if endpoint == "workbuddy":
                return ""
            return "opencode done"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", side_effect=fake_read_reply), \
                    mock.patch.object(ahl, "log") as logger:
                reply = ahl.call_executor("step: patch code", 3, 120, "auto")

            wb_task = (Path(tmp) / "workbuddy_task_round3.md").read_text(encoding="utf-8")
            oc_task = (Path(tmp) / "opencode_task_round3.md").read_text(encoding="utf-8")

        self.assertEqual(reply, "opencode done")
        self.assertEqual(len(seen), 2)
        self.assertIn("workbuddy", seen[0])
        self.assertIn("opencode", seen[1])
        self.assertIn(str(ahl.BRIDGE), seen[1])
        self.assertIn("ask", seen[1])
        self.assertIn("--headed", seen[1])
        for text in (wb_task, oc_task):
            self.assertTrue(text.startswith(ahl.scheduler_notice()))
            self.assertIn("opencode", text.splitlines()[0] + "".join(text.splitlines()[:3]))
            self.assertIn("step: patch code", text)
        self.assertTrue(any("executor_hit=opencode" in str(c) for c in logger.call_args_list))

    def test_auto_falls_back_on_nonzero_workbuddy_rc(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            calls.append(cmd[3] if len(cmd) > 3 else "")
            if "workbuddy" in cmd:
                return 124, "TIMEOUT"
            return 0, json.dumps({"ok": True, "reply": "fallback ok"})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step: patch code", 1, 120, "auto")
        self.assertEqual(reply, "fallback ok")
        self.assertEqual(calls, ["workbuddy", "opencode"])

    def test_explicit_workbuddy_does_not_fallback(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            calls.append(cmd[3] if len(cmd) > 3 else "")
            return 1, ""

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step", 2, 120, "workbuddy")
        self.assertEqual(reply, "")
        self.assertEqual(calls, ["workbuddy"])

    def test_explicit_opencode_is_forced_first(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            calls.append(cmd[3] if len(cmd) > 3 else "")
            return 0, json.dumps({"ok": True, "reply": "oc"})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step", 4, 120, "opencode")
        self.assertEqual(reply, "oc")
        self.assertEqual(calls, ["opencode"])

    def test_parser_defaults_to_auto(self):
        self.assertEqual(ahl.build_parser().parse_args([]).executor, "auto")


if __name__ == "__main__":
    unittest.main()
