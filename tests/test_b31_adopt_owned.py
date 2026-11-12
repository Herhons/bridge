"""B31：门禁写好的预约必须核对"是不是本次调用的"。

Round 3 会审三方一致：`_Ledger.adopt()` 只置 `reserved=True` 等于无条件信任门禁的
布尔值，过期回执会被当成本次预约，终态写账就会改掉旧记录（B24/B26 的同一类风险）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import submissions as sub                 # noqa: E402

EP = "chatgpt"
PROMPT = "BRIDGE-B31-ADOPT"
SCOPE = {"site": "chatgpt", "hwnd": 12345, "session_id": "s1"}


class AdoptOwnedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_matching_scope_is_accepted(self):
        sub.reserve(EP, PROMPT, root=self.root, scope=SCOPE, now=1000.0)
        out = sub.adopt_owned(EP, PROMPT, scope=dict(SCOPE), root=self.root)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["v2_state"], "submitting")

    def test_different_window_is_refused(self):
        sub.reserve(EP, PROMPT, root=self.root, scope=SCOPE, now=1000.0)
        out = sub.adopt_owned(EP, PROMPT, scope={**SCOPE, "hwnd": 999}, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "hwnd_mismatch")

    def test_different_session_alone_does_not_block(self):
        # 发送时新会话还没生成 session_id，doc_rid 也会变：这一层不能当判据
        sub.reserve(EP, PROMPT, root=self.root, scope=SCOPE, now=1000.0)
        out = sub.adopt_owned(EP, PROMPT, scope={**SCOPE, "session_id": "s2"}, root=self.root)
        self.assertTrue(out["ok"], out)

    def test_missing_record_is_refused(self):
        out = sub.adopt_owned(EP, "NO-SUCH-PROMPT", scope=SCOPE, root=self.root)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "reservation_missing")

    def test_unreadable_scope_does_not_block(self):
        # 读不到页面身份是常态：只拒绝"能比对且不一致"
        sub.reserve(EP, PROMPT, root=self.root, scope=SCOPE, now=1000.0)
        self.assertTrue(sub.adopt_owned(EP, PROMPT, scope=None, root=self.root)["ok"])
        sub.reserve(EP, "P2", root=self.root, scope=None, now=1000.0)
        self.assertTrue(sub.adopt_owned(EP, "P2", scope=SCOPE, root=self.root)["ok"])


if __name__ == "__main__":
    unittest.main()
