"""B32：首轮新会话「落库」判据要按路径形状，不能按"读了几次"。

2026-11-12 真机（gemini，Round 4 会审前的探活）：
发送「请只回复两个字：收到」后地址一直是 `/app`，到**第 16 秒**答案出来时
才变成 `/app/1bd58577…`。旧判据 `ACCEPT_NAV_GRACE_READS = 2` 只覆盖约 0.8 秒，
读到这里已经是第 40 次上下 —— 于是明明发成功了、答案也在页面上，
却被判 `conversation_changed / document_changed`，一次会审白跑。

这里钉住三条：
  1. 落库迟到（很多次读之后才换地址） -> 仍算同一会话，能收卷；
  2. 落库时答案已经上屏           -> 不许把答案划进基线，否则白等到超时；
  3. 同一容器下换到**另一个**会话   -> 必须继续拒（放宽不能顺带放走真的换会话）。
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

from test_conversation import FakeConversation, view          # noqa: E402
from qi.conversation import ask, _first_turn_landed           # noqa: E402

PROMPT = "first line\nsecond line"
APP = "https://gemini.google.com/app"
APP_ID = "https://gemini.google.com/app/1bd58577f9ed3799"
APP_OTHER = "https://gemini.google.com/app/ffffffffffffffff"
CHATGPT_LOCAL = "https://chatgpt.com/c/local-chatgpt:abc"
CHATGPT_REAL = "https://chatgpt.com/c/68f1a2b3-1111-2222-3333-444455556666"


def shape(old, new):
    return {"path": old, "rid": [1]}, {"path": new, "rid": [1]}


class LandingRule(unittest.TestCase):
    def test_gemini_container_to_session_is_landing(self):
        self.assertTrue(_first_turn_landed(*shape("/app", "/app/1bd58577f9ed3799")))

    def test_chatgpt_local_to_real_is_still_landing(self):
        """B17 的老路径不能被这次改动碰掉。"""
        self.assertTrue(_first_turn_landed(
            *shape("/c/local-chatgpt:abc", "/c/68f1a2b3-1111")))

    def test_another_session_under_the_same_container_is_not_landing(self):
        self.assertFalse(_first_turn_landed(
            *shape("/app/1bd58577f9ed3799", "/app/ffffffffffffffff")))

    def test_site_root_to_session_is_landing(self):
        """账本里有 4 条真实记录：`grok.com/` -> `grok.com/c/<uuid>`。

        形态和 gemini 完全一样，只是根路径写作 `/`。只覆盖 `/app` 等于把
        grok 与 chatgpt 的首轮落库漏在外面。
        """
        self.assertTrue(_first_turn_landed(
            *shape("/", "/c/e30751a4-35b0-4b2d-98c8-8c6ed164e")))
        self.assertTrue(_first_turn_landed(
            *shape("/", "/c/68f1a2b3-1111-2222-3333-444455556666")))

    def test_deepseek_root_to_chat_is_landing(self):
        """deepseek 的会话路径是 `/a/chat/s/<id>`，同样三段。"""
        self.assertTrue(_first_turn_landed(
            *shape("/", "/a/chat/s/4bc998b5-1767-49a5-9d93-0d1c2f2b1a11")))

    def test_root_to_a_deeper_path_is_not_landing(self):
        """更深的路径不是"首轮落库"，别放过去。"""
        self.assertFalse(_first_turn_landed(*shape("/", "/c/abc/extra/more")))
        self.assertFalse(_first_turn_landed(*shape("/", "/settings")))

    def test_session_to_another_session_is_not_landing(self):
        self.assertFalse(_first_turn_landed(*shape("/c/abc", "/c/def")))

    def test_unrelated_path_change_is_not_landing(self):
        self.assertFalse(_first_turn_landed(*shape("/app/abc", "/settings")))
        self.assertFalse(_first_turn_landed(*shape("/app", "/app")))


class LandingDuringCollection(unittest.TestCase):
    def run_turn(self, snapshots, timeout=40):
        clock = [0.0]
        conv = FakeConversation(snapshots)
        with patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                patch("qi.conversation.time.sleep",
                      side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout)
        return result, conv

    def test_late_landing_is_not_a_conversation_change(self):
        """地址在第 40 次读之后才变成 /app/<id>：这是落库迟到，不是换会话。"""
        idle = [view(url=APP, prompt=True)] * 40
        result, conv = self.run_turn(
            [view(url=APP), view(url=APP, value=PROMPT)] + idle
            + [view(url=APP_ID, prompt=True), view(url=APP_ID, prompt=True, reply=True)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["reply"], "new answer")
        self.assertEqual(conv.copied, [(30,)])
        self.assertEqual(conv.submits, 1, "绝不重发")

    def test_answer_already_visible_when_landing_happens_is_collected(self):
        """落库和答案同一帧到：不许把刚到的答案划进基线。"""
        idle = [view(url=APP, prompt=True)] * 40
        result, conv = self.run_turn(
            [view(url=APP), view(url=APP, value=PROMPT)] + idle
            + [view(url=APP_ID, prompt=True, reply=True)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "new answer")
        self.assertEqual(conv.copied, [(30,)])

    def test_moving_to_another_session_is_still_refused(self):
        """同一容器下换到别的会话：必须继续拒，且不复制别人的回答。"""
        idle = [view(url=APP_ID, prompt=True)] * 3
        result, conv = self.run_turn(
            [view(url=APP_ID), view(url=APP_ID, value=PROMPT)] + idle
            + [view(url=APP_OTHER, prompt=True, reply=True)])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "conversation_changed")
        self.assertEqual(result["reason"], "document_changed")
        self.assertEqual(conv.copied, [])
        self.assertEqual(conv.submits, 1)


if __name__ == "__main__":
    unittest.main()
