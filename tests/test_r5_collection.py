"""R5 / opencode 收集缺陷：**瞬时 UI 抖动**不许当成"对话变了"，**真的换文档**必须拒。

现场（2026-11-07 04:36–04:39）：唯一任务 accepted=true/sent=true，GLM 还在跑，
runner 却在 167s 报 `conversation_changed` 收场；等任务真的答完（04:49）也没人去收。
根因一面在 `conversation.ask`：接受之后只要**某一次读取**里提问气泡不在树里，
就立刻判"对话变了"，既不看还在不在生成，也不看是不是同一个文档。

这里钉住四种情形（都用真 `conversation.ask`，只伪造 UI 快照）：
  1. 同一会话、生成中短暂读不到提问  -> 继续等，最终收到答案；
  2. 提问一直读不到、但页面多出**唯一一条**回复、身份没变 -> 按新回复收（标 basis）；
  3. 身份变了（文档/地址）且新页面有一对同样的旧问答 -> **拒**，不复制、不改 accepted；
  4. 身份读不出来 -> 不用兜底，拒。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from test_conversation import FakeConversation, node, rect, view  # noqa: E402
from qi.conversation import ask                        # noqa: E402

PROMPT = "first line\nsecond line"


def no_rid(view_obj):
    def boom():
        raise RuntimeError("no runtime id")
    view_obj.root.control.GetRuntimeId = boom
    return view_obj


def foreign(view_obj, rid):
    """同一棵树，但换成另一个文档的 runtime id。"""
    view_obj.root.control.GetRuntimeId = lambda: [rid]
    return view_obj


def stamped(view_obj, title="Bridge A", tabs=("会话", "Bridge A")):
    """给快照补上**应用自己的会话身份**（R6）。

    2026-11-07 起，"提问被虚拟化掉"时只认"比发送前多出来的唯一一条回复"还不够：
    文档 rid 和 URL 在 SPA 换会话时一模一样，必须同时证明"还是同一个会话"
    （`conversation.session_stamp`）。这里按真机形态补：文档标题带会话名、
    顶部标签条里会话名恰好出现一次、48~96px 的标题栏里也是它。
    """
    root = view_obj.root
    root.name = f"{title} · OpenCode"
    root.control.BoundingRectangle = rect(0, 0, 1280, 800)
    for i, tab in enumerate(tabs):
        root.children.append(node("TextControl", tab, 300 + i,
                                  box=(80 + i * 160, 8, 220 + i * 160, 36)))
    root.children.append(node("TextControl", title, 399, box=(60, 55, 520, 83)))
    return view_obj


class CollectionIdentity(unittest.TestCase):
    def run_turn(self, snapshots, timeout=8):
        clock = [0.0]
        conv = FakeConversation(snapshots)
        with patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                patch("qi.conversation.time.sleep",
                      side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout)
        return result, conv

    def test_prompt_gap_while_still_generating_keeps_waiting(self):
        """还在生成时读不到提问：是重排，不是换会话。"""
        result, conv = self.run_turn([
            view(), view(value=PROMPT), view(prompt=True),
            view(busy=True), view(busy=True),
            view(prompt=True, reply=True)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["reply"], "new answer")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(conv.copied, [(30,)])

    def test_prompt_gone_with_only_a_title_stamp_is_refused(self):
        """R7：标题戳不再是证明。没有与标题无关的会话 id，就只多一条回复也不收。"""
        keep = lambda **kw: stamped(view(**kw))          # noqa: E731
        result, conv = self.run_turn(
            [keep(), keep(value=PROMPT), keep(prompt=True)] + [keep()] * 4
            + [keep(reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["reason"], "prompt_gone")
        self.assertEqual(result["session"], "unproven")
        self.assertEqual(conv.copied, [], "没有会话 id 就不许复制回复")
        self.assertEqual(conv.submits, 1, "绝不自动重发")

    def test_prompt_gone_without_session_identity_is_refused(self):
        """R6：同一形态但没有会话身份 -> 不许拿"只多一条回复"兜底。"""
        result, conv = self.run_turn(
            [view(), view(value=PROMPT), view(prompt=True)] + [view()] * 4
            + [view(reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["session"], "unproven")
        self.assertEqual(conv.copied, [])

    def test_two_new_replies_are_ambiguous_and_not_collected(self):
        result, conv = self.run_turn(
            [view(), view(value=PROMPT), view(prompt=True)] + [view()] * 4
            + [view(reply=True, old=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["reason"], "prompt_gone")
        self.assertEqual(conv.copied, [])

    def test_foreign_document_with_same_pair_is_refused(self):
        """CE1 的收集侧：换了文档、里面有一对同样的问答 -> 不许收。"""
        result, conv = self.run_turn([
            view(), view(value=PROMPT), view(prompt=True),
            foreign(view(prompt=True, reply=True), 999)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["reason"], "document_changed")
        self.assertEqual(result["submission"], "accepted", "已经发出去的事实不许改口")
        self.assertEqual(conv.copied, [], "别人的旧回答一个字都不许复制")
        self.assertEqual(conv.submits, 1, "绝不自动重发")

    def test_foreign_url_with_old_reply_is_refused(self):
        result, conv = self.run_turn([
            view(), view(value=PROMPT), view(prompt=True),
            view(prompt=True, reply=True, url="session:2")])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["reason"], "document_changed")
        self.assertEqual(conv.copied, [])

    def test_unreadable_identity_never_uses_the_fallback(self):
        """身份读不出来时，即使"只多一条回复"也不许收——宁可不交卷。"""
        result, conv = self.run_turn(
            [view(), view(value=PROMPT), view(prompt=True)] + [no_rid(view())] * 6)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["identity"], "unreadable")
        self.assertEqual(conv.copied, [])

    def test_first_send_navigation_without_a_reply_is_still_allowed(self):
        """新会话首轮换地址（/ -> /c/id）是正常的：允许一次重新锚定。"""
        result, conv = self.run_turn([
            view(), view(value=PROMPT), view(prompt=True),
            view(prompt=True, url="session:2"),
            view(prompt=True, reply=True, url="session:2")])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "new answer")


if __name__ == "__main__":
    unittest.main()
