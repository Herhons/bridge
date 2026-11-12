"""B30：提问被拆成多块时，不能判成 `prompt_gone`。

真机（2026-11-12，grok）：一条提问渲染成 9~10 个叶子节点（`**` 也单独成块），
`prompt_nodes()` 整段相等匹配返回 0 → 5 次读不到 → `conversation_changed/prompt_gone`，
而答案其实还在生成中。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui                    # noqa: E402

PROMPT = "附件 material.md：第一章是规格全文。请**只依据第一章**审这份规格，给 6 条意见。"


def node(kind, name, children=()):
    item = ui.Node(kind, name)
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view_with(*nodes):
    return SimpleNamespace(root=node("PaneControl", "", list(nodes)))


class FragmentTests(unittest.TestCase):
    def test_fragmented_prompt_is_recognized(self):
        parts = ["附件 material.md：第一章是规格全文。", "只依据第一章", "审这份规格，给 6 条意见。"]
        view = view_with(*[node("TextControl", part) for part in parts])
        self.assertEqual(conversation.prompt_nodes(view, PROMPT), [])
        got = conversation.prompt_fragments(view, PROMPT)
        # 只认 ≥12 字的块；`只依据第一章`（6 字）与 `**` 不算
        self.assertEqual(len(got), 2)

    def test_unrelated_text_is_not_claimed(self):
        view = view_with(node("TextControl", "这段文字和提问完全没有关系，长度也够。"))
        self.assertEqual(conversation.prompt_fragments(view, PROMPT), [])

    def test_short_fragments_are_ignored(self):
        # `**` 这类 2 字符块不算（否则任何页面都能凑出两块）
        view = view_with(node("TextControl", "**"), node("TextControl", "只依据第一章"))
        self.assertEqual(conversation.prompt_fragments(view, PROMPT), [])

    def test_wiring_is_only_a_presence_check(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("fragments = prompt_fragments(view, prompt)", source)
        self.assertIn("if len(fragments) >= 2:", source)


if __name__ == "__main__":
    unittest.main()
