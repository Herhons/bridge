"""R5 / CE1-CE2：恢复必须证明"同一个事务 + 同一个文档/标签"，否则 fail-closed。

独立复核（evidence/glm_independent_20261107_0436，CE1/CE2）证明：只靠
"仓库里有记录 + 页面上一对同样的提问/回答"会把**别的文档**里的旧回答交回去。
这里把两个反例固化成常驻回归，并钉住三件事：

  · 身份对不上 → 不返回答案、不改提交记录、不清草稿、不新开对话、不发送；
  · 身份对得上（同一事务同一文档）→ 仍然按 already_answered 交回；
  · 老记录（没有身份）→ 一律拒（没法证明归属，就不能采信当前页面）。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "src" / "scripts"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from test_card07 import bubble, flat, node, text            # noqa: E402
from qi import gemini, submissions, tier_guard as tg        # noqa: E402
from qi import workbuddy as wb                              # noqa: E402

PROMPT = "只回复 07-RECOVERY-OK"
ANSWER = "07-RECOVERY-OK"
WEB_PROMPT = "只回复 09F-OTHER-TAB"
WEB_ANSWER = "09F-FOREIGN-TAB-OLD-REPLY"


# ================================================================ 纯函数：比对矩阵
class ReconcileMatrix(unittest.TestCase):
    def base(self, **over):
        # R7：恢复还要求"与标题无关的会话 id"；没有它的记录一律拒（见下面的用例）。
        scope = {"site": "workbuddy", "url": "app://wb/x", "doc_rid": [7],
                 "hwnd": 4242, "pid": 100, "process_start_time": 1.5,
                 "session_id": "chat-1"}
        scope.update(over)
        return scope

    def test_happy_path(self):
        ok, why = submissions.reconcile(self.base(), self.base())
        self.assertTrue(ok, why)
        self.assertEqual(why, "matched")

    def test_missing_or_blank_is_fail_closed(self):
        for value in (None, {}, {"site": "workbuddy"}):
            with self.subTest(value=value):
                self.assertEqual(submissions.reconcile(value, self.base()),
                                 (False, "scope_missing"))
        self.assertEqual(submissions.reconcile(self.base(), None),
                         (False, "scope_unreadable"))

    def test_window_identity_must_match(self):
        cases = {
            "hwnd_missing": self.base(hwnd=None),
            "hwnd_mismatch": self.base(hwnd=1),
            "pid_missing": self.base(pid=None),
            "pid_mismatch": self.base(pid=101),
            "process_start_time_missing": self.base(process_start_time=None),
            "process_start_time_mismatch": self.base(process_start_time=2.5),
            "doc_rid_missing": self.base(doc_rid=None),
            "doc_rid_mismatch": self.base(doc_rid=[8]),
            "url_mismatch": self.base(url="app://wb/y"),
            "site_mismatch": self.base(site="codex"),
        }
        for reason, current in cases.items():
            with self.subTest(reason=reason):
                ok, why = submissions.reconcile(self.base(), current)
                self.assertFalse(ok)
                self.assertEqual(why, reason)

    def test_pid_and_start_time_may_be_unreadable_on_both_sides(self):
        """有些环境读不到 pid/启动时间：两边都没有时靠 hwnd+doc_rid 绑。"""
        blind = self.base(pid=None, process_start_time=None)
        self.assertEqual(submissions.reconcile(blind, blind), (True, "matched"))

    def test_one_sided_readability_is_not_a_match(self):
        old = self.base(pid=None, process_start_time=None)
        self.assertEqual(submissions.reconcile(old, self.base()),
                         (False, "pid_missing"))

    def test_url_is_compared_only_when_both_readable(self):
        old = self.base(url=None)
        self.assertEqual(submissions.reconcile(old, self.base()), (True, "matched"))

    def test_session_id_is_required_and_must_match(self):
        """R7：文档/窗口都对，也不能只靠它们"兼容成功"。"""
        cases = {
            "session_id_unreadable": self.base(session_id=None),
            "session_id_mismatch": self.base(session_id="chat-2"),
        }
        for reason, current in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(submissions.reconcile(self.base(), current),
                                 (False, reason))
        # 记录里没有 id（老记录/桌面端）：哪怕现在读得到，也不许当"已答"
        self.assertEqual(submissions.reconcile(self.base(session_id=None), self.base()),
                         (False, "session_id_missing"))


class JournalScopePersistence(unittest.TestCase):
    """发送前的 scope 与"接受那一页"的 scope_after：后者只写第一次，恢复优先用它。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(tg, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reserve_writes_scope_and_finish_does_not_invent_one(self):
        submissions.reserve("gemini", WEB_PROMPT, scope={
            "site": "gemini", "url": "https://gemini.google.com/app",
            "doc_rid": [1], "hwnd": 4242})
        value = submissions.read("gemini", WEB_PROMPT, root=self.root)
        self.assertEqual(value["scope"]["url"], "https://gemini.google.com/app")
        submissions.finish("gemini", WEB_PROMPT, {"submission": "accepted"})
        value = submissions.read("gemini", WEB_PROMPT, root=self.root)
        self.assertNotIn("scope_after", value)
        self.assertEqual(submissions.recovery_scope(value), value["scope"])

    def test_scope_after_is_written_once_and_wins(self):
        submissions.reserve("gemini", WEB_PROMPT, scope={
            "site": "gemini", "url": "https://gemini.google.com/app",
            "doc_rid": [1], "hwnd": 4242})
        after = {"site": "gemini", "url": "https://gemini.google.com/app/abc",
                 "doc_rid": [2], "hwnd": 4242}
        submissions.finish("gemini", WEB_PROMPT, {"submission": "accepted"}, scope=after)
        stale = {"site": "gemini", "url": "https://gemini.google.com/app/zzz",
                 "doc_rid": [3], "hwnd": 4242}
        submissions.finish("gemini", WEB_PROMPT, {"submission": "accepted"}, scope=stale)
        value = submissions.read("gemini", WEB_PROMPT, root=self.root)
        self.assertEqual(value["scope_after"]["url"], "https://gemini.google.com/app/abc")
        self.assertEqual(submissions.recovery_scope(value)["url"],
                         "https://gemini.google.com/app/abc")


# ================================================================ CE1：workbuddy
class WorkBuddyRecoveryIdentity(unittest.TestCase):
    HWND = 4242
    RID = 700

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(tg, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scope(self, **over):
        base = dict(site="workbuddy", url="app://wb", doc_rid=[self.RID], hwnd=self.HWND)
        base.update(over)
        return submissions.scope_from(**base)

    def seed(self, *, scope="default", state="accepted"):
        submissions.reserve("workbuddy", PROMPT,
                            scope=self.scope() if scope == "default" else scope)
        submissions.finish("workbuddy", PROMPT, {"submission": state})
        path = submissions.path_for("workbuddy", PROMPT, root=self.root)
        return path, path.read_bytes()

    def page(self, *, rid=None, answer=ANSWER):
        root, _copy = bubble(answer, prompt_line=PROMPT)
        root.control.GetRuntimeId = lambda: [self.RID if rid is None else rid]
        return SimpleNamespace(root=root, busy=False, value="", url="app://wb",
                               copies=[], editor=None)

    def call(self, view):
        conv = mock.Mock()
        conv.read = mock.Mock(return_value=view)
        conv.empty = mock.Mock(side_effect=[False, True])
        conv.clear = mock.Mock()
        with mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
                mock.patch.object(wb, "ReplyConversation", return_value=conv):
            got = wb._already_replied(PROMPT)
        return got, conv

    def test_same_document_without_session_id_is_refused(self):
        """R7：同一文档同一事务，但记录里没有会话 id -> 不再"兼容成功"。"""
        path, before = self.seed()
        got, conv = self.call(self.page())
        self.assertIsNone(got, "没有会话 id 就不能认已答")
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")
        self.assertFalse(conv.clear.called, "没有结论就不许清草稿")
        self.assertEqual(path.read_bytes(), before, "拒绝也不许改提交记录")

    def test_foreign_document_with_same_pair_is_refused(self):
        """CE1：另一个文档里恰好有同一条提问和旧回答。"""
        path, before = self.seed()
        got, conv = self.call(self.page(rid=999))
        self.assertIsNone(got, "跨文档旧回答绝不能当本轮结果")
        self.assertEqual(wb.recovery_note()["reason"], "doc_rid_mismatch")
        self.assertFalse(conv.clear.called, "没有结论就不许清草稿")
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_journal_without_scope_is_refused(self):
        self.seed(scope=None)
        got, conv = self.call(self.page())
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "scope_missing")
        self.assertFalse(conv.clear.called)

    def test_restarted_client_is_refused(self):
        self.seed(scope=self.scope(hwnd=777))
        got, _conv = self.call(self.page())
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "hwnd_mismatch")

    def test_without_journal_nothing_is_claimed(self):
        got, _conv = self.call(self.page())
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "no_journal")


