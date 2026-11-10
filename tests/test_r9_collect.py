"""R9：WorkBuddy 采集只交**本轮答案**；Gemini 发送前旧答案必须先证明同一会话。

两个现场缺陷（Hy4 S8/S10）：

1. WorkBuddy：唯一复制键挂在**提问气泡**上，采集从它往上爬到整页容器，
   把「上一轮完整报告 + 模型切换标记 + 本轮提问 + 本轮 nonce」整段当答案；
   旧实现只按"删掉提问那一行"处理，于是旧报告被当成新答案交回去。
   本文件钉住：整页容器必须按**最后一条本提问**截断，只留提问之后的文字；
   模型切换标记要剔除；读不到提问锚点又拿不到严格会话身份时必须拒，
   绝不回落到整页文本。剪贴板陈旧 / UIA 回退两条路都要只交 nonce。

2. Gemini：`_already_answered` 只看文本，发送前会认领**同一文档另一个会话**
   里的同文旧问答。本文件钉住：没有本事务记录、记录没有会话 id、当前页面读不到
   会话 id、或会话 id 不一致时，一律 `session_unproven` 拒发：零写、零提交、
   零清草稿、记录字节不变；只有记录与当前 URL 会话 id 一致才允许只读交卷。

全离线：假控件树 + 假 Conversation，不碰真窗口、不截图、不发送。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import gemini, submissions, tier_guard as tg, ui, workbuddy as wb  # noqa: E402

NONCE = "BRIDGE-R9-COLLECT-20261107-0705-H4"
PROMPT = f"Current-version unique live-loop test. Reply ONLY exactly: {NONCE}"
PRIOR = "R8 BUILDER REPORT — previous answer\n" + ("上一轮完整报告正文，共很多段落。" * 30)
MARK = "模型已切换到 DeepSeek-V4.1-Flash"
OLD_FOREIGN = "GEMINI-OLD-FOREIGN-ANSWER"
URL = "https://gemini.google.com/app/session-r9-1"
URL_OTHER = "https://gemini.google.com/app/session-r9-2"
URL_DESKTOP = "oc://renderer/index.html"
SITE = "gemini"


# ---------------------------------------------------------------- 假件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=(), height=20, top=0, rid=None):
    control = SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                              BoundingRectangle=box(100, height, 0, top))
    if rid is not None:
        control.GetRuntimeId = lambda: [rid]
    item = ui.Node(kind, name, control, children=list(children))
    for child in item.children:
        child.parent = item
    return item


def text(value, top=0):
    return node("TextControl", value, height=20, top=top)


def button(name, top=0):
    return node("ButtonControl", name, height=24, top=top)


def toolbar(top=0):
    return node("ToolBarControl", "", [button("复制", top=top)], height=30, top=top)


def wb_tree(*, prompt_visible=True, reply_copy=False):
    """真机形状：复制键只在提问气泡上；回复气泡只有正文。"""
    children = [text(PRIOR, top=0), text(MARK, top=200)]
    prompt_bits = []
    if prompt_visible:
        prompt_bits.append(text(PROMPT, top=260))
    prompt_bits.append(toolbar(top=300))
    children.append(node("GroupControl", "", prompt_bits, height=70, top=260))
    reply_bits = [text(NONCE, top=360)]
    if reply_copy:
        reply_bits.append(toolbar(top=420))
    children.append(node("GroupControl", "", reply_bits, height=90, top=360))
    return node("DocumentControl", "", children, height=600)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WbConv:
    """按顺序吐视图；最后一个重复（页面稳定）。"""

    def __init__(self, roots, copy_value=NONCE, copy_error=None, url=URL_DESKTOP):
        self.roots = list(roots)
        self.i = 0
        self.prompt = PROMPT
        self.copy_value = copy_value
        self.copy_error = copy_error
        self.read_via = None
        self.copy_note = None
        self.copy_calls = []
        self.site = "workbuddy"
        self.screen = None
        self.url = url

    def read(self, attempts=4, delay=0.4):
        item = self.roots[min(self.i, len(self.roots) - 1)]
        self.i += 1
        return SimpleNamespace(root=item, busy=False, value="", url=self.url,
                               editor=None, copies=[], site=self.site)

    def copy(self, node_):
        self.copy_calls.append(node_)
        if self.copy_error is not None:
            raise self.copy_error
        self.read_via = "clipboard"
        return self.copy_value


def assert_clean(test, reply):
    test.assertEqual(reply, NONCE, f"必须只交本轮 nonce，实际={reply!r}")
    test.assertNotIn("R8 BUILDER REPORT", reply)
    test.assertNotIn("上一轮完整报告", reply)
    test.assertNotIn(PROMPT, reply)
    test.assertNotIn("模型已切换", reply)


# ================================================================ 1. WorkBuddy 采集
class WorkBuddyCollectsOnlyCurrentAnswer(unittest.TestCase):
    def test_last_reply_returns_only_nonce(self):
        root = wb_tree()
        nodes = list(root.walk())
        at = wb.prompt_at(nodes, PROMPT)
        self.assertIsNotNone(at, "提问应当被认领")
        got, _node = wb.last_reply(nodes, PROMPT, at)
        self.assertIsNotNone(got)
        assert_clean(self, got)

    def test_wait_reply_returns_only_nonce(self):
        conv = WbConv([wb_tree()])
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(conv, PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        assert_clean(self, got["reply"])

    def test_final_read_returns_only_nonce(self):
        conv = WbConv([wb_tree()])
        got = wb.final_read(conv, PROMPT,
                            {"ok": False, "submission": "unknown", "sent": None})
        self.assertIsNotNone(got)
        assert_clean(self, got["reply"])

    def test_stale_clipboard_does_not_replace_uia_text(self):
        conv = WbConv([wb_tree()], copy_value=PRIOR)
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(conv, PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        assert_clean(self, got["reply"])
        self.assertEqual(got["read_via"], "uia", "剪贴板是旧报告，必须改用已归属的 UIA 正文")

    def test_clipboard_failure_uses_bounded_uia_text(self):
        conv = WbConv([wb_tree()], copy_error=RuntimeError("剪贴板没更新"))
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(conv, PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        assert_clean(self, got["reply"])
        self.assertEqual(got["read_via"], "uia")

    def test_virtualized_prompt_without_session_id_refuses(self):
        """提问不在树里、桌面地址没有会话 id：严格身份不成立，必须拒。"""
        root = wb_tree(prompt_visible=False, reply_copy=True)
        root.control.GetRuntimeId = lambda: [77]
        before = node("DocumentControl", "", [], height=100)
        before.control.GetRuntimeId = lambda: [77]
        baseline = wb.reply_baseline(
            SimpleNamespace(root=before, url=URL_DESKTOP), PROMPT)
        baseline["accepted_scope"] = baseline["scope"]
        view = SimpleNamespace(root=root, busy=False, value="", url=URL_DESKTOP,
                               copies=[], editor=None)
        text_out, _node, why = wb.attributed_reply(
            view, PROMPT, baseline, baseline.get("scope"))
        self.assertIsNone(text_out, f"身份未证明不许交整页文本（{why}）")
        conv = WbConv([root], url=URL_DESKTOP)
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(conv, PROMPT, 1.0, baseline=baseline)
        self.assertFalse(got["ok"], got)
        self.assertFalse(conv.copy_calls, "身份未证明不许点复制键")

    def test_virtualized_prompt_with_proved_session_still_positive(self):
        """证明得了同一会话时，未锚定路径仍可只读交卷（正例不被误杀）。"""
        url = "https://gemini.google.com/app/session-r9-9"
        root = wb_tree(prompt_visible=False, reply_copy=True)
        root.control.GetRuntimeId = lambda: [88]
        before = node("DocumentControl", "", [], height=100)
        before.control.GetRuntimeId = lambda: [88]
        baseline = wb.reply_baseline(SimpleNamespace(root=before, url=url), PROMPT)
        self.assertEqual(baseline.get("session_id"), "session-r9-9")
        baseline["accepted_scope"] = baseline["scope"]
        view = SimpleNamespace(root=root, busy=False, value="", url=url,
                               copies=[], editor=None)
        text_out, _node, why = wb.attributed_reply(
            view, PROMPT, baseline, baseline.get("scope"))
        self.assertEqual(text_out, NONCE, why)


# ================================================================ 2. Gemini 发送前
def gem_tree(prompt=PROMPT, reply=OLD_FOREIGN, draft=False):
    children = [text("你说"), text(prompt), text("Gemini 说"), text(reply)]
    if draft:
        children.append(button("发送"))
    return node("DocumentControl", "", children, height=400)


def gem_view(url=URL, *, draft=False, prompt=PROMPT):
    return SimpleNamespace(root=gem_tree(prompt=prompt, draft=draft),
                           value=PROMPT if draft else "", url=url, busy=False)


class GemConv:
    def __init__(self, views):
        self.views = list(views)
        self.i = 0
        self.writes = []
        self.clears = 0

    def read(self, attempts=4, delay=0.4):
        item = self.views[min(self.i, len(self.views) - 1)]
        self.i += 1
        return item

    def write(self, view, prompt):
        self.writes.append(prompt)
        view.value = prompt

    def clear(self, view):
        self.clears += 1
        view.value = ""

    def paste(self, view, prompt):
        view.value = prompt


def seed(tmp, *, sid, state="accepted", url=URL):
    scope = submissions.scope_from(site=SITE, url=url, doc_rid=[5],
                                   session_id=("" if sid is None else sid))
    with mock.patch.object(tg, "ROOT", Path(tmp)):
        submissions.reserve(SITE, PROMPT, scope=scope)
        submissions.finish(SITE, PROMPT, {"submission": state}, scope=scope)
    return submissions.path_for(SITE, PROMPT, root=Path(tmp))


def gem_run(tmp, view, recheck=None):
    conv = GemConv([view, recheck or SimpleNamespace(
        root=node("DocumentControl", "", [], height=100), value="", url=view.url, busy=False)])
    site = SimpleNamespace(name=SITE)
    with mock.patch.object(tg, "ROOT", Path(tmp)), \
            mock.patch.object(gemini, "Conversation", return_value=conv), \
            mock.patch.object(gemini, "write_prompt") as wp, \
            mock.patch.object(gemini, "_submit") as submit:
        got = gemini._send_and_wait(mock.Mock(), site, PROMPT, 1.0, purpose="test")
    return got, conv, wp, submit


class GeminiPreSendRequiresSessionIdentity(unittest.TestCase):
    def prepare(self):
        # NO DELETION：保留全部测试产物（不用会自动清目录的 TemporaryDirectory）
        return tempfile.mkdtemp(prefix="r9_gemini_")

    def assert_refused(self, got, conv, wp, submit, view, journal, reason, state):
        self.assertFalse(got["ok"], got)
        self.assertEqual(got["stage"], "session_unproven", got)
        self.assertEqual(got["reason"], reason, got)
        self.assertFalse(got["sent"], got)
        self.assertEqual(got["submission"], state, got)
        self.assertEqual(conv.writes, [], "失败关闭不许写输入框")
        self.assertEqual(conv.clears, 0, "失败关闭不许清草稿")
        wp.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(view.value, PROMPT, "未发出的草稿必须原样保留")
        if journal is not None:
            self.assertTrue(journal.exists())
            self.assertEqual(journal.read_bytes(), self.before, "提交记录不许被改")

    def test_missing_session_id_refuses_and_preserves_state(self):
        tmp = self.prepare()
        journal = seed(tmp, sid=None)
        self.before = journal.read_bytes()
        view = gem_view(draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assert_refused(got, conv, wp, submit, view, journal,
                            "session_id_missing", "accepted")

    def test_mismatched_session_id_refuses(self):
        tmp = self.prepare()
        journal = seed(tmp, sid="session-r9-1", url=URL)
        self.before = journal.read_bytes()
        view = gem_view(url=URL_OTHER, draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assert_refused(got, conv, wp, submit, view, journal,
                            "session_id_mismatch", "accepted")

    def test_unreadable_session_id_refuses(self):
        tmp = self.prepare()
        journal = seed(tmp, sid="session-r9-1", url=URL)
        self.before = journal.read_bytes()
        view = gem_view(url=URL_DESKTOP, draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assert_refused(got, conv, wp, submit, view, journal,
                            "session_id_unreadable", "accepted")

    def test_no_journal_refuses_foreign_identical_answer(self):
        tmp = self.prepare()
        self.before = None
        view = gem_view(url=URL_OTHER, draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assert_refused(got, conv, wp, submit, view, None,
                            "no_journal", "not_sent")

    def test_unknown_state_is_preserved(self):
        tmp = self.prepare()
        journal = seed(tmp, sid=None, state="unknown")
        self.before = journal.read_bytes()
        view = gem_view(draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assert_refused(got, conv, wp, submit, view, journal,
                            "session_id_missing", "unknown")

    def test_proved_same_session_is_already_answered(self):
        tmp = self.prepare()
        seed(tmp, sid="session-r9-1")
        view = gem_view(draft=True)
        got, conv, wp, submit = gem_run(tmp, view)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["stage"], "already_answered", got)
        self.assertEqual(got["reply"], OLD_FOREIGN)
        self.assertFalse(got["sent"])
        self.assertEqual(conv.writes, [])
        self.assertEqual(conv.clears, 1, "合法已答 + 回填草稿仍按 CARD-09f 清一次")
        self.assertEqual(view.value, "")
        wp.assert_not_called()
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
