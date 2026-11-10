"""UI-BOOTSTRAP-2 / 07 收尾：重启后的"已答"恢复必须仍是**本事务**的可信归属。

背景（上一轮独立验收留下的已知风险）：
`workbuddy._already_replied()` 是重启/重跑前的只读恢复，旧实现只看
"页面上有这条提问 + 它后面有回复"。WorkBuddy 的窗口标题固定是 "WorkBuddy"、
document runtime id 每次重启都变 —— 没有任何身份能跨重启对上。于是用户若开着
**另一个会话**、里面恰好有同一条提问和它的旧回答，恢复会把别人的旧回答当本轮答案。

本轮的收紧（只更严，不放宽）：
  1. 必须先有**本事务的持久化提交记录**（accepted/unknown）——没有记录就说明
     这条提问不是我们发的，谈不上"已答"；
  2. 页面上这条提问必须**只出现一处**；
  3. 它后面必须**恰好一条**可用回复。
任一不过 → None → 调用方走 `submissions.pending` 的拒发分支（不重发、不误归属）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from test_card07 import bubble, node, text            # noqa: E402
from qi import submissions, tier_guard, workbuddy as wb  # noqa: E402

PROMPT = "只回复 07-RECOVERY-OK"
ANSWER = "07-RECOVERY-OK"


def view(root, value=""):
    return SimpleNamespace(root=root, busy=False, value=value, url="app://wb",
                           copies=[], editor=None)


class AlreadyRepliedAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(tier_guard, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    # 2026-11-07 R5：恢复现在还要证明"同一个文档"。这里用固定假句柄 4242 +
    # 固定 document runtime id [700]，seed 和 view 两边用同一份，正例才成立。
    HWND = 4242
    RID = 700

    def scope(self, **over):
        base = dict(site="workbuddy", url="app://wb", doc_rid=[self.RID], hwnd=self.HWND)
        base.update(over)
        return submissions.scope_from(**base)

    def seed(self, state="accepted", *, scope=None):
        submissions.reserve("workbuddy", PROMPT, scope=self.scope() if scope is None else scope)
        if state != "unknown":
            submissions.finish("workbuddy", PROMPT, {"submission": state})

    def call(self, first, *, recheck=None):
        conv = mock.Mock()
        conv.read = mock.Mock(side_effect=[first, recheck] if recheck else [first])
        conv.empty = mock.Mock(return_value=True)
        conv.clear = mock.Mock()
        with mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
                mock.patch.object(wb, "ReplyConversation", return_value=conv):
            return wb._already_replied(PROMPT)

    def answered_root(self, answer=ANSWER):
        root, _copy = bubble(answer, prompt_line=PROMPT)
        root.control.GetRuntimeId = lambda: [self.RID]
        return root

    # ------------------------------------------------ R7：没有会话 id 就不再"兼容成功"
    def test_transaction_record_without_session_id_is_refused(self):
        """R5/CE1 的文档身份（hwnd/doc_rid/url）已经不够了：R7 要求会话 id。"""
        self.seed("accepted")
        got = self.call(view(self.answered_root()))
        self.assertIsNone(got, "只有文档身份、没有会话 id -> 不能认已答")
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")

    def test_unknown_state_without_session_id_is_refused_too(self):
        self.seed("unknown")
        got = self.call(view(self.answered_root()))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")

    # ------------------------------------------------ 反例
    def test_without_a_transaction_record_the_same_text_is_not_ours(self):
        """别的会话里恰好有同一条提问 + 旧回答：没有本事务记录就不能认。"""
        got = self.call(view(self.answered_root()))
        self.assertIsNone(got)

    def test_prompt_appearing_twice_is_ambiguous(self):
        self.seed()
        root = self.answered_root()
        root.children.insert(0, text(PROMPT))       # 同一提问又出现一次
        got = self.call(view(root))
        self.assertIsNone(got, "两处同文提问 -> 归属不唯一")

    def test_two_replies_after_the_prompt_are_ambiguous(self):
        self.seed()
        first, _c1 = bubble(ANSWER, prompt_line=PROMPT)
        second, _c2 = bubble("下一轮的旧回答")
        root = node("DocumentControl", "", [*first.children, *second.children])
        got = self.call(view(root))
        self.assertIsNone(got, "后面不止一条回复 -> 可能是别的轮次")

    def test_no_reply_after_the_prompt_is_not_answered(self):
        self.seed()
        root = node("DocumentControl", "", [text(PROMPT)])
        got = self.call(view(root))
        self.assertIsNone(got)

    def test_legacy_journal_without_scope_is_not_answered(self):
        """老记录没有文档身份：哪怕页面长得一模一样，也不能认已答（CE1）。"""
        submissions.reserve("workbuddy", PROMPT)          # 故意不带 scope
        submissions.finish("workbuddy", PROMPT, {"submission": "accepted"})
        got = self.call(view(self.answered_root()))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "scope_missing")

    def test_different_document_with_same_pair_is_not_answered(self):
        """目标是文档 A，屏幕上却是文档 B（同文同答）——不许认。"""
        self.seed("accepted")
        root, _copy = bubble(ANSWER, prompt_line=PROMPT)
        root.control.GetRuntimeId = lambda: [999]         # 另一个文档
        got = self.call(view(root))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "doc_rid_mismatch")

    def test_restarted_window_is_not_answered(self):
        """hwnd/pid 变了（客户端重启）：身份对不上，认不了。"""
        self.seed("accepted", scope=self.scope(hwnd=777))
        got = self.call(view(self.answered_root()))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "hwnd_mismatch")

    def test_busy_page_is_never_treated_as_answered(self):
        self.seed()
        busy = view(self.answered_root())
        busy.busy = True
        conv = mock.Mock()
        conv.read = mock.Mock(return_value=busy)
        with mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
                mock.patch.object(wb, "ReplyConversation", return_value=conv):
            got = wb._already_replied(PROMPT)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
