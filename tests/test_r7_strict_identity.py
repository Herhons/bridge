"""R7：标题**不是**身份。同一文档/同一 URL/同名会话也必须拒；恢复不再靠文档身份兼容。

R6 用"会话名 + 标签条唯一"当证明，R6 自己也承认同名会话分不出来。R7 的口径：
**标题可变、可重复，不能当证明**。
  · 采集：提问从树里消失后，"只多一条回复"的兜底只有在**与标题无关的会话 id**
    （地址里的会话路径，如 Gemini `/app/<id>`）可读且没变时才允许；否则一律拒；
  · 恢复：记录里没有会话 id、现在读不到、或 id 对不上 -> 一律拒，
    **不再用 hwnd / doc_rid / URL 的文档身份"兼容成功"**。

钉住的六件事（RED 在前、GREEN 在后，判据不改）：
  1. 同名同 URL 同文档、换了会话、只有一条外来新回复 -> 拒；
  2. 完全没有会话身份 -> 拒；
  3. 收上来的"新回复"其实是我们**已经有过的旧答案** -> 拒；
  4. 老记录（只有文档身份、没有会话 id）-> 恢复不认已答；
  5. 记录有 id、现在读不到 / 对不上 -> 恢复不认；
  6. 正例：提问还在树上（正常路径），或会话 id 读得到且一致 -> 照常工作。
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

from test_card07 import bubble                                  # noqa: E402
from test_conversation import FakeConversation, node            # noqa: E402
from qi import submissions, tier_guard as tg                    # noqa: E402
from qi import workbuddy as wb                                  # noqa: E402
from qi.conversation import View, ask                           # noqa: E402

PROMPT = "first line\nsecond line"
ANSWER = "new answer"
OLD = "old answer"                       # 发送前就已经在页面上的回答
RID = 4242
DESKTOP_URL = "oc://renderer/index.html"          # 没有会话 id 的 SPA
CHAT_URL = "https://gemini.google.com/app/abc123"  # 有会话 id 的会话页


def page(*, title="Bridge A", tabs=("会话", "Bridge A"), prompt=False, replies=(),
         value="", busy=False, rid=RID, url=DESKTOP_URL, header=True,
         suffix=" · OpenCode"):
    """一张桌面 SPA 快照：文档标题 + 标签条 + 可选提问/若干回复。rid 与 url 恒不变。"""
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
    for i, answer in enumerate(replies):
        top = 520 + i * 70
        body = node("TextControl", answer, 30 + i * 10, box=(80, top, 900, top + 28))
        copy_btn = node("ButtonControl", "Copy response", 31 + i * 10,
                        box=(80, top + 40, 104, top + 64))
        children.append(node("GroupControl", "", 32 + i * 10, body, copy_btn,
                             box=(80, top, 900, top + 70)))
        copies.append(copy_btn)
    root = node("DocumentControl", f"{title}{suffix}", rid,
                editor, *children, box=(0, 0, 1280, 800))
    return View(url, root, editor, value, busy, copies)


class TextFake(FakeConversation):
    """复制键 -> 文本（按控件 key），用来模拟"复制到的是哪一条"。"""

    def __init__(self, snapshots, texts):
        super().__init__(snapshots)
        self.texts = dict(texts)

    def copy(self, target):
        self.copied.append(target.key)
        return self.texts.get(target.key, "new answer")


class Collection(unittest.TestCase):
    def run_turn(self, snapshots, texts=None, timeout=8):
        clock = [0.0]
        conv = TextFake(snapshots, texts or {})
        with mock.patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("qi.conversation.time.sleep",
                           side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout)
        return result, conv

    def turn(self, first, then, texts=None):
        return self.run_turn(list(first) + list(then), texts=texts)

    def test_same_title_same_url_switched_session_is_never_answered(self):
        """标题、标签条、文档、URL 全一样，但换了会话：一条外来回复也不许收。"""
        keep = lambda **kw: page(title="Bridge A", tabs=("会话", "Bridge A"), **kw)  # noqa: E731
        result, conv = self.turn(
            [keep(), keep(value=PROMPT), keep(prompt=True)],
            [keep()] * 4 + [keep(replies=(ANSWER,))])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed", result)
        self.assertEqual(result["reason"], "prompt_gone", result)
        self.assertEqual(result.get("session"), "unproven", result)
        self.assertIsNone(result.get("session_id"), result)
        self.assertEqual(result["submission"], "accepted", "发出去的事实不许改口")
        self.assertEqual(conv.copied, [], "没有会话证明，一条回复都不许复制")
        self.assertEqual(conv.submits, 1, "绝不自动重发")

    def test_missing_session_surface_is_still_refused(self):
        """没有标题/标签条（浏览器页形态）：同样不许兜底。"""
        blank = lambda **kw: page(title="Bridge A", tabs=(), header=False, **kw)  # noqa: E731
        result, conv = self.turn(
            [blank(), blank(value=PROMPT), blank(prompt=True)],
            [blank()] * 4 + [blank(replies=(ANSWER,))])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("session"), "unproven", result)
        self.assertEqual(conv.copied, [])

    def test_old_identical_answer_is_not_collected_as_new(self):
        """换会话后"新"的那条回复，其实是我们发送前就见过的旧答案文本 -> 拒。"""
        one_old = lambda **kw: page(title="Bridge A", tabs=("会话", "Bridge A"),  # noqa: E731
                                    replies=(OLD,), **kw)
        two_replies = lambda **kw: page(title="Bridge A", tabs=("会话", "Bridge A"),  # noqa: E731
                                        replies=(OLD, OLD), **kw)
        result, conv = self.turn(
            [one_old(), one_old(value=PROMPT), one_old(prompt=True)],
            [one_old()] * 4 + [two_replies()],
            texts={(31,): OLD, (41,): OLD})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "prompt_gone", result)
        self.assertEqual(conv.copied, [], "旧答案不许被当成这一轮的新回复")

    def test_prompt_still_visible_is_the_normal_positive(self):
        """正例：提问还在树上、其后有新回复 -> 照常收（这条不依赖会话身份）。"""
        keep = lambda **kw: page(title="Bridge A", tabs=("会话", "Bridge A"), **kw)  # noqa: E731
        result, conv = self.turn(
            [keep(), keep(value=PROMPT), keep(prompt=True)],
            [keep(prompt=True, replies=(ANSWER,))])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["reply"], ANSWER)

    def test_provable_session_id_keeps_the_virtualized_prompt_fallback(self):
        """正例：地址里有与标题无关的会话 id，且没变 -> 兜底仍然可用。"""
        keep = lambda **kw: page(title="Chat", tabs=("Chat", "Other"),  # noqa: E731
                                 url=CHAT_URL, suffix=" · Gemini", **kw)
        result, conv = self.turn(
            [keep(), keep(value=PROMPT), keep(prompt=True)],
            [keep()] * 4 + [keep(replies=(ANSWER,))])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["session"], "proven", result)
        self.assertEqual(result["session_id"], "abc123", result)


class Recovery(unittest.TestCase):
    HWND = 4242
    RID = 900

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(tg, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, url, title="Bridge A"):
        doc, copy_btn = bubble(ANSWER, prompt_line=PROMPT)
        doc.name = f"{title} · WorkBuddy"
        doc.control.GetRuntimeId = lambda: [self.RID]
        for i, tab in enumerate(("会话", title)):
            doc.children.append(node("TextControl", tab, 200 + i,
                                     box=(80 + i * 160, 8, 220 + i * 160, 36)))
        doc.children.append(node("TextControl", title, 210, box=(60, 55, 520, 83)))
        return SimpleNamespace(root=doc, value="", busy=False, copies=[copy_btn],
                               url=url, editor=None)

    def journal(self, *, session_id=None, url="app://wb"):
        scope = {"site": "workbuddy", "url": url, "doc_rid": [self.RID],
                 "hwnd": self.HWND}
        if session_id is not None:
            scope["session_id"] = session_id
        submissions.reserve("workbuddy", PROMPT, scope=scope)
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

    def test_legacy_journal_without_session_id_is_not_answered(self):
        """老记录只有文档身份：页面上一模一样的问答也不认（不再兼容成功）。"""
        self.journal()
        got = self.recover(self.page("app://wb"))
        self.assertIsNone(got, "没有会话 id 的旧记录不能当'已答'")
        self.assertEqual(wb.recovery_note()["reason"], "session_id_missing")
        # 拒绝不许有副作用：记录仍是 accepted（不重发、不改写状态）
        self.assertEqual(submissions.read("workbuddy", PROMPT, root=self.root)["state"],
                         "accepted")

    def test_unreadable_session_id_is_refused(self):
        """记录有 id，现在这个页面读不到地址/id（桌面端、后台标签）-> 拒。"""
        self.journal(session_id="abc123", url=CHAT_URL)
        got = self.recover(self.page(""))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "session_id_unreadable")

    def test_mismatched_session_id_is_refused(self):
        """记录里的会话 id 与当前会话不一致 -> 拒（哪怕 URL 字符串相同）。"""
        submissions.reserve("workbuddy", PROMPT, scope={
            "site": "workbuddy", "url": CHAT_URL, "doc_rid": [self.RID],
            "hwnd": self.HWND, "session_id": "other999"})
        submissions.finish("workbuddy", PROMPT, {"submission": "accepted"})
        got = self.recover(self.page(CHAT_URL))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "session_id_mismatch")

    def test_different_chat_url_is_refused(self):
        """换了一个会话页（地址带不同 id）：连文档层都过不去。"""
        self.journal(session_id="abc123", url=CHAT_URL)
        got = self.recover(self.page("https://gemini.google.com/app/other999"))
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "url_mismatch")

    def test_matching_session_id_is_the_positive(self):
        """正例：会话 id 读得到且一致 -> 照常认已答（唯一合法的恢复路径）。"""
        self.journal(session_id="abc123", url=CHAT_URL)
        got = self.recover(self.page(CHAT_URL))
        self.assertIsNotNone(got, "会话 id 一致时应当认已答")
        self.assertEqual(got["stage"], "already_answered")
        self.assertEqual(got["reply"], ANSWER)
        self.assertEqual(got["identity"], "matched")


def recovery_side_effect_guard(case):
    """恢复失败时不许留下任何副作用（不清草稿、不发送、不改记录）。"""
    snapshot = submissions.read("workbuddy", PROMPT, root=case.root)
    return [k for k in ("state",) if not snapshot or snapshot.get(k) != "accepted"]


if __name__ == "__main__":
    unittest.main()
