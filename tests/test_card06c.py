"""CARD-06c：网页端适配与配置（聊天选档 / writer 白名单 / 无选择器 / 点芯片 /
Gemini 新对话 / GLM accept_lite / Grok 菜单模板）。

全部离线：不开窗口、不点桌面、不发消息。卡面 1–7 逐条对应：

    1  chatgpt 聊天模式选档（instant/mid/high；中英都认；--list 读回两项）
    2  tiers.json 的 chat 表 + 回执 writer + use 按 writer 取白名单
    3  Work 模式找不到模型入口 → stage=no_model_selector，fail-closed
    4  不用 Ctrl+Shift+M；点芯片，点前唯一性/距离校验，点后复核弹层
    5  gemini 新对话：侧栏折叠/展开两态都能找到
    6  opencode 独立用途 accept_lite（只白名单 GLM-5.3-Flash-Free），结论 PASS(GLM-lite)
    7  grok_item_* 新模板能选中（并且不是"一片空白"那种坏图）
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import ast
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = SRC / "scripts"
LIBRARY = SRC / "assets" / "library"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from qi import chip  # noqa: E402
from qi import tier_guard as tg  # noqa: E402


# ---------------------------------------------------------------- 工具
def read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def load_function(path: Path, name: str, namespace: dict):
    tree = ast.parse(read_source(path), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def literal_assignment(path: Path, name: str):
    tree = ast.parse(read_source(path), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    return ast.literal_eval(node.value)


class FakeRect:
    def __init__(self, left, top, width, height):
        self.left, self.top = left, top
        self.right, self.bottom = left + width, top + height


class FakeNode:
    """最小 UIA 节点：walk / kind / name / control / children。"""

    def __init__(self, kind="DocumentControl", name="", box=(0, 0, 10, 10), children=()):
        self.kind = kind
        self.name = name
        self.children = list(children)
        self.control = types.SimpleNamespace(
            IsOffscreen=False,
            BoundingRectangle=FakeRect(*box) if box else None)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, *args, **kwargs):
        self.events.append((args, kwargs))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ================================================================ 1. 聊天模式选档
class ChatTierTests(unittest.TestCase):
    def setUp(self):
        self.choose = SCRIPTS / "chatgpt_choose.py"

    def test_chat_effort_table_is_instant_mid_high(self):
        table = literal_assignment(self.choose, "EFFORTS")
        self.assertEqual(list(table["chat"]), ["instant", "mid", "high"])
        self.assertEqual([label for label, _px in table["chat"].values()],
                         ["即时", "中", "高"])
        # 工作模式五档不能被聊天模式带偏
        self.assertEqual(list(table["work"]), ["low", "mid", "high", "xhigh", "max"])

    def test_chat_effort_words_parse_chinese_and_english(self):
        words = literal_assignment(self.choose, "_CHIP_EFFORT_WORDS")
        ns = {"_CHIP_EFFORT_WORDS": words}
        fn = load_function(self.choose, "_effort_of_text", ns)
        cases = {"中": "mid", "Medium": "mid", "medium": "mid",
                 "高": "high", "High": "high",
                 "即时": "instant", "Instant": "instant",
                 "极高": "xhigh", "Extra High": "xhigh",
                 "轻度": "low", "Light": "low",
                 "Max": "max", "思考强度": None, "": None}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(fn(text), want)

    def test_longest_word_wins_so_xhigh_is_not_high(self):
        words = literal_assignment(self.choose, "_CHIP_EFFORT_WORDS")
        fn = load_function(self.choose, "_effort_of_text", {"_CHIP_EFFORT_WORDS": words})
        self.assertEqual(fn("极高"), "xhigh")
        self.assertEqual(fn("Extra High"), "xhigh")

    def test_chip_module_agrees_with_choose(self):
        """两个模块各有一份词表；对不上就会出现"choose 说 instant、use 读成 low"。"""
        words = literal_assignment(self.choose, "_CHIP_EFFORT_WORDS")
        for word, key in words:
            with self.subTest(word=word):
                parsed = chip._parse_effort(word)
                self.assertEqual(parsed, key)
        # 聊天最低档是 instant，工作最低档是 low：同一个「即时」不能再映射成 low
        self.assertEqual(chip._parse_effort("即时"), "instant")
        self.assertEqual(chip._parse_effort("轻度"), "low")

    def test_chat_models_are_gpt6_and_gpt56(self):
        models = literal_assignment(self.choose, "MODELS")
        self.assertEqual(set(models["chat"]), {"gpt6", "gpt56"})
        self.assertEqual(models["chat"]["gpt6"][0], "GPT-6（默认）")
        self.assertEqual(models["chat"]["gpt56"][0], "GPT-5.6 Sol")
        self.assertEqual(len(models["work"]), 9)

    def test_chip_effort_reads_english_label(self):
        node = FakeNode("ButtonControl", "选择 ChatGPT 模型", box=(1600, 660, 80, 37),
                        children=[FakeNode("TextControl", "High")])
        root = FakeNode("DocumentControl", "", children=[node])
        words = literal_assignment(self.choose, "_CHIP_EFFORT_WORDS")
        ns = {"CHIP_BUTTON": "选择 ChatGPT 模型",
              "_doc_root": lambda s: root,
              "_CHIP_EFFORT_WORDS": words,
              "_effort_of_text": load_function(self.choose, "_effort_of_text",
                                               {"_CHIP_EFFORT_WORDS": words})}
        ns["_chip_text"] = load_function(self.choose, "_chip_text", ns)
        fn = load_function(self.choose, "read_chip_effort", ns)
        got = fn(object())
        self.assertEqual(got["effort"], "high")
        self.assertEqual(got["raw"], "High")


class WriterReadTests(unittest.TestCase):
    """模式读法：UIA 的 TogglePattern 优先，墨色兜底。"""

    def setUp(self):
        self.choose = SCRIPTS / "chatgpt_choose.py"

    def toggle_node(self, name, state):
        node = FakeNode("ButtonControl", name, box=(0, 0, 100, 30))
        node.control.GetTogglePattern = lambda: types.SimpleNamespace(ToggleState=state)
        return node

    def run_uia(self, nodes):
        ns = {
            "WRITER_LABELS": {"chat": "聊天", "work": "工作"},
            "_doc_root": lambda s: FakeNode("DocumentControl", "", children=nodes),
            "_toggle_state": load_function(self.choose, "_toggle_state", {}),
        }
        fn = load_function(self.choose, "read_writer_uia", ns)
        return fn(object())

    def test_toggle_pattern_gives_the_selected_mode(self):
        got = self.run_uia([self.toggle_node("聊天", 0), self.toggle_node("工作", 1)])
        self.assertEqual(got["writer"], "work")
        self.assertEqual(got["source"], "uia_toggle")
        got = self.run_uia([self.toggle_node("聊天", 1), self.toggle_node("工作", 0)])
        self.assertEqual(got["writer"], "chat")

    def test_two_on_or_two_off_is_unknown(self):
        self.assertEqual(self.run_uia([self.toggle_node("聊天", 1),
                                       self.toggle_node("工作", 1)])["writer"], "unknown")
        self.assertEqual(self.run_uia([self.toggle_node("聊天", 0),
                                       self.toggle_node("工作", 0)])["writer"], "unknown")

    def test_nodes_without_the_pattern_are_ignored(self):
        plain = FakeNode("ButtonControl", "聊天", box=(0, 0, 100, 30))
        got = self.run_uia([plain, self.toggle_node("工作", 1)])
        self.assertEqual(got["writer"], "unknown")

    def test_read_writer_prefers_uia_over_ink(self):
        ns = {
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "read_writer_uia": lambda s: {"writer": "work", "source": "uia_toggle"},
            "_read_writer_once": lambda s: {"writer": "chat", "ink": {}},
        }
        fn = load_function(self.choose, "read_writer", ns)
        got = fn(object())
        self.assertEqual(got["writer"], "work")
        self.assertEqual(got["source"], "uia_toggle")

    def test_read_writer_falls_back_to_ink(self):
        ns = {
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "read_writer_uia": lambda s: {"writer": "unknown"},
            "_read_writer_once": lambda s: {"writer": "chat", "ink": {"chat": 13}},
        }
        fn = load_function(self.choose, "read_writer", ns)
        got = fn(object())
        self.assertEqual(got["writer"], "chat")
        self.assertEqual(got["source"], "ink")


# ================================================================ 2. 回执 writer / 白名单
class WriterReceiptTests(Base):
    def receipt(self, **kwargs):
        kwargs.setdefault("root", self.tmp)
        kwargs.setdefault("target", "high")
        kwargs.setdefault("readback", "high")
        kwargs.setdefault("dimension", "effort")
        kwargs.setdefault("now", tg.utc_now())
        kwargs.setdefault("dimensions", {
            "model": {"target": "gpt6", "readback": "gpt6"},
            "effort": {"target": kwargs["target"], "readback": kwargs["readback"]}})
        return tg.write_receipt("chatgpt", **kwargs)

    def test_receipt_carries_writer(self):
        payload = self.receipt(writer="chat")
        self.assertEqual(payload["writer"], "chat")
        self.assertIsNone(self.receipt(writer=None)["writer"])

    def test_allowed_tiers_follow_the_writer(self):
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "model", writer="chat"),
                         ["gpt6"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "accept", "model", writer="chat"),
                         ["gpt6"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "test", "model", writer="chat"),
                         ["gpt56"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "effort", writer="chat"),
                         ["high"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "test", "effort", writer="chat"),
                         ["instant"])
        # 工作模式没有覆盖表 → 默认表（不用把 8 个档抄两遍）
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "model", writer="work"),
                         ["gpt61_sol"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "model"),
                         ["gpt61_sol"])

    def test_unknown_writer_fails_closed(self):
        with self.assertRaises(tg.TierUnconfirmed) as ctx:
            tg.allowed_tiers("chatgpt", "review", "model", writer="copilot")
        self.assertEqual(ctx.exception.reason, "writer_not_in_table")

    def test_chat_receipt_passes_review(self):
        self.receipt(writer="chat", target="high", readback="high")
        out = tg.check("chatgpt", "review", root=self.tmp)
        self.assertTrue(out["ok"], out.get("reasons"))
        self.assertEqual(out["expected"], ["high"])
        self.assertEqual(out["writer"], "chat")

    def test_work_top_tier_is_not_accepted_for_chat(self):
        """max 是工作模式最高档，聊天模式最高只有 high——按 writer 取表就会拒。"""
        self.receipt(writer="chat", target="max", readback="max")
        out = tg.check("chatgpt", "review", root=self.tmp)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reasons"], ["purpose_mismatch"])

    def test_gate_reports_purpose_label(self):
        self.receipt(writer="chat", target="high", readback="high")
        got = tg.gate("chatgpt", "review", root=self.tmp, do_consume=False,
                      reread=lambda dim: {"writer": "chat", "dimensions": {
                          "model": "gpt6", "effort": "high"}},
                      live={"hwnd": None, "pid": None, "process_start_time": None})
        self.assertEqual(got["purpose_label"], "REVIEW")
        self.assertEqual(got["receipt"]["writer"], "chat")


# ================================================================ 3. 没有模型入口
class NoModelSelectorTests(unittest.TestCase):
    def setUp(self):
        self.choose = SCRIPTS / "chatgpt_choose.py"

    def namespace(self, **over):
        ns = {
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "_items_visible": lambda s, w: False,
            "open_effort": lambda s, w: True,
            "_model_entry": lambda s: None,
            "_menu_entry_pos": lambda s: None,
            "close_all": Recorder(),
            "safe_click": lambda s, target: (True, "ok"),
            "OPEN_STATE": {},
            "MODELS": {"work": {"gpt61_sol": ("GPT-6.1 Sol", "tpl")},
                       "chat": {"gpt6": ("GPT-6", "tpl")}},
            "read_writer": lambda s: {"writer": "work"},
            "chip_signature": lambda s: None,
            "_pick_model": lambda s, w: {"model": "gpt61_sol"},
            "NO_READ": {"default"},
            "_frame": None,
        }
        ns.update(over)
        return ns

    def test_enter_models_returns_no_model_selector(self):
        fn = load_function(self.choose, "enter_models", self.namespace())
        got = fn(object(), "work")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "no_model_selector")
        self.assertEqual(got["reason"], "model_entry_missing")
        self.assertEqual(got["writer"], "work")

    def test_set_model_propagates_the_stage(self):
        ns = self.namespace()
        ns["enter_models"] = lambda s, w: {"ok": False, "stage": "no_model_selector",
                                           "reason": "model_entry_missing",
                                           "writer": w}
        fn = load_function(self.choose, "set_model", ns)
        got = fn(object(), "gpt61_sol")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "no_model_selector")
        self.assertEqual(got["writer"], "work")

    def test_read_model_propagates_the_stage(self):
        ns = self.namespace()
        ns["enter_models"] = lambda s, w: {"ok": False, "stage": "no_model_selector",
                                           "reason": "model_entry_missing",
                                           "writer": w}
        fn = load_function(self.choose, "read_model", ns)
        got = fn(object(), "work")
        self.assertEqual(got["model"], "unknown")
        self.assertEqual(got["stage"], "no_model_selector")

    def test_failed_choice_invalidates_the_old_receipt(self):
        """fail-closed：选档失败要把回执写成 readback=null，不能让上一次的成功回执继续生效。"""
        calls = []
        ns = {
            "argparse": __import__("argparse"),
            "json": json,
            "sys": types.SimpleNamespace(argv=["chatgpt_choose.py", "--model", "gpt61_sol"]),
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "guard": types.SimpleNamespace(parse_guard_args=lambda a: (2, 120, True, a),
                                           guard=lambda *a: True),
            "Library": type("L", (), {"report": lambda self, need: "",
                                      "missing": lambda self, need: []}),
            "Screen": type("S", (), {"bind": lambda self, h: None,
                                     "focus": lambda self: True,
                                     "window_rect": lambda self, *a, **k: (0, 0, 800, 600)}),
            "ensure_site": lambda *a, **k: 42,
            "COMPOSER": "gpt_composer", "CHECK": "gpt_check",
            "TAB_WORDS": {"chat": "c", "work": "w"},
            "MODELS": {"work": {"gpt61_sol": ("GPT-6.1 Sol", "tpl")},
                       "chat": {"gpt6": ("GPT-6", "tpl")}},
            "EFFORTS": {"work": {"max": ("Max", 1)}, "chat": {"high": ("高", 2)}},
            "read_writer": lambda s: {"writer": "work"},
            "read_model": lambda s, w: {"model": "unknown"},
            "read_effort": lambda s, w: {"effort": "unknown"},
            "choose_writer": lambda s, k: {"ok": True},
            "set_model": lambda s, k: {"ok": False, "stage": "no_model_selector",
                                       "writer": "work", "want": k},
            "set_effort": lambda s, k: {"ok": True},
            "_zoom_guard": lambda s: None,
            "_evidence": lambda *a, **k: None,
            "_record_receipt": lambda *a, **k: calls.append((a, k)),
            "_open_new_chat": lambda *a, **k: {"ok": True},
            "ForegroundLost": type("F", (Exception,), {}),
            "zoom_mod": types.SimpleNamespace(ensure=lambda *a, **k: None,
                                              ZoomNot100=type("Z", (Exception,), {})),
        }
        fn = load_function(self.choose, "main", ns)
        with mock.patch("builtins.print"):
            code = fn()
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[1], "model")
        self.assertIsNone(args[3], "失败时 readback 必须是 None")


# ================================================================ 4. 点芯片（不用快捷键）
class ChipClickTests(unittest.TestCase):
    def setUp(self):
        self.choose = SCRIPTS / "chatgpt_choose.py"

    def namespace(self, nodes, *, layer_after="effort", voice=(2000, 800),
                  near=(), click_ok=True):
        events = []
        state = {"layer": "closed"}

        def safe_click(screen, target, voice=None):
            events.append(("click", tuple(target)))
            return (click_ok, "ok" if click_ok else "预算用完")

        def layer(screen, writer):
            return state["layer"]

        def focus(screen):
            events.append(("focus", None))
            return True

        ns = {
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "CHIP_BUTTON": "选择 ChatGPT 模型",
            "CHIP_BOX_LIMIT": (420, 90),
            "CHIP_MIN_VOICE": 60,
            "_doc_root": lambda s: FakeNode("DocumentControl", "", children=nodes),
            "_box": load_function(self.choose, "_box", {}),
            "_inside": load_function(self.choose, "_inside", {}),
            "safe_click": safe_click,
            "_voice_center": lambda s: voice,
            "_near_boxes": lambda s: list(near),
            "_layer": layer,
            "focus_composer": focus,
            "OPEN_STATE": {},
        }
        # 点完把 layer 翻成"开了"，模拟真实弹层
        def flip():
            state["layer"] = layer_after
        ns["_flip"] = flip
        return ns, events, state

    def run_open(self, nodes, **kwargs):
        ns, events, state = self.namespace(nodes, **kwargs)
        # 点完就翻状态：包一层 safe_click
        original = ns["safe_click"]

        def click_then_flip(screen, target, voice=None):
            ok = original(screen, target, voice=voice)
            if ok[0]:
                ns["_flip"]()
            return ok
        ns["safe_click"] = click_then_flip
        ns["chip_nodes"] = load_function(self.choose, "chip_nodes", ns)
        fn = load_function(self.choose, "open_chip_menu", ns)
        return fn(object(), "chat"), events

    def chip(self, box=(1600, 660, 80, 37)):
        return FakeNode("ButtonControl", "选择 ChatGPT 模型", box=box)

    def test_clicks_the_chip_center_and_never_a_hotkey(self):
        got, events = self.run_open([self.chip()])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["stage"], "opened")
        self.assertEqual(events, [("click", (1640, 678))])
        self.assertNotIn("hotkey", [e[0] for e in events])

    def test_two_chips_are_rejected_without_clicking(self):
        got, events = self.run_open([self.chip(), self.chip((1500, 660, 80, 37))])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "chip_not_unique")
        self.assertEqual(len(got["boxes"]), 2)
        self.assertEqual(events, [])

    def test_missing_chip_focuses_composer_then_gives_up(self):
        got, events = self.run_open([])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "chip_not_found")
        self.assertEqual(events, [("focus", None)])

    def test_chip_overlapping_dictation_is_rejected(self):
        got, events = self.run_open(
            [self.chip()], near=[("听写", (1630, 660, 37, 37))])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "chip_overlaps_control")
        self.assertEqual(got["near"]["name"], "听写")
        self.assertEqual(events, [])

    def test_chip_too_close_to_voice_is_rejected(self):
        got, events = self.run_open([self.chip()], voice=(1660, 678))
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "chip_too_close")
        self.assertLess(got["distance"], 60)
        self.assertEqual(events, [])

    def test_menu_must_appear_after_the_click(self):
        got, events = self.run_open([self.chip()], layer_after="closed")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "menu_not_open")
        self.assertEqual(len(events), 1, "点了但没开：只该有那一下点击")

    def test_absurd_chip_box_is_rejected(self):
        got, events = self.run_open([self.chip((500, 300, 900, 200))])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "chip_geometry")
        self.assertEqual(events, [])

    def test_source_has_no_ctrl_shift_m(self):
        """卡面第 4 条：不许再按 Ctrl+Shift+M（会被 Chrome 截成个人资料菜单）。"""
        source = read_source(self.choose)
        self.assertNotIn("CHIP_HOTKEY", source)
        self.assertIsNone(re.search(r'hotkey\(\s*"ctrl"\s*,\s*"shift"\s*,\s*"m"', source))


# ================================================================ 5. Gemini 新对话
class NewChatTests(unittest.TestCase):
    def setUp(self):
        self.flow = SRC / "qi" / "flow.py"

    def build_ui(self, trees, clicks):
        """trees: 依次返回的控件树；点的节点记进 clicks。"""
        queue = list(trees)

        def document(hwnd, site):
            return types.SimpleNamespace(), "https://x/"

        def tree(control):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def named(root, names, kinds=None):
            wanted = {n for n in names}
            return [n for n in root.walk() if n.name in wanted]

        def prefer(nodes):
            order = {"HyperlinkControl": 0, "ButtonControl": 1, "MenuItemControl": 2,
                     "TextControl": 3}
            return sorted(nodes, key=lambda n: order.get(n.kind, 9))

        def click(screen, node):
            clicks.append(node.name)

        return types.SimpleNamespace(document=document, tree=tree, named=named,
                                     prefer_clickables=prefer, click=click)

    def run_open(self, trees, site_name="gemini"):
        clicks = []
        ns = {
            "ui": self.build_ui(trees, clicks),
            "time": types.SimpleNamespace(sleep=lambda _s: None),
            "Site": lambda **kwargs: types.SimpleNamespace(**kwargs),
            "Screen": object,
        }
        fn = load_function(self.flow, "_open_new_chat", ns)
        ns["_newchat_nodes"] = load_function(self.flow, "_newchat_nodes", ns)
        ns["_sidebar_nodes"] = load_function(self.flow, "_sidebar_nodes", ns)
        ns["NEWCHAT_LABELS"] = {"gemini": {"发起新对话", "新对话", "New chat"},
                                "chatgpt": {"新聊天", "New chat"}}
        ns["SIDEBAR_TOGGLES"] = ("打开边栏", "打开侧边栏", "主菜单")
        fn = load_function(self.flow, "_open_new_chat", ns)
        site = types.SimpleNamespace(
            name=site_name, newchat="gem_newchat", sidebar=None,
            title_hint="Google Chrome", on_secondary=True)
        screen = types.SimpleNamespace(
            target_hwnd=1, window_rect=lambda *a, **k: (0, 0, 800, 600),
            find=lambda *a, **k: None)
        return fn(screen, site), clicks

    def node(self, name, kind="HyperlinkControl", box=(0, 0, 40, 20)):
        return FakeNode(kind, name, box=box)

    def test_expanded_sidebar_click(self):
        tree = FakeNode("DocumentControl", "", children=[
            self.node("发起新对话"), self.node("库"), self.node("设置", "ButtonControl")])
        got, clicks = self.run_open([tree])
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["basis"], "uia")
        self.assertEqual(clicks, ["发起新对话"])

    def test_collapsed_sidebar_expands_then_finds_it(self):
        collapsed = FakeNode("DocumentControl", "", children=[
            self.node("打开边栏", "ButtonControl")])
        expanded = FakeNode("DocumentControl", "", children=[
            self.node("发起新对话", box=(530, 357, 340, 40))])
        got, clicks = self.run_open([collapsed, expanded])
        self.assertTrue(got["ok"], got)
        self.assertEqual(clicks, ["打开边栏", "发起新对话"])
        self.assertEqual(got["tried"], ["sidebar_uia:打开边栏"])

    def test_nothing_found_reports_what_was_tried(self):
        empty = FakeNode("DocumentControl", "", children=[self.node("库")])
        got, clicks = self.run_open([empty])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "newchat_not_found")
        self.assertIn("3 轮", got["hint"])
        self.assertEqual(clicks, [])


# ================================================================ 6. GLM accept_lite
class AcceptLiteTests(Base):
    def test_accept_lite_whitelists_only_glm(self):
        self.assertEqual(tg.allowed_tiers("opencode", "accept_lite", "model"),
                         ["glm_53_flash_free"])
        self.assertEqual(tg.allowed_tiers("opencode", "accept", "model"),
                         ["claude_opus_55"])

    def test_accept_lite_is_not_registered_elsewhere(self):
        with self.assertRaises(tg.TierUnconfirmed) as ctx:
            tg.allowed_tiers("grok", "accept_lite", "model")
        self.assertEqual(ctx.exception.reason, "purpose_not_in_table")

    def test_purpose_labels_distinguish_glm(self):
        self.assertEqual(tg.purpose_label("accept"), "PASS")
        self.assertEqual(tg.purpose_label("accept_lite"), "PASS(GLM-lite)")
        self.assertNotEqual(tg.purpose_label("accept"), tg.purpose_label("accept_lite"))

    def test_opencode_choose_knows_glm(self):
        models = literal_assignment(SCRIPTS / "opencode_choose.py", "MODELS")
        labels = literal_assignment(SCRIPTS / "opencode_choose.py", "MODEL_LABELS")
        self.assertEqual(models["glm_53_flash_free"], "oc_m_glm53free")
        self.assertEqual(labels["glm_53_flash_free"], "GLM-5.3-Flash Free")
        self.assertTrue((LIBRARY / "oc_m_glm53free.png").exists())
        img = cv2.imread(str(LIBRARY / "oc_m_glm53free.png"))
        self.assertIsNotNone(img)
        self.assertLess(img.shape[1], 300, "模板只该是模型名那一小段")

    def test_opencode_use_matches_tier_guard(self):
        use = SCRIPTS / "opencode_use.py"
        self.assertEqual(literal_assignment(use, "PURPOSE_LABELS"), tg.PURPOSE_LABELS)
        source = read_source(use)
        choices = re.search(r'add_argument\("--purpose",\s*choices=\[(.*?)\]', source, re.S)
        self.assertIsNotNone(choices, "opencode_use 的 --purpose 参数没了？")
        self.assertIn("accept_lite", choices.group(1))
        models = literal_assignment(use, "MODELS")
        self.assertIn("glm_53_flash_free", models)

    def test_gate_accepts_a_glm_receipt(self):
        tg.write_receipt("opencode", target="glm_53_flash_free",
                         readback="glm_53_flash_free", dimension="model",
                         root=self.tmp, now=tg.utc_now())
        got = tg.gate("opencode", "accept_lite", root=self.tmp, do_consume=False,
                      require_readback=False,
                      live={"hwnd": None, "pid": None, "process_start_time": None})
        self.assertEqual(got["purpose_label"], "PASS(GLM-lite)")
        self.assertEqual(got["expected"], ["glm_53_flash_free"])

    def test_glm_receipt_is_rejected_for_accept(self):
        tg.write_receipt("opencode", target="glm_53_flash_free",
                         readback="glm_53_flash_free", dimension="model",
                         root=self.tmp, now=tg.utc_now())
        out = tg.check("opencode", "accept", root=self.tmp)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reasons"], ["purpose_mismatch"])


# ================================================================ 7. Grok 菜单模板
class GrokTemplatesTests(unittest.TestCase):
    OPTIONS = ("fast", "expert", "auto", "build")

    def load(self, key):
        path = LIBRARY / f"grok_item_{key}.png"
        self.assertTrue(path.exists(), f"缺模板 {path.name}")
        img = cv2.imread(str(path))
        self.assertIsNotNone(img, f"{path.name} 读不出来")
        return img

    def test_templates_are_inked_not_blank(self):
        """05:42 那批被判"空白图"：五张几乎一模一样、没有文字。
        这里锁住"每张都要有足够的深色像素"，防止再截成一片白。"""
        for key in self.OPTIONS:
            with self.subTest(key=key):
                img = self.load(key)
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                ink = float((gray < 170).mean())
                self.assertGreater(ink, 0.03, f"{key} 几乎没有文字像素（{ink:.3f}）")

    def test_templates_are_distinct_from_each_other(self):
        imgs = {key: self.load(key).astype(int) for key in self.OPTIONS}
        keys = list(imgs)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if imgs[a].shape != imgs[b].shape:
                    continue
                diff = float(np.abs(imgs[a] - imgs[b]).mean())
                with self.subTest(a=a, b=b):
                    self.assertGreater(diff, 2.0, f"{a} 和 {b} 几乎一样，多半截错了")

    def test_choose_selects_with_the_new_templates(self):
        from qi import choose as choose_mod

        class Match:
            def __init__(self, name, center, score=1.0):
                self.name, self.center, self.score, self.scale = name, center, score, 1.0
                self.x, self.y, self.w, self.h = center[0] - 10, center[1] - 10, 20, 20

        class Screen:
            last_best = 1.0
            threshold = 0.88
            library = LIBRARY

            def __init__(self):
                self.clicks = []

            def window_rect(self, *args, **kwargs):
                return (0, 0, 1364, 895)

            def find(self, name, threshold=0.85, region=None):
                if name == "grok_chip_expert":
                    return Match(name, (1500, 1000), 0.95)
                if name == "grok_item_expert":
                    return Match(name, (1490, 900), 0.99)
                return None

            def find_unique(self, name, threshold=0.85, region=None, scales=None):
                m = self.find(name)
                return {"match": m, "best": m.score if m else None, "scale": 1.0,
                        "ambiguous": False}

            def click(self, match, offset=None):
                self.clicks.append((match.name, match.center))

            def press(self, key, times=1):
                pass

            def move_to(self, x, y):
                pass

            def grab(self, region=None):
                return np.zeros((4, 4, 3), dtype=np.uint8)

        import grok_choose

        cfg = grok_choose.CFG
        screen = Screen()
        with mock.patch.object(choose_mod.time, "sleep", lambda _s: None):
            got = choose_mod.choose(screen, cfg, "expert")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["stage"], "already")
        self.assertEqual(screen.clicks, [], "已经是 expert 就不该再点菜单")


# ================================================================ 附：总表一致性
class TiersDocTests(unittest.TestCase):
    def test_chatgpt_writer_modes_declared(self):
        data = json.loads((ROOT / "docs" / "tiers.json").read_text(encoding="utf-8"))
        entry = data["endpoints"]["chatgpt"]
        self.assertEqual(sorted(entry["writer_modes"]), ["chat", "work"])
        for dimension in ("model", "effort"):
            self.assertIn("chat", entry["dimensions"][dimension]["writers"])

    def test_chat_review_and_accept_are_gpt6_plus_high(self):
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "model", writer="chat"),
                         ["gpt6"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "review", "effort", writer="chat"),
                         ["high"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "accept", "model", writer="chat"),
                         ["gpt6"])
        self.assertEqual(tg.allowed_tiers("chatgpt", "accept", "effort", writer="chat"),
                         ["high"])

    def test_example_receipts_in_docs_mention_writer(self):
        doc = (ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8-sig")
        self.assertIn("accept_lite", doc)
        self.assertIn("PASS(GLM-lite)", doc)
        self.assertIn("writer", doc)


if __name__ == "__main__":
    unittest.main()
