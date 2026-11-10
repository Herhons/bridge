
from offline_guard import ensure_offline
ensure_offline()

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import replycodes as rc  # noqa: E402
from qi import workbuddy as wb  # noqa: E402


# ---------------------------------------------------------------- 1. 常量
class CodeConstantTests(unittest.TestCase):
    def test_codes_are_distinct(self):
        # B10 加了 REPLY_NEVER_APPEARED，所以不再钉死"恰好 4 个"。
        # 要守的是"互不重复、都是大写串、原四个还在"。
        self.assertGreaterEqual(len(rc.ALL_CODES), 4)
        self.assertEqual(len(set(rc.ALL_CODES)), len(rc.ALL_CODES))
        for code in rc.ALL_CODES:
            self.assertIsInstance(code, str)
            self.assertEqual(code, code.upper())
        for original in ("ATTRIBUTION_UNVERIFIED", "CONTAINER_BOUNDARY_UNPROVEN",
                         "STREAMING_IN_PROGRESS", "SESSION_ID_MISSING"):
            self.assertIn(original, rc.ALL_CODES)

    def test_only_streaming_is_waitable(self):
        """施工单 T3-1 第 2 条：只有"还在生成"允许继续等，其余必须立刻返回。"""
        self.assertEqual(rc.WAITABLE, frozenset({rc.STREAMING_IN_PROGRESS}))
        for code in rc.ALL_CODES:
            if code != rc.STREAMING_IN_PROGRESS:
                self.assertNotIn(code, rc.WAITABLE)

    def test_every_code_has_a_human_description(self):
        for code in rc.ALL_CODES:
            text = rc.describe(code)
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())
            self.assertNotEqual(text, "未知状态", f"{code} 缺描述")
        self.assertEqual(rc.describe("NOPE"), "未知状态")


# ---------------------------------------------------------------- 2. classify 真值表
class ClassifyTests(unittest.TestCase):
    def classify(self, reasons, **kw):
        kw.setdefault("busy", False)
        kw.setdefault("sent", True)
        return rc.classify_stall(reasons, **kw)

    def test_busy_wins_over_everything(self):
        """页面还在生成时，任何结构原因都不许判死。"""
        for reasons in (["loose_text"] * 5, ["unattributed"] * 5, []):
            with self.subTest(reasons=reasons):
                self.assertEqual(
                    self.classify(reasons, busy=True), rc.STREAMING_IN_PROGRESS)

    def test_too_few_readings_never_convicts(self):
        for n in range(rc.STALL_READS):
            with self.subTest(n=n):
                self.assertIsNone(self.classify(["loose_text"] * n))

    def test_three_identical_boundary_reasons_mean_unproven(self):
        for reason in ("loose_text", "multiple_messages", "page_scope"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.classify([reason] * rc.STALL_READS),
                    rc.CONTAINER_BOUNDARY_UNPROVEN)

    def test_three_identical_unattributed_means_unverified(self):
        self.assertEqual(
            self.classify(["unattributed"] * rc.STALL_READS),
            rc.ATTRIBUTION_UNVERIFIED)

    def test_mixed_reasons_never_convict(self):
        """页面在几种状态之间抖动时不许判死——那多半只是还在渲染。"""
        self.assertIsNone(self.classify(["loose_text", "no_text", "loose_text"]))
        self.assertIsNone(self.classify(["loose_text", "multiple_messages",
                                         "loose_text"]))

    def test_transient_reasons_never_convict(self):
        for reason in ("no_text", "no_copy", "no_group"):
            with self.subTest(reason=reason):
                self.assertIsNone(self.classify([reason] * 5))

    def test_only_the_tail_matters(self):
        """早期抖动不算数：只要最近三次一致就该判。"""
        self.assertEqual(
            self.classify(["no_text", "no_text", "loose_text", "loose_text",
                           "loose_text"]),
            rc.CONTAINER_BOUNDARY_UNPROVEN)

    def test_missing_session_id_is_only_reported_when_sent(self):
        self.assertEqual(
            self.classify(["no_session_id"] * 3, has_session_id=False),
            rc.SESSION_ID_MISSING)
        self.assertIsNone(
            self.classify(["no_session_id"] * 3, sent=False, has_session_id=False))

    def test_empty_reasons_is_none(self):
        self.assertIsNone(self.classify([]))
        self.assertIsNone(self.classify(None))


