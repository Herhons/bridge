"""B37：气泡认领规则只能有一处实现（R3-6 的第一步）。

`qi/gemini.py` 与 `src/scripts/grok_use.py` 原来各自带着**逐字相同**的
`_squash` / `prompt_matches` 和同名常量。这正是"第二套栈"的真实危害形态：
改一边忘另一边，就会出现"这个端认领得到提问、那个端认不出"，而两边的失败
表现还不一样（一个报 `no_reply` 白等，一个报 `prompt_gone` 早退）。

Round 3 已经吃过一次同类亏：`iteration._squash` 只改一处，导致三轮会审的
"逐字引用"结论全错。所以这里不只测行为，**还测"没有第二份定义"**。
"""
from offline_guard import ensure_offline
ensure_offline()

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "src" / "scripts"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import bubble, gemini                        # noqa: E402
import grok_use                                     # noqa: E402

PROMPT = "第一章 总则\n规格全文见附件 material.md，共 21k 字符。"


class SharedImplementation(unittest.TestCase):
    def test_both_sites_use_the_same_object(self):
        self.assertIs(gemini.prompt_matches, bubble.prompt_matches)
        self.assertIs(grok_use.prompt_matches, bubble.prompt_matches)

    def test_constants_are_shared_too(self):
        self.assertIs(gemini.TAIL_MARKS, bubble.TAIL_MARKS)
        self.assertIs(grok_use.TAIL_MARKS, bubble.TAIL_MARKS)
        self.assertEqual(gemini.MIN_PREFIX, bubble.MIN_PREFIX)
        self.assertEqual(grok_use.MIN_PREFIX, bubble.MIN_PREFIX)

    def test_no_second_definition_anywhere(self):
        """防回归：谁再抄一份本地实现，这里就红。"""
        offenders = []
        for path in (ROOT / "src").rglob("*.py"):
            if path.name == "bubble.py" or "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8-sig")
            # 只认"气泡版"的特征。`iteration._squash` 是**验真归一**（去空白与
            # markdown 标记），语义不同、不是重复，不能算进来。
            if "def prompt_matches(" in text or 'TAIL_MARKS = ("…"' in text \
                    or "整段重复两遍" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "气泡认领规则只允许在 qi/bubble.py 定义")


class Behaviour(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(bubble.prompt_matches(PROMPT, PROMPT))

    def test_collapsed_bubble_with_ellipsis(self):
        """折叠气泡只留开头一段 + 省略号：要认得出来。"""
        head = PROMPT[:20]
        self.assertTrue(bubble.prompt_matches(head + "…", PROMPT))
        self.assertTrue(bubble.prompt_matches(head + "...", PROMPT))

    def test_doubled_bubble_is_squashed(self):
        """真机上用户气泡会挂内外两层，文本整段重复两遍。"""
        self.assertTrue(bubble.prompt_matches(PROMPT * 2, PROMPT))

    def test_too_short_prefix_does_not_claim(self):
        self.assertFalse(bubble.prompt_matches(PROMPT[:5], PROMPT))

    def test_unrelated_text_does_not_claim(self):
        self.assertFalse(bubble.prompt_matches("完全无关的一段话" * 3, PROMPT))

    def test_leading_list_marker_is_ignored(self):
        """页面会把行首「1.」吃掉，归一后才认领得到。"""
        numbered = "1. " + PROMPT
        self.assertTrue(bubble.prompt_matches(PROMPT, numbered))


if __name__ == "__main__":
    unittest.main()


class ReplyPicking(unittest.TestCase):
    """`_pick_reply` / `_already_answered` 也只能有一份实现。

    这两条原来也是逐字相同：改一边忘另一边，就会出现"这个端读得到答案、
    那个端报 no_reply 白等"。现在两端都是对 `qi/bubble` 的一行委托。
    """

    def _page(self, texts):
        """最小 Scan 桩：只要 `prompt_at` / `replies` / `reply_after`。"""
        from types import SimpleNamespace

        blocks = [SimpleNamespace(label=label, text=text)
                  for label, text in texts]
        replies = [b for b in blocks if b.label == "reply" and b.text.strip()]

        class Page:
            def prompt_at(self, prompt):
                return next((i for i, b in enumerate(blocks)
                             if b.label == "prompt" and bubble.prompt_matches(b.text, prompt)), None)

            def replies(self):
                return replies

            def reply_after(self, at):
                if at is None:
                    return None
                return next((b for i, b in enumerate(blocks)
                             if i > at and b.label == "reply" and b.text.strip()), None)

        return Page()

    def test_both_sites_pick_the_same_reply(self):
        page = self._page([("prompt", PROMPT), ("reply", "第一版答案")])
        a = gemini._pick_reply(page, 0, 0)
        b = grok_use._pick_reply(page, 0, 0)
        self.assertIs(a, b)
        self.assertEqual(a.text, "第一版答案")

    def test_both_sites_fall_back_the_same_way(self):
        """认领不到提问时，只认比基线多出来的最新一条。"""
        page = self._page([("reply", "旧答案")])
        self.assertIs(gemini._pick_reply(page, None, 0), grok_use._pick_reply(page, None, 0))
        self.assertIsNone(gemini._pick_reply(page, None, 1), "没多出来就不许读")

    def test_claimed_prompt_without_reply_returns_none(self):
        """认领到提问、但回复还没出来：必须返回 None，**不许**滑到"最新一条"。

        否则上一轮的旧答案会被当成本轮结果（收口时差点写出这个回归：
        `pick_reply` 里一旦在 `reply_after(at)` 返回 None 后继续走兜底就中招）。
        """
        page = self._page([("reply", "上一轮的旧答案"), ("prompt", PROMPT)])
        self.assertIsNone(bubble.pick_reply(page, 1, 0))

    def test_both_sites_see_the_same_already_answered(self):
        page = self._page([("prompt", PROMPT), ("reply", "早就答完了")])
        self.assertEqual(bubble.already_answered(page, PROMPT), "早就答完了")
        self.assertIsNone(bubble.already_answered(self._page([("prompt", PROMPT)]), PROMPT))

    def test_no_second_copy_of_the_body(self):
        """防回归：两端都不许再写一遍 `page.reply_after(at)` 取回复的正文。"""
        offenders = []
        for rel in ("src/qi/gemini.py", "src/scripts/grok_use.py"):
            text = (ROOT / rel).read_text(encoding="utf-8-sig")
            if "return _pick(page, at, baseline)" not in text:
                offenders.append(f"{rel}: 委托缺失")
            if "return _already(scan(root), prompt)" not in text:
                offenders.append(f"{rel}: already_answered 委托缺失")
        self.assertEqual(offenders, [])
