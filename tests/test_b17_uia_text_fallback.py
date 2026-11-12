"""B17：dsh 的只读 UIA 文本兜底必须「只认回复、标明来源」。

首页的复制键要 hover 才出现，`view.copies` 一直是空的。用户授权的兜底
读「末个提问之后、编辑器之前」的叶子文本，绝不把侧栏/提问/占位当回复。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui  # noqa: E402

PROMPT = "只回复下面这一行，不要任何解释、不要加标点：BRIDGE-B17-TEST"
REPLY = "BRIDGE-B17-TEST"


def node(kind, name, children=()):
    item = ui.Node(kind, name)
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def fixture(extra=()):
    sidebar = node("TextControl", "探索未至之境")
    question = node("TextControl", PROMPT)
    answer = node("TextControl", REPLY)
    placeholder = node("TextControl", "描述你想要构建的内容…")
    editor = node("EditControl", "")
    copy_button = node("ButtonControl", "复制")
    children = [sidebar, question, answer, *extra, placeholder, editor, copy_button]
    root = node("PaneControl", "", children)
    view = SimpleNamespace(root=root, editor=editor, copies=[], busy=False)
    return view, question, answer, editor


class PureHelperTests(unittest.TestCase):
    def test_only_reply_text_is_collected(self):
        view, question, answer, editor = fixture()
        got = conversation.uia_text_candidates(view, [question], editor, PROMPT)
        self.assertEqual(got, [answer])
        self.assertEqual(ui.canonical(ui.text_of(got[0])), REPLY)

    def test_prompt_echo_is_excluded(self):
        echo = node("TextControl", PROMPT)
        view, question, answer, editor = fixture(extra=[echo])
        got = conversation.uia_text_candidates(view, [question], editor, PROMPT)
        self.assertEqual(got, [answer])

    def test_ui_labels_are_excluded(self):
        words = node("TextControl", "Max")
        share = node("TextControl", "分享")
        view, question, answer, editor = fixture(extra=[words, share])
        self.assertEqual(conversation.uia_text_candidates(
            view, [question], editor, PROMPT), [answer])

    def test_no_question_node_returns_nothing(self):
        view, _question, _answer, editor = fixture()
        self.assertEqual(conversation.uia_text_candidates(
            view, [], editor, PROMPT), [])

    def test_missing_editor_still_stops_before_action_buttons(self):
        view, question, answer, _editor = fixture()
        got = conversation.uia_text_candidates(view, [question], None, PROMPT)
        self.assertEqual(got, [answer])       # ButtonControl 不是叶子文本

    def test_text_len_signature(self):
        _view, _question, answer, _editor = fixture()
        self.assertEqual(conversation.uia_text_len([answer]), len(REPLY))

    def test_only_opted_in_sites(self):
        """B17 只开 dsh；B19 加 deepseek；B29 加 chatgpt（同一类：动作条顶出视口）。

        这条用例原本写死 == {"dsh"}，之后每次有意放开一端都会改这里，
        所以改成"该开的三端都在，其它网页端一个都不许进"——比精确相等更能守住策略。

        chatgpt 的证据（2026-11-12 真机）：复制键 y=2061、窗口到 1340，
        `copy()` 抛「光标没能移到」，长回答此前只能靠 timeout 后的 text_extracted。
        """
        self.assertEqual(conversation.UIA_TEXT_SITES,
                         frozenset({"dsh", "deepseek", "chatgpt"}))
        for other in ("grok", "gemini"):
            self.assertNotIn(other, conversation.UIA_TEXT_SITES)


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = (ROOT / "src" / "qi" / "conversation.py").read_text(
            encoding="utf-8-sig")
        cls.server = (ROOT / "src" / "qi" / "server.py").read_text(
            encoding="utf-8-sig")
        cls.workbuddy = (ROOT / "src" / "qi" / "workbuddy.py").read_text(
            encoding="utf-8-sig")

    def test_fallback_is_gated_on_dsh_and_positional(self):
        self.assertIn("in UIA_TEXT_SITES", self.conv)
        self.assertIn("and positional and callable(helper)", self.conv)
        self.assertIn('"uia_text", len(view.copies), len(nodes)', self.conv)

    def test_result_marks_the_source_and_takes_a_screenshot(self):
        for literal in ('"basis": "uia_text_after_prompt"',
                        '"source": "uia_text"',
                        'screen.snapshot("dsh_uia_text")',
                        'uia_screenshot_failed'):
            self.assertIn(literal, self.conv)

    def test_no_new_stage_is_introduced(self):
        # test_r11_reject_signals.NoDeadStageTests 钉着 stage 字面量集合
        self.assertIn('"stage": "answered"', self.conv)
        self.assertNotIn('"stage": "uia_text"', self.conv)

    def test_server_detail_keeps_the_marker(self):
        self.assertIn('"source", "basis", "screenshot"', self.server)

    def test_new_chat_knows_the_real_dsh_label(self):
        self.assertIn('"dsh": {"新建会话", "New session", "新会话"}', self.workbuddy)


if __name__ == "__main__":
    unittest.main()
