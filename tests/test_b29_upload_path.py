"""B29：上传路径必须绝对化。

真机事故（2026-11-12）：给 `upload()` 传了相对路径，文件对话框按它自己的当前目录
解析，文件不存在 → 点了「打开」对话框不关 → `dialog_stuck`，整轮上传失败。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import upload                               # noqa: E402


class AbsolutePathTests(unittest.TestCase):
    def test_relative_becomes_absolute(self):
        got = upload._absolute("docs/iterations/round-2/materials/material.md")
        self.assertTrue(Path(got).is_absolute(), got)
        self.assertTrue(got.endswith("material.md"))

    def test_absolute_is_unchanged(self):
        src = str(Path(ROOT / "docs" / "x.md"))
        self.assertEqual(upload._absolute(src), str(Path(src).resolve()))

    def test_wiring_happens_before_dialog(self):
        source = (ROOT / "src" / "qi" / "upload.py").read_text(encoding="utf-8-sig")
        self.assertIn("path = _absolute(path)", source)


if __name__ == "__main__":
    unittest.main()
