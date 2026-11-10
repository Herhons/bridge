"""R11：端点拒绝这一轮的**结构化**判据（`src/qi/reject.py`）。

本文件只测纯函数，不碰 UIA、不跑主循环。
集成测（走 `conversation.ask`）与"防死值"测试见本文件后面的小节。

背景：`opencode` 额度耗尽时，Bridge 曾经把它判成 `answered`（reply 是半句话）
或 `timeout / no_reply`。原因是 `_TERMINAL_STAGES` 里那四个按原因命名的 stage
**从来没有生产者**。这次补的是生产者，但只生产"结构事实"，不猜原因。
"""
import ast
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, reject, ui  # noqa: E402

PROMPT = "请只原样回复这一行：BR9-test-20261107-7f2a"


def view(value="", sends=(), busy=False, copies=()):
    return SimpleNamespace(value=value, sends=tuple(sends), busy=busy,
                           copies=list(copies), root=None, url="", editor=None)


class ProfileTests(unittest.TestCase):
    def test_only_evidence_backed_endpoints_have_signals(self):
        self.assertTrue(reject.profile("opencode").echo_back)
        self.assertTrue(reject.profile("opencode").accepted_idle)
        self.assertTrue(reject.profile("codex").send_disabled)
        self.assertFalse(reject.profile("codex").accepted_idle)

    def test_unknown_endpoints_are_all_off(self):
        for name in ("chatgpt", "deepseek", "gemini", "grok", "dsh",
                     "", None, "  ", "OPENCODE-ish"):
            prof = reject.profile(name)
            self.assertEqual(prof, reject.DEFAULT, name)
            self.assertFalse(prof.echo_back or prof.send_disabled or prof.accepted_idle)

    def test_workbuddy_only_has_the_silent_switch(self):
        """B10：workbuddy 只开"收下后不出声"这一条。

        它的超时现场是"停止键一直可点"，所以 `submit_refused` / `send_unavailable`
        对它没有意义；能判的是"收下了却 90 秒不吐字"。
        """
        prof = reject.profile("workbuddy")
        self.assertFalse(prof.echo_back)
        self.assertFalse(prof.send_disabled)
        self.assertTrue(prof.accepted_idle)

    def test_case_and_space_insensitive(self):
        self.assertTrue(reject.profile("  OpenCode ").echo_back)


class ObserveTests(unittest.TestCase):
    def test_extracts_structure(self):
        f = reject.observe(view(PROMPT, sends=(False, False), copies=(1, 2, 3)),
                           PROMPT, prompt_here=True)
        self.assertTrue(f.echo)
        self.assertTrue(f.prompt_here)
        self.assertEqual((f.sends_total, f.sends_on), (2, 0))
        self.assertEqual(f.copies, 3)
        self.assertEqual(f.composer_chars, len(PROMPT))

    def test_missing_attributes_do_not_throw(self):
        """老假件没有 `sends` 字段，必须当成"不知道"而不是崩。"""
        f = reject.observe(SimpleNamespace(root=None), PROMPT)
        self.assertFalse(f.echo)
        self.assertEqual((f.sends_total, f.sends_on, f.copies), (0, 0, 0))

    def test_empty_value_is_not_echo(self):
        self.assertFalse(reject.observe(view(""), PROMPT).echo)

    def test_whitespace_only_difference_still_echoes(self):
        """归一之后相等就算回填——端把文字退回来时常会改缩进/换行。"""
        f = reject.observe(view("  " + PROMPT.replace(" ", "  ") + "\n"), PROMPT)
        self.assertTrue(f.echo)


