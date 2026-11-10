"""B24：账本不许被"这一轮根本没发出去"的收尾降级。

现场（四端会审 2026-11-11，ChatGPT 第 1 条）：`conversation.ask()` 无条件调
`ledger.finish(result)`；`conversation_busy` 属于可重试阶段，而 `submissions.finish`
没有"不许降级"的保护 —— 于是一轮"页面正忙、根本没走到 reserve"的调用，
会把上一轮已经 accepted 的记录改写成 failed_retryable；`pending()` 不拦可重试态，
同一 (endpoint, prompt) 就能被再发一次。
"""
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

from qi import conversation as conv_mod            # noqa: E402
from qi import submissions as sub                  # noqa: E402


PROMPT = "B24 ledger fixture"
SITE = "codex"


def view(value=""):
    return SimpleNamespace(url="https://chatgpt.com/c/abc", value=value, busy=False,
                           root=SimpleNamespace(control=SimpleNamespace(
                               GetRuntimeId=lambda: [7, 9])))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "runs" / "submissions").mkdir(parents=True, exist_ok=True)

    def ledger(self):
        led = conv_mod._Ledger(SimpleNamespace(site=SITE), PROMPT, "review")
        led.root = self.root
        return led

    def seed(self, state, **extra):
        """直接写一条记录，模拟"上一轮留下的账"。"""
        path = sub.path_for(SITE, PROMPT, root=self.root)
        record = {"v": 2, "state": state, "v2_state": state, "level": None,
                  "submission_count": 0, "attempts": [], "at": 1.0, "updated_at": 1.0}
        record.update(extra)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def state(self):
        return (sub.read_state(SITE, PROMPT, root=self.root) or {}).get("v2_state")


class NoDowngradeTests(Base):
    def test_busy_without_reserve_leaves_accepted_alone(self):
        path = self.seed(sub.STATE_ACCEPTED, level="L3")
        before = path.read_bytes()

        self.ledger().finish({"stage": "conversation_busy", "sent": False,
                              "submission": "not_sent"})

        self.assertEqual(path.read_bytes(), before, "账本被没发出去的一轮改写了")
        self.assertEqual(self.state(), sub.STATE_ACCEPTED)
        self.assertIsNotNone(sub.pending(SITE, PROMPT, root=self.root),
                             "已接受的记录必须继续拦住重发")

    def test_draft_present_without_reserve_is_also_ignored(self):
        path = self.seed(sub.STATE_ACCEPTED, level="L3")
        before = path.read_bytes()
        self.ledger().finish({"stage": "draft_present", "sent": False,
                              "submission": "not_sent"})
        self.assertEqual(path.read_bytes(), before)

    def test_reserved_failure_still_becomes_retryable(self):
        """本轮真的预约过、真的失败了 —— 仍然要允许重试。"""
        led = self.ledger()
        self.assertIsNone(led.reserve(view()), "首次预约应放行")
        self.assertTrue(led.reserved)

        led.finish({"stage": "draft_present", "sent": False, "submission": "not_sent"})

        self.assertEqual(self.state(), sub.STATE_FAILED_RETRYABLE)
        self.assertIsNone(sub.pending(SITE, PROMPT, root=self.root),
                          "可重试态必须放行")

    def test_refusal_before_reserve_does_not_downgrade(self):
        """账本里已有 accepted 时，reserve 本身会被拦；这次拦发不该把状态改坏。"""
        self.seed(sub.STATE_ACCEPTED, level="L3")
        led = self.ledger()
        refused = led.reserve(view("写了字"))
        self.assertIsNotNone(refused, "已接受的记录必须拦发")
        self.assertFalse(led.reserved)
        led.finish(refused)
        self.assertEqual(self.state(), sub.STATE_ACCEPTED, "拦发不许改状态")
        self.assertIsNotNone(sub.pending(SITE, PROMPT, root=self.root))

    def test_submissions_finish_refuses_downgrade_of_accepted(self):
        path = self.seed(sub.STATE_ACCEPTED, level="L3")
        before = path.read_bytes()
        sub.finish(SITE, PROMPT, {"stage": "x", "submission": "not_sent", "sent": False},
                   root=self.root, scope={"doc": "should-not-be-written"})
        self.assertEqual(path.read_bytes(), before, "不许写状态，也不许写 scope_after")

    def test_submissions_finish_refuses_downgrade_of_unknown(self):
        self.seed(sub.STATE_UNKNOWN)
        sub.finish(SITE, PROMPT, {"stage": "x", "submission": "not_sent", "sent": False},
                   root=self.root)
        self.assertEqual(self.state(), sub.STATE_UNKNOWN)

    def test_submitting_still_downgrades(self):
        """旧态是 submitting（本轮刚预约）→ 允许变 failed_retryable。"""
        self.seed(sub.STATE_SUBMITTING)
        sub.finish(SITE, PROMPT, {"stage": "x", "submission": "not_sent", "sent": False},
                   root=self.root)
        self.assertEqual(self.state(), sub.STATE_FAILED_RETRYABLE)


class SubmissionCountTests(Base):
    def test_count_is_a_flag_not_a_retry_counter(self):
        """`submission_count` 是 0/1 的不变量，不是重试次数；重试历史在 attempts。"""
        sub.reserve(SITE, PROMPT, root=self.root)
        sub.finish(SITE, PROMPT, {"stage": "x", "submission": "not_sent", "sent": False},
                   root=self.root)
        sub.reserve(SITE, PROMPT, root=self.root)

        raw = json.loads(sub.path_for(SITE, PROMPT, root=self.root).read_text(encoding="utf-8"))
        self.assertEqual(len(raw["attempts"]), 2, "一次 submit + 一次 retry")
        self.assertEqual(raw["submission_count"], 1, "0/1 不变量：有过提交就是 1")


if __name__ == "__main__":
    unittest.main()