# ================================================================ CE2：gemini
class StubScreen:
    def __init__(self, *args, **kwargs):
        self.target_hwnd = 4242

    def bind(self, hwnd):
        return True

    def focus(self):
        return True

    def grab(self, *args):
        return np.zeros((2, 2, 3), dtype=np.uint8)


def foreign_tab_page(answer=WEB_ANSWER):
    return flat("T:你说", f"T:{WEB_PROMPT}", "T:Gemini 说", f"T:{answer}",
                "B:答得很好", "B:复制")


class GeminiRecoveryIdentity(unittest.TestCase):
    RID = 700
    HWND = 4242
    URL = "https://gemini.google.com/app/FOREIGN-TAB"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(tg, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scope(self, url=None, rid=None):
        return submissions.scope_from(site="gemini", url=url or self.URL,
                                      doc_rid=[self.RID if rid is None else rid],
                                      hwnd=self.HWND)

    def seed(self, *, scope="default"):
        submissions.reserve("gemini", WEB_PROMPT,
                            scope=self.scope() if scope == "default" else scope)
        submissions.finish("gemini", WEB_PROMPT, {"submission": "accepted"})
        path = submissions.path_for("gemini", WEB_PROMPT, root=self.root)
        return path, path.read_bytes()

    def run_ask(self):
        page = foreign_tab_page()
        page.control.GetRuntimeId = lambda: [self.RID]

        def conversation(screen, name):
            return SimpleNamespace(read=lambda **k: SimpleNamespace(
                root=page, busy=False, value="", url=self.URL,
                copies=[], editor=None))

        site = SimpleNamespace(name="gemini", newchat="gem_newchat")
        with mock.patch.object(gemini, "Screen", StubScreen), \
                mock.patch.object(gemini.tier_guard, "ROOT", self.root), \
                mock.patch.object(gemini, "ensure_site", return_value=self.HWND) as ensure, \
                mock.patch.object(gemini.zoom_mod, "ensure") as zoom, \
                mock.patch.object(gemini, "open_new_chat",
                                  return_value={"ok": True}) as newchat, \
                mock.patch.object(gemini, "Conversation", conversation), \
                mock.patch.object(gemini, "_send_and_wait",
                                  return_value={"ok": True, "stage": "answered",
                                                "submission": "accepted",
                                                "sent": True, "reply": "SENT"}) as send, \
                mock.patch.object(gemini.failure, "FAIL_DIR", self.root / "fail"), \
                mock.patch.object(submissions, "reserve",
                                  side_effect=AssertionError("recovery must not write")), \
                mock.patch.object(submissions, "finish",
                                  side_effect=AssertionError("recovery must not write")):
            result = gemini.ask(site, WEB_PROMPT, mode="new", timeout=0, purpose="test")
        return result, newchat, send, zoom, ensure

    def test_foreign_tab_same_pair_is_refused(self):
        """CE2：选中的标签里有一对同样的问答，但不是我们发的那个标签。"""
        path, before = self.seed(scope=self.scope(url="https://gemini.google.com/app/OURS"))
        result, newchat, send, zoom, ensure = self.run_ask()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "interrupted")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(result["prior_submission"], "accepted")
        self.assertEqual(result["identity"], "url_mismatch")
        self.assertEqual(newchat.call_count, 0, "不许新开对话")
        self.assertEqual(send.call_count, 0, "不许发送")
        self.assertEqual(zoom.call_count, 0, "不许做任何准备动作")
        self.assertEqual(ensure.call_args.kwargs.get("continue_session"), True)
        self.assertEqual(path.read_bytes(), before, "记录一个字节都不许变")
        self.assertNotIn("reply", result, "身份不对就不许把旧回答交出去")

    def test_legacy_journal_is_refused(self):
        path, before = self.seed(scope=None)
        result, newchat, send, _zoom, _ensure = self.run_ask()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["identity"], "scope_missing")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(path.read_bytes(), before)

    def test_same_tab_same_transaction_still_recovers(self):
        path, before = self.seed()
        result, newchat, send, _zoom, _ensure = self.run_ask()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered")
        self.assertEqual(result["reply"], WEB_ANSWER)
        self.assertEqual(result["identity"], "matched")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
