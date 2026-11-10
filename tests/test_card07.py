"""CARD-07：回复检测与输入框。全离线，不开窗口、不截图、不点桌面、不发消息。

三条修复各一组用例（卡面第 5 条要求覆盖 1–3 的**回退分支**）：

    1  workbuddy：页面已有回复却报 no_reply
       · 共享判据认不出 WorkBuddy 的「孤零零一个复制键」→ 本端 `reply_copy` 认
       · 提问气泡在树里被截断 → 前缀认领
       · 停止键还在 → 不算答完；连续 2 轮不变 → 答完（basis/read_via 都写出来）
       · 超时前最后那次读：读到就交卷（read_via=uia），读不到才是 no_reply
    2  gemini：动作条不出现时按「停止键消失 + 文本 2 轮不变」判完成，
       树里一块都切不出来时用页面全文兜底，并把判据写进结果（runlog 用）
    3  codex：最小化 → 恢复失败退 7；composer 模板失配 → UIA 找编辑框、
       点它获焦、重找模板，仍不认就用 UIA 落点

假控件沿用 test_card06d 的形状：`ui.Node` + 只有 `IsEnabled/IsOffscreen/BoundingRectangle`
的 control，够 `workbuddy.reply_text` 按矩形归行。
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import argparse
import ast
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SRC / "scripts") not in sys.path:
    sys.path.insert(0, str(SRC / "scripts"))

from qi import gemini, ui, workbuddy  # noqa: E402

PROMPT = "只回复 07-OK"
ANSWER = "07-OK"


# ---------------------------------------------------------------- 假件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=(), width=100, height=20,
         left=0, top=0, enabled=True, offscreen=False, control=None):
    item = ui.Node(kind, name, control or SimpleNamespace(
        IsEnabled=enabled, IsOffscreen=offscreen, BoundingRectangle=box(width, height, left, top)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def text(value, left=0, top=0):
    return node("TextControl", value, width=90, height=20, left=left, top=top)


def button(name, left=0, top=0, enabled=True):
    return node("ButtonControl", name, width=24, height=24, left=left, top=top,
                enabled=enabled)


def bubble(answer=ANSWER, prompt_line=None, bar_kind="ToolBarControl"):
    """一条回复气泡：正文 + `ToolBar[08:46, 复制]`（2026-11-02 真机的形状）。

    共享判据要的"赞/踩/重做"兄弟**故意不给**——WorkBuddy 现在就是没有。
    """
    lines = [text("WorkBuddy", top=0), text("预计消耗", left=100, top=0),
             text("0.41 ~ 18.23", left=200, top=0)]
    lines += [text(answer, top=40)]
    lines += [text("已消耗", top=80), text("1.97", left=60, top=80)]
    bar = node(bar_kind, "", [text("08:46", top=120), button("复制", left=40, top=120)])
    bubble_node = node("GroupControl", "", lines + [bar], height=200)
    wrapper = node("GroupControl", "", [bubble_node], height=220)
    doc = node("DocumentControl", "", [wrapper], height=400)
    if prompt_line is not None:
        doc.children.insert(0, text(prompt_line))
        doc.children[0].parent = doc
    return doc, bar.children[1]


def stop_button(top=300):
    return node("ButtonControl", "停止", width=24, height=24, left=0, top=top)


class Clock:
    """sleep 多少 monotonic 就走多少：等待循环在测试里是瞬移的。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeConversation:
    """按顺序吐视图，最后一个会一直重复（和真页面"稳定下来"等价）。

    两种喂法都收：整棵 `ui.Node`（workbuddy 那边自己算 busy），或者现成的视图对象
    （gemini 那边的 busy 是视图上写好的）。
    """

    def __init__(self, roots, copies=None):
        self.roots = list(roots)
        self.i = 0
        self.prompt = PROMPT
        self.read_via = None
        self.copy_note = None
        self.copy_calls = []
        self.copied = copies or {}
        self.site = "workbuddy"
        self.screen = None

    def read(self, attempts=4, delay=0.4):
        index = min(self.i, len(self.roots) - 1)
        self.i += 1
        item = self.roots[index]
        if isinstance(item, Exception):
            raise item
        if hasattr(item, "root"):            # 已经是一个视图
            return item
        busy = any(n.kind == "ButtonControl" and n.name == "停止" and n.enabled
                   for n in item.walk())
        return SimpleNamespace(root=item, busy=busy, value="", url="", editor=None,
                               copies=[], site=self.site)

    def write(self, view, prompt):
        """gemini 的写输入：测试里不真打字，记一笔就够。"""
        self.written = prompt

    def copy(self, node_):
        self.copy_calls.append(node_)
        value = self.copied.get("value")
        if isinstance(value, Exception):
            raise value
        self.read_via = self.copied.get("via", "clipboard")
        return "" if value is None else value