class SendsBlockedTests(unittest.TestCase):
    def test_present_but_all_disabled(self):
        prof = reject.profile("codex")
        f = reject.Facts(sends_total=2, sends_on=0, busy=False)
        self.assertTrue(reject.sends_blocked(prof, f))

    def test_missing_send_button_is_not_blocked(self):
        """找不到发送键是脚本/UI 问题，**不能**说成端不接受输入。"""
        prof = reject.profile("codex")
        self.assertFalse(reject.sends_blocked(prof, reject.Facts(sends_total=0)))

    def test_busy_is_never_blocked(self):
        prof = reject.profile("codex")
        f = reject.Facts(sends_total=1, sends_on=0, busy=True)
        self.assertFalse(reject.sends_blocked(prof, f))

    def test_endpoint_without_switch_is_never_blocked(self):
        f = reject.Facts(sends_total=3, sends_on=0, busy=False)
        for name in ("deepseek", "gemini", "grok", "chatgpt", "dsh", "workbuddy"):
            self.assertFalse(reject.sends_blocked(reject.profile(name), f), name)


class SubmitRefusedTests(unittest.TestCase):
    def setUp(self):
        self.prof = reject.profile("opencode")
        self.state = reject.new_state()
        self.state["submit_at"] = 100.0

    def facts(self, **kw):
        base = dict(echo=True, prompt_here=False)
        base.update(kw)
        return reject.Facts(**base)

    def test_waits_out_the_controlled_retry_window(self):
        """10s 之内不判——那段时间受控重试还会再点一次发送。"""
        self.assertIsNone(reject.verdict(self.prof, self.facts(), self.state, 105.0))
        self.assertIsNone(reject.verdict(self.prof, self.facts(), self.state, 109.9))

    def test_two_consecutive_echoes_after_window(self):
        self.assertIsNone(reject.verdict(self.prof, self.facts(), self.state, 111.0))
        got = reject.verdict(self.prof, self.facts(), self.state, 111.4)
        self.assertIsNotNone(got)
        self.assertEqual(got["stage"], reject.SUBMIT_REFUSED)
        self.assertTrue(got["sent"])
        self.assertEqual(got["submission"], "unknown")

    def test_prompt_bubble_present_means_it_was_accepted(self):
        """气泡出现了 = 端收下了，绝不能判成拒收。"""
        for i in range(5):
            self.assertIsNone(
                reject.verdict(self.prof, self.facts(prompt_here=True), self.state,
                               120.0 + i))

    def test_echo_counter_resets_when_the_box_clears(self):
        reject.verdict(self.prof, self.facts(), self.state, 111.0)
        self.assertEqual(self.state["echo_reads"], 1)
        reject.verdict(self.prof, self.facts(echo=False), self.state, 111.4)
        self.assertEqual(self.state["echo_reads"], 0)

    def test_disabled_on_endpoints_without_the_switch(self):
        state = reject.new_state()
        state["submit_at"] = 100.0
        for name in ("deepseek", "gemini", "grok", "chatgpt", "dsh", "workbuddy"):
            self.assertIsNone(
                reject.verdict(reject.profile(name), self.facts(), state, 200.0), name)


class AcceptedSilentTests(unittest.TestCase):
    def setUp(self):
        self.prof = reject.profile("opencode")
        self.state = reject.new_state()
        self.state["accepted"] = True

    def test_reports_after_idle_window(self):
        quiet = reject.Facts(busy=False, copies=0)
        self.assertIsNone(reject.verdict(self.prof, quiet, self.state, 1000.0))
        got = reject.verdict(self.prof, quiet, self.state, 1000.0 + reject.IDLE_SECONDS)
        self.assertIsNotNone(got)
        self.assertEqual(got["stage"], reject.ACCEPTED_SILENT)
        self.assertEqual(got["submission"], "accepted")

    def test_any_generation_activity_vetoes_it(self):
        """见过一次"停止键可点"，就说明端在干活，永远不判无声。"""
        reject.verdict(self.prof, reject.Facts(busy=True), self.state, 1000.0)
        self.assertTrue(self.state["busy_seen"])
        quiet = reject.Facts(busy=False, copies=0)
        self.assertIsNone(reject.verdict(self.prof, quiet, self.state, 5000.0))

    def test_new_reply_vetoes_it(self):
        state = reject.new_state()
        state["accepted"] = True
        state["baseline_copies"] = 1
        self.assertIsNone(
            reject.verdict(self.prof, reject.Facts(copies=2), state, 2000.0))

    def test_not_accepted_yet_never_silent(self):
        state = reject.new_state()
        quiet = reject.Facts(busy=False, copies=0)
        self.assertIsNone(reject.verdict(self.prof, quiet, state, 9999.0))


