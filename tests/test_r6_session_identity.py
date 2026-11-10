"""R6：**同一 document / 同一 URL** 的 SPA 会话切换必须 fail-closed。

R5 承认的残留（BUILDER_RESULT 9.5）：opencode 的会话切换不换 document runtime id、
也不换 URL（`oc://renderer/index.html`），`_collection_identity` 看不出来；
如果新会话里恰好只剩一条回复，`_single_new_reply` 兜底会把它当成本轮答案。

这里钉住"应用自己的会话身份"（会话名 + 标签条证据）：
  1. 同一文档同一 URL、不同会话、一条外来回复 -> 拒（不复制、不改 accepted、不重发）；
  2. UI 根本给不出会话身份（没有标题/标签条） -> 拒（不许猜）；
  3. 同一会话、提问被虚拟化掉、只多一条回复 -> 照收（合法路径不能丢）；
  4. 新会话被应用自动命名（占位名 -> 真名）且提问还在树上 -> 允许一次，随后照收；
  5. 标题不唯一（两个同名标签） -> 不交卷；
  6. 恢复侧：记录里的会话名和当前会话名不一致 -> 不认已答，记录一个字节不动。
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

from test_card07 import bubble                     # noqa: E402
from test_conversation import FakeConversation, node, view  # noqa: E402
from qi import submissions, tier_guard as tg       # noqa: E402
from qi import workbuddy as wb                     # noqa: E402
from qi.conversation import View, ask              # noqa: E402

PROMPT = "first line\nsecond line"
ANSWER = "new answer"
DOC_RID = 4242
URL = "oc://renderer/index.html"
APP_SUFFIX = " · OpenCode"


def titled(*, title, tabs, prompt=False, reply=False, value="", busy=False,
           rid=DOC_RID, url=URL, header=True):
    """一张 opencode 页面快照：会话名（文档标题）+ 顶部标签条 + 可选提问/回复。

    document rid 和 URL 在所有用例里都**保持不变**——这正是 R6 要处理的形态。
    """
    editor = node("EditControl", "", 2, box=(80, 300, 900, 360))
    children = []
    for i, tab in enumerate(tabs):
        children.append(node("TextControl", tab, 100 + i,
                             box=(80 + i * 160, 8, 220 + i * 160, 36)))
    if header:
        children.append(node("TextControl", title, 60, box=(60, 55, 520, 83)))
    if prompt:
        children.append(node("TextControl", PROMPT, 20, box=(80, 420, 900, 448)))
    copies = []
    if reply:
        body = node("TextControl", ANSWER, 30, box=(80, 520, 900, 548))
        copy_btn = node("ButtonControl", "Copy response", 31, box=(80, 560, 104, 584))
        children.append(node("GroupControl", "", 32, body, copy_btn,
                             box=(80, 520, 900, 590)))
        copies.append(copy_btn)
    root = node("DocumentControl", f"{title}{APP_SUFFIX}", rid,
                editor, *children, box=(0, 0, 1280, 800))
    return View(url, root, editor, value, busy, copies)


class SpaCollection(unittest.TestCase):
    def run_turn(self, snapshots, timeout=8):
        clock = [0.0]
        conv = FakeConversation(snapshots)
        with mock.patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("qi.conversation.time.sleep",
                           side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout)
        return result, conv

    def turn(self, first, then, timeout=8):
        """first 是"发送/接受"三帧，then 是接受之后的帧序列。"""
        return self.run_turn(list(first) + list(then), timeout)

    # ---------------------------------------------------------------- 拒（R6 主体）
    def test_other_session_in_the_same_document_is_refused(self):
        a_tabs = ["会话", "Bridge A"]
        b_tabs = ["会话", "Bridge A", "Bridge B"]
        result, conv = self.turn(
            [titled(title="Bridge A", tabs=a_tabs),
             titled(title="Bridge A", tabs=a_tabs, value=PROMPT),
             titled(title="Bridge A", tabs=a_tabs, prompt=True)],
            [titled(title="Bridge B", tabs=b_tabs)] * 5
            + [titled(title="Bridge B", tabs=b_tabs, reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed", result)
        self.assertEqual(result["reason"], "session_changed", result)
        self.assertEqual(result["submission"], "accepted", "发出去的事实不许改口")
        self.assertEqual(conv.copied, [], "另一个会话的回复一个字都不许复制")
        self.assertEqual(conv.submits, 1, "绝不自动重发")

    def test_missing_session_identity_never_uses_the_fallback(self):
        """UI 不给会话名/标签条：不许拿"只多一条回复"当答案。"""
        result, conv = self.turn(
            [view(), view(value=PROMPT), view(prompt=True)],
            [view()] * 4 + [view(reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed", result)
        self.assertEqual(result["session"], "unproven", result)
        self.assertIn("没有与标题无关的会话 id", result.get("hint") or "")
        self.assertEqual(conv.copied, [])

    def test_two_tabs_with_the_same_title_are_not_provable(self):
        """同名标签（例如两个都叫"会话"）分不出是哪一个 -> 拒。"""
        result, conv = self.turn(
            [titled(title="会话", tabs=["会话", "会话"], value=""),
             titled(title="会话", tabs=["会话", "会话"], value=PROMPT),
             titled(title="会话", tabs=["会话", "会话"], prompt=True)],
            [titled(title="会话", tabs=["会话", "会话"])] * 4
            + [titled(title="会话", tabs=["会话", "会话"], reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["session"], "unproven", result)
        self.assertEqual(conv.copied, [])

    # ---------------------------------------------------------------- 合法路径（正例）
    def test_same_title_alone_never_collects_after_the_prompt_vanishes(self):
        """R7：标题一致**不是**证明。没有会话 id 就不许收（R6 的旧正例被收紧）。"""
        tabs = ["会话", "Bridge A"]
        keep = lambda **kw: titled(title="Bridge A", tabs=tabs, **kw)   # noqa: E731
        result, conv = self.turn(
            [keep(), keep(value=PROMPT), keep(prompt=True)],
            [keep()] * 4 + [keep(reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "prompt_gone", result)
        self.assertEqual(result["session"], "unproven", result)
        self.assertEqual(conv.copied, [], "标题一致也不许复制回复")

    def test_initial_title_assignment_is_not_a_switch_but_is_not_proof_either(self):
        """R7：自动命名不算"换会话"，但也不构成证明 —— 没有会话 id 照样不收。"""
        before = dict(title="会话", tabs=["会话", "Other task"])
        after = dict(title="BR9-new", tabs=["BR9-new", "Other task"])
        result, conv = self.turn(
            [titled(**before, value=""), titled(**before, value=PROMPT),
             titled(**before, prompt=True), titled(**after, prompt=True)],
            [titled(**after)] * 4 + [titled(**after, reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "prompt_gone", result)
        self.assertEqual(result["session"], "unproven", result)
        self.assertEqual(conv.copied, [])


class SpaRecovery(unittest.TestCase):
    """恢复侧：记录里的会话名是 A，屏幕上却是 B（同文档同 URL）。"""

    HWND = 4242
    RID = 900

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(tg, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, title, tabs):
        doc, copy_btn = bubble(ANSWER, prompt_line=PROMPT)
        doc.name = f"{title}{APP_SUFFIX}"
        doc.control.GetRuntimeId = lambda: [self.RID]      # 同一个 document
        for i, tab in enumerate(tabs):
            doc.children.append(node("TextControl", tab, 200 + i,
                                     box=(80 + i * 160, 8, 220 + i * 160, 36)))
        doc.children.append(node("TextControl", title, 210, box=(60, 55, 520, 83)))
        return SimpleNamespace(root=doc, value="", busy=False, copies=[copy_btn],
                               url="app://wb", editor=None)

    def journal(self, title):
        submissions.reserve("workbuddy", PROMPT, scope={
            "site": "workbuddy", "url": "app://wb", "doc_rid": [self.RID],
            "hwnd": self.HWND, "session": {"title": title, "proven": True}})
        submissions.finish("workbuddy", PROMPT, {"submission": "accepted"})

    def recover(self, view):
        conv = mock.Mock()
        conv.read = mock.Mock(return_value=view)
        conv.empty = mock.Mock(return_value=True)
        conv.clear = mock.Mock()
        with mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
                mock.patch.object(wb, "ReplyConversation", return_value=conv):
            return wb._already_replied(PROMPT)

    def test_same_document_other_session_is_not_answered(self):
        self.journal("Bridge A")
        got = self.recover(self.page("Bridge B", ["会话", "Bridge A", "Bridge B"]))
        self.assertIsNone(got, "另一个会话里的同文问答不是本轮答案")
        # R7：记录里没有会话 id -> 先按 session_id_missing 拒（比以前更早、更严）。
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")

    def test_same_name_without_session_id_is_refused(self):
        """R7：同名也不算证明；没有会话 id 的恢复一律拒（正例见 test_r7）。"""
        self.journal("Bridge A")
        got = self.recover(self.page("Bridge A", ["会话", "Bridge A"]))
        self.assertIsNone(got, "同名同文档也不能当已答")
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")


if __name__ == "__main__":
    unittest.main()
