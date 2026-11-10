"""B21：`desk_sites` 的 schema 校验。

它是四个桌面端的静态坐标 + 发送键色彩表，也是唯一"测试源码里从未出现过"的模块。
这些值全靠人工实测抄进来，最容易出的错是**复制一行改一半**（`name` 忘改、模板名
沿用上一个端、min/max 写反）。这里不验证坐标对不对（只有真机能验），只验证
数据结构自洽——那类错误能在这里当场拦住。
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

from qi import desk_flow                             # noqa: E402
from qi.desk_sites import SITES                      # noqa: E402

EXPECTED = {"codex", "dsh", "opencode", "workbuddy"}
CHANNELS = ("b", "g", "r")


def _pair(value):
    return (isinstance(value, tuple) and len(value) == 2
            and all(isinstance(v, int) for v in value))


class DeskSiteSchema(unittest.TestCase):
    def test_key_set(self):
        self.assertEqual(set(SITES), EXPECTED)

    def test_name_matches_key(self):
        for key, site in SITES.items():
            self.assertIsInstance(site, desk_flow.DeskSite, key)
            self.assertEqual(site.name, key, "复制后忘了改 name")

    def test_process_and_title(self):
        for key, site in SITES.items():
            self.assertTrue(site.proc.endswith(".exe"), f"{key}: {site.proc}")
            self.assertTrue(site.title_hint.strip(), key)

    def test_colour_ranges(self):
        for key, site in SITES.items():
            for ch in CHANNELS:
                lo = getattr(site, f"send_{ch}_min")
                hi = getattr(site, f"send_{ch}_max")
                self.assertLessEqual(0, lo, f"{key}/{ch} 下界为负")
                self.assertLess(lo, hi, f"{key}/{ch} 写反了（{lo} >= {hi}）")
                self.assertLessEqual(hi, 255, f"{key}/{ch} 上界超 255")

    def test_send_size(self):
        for key, site in SITES.items():
            self.assertTrue(_pair(site.send_size), f"{key}: {site.send_size}")
            lo, hi = site.send_size
            self.assertLess(0, lo, key)
            self.assertLess(lo, hi, f"{key} 尺寸写反了")
            self.assertLessEqual(hi, 64, f"{key} 尺寸异常（>64px）")

    def test_offsets_are_pairs_and_not_origin(self):
        for key, site in SITES.items():
            for field in ("composer", "chip"):
                value = getattr(site, field)
                self.assertTrue(_pair(value), f"{key}/{field}: {value}")
                self.assertNotEqual(value, (0, 0), f"{key}/{field} 还是占位的 (0,0)")

    def test_effort_only_opencode(self):
        owners = [key for key, site in SITES.items() if site.effort is not None]
        self.assertEqual(owners, ["opencode"], "目前只有 opencode 有独立的强度芯片")
        self.assertTrue(_pair(SITES["opencode"].effort))

    def test_newchat_is_none_or_pair(self):
        for key, site in SITES.items():
            if site.newchat is not None:
                self.assertTrue(_pair(site.newchat), f"{key}: {site.newchat}")

    def test_copy_template_names(self):
        for key, site in SITES.items():
            self.assertIsInstance(site.copy_tpl, str, key)
            self.assertTrue(site.copy_tpl.startswith("desk_"), f"{key}: {site.copy_tpl}")
            self.assertTrue(site.copy_tpl.endswith("_copy"), f"{key}: {site.copy_tpl}")

    def test_defaults_are_sane(self):
        site = desk_flow.DeskSite("x", "X.exe", "X")
        self.assertEqual(site.name, "x")
        self.assertIsNone(site.effort)
        self.assertIsNone(site.newchat)
        self.assertEqual(site.copy_tpl, "")


if __name__ == "__main__":
    unittest.main()