def doc(*children):
    return node("DocumentControl", "", list(children), height=600)


# ================================================================ 1. workbuddy
class ReplyCopyTests(unittest.TestCase):
    """共享 `is_reply_copy` 认不出的那个复制键，本端得认出来。"""

    def test_copy_in_a_toolbar_counts(self):
        root, copy = bubble()
        self.assertEqual(workbuddy.reply_copies(list(root.walk())), [copy])

    def test_shared_predicate_still_says_no(self):
        """这是本条修复的**理由**：共享判据对这个形状返回 False。"""
        from qi.conversation import is_reply_copy

        _root, copy = bubble()
        self.assertFalse(is_reply_copy(copy))

    def test_copy_outside_a_toolbar_is_not_counted(self):
        stray = node("GroupControl", "", [button("复制")])
        root = doc(stray)
        self.assertEqual(workbuddy.reply_copies(list(root.walk())), [])

    def test_disabled_copy_is_not_counted(self):
        root, _copy = bubble()
        bar = [n for n in root.walk() if n.kind == "ToolBarControl"][0]
        bar.children[1] = button("复制", left=40, top=120, enabled=False)
        bar.children[1].parent = bar
        self.assertEqual(workbuddy.reply_copies(list(root.walk())), [])


class PromptAttributionTests(unittest.TestCase):
    """提问气泡在树里是截断的，必须按前缀认领。"""

    LONG = "请核对 CARD-07 的回复检测，只回一句 07-OK 就可以"

    def test_truncated_prompt_is_claimed(self):
        root, _copy = bubble()
        truncated = node("TextControl", self.LONG[:16])
        nodes = list(doc(truncated, root).walk())
        self.assertEqual(workbuddy.prompt_at(nodes, self.LONG), 1)

    def test_short_fragment_is_not_enough(self):
        """太短的前缀不算认领（和 gemini 的 MIN_PREFIX 同一条约定）。"""
        nodes = list(doc(node("TextControl", self.LONG[:5])).walk())
        self.assertIsNone(workbuddy.prompt_at(nodes, self.LONG))

    def test_unrelated_text_is_not_claimed(self):
        nodes = list(doc(node("TextControl", "别的对话")).walk())
        self.assertIsNone(workbuddy.prompt_at(nodes, PROMPT))

    def test_takes_the_last_occurrence(self):
        first = node("TextControl", PROMPT)
        second = node("TextControl", PROMPT)
        nodes = list(doc(first, second).walk())
        self.assertEqual(workbuddy.prompt_at(nodes, PROMPT), 2)

    def test_question_is_never_returned_as_the_answer(self):
        """提问气泡上也有复制键时，绝不能把提问当回答交出去。"""
        question = node("GroupControl", "", [
            text(PROMPT, top=0),
            node("ToolBarControl", "", [button("复制", left=40, top=40)]),
        ])
        nodes = list(doc(question).walk())
        text_, _node = workbuddy.last_reply(nodes, PROMPT, None)
        self.assertIsNone(text_)


