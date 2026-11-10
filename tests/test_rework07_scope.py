"""CARD-07 返工：锚定到提问文本还不够，必须确认"这个文档就是本轮那个文档"。

独立验收反例（2026-11-06）：在 A 里认领 accepted 之后切到 B（同 URL、不同文档
runtime id），B 里有一条**一模一样的历史提问**和它的旧回答；旧代码一见
`prompt_at` 非空就直接 `attribution="prompt"` 交卷 —— wait_reply 与 final_read
都会把 B 的旧回答当成本轮答案（`submission=accepted, sent=true`）。

这里同时守住两个方向：
  · 文档换了 → unknown/no_reply，绝不复制、绝不重发；
  · 同一个文档里的锚定回答照旧能交卷（别为了拒旧回答把正常路径也毙掉）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from test_card07 import Clock, FakeConversation, PROMPT, ANSWER, bubble, node, text
from qi import workbuddy as wb

SCOPE_A = ("app://fixture", (100, 200))
SCOPE_B = ("app://fixture", (999, 999))


def view(root, rid=(100, 200), url="app://fixture"):
    root.control.GetRuntimeId = lambda: rid
    return SimpleNamespace(root=root, busy=False, value="", url=url, copies=[],
                           editor=None)


def asked_document(answer=None, rid=(100, 200), prompt_line=None):
    """一个文档：可选一条历史提问 + 可选一条回答。"""
    if answer is None:
        root = node("DocumentControl", "", [text(prompt_line)] if prompt_line else [])
    else:
        root, _copy = bubble(answer, prompt_line=prompt_line)
    return view(root, rid)


def baseline_before(answer, rid=(100, 200)):
    """发送前快照：同一文档、还没有本轮提问。"""
    return wb.reply_baseline(asked_document(answer, rid), PROMPT)


class CrossDocumentAttribution(unittest.TestCase):
    def wait(self, roots, baseline):
        conv = FakeConversation(roots)
        with mock.patch.object(wb, "time", Clock()):
            result = wb.wait_reply(conv, PROMPT, 2.0, baseline=baseline)
        return result, conv

    def test_accepted_in_a_then_identical_prompt_in_b_is_not_answered(self):
        baseline = baseline_before("BASELINE-OLD-ANSWER", (100, 200))
        a = asked_document(None, (100, 200), prompt_line=PROMPT)     # 本轮提问，尚无回答
        b = asked_document("OTHER-DOCUMENT-OLD-ANSWER", (999, 999),
                           prompt_line=PROMPT)                        # 别的文档里的同文历史
        got, conv = self.wait([a, b, b], baseline)
        self.assertFalse(got["ok"], got)
        self.assertEqual(got["submission"], "accepted", "事实是发出去过，不能改口")
        self.assertEqual(got["stage"], "timeout")
        self.assertNotIn("reply", got)
        self.assertFalse(conv.copy_calls, "跨文档的旧回答不许点复制键")

    def test_final_read_applies_the_same_document_rule(self):
        baseline = baseline_before("BASELINE-OLD-ANSWER", (100, 200))
        baseline["accepted_scope"] = SCOPE_A
        b = asked_document("OTHER-DOCUMENT-OLD-ANSWER", (999, 999), prompt_line=PROMPT)
        got = wb.final_read(FakeConversation([b]), PROMPT,
                            {"ok": False, "submission": "accepted", "sent": True},
                            baseline, SCOPE_A)
        self.assertIsNone(got, "final_read 不能把别的文档的旧回答交出去")

    def test_actual_ask_does_not_return_another_documents_old_answer(self):
        a = asked_document(None, (100, 200), prompt_line=PROMPT)
        b = asked_document("OTHER-DOCUMENT-OLD-ANSWER", (999, 999), prompt_line=PROMPT)
        conv = FakeConversation([a, b, b])
        with mock.patch.object(wb, "time", Clock()), \
                mock.patch.object(wb, "send_and_wait", return_value={
                    "ok": False, "stage": "timeout", "submission": "unknown", "sent": None}):
            got = wb.ask(conv, PROMPT, 2.0)
        self.assertFalse(got["ok"], got)
        self.assertNotEqual(got.get("reply"), "OTHER-DOCUMENT-OLD-ANSWER")
        self.assertFalse(conv.copy_calls)

    def test_same_document_anchor_still_answers(self):
        baseline = baseline_before("BASELINE-OLD-ANSWER", (100, 200))
        a = asked_document(None, (100, 200), prompt_line=PROMPT)
        b = asked_document(ANSWER, (100, 200), prompt_line=PROMPT)   # 同一个文档，答完了
        got, conv = self.wait([a, b, b], baseline)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["reply"], ANSWER)
        self.assertEqual(got["attribution"], "prompt")
        self.assertEqual(got["submission"], "accepted")

    def test_switched_document_with_brand_new_reply_is_still_unknown(self):
        """换了文档还多出一条"没见过的回复"也不行：文档身份没确认，宁可未知。"""
        baseline = baseline_before("BASELINE-OLD-ANSWER", (100, 200))
        a = asked_document(None, (100, 200), prompt_line=PROMPT)
        b = asked_document("BRAND-NEW-LOOKING-ANSWER", (999, 999), prompt_line=PROMPT)
        got, conv = self.wait([a, b, b], baseline)
        self.assertFalse(got["ok"], got)
        self.assertFalse(conv.copy_calls)

    def test_pre_send_scope_alone_is_enough_to_reject_after_a_switch(self):
        """循环里一次都没认领到提问（accepted_scope 为空）时，发送前快照的文档
        身份仍然要挡住换文档后的同文历史。"""
        baseline = baseline_before("BASELINE-OLD-ANSWER", (100, 200))
        b = asked_document("OTHER-DOCUMENT-OLD-ANSWER", (999, 999), prompt_line=PROMPT)
        got = wb.final_read(FakeConversation([b]), PROMPT,
                            {"ok": False, "submission": "unknown", "sent": None},
                            baseline)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