# ---------------------------------------------------------------- 3. 包装层不改形状
class WrapperShapeTests(unittest.TestCase):
    def test_reply_text_still_returns_a_bare_string(self):
        with mock.patch.object(wb, "reply_text_detail",
                               return_value=("hello", "bubble")):
            self.assertEqual(wb.reply_text(1, "p"), "hello")

    def test_reply_text_returns_none_on_refusal(self):
        with mock.patch.object(wb, "reply_text_detail",
                               return_value=(None, "loose_text")):
            self.assertIsNone(wb.reply_text(1, "p"))

    def test_last_reply_still_returns_a_pair(self):
        with mock.patch.object(wb, "last_reply_detail",
                               return_value=("t", "node", "bubble")):
            self.assertEqual(wb.last_reply([], "p"), ("t", "node"))

    def test_attributed_reply_still_returns_a_triple(self):
        view = SimpleNamespace(root=SimpleNamespace(walk=lambda: []))
        with mock.patch.object(wb, "attributed_reply_detail",
                               return_value=("t", "n", "prompt", "bubble")):
            self.assertEqual(wb.attributed_reply(view, "p"), ("t", "n", "prompt"))


# ---------------------------------------------------------------- 4. wait_reply 早退
class _FakeConv:
    """够 `wait_reply` 跑一圈的最小对象。"""

    def __init__(self, busy=False):
        self.busy = busy
        self.reads = 0

    def read(self, attempts=1, delay=0.0):
        self.reads += 1
        return SimpleNamespace(root=SimpleNamespace(walk=lambda: []),
                               busy=self.busy, url=None)


class EarlyExitTests(unittest.TestCase):
    def test_structural_stall_returns_within_seconds_not_at_timeout(self):
        """结构不可证时必须在几秒内交代，而不是烧完 10 秒预算。"""
        conv = _FakeConv()
        started = time.monotonic()
        with mock.patch.object(wb, "attributed_reply_detail",
                               return_value=(None, None, "unconfirmed", "loose_text")), \
                mock.patch.object(wb, "final_read", return_value=None):
            result = wb.wait_reply(conv, "p", timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(result["error_code"], rc.CONTAINER_BOUNDARY_UNPROVEN)
        self.assertEqual(result["timeout_kind"], "stalled")
        self.assertEqual(result["stage"], "timeout", "stage 名不许改，老调用方按它分支")
        self.assertLess(elapsed, 10, f"早退太慢：{elapsed:.1f}s（F5 要求 ≤10s）")

    def test_attribution_stall_also_exits_early(self):
        conv = _FakeConv()
        with mock.patch.object(wb, "attributed_reply_detail",
                               return_value=(None, None, "unconfirmed", "unattributed")), \
                mock.patch.object(wb, "final_read", return_value=None):
            result = wb.wait_reply(conv, "p", timeout=30)
        self.assertEqual(result["error_code"], rc.ATTRIBUTION_UNVERIFIED)

    def test_busy_page_never_exits_early(self):
        """停止键还在 = 真在生成。这时候判死是错的。"""
        conv = _FakeConv(busy=True)
        with mock.patch.object(wb, "final_read", return_value=None):
            result = wb.wait_reply(conv, "p", timeout=0.2)
        self.assertNotIn("error_code", result)
        self.assertEqual(result["stage"], "timeout")

    def test_transient_reads_do_not_exit_early(self):
        conv = _FakeConv()
        with mock.patch.object(wb, "attributed_reply_detail",
                               return_value=(None, None, "unconfirmed", "no_copy")), \
                mock.patch.object(wb, "final_read", return_value=None):
            result = wb.wait_reply(conv, "p", timeout=0.3)
        self.assertNotIn("error_code", result)

    def test_stall_carries_a_usable_hint(self):
        conv = _FakeConv()
        with mock.patch.object(wb, "attributed_reply_detail",
                               return_value=(None, None, "unconfirmed", "loose_text")), \
                mock.patch.object(wb, "final_read", return_value=None):
            result = wb.wait_reply(conv, "p", timeout=30)
        self.assertTrue(result.get("hint"))
        self.assertEqual(result.get("stall_reason"), "loose_text")
        self.assertEqual(result.get("stall_reads"), rc.STALL_READS)


# ---------------------------------------------------------------- 5. 静态接线
class WiringTests(unittest.TestCase):
    def test_workbuddy_imports_replycodes(self):
        source = (ROOT / "src" / "qi" / "workbuddy.py").read_text(encoding="utf-8")
        self.assertIn("replycodes", source)

    def test_all_four_codes_are_documented(self):
        doc = (ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8")
        for code in rc.ALL_CODES:
            with self.subTest(code=code):
                self.assertIn(code, doc, f"{code} 必须登记在 docs/exit-codes.md")


if __name__ == "__main__":
    unittest.main()
