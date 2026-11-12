"""S4：推进器骨架（只做交接与续跑）。"""
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import importlib.util                              # noqa: E402

_spec = importlib.util.spec_from_file_location("sisyphus_tool", ROOT / "tools" / "sisyphus.py")
sisyphus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sisyphus)                  # noqa: E402

PLAN = """# 计划

| ID | 项 | 落点 | 验收 |
|---|---|---|---|
| S1 | 安全原语 | src/qi/safe_ui.py | 用例通过 |
| S2 | 回声过滤 | tools/iterate.py | 用例通过 |
"""


class ParseTests(unittest.TestCase):
    def test_parse_plan(self):
        items = sisyphus.parse_plan(PLAN)
        self.assertEqual([i["id"] for i in items], ["S1", "S2"])
        self.assertEqual(items[0]["title"], "安全原语")

    def test_hard_stop_words(self):
        self.assertIsNone(sisyphus.hard_stop({"title": "安全原语"}))
        self.assertEqual(sisyphus.hard_stop({"title": "要花钱买额度"}), "花钱")
        self.assertEqual(sisyphus.hard_stop({"title": "改 F://Bridge//v0.0.8"}), "v0.0.8")


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        rdir = root / "docs" / "iterations" / "round-1"
        rdir.mkdir(parents=True)
        (rdir / "plan.md").write_text(PLAN, encoding="utf-8")
        (rdir / "verification.md").write_text(
            "| # | 项 | 判定 |\n|---|---|---|\n| S1 | 安全原语 | **成立** |\n"
            "| S2 | 回声过滤 | 部分成立 |\n| S3 | 要登录账号 | 成立 |\n", encoding="utf-8")
        self.root = root
        self._old = sisyphus.ROOT
        sisyphus.ROOT = root

    def tearDown(self):
        sisyphus.ROOT = self._old

    def test_next_then_done_then_next(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(sisyphus.main(["next", "--round", "round-1"]), 0)
            self.assertEqual(sisyphus.main(["done", "--round", "round-1", "--id", "S1"]), 0)
        state = json.loads((self.root / "docs/iterations/round-1/sisyphus.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["items"]["S1"]["status"], "done")
        # 下一个应该是 S2
        items = sisyphus.parse_plan(PLAN)
        pending = [i["id"] for i in items
                   if (state["items"].get(i["id"]) or {}).get("status") != "done"]
        self.assertEqual(pending, ["S2"])

    def test_three_attempts_stop(self):
        state = {"schema": "sisyphus/1",
                 "items": {"S1": {"status": "pending", "attempts": 3}}}
        (self.root / "docs/iterations/round-1/sisyphus.json").write_text(
            json.dumps(state), encoding="utf-8")
        with mock.patch("sys.stdout"):
            self.assertEqual(sisyphus.main(["next", "--round", "round-1"]), 5)

    def test_hard_stop_blocks_next(self):
        (self.root / "docs/iterations/round-1/plan.md").write_text(
            PLAN + "| S3 | 要登录账号 | x | y |\n", encoding="utf-8")
        with mock.patch("sys.stdout"):
            self.assertEqual(sisyphus.main(["done", "--round", "round-1", "--id", "S1"]), 0)
            self.assertEqual(sisyphus.main(["done", "--round", "round-1", "--id", "S2"]), 0)
            self.assertEqual(sisyphus.main(["next", "--round", "round-1"]), 3)


if __name__ == "__main__":
    unittest.main()
