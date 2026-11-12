"""B29：DeepSeek 的完成检测（不用图片模板、不靠 hover）。

背景（2026-11-12）：deepseek 连续三轮会审都走 `stage=text_extracted` 兜底——
`conversation.ask` 每轮空转到 timeout。两个原因一起修：

1. `busy` 由「停止」图标的模板匹配决定，回答结束后图标还在画面上 → busy 恒真
   → `positional` 永不放行 → 25 分钟空转；
2. 即使放行，交卷只取 `nodes[-1]`，而 deepseek 的长回答是「一段一个 TextControl」
   （实测 50+ 段），只交最后一段等于交半句话。

这里钉住三条纯函数与一处接线（不依赖真机）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui                       # noqa: E402

PROMPT = "请审这份规格，给 6 条意见。"


def node(kind, name, children=()):
    item = ui.Node(kind, name)
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


class AnswerActionTests(unittest.TestCase):
    def test_counts_only_after_last_prompt(self):
        old_q = node("TextControl", "上一轮的提问")
        old_action = node("ButtonControl", "复制")       # 历史回答的动作条
        question = node("TextControl", PROMPT)
        answer = node("TextControl", "答案第一段")
        speak = node("ButtonControl", "朗读")
        root = node("PaneControl", "", [old_q, old_action, question, answer, speak])
        prompts = conversation.prompt_nodes(SimpleNamespace(root=root), PROMPT)
        self.assertTrue(prompts)
        self.assertEqual(conversation.answer_action_after(root, prompts), 1)

    def test_zero_while_generating(self):
        question = node("TextControl", PROMPT)
        answer = node("TextControl", "正在生成的半句话")
        root = node("PaneControl", "", [question, answer])
        prompts = conversation.prompt_nodes(SimpleNamespace(root=root), PROMPT)
        self.assertEqual(conversation.answer_action_after(root, prompts), 0)

    def test_non_action_buttons_do_not_count(self):
        question = node("TextControl", PROMPT)
        answer = node("TextControl", "答案")
        root = node("PaneControl", "", [question, answer,
                                        node("ButtonControl", "发送"),
                                        node("ButtonControl", "深度思考")])
        prompts = conversation.prompt_nodes(SimpleNamespace(root=root), PROMPT)
        self.assertEqual(conversation.answer_action_after(root, prompts), 0)

    def test_no_prompt_means_no_evidence(self):
        root = node("PaneControl", "", [node("TextControl", "随便什么")])
        self.assertEqual(conversation.answer_action_after(root, []), 0)


class PositionalGateTests(unittest.TestCase):
    def test_busy_with_evidence_is_allowed(self):
        self.assertTrue(conversation.positional_ready(
            busy=True, accepted=True, since=0.0, now=100.0, evidence=True))

    def test_busy_without_evidence_still_blocked(self):
        self.assertFalse(conversation.positional_ready(
            busy=True, accepted=True, since=0.0, now=100.0))

    def test_evidence_does_not_bypass_time_or_acceptance(self):
        self.assertFalse(conversation.positional_ready(
            busy=True, accepted=True, since=100.0, now=110.0, evidence=True))
        self.assertFalse(conversation.positional_ready(
            busy=True, accepted=False, since=0.0, now=100.0, evidence=True))


class JoinTests(unittest.TestCase):
    def test_multi_paragraph_is_joined_in_order(self):
        nodes = [node("TextControl", "第一段"), node("TextControl", "第二段"),
                 node("TextControl", "第三段")]
        self.assertEqual(conversation.uia_text_join(nodes), "第一段\n第二段\n第三段")

    def test_adjacent_duplicates_are_collapsed(self):
        nodes = [node("TextControl", "第一段"), node("TextControl", "第一段"),
                 node("TextControl", "第二段")]
        self.assertEqual(conversation.uia_text_join(nodes), "第一段\n第二段")

    def test_empty_nodes_are_skipped(self):
        nodes = [node("TextControl", "第一段"), node("TextControl", "")]
        self.assertEqual(conversation.uia_text_join(nodes), "第一段")

    def test_join_is_not_last_node_only(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("text = uia_text_join(nodes)", source)
        self.assertNotIn("ui.canonical(ui.text_of(nodes[-1]))", source)


class WiringTests(unittest.TestCase):
    def test_evidence_is_wired_into_the_gate(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("evidence = answer_action_after(view.root, prompts,", source)
        self.assertIn("evidence=evidence", source)

    def test_timeout_carries_diagnostics(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        for key in ('"diagnostics"', '"copies_max"', '"busy_reads"', '"evidence_seen"'):
            self.assertIn(key, source)


class PromptBoundsTests(unittest.TestCase):
    """编辑器之后的历史提问预览不能当锚点（deepseek 实际排版）。"""

    def test_preview_after_editor_is_excluded(self):
        q = node("TextControl", PROMPT)
        answer = node("TextControl", "答案正文，足够长的一句。")
        editor = node("EditControl", "")
        preview = node("TextControl", PROMPT)
        root = node("PaneControl", "", [q, answer, editor, preview])
        got = conversation.uia_text_candidates(
            SimpleNamespace(root=root, editor=editor, copies=[], busy=False),
            [q, preview], editor, PROMPT)
        self.assertEqual(got, [answer])

    def test_indices_keep_only_prompt_before_editor(self):
        q = node("TextControl", PROMPT)
        editor = node("EditControl", "")
        preview = node("TextControl", PROMPT)
        ordered = [q, editor, preview]
        inside, end = conversation._prompt_indices(ordered, [q, preview], editor)
        self.assertEqual(inside, [0])
        self.assertEqual(end, 1)


class CountTextTests(unittest.TestCase):
    def test_counts_and_timestamps(self):
        for text in ("-1-11", "15个网页", "2s", "  12 "):
            self.assertTrue(conversation.is_count_text(text), text)

    def test_real_content_is_kept(self):
        for text in ("第 8 段：结论", "2026-11-12", "", "completed。"):
            self.assertFalse(conversation.is_count_text(text), text)


class EchoFragmentTests(unittest.TestCase):
    """提问气泡被按行拆开时，碎片也要当回声丢掉。"""

    def test_fragment_of_prompt_is_excluded(self):
        # 提问是 GroupControl，正文在子节点里（deepseek 真机形状）
        fragment = node("TextControl", "有哪些可靠信号、各自会怎样误判")
        q = node("GroupControl", "", [fragment])
        answer = node("TextControl", "答案正文，足够长的一句。")
        editor = node("EditControl", "")
        root = node("PaneControl", "", [q, answer, editor])
        got = conversation.uia_text_candidates(
            SimpleNamespace(root=root, editor=editor, copies=[], busy=False),
            [q], editor, PROMPT)
        self.assertEqual(got, [answer])


if __name__ == "__main__":
    unittest.main()