class SubmitPathTests(unittest.TestCase):
    """`Conversation._submit` 必须把"找不到发送键"和"发送键不可用"分开。"""

    def _conv(self, site):
        conv = conversation.Conversation.__new__(conversation.Conversation)
        conv.site = site
        conv.screen = SimpleNamespace()
        conv._input_mark = None
        return conv

    def _view(self, *buttons):
        root = ui.Node("DocumentControl", "", SimpleNamespace(
            IsEnabled=True, IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=100)))
        root.children = list(buttons)
        for b in root.children:
            b.parent = root
        return SimpleNamespace(root=root, busy=False, value="", sends=())

    def _button(self, enabled):
        return ui.Node("ButtonControl", "发送", SimpleNamespace(
            IsEnabled=enabled, IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=0, top=0, right=10, bottom=10)))

    def _run(self, site, view):
        conv = self._conv(site)
        with unittest.mock.patch.object(conversation.guard, "user_moved_since",
                                        return_value=False), \
             unittest.mock.patch.object(conversation.time, "sleep"), \
             unittest.mock.patch.object(conversation.Conversation, "read",
                                        return_value=view):
            conv._submit(view)

    def test_present_but_disabled_raises_send_unavailable(self):
        view = self._view(self._button(False), self._button(False))
        with self.assertRaises(conversation.SendUnavailable) as caught:
            self._run("codex", view)
        self.assertEqual(caught.exception.present, 2)

    def test_no_send_button_keeps_the_original_error(self):
        """找不到发送键是脚本/UI 问题，**不能**报成"端不接受输入"。"""
        view = self._view()
        with self.assertRaises(RuntimeError) as caught:
            self._run("codex", view)
        self.assertNotIsInstance(caught.exception, conversation.SendUnavailable)
        self.assertIn("找到 0 个控件", str(caught.exception))

    def test_endpoint_without_switch_keeps_the_original_error(self):
        """六个无证据端即使看到禁用键也不改口。（deepseek 走截图颜色路径，这里用 gemini）"""
        view = self._view(self._button(False))
        with self.assertRaises(RuntimeError) as caught:
            self._run("gemini", view)
        self.assertNotIsInstance(caught.exception, conversation.SendUnavailable)

    def test_enabled_button_is_clicked_normally(self):
        view = self._view(self._button(True))
        conv = self._conv("codex")
        with unittest.mock.patch.object(conversation.guard, "user_moved_since",
                                        return_value=False), \
             unittest.mock.patch.object(conversation.ui, "click") as clicked:
            conv._submit(view)
        clicked.assert_called_once()


