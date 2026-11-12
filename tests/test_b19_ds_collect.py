"""B19：deepseek 的长回答采集兜底。

现场（job_review4）：回答在页面上已经完整（1658 字），但 `ds_actions`/`ds_copy`
模板被长回答顶出视口 → `view.copies` 恒为 0 → 等待循环每轮 `continue`，
25 分钟后按 `no_reply` 收尾。

修法是把它接进 B17 为 dsh 写的只读 UIA 文本兜底（`uia_text_candidates`），
**只在 `positional` 就绪后生效**（已接受、不忙、>45s），避免拿到半句话。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import conversation                          # noqa: E402


class WhitelistTests(unittest.TestCase):
    def test_deepseek_is_opted_in(self):
        self.assertIn("deepseek", conversation.UIA_TEXT_SITES)

    def test_other_web_sites_are_not(self):
        """还没放开的网页端不该被这条兜底影响。

        chatgpt 已于 2026-11-12 放开（长回答的复制键被顶出视口），
        grok/gemini 仍然没放开——它们的复制路径目前够用。
        """
        for other in ("grok", "gemini"):
            self.assertNotIn(other, conversation.UIA_TEXT_SITES)


class GateTests(unittest.TestCase):
    """源码级约束：兜底必须挂在 positional 后面，否则会拿半句话当答案。"""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "src" / "qi" / "conversation.py").read_text(
            encoding="utf-8-sig")

    def test_fallback_is_gated_by_positional(self):
        idx = self.text.find("in UIA_TEXT_SITES")
        self.assertGreater(idx, 0, "没找到白名单判断")
        window = self.text[max(0, idx - 260):idx]
        self.assertIn("positional", window,
                      "UIA 文本兜底必须在 positional 之后才生效")

    def test_fallback_keeps_screenshot_evidence(self):
        idx = self.text.find("uia_text_after_prompt")
        self.assertGreater(idx, 0, "没找到 uia_text 兜底的 basis")
        window = self.text[max(0, idx - 900):idx + 200]
        self.assertIn("snapshot(", window, "兜底要留截图证据，便于事后复核")


if __name__ == "__main__":
    unittest.main()
