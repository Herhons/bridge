"""Business-level turn tests with UI snapshots, never real input."""

from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi import ui
from qi.conversation import View, ask, composer_empty, is_reply_copy
from qi.screen import Screen


def rect(left, top, right, bottom):
    return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


def node(kind, name, key, *children, box=None):
    control = SimpleNamespace(GetRuntimeId=lambda: [key], IsEnabled=True)
    if box is not None:
        control.BoundingRectangle = rect(*box)
    n = ui.Node(kind, name, control, children=list(children))
    for child in children:
        child.parent = n
    return n


def view(prompt=False, reply=False, busy=False, value="", url="session:1", old=False,
         prompt_key=20, reply_key=30, placeholder=None):
    children = []
    copies = []
    editor = node("EditControl", "", 2, box=(0, 0, 400, 40))
    if old:
        c = node("ButtonControl", "Copy response", 90)
        copies.append(c)
        children += [node("TextControl", "previous answer", 89), c]
    if prompt:
        children.append(node("TextControl", "first line\nsecond line", prompt_key))
    if reply:
        c = node("ButtonControl", "Copy response", reply_key)
        copies.append(c)
        children += [node("TextControl", "new answer", 29), c]
    root = node("DocumentControl", "", 1, editor, *children)
    if placeholder is not None:
        text, box = placeholder
        editor.control.BoundingRectangle = rect(0, 0, 400, 40)
        root.children.append(node("TextControl", text, 4, box=box))
    return View(url, root, editor, value, busy, copies)


class FakeConversation:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.last = None
        self.submits = 0
        self.writes = []
        self.copied = []

    def read(self, **_):
        self.last = next(self.snapshots, self.last)
        return self.last

    def write(self, before, prompt):
        self.writes.append(prompt)

    def submit(self, written):
        self.submits += 1

    def copy(self, target):
        self.copied.append(target.key)
        return "new answer"


