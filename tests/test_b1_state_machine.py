
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qi import submissions as sub, tier_guard as tg, server  # noqa: E402

EP = "fixture-endpoint"
PROMPT = "fixture prompt for the ledger"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "runs" / "submissions").mkdir(parents=True, exist_ok=True)

    def path(self, prompt=PROMPT):
        return sub.path_for(EP, prompt, root=self.root)

    def seed(self, state, *, level=None, count=None, extra=None, prompt=PROMPT):
        """直接落一条记录（绕开 reserve 的状态推进），用于测 pending 真值表。"""
        record = sub._skeleton(state, now=1000.0)
        if level is not None:
            record["level"] = level
        if count is not None:
            record["submission_count"] = count
        if extra:
            record.update(extra)
        sub._write(self.path(prompt), record)
        return record

    def read_raw(self, prompt=PROMPT):
        return json.loads(self.path(prompt).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. 状态名自洽
class StateNameTests(unittest.TestCase):
    def test_every_v2_state_has_a_v1_compat_name(self):
        """磁盘上 `state` 必须永远能写出一个老代码认识的 v1 名。"""
        self.assertEqual(len(sub.STATES_V2), 6)
        for state in sub.STATES_V2:
            self.assertIn(state, sub._V1_NAMES)
            self.assertIsInstance(sub._V1_NAMES[state], str)

    def test_v1_names_are_the_four_legacy_ones_only(self):
        self.assertEqual(set(sub._V1_NAMES.values()),
                         {"not_sent", "unknown", "accepted"})

    def test_blocking_states_are_exactly_the_four_dangerous_ones(self):
        """draft 与 failed_retryable 放行，其余四个拦。"""
        self.assertEqual(sub.BLOCKING_STATES,
                         frozenset({"submitting", "accepted", "unknown", "failed_terminal"}))


# ---------------------------------------------------------------- 2. v1 归一
class NormalizeTests(Base):
    def normalize_v1(self, state):
        return sub.normalize({"state": state, "pid": 1, "at": 1000.0})

    def test_not_sent_becomes_failed_retryable_with_zero_count(self):
        view = self.normalize_v1("not_sent")
        self.assertEqual(view["v2_state"], sub.STATE_FAILED_RETRYABLE)
        self.assertEqual(view["subtype"], "not_sent")
        self.assertEqual(view["submission_count"], 0)
        self.assertTrue(view["legacy"])

    def test_unknown_stays_unknown_and_counts_as_possibly_sent(self):
        view = self.normalize_v1("unknown")
        self.assertEqual(view["v2_state"], sub.STATE_UNKNOWN)
        self.assertEqual(view["submission_count"], 1)

    def test_accepted_and_answered_both_become_accepted(self):
        for legacy in ("accepted", "answered"):
            with self.subTest(legacy=legacy):
                view = self.normalize_v1(legacy)
                self.assertEqual(view["v2_state"], sub.STATE_ACCEPTED)
                self.assertEqual(view["submission_count"], 1)
                self.assertEqual(view["state_v1"], legacy)

    def test_legacy_records_never_get_an_evidence_level(self):
        """**不补造落点**：老记录没有 basis，就不许推断它是 L2 还是 L3。"""
        for legacy in ("not_sent", "unknown", "accepted", "answered"):
            with self.subTest(legacy=legacy):
                view = self.normalize_v1(legacy)
                self.assertIsNone(view["level"])
                self.assertTrue(view["legacy"])

    def test_unknown_legacy_name_is_treated_as_unknown_not_guessed(self):
        view = self.normalize_v1("something_from_the_future")
        self.assertEqual(view["v2_state"], sub.STATE_UNKNOWN)
        self.assertIsNone(view["level"])

    def test_v2_record_passes_through_untouched(self):
        record = {"v": 2, "state": "accepted", "v2_state": "accepted",
                  "level": "L2", "submission_count": 1}
        view = sub.normalize(record)
        self.assertEqual(view["v2_state"], sub.STATE_ACCEPTED)
        self.assertEqual(view["level"], "L2")
        self.assertFalse(view["legacy"])

    def test_normalize_rejects_non_dict(self):
        for value in (None, [], "accepted", 3):
            self.assertIsNone(sub.normalize(value))


# ---------------------------------------------------------------- 3. 三条不变量
class InvariantTests(Base):
    def test_accepted_without_l2_or_l3_is_refused(self):
        """封死"后端 202 就当发送成功"：accepted 必须有 L2/L3 证据。"""
        for level in (None, "L1"):
            with self.subTest(level=level):
                record = sub._skeleton(sub.STATE_ACCEPTED, now=1000.0)
                record["level"] = level
                with self.assertRaises(tg.TierUnconfirmed) as caught:
                    sub._write(self.path(), record)
                self.assertEqual(caught.exception.reason, "accepted_without_evidence")

    def test_accepted_with_l2_or_l3_is_allowed(self):
        for level in ("L2", "L3"):
            with self.subTest(level=level):
                record = sub._skeleton(sub.STATE_ACCEPTED, now=1000.0)
                record["level"] = level
                sub._write(self.path(), record)
                self.assertTrue(self.path().is_file())

    def test_submission_count_must_match_state(self):
        record = sub._skeleton(sub.STATE_SUBMITTING, now=1000.0)
        record["submission_count"] = 0            # submitting 应为 1
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            sub._write(self.path(), record)
        self.assertEqual(caught.exception.reason, "submission_count_invariant")

    def test_two_submit_attempts_are_refused(self):
        """同一事务至多一条 submit；重试只能记 retry。"""
        record = sub._skeleton(sub.STATE_SUBMITTING, now=1000.0)
        record["attempts"] = [{"kind": "submit", "at": 1.0},
                              {"kind": "submit", "at": 2.0}]
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            sub._write(self.path(), record)
        self.assertEqual(caught.exception.reason, "duplicate_submit_attempt")

    def test_retry_attempts_are_allowed(self):
        record = sub._skeleton(sub.STATE_SUBMITTING, now=1000.0)
        record["attempts"] = [{"kind": "submit", "at": 1.0},
                              {"kind": "retry", "at": 2.0}]
        sub._write(self.path(), record)           # 不该抛
        self.assertEqual(len(self.read_raw()["attempts"]), 2)


# ---------------------------------------------------------------- 4. pending 真值表
class PendingTests(Base):
    def test_blocking_and_passing_states(self):
        blocking = {"submitting", "accepted", "unknown", "failed_terminal"}
        passing = {"draft", "failed_retryable"}
        for state in sorted(blocking):
            with self.subTest(state=state, expect="block"):
                prompt = f"block-{state}"
                self.seed(state, prompt=prompt,
                          level="L2" if state == "accepted" else None)
                self.assertIsNotNone(sub.pending(EP, prompt, root=self.root))
        for state in sorted(passing):
            with self.subTest(state=state, expect="pass"):
                prompt = f"pass-{state}"
                self.seed(state, prompt=prompt)
                self.assertIsNone(sub.pending(EP, prompt, root=self.root))

    def test_no_record_passes(self):
        self.assertIsNone(sub.pending(EP, PROMPT, root=self.root))

    def test_legacy_records_behave_exactly_like_v1(self):
        """等价性证明：老记录归一后，拦/放与 v1 完全一致。"""
        cases = {"not_sent": False, "unknown": True,
                 "accepted": True, "answered": True}
        for legacy, should_block in cases.items():
            with self.subTest(legacy=legacy):
                prompt = f"legacy-{legacy}"
                self.path(prompt).write_text(json.dumps(
                    {"state": legacy, "pid": 1, "at": 1000.0}), encoding="utf-8")
                got = sub.pending(EP, prompt, root=self.root)
                self.assertEqual(bool(got), should_block)

    def test_unreadable_journal_is_treated_as_unknown(self):
        self.path().write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(sub.pending(EP, PROMPT, root=self.root))


# ---------------------------------------------------------------- 5. reserve / begin
class ReserveTests(Base):
    def test_reserve_writes_submitting_and_occupies_one_submission(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "submitting")
        self.assertEqual(raw["state"], "unknown")          # v1 兼容名
        self.assertEqual(raw["submission_count"], 1)
        self.assertEqual(len(raw["attempts"]), 1)
        self.assertEqual(raw["attempts"][0]["kind"], "submit")

    def test_second_reserve_is_refused(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            sub.reserve(EP, PROMPT, root=self.root, now=1001.0)
        self.assertEqual(caught.exception.reason, "submission_pending")

    def test_reserve_after_retryable_is_allowed(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.fail(EP, PROMPT, state=sub.STATE_FAILED_RETRYABLE,
                 subtype="input_mismatch", root=self.root, now=1001.0)
        sub.reserve(EP, PROMPT, root=self.root, now=1002.0)   # 不该抛
        self.assertEqual(self.read_raw()["v2_state"], "submitting")

    def test_reserve_with_job_writes_l1_evidence(self):
        sub.reserve(EP, PROMPT, root=self.root, job_id="job-1",
                    request_id="req-1", now=1000.0)
        raw = self.read_raw()
        self.assertEqual(raw["level"], "L1")
        self.assertEqual(raw["evidence"]["l1"]["job_id"], "job-1")
        self.assertEqual(raw["evidence"]["l1"]["source"], "caller")

    def test_begin_is_the_same_gate_as_reserve(self):
        sub.begin(EP, PROMPT, root=self.root, now=1000.0)
        self.assertEqual(self.read_raw()["v2_state"], "submitting")
        with self.assertRaises(tg.TierUnconfirmed):
            sub.begin(EP, PROMPT, root=self.root, now=1001.0)


# ---------------------------------------------------------------- 6. mark_level
class MarkLevelTests(Base):
    def test_raising_to_l2_confirms_the_submission(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.mark_level(EP, PROMPT, "L2", {"basis": "prompt_bubble"},
                       root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "accepted")
        self.assertEqual(raw["state"], "accepted")
        self.assertEqual(raw["level"], "L2")
        self.assertEqual(raw["evidence"]["l2"]["basis"], "prompt_bubble")

    def test_level_never_goes_backwards(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.mark_level(EP, PROMPT, "L3", {"basis": "container"},
                       root=self.root, now=1001.0)
        sub.mark_level(EP, PROMPT, "L2", {"basis": "late"},
                       root=self.root, now=1002.0)
        self.assertEqual(self.read_raw()["level"], "L3")

    def test_mark_level_never_creates_a_record(self):
        """没提交过就不该因为读到一个回答而凭空记账。"""
        sub.mark_level(EP, PROMPT, "L3", {"basis": "x"}, root=self.root, now=1000.0)
        self.assertFalse(self.path().is_file())


# ---------------------------------------------------------------- 7. fail
class FailTests(Base):
    def test_retryable_frees_the_slot(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.fail(EP, PROMPT, state=sub.STATE_FAILED_RETRYABLE,
                 subtype="draft_present", root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "failed_retryable")
        self.assertEqual(raw["state"], "not_sent")
        self.assertEqual(raw["submission_count"], 0)
        self.assertIsNone(sub.pending(EP, PROMPT, root=self.root))

    def test_terminal_keeps_blocking_forever(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.fail(EP, PROMPT, state=sub.STATE_FAILED_TERMINAL,
                 subtype="quota", root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "failed_terminal")
        self.assertEqual(raw["state"], "unknown")
        self.assertIsNotNone(sub.pending(EP, PROMPT, root=self.root))

    def test_fail_rejects_states_that_are_not_failures(self):
        for state in ("accepted", "submitting", "draft"):
            with self.subTest(state=state):
                with self.assertRaises(tg.TierUnconfirmed) as caught:
                    sub.fail(EP, PROMPT, state=state, root=self.root, now=1000.0)
                self.assertEqual(caught.exception.reason, "bad_fail_state")


# ---------------------------------------------------------------- 8. finish 映射
class FinishTests(Base):
    def test_answered_maps_to_accepted_plus_l3(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "answered", "chars": 42, "stable_reads": 2},
                   root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "accepted")
        self.assertEqual(raw["level"], "L3")
        self.assertEqual(raw["evidence"]["l3"]["chars"], 42)

    def test_already_answered_also_maps_to_l3(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "already_answered"}, root=self.root, now=1001.0)
        self.assertEqual(self.read_raw()["level"], "L3")

    def test_accepted_without_answer_maps_to_l2_only(self):
        """202 / submission=accepted 只是 L2，**不是** L3。"""
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "timeout", "submission": "accepted"},
                   root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "accepted")
        self.assertEqual(raw["level"], "L2")

    def test_not_sent_maps_to_retryable(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "input_mismatch", "submission": "not_sent",
                                "sent": False}, root=self.root, now=1001.0)
        raw = self.read_raw()
        self.assertEqual(raw["v2_state"], "failed_retryable")
        self.assertEqual(raw["submission_count"], 0)

    def test_anything_else_maps_to_unknown(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "interrupted"}, root=self.root, now=1001.0)
        self.assertEqual(self.read_raw()["v2_state"], "unknown")

    def test_finish_without_a_record_is_a_noop(self):
        sub.finish(EP, PROMPT, {"stage": "answered"}, root=self.root, now=1000.0)
        self.assertFalse(self.path().is_file())

    def test_scope_after_is_written_only_once(self):
        scope = {"site": "x", "hwnd": 11, "doc_rid": [1, 2], "session_id": "s-1"}
        sub.reserve(EP, PROMPT, root=self.root, scope=scope, now=1000.0)
        sub.finish(EP, PROMPT, {"stage": "answered"}, root=self.root,
                   scope={**scope, "session_id": "s-2"}, now=1001.0)
        self.assertEqual(self.read_raw()["scope_after"]["session_id"], "s-2")
        sub.finish(EP, PROMPT, {"stage": "answered"}, root=self.root,
                   scope={**scope, "session_id": "s-3"}, now=1002.0)
        self.assertEqual(self.read_raw()["scope_after"]["session_id"], "s-2")


# ---------------------------------------------------------------- 9. refusal
class RefusalTests(Base):
    def test_accepted_reports_accepted(self):
        got = sub.refusal({"state": "accepted"})
        self.assertEqual(got["submission"], "accepted")

    def test_answered_legacy_also_reports_accepted(self):
        self.assertEqual(sub.refusal({"state": "answered"})["submission"], "accepted")

    def test_unknown_and_submitting_report_unknown(self):
        """submitting 在 v1 里本来就写作 unknown，这里保持一致，不许悄悄升级措辞。"""
        for state in ("unknown", "not_sent"):
            with self.subTest(state=state):
                self.assertEqual(sub.refusal({"state": state})["submission"], "unknown")

    def test_refusal_shape_is_stable(self):
        got = sub.refusal({"state": "accepted"})
        self.assertEqual(set(got), {"ok", "stage", "sent", "submission", "reason", "hint"})
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "interrupted")


