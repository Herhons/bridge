"""B31：提问定位与答案取回必须共用同一套匹配。

Round 3 会审：`tools/iterate.py` 与 `qi/conversation.py` 各写一套"拼接后定位"，
改一边忘另一边就会出现"能定位不能取回 / 能取回不能定位"。现在实现只在
`qi.conversation`，tools 只做委托。
"""
from offline_guard import ensure_offline
ensure_offline()

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation                        # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)

FRAGMENTS = ["侧栏", "附件 material.md：第一章是", "**", "规格全文。",
             "答案第一段", "附件 material.md：第一章是", "**", "规格全文。"]
NEEDLE = "附件material.md：第一章是规格全文。"


class SharedRuleTests(unittest.TestCase):
    def test_same_input_same_span(self):
        self.assertEqual(iterate._prompt_span(FRAGMENTS, NEEDLE),
                         conversation.prompt_span(FRAGMENTS, NEEDLE))

    def test_tail_echo_agrees_too(self):
        first = conversation.prompt_span(FRAGMENTS, NEEDLE)
        self.assertEqual(iterate._prompt_span(FRAGMENTS, NEEDLE, first[2]),
                         conversation.prompt_span(FRAGMENTS, NEEDLE, first[2]))

    def test_tools_does_not_reimplement_it(self):
        source = (ROOT / "tools" / "iterate.py").read_text(encoding="utf-8-sig")
        self.assertIn("return conversation.prompt_span(", source)
        self.assertNotIn("merged.find(needle", source)


if __name__ == "__main__":
    unittest.main()
