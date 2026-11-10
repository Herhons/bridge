
from offline_guard import ensure_offline
ensure_offline()

import unittest
from types import SimpleNamespace
from unittest import mock

from test_card07 import Clock, FakeConversation, PROMPT, ANSWER, bubble, node, text
from qi import workbuddy as wb


def view(root, rid=(100, 200), url="app://fixture"):
    root.control.GetRuntimeId = lambda: rid
    return SimpleNamespace(root=root, busy=False, value="", url=url, copies=[], editor=None)


class ReplyAttribution(unittest.TestCase):
    def wait(self, roots, baseline=None, copied=None):
        conv = FakeConversation(roots, copies={"value": copied} if copied else None)
        with mock.patch.object(wb, "time", Clock()):
            result = wb.wait_reply(conv, PROMPT, 1.0, baseline=baseline)
        return result, conv

    def test_reported_old_reply_counterexample_is_refused_in_main_loop(self):
        root, _ = bubble("OLD-ANSWER")
        got, conv = self.wait([view(root)], baseline=1)
        self.assertFalse(got["ok"])
        self.assertEqual(got["submission"], "unknown")
        self.assertFalse(conv.copy_calls)

    def test_no_before_snapshot_cannot_turn_existing_reply_into_new(self):
        root, _ = bubble("OLD-ANSWER")
        got, _ = self.wait([view(root)])
        self.assertFalse(got["ok"])

    def test_replaced_or_virtualized_copy_with_same_count_is_not_new(self):
        before, _ = bubble("OLD-ANSWER")
        after, _ = bubble("REPLACED-OLD-ANSWER")
        baseline = wb.reply_baseline(view(before), PROMPT)
        got, _ = self.wait([view(after)], baseline=baseline)
        self.assertFalse(got["ok"])

    def test_loaded_duplicate_old_text_is_not_new_even_if_count_increases(self):
        old, _ = bubble("OLD-ANSWER")
        old2, _ = bubble("OLD-ANSWER")
        baseline = wb.reply_baseline(view(old), PROMPT)
        after = node("DocumentControl", children=[*old.children, *old2.children])
        got, _ = self.wait([view(after)], baseline)
        self.assertFalse(got["ok"])

    def test_new_reply_evidence_requires_same_document(self):
        # R9：未锚定路径只剩"条数变多"这一条证据，它必须再加**严格会话 id**。
        # 这里给同一条带会话路径的地址；不是放宽判据，而是把"证明得了"的前提补齐。
        chat = "https://gemini.google.com/app/s-r9"
        before = view(node("DocumentControl"), url=chat)
        baseline = wb.reply_baseline(before, PROMPT)
        after, _ = bubble()
        got, _ = self.wait([view(after, rid=(9, 9), url=chat)], baseline)
        self.assertFalse(got["ok"])
        got, _ = self.wait([view(after, url=chat)], baseline)
        self.assertFalse(got["ok"], "history loading alone is not current-turn evidence")
        claimed = view(node("DocumentControl", children=[text(PROMPT)]), url=chat)
        got, _ = self.wait([claimed, view(after, url=chat), view(after, url=chat)], baseline)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["attribution"], "new_reply")

    def test_final_read_and_main_loop_use_identical_negative_rule(self):
        root, _ = bubble("OLD-ANSWER")
        current = view(root)
        baseline = wb.reply_baseline(current, PROMPT)
        got = wb.final_read(FakeConversation([current]), PROMPT,
                            {"ok": False, "submission": "unknown", "sent": None}, baseline)
        self.assertIsNone(got)

    def test_anchored_reply_wins_over_stale_clipboard(self):
        root, _ = bubble(ANSWER, prompt_line=PROMPT)
        got, _ = self.wait([view(root)], copied="STALE-CLIPBOARD-ANSWER")
        self.assertTrue(got["ok"])
        self.assertEqual(got["reply"], ANSWER)
        self.assertEqual(got["read_via"], "uia")
        self.assertEqual(got["attribution"], "prompt")

    def test_actual_ask_captures_before_baseline_and_does_not_accept_old_reply(self):
        root, _ = bubble("OLD-ANSWER")
        current = view(root)
        conv = FakeConversation([current])
        with mock.patch.object(wb, "time", Clock()), \
                mock.patch.object(wb, "send_and_wait", return_value={
                    "ok": False, "stage": "timeout", "submission": "unknown", "sent": None}):
            got = wb.ask(conv, PROMPT, 1.0)
        self.assertFalse(got["ok"])
        self.assertFalse(conv.copy_calls)


if __name__ == "__main__":
    unittest.main()
