
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/scripts")]
from qi import failure, gemini, restart, runlog, server, submissions, tier_guard as tg, ui, workbuddy as wb
import grok_use
from test_card07 import ANSWER, PROMPT, Clock, bubble, node, text
import test_card09f as card09f_fixtures
from test_card09a import make_root


class EntryRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = {"submitted": 0, "answer": False, "value": "", "reads": 0}
        self.screen = SimpleNamespace(target_hwnd=4242)   # R5：身份要有真句柄
        table = tg.load_tiers()
        table["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=["fixture-low"], review=["fixture-high"], accept=["fixture-high"])
        config = self.root / "tiers.json"
        config.write_text(json.dumps(table), encoding="utf-8")
        self.patchers = [mock.patch.object(tg, "ROOT", self.root),
                         mock.patch.object(tg, "TIERS_FILE", config),
                         mock.patch.object(runlog, "RUNS", self.root / "logs"),
                         mock.patch.object(failure, "FAIL_DIR", self.root / "fail"),
                         mock.patch.object(wb.guard, "guard", return_value=True),
                         mock.patch.object(wb, "open_app", return_value=self.screen),
                         mock.patch.object(wb, "controls", return_value=[node("ComboBoxControl", "Select model: fixture-low")]),
                         mock.patch.object(wb.sendgate, "page_stamp", return_value={"doc_rid": [1]}),
                         mock.patch.object(wb, "time", Clock())]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        owner = self

        class FixtureConversation:
            read_via = copy_note = None
            site = "workbuddy"
            prompt = PROMPT
            def __init__(self, *args):
                self.screen = owner.screen
            def read(self, **kwargs):
                state = owner.state
                if state["submitted"] and not state["answer"]:
                    state["reads"] += 1
                    if state["reads"] > 1:
                        raise ui.UiDocsLost("fixture after accepted", rounds=[])
                children = [node("ComboBoxControl", "Select model: fixture-low")]
                if state["submitted"]:
                    children.append(text(PROMPT))
                if state["answer"]:
                    answer, _ = bubble(ANSWER)
                    children.extend(answer.children)
                root = node("DocumentControl", children=children)
                root.control.GetRuntimeId = lambda: [1]      # R5：恢复要比对文档身份
                return SimpleNamespace(root=root, value=state["value"], busy=False,
                                       copies=[], url="app://fixture")
            def empty(self, view):
                return view.value == ""
            def write(self, view, prompt):
                owner.state["value"] = prompt
            def clear(self, view):
                owner.state["value"] = ""
            def submit(self, view):
                owner.state["submitted"] += 1
                owner.state["value"] = ""
            def copy(self, node):
                return ANSWER

        self.conv = FixtureConversation
        self.patchers.append(mock.patch.object(wb, "ReplyConversation", FixtureConversation))
        self.patchers[-1].start()
        self.addCleanup(self.patchers[-1].stop)

    def receipt(self):
        # R5：回执和"当前窗口"要绑同一个句柄，否则 identity 校验会（正确地）拒
        tg.write_receipt("workbuddy", target="fixture-low", readback="fixture-low",
                         root=self.root, hwnd=self.screen.target_hwnd)

    def main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wb.main([PROMPT, "--continue", "--purpose", "test"], sending=True)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_actual_entry_accepted_then_docs_lost_never_resubmits_without_session_id(self):
        """R7：中断后重跑，没有会话 id -> 不认已答、也不重发、不清草稿。"""
        self.receipt()
        with mock.patch.object(wb, "ask", wraps=wb.ask) as actual_ask, \
                mock.patch.object(tg, "gate", wraps=tg.gate) as actual_gate:
            code, first = self.main()
            self.assertEqual(code, 1, first)
            self.assertEqual(first["submission"], "accepted")
            self.assertEqual(self.state["submitted"], 1)
            self.assertTrue(tg.read_receipt("workbuddy", root=self.root)["consumed"])
            self.state.update(answer=True, value=PROMPT)
            prior_gates = actual_gate.call_count
            code, second = self.main()
            # 页面上就算已经答完，没有"与标题无关的会话 id"就不能采信（R7）。
            self.assertEqual(code, 1, second)
            self.assertEqual(second["submission"], "accepted")
            self.assertEqual(second["reason"], "submission_pending")
            self.assertEqual(second["identity"], "session_id_missing")
            self.assertEqual(self.state["value"], PROMPT, "没有结论就不许清草稿")
            self.assertEqual(self.state["submitted"], 1, "绝不重发")
            self.assertEqual(actual_ask.call_count, 1)
            self.assertEqual(actual_gate.call_count, prior_gates)

    def test_new_receipt_does_not_erase_unknown_attempt(self):
        submissions.reserve("workbuddy", PROMPT)
        self.receipt()
        with mock.patch.object(wb, "ask") as ask, mock.patch.object(tg, "gate") as gate:
            code, got = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(got["submission"], "unknown")
        self.assertEqual(got["reason"], "submission_pending")
        ask.assert_not_called()
        gate.assert_not_called()

    def test_uia_restart_path_cannot_resubmit_a_pending_attempt(self):
        submissions.reserve("workbuddy", PROMPT)
        calls = []
        def opening():
            calls.append(True)
            if len(calls) == 1:
                raise ui.UiDocsLost("before read", rounds=[])
            return self.screen
        with mock.patch.object(wb, "open_app", side_effect=opening), \
                mock.patch.object(restart, "restart", return_value={"ok": True, "docs": 1, "steps": []}) as restarter, \
                mock.patch.object(wb, "ask") as ask:
            code, got = self.main()
        self.assertEqual(code, 1, got)
        self.assertEqual(got["reason"], "submission_pending")
        ask.assert_not_called()
        restarter.assert_called_once()

    def test_corrupt_journal_is_pending_and_persistence_failure_refuses_before_submit(self):
        path = submissions.path_for("workbuddy", PROMPT)
        path.parent.mkdir(parents=True)
        path.write_text("broken", encoding="utf-8")
        self.assertEqual(submissions.pending("workbuddy", PROMPT)["state"], "unknown")
        with self.assertRaises(tg.TierUnconfirmed):
            submissions.reserve("workbuddy", PROMPT)
        path.unlink()
        with mock.patch.object(submissions, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(tg.TierUnconfirmed) as caught:
                submissions.reserve("workbuddy", PROMPT)
        self.assertEqual(caught.exception.reason, "submission_state_unwritable")

    def test_web_entry_refuses_prior_unknown_before_write_for_both_endpoints(self):
        for module, endpoint in ((gemini, "gemini"), (grok_use, "grok")):
            with self.subTest(endpoint=endpoint):
                submissions.reserve(endpoint, PROMPT)
                conv = mock.Mock()
                conv.read.return_value = SimpleNamespace(root=node("DocumentControl"), busy=False)
                with mock.patch.object(module, "Conversation", return_value=conv), \
                        mock.patch.object(module, "write_prompt") as write, \
                        mock.patch.object(module, "_submit") as submit:
                    got = module._send_and_wait(None, SimpleNamespace(name=endpoint), PROMPT, 1, purpose="test")
                self.assertEqual(got["reason"], "submission_pending")
                write.assert_not_called()
                submit.assert_not_called()

    def test_accepted_wait_clears_refilled_draft_without_a_second_submit(self):
        builder = card09f_fixtures.WebResendGuard()
        for module, endpoint in ((gemini, "gemini"), (grok_use, "grok")):
            with self.subTest(endpoint=endpoint):
                state = {"submitted": False, "written": False, "refill": True}
                class Conv:
                    def read(self, **kwargs):
                        if state["submitted"]:
                            method = builder._gemini_tree if endpoint == "gemini" else builder._grok_tree
                            root = method(prompt=PROMPT, reply=ANSWER, with_send=state["refill"])
                        else:
                            root = node("DocumentControl")
                        return SimpleNamespace(root=root, busy=False, value=PROMPT if state["written"] else "")
                    def write(self, view, prompt):
                        state["written"] = True
                    def clear(self, view):
                        state["refill"] = False
                        state["written"] = False
                conv = Conv()
                with mock.patch.object(module, "Conversation", return_value=conv), \
                        mock.patch.object(module, "time", Clock()), \
                        mock.patch.object(module, "_submit", side_effect=lambda *a: state.update(submitted=True)) as submit:
                    got = module._send_and_wait(mock.Mock(), SimpleNamespace(name=endpoint), PROMPT, 4)
                self.assertFalse(state["refill"])
                submit.assert_called_once()
                self.assertEqual(got["submission"], "accepted", got)

    def test_web_clear_requires_empty_value_not_only_disappearing_send_button(self):
        for module in (gemini, grok_use):
            conv = mock.Mock()
            conv.read.return_value = SimpleNamespace(root=node("DocumentControl"), value=PROMPT)
            self.assertFalse(module._clear_draft(conv, None))



class OuterBudgets(unittest.TestCase):
    def test_real_server_use_command_and_runner_both_cover_the_review_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            captured = []
            def runner(cmd, **kwargs):
                captured.append((cmd, kwargs["timeout"]))
                return {"exit_code": 0, "timeout": False, "stdout": '{"ok":true,"stage":"answered","reply":"fixture"}'}
            engine = server.Engine(root=root, token="fixture", port=0, runner=runner)
            # B12：review 的下限从 900 抬到 1500，外层要 ≥1500+30
            got = engine._run_use("grok", "fixture", "review", time.monotonic() + 1600)
            self.assertTrue(got["ok"], got)
            cmd, outer = captured[0]
            self.assertGreaterEqual(float(cmd[cmd.index("--timeout") + 1]), 1500)
            self.assertGreaterEqual(outer, 1530)
            captured.clear()
            got = engine._run_use("grok", "fixture", "review", time.monotonic() + 180)
            self.assertFalse(got["ok"])
            self.assertFalse(captured)


if __name__ == "__main__":
    unittest.main()
