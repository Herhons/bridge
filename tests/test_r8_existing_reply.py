"""R8：发送前 `_existing_reply` 必须证明"同一会话"，否则 fail-closed。

现场（Hy4 E.3 / R7 §6.4）：`conversation.ask()` 在**采集会话身份之前**先调用
`_existing_reply()`；它只按文本 + 当前文档认领。同一文档、同一 URL 的 SPA 换到
另一个会话时，里面恰好有同一条提问和旧回答，会被当成本轮答案交回去。

本文件钉住：
  1. 同文档同 URL、会话 id 缺失 -> 不认领、不发送、不清草稿、记录不变；
  2. 记录有 id、当前页面 id 不同 -> 同上；
  3. 记录有 id、当前页面读不到 id（桌面 oc://）-> 同上；
  4. 老记录没有会话 id -> 同上；
  5. 根本没有提交记录 -> 同上（页面上同文问答不是我们的）；
  6. 正例：记录里的会话 id 与当前 URL 会话 id 一致 -> 仍按 already_answered；
  7. 正例：合法已答且输入框被回填 -> 仍清草稿并读回为空；
  8. 普通发送路径不受影响（页面上没有本提问时照常写、照常提交）。

全部离线：假控件树 + 假 Conversation，不碰真窗口、不截图、不发消息。
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

from qi import conversation, submissions, tier_guard as tg, ui  # noqa: E402

PROMPT = "R8-SAME-PROMPT"
OLD = "R8-OLD-FOREIGN-ANSWER"
SITE = "chatgpt"
URL_SENT = "https://chatgpt.com/c/SENT-1"
URL_OTHER = "https://chatgpt.com/c/OTHER-2"
URL_DESKTOP = "oc://renderer/index.html"


def node(kind, name, key, *children, top=0, height=20):
    control = SimpleNamespace(
        GetRuntimeId=lambda: [key], IsEnabled=True,
        BoundingRectangle=SimpleNamespace(left=0, top=top, right=100, bottom=top + height))
    item = ui.Node(kind, name, control, children=list(children))
    for child in item.children:
        child.parent = item
    return item


def make_view(url, *, value="", has_prompt=True, has_reply=True):
    children = []
    copies = []
    if has_prompt:
        children.append(node("TextControl", PROMPT, 20, top=0))
    if has_reply:
        children.append(node("TextControl", OLD, 21, top=40))
        copy_btn = node("ButtonControl", "复制", 30, top=60)
        children.append(copy_btn)
        copies = [copy_btn]
    root = node("DocumentControl", "", 1, *children, top=0, height=200)
    return SimpleNamespace(root=root, value=value, url=url, busy=False,
                           copies=copies, editor=None)


class Conv:
    """最小替身：真的走 conversation.ask 的写/提交/清/复制分支。"""

    site = SITE

    def __init__(self, views, copy_text=OLD):
        self.views = list(views)
        self.copy_text = copy_text
        self.writes = []
        self.submits = 0
        self.clears = 0
        self.copied = []

    def read(self, attempts=4, delay=0.4):
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]

    def empty(self, view):
        return not view.value.strip()

    def write(self, view, prompt):
        self.writes.append(prompt)
        view.value = prompt

    def submit(self, view):
        self.submits += 1

    def clear(self, view):
        self.clears += 1
        view.value = ""

    def paste(self, view, prompt):
        view.value = prompt

    def copy(self, target):
        self.copied.append(target)
        return self.copy_text


def seed(tmp, *, state="accepted", sid=None, url=URL_SENT):
    """在隔离 ROOT 里写一条真实结构的提交记录（不碰仓库 runs/）。"""
    scope = {"site": SITE, "url": url, "doc_rid": [7], "hwnd": 4242,
             "pid": 100, "process_start_time": 1.5}
    if sid:
        scope["session_id"] = sid
    with mock.patch.object(tg, "ROOT", Path(tmp)):
        submissions.reserve(SITE, PROMPT, scope=scope)
        submissions.finish(SITE, PROMPT, {"submission": state}, scope=scope)
    return submissions.path_for(SITE, PROMPT, root=Path(tmp))


def run(tmp, view, *, conv=None):
    conv = conv or Conv([view])
    with mock.patch.object(tg, "ROOT", Path(tmp)):
        result = conversation.ask(conv, PROMPT, timeout=0.05)
    return result, conv


class FailClosedWithoutProof(unittest.TestCase):
    def assert_fail_closed(self, result, conv, view, journal, reason, state,
                           copied=0):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "session_unproven", result)
        self.assertEqual(result["reason"], reason, result)
        self.assertEqual(result["submission"], state, result)
        self.assertFalse(result["sent"], result)
        self.assertEqual(len(conv.copied), copied,
                         "证明不了会话就不许复制旧回答；证明后也只读复制")
        self.assertEqual(conv.writes, [], "失败关闭不许写输入框")
        self.assertEqual(conv.submits, 0, "失败关闭绝不提交")
        self.assertEqual(conv.clears, 0, "失败关闭不许清草稿")
        self.assertEqual(view.value, "未发出的草稿", "草稿必须原样保留")
        if journal is not None:
            self.assertTrue(journal.exists())
            self.assertEqual(journal.read_bytes(), self.before, "提交记录不许被改")

    def prepare(self):
        # NO DELETION：卡面要求保留所有测试产物；用 mkdtemp 而不是
        # TemporaryDirectory（后者退出时会自动清掉刚写下的记录）。
        return tempfile.mkdtemp(prefix="r8_existing_reply_")

    def test_same_document_missing_session_id_is_not_claimed(self):
        """同文档同 URL、记录里没有会话 id：旧回答不许认领。"""
        tmp = self.prepare()
        journal = seed(tmp, sid=None, url=URL_DESKTOP)
        self.before = journal.read_bytes()
        view = make_view(URL_DESKTOP, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, journal,
                                "session_id_missing", "accepted")

    def test_different_session_id_is_not_claimed(self):
        """记录 id=SENT-1，当前页面是另一个会话：不许认领。"""
        tmp = self.prepare()
        journal = seed(tmp, sid="SENT-1", url=URL_SENT)
        self.before = journal.read_bytes()
        view = make_view(URL_OTHER, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, journal,
                                "session_id_mismatch", "accepted")

    def test_unreadable_session_id_is_not_claimed(self):
        """记录有 id，当前页面读不到（桌面 oc://）：不许认领。"""
        tmp = self.prepare()
        journal = seed(tmp, sid="SENT-1", url=URL_SENT)
        self.before = journal.read_bytes()
        view = make_view(URL_DESKTOP, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, journal,
                                "session_id_unreadable", "accepted")

    def test_legacy_journal_without_session_id_is_not_claimed(self):
        """老记录只有文档身份：页面上同文问答也不认。"""
        tmp = self.prepare()
        journal = seed(tmp, sid=None, url=URL_SENT)
        self.before = journal.read_bytes()
        view = make_view(URL_SENT, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, journal,
                                "session_id_missing", "accepted")

    def test_no_journal_foreign_identical_answer_is_not_claimed(self):
        """没有本事务记录：页面上的同文问答不是我们的，不许认领。"""
        tmp = self.prepare()
        self.before = None
        view = make_view(URL_OTHER, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, None,
                                "no_journal", "not_sent")

    def test_unknown_state_is_preserved_on_refusal(self):
        """记录是 unknown：拒绝时仍如实保留 unknown，不降级成别的。"""
        tmp = self.prepare()
        journal = seed(tmp, state="unknown", sid=None, url=URL_DESKTOP)
        self.before = journal.read_bytes()
        view = make_view(URL_DESKTOP, value="未发出的草稿")
        result, conv = run(tmp, view)
        self.assert_fail_closed(result, conv, view, journal,
                                "session_id_missing", "unknown")

    def test_proved_identity_but_unreadable_reply_does_not_resend(self):
        """身份已证明、但复制回来的是提问本身：也不许当"没答过"重发。"""
        tmp = self.prepare()
        journal = seed(tmp, sid="1", url="https://chatgpt.com/c/1")
        self.before = journal.read_bytes()
        view = make_view("https://chatgpt.com/c/1", value="未发出的草稿")
        result, conv = run(tmp, view, conv=Conv([view], copy_text=PROMPT))
        self.assert_fail_closed(result, conv, view, journal,
                                "reply_unreadable", "accepted", copied=1)


