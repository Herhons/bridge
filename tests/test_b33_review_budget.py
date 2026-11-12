"""B33：`iterate.py` 的会审预算必须过 `tier_guard.floor_timeout`。

规格 §7.1：`purpose=review` 会审走 `REVIEW_TIMEOUT` 下限，不得把高档会审判成小超时。
`*_use.py` 的 CLI 入口本来就会 floor，但 `tools/iterate.py` 是**直接**调
`conversation.ask` / `subprocess.run` 的，绕过了那一层。

2026-11-12 实测两处后果：
  1. `docs/iteration.json` 写 `review_timeout: 480`，`conversation.ask` 就在 480s
     收工（Round 4 chatgpt 跑 582s 报 `stage=timeout`、0 字，正落在这条线上）；
  2. 同一份配置交给 `gemini_use.py` 时子进程自己抬到 1500s，而父进程
     `subprocess.run(timeout=budget+180)` 只有 660s —— **父进程先杀掉子进程**，
     答案就在页面上却收不回来。
"""
from offline_guard import ensure_offline
ensure_offline()

import importlib.util
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import tier_guard                              # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)

SOURCE = (ROOT / "tools" / "iterate.py").read_text(encoding="utf-8-sig")


class BudgetFloor(unittest.TestCase):
    def test_small_config_is_raised_to_the_review_floor(self):
        self.assertEqual(iterate._review_budget({"review_timeout": 480}),
                         int(tier_guard.REVIEW_TIMEOUT))

    def test_larger_config_is_kept(self):
        bigger = int(tier_guard.REVIEW_TIMEOUT) + 600
        self.assertEqual(iterate._review_budget({"review_timeout": bigger}), bigger)

    def test_missing_or_bad_config_still_floors(self):
        for bad in ({}, {"review_timeout": None}, {"review_timeout": "abc"}):
            self.assertGreaterEqual(iterate._review_budget(bad),
                                    int(tier_guard.REVIEW_TIMEOUT), bad)

    def test_default_budget_is_not_below_the_floor(self):
        """默认配置本身也不能低于下限，否则 default 与 floor 会互相打架。"""
        self.assertGreaterEqual(iterate._review_budget(iterate.DEFAULT_CONFIG),
                                int(tier_guard.REVIEW_TIMEOUT))


class NoRawReviewTimeout(unittest.TestCase):
    """防回归：谁再把裸 `cfg["review_timeout"]` 递给 ask/subprocess，这里就红。"""

    def test_no_raw_review_timeout_in_calls(self):
        offenders = [
            f"{i}:{line.strip()}"
            for i, line in enumerate(SOURCE.splitlines(), 1)
            if 'cfg["review_timeout"]' in line or "cfg['review_timeout']" in line
        ]
        self.assertEqual(offenders, [], "会审预算必须走 _review_budget(cfg)")

    def test_only_the_last_chunk_gets_the_review_budget(self):
        """分段粘贴：前面几段只等"贴进去"，最后一段才等答案，不能都按 1500s 等。"""
        self.assertIn("_review_budget(cfg) if index == total", SOURCE)
        self.assertIn("_review_budget(cfg) if index == len(chunks)", SOURCE)


if __name__ == "__main__":
    unittest.main()


class SubprocessTimeout(unittest.TestCase):
    """子进程超时必须变成结构化失败，不能把 `TimeoutExpired` 抛给调用方。

    原来四处 `subprocess.run(timeout=…)` 都没接：子进程卡住时（UI 锁死、网页端
    不返回）整个 `review` / `summarize` 以 traceback 收场，归档里一行结果都没有，
    事后连"是超时还是崩了"都分不清。
    """

    def test_timeout_becomes_a_result_not_an_exception(self):
        import subprocess as sp

        boom = sp.TimeoutExpired(cmd=["x"], timeout=5)
        with unittest.mock.patch.object(iterate.subprocess, "run", side_effect=boom):
            proc, err = iterate._run_or_timeout(["x"], 5, "测试")
        self.assertIsNone(proc)
        self.assertEqual(err["stage"], "timeout")
        self.assertEqual(err["timeout_kind"], "hard")
        self.assertIn("测试", err["hint"])

    def test_normal_run_is_passed_through(self):
        from types import SimpleNamespace

        fake = SimpleNamespace(stdout="{}\n", returncode=0)
        with unittest.mock.patch.object(iterate.subprocess, "run", return_value=fake):
            proc, err = iterate._run_or_timeout(["x"], 5, "测试")
        self.assertIsNone(err)
        self.assertIs(proc, fake)

    def test_every_long_call_goes_through_the_helper(self):
        """防回归：不许再有裸的 `subprocess.run(... timeout=...)`。"""
        import re

        lines = SOURCE.splitlines()
        bad = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 只看真正的调用语句：注释/docstring/`return subprocess.run(…)`（helper 自己）不算
            if not stripped.startswith("subprocess.run("):
                continue
            window = "\n".join(lines[i - 1:i + 6])
            if "timeout=" not in window:
                continue
            if "except" in window:        # 已经在 try/except 里包好了
                continue
            bad.append(f"{i}:{stripped}")
        self.assertEqual(bad, [], "长调用必须走 _run_or_timeout")
