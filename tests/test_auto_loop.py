"""Unit coverage for the deterministic overnight runner (scripts/auto_loop.py)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import auto_loop  # noqa: E402


class TestInstructionParsing(unittest.TestCase):
    def test_full_block(self):
        text = (
            "DECISION: DISPATCH_OPENCODE\n"
            "GEMINI_PROFILE_NEXT: routine\n"
            "TASK_ID: M1-attach\n"
            "PAYLOAD: 请实现通用 --attach 原语\n"
        )
        got = auto_loop.parse_instruction(text)
        self.assertEqual(got["decision"], "DISPATCH_OPENCODE")
        self.assertEqual(got["profile_next"], "routine")
        self.assertEqual(got["task_id"], "M1-attach")
        self.assertEqual(got["payload"], "请实现通用 --attach 原语")

    def test_payload_is_multiline_and_stops_at_next_key(self):
        text = (
            "DECISION: APPLY_PATCH\n"
            "TASK_ID: M1\n"
            "PAYLOAD:\n"
            "diff --git a/x.py b/x.py\n"
            "+print(1)\n"
            "GEMINI_PROFILE_NEXT: test\n"
        )
        got = auto_loop.parse_instruction(text)
        self.assertIn("diff --git", got["payload"])
        self.assertIn("+print(1)", got["payload"])
        self.assertNotIn("GEMINI_PROFILE_NEXT", got["payload"])
        self.assertEqual(got["profile_next"], "test")

    def test_unknown_values_fall_back(self):
        got = auto_loop.parse_instruction("DECISION: NOPE\nGEMINI_PROFILE_NEXT: ultra\n")
        self.assertEqual(got["decision"], "STOP_FOR_USER")
        self.assertEqual(got["profile_next"], "test")


class TestQuotaAndFingerprint(unittest.TestCase):
    def test_quota_detection_both_languages(self):
        self.assertTrue(auto_loop.is_quota_error("HTTP 429 too many requests"))
        self.assertTrue(auto_loop.is_quota_error("余额不足，请充值"))
        self.assertFalse(auto_loop.is_quota_error("all tests passed"))

    def test_fingerprint_ignores_numbers_and_case(self):
        a = "Error at line 12: Quota Exceeded for user 42"
        b = "ERROR at line 99: quota exceeded for user 7"
        self.assertEqual(auto_loop.fingerprint(a), auto_loop.fingerprint(b))
        self.assertNotEqual(auto_loop.fingerprint(a), auto_loop.fingerprint("other"))

    def test_cooldown_and_pick_executor(self):
        state = {
            "opencode": {"cooldown_until": auto_loop.next_midnight()},
            "workbuddy": {"cooldown_until": None},
        }
        self.assertTrue(auto_loop.is_cooled(state["opencode"]))
        self.assertEqual(auto_loop.pick_executor(state), "workbuddy")
        state["workbuddy"]["cooldown_until"] = auto_loop.next_midnight()
        self.assertIsNone(auto_loop.pick_executor(state))

    def test_two_same_errors_mark_stalled_and_escalate(self):
        state = {
            "opencode": {"streak": 0, "last_error_fp": None,
                         "failures_today": 0, "cooldown_until": None,
                         "stalled": False},
            "meta": {"force_council": False},
        }
        auto_loop.record_failure(state, "opencode", "quota 429 error")
        auto_loop.record_failure(state, "opencode", "quota 429 error")
        entry = state["opencode"]
        self.assertEqual(entry["streak"], 2)
        self.assertTrue(entry["stalled"])
        self.assertTrue(state["meta"]["force_council"])
        self.assertTrue(auto_loop.is_cooled(entry))


class TestArchitectureGate(unittest.TestCase):
    def test_clean_baseline(self):
        self.assertEqual(auto_loop.architecture_gate(), [])

    def test_detects_exec_in_bridge_and_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bridge.py").write_text("x = exec('1')\n", encoding="utf-8")
            engine = root / "engine"
            engine.mkdir()
            (engine / "uia.py").write_text(
                "if self.name == 'x':\n    pass\n", encoding="utf-8"
            )
            bad = auto_loop.architecture_gate(root)
            self.assertTrue(any("bridge.py" in item and "exec" in item for item in bad))
            self.assertTrue(any("uia.py" in item and "self.name" in item for item in bad))

    def test_execute_is_not_false_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bridge.py").write_text("conn.execute(sql)\n", encoding="utf-8")
            self.assertEqual(auto_loop.architecture_gate(root), [])


class TestRollbackAndSafety(unittest.TestCase):
    def test_rollback_calls_exact_reset(self):
        with mock.patch.object(auto_loop, "git") as fake:
            fake.return_value = mock.Mock(returncode=0, stderr="")
            auto_loop.rollback("0123456789abcdef")
            fake.assert_called_once_with("reset", "--hard", "0123456789abcdef")

    def test_rollback_refuses_bad_commit(self):
        with mock.patch.object(auto_loop, "git") as fake:
            msg = auto_loop.rollback("main")
            self.assertIn("refused", msg)
            fake.assert_not_called()

    def test_executor_and_council_are_forced_to_test_profile(self):
        with mock.patch.object(auto_loop, "call_bridge") as fake:
            fake.return_value = {"ok": True}
            auto_loop.call_executor("opencode", "hello", Path("runs"))
            self.assertEqual(fake.call_args.kwargs["profile"], "test")
            auto_loop.call_council("grok", "hello", Path("runs"))
            self.assertEqual(fake.call_args.kwargs["profile"], "test")
            self.assertIn("--new", fake.call_args.kwargs["extra"])

    def test_patch_extraction(self):
        payload = (
            "some prose\n"
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        )
        patch = auto_loop.extract_patch(payload)
        self.assertEqual(patch.count("diff --git "), 2)
        self.assertNotIn("some prose", patch)

    def test_cap_text(self):
        self.assertEqual(len(auto_loop.cap_text("x" * 900)), 600)
        self.assertEqual(auto_loop.cap_text("short"), "short")


class TestOvernightFixes(unittest.TestCase):
    def test_empty_reply_uses_named_fingerprint(self):
        state = {
            "workbuddy": {"streak": 0, "last_error_fp": None,
                          "failures_today": 0, "cooldown_until": None,
                          "stalled": False},
            "meta": {"force_council": False},
        }
        action = auto_loop.record_failure(state, "workbuddy", "")
        self.assertIn("workbuddy:empty_reply", action)
        self.assertEqual(state["workbuddy"]["last_error_fp"], "workbuddy:empty_reply")
        self.assertNotEqual(auto_loop.fingerprint(""), "workbuddy:empty_reply")

    def test_round_two_prompt_uses_last_round_state_not_kickoff(self):
        state = {"meta": {
            "last_round": 1, "last_task_id": "T2-ATTACH",
            "last_decision": "DISPATCH_WORKBUDDY", "last_executor": "workbuddy",
            "last_rc": 6, "last_error_tail": "error: quota 429\nexit=6",
        }}
        kickoff = "【过夜自迭代启动｜TASK=V025-OVERNIGHT-START】"
        prompt = auto_loop.build_prompt(kickoff, state, 2, "clean")
        self.assertNotIn("V025-OVERNIGHT-START", prompt)
        for token in ("last_task_id=T2-ATTACH", "last_decision=DISPATCH_WORKBUDDY",
                      "last_executor=workbuddy", "last_rc=6", "quota 429"):
            self.assertIn(token, prompt)


if __name__ == "__main__":
    unittest.main()
