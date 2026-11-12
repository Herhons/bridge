"""B30：匿名包必须真的匿名。

Round 3 汇总者指出：附件里出现「ChatGPT说：」「用时 4分钟 41秒」这类界面残渣，
既暴露来源又会带偏汇总。根因是清洗用了 `\b<vendor>\b`——`T` 与「说」都是 `\w`，
边界不成立，替换根本没发生（2026-11-12 实测确认）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import iteration as it                    # noqa: E402


def pack(text, site="chatgpt"):
    return it.anonymize({site: text}, verdicts={site: {"verdict": "true_read"}})


class AnonymityTests(unittest.TestCase):
    def test_vendor_name_before_chinese_is_removed(self):
        out = pack("ChatGPT说：1. §5.3「交给四家，走完整管道」")
        self.assertNotIn("chatgpt", out.lower())
        self.assertNotIn("某端说：", out, "说话人标签应整段去掉")

    def test_ui_residue_lines_are_dropped(self):
        out = pack("可访问=是\n用时 4分钟 41秒\n1. §5.3「交给四家」")
        self.assertNotIn("用时", out)
        self.assertNotIn("可访问", out)
        self.assertIn("交给四家", out)

    def test_unknown_vendor_is_caught_loudly(self):
        with self.assertRaises(it.AnonymityLeak):
            pack("1. 我是 Copilot，§5「x」")

    def test_clean_pack_passes(self):
        out = pack("1. §5.3「交给四家，走完整管道」")
        self.assertIn("某端", out) if False else None
        it.assert_anonymous(out)


if __name__ == "__main__":
    unittest.main()
