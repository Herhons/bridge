"""R4 / opencode `read_effort` 下限回到 0.85：**边界**回归。

背景（房主 2026-11-07 R4 明令）：R3 把 use 的 read_effort 从"绝对 0.85"
改成 choose 的"≥0.80 且领先 0.15"，等于把档位下限降了 0.05。现在两边都回到
**0.85**，并且必须继续逐字一致（tests/test_r3_choose_use_lockstep 会核对 AST）。

这里不是复述实现，是把边界钉死：
    · 0.80 / 0.82 / 0.845 / 0.8499 —— 哪怕领先第二名 0.7，也必须判 unknown
      （0.845 是实测里"Low"最常见的分数，以前正是它逼着把门槛降到 0.80）
    · 0.85 + 明显领先 —— 必须判成那一档
    · 分数够但**领先不够**（0.09 / 0.14）—— 仍然 unknown（分离边距没被放松）
    · 这些结论同样要能传到 `_reread_tier`（value=None → 发送前拒发）

做法：`find()` 直接回**指定分数**的命中框，不靠图像混合去"凑"一个分数——
边界用例必须可复现到小数位，不能因为 cv2 版本/插值差一点就飘。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "src" / "scripts", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import test_r3_opencode_use_path as R3  # noqa: E402
import opencode_choose as CHOOSE  # noqa: E402

USE = R3.USE
WIN = R3.WIN
SCRIPTS = ROOT / "src" / "scripts"
TPL_E = {"default": "oc_e_default", "low": "oc_e_low",
         "high": "oc_e_high", "max": "oc_e_max"}
BOTH = (("choose", CHOOSE), ("use", USE))


class ScoreDesk:
    """`find()` 返回**指定分数**的命中框；`find_all` 返回 2 个 ⌄。"""

    def __init__(self, scores, *, chips=2):
        self.scores = dict(scores)
        self.chips = chips
        self.asked = []

    def find(self, name, threshold=0.0, region=None):
        self.asked.append((name, threshold, region))
        if name == "oc_send":
            return (1051, 421, 30, 30, 1.0)
        if name in self.scores:
            score = self.scores[name]
            return None if score is None else (100, 430, 40, 16, float(score))
        return None

    def find_all(self, name, threshold=0.85, region=None, nms=6):
        if name == "oc_chevron":
            return [(476, 436, 0.99), (565, 436, 0.99)][:self.chips]
        return []

    def grab(self, region=None, *, fresh=False):
        raise AssertionError("read_effort 不该抓图")


def desk(*, default=0.0, low=0.0, high=0.0, max=0.0, chips=2):
    return ScoreDesk({"oc_e_default": default, "oc_e_low": low,
                      "oc_e_high": high, "oc_e_max": max}, chips=chips)


class FloorIs085(unittest.TestCase):
    """**低于 0.85 一律 unknown**，领先多少都不行。"""

    def test_scores_below_the_floor_are_refused_even_with_a_huge_lead(self):
        for score in (0.80, 0.82, 0.845, 0.8499):
            for name, mod in BOTH:
                with self.subTest(score=score, module=name):
                    got = mod.read_effort(desk(low=score), WIN)
                    self.assertEqual(got["effort"], "unknown", got)
                    self.assertNotIn("label", got, "没确认就不许给档位标签")

    def test_exactly_at_the_floor_is_accepted_with_a_clear_margin(self):
        for name, mod in BOTH:
            with self.subTest(module=name):
                got = mod.read_effort(desk(low=0.85, default=0.10), WIN)
                self.assertEqual(got["effort"], "low", got)
                self.assertEqual(got["label"], "Low")
                self.assertAlmostEqual(got["score"], 0.85, places=6)

    def test_a_high_score_without_margin_is_still_refused(self):
        """分数 0.95 够高，但第二档 0.86（只领先 0.09）分不清是哪一档。"""
        for name, mod in BOTH:
            with self.subTest(module=name):
                got = mod.read_effort(desk(high=0.95, low=0.86), WIN)
                self.assertEqual(got["effort"], "unknown", got)

    def test_margin_of_014_is_not_enough(self):
        for name, mod in BOTH:
            with self.subTest(module=name):
                got = mod.read_effort(desk(high=0.90, low=0.76), WIN)
                self.assertEqual(got["effort"], "unknown", got)

    def test_clear_separation_above_the_floor_is_accepted(self):
        for name, mod in BOTH:
            with self.subTest(module=name):
                got = mod.read_effort(desk(high=0.95, low=0.60), WIN)
                self.assertEqual(got["effort"], "high", got)

    def test_no_variant_chip_keeps_none(self):
        """GLM 那种只有一个 ⌄ 的：仍是 none（no-effort 语义不变）。"""
        for name, mod in BOTH:
            with self.subTest(module=name):
                got = mod.read_effort(desk(low=0.99, chips=1), WIN)
                self.assertEqual(got["effort"], "none", got)


class GuardUsesTheSameFloor(unittest.TestCase):
    """0.845 这种分数不只 read_effort 拒，`_reread_tier` 也要折成 None（拒发）。"""

    def test_below_floor_becomes_none_for_the_gate(self):
        got = USE._reread_tier(desk(low=0.845), WIN, "effort")
        self.assertIsNone(got["value"], got)

    def test_at_floor_with_margin_passes_to_the_gate(self):
        got = USE._reread_tier(desk(low=0.90, default=0.10), WIN, "effort")
        self.assertEqual(got["value"], "low", got)


class SourceKeepsBothFilesAt085(unittest.TestCase):
    """源码层再钉一次：两边都必须是 0.85，且都不许再出现 0.80 这个下限。"""

    def numeric_constants(self, name):
        path = SCRIPTS / ("opencode_%s.py" % ("choose" if name == "choose" else "use"))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "read_effort")
        nums = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)) \
                    and not isinstance(sub.value, bool):
                nums.add(round(float(sub.value), 6))
        return nums, ast.dump(node)

    def test_both_files_pin_the_floor_at_085_and_keep_the_margin(self):
        for name in ("choose", "use"):
            with self.subTest(module=name):
                nums, _ = self.numeric_constants(name)
                self.assertIn(0.85, nums, f"{name}.read_effort 的下限不是 0.85")
                self.assertIn(0.15, nums, f"{name}.read_effort 的分离边距没了")
                self.assertNotIn(0.8, nums,
                                 f"{name}.read_effort 仍带着 0.80（房主明令不许降）")

    def test_the_two_copies_are_still_ast_identical(self):
        _, choose_src = self.numeric_constants("choose")
        _, use_src = self.numeric_constants("use")
        self.assertEqual(choose_src, use_src,
                         "read_effort 在 choose/use 里已经不一致了")


if __name__ == "__main__":
    unittest.main()
