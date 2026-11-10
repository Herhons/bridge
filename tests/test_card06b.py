"""CARD-06b：发送安全门禁（档位读不到即拒发 / 输入归一 / 缩放 / 人机验证 / 留证）。

全部离线：不开窗口、不点桌面、不发消息。卡面 1–11 逐条对应：

    1  档位读不到一律拒发（tier_unreadable，退出码 5；模板→UIA 兜底一次）
    2  档位漂移（tier_drift；use 不自动重选）
    3  最后一刻复核（写入之后、点击之前；复核后页面变了就拒发）
    4  回执身份（receipt_identity）+ 原子写
    5  输入归一只作用于比对（行首列表标记吃掉也不再误报 input_mismatch）
    6  缩放（Ctrl+0 → 复检 → 仍非 100% 退出；尺度限定 0.8–1.25；唯一性）
    7  最小化统一走 qi.window.restore（失败 → 退出码 7）
    8  人机验证 / Unknown error（不点、不重试、分开归类）
    9  失败自动留证（evidence\\fail\\<端>_<时间>.png/.json + 一行日志）
    10 chatgpt 假读回（任一维度未读回即 ok=false；不推断「默认」）
    11 opencode_use 重复行（并且全仓不许再有相邻重复语句）
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import ast
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = SRC / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cv2  # noqa: E402

from qi import chip, conversation, failure, human, runlog, sendgate, textcmp  # noqa: E402
from qi import tier_guard as tg  # noqa: E402
from qi import window as window_mod  # noqa: E402
from qi import zoom  # noqa: E402
from qi.screen import Screen  # noqa: E402


# ---------------------------------------------------------------- 工具
def load_function(path: Path, name: str, namespace: dict):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    return ast.literal_eval(node.value)


class FakeNode:
    """最小 UIA 节点：只要 walk/kind/name/children。"""

    def __init__(self, kind="DocumentControl", name="", children=(), offscreen=False):
        self.kind = kind
        self.name = name
        self.children = list(children)
        self.control = types.SimpleNamespace(IsOffscreen=offscreen)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class FakeScreen:
    """只记动作的屏幕替身。"""

    def __init__(self, hwnd=123):
        self.target_hwnd = hwnd
        self.events = []
        self.require_foreground = False

    def hotkey(self, *keys):
        self.events.append(("hotkey", keys))

    def press(self, key):
        self.events.append(("press", key))

    def click_xy(self, x, y):
        self.events.append(("click", (x, y)))

    def move_to(self, x, y):
        self.events.append(("move", (x, y)))

    def window_rect(self, *args, **kwargs):
        return (0, 0, 800, 600)

    def grab(self, region=None):
        return np.zeros((20, 20, 3), dtype=np.uint8)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        failure._LAST.clear()
        failure._LAST_PATH.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def receipt(self, endpoint="grok", **kwargs):
        kwargs.setdefault("root", self.tmp)
        kwargs.setdefault("target", "fast")
        kwargs.setdefault("readback", kwargs["target"])
        return tg.write_receipt(endpoint, **kwargs)

    def readback(self, value="fast", **extra):
        got = {"value": value, "source": "template",
               "template": {"name": "grok_chip_fast", "score": 0.97,
                            "threshold": 0.85, "scale": 1.0, "hit": True}}
        got.update(extra)
        return got


# ================================================================ 1. 读不到即拒发
class TierUnreadableTests(Base):
    def test_template_miss_rejects_with_code_five(self):
        self.receipt()
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, reread=lambda d: {
                "value": None, "source": "none", "reason": "template_miss",
                "template": {"name": "grok_chip_fast", "score": 0.41,
                             "threshold": 0.85, "scale": 1.0, "hit": False},
                "zoom_percent": 125})
        exc = caught.exception
        self.assertEqual(exc.reason, "tier_unreadable")
        self.assertEqual(exc.code, 5)
        result = exc.as_result()
        self.assertEqual(result["template"]["score"], 0.41)
        self.assertEqual(result["zoom_percent"], 125)
        self.assertIn("读不到一律拒发", result["hint"])

    def test_unreadable_also_burns_the_receipt(self):
        self.receipt()
        with self.assertRaises(tg.TierUnconfirmed):
            tg.gate("grok", "test", root=self.tmp, reread=lambda d: None)
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"])

    def test_reread_exception_counts_as_unreadable(self):
        self.receipt()
        def boom(_dim):
            raise RuntimeError("COM 抽风")
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, reread=boom)
        self.assertEqual(caught.exception.reason, "tier_unreadable")

    def test_no_reader_is_unreadable_by_default(self):
        """CARD-06b 的默认是 fail-closed：没有只读通道就不能放行。"""
        self.receipt()
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp)
        self.assertEqual(caught.exception.reason, "tier_unreadable")

    def test_desktop_may_declare_unsupported_explicitly(self):
        self.receipt(endpoint="codex", target="gpt61_sol")
        result = tg.gate("codex", "accept", root=self.tmp, require_readback=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier_readback"], "unsupported")

    def test_confirmed_readback_is_reported(self):
        self.receipt()
        result = tg.gate("grok", "test", root=self.tmp,
                         reread=lambda d: self.readback("fast"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier_readback"], "confirmed")
        self.assertEqual(result["drift"]["value"], "fast")


# ================================================================ 芯片读法（1 的兜底）
class ChipReadTests(Base):
    def test_template_hit_wins_without_uia(self):
        with mock.patch.object(chip, "_read_templates",
                               return_value={"value": "fast", "source": "template",
                                             "reason": "",
                                             "template": {"name": "grok_chip_fast",
                                                          "score": 0.97}}), \
             mock.patch.object(chip, "_tree", side_effect=AssertionError("不该读 UIA")), \
             mock.patch.object(chip.zoom, "percent", return_value=100):
            got = chip.read(FakeScreen(), "grok", "model")
        self.assertEqual(got["value"], "fast")
        self.assertEqual(got["source"], "template")

    def test_template_miss_is_rejected_not_washed_by_uia(self):
        """卡面完成标准：故意换成错图 → 必须判"读不到"（不能被 UIA 洗成确认）。"""
        miss = {"value": None, "source": "none", "reason": "template_miss",
                "template": {"name": "grok_chip_expert", "score": 0.42,
                             "threshold": 0.85, "scale": 1.0, "hit": False}}
        with mock.patch.object(chip, "_read_templates", return_value=miss), \
             mock.patch.object(chip, "_tree",
                               side_effect=AssertionError("不该用 UIA 兜底")), \
             mock.patch.object(chip.zoom, "percent", return_value=125):
            got = chip.read(FakeScreen(), "grok", "model")
        self.assertIsNone(got["value"])
        self.assertEqual(got["reason"], "template_miss")
        self.assertEqual(got["template"]["score"], 0.42)
        self.assertEqual(got["zoom_percent"], 125)
        self.assertFalse(chip.UIA_FALLBACK_WITH_TEMPLATES)

    def test_all_chip_scores_are_reported(self):
        class S:
            threshold = 0.85

            def find_unique(self, name, **kwargs):
                score = 0.42 if name.endswith("fast") else 0.61
                return {"match": None, "ambiguous": False, "best": score,
                        "scale": 1.1, "hits": []}

            def window_rect(self, *a, **k):
                return (0, 0, 100, 100)

        got = chip._read_templates(S(), "grok", calm=False)
        self.assertEqual([t["name"] for t in got["templates"]],
                         ["grok_chip_fast", "grok_chip_expert"])
        self.assertEqual(got["templates"][1]["score"], 0.61)
        self.assertEqual(got["template"]["name"], "grok_chip_expert")

    def test_both_readers_missing_returns_none(self):
        root = FakeNode("DocumentControl", "", [FakeNode("ButtonControl", "无关按钮")])
        miss = {"value": None, "source": "none", "reason": "ambiguous",
                "template": {"name": "grok_chip_fast", "score": 0.99,
                             "threshold": 0.85, "hit": True}}
        with mock.patch.object(chip, "_read_templates", return_value=miss), \
             mock.patch.object(chip, "_tree", return_value=root), \
             mock.patch.object(chip.zoom, "percent", return_value=100):
            got = chip.read(FakeScreen(), "grok", "model")
        self.assertIsNone(got["value"])
        self.assertEqual(got["reason"], "ambiguous")

    def test_gemini_chip_name_is_parsed(self):
        root = FakeNode("DocumentControl", "", [
            FakeNode("ButtonControl", "打开模式选择器，当前模式为“Argon”")])
        with mock.patch.object(chip, "_tree", return_value=root):
            got = chip._read_gemini_uia(FakeScreen(), "model")
        self.assertEqual((got["value"], got["source"]), ("argon", "uia"))
        # 但 gemini 有芯片模板：模板失配时整体仍判"读不到"
        self.assertIn("gemini", chip.TEMPLATE_CHIPS)

    def test_chatgpt_has_no_chip_template_so_uia_is_the_reader(self):
        self.assertNotIn("chatgpt", chip.TEMPLATE_CHIPS)
        self.assertTrue(chip.supports("chatgpt"))

    def test_chatgpt_chip_gives_effort_and_model(self):
        chip_button = FakeNode("ButtonControl", chip.CHATGPT_CHIP, [
            FakeNode("TextControl", "GPT-6 Astra 极高")])
        root = FakeNode("DocumentControl", "", [chip_button])
        with mock.patch.object(chip, "_tree", return_value=root), \
             mock.patch.object(chip.zoom, "percent", return_value=100):
            effort = chip.read(FakeScreen(), "chatgpt", "effort")
            model = chip.read(FakeScreen(), "chatgpt", "model")
        self.assertEqual(effort["value"], "xhigh")
        self.assertEqual(model["value"], "gpt6_astra")

    def test_chatgpt_model_name_table_matches_choose(self):
        mods = literal_assignment(SCRIPTS / "chatgpt_choose.py", "MODELS")
        displays = [display for table in mods.values() for display, _tpl in table.values()]
        for display in displays:
            self.assertIn(display, chip.CHATGPT_MODEL_NAMES,
                          f"chip.py 少了 {display} 的映射（两处不能各说各话）")

    def test_effort_words_longest_first(self):
        self.assertEqual(chip._parse_effort("极高"), "xhigh")
        self.assertEqual(chip._parse_effort("高"), "high")
        self.assertIsNone(chip._parse_effort("思考强度"))


class ChooseUniqueTests(unittest.TestCase):
    class _S:
        threshold = 0.85

        def __init__(self, got):
            self.got = got

        def find_unique(self, *args, **kwargs):
            return self.got

        def window_rect(self, *args, **kwargs):
            return (0, 0, 100, 100)

        def press(self, *args):
            pass

        def move_to(self, *args):
            pass

    def cfg(self):
        from qi.choose import Chooser
        return Chooser(name="grok", anchor="a", open_offset=(0, 0), prefix="grok_",
                       options={"fast": "Fast", "expert": "Expert"},
                       has_chip=("fast", "expert"))

    def test_ambiguous_single_template_is_unknown(self):
        from qi.choose import read_current
        got = {"match": None, "ambiguous": True, "best": 0.99, "scale": 1.0, "hits": []}
        out = read_current(self._S(got), self.cfg())
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "ambiguous")

    def test_two_chips_selected_is_unknown(self):
        from qi.choose import read_current
        match = types.SimpleNamespace(score=0.99, scale=1.0)
        got = {"match": match, "ambiguous": False, "best": 0.99, "scale": 1.0, "hits": [match]}
        out = read_current(self._S(got), self.cfg())
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "multiple_chips")

    def test_already_selected_does_not_open_the_menu(self):
        """已经是目标档就别点菜单：2026-11-02 实测 grok 的 item 模板失配，
        为了"走一遍流程"去开菜单会把一次本来成功的选择变成 menu_not_open。"""
        def boom(*_a, **_k):
            raise AssertionError("不该开菜单")

        ns = {"Screen": object,
              "read_current": lambda s, c: {"state": "expert", "score": 0.9, "scale": 1.0},
              "open_menu": boom}
        fn = load_function(SRC / "qi" / "choose.py", "choose", ns)
        got = fn(FakeScreen(), self.cfg(), "expert")
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "already")

    def test_single_hit_is_returned_with_scale(self):
        from qi.choose import read_current
        match = types.SimpleNamespace(score=0.93, scale=0.8)
        # 每个 chip 各问一次：fast 命中、expert 不命中
        got = {"match": match, "ambiguous": False, "best": 0.93, "scale": 0.8, "hits": [match]}
        cfg = self.cfg()
        calls = {"n": 0}

        class S(self._S):
            def find_unique(self, *args, **kwargs):
                self.n = getattr(self, "n", 0) + 1
                if self.n == 1:
                    return got
                return {"match": None, "ambiguous": False, "best": 0.2, "scale": 1.0, "hits": []}

        out = read_current(S(got), cfg)
        self.assertEqual(out["state"], "fast")
        self.assertEqual(out["scale"], 0.8)


# ================================================================ 2. 漂移
class TierDriftTests(Base):
    def test_drift_rejects_and_never_reselects(self):
        self.receipt(target="fast", readback="fast")
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp,
                    reread=lambda d: self.readback("expert"))
        exc = caught.exception
        self.assertEqual(exc.reason, "tier_drift")
        self.assertIn("grok_choose", exc.hint)
        self.assertIn("不切档", exc.hint)
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"])

    def test_use_source_has_no_choose_call(self):
        """use 自己绝不点菜单重选：只允许出现 *_choose 的提示文字，不允许调用。"""
        text = (SCRIPTS / "grok_use.py").read_text(encoding="utf-8")
        self.assertNotIn("choose(screen", text)
        self.assertNotIn("open_menu(", text)


# ================================================================ 3. 最后一刻复核
class _View:
    def __init__(self, value="", url="https://grok.com/c/1", busy=False, copies=()):
        self.url = url
        self.value = value
        self.busy = busy
        self.copies = list(copies)
        self.root = FakeNode("DocumentControl", "")
        self.editor = None


class _Conv:
    """conversation.ask 的最小替身：记事件，不做任何真实 UI。"""

    def __init__(self, events, *, echo=None, url="https://grok.com/c/1"):
        self.site = "grok"
        self.screen = FakeScreen()
        self.images = None
        self.empty = lambda view: True
        self.events = events
        self.echo = echo
        self.url = url
        self.written = None

    def read(self, attempts=4, delay=0.4):
        return _View(value=self.echo if self.echo is not None else (self.written or ""),
                     url=self.url)

    def write(self, view, prompt):
        self.events.append(("write", prompt))
        self.written = prompt

    def submit(self, view):
        self.events.append(("submit",))


class LastMomentTests(unittest.TestCase):
    def test_door_runs_after_write_and_before_submit(self):
        events = []
        conv = _Conv(events)
        stamp = {"hwnd": 1, "pid": 2, "process_start_time": 3,
                 "url": "https://grok.com/c/1", "doc_rid": (1,), "foreground": True}

        def door():
            events.append(("pre_send",))
            return {"stamp": stamp}

        with mock.patch.object(sendgate, "page_stamp", return_value=dict(stamp)), \
             mock.patch.object(sendgate, "page_confirm",
                               return_value={"ok": True, "changed": []}):
            conversation.ask(conv, "1. 第一行\n2. 第二行", timeout=0.05, pre_send=door)
        kinds = [e[0] for e in events]
        self.assertLess(kinds.index("write"), kinds.index("pre_send"))
        self.assertLess(kinds.index("pre_send"), kinds.index("submit"))
        self.assertEqual(events[0][1] if isinstance(events[0], tuple) else None,
                         "1. 第一行\n2. 第二行")

    def test_tier_failure_after_write_does_not_click_send(self):
        events = []
        conv = _Conv(events)

        def door():
            raise tg.TierUnconfirmed("tier_unreadable", "读不到")
        with contextlib.redirect_stdout(io.StringIO()):
            result = conversation.ask(conv, "提问", timeout=0.05, pre_send=door)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tier_unconfirmed")
        self.assertEqual(result["submission"], "not_sent")
        self.assertNotIn("submit", [e[0] for e in events])

    def test_page_change_after_recheck_blocks_send(self):
        events = []
        conv = _Conv(events)
        with mock.patch.object(sendgate, "page_stamp", return_value={
                "hwnd": 1, "pid": 2, "process_start_time": 3,
                "url": "https://grok.com/new", "doc_rid": (9,)}) as stamp, \
             mock.patch.object(sendgate, "page_confirm",
                               return_value={"ok": False, "changed": ["url"],
                                             "now": {"url": "https://grok.com/new"}}):
            result = conversation.ask(conv, "提问", timeout=0.05,
                                      pre_send=lambda: {"stamp": {"url": "https://grok.com/c/1"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "tier_unconfirmed")
        self.assertEqual(result["reason"], "page_changed")
        self.assertEqual(result["changed"], ["url"])
        self.assertNotIn("submit", [e[0] for e in events])

    def test_pre_send_not_called_when_no_purpose(self):
        events = []
        conv = _Conv(events)
        conversation.ask(conv, "提问", timeout=0.05, pre_send=None)
        self.assertIn("submit", [e[0] for e in events])


# ================================================================ 4. 回执身份 + 原子写
class ReceiptIdentityTests(Base):
    def test_identity_mismatch_has_its_own_reason(self):
        self.receipt(hwnd=None)
        live = {"hwnd": 999, "pid": 1, "process_start_time": 2}
        verdict = tg.check("grok", "test", root=self.tmp, live=live)
        self.assertIn("receipt_identity", verdict["reasons"])
        self.assertEqual(verdict["identity"]["field"], "hwnd")
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("grok", "test", root=self.tmp, live=live, do_consume=False)
        self.assertEqual(caught.exception.reason, "receipt_identity")

    def test_write_is_atomic_and_leaves_no_temp(self):
        self.receipt()
        left = list((self.tmp / "runs" / "tier").glob("*.tmp"))
        self.assertEqual(left, [])
        json.loads((self.tmp / "runs" / "tier" / "grok.json").read_text(encoding="utf-8"))

    def test_consume_is_atomic_too(self):
        self.receipt()
        self.assertTrue(tg.consume("grok", root=self.tmp)["ok"])
        left = list((self.tmp / "runs" / "tier").glob("*.tmp"))
        self.assertEqual(left, [])
        self.assertTrue(tg.read_receipt("grok", root=self.tmp)["consumed"])


# ================================================================ 5. 输入归一
class InputNormalizeTests(Base):
    def test_markers_are_stripped_for_comparison(self):
        sent = "1. 第一行\n2. 第二行\n- 项目\n4) 第四行\n正文 1.5 不是列表"
        echoed = "第一行\n第二行\n项目\n第四行\n正文 1.5 不是列表"
        self.assertTrue(textcmp.equal(sent, echoed))
        self.assertFalse(textcmp.equal(sent, echoed + "改动"))

    def test_editor_added_numbering_is_tolerated(self):
        """Grok 真机读回：`2. 第二条` 被渲染成 `2. 2. 第二条`（自动序号 + 原标记）。"""
        sent = "开场\n\n补充：\n1. 第一条\n2. 第二条\n4. 第四条\n- 破折号项"
        echo = "开场\n补充：\n1. 第一条\n2. 2. 第二条\n3. 4. 第四条\n4. - 破折号项"
        self.assertTrue(textcmp.equal(sent, echo), textcmp.describe(sent, echo))

    def test_blank_lines_are_ignored_because_the_editor_drops_them(self):
        self.assertTrue(textcmp.equal("甲\n\n乙", "甲\n乙"))

    def test_decimal_is_not_a_list_marker(self):
        self.assertEqual(textcmp.normalize("正文 1.5 不是列表"), "正文 1.5 不是列表")

    def test_newlines_are_unified(self):
        self.assertTrue(textcmp.equal("a\r\nb\rc", "a\nb\nc"))

    def test_describe_gives_line_and_context(self):
        got = textcmp.describe("1. 甲\n2. 乙", "甲\n丙")
        self.assertIn("第 2 行", got)
        self.assertIn("乙", got)
        self.assertIn("丙", got)
        diff = textcmp.first_diff("1. 甲\n2. 乙", "甲\n丙")
        self.assertEqual(diff["line"], 2)
        long = "x" * 60 + "差异甲" + "y" * 60
        short = "x" * 60 + "差异乙" + "y" * 60
        ctx = textcmp.first_diff(long, short)
        self.assertLessEqual(len(ctx["expected"]), 41, "前后各 20 字（±1）")

    def test_lengths(self):
        self.assertEqual(textcmp.lengths("12345", "123"), "期望 5 字 / 实际 3 字（差 -2）")

    def test_input_mismatch_hint_names_the_line(self):
        conv = _Conv([], echo="甲\n丙")
        with contextlib.redirect_stdout(io.StringIO()):
            result = conversation.ask(conv, "1. 甲\n2. 乙", timeout=0.05)
        self.assertEqual(result["stage"], "input_mismatch")
        self.assertIn("第 2 行", result["hint"])

    def test_body_is_sent_unchanged(self):
        """归一**只**用于比对：带标记的原文必须原样打进去。"""
        events = []
        conv = _Conv(events, echo="第一行\n第二行")
        prompt = "1. 第一行\n2. 第二行"
        with mock.patch.object(sendgate, "page_stamp", return_value={}), \
             mock.patch.object(sendgate, "page_confirm",
                               return_value={"ok": True, "changed": []}):
            conversation.ask(conv, prompt, timeout=0.05, pre_send=lambda: {"stamp": {}})
        written = [e for e in events if e[0] == "write"][0][1]
        self.assertEqual(written, prompt)
        self.assertIn("1. ", written)


# ================================================================ 6. 缩放
class ZoomTests(unittest.TestCase):
    def test_already_100_needs_no_hotkey(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=True), \
             mock.patch.object(zoom, "percent", return_value=100):
            facts = zoom.ensure(screen)
        self.assertTrue(facts["ok"])
        self.assertFalse(facts["pressed"])
        self.assertEqual(screen.events, [])

    def test_125_is_reset_with_ctrl_zero_and_rechecked(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=True), \
             mock.patch.object(zoom, "percent", side_effect=[125, 100]):
            facts = zoom.ensure(screen, settle=0)
        self.assertEqual((facts["before"], facts["after"]), (125, 100))
        self.assertTrue(facts["pressed"])
        self.assertEqual(screen.events, [("hotkey", ("ctrl", "0"))])

    def test_still_not_100_after_ctrl_zero_raises(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=True), \
             mock.patch.object(zoom, "percent", return_value=125):
            with self.assertRaises(zoom.ZoomNot100) as caught:
                zoom.ensure(screen, settle=0)
        self.assertEqual(caught.exception.stage, "zoom_not_100")
        self.assertEqual(caught.exception.facts["after"], 125)

    def test_non_browser_is_never_touched(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=False), \
             mock.patch.object(zoom, "percent",
                               side_effect=AssertionError("非浏览器不该读页面缩放")):
            facts = zoom.ensure(screen)
        self.assertEqual(facts["skipped"], "not_browser")
        self.assertEqual(screen.events, [])

    def test_browser_without_zoom_readout_fails_closed(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=True), \
             mock.patch.object(zoom, "percent", return_value=None):
            with self.assertRaises(zoom.ZoomNot100):
                zoom.ensure(screen)
        self.assertEqual(screen.events, [])

    def test_unknown_process_does_not_get_ctrl_zero(self):
        screen = FakeScreen()
        with mock.patch.object(zoom, "is_browser", return_value=None), \
             mock.patch.object(zoom, "percent", return_value=125):
            with self.assertRaises(zoom.ZoomNot100) as caught:
                zoom.ensure(screen)
        self.assertIn("进程名", str(caught.exception))
        self.assertEqual(screen.events, [])

    def test_scales_are_clamped_to_card_range(self):
        got = zoom.clamp_scales((0.5, 0.8, 1.0, 1.25, 2.0, "x"))
        self.assertEqual(got, (1.0, 0.8, 1.25))
        self.assertEqual(got[0], 1.0)

    def test_multiscale_records_scale_and_rejects_duplicates(self):
        lib = Path(tempfile.mkdtemp())
        pat = np.zeros((30, 60), np.uint8)
        cv2.rectangle(pat, (1, 1), (58, 28), 210, -1)
        cv2.putText(pat, "EXP", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 20, 1)
        cv2.imwrite(str(lib / "chip.png"), pat)
        small = cv2.resize(pat, None, fx=0.8, fy=0.8, interpolation=cv2.INTER_AREA)
        canvas = np.full((200, 300, 3), 255, np.uint8)
        canvas[60:60 + small.shape[0], 80:80 + small.shape[1]] = cv2.cvtColor(
            small, cv2.COLOR_GRAY2BGR)

        class S(Screen):
            def __init__(self, image):
                super().__init__(library=lib, verbose=False)
                self.image = image

            def grab(self, region=None):
                return self.image.copy()

        got = S(canvas).find_unique("chip", threshold=0.9, region=(0, 0, 300, 200),
                                    scales=(1.0, 0.9, 0.8))
        self.assertIsNotNone(got["match"])
        self.assertEqual(got["scale"], 0.8)
        self.assertEqual(got["match"].scale, 0.8)

        twice = canvas.copy()
        twice[120:120 + small.shape[0], 200:200 + small.shape[1]] = cv2.cvtColor(
            small, cv2.COLOR_GRAY2BGR)
        dup = S(twice).find_unique("chip", threshold=0.9, region=(0, 0, 300, 200),
                                   scales=(1.0, 0.9, 0.8))
        self.assertTrue(dup["ambiguous"])
        self.assertIsNone(dup["match"], "同一模板多处命中必须判失败")


# ================================================================ 7. 最小化
class MinimizeTests(unittest.TestCase):
    class _Api:
        """最小化 → 恢复的状态机。ShowWindow 故意返回 False：它本来就不可信。"""

        def __init__(self, *, restore_ok=True):
            self.restore_ok = restore_ok
            self.shown = 0
            self.iconic = True

        def is_window(self, hwnd):
            return True

        def is_iconic(self, hwnd):
            return self.iconic

        def show(self, hwnd, cmd):
            self.shown += 1
            if self.restore_ok:
                self.iconic = False
            return False

        def rect(self, hwnd):
            if self.shown and self.restore_ok:
                return (10, 20, 800, 600)
            return (-32000, -32000, 160, 28)

    def test_restore_polls_until_rect_is_usable(self):
        api = self._Api(restore_ok=True)
        facts = window_mod.restore(1, timeout=1.0, api=api)
        self.assertTrue(facts["ok"], facts)
        self.assertEqual(facts["reason"], "restored")
        self.assertEqual(facts["rect"], [10, 20, 800, 600])

    def test_restore_failure_reports_iconic(self):
        api = self._Api(restore_ok=False)
        facts = window_mod.restore(1, timeout=0.2, api=api)
        self.assertFalse(facts["ok"])
        self.assertIn(facts["reason"], ("iconic", "sentinel", "too_small"))

    def test_bound_rect_raises_exit_seven(self):
        screen = Screen(verbose=False)
        screen.target_hwnd = 1
        with mock.patch.object(window_mod, "restore",
                               return_value={"ok": False, "reason": "iconic",
                                             "rect": [-32000, -32000, 160, 28],
                                             "waited": 5.0}):
            with self.assertRaises(window_mod.WindowMinimized) as caught:
                screen.bound_rect()
        self.assertEqual(caught.exception.code, 7)


# ================================================================ 8. 人机验证
class HumanCheckTests(unittest.TestCase):
    def test_cloudflare_short_label_is_detected(self):
        root = FakeNode("DocumentControl", "", [FakeNode("TextControl", "Verify you are human")])
        got = human.detect(root)
        self.assertTrue(got["hit"])
        self.assertEqual(got["kind"], "cloudflare")

    def test_chinese_cloudflare_label(self):
        root = FakeNode("DocumentControl", "", [FakeNode("TextControl", "确认您是真人")])
        self.assertEqual(human.detect(root)["kind"], "cloudflare")

    def test_long_text_mentioning_cloudflare_is_not_a_challenge(self):
        text = "Cloudflare 是一家 CDN 厂商，本段是评审意见里的普通讨论，不该被当成人机验证"
        root = FakeNode("DocumentControl", "", [FakeNode("TextControl", text)])
        self.assertFalse(human.detect(root)["hit"])

    def test_unknown_error_is_classified_separately(self):
        root = FakeNode("DocumentControl", "", [FakeNode("TextControl", "Unknown error")])
        got = human.detect(root)
        self.assertEqual(got["kind"], "unknown_error")
        self.assertEqual(human.HumanCheck("unknown_error").reason, "unknown_error")

    def test_title_just_a_moment_counts(self):
        got = human.detect(None, title="Just a moment...")
        self.assertTrue(got["hit"])

    def test_pre_send_reads_the_chip_without_calming(self):
        """发送前那次只读不能按 Esc/移鼠标：会被 guard 当成用户输入。"""
        seen = {}

        def fake_read(*args, **kwargs):
            seen.update(kwargs)
            return {"value": "expert", "source": "template"}

        def fake_gate(endpoint, purpose, **kwargs):
            kwargs["reread"]("model")
            return {"ok": True, "receipt": {"dimension": "model"}}

        with mock.patch.object(sendgate.tier_guard, "gate", side_effect=fake_gate), \
             mock.patch.object(sendgate.chip, "read", side_effect=fake_read), \
             mock.patch.object(sendgate.ui, "title_of", return_value=""), \
             mock.patch.object(sendgate.ui, "document", return_value=(object(), "u")):
            sendgate.pre_send(FakeScreen(), "grok", "test", root=FakeNode())
        self.assertTrue(seen.get("calm") is False)

    def test_pre_send_stops_before_the_tier_gate(self):
        """人机验证先判：不点、不重试，也不消费档位回执。"""
        root = FakeNode("DocumentControl", "", [FakeNode("TextControl", "Verify you are human")])
        screen = FakeScreen()
        with mock.patch.object(sendgate.ui, "title_of", return_value=""):
            with self.assertRaises(human.HumanCheck) as caught:
                sendgate.pre_send(screen, "grok", "test", root=root)
        self.assertEqual(caught.exception.stage, "human_check")
        self.assertEqual(screen.events, [])


# ================================================================ 9. 失败留证
class FailureEvidenceTests(Base):
    def test_wanted_matrix(self):
        for stage in ("tier_unconfirmed", "tier_unreadable", "tier_drift",
                      "zoom_not_100", "human_check", "input_mismatch"):
            self.assertTrue(failure.wanted(stage), stage)
        for stage in ("answered", "done", "timeout", "interrupted", ""):
            self.assertFalse(failure.wanted(stage), stage)

    def test_capture_writes_png_json_and_log(self):
        result = {"ok": False, "stage": "tier_unconfirmed", "reason": "tier_unreadable",
                  "code": 5, "hint": "读不到", "zoom_percent": 125,
                  "template": {"name": "grok_chip_fast", "score": 0.41,
                               "threshold": 0.85, "scale": 1.0, "hit": False}}
        with mock.patch.object(failure, "FAIL_DIR", self.tmp / "fail"), \
             mock.patch.object(runlog, "RUNS", self.tmp / "runs"):
            got = failure.record("grok", result, screen=FakeScreen(), hwnd=None)
        self.assertTrue(Path(got["png"]).exists())
        meta = json.loads(Path(got["json"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(meta["endpoint"], "grok")
        self.assertEqual(meta["stage"], "tier_unconfirmed")
        self.assertEqual(meta["templates"][0]["score"], 0.41)
        self.assertEqual(meta["window"]["zoom_percent"], 125)
        self.assertEqual(result["evidence"], got)
        lines = (self.tmp / "runs").glob("*.jsonl")
        row = json.loads(next(iter(lines)).read_text(encoding="utf-8").strip())
        self.assertEqual(row["result"], "fail_evidence")
        self.assertEqual(row["evidence"]["stage"], "tier_unconfirmed")

    def test_capture_survives_a_failed_screenshot(self):
        class Bad(FakeScreen):
            def grab(self, region=None):
                raise RuntimeError("BitBlt 失败")
        result = {"ok": False, "stage": "input_mismatch", "reason": "mismatch"}
        with mock.patch.object(failure, "FAIL_DIR", self.tmp / "fail"), \
             mock.patch.object(runlog, "RUNS", self.tmp / "runs"):
            got = failure.record("grok", result, screen=Bad())
        self.assertIsNone(got["png"])
        self.assertTrue(Path(got["json"]).exists())

    def test_repeated_failure_reuses_the_last_screenshot(self):
        """外部巡检 10 秒一重试时，别每次都写一张 4.6MB 的整屏图。"""
        result = {"ok": False, "stage": "tier_unconfirmed",
                  "reason": "tier_drift", "hint": "重复"}
        with mock.patch.object(failure, "FAIL_DIR", self.tmp / "fail"), \
             mock.patch.object(runlog, "RUNS", self.tmp / "runs"):
            first = failure.capture("grok", result, screen=FakeScreen())
            second = failure.capture("grok", dict(result), screen=FakeScreen())
        self.assertTrue(first["png"])
        self.assertEqual(second["png"], first["png"])
        self.assertEqual(len(list((self.tmp / "fail").glob("*.png"))), 1,
                         "30 秒内同一种失败只该有一张图")
        meta = json.loads(Path(second["json"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["deduped"], first["png"])

    def test_full_desktop_capture_is_downscaled(self):
        class Big(FakeScreen):
            def grab(self, region=None):
                return np.zeros((900, 2400, 3), dtype=np.uint8)

        result = {"ok": False, "stage": "zoom_not_100", "reason": "still_125"}
        with mock.patch.object(failure, "FAIL_DIR", self.tmp / "fail"), \
             mock.patch.object(runlog, "RUNS", self.tmp / "runs"):
            got = failure.capture("grok", result, screen=Big(), hwnd=None)
        img = cv2.imread(str(got["png"]))
        self.assertLessEqual(img.shape[1], failure.MAX_FULL_WIDTH)

    def test_non_target_stage_writes_nothing(self):
        with mock.patch.object(failure, "FAIL_DIR", self.tmp / "fail"):
            self.assertIsNone(failure.capture("grok", {"ok": False, "stage": "timeout"},
                                              screen=FakeScreen()))
        self.assertFalse((self.tmp / "fail").exists())


# ================================================================ 10. chatgpt 假读回
class ChatgptReadbackTests(Base):
    def test_name_key_normalizes_annotations(self):
        choose = SCRIPTS / "chatgpt_choose.py"
        ns = {"MODELS": literal_assignment(choose, "MODELS")}
        name_key = load_function(choose, "_name_key", ns)
        self.assertEqual(name_key("GPT-6（默认）"), name_key("GPT-6"))
        self.assertEqual(name_key("默认（推荐模型集）"), "默认")

    def test_effort_words_are_longest_first(self):
        choose = SCRIPTS / "chatgpt_choose.py"
        ns = {"_CHIP_EFFORT_WORDS": literal_assignment(choose, "_CHIP_EFFORT_WORDS")}
        fn = load_function(choose, "_effort_of_text", ns)
        self.assertEqual(fn("极高"), "xhigh")
        self.assertEqual(fn("高"), "high")
        self.assertEqual(fn("思考强度"), None)

    def test_uia_radio_reports_selected_model(self):
        choose = SCRIPTS / "chatgpt_choose.py"
        # B17：read_model_uia 现在会跳过视口外的选中项；这个桩没有可见性信息，
        # 统一按「可见」处理（隐藏项的行为由 test_b17_chatgpt_uia_scroll 覆盖）。
        ns = {"MODELS": literal_assignment(choose, "MODELS"), "Screen": object,
              "_offscreen": lambda node: False}
        ns["_name_key"] = load_function(choose, "_name_key", {})

        def radio(name, selected):
            node = FakeNode("RadioButtonControl", name)
            node.control = types.SimpleNamespace(
                GetSelectionItemPattern=lambda: types.SimpleNamespace(IsSelected=selected))
            return node

        root = FakeNode("DocumentControl", "", [radio("GPT-6", True),
                                                radio("GPT-5.6 Sol", False)])
        fn = load_function(choose, "read_model_uia", {**ns, "_doc_root": lambda s: root})
        got = fn(FakeScreen(), "chat")
        self.assertEqual(got["model"], "gpt6")
        self.assertEqual(got["source"], "uia")

    def test_uia_radio_multiple_selected_is_unknown(self):
        choose = SCRIPTS / "chatgpt_choose.py"
        ns = {"MODELS": literal_assignment(choose, "MODELS"), "Screen": object,
              "_offscreen": lambda node: False}
        ns["_name_key"] = load_function(choose, "_name_key", {})

        def radio(name):
            node = FakeNode("RadioButtonControl", name)
            node.control = types.SimpleNamespace(
                GetSelectionItemPattern=lambda: types.SimpleNamespace(IsSelected=True))
            return node

        root = FakeNode("DocumentControl", "", [radio("GPT-6"), radio("GPT-5.6 Sol")])
        fn = load_function(choose, "read_model_uia", {**ns, "_doc_root": lambda s: root})
        got = fn(FakeScreen(), "chat")
        self.assertEqual(got["model"], "unknown")
        self.assertEqual(got["reason"], "multiple_selected")

    def test_pick_model_no_longer_infers_default(self):
        """旧版"勾在所有可读项上方 → 默认"的推断必须消失（第 10 条的根因）。"""
        choose = SCRIPTS / "chatgpt_choose.py"
        ns = {
            "Screen": object,
            "read_model_uia": lambda s, w: {"model": "unknown",
                                            "reason": "no_selected_radio"},
            "_pick_model_templates": lambda s, w: {
                "model": "unknown", "source": "template",
                "reason": "ambiguous_check"},
        }
        fn = load_function(choose, "_pick_model", ns)
        got = fn(FakeScreen(), "chat")
        self.assertEqual(got["model"], "unknown")
        self.assertNotEqual(got.get("inferred"), "勾在所有可读项上方，选中的是第一行「默认」")

    def test_set_effort_chip_wins_over_slider_value(self):
        choose = SCRIPTS / "chatgpt_choose.py"
        slider = {"knob": 1000, "cy": 10, "win": (0, 0, 800, 600)}
        ns = {
            "Screen": object, "time": types.SimpleNamespace(sleep=lambda s: None),
            "EFFORTS": {"chat": {"low": ("即时", 1013), "mid": ("中", 1115),
                                 "high": ("高", 1217)}},
            "read_writer": lambda s: {"writer": "chat"},
            "effort_order": lambda w: ["low", "mid", "high"],
            "_gap_limit": lambda w: 20,
            "_effort_of": lambda knob, w: ("high", 0),
            "open_effort": lambda s, w: True,
            "_effort_item": lambda s: None,
            "_slider": lambda s: dict(slider),
            "safe_click": lambda s, point: (True, "ok"),
            "_announce_effort": lambda s, root=None, writer=None: {
                "effort": "high", "raw": "高，第 3 项，共 3 项。", "source": "uia_slider"},
            "close_all": lambda s, w: None,
            "read_chip_effort": lambda s: {"effort": "mid", "raw": "中",
                                           "source": "uia_chip"},
        }
        fn = load_function(choose, "set_effort", ns)
        got = fn(FakeScreen(), "high")
        self.assertFalse(got["ok"], "芯片显示 mid，滑杆读数不能盖过芯片")
        self.assertEqual(got["reason"], "readback_mismatch")
        self.assertEqual(got["after"], "mid")

        ns["read_chip_effort"] = lambda s: {"effort": "high", "raw": "高",
                                            "source": "uia_chip"}
        fn = load_function(choose, "set_effort", ns)
        self.assertTrue(fn(FakeScreen(), "high")["ok"])

    def test_set_effort_falls_back_to_slider_value_when_chip_is_unreadable(self):
        """2026-11-09：新版芯片不再暴露档位文本，滑杆可访问值成为唯一 UIA 读数。

        仍然要求"读回等于目标"；只是读数来源从芯片换成滑杆自己念出的第 N 项。
        """
        choose = SCRIPTS / "chatgpt_choose.py"
        slider = {"knob": 1000, "cy": 10, "win": (0, 0, 800, 600)}
        ns = {
            "Screen": object, "time": types.SimpleNamespace(sleep=lambda s: None),
            "EFFORTS": {"chat": {"low": ("即时", 0), "mid": ("中", 1), "high": ("高", 2)}},
            "read_writer": lambda s: {"writer": "chat"},
            "effort_order": lambda w: ["low", "mid", "high"],
            "_gap_limit": lambda w: 20,
            "_effort_of": lambda knob, w: ("high", 0),
            "open_effort": lambda s, w: True,
            "_effort_item": lambda s: None,
            "_slider": lambda s: dict(slider),
            "safe_click": lambda s, point: (True, "ok"),
            "_announce_effort": lambda s, root=None, writer=None: {
                "effort": "high", "raw": "高，第 3 项，共 3 项。", "source": "uia_slider"},
            "close_all": lambda s, w: None,
            "read_chip_effort": lambda s: {"effort": None, "raw": "", "source": "none"},
        }
        fn = load_function(choose, "set_effort", ns)
        got = fn(FakeScreen(), "high")
        self.assertTrue(got["ok"])
        self.assertEqual(got["read"], "uia_slider")
        self.assertEqual(got["after"], "high")

        # 两个来源都读不到时仍然 fail-closed
        ns["_announce_effort"] = lambda s, root=None, writer=None: {
            "effort": None, "raw": "", "source": "none"}
        fn = load_function(choose, "set_effort", ns)
        got = fn(FakeScreen(), "high")
        self.assertFalse(got["ok"])
        self.assertEqual(got["reason"], "effort_unreadable")


# ================================================================ 11. 重复行
class NoDuplicateLinesTests(unittest.TestCase):
    def test_no_adjacent_duplicate_statements(self):
        """CARD-06b 第 11 条：同一段里不许有相邻的重复语句（opencode_use 那处）。"""
        bad = []
        files = sorted(list(SRC.rglob("*.py")) + list((ROOT / "tools").rglob("*.py")))
        for path in files:
            if ".bak" in path.name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list):
                    continue
                for first, second in zip(body, body[1:]):
                    if isinstance(first, (ast.Pass, ast.Expr)):
                        continue
                    if ast.dump(first) == ast.dump(second):
                        bad.append(f"{path.relative_to(ROOT)}:{first.lineno}")
        self.assertEqual(bad, [], f"相邻重复语句：{bad}")

    def test_opencode_aborted_assignment_appears_once(self):
        text = (SCRIPTS / "opencode_use.py").read_text(encoding="utf-8")
        self.assertEqual(text.count('r = {"ok": False, "stage": "aborted", "hint": str(e)}'), 1)


# ================================================================ 退出码/文档
class ExitAndDocsTests(unittest.TestCase):
    def test_new_stages_are_registered(self):
        from qi import exit_codes as ec
        self.assertEqual(ec.code_for("zoom_not_100"), 1)
        self.assertEqual(ec.code_for("human_check"), 1)
        self.assertEqual(ec.code_for("tier_unconfirmed"), 5)
        # 新 stage 不许挤占 0–7 之外的码
        self.assertEqual(ec.code_for("whatever"), 1)

    def test_docs_mention_new_reasons(self):
        doc = (ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8")
        for word in ("tier_unreadable", "tier_drift", "receipt_identity",
                     "zoom_not_100", "human_check", "fail_evidence"):
            self.assertIn(word, doc, f"docs/exit-codes.md 少了 {word}")


if __name__ == "__main__":
    unittest.main()