def stage_literals(path):
    """扫一个源文件：**函数体里**真正作为 stage 值出现过的字符串。

    只认两种发射形态：
      · dict 字面量里键为 "stage" 的值        {"stage": "xxx"} / {"stage": CONST}
      · 调用里的 stage= 关键字                foo(stage="xxx")

    模块级常量定义本身**不算**发射点（否则 `_TERMINAL_STAGES` 自己就能给自己作证）。
    模块级 `NAME = "literal"` 会被收集起来，用于解析 `"stage": NAME` 这种写法。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value

    def value_of(expr):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.Name):
            return consts.get(expr.id)
        return None

    found = set()
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in functions:
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "stage":
                        got = value_of(val)
                        if got:
                            found.add(got)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "stage":
                        got = value_of(kw.value)
                        if got:
                            found.add(got)
    return found


def all_emitted_stages():
    """全项目（src/qi + src/scripts）的发射点合集。"""
    emitted = set()
    for base in ("src/qi", "src/scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            emitted |= stage_literals(path)
    return emitted


class NoDeadStageTests(unittest.TestCase):
    """防死值：注册过的 stage 必须真的有人产生。

    `_TERMINAL_STAGES` 原来的 `{login_required, quota, limit, unavailable}` 全仓
    一个发射点都没有（三份备份快照逐字相同可证）。这条测试就是为那类情况写的：
    只要有人再往里塞一个"听起来很对"的名字，它当场变红。
    """

    def test_scanner_is_not_vacuous(self):
        """反例自检：扫描器本身必须真的能看见发射点，否则它是个摆设。"""
        emitted = all_emitted_stages()
        self.assertIn("answered", emitted)
        self.assertIn("timeout", emitted)
        self.assertIn(reject.SUBMIT_REFUSED, emitted)
        self.assertNotIn(reject.SUBMIT_REFUSED + "_nope", emitted)

    def test_every_registered_stage_has_an_emitter(self):
        emitted = all_emitted_stages()
        for name in sorted(conversation._TERMINAL_STAGES | conversation._RETRYABLE_STAGES):
            self.assertIn(name, emitted,
                          f"{name} 注册在 _TERMINAL_STAGES/_RETRYABLE_STAGES，"
                          f"但全项目没有任何地方产生它")

    def test_dead_reason_names_are_gone(self):
        """按原因命名的那四个死值必须已经删掉 —— 留着会诱导下一个人继续往里加。"""
        for dead in ("login_required", "quota", "limit", "unavailable"):
            self.assertNotIn(dead, conversation._TERMINAL_STAGES)
            self.assertNotIn(dead, conversation._RETRYABLE_STAGES)

    def test_every_reject_stage_is_registered(self):
        from qi import exit_codes
        for name in reject.ALL_STAGES:
            self.assertIn(name, exit_codes.STAGE_CODES, name)
            self.assertEqual(exit_codes.code_for(name), exit_codes.FAIL, name)

    def test_every_reject_stage_keeps_a_failure_snapshot(self):
        """这三条只能靠看现场判断，不留截图等于没证据。"""
        from qi import failure
        for name in reject.ALL_STAGES:
            self.assertTrue(failure.wanted(name), name)


class WireInTests(unittest.TestCase):
    """端到端：`conversation.ask` 真的会在主循环里把它们判出来。"""

    def _conv(self, site, value, *, prompt_here=False):
        from qi import ui

        class Conv:
            settle = 0
            screen = None

            def __init__(self):
                self.site = site
                self.submits = 0
                self.writes = 0
                self._n = 0

            def empty(self, v):
                return not (v.value or "").strip()

            def read(self, attempts=1, delay=0.2):
                self._n += 1
                # 第一轮是提交前的预检：那时框是空的。之后才是"被退回来的样子"。
                now_value = "" if self._n == 1 else value
                root = ui.Node("DocumentControl", "", SimpleNamespace(
                    IsEnabled=True, IsOffscreen=False,
                    BoundingRectangle=SimpleNamespace(left=0, top=0, right=9, bottom=9)))
                kids = []
                if prompt_here:
                    kids.append(ui.Node("TextControl", PROMPT, SimpleNamespace(
                        IsEnabled=True, IsOffscreen=False,
                        BoundingRectangle=SimpleNamespace(left=0, top=0, right=9, bottom=9))))
                root.children = kids
                return SimpleNamespace(root=root, value=now_value, url="", busy=False,
                                       copies=[], editor=None, sends=())

            def write(self, before, prompt):
                self.writes += 1

            def submit(self, v):
                self.submits += 1

            def clear(self, v):
                pass

            def copy(self, n):
                return ""

        return Conv()

    def test_submit_refused_is_reported_end_to_end(self):
        conv = self._conv("opencode", PROMPT)
        with unittest.mock.patch.object(reject, "SUBMIT_SETTLE", 0.0):
            got = conversation.ask(conv, PROMPT, timeout=3.0)
        self.assertEqual(got["stage"], reject.SUBMIT_REFUSED)
        self.assertTrue(got["sent"])
        self.assertEqual(conv.submits, 1, "拒收之后不该再点第二次")

    def test_same_snapshot_on_an_endpoint_without_the_switch_stays_grey(self):
        """同样的现场，六端无证据的端必须仍然走老路（timeout），不许跟着改口。"""
        conv = self._conv("gemini", PROMPT)
        with unittest.mock.patch.object(reject, "SUBMIT_SETTLE", 0.0):
            got = conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(got["stage"], "timeout")


def _node(kind, name, children=()):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=9, bottom=9)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


class ReplyTextLenTests(unittest.TestCase):
    """`reply_text_len` 只回答"变了没有"，不参与任何内容判定。"""

    def test_no_candidates_is_zero(self):
        self.assertEqual(conversation.reply_text_len(SimpleNamespace(), []), 0)

    def test_finds_the_bubble_above_the_copy_button(self):
        copy = _node("ButtonControl", "复制")
        bubble = _node("GroupControl", "", [_node("TextControl", "回复正文四五六"), copy])
        self.assertEqual(conversation.reply_text_len(None, [copy]), len("回复正文四五六"))

    def test_growing_text_changes_the_number(self):
        copy = _node("ButtonControl", "复制")
        short = _node("GroupControl", "", [_node("TextControl", "半句"), copy])
        first = conversation.reply_text_len(None, [copy])
        short.children[0].name = "半句话说完了"
        second = conversation.reply_text_len(None, [copy])
        self.assertNotEqual(first, second, "文本变长时长度必须跟着变，否则签名认不出来")
        bubble = short  # noqa: F841 - 仅为可读性保留


class SettleGuardTests(unittest.TestCase):
    """有回复候选但文本一直变时，SETTLE_MAX 兜底：交卷并打标记，不因噪声超时。"""

    def _conv(self, settle):
        class Conv:
            screen = None

            def __init__(self):
                self.site = "chatgpt"
                self.settle = settle
                self._n = 0
                self.submits = 0

            def empty(self, v):
                return not (v.value or "").strip()

            def read(self, attempts=1, delay=0.2):
                self._n += 1
                copy = _node("ButtonControl", "复制")
                parent = _node("GroupControl", "", [
                    _node("TextControl", "回复正文" * max(1, self._n)), copy])
                btn = _node("ButtonControl", "发送")
                kids = [btn, parent]
                if self._n >= 3:      # 提交之后，提问气泡才出现在树上
                    kids.insert(0, _node("TextControl", PROMPT))
                root = _node("DocumentControl", "", kids)
                # 第 1 轮预检是空框；第 2 轮是"写完读回"必须等于提问；
                # 之后才是提交后的样子。
                value = PROMPT if self._n == 2 else ""
                return SimpleNamespace(root=root, value=value, url="", busy=False,
                                       copies=[copy] if self._n > 2 else [],
                                       editor=None, sends=(True,))

            def write(self, before, prompt):
                pass

            def submit(self, v):
                self.submits += 1

            def clear(self, v):
                pass

            def copy(self, n):
                return "回复正文"

        return Conv()

    def test_normal_case_is_marked_text_stable(self):
        conv = self._conv(settle=0.0)
        got = conversation.ask(conv, PROMPT, timeout=3.0)
        self.assertEqual(got["stage"], "answered")
        self.assertEqual(got.get("settle_basis"), "text_stable")

    def test_capped_when_text_keeps_changing(self):
        conv = self._conv(settle=10.0)
        with unittest.mock.patch.object(conversation, "SETTLE_MAX", 0.0):
            got = conversation.ask(conv, PROMPT, timeout=3.0)
        self.assertEqual(got["stage"], "answered",
                         "噪声再大也要交卷，不能因超时丢答案")
        self.assertEqual(got.get("settle_basis"), "capped")
        self.assertIn("text_never_settled", got.get("warnings", []))


if __name__ == "__main__":
    unittest.main()