# ---------------------------------------------------------------- 10. 超时口径
class TimeoutCaliberTests(unittest.TestCase):
    def test_default_use_timeout_is_120(self):
        self.assertEqual(server.DEFAULT_CONFIG["use_timeout"], 120.0)

    def test_shipped_config_matches_the_default(self):
        shipped = json.loads(
            (ROOT / "docs" / "bridge_server.json").read_text(encoding="utf-8"))
        self.assertEqual(shipped["use_timeout"], 120.0)

    def test_120_governs_test_and_accept_lite_only(self):
        """高档会审本来就慢：review/accept 由 floor_timeout 抬到 1500（B12）。"""
        base = server.DEFAULT_CONFIG["use_timeout"]
        self.assertEqual(tg.floor_timeout("test", base), 120.0)
        self.assertEqual(tg.floor_timeout("accept_lite", base), 120.0)
        self.assertEqual(tg.floor_timeout("review", base), 1500.0)
        self.assertEqual(tg.floor_timeout("accept", base), 1500.0)


# ---------------------------------------------------------------- 11. 工具存在
class ToolTests(unittest.TestCase):
    def test_audit_and_clear_tools_exist_and_import(self):
        for name in ("submission_audit.py", "submission_clear.py"):
            path = ROOT / "tools" / name
            self.assertTrue(path.is_file(), f"{name} 缺失")
            self.assertIn("def main", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