class ProvedPositive(unittest.TestCase):
    def test_proved_same_session_still_returns_already_answered(self):
        """记录 id 与当前 URL 会话 id 一致：合法正例照常只读返回。"""
        tmp = tempfile.mkdtemp(prefix="r8_positive_")
        seed(tmp, sid="1", url="https://chatgpt.com/c/1")
        view = make_view("https://chatgpt.com/c/1")
        result, conv = run(tmp, view)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered", result)
        self.assertEqual(result["reply"], OLD)
        self.assertFalse(result["sent"])
        self.assertEqual(conv.writes, [])
        self.assertEqual(conv.submits, 0)
        self.assertEqual(len(conv.copied), 1)

    def test_proved_same_session_clears_refilled_draft(self):
        """合法已答 + 回填草稿：仍清草稿并读回为空（CARD-09f 正例不变）。"""
        tmp = tempfile.mkdtemp(prefix="r8_positive_refill_")
        seed(tmp, sid="1", url="https://chatgpt.com/c/1")
        view = make_view("https://chatgpt.com/c/1", value=PROMPT)
        result, conv = run(tmp, view)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "already_answered", result)
        self.assertTrue(result["cleared_draft"], result)
        self.assertEqual(conv.clears, 1)
        self.assertEqual(view.value, "")
        self.assertEqual(conv.submits, 0)


class NormalSendUnaffected(unittest.TestCase):
    def test_no_prompt_on_page_still_writes_and_submits(self):
        """页面上没有本提问：照常写、照常提交（不因新闸门误伤）。"""
        tmp = tempfile.mkdtemp(prefix="r8_normal_send_")
        view = make_view(URL_OTHER, has_prompt=False, has_reply=False)
        conv = Conv([view])
        with mock.patch.object(tg, "ROOT", Path(tmp)):
            result = conversation.ask(conv, PROMPT, timeout=0.05)
        self.assertEqual(conv.writes, [PROMPT])
        self.assertEqual(conv.submits, 1)
        # 假页面上没有"提问气泡"，认领不到 accepted；只要确认它真的写了/发了，
        # 没被新闸门误拦即可（submission 仍是 unknown，stage 超时）。
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["stage"], "timeout")


if __name__ == "__main__":
    unittest.main()
