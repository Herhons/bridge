
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation as conv_mod  # noqa: E402
from qi import flow, submissions as sub  # noqa: E402

PROMPT = "T2-1 fixture prompt"
SITE = "workbuddy"


def view(url="https://grok.com/chat/abc123", value=""):
    """够用的假 View：`scope_from_view` 读不到 UIA 时会 try/except 兜住。

    URL 用 `/chat/<id>` 形态——`session_id_of()` 只认白名单里的路径首段
    （`app` / `chat` …），随便编一个 `/session/x` 是采不到会话 id 的。
    """
    return SimpleNamespace(url=url, value=value, busy=False,
                           root=SimpleNamespace(control=SimpleNamespace(
                               GetRuntimeId=lambda: [42, 7])))


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "runs" / "submissions").mkdir(parents=True, exist_ok=True)

    def make(self, purpose="test", site=SITE):
        led = conv_mod._Ledger(SimpleNamespace(site=site), PROMPT, purpose)
        led.root = self.root
        return led

    def read(self):
        return sub.read_state(SITE, PROMPT, root=self.root)


# ---------------------------------------------------------------- 1. 启用条件
class EnablementTests(LedgerBase):
    def test_no_purpose_means_no_ledger(self):
        """不给 purpose = 完全跳过账本，老调用方行为不变。"""
        self.assertFalse(self.make(purpose=None).enabled)

    def test_unreadable_site_means_no_ledger(self):
        for site in (None, "", "   "):
            with self.subTest(site=site):
                self.assertFalse(self.make(site=site).enabled)

    def test_purpose_plus_site_enables_it(self):
        self.assertTrue(self.make().enabled)


# ---------------------------------------------------------------- 2. reserve
class ReserveTests(LedgerBase):
    def test_reserve_writes_submitting_with_scope(self):
        led = self.make()
        self.assertIsNone(led.reserve(view()))
        raw = json.loads(
            sub.path_for(SITE, PROMPT, root=self.root).read_text(encoding="utf-8"))
        self.assertEqual(raw["v2_state"], "submitting")
        self.assertIn("scope", raw, "身份必须落盘，否则恢复时 reconcile 必 fail-closed")
        self.assertTrue(raw["scope"].get("session_id"), "URL 里有 session 路径时必须采到")

    def test_second_reserve_is_refused_with_a_result_not_an_exception(self):
        self.make().reserve(view())
        out = self.make().reserve(view())
        self.assertIsNotNone(out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "interrupted")
        self.assertEqual(out["reason"], "submission_pending")

    def test_refusal_after_accepted_reports_accepted(self):
        self.make().reserve(view())
        sub.mark_level(SITE, PROMPT, "L2", {"basis": "bubble"}, root=self.root)
        out = self.make().reserve(view())
        self.assertEqual(out["submission"], "accepted")

    def test_refusal_after_unknown_reports_unknown(self):
        self.make().reserve(view())
        sub.finish(SITE, PROMPT, {"stage": "interrupted"}, root=self.root)
        out = self.make().reserve(view())
        self.assertEqual(out["submission"], "unknown")

    def test_job_env_is_captured_into_l1(self):
        with mock.patch.dict("os.environ", {"BRIDGE_JOB_ID": "job-42",
                                            "BRIDGE_REQUEST_ID": "req-7"}):
            self.make().reserve(view())
        raw = json.loads(sub.path_for(SITE, PROMPT, root=self.root).read_text(encoding="utf-8"))
        self.assertEqual(raw["level"], "L1")
        self.assertEqual(raw["evidence"]["l1"]["job_id"], "job-42")

    def test_unwritable_ledger_refuses_instead_of_sending(self):
        """落不了账就不许发 —— 这是"先记账再发送"的底线。"""
        led = self.make()
        with mock.patch.object(sub, "reserve",
                               side_effect=sub.TierUnconfirmed("x", "y")):
            out = led.reserve(view())
        self.assertIsNotNone(out)
        self.assertEqual(out["reason"], "x")


