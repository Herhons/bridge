"""B17：chatgpt 模型列表的「第 9 项在折叠线下」问题。

现场（B17-S2）：work 模式 9 个模型、一屏只放得下 8 个。`_find_item_uia`
拿到 GPT-5.6 Luna 的 UIA 矩形就直接点，矩形在折叠线以下，点在列表外，
选中态没变；复核读到 `gpt6_astra`，整体判 `choose_failed`。

契约：
1. UIA 行必须带 node + offscreen，调用方才能判断「这个矩形能不能点」；
2. 目标在视口外 → 先 ScrollIntoView，再重新枚举取新矩形；滚不动返回 None，
   交给模板 + `_scroll_list` 的旧兜底；
3. `_items_visible` 不把隐藏残影当「列表已开」，否则 `enter_models` 会跳过重开；
4. `read_model_uia` 只信 onscreen 的选中项，隐藏旧列表的 IsSelected 不算读数；
5. 复核前先把目标滚进视口；失配时落截图 + 行清单，但判定仍是 fail-closed。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "scripts" / "chatgpt_choose.py"


def load(names, namespace):
    """把指定的顶层 def/class 抠出来 exec 到 namespace（沿用现有测试风格）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"), filename=str(SCRIPT))
    wanted = [node for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.ClassDef))
              and node.name in names]
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPT), "exec"), namespace)
    return namespace


class FakeControl:
    def __init__(self, selected=False, offscreen=False):
        self._selected = selected
        self.IsOffscreen = offscreen
        self.rect = (100, 200, 300, 36)

    @property
    def BoundingRectangle(self):
        left, top, width, height = self.rect
        return type("Rect", (), {"left": left, "top": top,
                                 "right": left + width,
                                 "bottom": top + height})()

    def GetSelectionItemPattern(self):
        return type("Pattern", (), {"IsSelected": self._selected})()


class FakeNode:
    def __init__(self, name, *, selected=False, offscreen=False):
        self.kind = "RadioButtonControl"
        self.name = name
        self.control = FakeControl(selected=selected, offscreen=offscreen)


class FakeRoot:
    def __init__(self, nodes):
        self._nodes = nodes

    def walk(self):
        return list(self._nodes)


class RowShapeTests(unittest.TestCase):
    def test_rows_carry_node_and_offscreen_flag(self):
        onscreen = FakeNode("GPT-6 Astra")
        hidden = FakeNode("GPT-5.6 Luna", offscreen=True)
        ns = {"_doc_root": lambda _s: FakeRoot([onscreen, hidden]),
              "MODELS": {"work": {"gpt6_astra": ("GPT-6 Astra", "t1"),
                                  "gpt56_luna": ("GPT-5.6 Luna", "t2")}},
              "_name_key": lambda text: "".join(str(text or "").split()).lower(),
              "_offscreen": lambda node: bool(node.control.IsOffscreen)}
        load({"_ui_model_rows"}, ns)
        rows = ns["_ui_model_rows"](object())
        self.assertEqual([row[0] for row in rows], ["gpt6_astra", "gpt56_luna"])
        self.assertFalse(rows[0][4])
        self.assertTrue(rows[1][4])
        self.assertIs(rows[1][3], hidden)


class FindItemTests(unittest.TestCase):
    def _namespace(self, rows_after_scroll, scroll_ok=True):
        calls = {"scroll": 0}
        state = {"scrolled": False}
        hidden = FakeNode("GPT-5.6 Luna", offscreen=True)
        shown = FakeNode("GPT-5.6 Luna")

        def rows(_screen):
            if state["scrolled"]:
                return [("gpt56_luna", "GPT-5.6 Luna", (100, 300, 300, 36), shown, False)]
            return [("gpt56_luna", "GPT-5.6 Luna", (100, 900, 300, 36), hidden, True)]

        def scroll(_node):
            calls["scroll"] += 1
            if scroll_ok:
                state["scrolled"] = True
            return scroll_ok

        ns = {"_ui_model_rows": rows, "_scroll_node": scroll,
              "time": type("T", (), {"sleep": staticmethod(lambda _s: None)})}
        load({"_Row", "_find_item_uia"}, ns)
        return ns["_find_item_uia"], calls

    def test_offscreen_target_is_scrolled_before_returning(self):
        fn, calls = self._namespace(None)
        row = fn(object(), "gpt56_luna")
        self.assertIsNotNone(row)
        self.assertEqual(calls["scroll"], 1)
        self.assertEqual((row.x, row.y), (100, 300))
        self.assertEqual(row.center, (250, 318))

    def test_scroll_failure_returns_none_for_template_fallback(self):
        fn, calls = self._namespace(None, scroll_ok=False)
        self.assertIsNone(fn(object(), "gpt56_luna"))
        self.assertEqual(calls["scroll"], 1)


