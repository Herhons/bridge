"""B29：收卷时把「同一视觉行」的碎片拼回一行。

2026-11-12 实测：chatgpt 的行内代码（`safe_ui.py`、`_ask_upload_body`）在 UIA 树里
是独立节点，按节点换行会把一句「引用」拆成 3 行，验真抓不到引号 → 引句齐全的
回复被判 unread。这里钉住回拼规则（纯函数，不碰真机）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class JoinTests(unittest.TestCase):
    def test_same_line_fragments_are_joined_without_newline(self):
        items = [("收口到守卫", Rect(0, 100, 80, 120)),
                 ("safe_ui.py", Rect(82, 100, 160, 120)),
                 ("绑定预期顶层。", Rect(162, 100, 260, 120))]
        self.assertEqual(iterate._join_by_lines(items),
                         "收口到守卫safe_ui.py绑定预期顶层。")

    def test_next_line_starts_a_new_line(self):
        items = [("第一行", Rect(0, 100, 100, 120)),
                 ("第二行", Rect(0, 130, 100, 150))]
        self.assertEqual(iterate._join_by_lines(items), "第一行\n第二行")

    def test_far_apart_fragments_are_not_merged(self):
        # 同一水平线上但隔得很远（分栏 / 右对齐时间戳）不能拼成一句
        items = [("左栏", Rect(0, 100, 60, 120)),
                 ("右栏", Rect(400, 100, 460, 120))]
        self.assertEqual(iterate._join_by_lines(items), "左栏\n右栏")

    def test_missing_rect_falls_back_to_own_line(self):
        items = [("没有矩形", None), ("独立行", None)]
        self.assertEqual(iterate._join_by_lines(items), "没有矩形\n独立行")

    def test_empty_list(self):
        self.assertEqual(iterate._join_by_lines([]), "")


class EchoRuleTests(unittest.TestCase):
    PROMPT = "附件 material.md 是规格全文。请给 6 条意见，按收益排序。"

    def test_plain_echo_is_dropped(self):
        self.assertTrue(iterate._is_prompt_echo(
            "附件 material.md 是规格全文。", self.PROMPT,
            "".join(self.PROMPT.split())))

    def test_citation_is_exempt(self):
        self.assertFalse(iterate._is_prompt_echo(
            "「附件 material.md 是规格全文。」", self.PROMPT,
            "".join(self.PROMPT.split())))

    def test_short_block_is_not_echo(self):
        self.assertFalse(iterate._is_prompt_echo(
            "可访问=是", self.PROMPT, "".join(self.PROMPT.split())))


class WiringTests(unittest.TestCase):
    def test_read_answer_text_uses_line_join(self):
        source = (ROOT / "tools" / "iterate.py").read_text(encoding="utf-8-sig")
        self.assertIn("return _join_by_lines(out)", source)
        self.assertIn("return _join_by_lines(kept)", source)
        self.assertNotIn('return "\\n".join(_drop_prompt_echo(body, prompt)).strip()', source)


class PromptSpanTests(unittest.TestCase):
    """提问被拆成多块时，用拼接后的整段定位气泡起止。"""

    TEXTS = ["侧栏标题", "附件 material.md：第一章是", "**", "规格全文。",
             "答案第一段", "答案第二段", "附件 material.md：第一章是", "**", "规格全文。"]

    def test_span_covers_all_fragments(self):
        needle = "附件material.md：第一章是规格全文。"
        first, last, end = iterate._prompt_span(self.TEXTS, needle)
        self.assertEqual(first, 1)
        self.assertEqual(last, 3)          # 跨过 `**` 那块
        self.assertGreater(end, 0)

    def test_second_occurrence_is_the_tail_echo(self):
        needle = "附件material.md：第一章是规格全文。"
        first, last, end = iterate._prompt_span(self.TEXTS, needle)
        tail = iterate._prompt_span(self.TEXTS, needle, start_at=end)
        self.assertIsNotNone(tail)
        self.assertEqual(tail[0], 6)

    def test_missing_needle_returns_none(self):
        self.assertIsNone(iterate._prompt_span(self.TEXTS, "根本不存在的句子"))


class ProgressNoiseTests(unittest.TestCase):
    def test_process_lines_are_noise(self):
        for text in ("Analyzing the specification document", "工作了 57s",
                     "Grok", "Grok 已完成回复"):
            self.assertTrue(iterate.is_progress_noise(text), text)

    def test_chinese_content_is_kept(self):
        for text in ("问题：闸门关闭后施工不再等人", "①§10", "第二段：结论如下。"):
            self.assertFalse(iterate.is_progress_noise(text), text)

    def test_english_sentence_with_period_is_kept(self):
        self.assertFalse(iterate.is_progress_noise("This is a real sentence."))


class FallbackOrderTests(unittest.TestCase):
    def test_legacy_path_stays_primary(self):
        source = (ROOT / "tools" / "iterate.py").read_text(encoding="utf-8-sig")
        self.assertIn("if len(_join_by_lines(body)) < 80:", source)


if __name__ == "__main__":
    unittest.main()