class WaitReplyTests(unittest.TestCase):
    """等待循环：停止键消失 + 正文连续 2 轮不变。"""

    def run_wait(self, roots, timeout=5.0, copies=None):
        clock = Clock()
        conv = FakeConversation(roots, copies)
        with mock.patch.object(workbuddy, "time", clock):
            result = workbuddy.wait_reply(conv, PROMPT, timeout)
        return result, conv, clock

    def test_answer_after_two_identical_reads(self):
        root, _copy = bubble(prompt_line=PROMPT)
        result, conv, clock = self.run_wait([root], copies={"value": ANSWER})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["basis"], "uia_stop_gone_and_stable_text")
        self.assertEqual(result["read_via"], "clipboard")
        self.assertLess(clock.now, 2.0, "页面已经答完了，不该等满超时")
        self.assertTrue(conv.copy_calls, "答完之后要走原来的复制路（剪贴板优先）")

    def test_copy_that_returns_the_prompt_is_not_the_answer(self):
        """2026-11-02 真机：剪贴板里还是提问本身 → 用树里的正文，read_via 写 uia。"""
        root, _copy = bubble(prompt_line=PROMPT)
        result, conv, _clock = self.run_wait([root], copies={"value": PROMPT})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["read_via"], "uia")
        self.assertIn("提问本身", conv.copy_note["copy"])

    def test_stop_button_means_still_generating(self):
        root, _copy = bubble()
        generating = doc(node("TextControl", PROMPT, top=10),
                         node("GroupControl", "", [*root.children[0].children, stop_button()]))
        result, _conv, _clock = self.run_wait([generating], timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "timeout")
        self.assertEqual(result["timeout_kind"], "still_generating")

    def test_clipboard_failure_falls_back_to_uia_text(self):
        root, _copy = bubble(prompt_line=PROMPT)
        result, conv, _clock = self.run_wait(
            [root], copies={"value": RuntimeError("剪贴板没更新")})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER, "复制失败就用刚读到的树正文")
        self.assertEqual(result["read_via"], "uia")
        self.assertIn("剪贴板没更新", conv.copy_note["copy"])

    def test_no_reply_when_nothing_is_there(self):
        empty = doc(node("TextControl", "今天帮你做些什么？"))
        result, _conv, _clock = self.run_wait([empty], timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["timeout_kind"], "no_reply")

    def test_final_read_saves_a_reply_the_loop_never_saw(self):
        """循环里一直只看到空页面，超时前最后那次读到了回复 → 按成功返回。"""
        empty = doc(node("TextControl", "问问 Gemini"))
        root, _copy = bubble(prompt_line=PROMPT)
        result, conv, _clock = self.run_wait([empty, empty, root], timeout=1.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["basis"], "uia_final_read")
        self.assertEqual(result["read_via"], "uia")
        self.assertEqual(result["attribution"], "prompt")
        self.assertFalse(conv.copy_calls, "最后一次读是纯只读，不许点复制键")

    def test_final_read_refuses_the_previous_turn(self):
        """认领不到提问、复制键也没比发送前多 → 那是上一轮的答案，不交卷。"""
        root, _copy = bubble()
        result = workbuddy.final_read(
            FakeConversation([root]), PROMPT,
            {"ok": False, "submission": "unknown", "sent": None}, baseline=1)
        self.assertIsNone(result)