class ItemsVisibleTests(unittest.TestCase):
    def _fn(self, rows):
        ns = {"_ui_model_rows": lambda _s: rows, "_win": lambda _s: None,
              "CHECK": "gpt_check", "MODELS": {"work": {}}}
        load({"_items_visible"}, ns)
        return ns["_items_visible"]

    def test_hidden_rows_do_not_count_as_open(self):
        fn = self._fn([("gpt56_luna", "GPT-5.6 Luna", (1, 2, 3, 4), FakeNode("x"), True)])
        self.assertFalse(fn(object(), "work"))

    def test_one_visible_row_is_enough(self):
        fn = self._fn([("gpt56_luna", "GPT-5.6 Luna", (1, 2, 3, 4), FakeNode("x"), False)])
        self.assertTrue(fn(object(), "work"))


class ReadModelUiaTests(unittest.TestCase):
    def _run(self, nodes):
        ns = {"_doc_root": lambda _s: FakeRoot(nodes),
              "MODELS": {"work": {"gpt6_astra": ("GPT-6 Astra", "t1"),
                                  "gpt56_luna": ("GPT-5.6 Luna", "t2")}},
              "_name_key": lambda text: "".join(str(text or "").split()).lower(),
              "_offscreen": lambda node: bool(node.control.IsOffscreen)}
        load({"read_model_uia"}, ns)
        return ns["read_model_uia"](object(), "work")

    def test_visible_selection_wins(self):
        out = self._run([FakeNode("GPT-5.6 Luna", selected=True),
                         FakeNode("GPT-6 Astra", selected=True, offscreen=True)])
        self.assertEqual(out["model"], "gpt56_luna")
        self.assertEqual(out["source"], "uia")

    def test_only_hidden_selection_is_not_a_reading(self):
        out = self._run([FakeNode("GPT-6 Astra", selected=True, offscreen=True)])
        self.assertEqual(out["model"], "unknown")
        self.assertEqual(out["reason"], "selected_offscreen")


class SetModelReverifyTests(unittest.TestCase):
    def test_reverify_scrolls_target_then_dumps_before_failing(self):
        picked = iter([{"model": "gpt6_astra", "source": "uia"},
                       {"model": "gpt6_astra", "source": "uia"}])
        find_calls, dumps, closes = [], [], []

        ns = {
            "MODELS": {"work": {"gpt56_luna": ("GPT-5.6 Luna", "tpl")}},
            "NO_READ": set(),
            "read_writer": lambda _s: {"writer": "work"},
            "chip_signature": lambda _s: None,
            "enter_models": lambda _s, _w: {"ok": True, "stage": "opened"},
            "_pick_model": lambda _s, _w: next(picked),
            "_find_item": lambda _s, _w, _t, scroll=True, key=None:
                find_calls.append(key) or type("M", (), {"center": (10, 10)})(),
            "safe_click": lambda _s, _p: (True, "ok"),
            "time": type("T", (), {"sleep": staticmethod(lambda _s: None)}),
            "_win": lambda _s: None,
            "_slider": lambda _s: None,
            "close_all": lambda _s, _w: closes.append(1),
            "_dump_unverified": lambda _s, key, info: dumps.append((key, info["model"])),
        }
        load({"set_model"}, ns)
        out = ns["set_model"](object(), "gpt56_luna")
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "unverified")
        self.assertEqual(out["after"], "gpt6_astra")
        # 找目标一次（点击）+ 复核前再滚一次
        self.assertEqual(find_calls, ["gpt56_luna", "gpt56_luna"])
        self.assertEqual(dumps, [("gpt56_luna", "gpt6_astra")])
        self.assertEqual(closes, [1])


if __name__ == "__main__":
    unittest.main()
