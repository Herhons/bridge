"""B21：`lastreply` 不能再把回复拼成"一词一行"的竖排。

真实现场：`evidence/card09e_probe_chatgpt.txt` 的回复气泡有 200 个叶子文本，
旧实现用 `"\\n".join(...)` 直接拼，读出来是 200 行、每行一个词。
死代码 `_rows_to_text` 正是为这件事写的，这里把它接上并钉住两条不变量：

1. 只改**分行**，不改**字符**——去掉换行后必须逐字相等；
2. 同一行里保持**文档序**，不按 x 重排（实测按 x 排会让中文乱码）。
"""
from offline_guard import ensure_offline
ensure_offline()

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import lastreply, ui                          # noqa: E402

FIXTURE = ROOT / "evidence" / "card09e_probe_chatgpt.txt"
LINE = re.compile(r"^(TextControl)\t'(.*)'\t(-?\d+),(-?\d+) (\d+)x(\d+)\t")


def bubble_items():
    """真实气泡段（283–648 行）的叶子几何。"""
    items = []
    for line in FIXTURE.read_text(encoding="utf-8-sig").splitlines()[282:648]:
        hit = LINE.match(line)
        if not hit:
            continue
        text = hit.group(2)
        x, y = int(hit.group(3)), int(hit.group(4))
        w, h = int(hit.group(5)), int(hit.group(6))
        if not text.strip() or w <= 0:
            continue
        items.append((text, x, y, x + w, y + h))
    return items


class RealBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = bubble_items()
        cls.flat = "\n".join(text for text, *_ in cls.items)
        cls.rows = lastreply._rows_to_text(cls.items)

    def test_fixture_is_the_real_one(self):
        self.assertEqual(len(self.items), 200, "夹具变了？先确认不是换了一份 dump")

    def test_real_bubble_is_not_vertical(self):
        lines = self.rows.splitlines()
        self.assertLessEqual(len(lines), 60, f"还是竖排：{len(lines)} 行")
        self.assertEqual([ln for ln in lines if len(ln) == 1], [], "出现了单字行")
        self.assertGreater(max(len(ln) for ln in lines), 40)

    def test_real_bubble_preserves_every_character(self):
        """只改分行，不改内容——这条比行数更重要。"""
        self.assertEqual(self.flat.replace("\n", ""), self.rows.replace("\n", ""))

    def test_bullets_get_their_own_lines(self):
        lines = self.rows.splitlines()
        bullets = [ln for ln in lines if ln.startswith("•")]
        self.assertGreaterEqual(len(bullets), 2)
        self.assertNotIn("token。•", self.rows, "bullet 被串到上一行末尾了")

    def test_paragraph_numbers_not_garbled(self):
        """数字开头的段落不能被行内重排搅乱。"""
        self.assertTrue(any(ln.startswith("1.") for ln in self.rows.splitlines()), self.rows[:200])


class RowsUnitTests(unittest.TestCase):
    def test_same_row_keeps_document_order(self):
        """四个叶子在同一视觉行 → 拼成一行，且按传入顺序（不是 x）。"""
        items = [("丙", 300, 0, 320, 20), ("甲", 0, 0, 20, 20),
                 ("丁", 400, 0, 420, 20), ("乙", 100, 0, 120, 20)]
        self.assertEqual(lastreply._rows_to_text(items), "丙甲丁乙")

    def test_different_rows_are_split(self):
        items = [("第一行", 0, 0, 60, 20), ("第二行", 0, 40, 80, 60)]
        self.assertEqual(lastreply._rows_to_text(items), "第一行\n第二行")

    def test_half_overlap_counts_as_same_row(self):
        items = [("甲", 0, 0, 20, 20), ("乙", 40, 10, 60, 30)]
        self.assertEqual(lastreply._rows_to_text(items), "甲乙")

    def test_slight_overlap_is_a_new_row(self):
        """只压到一点边（< 50%）不算同一行，否则 bullet 会和上一行连起来。"""
        items = [("甲", 0, 0, 20, 20), ("乙", 40, 18, 60, 38)]
        self.assertEqual(lastreply._rows_to_text(items), "甲\n乙")

    def test_empty_items(self):
        self.assertEqual(lastreply._rows_to_text([]), "")


class LeafTextTests(unittest.TestCase):
    def test_node_without_control_is_skipped(self):
        """离线夹具可能只给名字、不给 control —— 不许抛。"""
        group = ui.Node("DocumentControl", "", None, None)
        group.children.append(ui.Node("TextControl", "有字但没几何", None, group))
        self.assertEqual(lastreply._leaf_texts(group, None), [])

    def test_control_with_broken_rect_is_skipped(self):
        group = ui.Node("DocumentControl", "", None, None)
        broken = ui.Node("TextControl", "坏的", SimpleNamespace(BoundingRectangle=None), group)
        group.children.append(broken)
        self.assertEqual(lastreply._leaf_texts(group, None), [])


if __name__ == "__main__":
    unittest.main()

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/card09e_probe_chatgpt.txt',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
