"""干跑：夹具规格 + 夹具回复 → 验真 → 匿名包 → 归档（规格 §14.2）。"""
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 按文件路径加载 tools/iterate.py：**不能**把 tools/ 放进 sys.path。
# tools/ 里有 test_all.py、test_writer.py，会让 unittest 发现阶段导入错模块
# （实测：全量 19 条红，单跑却全绿）。
import importlib.util                             # noqa: E402
_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)                  # noqa: E402

SPEC = ("# 自迭代规格\n\n## 5. Round 0\n\n把已提交的 `docs/self_iteration_spec.md` "
        "交给四家，走完整管道。\n\n## 8. 材料投喂\n\n网页端上传是循环的必备能力。\n\n"
        "## 12. 停机条件\n\n满足任意一条即停，通知 Herhons。\n")

# 2026-11-12 起验真按「结构化意见条数」计分母：每条至少一处有效引句才算命中，
# 所以夹具要写成 3 条独立意见（以前两句话就能靠路径凑够 3 个 hit）。
GOOD = ('可访问=是\n1. 第 5 节「交给四家，走完整管道」\n'
        '2. 第 8 节「网页端上传是循环的必备能力」\n'
        '3. 第 12 节「满足任意一条即停，通知 Herhons」\n'
        'docs/self_iteration_spec.md:108\n')
BAD = "可访问=是\n第 9 节「这句话不存在于规格」\n"


class DryRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "docs" / "iterations" / "round-0" / "replies").mkdir(parents=True)
        (root / "docs" / "self_iteration_spec.md").write_text(SPEC, encoding="utf-8")
        (root / "docs" / "iterations" / "round-0" / "prompt.md").write_text("审规格", encoding="utf-8")
        (root / "docs" / "iterations" / "round-0" / "tree.txt").write_text(
            "docs/self_iteration_spec.md\nsrc/qi/server.py\n", encoding="utf-8")
        for name, text in (("chatgpt", GOOD), ("grok", GOOD), ("gemini", GOOD), ("deepseek", BAD)):
            (root / "docs" / "iterations" / "round-0" / "replies" / f"{name}.md").write_text(
                text, encoding="utf-8")
        self.root = root
        self._old_root = iterate.ROOT
        iterate.ROOT = root

    def tearDown(self):
        iterate.ROOT = self._old_root

    def test_verify_anonymize_archive(self):
        args = SimpleNamespace(round="round-0", site=None, force=False)
        code = iterate.cmd_verify(args, iterate.load_config())
        self.assertEqual(code, 0, "三家真读应满足门槛")

        qc = json.loads((self.root / "docs/iterations/round-0/quote_check.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(qc["summary"]["valid_count"], 3)
        self.assertTrue(qc["summary"]["meets_three_vendors"])

        self.assertEqual(iterate.cmd_anonymize(args, iterate.load_config()), 0)
        pack = (self.root / "docs/iterations/round-0/anonymous_pack.md").read_text(encoding="utf-8")
        self.assertIn("F1", pack)
        for vendor in ("chatgpt", "grok", "gemini", "deepseek"):
            self.assertNotIn(vendor, pack.lower())
        self.assertTrue((self.root / "docs/iterations/round-0/quote_check.md").is_file())


if __name__ == "__main__":
    unittest.main()
