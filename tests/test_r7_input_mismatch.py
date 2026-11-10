"""R7：Gemini `input_mismatch` 的精确复现 —— 行首 `·` 后面是 NBSP。

现场（2026-11-07 05:39:35，hwnd 67552，`evidence/fail/gemini_20261107-053935.json`）：
  第 19 行第 1 字起不一致：期望…文档标题里的会话名（<会话名> · <应…
                           实际…· 文档标题里的会话名（<会话名> · …
源码里第 19 行（归一后）是 `文档标题里的会话名（<会话名> · <应用名>）；不是单独拿标题当身份；`，
原文行首是 `      · 文档标题里的会话名（…`。两边**只差一个行首 `·`**：
提示词里的 `·` 后面是普通空格（归一被吃掉），读回来的 `·` 后面是 U+00A0。

旧 `_MARKER` 只认 `[ \t]`，所以读回来那一侧的标记没被吃掉，紧接着 `_SPACES` 把
U+00A0 换成普通空格 -> 实际行以 `· ` 开头 -> 判不一致（not_sent）。
发送的正文没有丢字：18 行全对，只有这一行的**标记空白**不同。

这里钉住：
  1. 同文、只是 `·` 后面是 NBSP / 全角空格 -> 视为一致（归一，不改发送正文）；
  2. `·` 前面是 NBSP 也一样；
  3. **内容变了必须仍然不一致**（换字、丢词），并指到那一处；
  4. 行中间的 `·` 不是列表标记，不许被吃掉；
  5. 真实入口 `conversation.ask`：同一段文本不再 input_mismatch；改一个字仍然拒绝。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from test_conversation import FakeConversation, view          # noqa: E402
from qi import textcmp                                       # noqa: E402
from qi.conversation import ask                              # noqa: E402

# 现场那两行（期望 = 提示词原文行；实际 = 读回来的那一行）
EXPECTED = "      · 文档标题里的会话名（`<会话名> · <应用名>`）；**不是**单独拿标题当身份；"
READBACK = "·\u00a0文档标题里的会话名（<会话名> · <应用名>）；不是单独拿标题当身份；"


class Normalization(unittest.TestCase):
    def test_bullet_followed_by_nbsp_is_the_same_text(self):
        self.assertTrue(textcmp.equal(EXPECTED, READBACK),
                        textcmp.describe(EXPECTED, READBACK))
        self.assertIsNone(textcmp.first_diff(EXPECTED, READBACK))

    def test_nbsp_before_the_marker_is_the_same_text(self):
        actual = "\u00a0" + READBACK
        self.assertTrue(textcmp.equal(EXPECTED, actual), textcmp.describe(EXPECTED, actual))

    def test_ideographic_space_after_the_marker_is_the_same_text(self):
        actual = "·\u3000文档标题里的会话名（<会话名> · <应用名>）；不是单独拿标题当身份；"
        self.assertTrue(textcmp.equal(EXPECTED, actual), textcmp.describe(EXPECTED, actual))

    def test_changed_character_still_mismatches(self):
        """改一个字（身份->身分）必须仍然判不一致，并指到那一处。"""
        actual = READBACK.replace("身份", "身分")
        self.assertFalse(textcmp.equal(EXPECTED, actual))
        diff = textcmp.first_diff(EXPECTED, actual)
        self.assertIsNotNone(diff)
        self.assertIn("身份", diff["expected"])
        self.assertIn("身分", diff["actual"])

    def test_dropped_word_still_mismatches(self):
        actual = READBACK.replace("不是", "")
        self.assertFalse(textcmp.equal(EXPECTED, actual))

    def test_marker_in_the_middle_is_not_stripped(self):
        """行内 `·` 是正文，不是列表标记。"""
        self.assertFalse(textcmp.equal("甲 · 乙", "乙"))
        self.assertTrue(textcmp.equal("甲 · 乙", "甲 · 乙"))


class RealEntry(unittest.TestCase):
    def run_turn(self, prompt, readback, timeout=2):
        clock = [0.0]
        conv = FakeConversation([view(), view(value=readback)])
        with mock.patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("qi.conversation.time.sleep",
                           side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, prompt, timeout)
        return result, conv

    def test_same_text_with_nbsp_bullet_passes_the_equality_check(self):
        result, conv = self.run_turn(EXPECTED, READBACK)
        self.assertNotEqual(result["stage"], "input_mismatch", result)
        self.assertEqual(conv.submits, 1, "归一后一致就应当真的发出去")
        self.assertEqual(result["submission"], "unknown")

    def test_changed_text_is_still_rejected_by_the_real_entry(self):
        result, conv = self.run_turn(EXPECTED, READBACK.replace("身份", "身分"))
        self.assertEqual(result["stage"], "input_mismatch", result)
        self.assertEqual(result["submission"], "not_sent")
        self.assertEqual(conv.submits, 0, "内容变了就不许发送")
        self.assertIn("第 1 行", result["hint"])


if __name__ == "__main__":
    unittest.main()
