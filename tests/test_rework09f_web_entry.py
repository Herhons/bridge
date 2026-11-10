"""CARD-09f 返工：Web 端的 ask 必须**在准备动作之前**只读恢复已提交事实。

独立验收反例（2026-11-06）：持久化记录已经是 accepted，但 `ask` 先建/切换会话，
新会话失败或读文档失败时直接报 `submission=not_sent, sent=false`，调用方会以为
没发过而重发。卡面第 18/27 行要求：先做本轮已答/accepted 的**只读**恢复，
恢复失败也要**保留** accepted/unknown。

这里的判定标准（逐条断言）：
  · 记录已存在时，一次新对话都不许点、一次 submit 都不许有、journal 一个字节都不许变；
  · 页面里已经有本轮回复 → 只读返回 already_answered（不新开对话）；
  · 恢复过程自己失败 → submission 仍是 accepted/unknown，绝不退回 not_sent；
  · 没有记录时行为不变（照常开新对话、照常走 _send_and_wait）。
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
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "src" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "scripts"))

from test_card07 import flat
from qi import gemini, submissions, tier_guard as tg
from qi.ui import UiDocsLost
import grok_use  # noqa: E402  （src/scripts 已在 sys.path 上，和 test_card09f 共用同一个模块）

PROMPT = "只回复 09f-OK"
ANSWER = "09f-OK"


def gemini_page(answer=None):
    items = ["T:你说", f"T:{PROMPT}"]
    if answer:
        items += ["T:Gemini 说", f"T:{answer}", "B:答得很好", "B:复制"]
    return flat(*items)


def grok_page(answer=None):
    items = ["T:你", f"T:{PROMPT}"]
    if answer:
        items += ["T:Grok", f"T:{answer}", "B:复制响应", "B:Like"]
    return flat(*items)


class StubScreen:
    def __init__(self, *args, **kwargs):
        self.target_hwnd = 4242

    def bind(self, hwnd):
        return True

    def focus(self):
        return True

    def grab(self, *args):
        return np.zeros((2, 2, 3), dtype=np.uint8)


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(tg, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    # 2026-11-07 R5：恢复还要求"同一个标签/文档"。固定 rid 700、句柄 4242。
    RID = 700
    HWND = 4242

    def identity_scope(self, site, url):
        return submissions.scope_from(site=site, url=url, doc_rid=[self.RID],
                                      hwnd=self.HWND)

    def seed(self, endpoint, state, *, scope=None):
        if state is None:
            return None
        submissions.reserve(endpoint, PROMPT, scope=scope)
        if state == "accepted":
            submissions.finish(endpoint, PROMPT, {"submission": "accepted"})
        elif state == "answered":
            submissions.finish(endpoint, PROMPT, {"stage": "answered"})
        return submissions.path_for(endpoint, PROMPT, root=self.root)

    def snapshot(self, path):
        return path.read_bytes() if path and path.exists() else None


class GeminiEntryRecovery(JournalCase):
    def run_ask(self, *, journal=None, answer=None, read_error=None, mode="new",
                prior_works=True, scope=None):
        path = self.seed("gemini", journal, scope=scope)
        before = self.snapshot(path)
        conv_root = gemini_page(answer)
        conv_root.control.GetRuntimeId = lambda: [self.RID]

        def conversation(screen, name):
            if read_error is not None:
                raise read_error
            return SimpleNamespace(read=lambda **k: SimpleNamespace(
                root=conv_root, busy=False, value="",
                url="https://gemini.google.com/app/g-09f",
                copies=[], editor=None))

        site = SimpleNamespace(name="gemini", newchat="gem_newchat")
        with mock.patch.object(gemini, "Screen", StubScreen), \
                mock.patch.object(gemini.tier_guard, "ROOT", self.root), \
                mock.patch.object(gemini, "ensure_site", return_value=4242) as ensure, \
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
            result = gemini.ask(site, PROMPT, mode=mode, timeout=0, purpose="test")
        return result, newchat, send, ensure, zoom, path, before

    def test_prior_accepted_refuses_before_new_chat_and_keeps_state(self):
        result, newchat, send, ensure, zoom, path, before = self.run_ask(journal="accepted")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "accepted")
        self.assertFalse(result["sent"])
        self.assertEqual(result["stage"], "interrupted")
        self.assertEqual(result["reason"], "submission_pending")
        self.assertEqual(newchat.call_count, 0, "不许在确认已提交之前换会话")
        self.assertEqual(zoom.call_count, 0, "不许做任何准备动作")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(ensure.call_args.kwargs.get("continue_session"), True,
                         "只读恢复只能用续聊选标签，绝不打开首页")
        self.assertEqual(self.snapshot(path), before, "持久化记录一个字节都不许变")

    def test_prior_accepted_with_page_answer_returns_read_only_already_answered(self):
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER,
            scope=self.identity_scope("gemini", "https://gemini.google.com/app/g-09f"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered")
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["identity"], "matched")
        self.assertEqual(result["basis"], "prior_submission_recovery")
        self.assertEqual(result.get("prior_submission"), "accepted")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_unknown_prior_is_preserved_when_recovery_read_fails(self):
        err = UiDocsLost("docs lost")
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="unknown", read_error=err)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["stage"], "uia_docs_lost")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_answered_prior_is_reported_as_already_answered_without_mutation(self):
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="answered", answer=ANSWER,
            scope=self.identity_scope("gemini", "https://gemini.google.com/app/g-09f"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered")
        self.assertEqual(result.get("prior_submission"), "answered")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_without_prior_record_the_normal_path_is_untouched(self):
        result, newchat, send, _ensure, zoom, _path, _before = self.run_ask(journal=None)
        self.assertEqual(newchat.call_count, 1)
        self.assertEqual(zoom.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "SENT")

    def test_legacy_journal_without_scope_is_refused(self):
        """老记录没有标签身份：页面上一模一样的问答也不能采信（CE2）。"""
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER)          # 故意不带 scope
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(result["stage"], "interrupted")
        self.assertEqual(result["identity"], "scope_missing")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_foreign_tab_scope_is_refused(self):
        """记录是 A 标签，选中的却是 B 标签（同文同答）——不许采信。"""
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER,
            scope=self.identity_scope("gemini", "https://gemini.google.com/app/OTHER"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["identity"], "url_mismatch")
        self.assertEqual(result["stage"], "interrupted")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)


class GrokEntryRecovery(JournalCase):
    def run_ask(self, *, journal=None, answer=None, read_error=None, mode="new",
                scope=None):
        path = self.seed("grok", journal, scope=scope)
        before = self.snapshot(path)
        conv_root = grok_page(answer)
        conv_root.control.GetRuntimeId = lambda: [self.RID]

        def conversation(screen, name):
            if read_error is not None:
                raise read_error
            return SimpleNamespace(read=lambda **k: SimpleNamespace(
                root=conv_root, busy=False, value="", url="https://grok.com/chat/k-09f",
                copies=[], editor=None))

        site = SimpleNamespace(name="grok", newchat="grok_newchat")
        with mock.patch.object(grok_use, "Screen", StubScreen), \
                mock.patch.object(grok_use.tier_guard, "ROOT", self.root), \
                mock.patch.object(grok_use, "ensure_site", return_value=4242) as ensure, \
                mock.patch.object(grok_use.zoom_mod, "ensure") as zoom, \
                mock.patch.object(grok_use, "open_new_chat",
                                  return_value={"ok": True}) as newchat, \
                mock.patch.object(grok_use, "Conversation", conversation), \
                mock.patch.object(grok_use, "_send_and_wait",
                                  return_value={"ok": True, "stage": "answered",
                                                "submission": "accepted",
                                                "sent": True, "reply": "SENT"}) as send, \
                mock.patch.object(grok_use.failure, "FAIL_DIR", self.root / "fail"), \
                mock.patch.object(submissions, "reserve",
                                  side_effect=AssertionError("recovery must not write")), \
                mock.patch.object(submissions, "finish",
                                  side_effect=AssertionError("recovery must not write")):
            result = grok_use.ask(site, PROMPT, mode=mode, timeout=0, purpose="test")
        return result, newchat, send, ensure, zoom, path, before

    def test_prior_accepted_refuses_before_new_chat_and_keeps_state(self):
        result, newchat, send, ensure, zoom, path, before = self.run_ask(journal="accepted")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "accepted")
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "submission_pending")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(zoom.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(ensure.call_args.kwargs.get("continue_session"), True)
        self.assertEqual(self.snapshot(path), before)

    def test_prior_accepted_with_page_answer_returns_read_only_already_answered(self):
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER,
            scope=self.identity_scope("grok", "https://grok.com/chat/k-09f"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered")
        self.assertEqual(result["reply"], ANSWER)
        self.assertEqual(result["identity"], "matched")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_unknown_prior_is_preserved_when_recovery_read_fails(self):
        err = UiDocsLost("docs lost")
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="unknown", read_error=err)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["stage"], "uia_docs_lost")
        self.assertEqual(newchat.call_count, 0)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_without_prior_record_the_normal_path_is_untouched(self):
        result, newchat, send, _ensure, zoom, _path, _before = self.run_ask(journal=None)
        self.assertEqual(newchat.call_count, 1)
        self.assertEqual(zoom.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["ok"])

    def test_legacy_journal_without_scope_is_refused(self):
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(result["identity"], "scope_missing")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)

    def test_foreign_tab_scope_is_refused(self):
        result, newchat, send, _ensure, _zoom, path, before = self.run_ask(
            journal="accepted", answer=ANSWER,
            scope=self.identity_scope("grok", "https://grok.com/chat/OTHER"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["identity"], "url_mismatch")
        self.assertEqual(send.call_count, 0)
        self.assertEqual(self.snapshot(path), before)


if __name__ == "__main__":
    unittest.main()
