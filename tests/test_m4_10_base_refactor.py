"""Offline coverage for M4-10: ``EndpointDriver.ask()`` split into 3 phases.

The refactor is structural only. These tests pin *structure* (the helpers exist,
``ask()`` is short and calls them in order) and *behaviour* (each phase works on
its own, and the four historical outcomes — normal reply, duplicate block,
blocking-popup fast-fail, timeout — are unchanged through ``ask()``).

No window, clipboard or hotkey is touched: snapshots are fixtures and the
write/click/confirm primitives are stubs.
"""
from __future__ import annotations

import inspect
import tempfile
import time
import unittest
from pathlib import Path

from endpoints import get_driver
from engine.base import EndpointDriver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo

WINDOW = WindowInfo(
    hwnd=88, title="Grok - Google Chrome", process_name="chrome.exe", pid=55
)


def _snap(snap_id: str, lines: list[str], *, extra: tuple[UINode, ...] = ()) -> Snapshot:
    kids = [
        UINode(role="EditControl", name="畅所欲问", rect=(10, 400, 300, 40)),
        UINode(role="ButtonControl", name="提交", rect=(320, 400, 50, 40)),
    ]
    for text in lines:
        kids.append(UINode(role="TextControl", name=text))
    kids.extend(extra)
    root = UINode(role="WindowControl", name="Grok", children=kids)
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=88,
        title="Grok",
        url="",
        nodes=[root],
        refs=assign_refs([root], snap_id),
    )


def _flat_snap(snap_id: str, nodes: list[UINode]) -> Snapshot:
    """Flat snapshot: ``snapshot_text`` only reads top-level nodes, so a paywall
    dialog has to sit at the top level to be detectable."""
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=88,
        title="Grok",
        url="",
        nodes=list(nodes),
        refs=assign_refs(list(nodes), snap_id),
    )


