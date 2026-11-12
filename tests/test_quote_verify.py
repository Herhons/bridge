"""引用核对口径的离线夹具（Round 0 修复第 2 项）。

锁死四条规则（Herhons 2026-11-12 批准，规格 §9.1 未定义分母，这里补空白）：

1. 分母 = **结构化意见条数**，不是全文引用总数；
2. 一条意见**至少一处有效引用**才算命中；
3. **光写章节号不算命中**（必须有逐字引句或有效路径）；
4. **重复引句只算一次**，后面的复用不算命中。

阈值不变：真读 = 命中 ≥ 3 且命中率 ≥ 0.7 且至少一条落在核心位置。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import iteration as it                    # noqa: E402

SPEC = """# 自迭代循环规格

## 5. Round 0

把已提交的 `docs/self_iteration_spec.md` 交给四家，走完整管道。

## 8. 材料投喂

网页端上传是循环的必备能力，须在 Round 0 修通。

## 6. 代码轮

修复 + 全量离线单测 + 本轮回归测试。

## 7.1 锁

严格串行：上一席发出并读完回复后，才开下一席。

## 9.1 引用验真

真读 = 命中 ≥ 3 且命中率 ≥ 0.7。

## 10. 计划

计划必须在修复之前落盘。

## 12. 停机条件

满足任意一条即停，通知 Herhons。
"""

TREE = ["docs/self_iteration_spec.md", "src/qi/server.py", "src/qi/upload.py"]

Q1 = "交给四家，走完整管道"
Q2 = "网页端上传是循环的必备能力，须在 Round 0 修通"
Q3 = "满足任意一条即停，通知 Herhons"
Q4 = "真读 = 命中 ≥ 3 且命中率 ≥ 0.7"
Q5 = "修复 + 全量离线单测 + 本轮回归测试"
Q6 = "计划必须在修复之前落盘"


def check(reply):
    return it.verify_reply(reply, tree_paths=TREE, spec_text=SPEC,
                           core_paths=(it.SPEC_PATH,))


class DenominatorTests(unittest.TestCase):
    def test_denominator_is_opinion_count(self):
        """6 条意见、5 条有有效引句 → 5/6 = 0.83，而不是按全文引用总数算。"""
        reply = "\n".join([
            f"1. 第 5 节「{Q1}」",
            f"2. 第 8 节「{Q2}」",
            f"3. 第 12 节「{Q3}」",
            f"4. 第 9.1 节「{Q4}」",
            f"5. 第 5 节「{Q5}」",
            "6. 第 9 节「这句话在规格里根本不存在」",
        ])
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (5, 6), out)
        self.assertEqual(out["rate"], 0.83)
        self.assertEqual(out["verdict"], "true_read")

    def test_one_hit_per_opinion_even_with_many_quotes(self):
        """一条里引三句只算这一条命中，不会把分母撑大。"""
        reply = (f"1. 第 5 节「{Q1}」第 8 节「{Q2}」第 12 节「{Q3}」\n"
                 f"2. 第 5 节「{Q5}」")
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (2, 2), out)


class SectionOnlyTests(unittest.TestCase):
    def test_section_number_alone_is_not_a_hit(self):
        """只写「§8」不给引句 → 这条不计命中。"""
        reply = "\n".join([
            "1. §8 这一节有问题，建议改。",
            f"2. 第 5 节「{Q1}」",
            f"3. 第 12 节「{Q3}」",
        ])
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (2, 3), out)
        self.assertFalse(out["rows"][0]["hit"], "光写章节号不该算命中")
        self.assertEqual(out["verdict"], "partial", "2/3=0.67 不到 0.7")

    def test_preamble_with_bare_filename_is_not_an_opinion(self):
        """「（全文已读：xxx.md）」这种前言不能凑成一条意见。"""
        out = check("（全文已读：docs/self_iteration_spec.md）")
        self.assertEqual(out["total"], 0, out)
        self.assertEqual(out["verdict"], "unread")


class DedupeTests(unittest.TestCase):
    def test_duplicate_quote_counts_once(self):
        """同一句引文复用三次 → 只算 1 条命中。"""
        reply = "\n".join([
            f"1. 第 5 节「{Q1}」",
            f"2. 第 5 节「{Q1}」",
            f"3. 第 5 节「{Q1}」",
        ])
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (1, 3), out)
        self.assertEqual(out["rows"][1]["quotes"][0].get("dup_of"), 1)
        self.assertEqual(out["verdict"], "unread")

    def test_duplicate_does_not_rescue_a_weak_opinion(self):
        """某条只有重复引句 → 这条不算命中（复用的那句不再加分）。"""
        reply = "\n".join([
            f"1. 第 5 节「{Q1}」",
            f"2. 第 8 节「{Q2}」",
            f"3. 第 5 节「{Q1}」另外还有个问题。",
        ])
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (2, 3), out)
        self.assertFalse(out["rows"][2]["hit"])


class TruncatedQuoteTests(unittest.TestCase):
    def test_truncated_quote_still_misses(self):
        """尾部截断的引句按现状口径不算命中（本轮不改这一条）。"""
        reply = "\n".join([
            "1. 第 5.3 节「真读 = 命中 ≥ 3 且命中率 ≥ 0.7，且至少一条落在 docs/self」",
            "2. 第 8 节「网页端上传是循环的必备能力，须在 Round 0 修通并留下记录」",
            "3. 第 12 节「满足任意一条就停止，通知 Herhons」",
        ])
        out = check(reply)
        self.assertEqual(out["hits"], 0, out)
        self.assertEqual(out["verdict"], "unread")


class SplitTests(unittest.TestCase):
    def test_gemini_style_single_paragraph_is_split_by_anchor(self):
        """一整段里用「第 X 节…原文依据：」逐条展开，也要按条切。"""
        reply = (f"第 8 节与第 5 节原文依据：「{Q2}」为什么是问题：A。"
                 f"第 12 节原文依据：「{Q3}」为什么是问题：B。"
                 f"第 9.1 节原文依据：「{Q4}」为什么是问题：C。")
        blocks = it.split_opinions(reply)
        self.assertEqual(len(blocks), 3, blocks)
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (3, 3), out)
        self.assertEqual(out["verdict"], "true_read")

    def test_numbered_list_is_split_by_number(self):
        reply = f"1. 第 5 节「{Q1}」\n2. 第 8 节「{Q2}」\n3. 第 12 节「{Q3}」"
        self.assertEqual(len(it.split_opinions(reply)), 3)


class PathCitationTests(unittest.TestCase):
    def test_path_with_line_is_an_opinion_and_can_hit(self):
        reply = f"1. 第 5 节「{Q1}」\n2. src/qi/server.py:120 这里也要改"
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (2, 2), out)
        self.assertTrue(out["rows"][1]["paths"][0]["hit"])

    def test_missing_path_opinion_misses(self):
        reply = ("1. 第 5 节「" + Q1 + "」\n"
                 "2. src/qi/nope.py:9 这里也要改\n"
                 "3. 第 12 节「" + Q3 + "」")
        out = check(reply)
        self.assertEqual((out["hits"], out["total"]), (2, 3), out)
        self.assertFalse(out["rows"][1]["hit"])

    def test_no_access_reply_shape_is_unread(self):
        out = check("可访问=否\n仓库与附件在当前环境无法读取，我手上只有部分文本。")
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["verdict"], "unread")


if __name__ == "__main__":
    unittest.main()
