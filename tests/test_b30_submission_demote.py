"""B30：账本 `accepted → unknown` 的降级。

会审三方一致（Round 3）：页面事实与账本不符时允许降级，但必须**只降一级、只降一次、
留审计**，且不得自动放行重发（unknown 仍拦截）。
"""
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import submissions as sub                 # noqa: E402

EP = "gemini"
PROMPT = "BRIDGE-B30-DEMOTE"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make_accepted(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)
        sub.mark_level(EP, PROMPT, "L2", {"basis": "prompt_bubble"},
                       root=self.root, now=1001.0)

    def raw(self):
        return json.loads(sub.path_for(EP, PROMPT, root=self.root)
                          .read_text(encoding="utf-8"))


class DemoteTests(Base):
    def test_accepted_becomes_unknown_and_keeps_evidence(self):
        self.make_accepted()
        out = sub.demote(EP, PROMPT, reason="页面 /app、0 条消息",
                         evidence={"page_empty": True, "scope_mismatch": True},
                         root=self.root, now=2000.0)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["blocking"], "降级后仍必须拦截重发")
        raw = self.raw()
        self.assertEqual(raw["v2_state"], "unknown")
        self.assertEqual(raw["state"], "unknown")
        self.assertEqual(raw["level"], "L2", "原证据不能丢")
        self.assertEqual(len(raw["demotions"]), 1)
        self.assertEqual(raw["demotions"][0]["from"], "accepted")
        self.assertEqual(raw["demotions"][0]["to"], "unknown")

    def test_page_empty_alone_is_not_enough(self):
        self.make_accepted()
        out = sub.demote(EP, PROMPT, reason="页面空白",
                         evidence={"page_empty": True}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "evidence_missing:scope_mismatch|no_l3")

    def test_support_flag_alone_is_not_enough(self):
        self.make_accepted()
        out = sub.demote(EP, PROMPT, reason="scope 不符",
                         evidence={"scope_mismatch": True}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "evidence_missing:page_empty")

    def test_only_once(self):
        self.make_accepted()
        ev = {"page_empty": True, "no_l3": True}
        self.assertTrue(sub.demote(EP, PROMPT, reason="第一次", evidence=ev,
                                   root=self.root)["ok"])
        again = sub.demote(EP, PROMPT, reason="第二次", evidence=ev, root=self.root)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "already_demoted")

    def test_not_accepted_is_refused(self):
        sub.reserve(EP, PROMPT, root=self.root, now=1000.0)   # submitting
        out = sub.demote(EP, PROMPT, reason="x",
                         evidence={"page_empty": True, "no_l3": True}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_accepted")

    def test_missing_record_is_refused(self):
        out = sub.demote(EP, "NOT-THERE", reason="x",
                         evidence={"page_empty": True, "no_l3": True}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_record")

    def test_reason_is_required(self):
        self.make_accepted()
        out = sub.demote(EP, PROMPT, reason="   ",
                         evidence={"page_empty": True, "no_l3": True}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "reason_required")


if __name__ == "__main__":
    unittest.main()