class ConversationTests(unittest.TestCase):
    prompt = "first line\nsecond line"

    def run_turn(self, snapshots, timeout=4):  # 2026-10-30: +1.5s settle window
        clock = [0.0]
        conv = FakeConversation(snapshots)
        with patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
             patch("qi.conversation.time.sleep", side_effect=lambda dt: clock.__setitem__(0, clock[0]+dt)):
            result = ask(conv, self.prompt, timeout)
        return result, conv

    def test_unsent_question_never_returns_existing_answer(self):
        result, conv = self.run_turn([view(old=True), view(old=True, value=self.prompt)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(conv.submits, 1)
        self.assertEqual(conv.copied, [])

    def test_new_reply_waits_for_generation_to_finish(self):
        result, conv = self.run_turn([
            view(old=True), view(old=True, value=self.prompt),
            view(prompt=True, reply=True, busy=True, old=True),
            view(prompt=True, reply=True, busy=False, old=True)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "new answer")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(conv.copied, [(30,)])
        self.assertEqual(conv.submits, 1)

    def test_generation_pause_with_copy_available_is_not_completion(self):
        result, conv = self.run_turn([view(), view(value=self.prompt),
                                     view(prompt=True, reply=True, busy=True)])
        self.assertEqual(result["stage"], "timeout")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(conv.copied, [])

    def test_switching_conversation_does_not_copy_another_reply(self):
        """地址栏换了、屏幕上已经有回复：宁可超时也不复制它。"""
        result, conv = self.run_turn([view(), view(value=self.prompt),
                                     view(prompt=True, busy=True),
                                     view(prompt=True, reply=True, url="session:2")])
        self.assertIn(result["stage"], ("timeout", "conversation_changed"))
        self.assertTrue(result["sent"])
        self.assertEqual(conv.copied, [])

    def test_prompt_disappearing_is_a_conversation_change(self):
        result, conv = self.run_turn([view(), view(value=self.prompt),
                                     view(prompt=True), view()])
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(conv.copied, [])

    def test_first_turn_url_change_is_not_a_conversation_change(self):
        """首轮发送后网页把 / 改成 /c/<id>，这是新建会话，不是用户切走。"""
        result, conv = self.run_turn([
            view(), view(value=self.prompt),
            view(prompt=True), view(prompt=True, url="session:2"),
            view(prompt=True, reply=True, url="session:2")])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "new answer")
        self.assertEqual(conv.copied, [(30,)])

    def test_url_change_after_accept_is_refused_not_rebaselined(self):
        """接受之后地址变了 = 换到别的会话：直接拒（不复制、也不再等）。

        2026-11-07 R5 收紧：以前这里只是"把已有回复划成旧回复"然后继续等，
        等于承认"同一页换地址"还能继续收集。可是地址变了就是另一个会话——
        它里面恰好有同样的提问和旧回答时，继续等就可能把别人的答案当本轮结果。
        现在只有"刚接受、还没有任何回复"的首轮换路由才允许重新锚定一次；
        这一例已经有旧回复，所以必须拒。`conv.copied == []` 的底线不变。
        """
        result, conv = self.run_turn(
            [view(), view(value=self.prompt)] + [view(prompt=True, old=True)] * 5
            + [view(prompt=True, old=True, reply=True, url="session:2", reply_key=31)])
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result.get("reason"), "document_changed")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(conv.copied, [])

    def test_runtime_id_churn_does_not_look_like_a_switch(self):
        """Chrome 重排会给同一个气泡换 RuntimeId；不能当成会话切换。"""
        result, conv = self.run_turn([
            view(), view(value=self.prompt),
            view(prompt=True, prompt_key=21),
            view(prompt=True, prompt_key=22, reply=True, reply_key=31)])
        self.assertTrue(result["ok"])
        self.assertEqual(conv.copied, [(31,)])

    def test_placeholder_text_counts_as_empty_composer(self):
        v = view(value="问问 Gemini\n", placeholder=("问问 Gemini", (4, 4, 120, 34)))
        self.assertTrue(composer_empty(v))
        v = view(value="问问 Gemini\n", placeholder=("问问 Gemini", (500, 300, 620, 330)))
        self.assertFalse(composer_empty(v))

    def test_typed_text_is_not_a_placeholder(self):
        v = view(value="first line\nsecond line", placeholder=("问问 Gemini", (4, 4, 120, 34)))
        self.assertFalse(composer_empty(v))

    def test_existing_draft_and_busy_conversation_are_not_modified(self):
        for initial in (view(value="user draft"), view(busy=True)):
            result, conv = self.run_turn([initial])
            self.assertFalse(result["ok"])
            self.assertEqual(result["submission"], "not_sent")
            self.assertEqual(conv.writes, [])
            self.assertEqual(conv.submits, 0)

    def test_mismatched_input_never_submits(self):
        result, conv = self.run_turn([view(), view(value="truncated prompt")])
        self.assertEqual(result["stage"], "input_mismatch")
        self.assertEqual(conv.submits, 0)

    def test_user_copy_without_reply_actions_is_not_a_reply(self):
        copy = node("ButtonControl", "复制", 3)
        node("ToolBarControl", "", 2, copy, node("ButtonControl", "编辑", 4))
        self.assertFalse(is_reply_copy(copy))
        copy = node("ButtonControl", "复制", 7)
        node("GroupControl", "", 6, copy, node("ButtonControl", "点赞", 8))
        self.assertTrue(is_reply_copy(copy))

    def test_shared_text_input_does_not_submit_on_newline(self):
        screen = Screen(verbose=False)
        with patch.object(screen, "_assert_foreground"), patch.object(screen, "hotkey") as chord, \
             patch.object(screen, "press") as key, patch("qi.screen._send"):
            screen.type_text("first\r\nsecond", interval=0)
        chord.assert_called_once_with("shift", "enter")
        key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