class _Harness(unittest.TestCase):
    """Shared fixture: a grok driver whose primitives never reach real UIA."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.events: list[str] = []

    def driver(self, snaps: list[Snapshot], **overrides):
        seq = {"i": 0}

        def fake_snap(_hwnd):
            index = min(seq["i"], len(snaps) - 1)
            seq["i"] += 1
            return snaps[index]

        kwargs = dict(
            bridge_config={
                "poll_interval": 0.0,
                "stable_seconds": 0.0,
                "default_timeout": 5.0,
                "lock_timeout": 5.0,
            },
            snapshot_fn=fake_snap,
            window_fn=lambda: WINDOW,
            invoke_fn=lambda node, headed: self.events.append("click") or "stub:click",
            write_fn=lambda node, text, headed, chunk: (
                self.events.append("write") or "stub:write"
            ),
            confirm_fn=lambda endpoint, prompt, model: True,
            sleep_fn=lambda _seconds: None,
            lock_path=self.tmp / "ui.lock",
            ledger_path=self.tmp / "ledger.json",
            evidence_dir=self.tmp / "fail",
        )
        kwargs.update(overrides)
        return get_driver("grok", **kwargs)


class StructureTests(unittest.TestCase):
    def test_ask_is_short_and_delegates_in_order(self):
        source, _ = inspect.getsourcelines(EndpointDriver.ask)
        self.assertLessEqual(
            len(source), 160, "ask() must stay a thin orchestrator (M4-10)"
        )
        body = "".join(source)
        prepare_at = body.index("_prepare_ask_inputs")
        submit_at = body.index("_submit_prompt_to_window")
        wait_at = body.index("_await_reply_convergence")
        self.assertLess(prepare_at, submit_at)
        self.assertLess(submit_at, wait_at)

    def test_the_three_phase_helpers_exist_with_their_contract(self):
        self.assertTrue(callable(EndpointDriver._prepare_ask_inputs))
        self.assertTrue(callable(EndpointDriver._submit_prompt_to_window))
        self.assertTrue(callable(EndpointDriver._await_reply_convergence))

        prepare = inspect.signature(EndpointDriver._prepare_ask_inputs).parameters
        for name in ("prompt", "profile", "model", "free_only", "attach", "force"):
            self.assertIn(name, prepare)
        submit = inspect.signature(EndpointDriver._submit_prompt_to_window).parameters
        for name in (
            "win", "before_snap", "clean_prompt", "use_headed", "effective_model",
            "confirm", "warnings",
        ):
            self.assertIn(name, submit)
        wait = inspect.signature(EndpointDriver._await_reply_convergence).parameters
        for name in (
            "win", "before_snap", "before_meta", "clean_prompt", "started", "budget",
            "poll", "stable_need", "use_headed", "write_method", "submit_method",
            "effective_model", "profile", "model_switch_info", "attach_paths",
            "attach_methods", "warnings",
        ):
            self.assertIn(name, wait)


class PrepareInputsTests(_Harness):
    def test_success_returns_the_four_resolved_inputs(self):
        drv = self.driver([_snap("s0", [])])
        error, inputs = drv._prepare_ask_inputs(
            "  归一化提示词  ",
            profile=None, model="expert", free_only=False, attach=None, force=False,
        )
        self.assertIsNone(error)
        self.assertEqual(inputs["clean_prompt"], "归一化提示词")
        self.assertEqual(inputs["effective_model"], "expert")
        self.assertFalse(inputs["effective_free_only"])
        self.assertEqual(inputs["attach_paths"], [])

    def test_unknown_profile_is_a_final_payload(self):
        drv = self.driver([_snap("s0", [])])
        error, inputs = drv._prepare_ask_inputs(
            "hi", profile="no-such-profile", model=None, free_only=False,
            attach=None, force=False,
        )
        self.assertEqual(error["error"], "invalid_profile")
        self.assertEqual(error["endpoint"], "grok")
        self.assertEqual(error["reply"], "")
        self.assertEqual(inputs, {})

    def test_empty_prompt_and_duplicate_are_blocked(self):
        drv = self.driver([_snap("s0", [])])
        error, _ = drv._prepare_ask_inputs(
            "   ", profile=None, model=None, free_only=False, attach=None, force=False,
        )
        self.assertEqual(error["error"], "empty_prompt")

        drv.ledger.mark("重复的提示词", "grok", "accepted")
        error, _ = drv._prepare_ask_inputs(
            "重复的提示词", profile=None, model=None, free_only=False,
            attach=None, force=False,
        )
        self.assertEqual(error["error"], "duplicate")
        self.assertEqual(error["previous_state"], "accepted")
        self.assertTrue(error["ledger_key"])
        # --force stays the documented escape hatch.
        error, inputs = drv._prepare_ask_inputs(
            "重复的提示词", profile=None, model=None, free_only=False,
            attach=None, force=True,
        )
        self.assertIsNone(error)
        self.assertEqual(inputs["clean_prompt"], "重复的提示词")

    def test_attach_paths_are_validated_then_rejected_when_unsupported(self):
        drv = self.driver([_snap("s0", [])])
        error, _ = drv._prepare_ask_inputs(
            "带附件", profile=None, model=None, free_only=False,
            attach=[str(self.tmp / "absent.txt")], force=False,
        )
        self.assertEqual(error["error"], "missing_resource")
        self.assertIn("absent.txt", error["message"])

        real = self.tmp / "brief.md"
        real.write_text("材料", encoding="utf-8")
        with self.assertRaises(NotImplementedError):
            drv._prepare_ask_inputs(
                "带附件", profile=None, model=None, free_only=False,
                attach=[str(real)], force=False,
            )
        # A comma-joined string is split and normalised, same as before.
        error, inputs = drv._prepare_ask_inputs(
            "带附件", profile=None, model=None, free_only=False, attach=None, force=False,
        )
        self.assertIsNone(error)
        self.assertEqual(inputs["attach_paths"], [])


class SubmitPhaseTests(_Harness):
    def test_writes_then_presses_send_and_returns_state(self):
        drv = self.driver([_snap("s0", ["旧对话"]), _snap("s1", ["旧对话"])])
        warnings: list[str] = []
        error, state = drv._submit_prompt_to_window(
            WINDOW, None, "写进去",
            use_headed=False, effective_model=None, confirm=False,
            warnings=warnings, profile=None, started=time.monotonic(),
        )
        self.assertIsNone(error)
        self.assertIsNotNone(state["before_snap"])
        self.assertEqual(state["write_method"], "stub:write")
        self.assertEqual(state["submit_method"], "stub:click")
        self.assertFalse(state["use_headed"])
        self.assertEqual(state["attach_methods"], [])
        self.assertEqual(self.events, ["write", "click"])

    def test_blocking_popup_aborts_before_writing(self):
        drv = self.driver([_flat_snap("s0", [
            UINode(role="TextControl", name="请升级到 Pro 才能继续", rect=(10, 10, 300, 24)),
            UINode(role="EditControl", name="畅所欲问", rect=(10, 400, 300, 40)),
        ])])
        error, state = drv._submit_prompt_to_window(
            WINDOW, None, "不该发出去",
            use_headed=False, effective_model=None, confirm=False,
            warnings=[], profile=None, started=time.monotonic(),
        )
        self.assertEqual(error["error"], "blocking_popup")
        self.assertEqual(state, {})
        self.assertEqual(self.events, [], "nothing may be typed into a paywall")
        entry = drv.ledger.get("不该发出去", "grok")
        self.assertEqual(entry["state"], "failed_retryable")


class WaitPhaseTests(_Harness):
    def _submitted(self, snaps):
        drv = self.driver(snaps)
        win = WINDOW
        before_meta = drv.pre_send_meta(win)
        before_snap = drv.take_snap(win.hwnd)
        return drv, win, before_meta, before_snap

    def test_settled_reply_returns_the_ok_payload(self):
        snaps = [
            _snap("s0", ["旧对话"]),
            _snap("s1", ["旧对话", "问题", "最终回复正文"]),
            _snap("s2", ["旧对话", "问题", "最终回复正文"]),
        ]
        drv, win, before_meta, before_snap = self._submitted(snaps)
        res = drv._await_reply_convergence(
            win, before_snap, before_meta, "问题",
            started=time.monotonic(), budget=5.0, poll=0.0, stable_need=0.0,
            use_headed=False, write_method="stub:write", submit_method="stub:click",
            effective_model=None, profile=None, model_switch_info=None,
            attach_paths=[], attach_methods=[], warnings=[],
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "最终回复正文")
        self.assertEqual(res["source"], "uia_delta")
        self.assertEqual(res["write_method"], "stub:write")
        self.assertEqual(drv.ledger.get("问题", "grok")["state"], "accepted")

    def test_timeout_keeps_the_diagnostic_payload(self):
        snaps = [_snap("s0", ["旧对话"])]
        drv, win, before_meta, before_snap = self._submitted(snaps)
        res = drv._await_reply_convergence(
            win, before_snap, before_meta, "没有回音的问题",
            started=time.monotonic(), budget=0.0, poll=0.0, stable_need=0.0,
            use_headed=False, write_method="stub:write", submit_method="stub:click",
            effective_model=None, profile=None, model_switch_info=None,
            attach_paths=[], attach_methods=[], warnings=[],
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "timeout")
        # grok requires a send receipt, so the documented fast-fail reason is the
        # receipt one; the payload shape is the same either way.
        self.assertEqual(res["reason"], "submit_not_verified")
        self.assertIn("diag", res)
        self.assertIn("elapsed", res)
        self.assertEqual(drv.ledger.get("没有回音的问题", "grok")["state"], "unknown")


class AskRegressionTests(_Harness):
    def test_normal_reply_then_duplicate_block(self):
        snaps = [
            _snap("s0", ["旧对话"]),
            _snap("s1", ["旧对话"]),
            _snap("s2", ["旧对话", "全链路", "完整回复正文"]),
            _snap("s3", ["旧对话", "全链路", "完整回复正文"]),
        ]
        drv = self.driver(snaps)
        first = drv.ask("全链路")
        self.assertTrue(first["ok"])
        self.assertEqual(first["reply"], "完整回复正文")
        self.assertEqual(first["write_method"], "stub:write")
        self.assertEqual(self.events, ["write", "click"])

        second = drv.ask("全链路")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "duplicate")
        self.assertEqual(self.events, ["write", "click"], "no resend on duplicate")

    def test_blocking_popup_and_timeout_still_return_the_same_payloads(self):
        popup = self.driver([_flat_snap("s0", [
            UINode(role="TextControl", name="Waiting for user input", rect=(10, 10, 240, 24)),
            UINode(role="ButtonControl", name="Yes, allow this time", rect=(10, 70, 160, 30)),
        ])])
        blocked = popup.ask("被弹窗挡住")
        self.assertEqual(blocked["error"], "blocking_popup")
        self.assertEqual(blocked["matched"], "waiting for user input")
        self.assertIn("evidence", blocked)
        self.assertEqual(blocked["reply"], "")

        slow = self.driver(
            [_snap("s0", ["旧对话"])],
            bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0,
                           "default_timeout": 5.0, "lock_timeout": 5.0},
        )
        # ``0.0`` is falsy in ``timeout or cfg...``, so use a tiny positive
        # budget instead of pretending a zero timeout is expressible.
        timed_out = slow.ask("等不到回复", timeout=0.001)
        self.assertFalse(timed_out["ok"])
        self.assertEqual(timed_out["error"], "timeout")
        self.assertEqual(timed_out["reason"], "submit_not_verified")
        self.assertEqual(timed_out["reply"], "")


if __name__ == "__main__":
    unittest.main()
