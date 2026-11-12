"""自迭代循环纯函数层的用例（规格 §13/§14）。"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import iteration as it                    # noqa: E402

SPEC = """# 自迭代循环规格

## 5. Round 0

把已提交的 `docs/self_iteration_spec.md` 交给四家，走完整管道。

## 8. 材料投喂

网页端上传是循环的必备能力。

## 12. 停机条件

满足任意一条即停，通知 Herhons。
"""


class StripTests(unittest.TestCase):
    def test_inline_double_star_is_kept(self):
        src = "def f():\n    return {**result, \"stage\": \"x\"}\n"
        self.assertIn("**result", it.strip_markdown(src))

    def test_headings_and_bullets_are_stripped(self):
        out = it.strip_markdown("# 标题\n\n- 条目\n> 引用\n")
        self.assertNotIn("# 标题", out)
        self.assertNotIn("- 条目", out)
        self.assertIn("条目", out)

    def test_code_fence_body_untouched(self):
        src = "```python\n# 不是标题\n```\n"
        out = it.strip_markdown(src)
        self.assertIn("# 不是标题", out)


class PackTests(unittest.TestCase):
    def test_chunks_carry_index_and_final_note(self):
        text = "\n".join(f"第 {i} 行内容" for i in range(60))
        chunks = it.pack_chunks(text, 200, head="头部说明\n")
        self.assertGreater(len(chunks), 1)
        self.assertIn("第 1/", chunks[0])
        self.assertIn("头部说明", chunks[0])
        self.assertIn("材料已全部发完", chunks[-1])
        self.assertNotIn("材料已全部发完", chunks[0])

    def test_every_chunk_within_limit(self):
        text = "x" * 50 + "\n" + "y" * 50 + "\n"
        for chunk in it.pack_chunks(text, 120):
            self.assertLess(len(chunk), 260, chunk[:60])

    def test_fence_is_closed_before_split(self):
        text = "```\n" + "\n".join("code line " * 5 for _ in range(10)) + "\n```\n"
        chunks = it.pack_chunks(text, 120)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0, "代码块被从中间切开")


class VerifyTests(unittest.TestCase):
    def _tree(self):
        return ["docs/self_iteration_spec.md", "src/qi/server.py", "src/qi/upload.py"]

    def test_true_read_for_spec_quotes(self):
        reply = ('可访问=是\n第 5 节「交给四家，走完整管道」\n'
                 '第 8 节「网页端上传是循环的必备能力」\n'
                 '第 12 节「满足任意一条即停，通知 Herhons」\n'
                 'src/qi/server.py:120 也要改\n')
        out = it.verify_reply(reply, tree_paths=self._tree(), spec_text=SPEC,
                              core_paths=(it.SPEC_PATH,))
        self.assertEqual(out["verdict"], "true_read", out)
        self.assertTrue(out["core"])

    def test_unread_when_nothing_matches(self):
        reply = "可访问=是\n第 5 节「这句根本不在规格里」\nsrc/qi/nope.py:1"
        out = it.verify_reply(reply, tree_paths=self._tree(), spec_text=SPEC,
                              core_paths=(it.SPEC_PATH,))
        self.assertEqual(out["verdict"], "unread", out)
        self.assertFalse(out["core"])

    def test_partial_when_some_hits(self):
        reply = ('第 5 节「把已提交的 docs/self_iteration_spec.md 交给四家」\n'
                 'src/qi/nope.py:9\n另外两个乱写的引用\n')
        out = it.verify_reply(reply, tree_paths=self._tree(), spec_text=SPEC,
                              core_paths=(it.SPEC_PATH,))
        self.assertIn(out["verdict"], ("partial", "unread"))

    def test_path_normalisation(self):
        self.assertEqual(it.normalize_path(r"src\qi\server.py:120"),
                         "src/qi/server.py")
        self.assertEqual(
            it.normalize_path("https://github.com/x/y/blob/main/src/qi/upload.py"),
            "src/qi/upload.py")


class AnonymizeTests(unittest.TestCase):
    def test_vendor_names_are_gone(self):
        replies = {
            "chatgpt": "ChatGPT 认为 src/qi/server.py:1 有问题，我作为 ChatGPT 说……",
            "grok": "Grok 说没事。",
        }
        pack = it.anonymize(replies, verdicts={"chatgpt": {"verdict": "true_read"},
                                               "grok": {"verdict": "true_read"}})
        for vendor in it.VENDORS:
            self.assertNotIn(vendor, pack.lower())
        self.assertIn("F1", pack)

    def test_unread_replies_are_dropped(self):
        replies = {"a": "内容甲", "b": "内容乙"}
        pack = it.anonymize(replies, verdicts={"a": {"verdict": "true_read"},
                                               "b": {"verdict": "unread"}})
        self.assertIn("内容甲", pack)
        self.assertNotIn("内容乙", pack)


class StateTests(unittest.TestCase):
    def test_mark_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = it.State.load(tmp, "round-0")
            state.mark("snapshot", {"sha": "abc"})
            again = it.State.load(tmp, "round-0")
            self.assertTrue(again.done("snapshot"))
            self.assertEqual(again.done("snapshot")["sha"], "abc")


if __name__ == "__main__":
    unittest.main()
