"""S2：收卷剔除尾部 prompt 回声。"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import importlib.util                              # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)                   # noqa: E402

PROMPT = "附件 material.md 是规格全文。首行写 可访问=是。给 6 条意见，按收益排序。"


class EchoTests(unittest.TestCase):
    def test_tail_echo_is_dropped(self):
        blocks = ["第 5 节「交给四家，走完整管道」", "问题：这里有问题",
                  "附件 material.md 是规格全文。", "首行写 可访问=是。",
                  "给 6 条意见，按收益排序。"]
        kept = iterate._drop_prompt_echo(blocks, PROMPT)
        self.assertEqual(kept, ["第 5 节「交给四家，走完整管道」", "问题：这里有问题"])

    def test_body_is_untouched(self):
        blocks = ["可访问=是", "这条不是提问的一部分，必须留着"]
        self.assertEqual(iterate._drop_prompt_echo(blocks, PROMPT), blocks)

    def test_citation_block_is_exempt(self):
        # 逐字引文即使与提问重合也必须保留（否则验真归零）
        quote = "「附件 material.md 是规格全文。」"
        blocks = ["问题：这里引用一句", quote]
        kept = iterate._drop_prompt_echo(blocks, PROMPT + quote)
        self.assertIn(quote, kept)

    def test_empty_prompt_is_noop(self):
        blocks = ["任意内容"]
        self.assertEqual(iterate._drop_prompt_echo(blocks, ""), blocks)


if __name__ == "__main__":
    unittest.main()
