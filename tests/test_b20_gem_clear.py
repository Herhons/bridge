"""B20：gemini 清草稿时要把占位文字当空。

现场（B20 汇总轮 turn2）：消息已经发出去了、回答也回来了，但收尾时 `_clear_draft`
读回的是占位文字 `问问 Gemini\n`，和 `""` 一比不相等，于是判"没清掉"，
整轮 abort 成 `interrupted / draft_clear_failed`。后续轮次因此撞 `conversation_busy`。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import gemini                                    # noqa: E402
from test_conversation import node, view                 # noqa: E402


class _Conv:
    def __init__(self, value):
        self.value = value
        self.cleared = 0

    def clear(self, _view):
        self.cleared += 1

    def read(self):
        return SimpleNamespace(value=self.value, root=node("DocumentControl", "", 1))


class ClearDraftTests(unittest.TestCase):
    def test_placeholder_counts_as_empty(self):
        conv = _Conv("问问 Gemini\n")
        self.assertTrue(gemini._clear_draft(conv, view()))
        self.assertEqual(conv.cleared, 1)

    def test_english_placeholder_counts_as_empty(self):
        self.assertTrue(gemini._clear_draft(_Conv("Ask Gemini\n"), view()))

    def test_real_text_still_reports_failure(self):
        """用户真的写了东西、又没清掉 —— 仍然要如实返回 False。"""
        self.assertFalse(gemini._clear_draft(_Conv("用户自己写的一段话"), view()))

    def test_non_string_value_is_failure(self):
        self.assertFalse(gemini._clear_draft(_Conv(None), view()))


if __name__ == "__main__":
    unittest.main()
