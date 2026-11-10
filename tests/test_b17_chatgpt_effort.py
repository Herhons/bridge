"""B17：chatgpt 新版界面的强度读数与两维回执。

2026-11-09 现场：芯片 Button 只剩「选择 ChatGPT 模型」一个名字、没有子
TextControl，`_chip_text` 永远空；同时强度弹层整体左移，`_slider` 的 cy
落到输入框底边（差 17px），点下去滑杆拿不到焦点，方向键一个都不生效。

契约：
1. 滑杆可访问值带「第 N 项，共 M 项」→ 按序号映射档位（「最高」含「高」，
   旧词串匹配会误判成 high）；
2. 「强度」MenuItemControl 是稳定锚点，点它的中心才能让滑杆拿到焦点；
3. 芯片读不到时，`set_effort` 退到滑杆读数；两维回执也用各维度设完后的
   独立读数（来源标 `post_action_readback`），不伪造芯片读数。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCRIPT = ROOT / "src" / "scripts" / "chatgpt_choose.py"


def load(names, namespace):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"), filename=str(SCRIPT))
    wanted = [node for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.ClassDef))
              and node.name in names]
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPT), "exec"), namespace)
    return namespace


class FakeControl:
    def __init__(self, rect, offscreen=False):
        self._rect = rect
        self.IsOffscreen = offscreen

    @property
    def BoundingRectangle(self):
        left, top, width, height = self._rect
        return types.SimpleNamespace(left=left, top=top,
                                     right=left + width, bottom=top + height)


class FakeNode:
    def __init__(self, kind, name, *, rect=(0, 0, 272, 33), offscreen=False):
        self.kind = kind
        self.name = name
        self.control = FakeControl(rect, offscreen)


class FakeRoot:
    def __init__(self, nodes):
        self._nodes = nodes

    def walk(self):
        return list(self._nodes)


WORDS = (("最高", "max"), ("极高", "xhigh"), ("即时", "instant"),
         ("轻度", "low"), ("中", "mid"), ("高", "high"), ("Max", "max"))


class AnnounceEffortTests(unittest.TestCase):
    def _fn(self, text, writer="work"):
        ns = {"re": __import__("re"), "_doc_root": lambda _s: FakeRoot([FakeNode("TextControl", text)]),
              "_CHIP_EFFORT_WORDS": WORDS,
              "effort_order": lambda w: ["low", "mid", "high", "xhigh", "max"]}
        # _effort_of_text 也要一起加载
        load({"_effort_of_text", "_announce_effort"}, ns)
        return ns["_announce_effort"](object(), writer=writer)

    def test_item_index_beats_word_matching(self):
        out = self._fn("最高，第 5 项，共 5 项。")
        self.assertEqual(out["effort"], "max")      # 旧实现会因「高」判成 high
        self.assertEqual(out["source"], "uia_slider")

    def test_first_item_is_low(self):
        self.assertEqual(self._fn("轻度，第 1 项，共 5 项。")["effort"], "low")

    def test_without_writer_falls_back_to_words(self):
        out = self._fn("最高，第 5 项，共 5 项。", writer=None)
        self.assertEqual(out["effort"], "max")


class EffortItemTests(unittest.TestCase):
    def _fn(self, nodes):
        ns = {"_doc_root": lambda _s: FakeRoot(nodes),
              "_offscreen": lambda node: bool(node.control.IsOffscreen)}
        load({"_effort_item"}, ns)
        return ns["_effort_item"](object())

    def test_returns_the_strength_row_center(self):
        item = self._fn([FakeNode("MenuItemControl", "强度", rect=(-544, 248, 272, 33))])
        self.assertIsNotNone(item)
        self.assertEqual(item["center"], (-408, 264))
        self.assertEqual(item["box"], (-544, 248, 272, 33))

    def test_offscreen_or_wrong_name_is_ignored(self):
        self.assertIsNone(self._fn([FakeNode("MenuItemControl", "强度", offscreen=True)]))
        self.assertIsNone(self._fn([FakeNode("MenuItemControl", "模型")]))


class CompositeReceiptTests(unittest.TestCase):
    def test_falls_back_to_post_action_readbacks(self):
        from qi import chip, tier_guard

        empty = {"writer": "work", "dimensions": {"model": None, "effort": None}}
        saved = {"written": True}
        namespace = {"_record_receipt": lambda *a, **k: None}
        load({"_record_composite"}, namespace)
        with mock.patch.object(chip, "chatgpt_snapshot", return_value=empty), \
                mock.patch.object(tier_guard, "record_choice", return_value=saved) as rec:
            out = namespace["_record_composite"](
                object(), 1234, {"model": "gpt56_luna", "effort": "low"},
                {"model": "gpt56_luna", "effort": "low"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "post_action_readback")
        self.assertEqual(out["dimensions"]["model"]["readback"], "gpt56_luna")
        self.assertEqual(out["dimensions"]["effort"]["readback"], "low")
        self.assertTrue(rec.called)

    def test_mismatch_still_fails_closed(self):
        from qi import chip, tier_guard

        empty = {"writer": "work", "dimensions": {"model": None, "effort": None}}
        namespace = {"_record_receipt": lambda *a, **k: None}
        load({"_record_composite"}, namespace)
        with mock.patch.object(chip, "chatgpt_snapshot", return_value=empty), \
                mock.patch.object(tier_guard, "record_choice",
                                  return_value={"written": True}):
            out = namespace["_record_composite"](
                object(), 1234, {"model": "gpt56_luna", "effort": "low"},
                {"model": "gpt56_sol", "effort": "low"})
        self.assertFalse(out["ok"])
        self.assertIsNone(out["dimensions"]["model"]["readback"])


if __name__ == "__main__":
    unittest.main()
