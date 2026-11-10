"""B14：`required_dimensions` 的端必须**一次设完所有维度**。

背景（2026-11-08 核实）：
- 正式目录 `F:\\Bridge\\v0.0.9` 的 `tiers.json` 里 chatgpt 没有 `required_dimensions`，
  所以那时"只设模型"就能过 —— 台账里 11 次成功。
- WIP 里给 chatgpt 加了 `required_dimensions:["model","effort"]` + `require_writer`，
  闸门要求两维在同一张 `composite_version=1` 回执里且都读回一致。
- 而服务端 `_pick_targets` 原来只返回第一个维度 ⇒ 第二维靠 `chatgpt_choose.py`
  拿"页面当前值"补 ⇒ 能不能过取决于页面当时是什么档，不稳定。

这些用例钉住"拼参"这一层；真机仍要单独验（改的是会点菜单的路径）。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import server  # noqa: E402

TIERS = {"endpoints": {
    "chatgpt": {
        "dimensions": {
            "model": {"test": ["m1"], "review": ["m2"],
                      "writers": {"chat": {"test": ["c1"], "review": ["c2"]}}},
            "effort": {"test": ["low"], "review": ["max"],
                       "writers": {"chat": {"test": ["instant"], "review": ["high"]}}},
        },
        "required_dimensions": ["model", "effort"],
        "require_writer": True,
    },
    "codex": {
        "dimensions": {"model": {"test": ["c1"]}, "effort": {"test": ["high"]}},
    },
    "halfdone": {
        "dimensions": {"model": {"test": ["h1"]}, "effort": {}},
        "required_dimensions": ["model", "effort"],
    },
}}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
        self.calls.append(list(cmd))
        return {"ok": True, "timeout": False, "exit_code": 0,
                "stdout": '{"ok": true, "stage": "done"}', "stderr": "", "elapsed": 0.1}


def make_engine():
    tmp = Path(tempfile.mkdtemp(prefix="b14-md-"))
    (tmp / "docs").mkdir()
    (tmp / "docs" / "tiers.json").write_text(json.dumps(TIERS), encoding="utf-8")
    scripts = tmp / "src" / "scripts"
    scripts.mkdir(parents=True)
    for name in TIERS["endpoints"]:
        (scripts / f"{name}_choose.py").write_text("", encoding="utf-8")
    runner = Recorder()
    return server.Engine(root=tmp, token="x", port=0, runner=runner), runner


class PickTargetsTests(unittest.TestCase):
    def test_declared_dimensions_are_all_returned_in_order(self):
        engine, _ = make_engine()
        self.assertEqual(engine._pick_targets("chatgpt", "test"),
                         [("model", "m1"), ("effort", "low")])
        self.assertEqual(engine._pick_targets("chatgpt", "review"),
                         [("model", "m2"), ("effort", "max")])

    def test_endpoint_without_declaration_keeps_old_single_dimension_behaviour(self):
        """codex 也有 model+effort 两维，但没声明 required —— 不许顺手去设 effort。"""
        engine, _ = make_engine()
        self.assertEqual(engine._pick_targets("codex", "test"), [("model", "c1")])

    def test_declared_dimension_with_no_tier_is_an_error(self):
        engine, _ = make_engine()
        with self.assertRaises(server.EndpointFailure) as ctx:
            engine._pick_targets("halfdone", "test")
        self.assertEqual(ctx.exception.code, "tier_table_empty")

    def test_primary_target_is_still_the_first_dimension(self):
        engine, _ = make_engine()
        self.assertEqual(engine._pick_target("chatgpt", "test"), ("model", "m1"))


class ChooseArgvTests(unittest.TestCase):
    def test_two_dimension_endpoint_gets_both_flags(self):
        engine, runner = make_engine()
        pairs = engine._pick_targets("chatgpt", "test")
        out = engine._run_choose("chatgpt", "model", "m1", time.monotonic() + 30,
                                 targets=pairs)
        cmd = runner.calls[0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "m1")
        self.assertIn("--effort", cmd, "缺了 effort 就写不出联合回执")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(out["targets"], {"model": "m1", "effort": "low"})

    def test_single_dimension_endpoint_keeps_one_flag(self):
        engine, runner = make_engine()
        engine._run_choose("codex", "model", "c1", time.monotonic() + 30)
        cmd = runner.calls[0]
        self.assertIn("--model", cmd)
        self.assertNotIn("--effort", cmd)

    def test_choose_records_the_primary_dimension_for_the_job(self):
        """job 里仍然要有 dimension/target 两个老字段（面板与既有测试都在读）。"""
        engine, _ = make_engine()
        out = engine._run_choose("chatgpt", "model", "m1", time.monotonic() + 30,
                                 targets=[("model", "m1"), ("effort", "low")])
        self.assertEqual(out["dimension"], "model")
        self.assertEqual(out["target"], "m1")


class WriterAwareTests(unittest.TestCase):
    """chatgpt 有 chat / work 两套表，拿错表会被闸门判 purpose_mismatch。"""

    def test_chat_mode_uses_the_chat_table(self):
        engine, _ = make_engine()
        self.assertEqual(engine._pick_targets("chatgpt", "review", writer="chat"),
                         [("model", "c2"), ("effort", "high")])

    def test_work_mode_or_unknown_uses_the_default_table(self):
        engine, _ = make_engine()
        self.assertEqual(engine._pick_targets("chatgpt", "review", writer="work"),
                         [("model", "m2"), ("effort", "max")])
        self.assertEqual(engine._pick_targets("chatgpt", "review"),
                         [("model", "m2"), ("effort", "max")])

    def test_endpoint_without_writer_tables_is_unaffected(self):
        engine, _ = make_engine()
        self.assertEqual(engine._pick_targets("codex", "test", writer="chat"),
                         [("model", "c1")])


class DetectWriterTests(unittest.TestCase):
    def test_reads_the_writer_from_list_output(self):
        engine, _runner = make_engine()
        class Fake:
            def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
                return {"ok": True, "timeout": False, "exit_code": 0,
                        "stdout": '{"writer": "chat", "model": "unknown"}',
                        "stderr": "", "elapsed": 0.1}
        engine.runner = Fake()
        self.assertEqual(engine._detect_writer("chatgpt", time.monotonic() + 30), "chat")

    def test_unknown_writer_returns_none(self):
        engine, _ = make_engine()
        class Fake:
            def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
                return {"ok": True, "timeout": False, "exit_code": 0,
                        "stdout": '{"stage": "chip_not_found"}', "stderr": "", "elapsed": 0.1}
        engine.runner = Fake()
        self.assertIsNone(engine._detect_writer("chatgpt", time.monotonic() + 30))

    def test_only_endpoints_declaring_require_writer_are_probed(self):
        engine, _ = make_engine()
        self.assertTrue(engine._requires_writer("chatgpt"))
        self.assertFalse(engine._requires_writer("codex"))


if __name__ == "__main__":
    unittest.main()
