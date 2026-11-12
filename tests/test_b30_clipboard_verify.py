"""B30：剪贴板写入后必须核对内容。

现状：只比「序列号变没变」。序列号变了不代表内容是我们的——别的进程可以在
两次读之间把剪贴板换掉，于是 `Ctrl+V` 会把**别人的内容**粘进提问框。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation                      # noqa: E402


class ClipboardMatchTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(conversation.clipboard_matches("BRIDGE-1", "BRIDGE-1"))

    def test_trailing_newline_is_tolerated(self):
        self.assertTrue(conversation.clipboard_matches("a\r\nb", "a\nb"))

    def test_other_content_is_refused(self):
        self.assertFalse(conversation.clipboard_matches("BRIDGE-1", "别人的内容"))

    def test_unreadable_clipboard_is_tolerated(self):
        # 读不出来时不能拒发（环境不支持读取）
        self.assertTrue(conversation.clipboard_matches("BRIDGE-1", None))


class WiringTests(unittest.TestCase):
    def test_verify_runs_before_paste(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("if not clipboard_matches(text, self._clipboard_now()):", source)
        self.assertIn("剪贴板内容与写入不一致：不粘贴", source)


if __name__ == "__main__":
    unittest.main()


class RestoreOwnershipTests(unittest.TestCase):
    """恢复原剪贴板前必须确认"当前内容还是我们写进去的那一份"。"""

    def test_source_only_restores_own_content(self):
        source = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("if clipboard_matches(text, self._clipboard_now()):", source)

    def test_user_content_is_not_clobbered(self):
        # 语义级：写进去的是 A，粘贴期间被用户换成 B → 不该写回旧值
        self.assertFalse(conversation.clipboard_matches("A", "B"))
        # 还是 A → 可以写回
        self.assertTrue(conversation.clipboard_matches("A", "A"))