class SendThenWaitTests(unittest.TestCase):
    """`workbuddy.ask`：发送交给共享 ask（0 预算），等待交给本端。"""

    def test_not_sent_results_are_passed_through(self):
        conv = FakeConversation([doc(node("TextControl", "x"))])
        sent = {"ok": False, "stage": "input_mismatch", "hint": "写歪了"}
        with mock.patch.object(workbuddy, "send_and_wait", return_value=sent) as shared:
            result = workbuddy.ask(conv, PROMPT, 5.0)
        self.assertEqual(result, sent)
        self.assertEqual(shared.call_args.args[2], 0.0, "共享 ask 只负责发送")

    def test_zero_budget_timeout_is_not_a_failure(self):
        root, _copy = bubble(prompt_line=PROMPT)
        conv = FakeConversation([root])
        sent = {"ok": False, "stage": "timeout", "timeout_kind": "no_reply"}
        with mock.patch.object(workbuddy, "send_and_wait", return_value=sent), \
                mock.patch.object(workbuddy, "time", Clock()):
            result = workbuddy.ask(conv, PROMPT, 5.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER)


# ================================================================ 2. gemini
def flat(*items):
    """按 "T:文本" / "B:按钮" / "E:编辑框" 造一棵平铺的树（抄 test_cardweb2 的写法）。"""
    children = []
    for index, item in enumerate(items):
        kind, _, value = item.partition(":")
        node_ = node({"T": "TextControl", "B": "ButtonControl", "E": "EditControl"}[kind],
                     value, width=120, height=20, top=30 * index)
        children.append(node_)
    return node("DocumentControl", "", children, height=600)


def gview(root, value="", busy=False):
    return SimpleNamespace(root=root, value=value, busy=busy,
                           url="https://gemini.google.com/app", editor=None,
                           copies=[], site="gemini")


class DomTextTests(unittest.TestCase):
    """页面全文兜底：取提问之后那段，剔掉按钮名。"""

    PAGE = "\n".join([
        "你说", PROMPT, "Gemini 说", "07-OK", "答得好", "复制", "问问 Gemini",
    ])

    def test_takes_text_after_the_last_prompt(self):
        got = gemini._text_after_prompt(self.PAGE, PROMPT)
        self.assertEqual(got, "07-OK")

    def test_prompt_missing_returns_empty(self):
        self.assertEqual(gemini._text_after_prompt("没有提问的页面", PROMPT), "")

    def test_truncated_prompt_still_matches(self):
        long_prompt = "请核对 CARD-07 的回复检测，只回一句 07-OK 就可以"
        page = f"你说\n{long_prompt[:16]}…\nGemini 说\n07-OK"
        self.assertEqual(gemini._text_after_prompt(page, long_prompt), "07-OK")


class FakeTextControl:
    """DocumentControl 的假件：只实现 GetPattern(TextPattern)。"""

    def __init__(self, page):
        self.page = page

    def GetPattern(self, _pattern):
        page = self.page
        return SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _n: page))