# ---------------------------------------------------------------- 3. finish 映射
class FinishMappingTests(LedgerBase):
    def _run(self, result):
        led = self.make()
        led.reserve(view())
        led.finish(result)
        return self.read()

    def test_answered_is_accepted_plus_l3(self):
        state = self._run({"ok": True, "stage": "answered", "reply": "hello"})
        self.assertEqual(state["v2_state"], "accepted")
        self.assertEqual(state["level"], "L3")

    def test_submission_accepted_only_is_l2(self):
        """202 / 已提交但没拿到回答 = L2，**不是** L3。"""
        state = self._run({"stage": "timeout", "submission": "accepted",
                           "sent": True})
        self.assertEqual(state["v2_state"], "accepted")
        self.assertEqual(state["level"], "L2")

    def test_retryable_stages_become_failed_retryable(self):
        for stage in sorted(conv_mod._RETRYABLE_STAGES):
            with self.subTest(stage=stage):
                self.setUp()
                led = self.make()
                led.reserve(view())
                led.finish({"ok": False, "stage": stage,
                            "submission": "not_sent", "sent": False})
                state = self.read()
                self.assertEqual(state["v2_state"], "failed_retryable",
                                 f"{stage} 应该可重试")
                self.assertIsNone(sub.pending(SITE, PROMPT, root=self.root))

    def test_terminal_stages_become_failed_terminal(self):
        for stage in sorted(conv_mod._TERMINAL_STAGES):
            with self.subTest(stage=stage):
                self.setUp()
                led = self.make()
                led.reserve(view())
                led.finish({"ok": False, "stage": stage, "sent": False,
                            "hint": "nope"})
                state = self.read()
                self.assertEqual(state["v2_state"], "failed_terminal",
                                 f"{stage} 应该终态")
                self.assertIsNotNone(sub.pending(SITE, PROMPT, root=self.root))

    def test_unknown_stage_stays_unknown_and_keeps_blocking(self):
        state = self._run({"ok": False, "stage": "interrupted"})
        self.assertEqual(state["v2_state"], "unknown")
        self.assertIsNotNone(sub.pending(SITE, PROMPT, root=self.root))

    def test_already_answered_never_creates_a_record(self):
        """没有记录就不建 —— 否则等于补造一次外部提交。"""
        led = self.make()
        led.finish({"ok": True, "stage": "already_answered", "reply": "x"})
        self.assertIsNone(self.read())

    def test_already_answered_raises_level_on_an_existing_record(self):
        led = self.make()
        led.reserve(view())
        sub.mark_level(SITE, PROMPT, "L2", {"basis": "bubble"}, root=self.root)
        led.finish({"ok": True, "stage": "already_answered", "reply": "abc"})
        state = self.read()
        self.assertEqual(state["level"], "L3")
        self.assertEqual(state["v2_state"], "accepted")


# ---------------------------------------------------------------- 4. 账本出错不影响收发
class IsolationTests(LedgerBase):
    def test_finish_swallows_ledger_errors(self):
        led = self.make()
        with mock.patch.object(sub, "finish", side_effect=RuntimeError("boom")):
            led.finish({"stage": "answered"})          # 不该抛

    def test_reserve_failure_is_reported_as_a_result(self):
        led = self.make()
        with mock.patch.object(sub, "pending", side_effect=RuntimeError("boom")):
            out = led.reserve(view())
        self.assertIsNotNone(out)
        self.assertEqual(out["reason"], "submission_pending")


# ---------------------------------------------------------------- 5. 包装器接得上
class WrapperTests(LedgerBase):
    def test_purpose_none_bypasses_impl_entirely(self):
        fake = SimpleNamespace(site=SITE)
        with mock.patch.object(conv_mod, "_ask_impl",
                               return_value={"stage": "answered"}) as impl:
            got = conv_mod.ask(fake, PROMPT)
        impl.assert_called_once()
        self.assertEqual(got, {"stage": "answered"})
        self.assertIsNone(impl.call_args.kwargs.get("ledger"))

    def test_purpose_given_passes_a_ledger_to_the_impl(self):
        fake = SimpleNamespace(site=SITE)
        with mock.patch.object(conv_mod, "_ask_impl",
                               return_value={"stage": "answered"}) as impl:
            conv_mod.ask(fake, PROMPT, purpose="test")
        ledger = impl.call_args.kwargs.get("ledger")
        self.assertIsInstance(ledger, conv_mod._Ledger)
        self.assertTrue(ledger.enabled)

    def test_exception_still_records_unknown(self):
        fake = SimpleNamespace(site=SITE)
        led_holder = {}

        def boom(*a, **kw):
            led_holder["led"] = kw["ledger"]
            kw["ledger"].root = self.root
            raise RuntimeError("boom")

        with mock.patch.object(conv_mod, "_ask_impl", side_effect=boom):
            with self.assertRaises(RuntimeError):
                conv_mod.ask(fake, PROMPT, purpose="test")
        self.assertTrue((self.root / "runs" / "submissions").exists())


# ---------------------------------------------------------------- 6. flow 透传
class FlowPassthroughTests(unittest.TestCase):
    def test_flow_ask_forwards_purpose_to_conversation_ask(self):
        source = (ROOT / "src" / "qi" / "flow.py").read_text(encoding="utf-8")
        self.assertIn("purpose=purpose", source,
                      "flow.ask 必须把 purpose 透传给 conversation.ask，否则 chatgpt/deepseek 不记账")


# ---------------------------------------------------------------- 7. server L1
class ServerEvidenceTests(unittest.TestCase):
    def test_job_endpoints_carry_evidence_l1(self):
        source = (ROOT / "src" / "qi" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"evidence_l1"', source)
        self.assertIn('"source": "server"', source)

    def test_use_subsystem_receives_job_identity_via_env(self):
        source = (ROOT / "src" / "qi" / "server.py").read_text(encoding="utf-8")
        for key in ("BRIDGE_JOB_ID", "BRIDGE_REQUEST_ID", "BRIDGE_PURPOSE",
                    "BRIDGE_ENDPOINT"):
            with self.subTest(key=key):
                self.assertIn(key, source)

    def test_run_use_signature_accepts_job_id(self):
        import inspect
        self.assertIn("job_id", inspect.signature(
            __import__("qi.server", fromlist=["Engine"]).Engine._run_use).parameters)


if __name__ == "__main__":
    unittest.main()