class GeminiCompletionTests(unittest.TestCase):
    """动作条不出现时，按「停止键消失 + 文本连续 2 次不变」判完成。"""

    SITE = SimpleNamespace(name="gemini", title_hint="Google Chrome", on_secondary=True,
                           send="gem_send_on", send_threshold=0.88, newchat="gem_newchat")

    def run_wait(self, views, timeout=60.0, prompt=PROMPT, answer=None):
        clock = Clock()
        conv = FakeConversation(views)
        conv.copied = {}
        with mock.patch.object(gemini, "Conversation", return_value=conv), \
                mock.patch.object(gemini, "_submit", return_value=None), \
                mock.patch.object(gemini, "Screen", return_value=object()), \
                mock.patch.object(gemini, "time", clock), \
                mock.patch.object(gemini, "SETTLE", 3.0):
            result = gemini._send_and_wait(object(), self.SITE, prompt, timeout)
        return result, clock

    def test_answer_without_action_bar_is_judged_by_stable_text(self):
        tree = flat("T:你说", f"T:{PROMPT}", "T:Gemini 说", f"T:{ANSWER}")
        result, clock = self.run_wait([gview(flat("T:问问 Gemini")),
                                       gview(flat("T:问问 Gemini"), value=PROMPT),
                                       gview(tree)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["basis"], "stop_gone_and_stable_text")
        self.assertEqual(result["criteria"], "stop_gone+text_stable_2_reads")
        self.assertLess(clock.now, 10.0, "两轮不变就该交卷，不干等 3 秒")

    def test_bar_appearing_later_does_not_crash_the_settle_timer(self):
        """先按"没有动作条"记了一轮，下一轮动作条出来了：不能拿 None 去算时间差。"""
        no_bar = flat("T:你说", f"T:{PROMPT}", "T:Gemini 说", f"T:{ANSWER}")
        with_bar = flat("T:你说", f"T:{PROMPT}", "T:Gemini 说", f"T:{ANSWER}",
                        "B:答得好", "B:复制")
        result, _clock = self.run_wait([gview(flat("T:问问 Gemini")),
                                        gview(flat("T:问问 Gemini"), value=PROMPT),
                                        gview(no_bar), gview(with_bar)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["basis"], "tree_reply_bar_and_stable_text")

    def test_stop_button_blocks_completion(self):
        tree = flat("T:你说", f"T:{PROMPT}", "T:Gemini 说", f"T:{ANSWER}")
        result, _clock = self.run_wait([gview(flat("T:问问 Gemini")),
                                        gview(flat("T:问问 Gemini"), value=PROMPT),
                                        gview(tree, busy=True)], timeout=2.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "timeout")

    def test_dom_text_is_the_last_resort(self):
        """树里一块都切不出来：读页面全文，取提问之后那段。"""
        page = f"你说\n{PROMPT}\nGemini 说\n{ANSWER}\n答得好"
        root = flat("T:问问 Gemini")
        root.control = FakeTextControl(page)
        result, _clock = self.run_wait([gview(root), gview(root, value=PROMPT), gview(root)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["read_via"], "dom")
        self.assertEqual(result["basis"], "dom_text_after_prompt")
        self.assertIn("dom", result["criteria"])

    def test_dom_text_is_not_used_while_generating(self):
        page = f"你说\n{PROMPT}\nGemini 说\n{ANSWER}"
        root = flat("T:问问 Gemini")
        root.control = FakeTextControl(page)
        result, _clock = self.run_wait([gview(root, busy=True), gview(root, value=PROMPT),
                                        gview(root, busy=True)], timeout=2.0)
        self.assertFalse(result["ok"], "还在生成时不许拿全文当答案")


class CriteriaReachesTheLog(unittest.TestCase):
    """判据写进 runlog：`runlog.record` 只收固定字段，所以另记一行。"""

    def test_writes_one_extra_line_with_the_criterion(self):
        import tempfile

        from qi import runlog

        result = {"ok": True, "stage": "answered", "criteria": "stop_gone+text_stable_2_reads",
                  "basis": "stop_gone_and_stable_text", "submission": "accepted", "elapsed": 1.0}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runlog, "RUNS", Path(tmp)):
            gemini._log_criteria("gemini", result)
            lines = [json.loads(x) for x in (Path(tmp) / list(Path(tmp).glob("*.jsonl"))[0].name)
                     .read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["result"], "reply_criteria")
        self.assertEqual(lines[0]["criteria"], "stop_gone+text_stable_2_reads")

    def test_no_criterion_no_line(self):
        import tempfile

        from qi import runlog

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runlog, "RUNS", Path(tmp)):
            gemini._log_criteria("gemini", {"ok": False, "stage": "timeout"})
            self.assertEqual(list(Path(tmp).glob("*.jsonl")), [])


# ================================================================ 3. codex
def load_function(script, name, namespace):
    """把单文件脚本里的某个函数抠出来 exec（和 test_desktop_controls 同一招）。"""
    path = ROOT / "src" / "scripts" / script
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node_ = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.fix_missing_locations(ast.Module(body=future + [node_], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def codex_module():
    import importlib.util

    path = ROOT / "src" / "scripts" / "codex_use.py"
    name = "card07_codex_use"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CodexComposerTests(unittest.TestCase):
    """模板找不到输入框 → UIA 找编辑框 → 点它获焦 → 重找模板 → 用 UIA 落点。"""

    @classmethod
    def setUpClass(cls):
        cls.codex = codex_module()

    def fake_tree(self, *specs):
        nodes = []
        for kind, rect, enabled, offscreen in specs:
            left, top, width, height = rect
            nodes.append(ui.Node(kind, "", SimpleNamespace(
                IsEnabled=enabled, IsOffscreen=offscreen,
                BoundingRectangle=box(width, height, left, top))))
        return node("DocumentControl", "", nodes, height=1200)

    def test_picks_the_big_edit_box_in_the_lower_half(self):
        tree = self.fake_tree(
            ("EditControl", (10, 100, 300, 40), True, False),      # 上半窗：不要
            ("EditControl", (10, 900, 800, 60), True, False),      # 输入框
            ("EditControl", (10, 950, 60, 20), True, False))       # 太小
        with mock.patch("qi.ui.documents", return_value=[(object(), "app://codex")]), \
                mock.patch("qi.ui.tree", return_value=tree):
            got = self.codex.uia_edit_box(100, (0, 0, 1380, 1340))
        self.assertEqual(got, (410, 930))

    def test_skips_offscreen_and_disabled(self):
        tree = self.fake_tree(
            ("EditControl", (10, 900, 800, 60), False, False),
            ("EditControl", (10, 910, 800, 50), True, True))
        with mock.patch("qi.ui.documents", return_value=[(object(), "app://codex")]), \
                mock.patch("qi.ui.tree", return_value=tree):
            self.assertIsNone(self.codex.uia_edit_box(100, (0, 0, 1380, 1340)))

    def test_focus_then_template(self):
        """点完输入框，模板认了就按模板的落点来（basis 记下这一步）。"""
        calls = []
        desk = SimpleNamespace(click=lambda x, y: calls.append((x, y)))
        with mock.patch.object(self.codex, "uia_edit_box", return_value=(700, 1200)), \
                mock.patch.object(self.codex, "locate",
                                  return_value={"missing": [], "composer": (710, 1210),
                                                "send": (1200, 1200)}), \
                mock.patch.object(self.codex.time, "sleep"):
            got = self.codex.focus_composer(desk, 1, (0, 0, 1380, 1340), {"missing": []})
        self.assertEqual(calls, [(700, 1200)], "先点一下让它获焦")
        self.assertEqual(got["composer"], (710, 1210))
        self.assertEqual(got["composer_basis"], "uia_focus+template")

    def test_uia_point_wins_when_template_still_misses(self):
        desk = SimpleNamespace(click=lambda x, y: None)
        still = {"missing": [self.codex.COMPOSER_TPL], "send": (1200, 1200)}
        with mock.patch.object(self.codex, "uia_edit_box", return_value=(700, 1200)), \
                mock.patch.object(self.codex, "locate", return_value=still), \
                mock.patch.object(self.codex.time, "sleep"):
            got = self.codex.focus_composer(desk, 1, (0, 0, 1380, 1340), dict(still))
        self.assertEqual(got["composer"], (700, 1200))
        self.assertEqual(got["composer_basis"], "uia")
        self.assertNotIn(self.codex.COMPOSER_TPL, got["missing"])
        self.assertEqual(got["missing"], [], "只剩 composer 缺，不该再报 controls_not_found")

    def test_no_edit_box_keeps_the_original_result(self):
        desk = SimpleNamespace(click=lambda x, y: None)
        pm = {"missing": [self.codex.COMPOSER_TPL]}
        with mock.patch.object(self.codex, "uia_edit_box", return_value=None):
            self.assertIs(self.codex.focus_composer(desk, 1, (0, 0, 1380, 1340), pm), pm)

    def test_fallback_only_runs_when_composer_is_missing(self):
        pm = {"missing": []}
        with mock.patch.object(self.codex, "focus_composer") as focus:
            self.assertIs(self.codex.locate_with_composer_fallback(None, 1, None, pm), pm)
        focus.assert_not_called()


class CodexWindowPrepTests(unittest.TestCase):
    """最小化就先恢复；恢复不了退 7（不是带着哨兵坐标继续点）。"""

    @classmethod
    def setUpClass(cls):
        cls.codex = codex_module()

    def test_not_minimized_is_a_noop(self):
        with mock.patch.object(self.codex, "_is_minimized", return_value=False):
            self.assertIsNone(self.codex._prepare_window(1))

    def test_restored(self):
        with mock.patch.object(self.codex, "_is_minimized", return_value=True), \
                mock.patch.object(self.codex, "_restore_window", return_value={"ok": True}):
            self.assertIsNone(self.codex._prepare_window(1))

    def test_restore_failure_reports_window_minimized(self):
        with mock.patch.object(self.codex, "_is_minimized", return_value=True), \
                mock.patch.object(self.codex, "_restore_window",
                                  return_value={"ok": False, "reason": "still_iconic"}):
            got = self.codex._prepare_window(1)
        self.assertEqual(got["stage"], "window_minimized")
        self.assertEqual(got["reason"], "still_iconic")

    def run_main(self, prepare):
        """把 main 抠出来跑：窗口/控件/动作全打桩，只看退出码和输出。"""
        calls = []
        namespace = {
            "argparse": argparse, "json": json, "time": Clock(), "sys": sys,
            "find_window": lambda: 100,
            "rect_of": lambda hwnd: (0, 0, 1380, 1340),
            "Desk": lambda hwnd: SimpleNamespace(click=lambda *a: None),
            "activate": lambda hwnd: True,
            "locate": lambda *a, **k: {"missing": []},
            "WIN_SIZE": (1380, 1340), "WIN_POS": (-1440, -560), "MIN_W": 1050,
            "set_model": lambda *a: {"ok": True},
            "set_effort": lambda *a: {"ok": True},
            "ask": lambda *a: calls.append("ask") or {"ok": True},
            "_CURRENT_HWND": {"v": None},
            "_prepare_window": prepare,
            "place": lambda *a, **k: None,
            "MIN_WIN_W": 200, "MIN_WIN_H": 120,
        }
        fn = load_function("codex_use.py", "main", namespace)
        import contextlib
        import io

        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["codex_use.py", "--model", "default",
                                             "--force"]), \
                mock.patch("qi.runlog.record"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = fn()
        return code, out.getvalue(), calls

    def test_minimized_window_exits_seven(self):
        code, out, calls = self.run_main(
            lambda hwnd: {"ok": False, "stage": "window_minimized", "reason": "still_iconic"})
        self.assertEqual(code, 7)
        self.assertIn("window_minimized", out)
        self.assertEqual(calls, [], "恢复不了就不该继续去点任何东西")


# ================================================================ 模板备份
class TemplateBackups(unittest.TestCase):
    """第 4 条要求旧模板留 .bak-card07，别把旧图弄丢。"""

    NAMES = ("gem_chip_argon", "gem_chip_flash", "gem_chip_pro", "gem_chip_flash_lite",
             "gem_item_argon", "gem_item_flash", "gem_item_pro", "gem_item_flash_lite",
             "gem_mic", "gem_writer_tabs_dialog", "gem_writer_tabs_task")

    def test_every_replaced_template_has_a_backup(self):
        library = ROOT / "src" / "assets" / "library"
        missing = [n for n in self.NAMES if not (library / f"{n}.png.bak-card07").exists()]
        self.assertEqual(missing, [], "旧模板必须先备份成 .bak-card07 再覆盖")

    def test_backup_is_not_picked_up_by_the_library(self):
        from qi import Library

        names = Library().names()
        self.assertEqual([n for n in names if "bak" in n], [],
                         ".bak 不是 .png，资源库不该把它当成模板")


if __name__ == "__main__":
    unittest.main()
