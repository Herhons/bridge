"""B19：gemini 的两处"落点"校验。

1. 提交后 3 秒内必须看到落点（提问块 / 新回复 / 生成中 / 会话 id），否则立刻
   `submit_not_landed`，不再空转到 900 秒（真机 job_review4 就是这么空转的）。
2. 点完「发起新对话」要校验是不是真的回到了可写的新对话，两次都不行才
   `newchat_not_landed`；读不出树时不判死。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import flow, gemini                          # noqa: E402

SITE = flow.Site(name="gemini", title_hint="Google Chrome", composer="gem_composer",
                 send="gem_send_on", actions=("gem_actions", "gem_actions_task"),
                 copy="gem_copy", composer_offset=(200, 0), newchat="gem_newchat",
                 send_mode="exists", send_threshold=0.88,
                 placeholders=("问问 Gemini", "Ask Gemini"))


class Node:
    def __init__(self, kind="TextControl", name="", children=None):
        self.kind = kind
        self.name = name
        self.children = children or []
        self.control = SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                       BoundingRectangle=None)

    def walk(self):
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


def _root():
    return Node("DocumentControl", "", [])


def _stuck_root():
    """输入框里还留着文字的样子：动作条上有可点的发送键。"""
    return Node("DocumentControl", "", [Node("ButtonControl", "发送", [])])


class LandingTests(unittest.TestCase):
    def _run(self, views, timeout=60):
        clock = [0.0]
        fake_conv = mock.Mock()
        fake_conv.read.side_effect = lambda *a, **k: views.pop(0) if len(views) > 1 else views[0]
        written = SimpleNamespace(value="提问")
        submits = []

        with mock.patch.object(gemini, "Conversation", return_value=fake_conv), \
                mock.patch.object(gemini, "write_prompt", return_value=written), \
                mock.patch.object(gemini, "_submit", side_effect=lambda *a: submits.append(1)), \
                mock.patch.object(gemini, "_clear_draft", return_value=True), \
                mock.patch.object(gemini, "heartbeat"), \
                mock.patch.object(gemini.uilock, "section",
                                  mock.MagicMock(return_value=mock.MagicMock(
                                      __enter__=lambda s: s, __exit__=lambda *a: False))), \
                mock.patch.object(gemini.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(gemini.time, "sleep",
                                  side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = gemini._send_and_wait(SimpleNamespace(), SITE, "提问", timeout)
        return result, submits, clock

    def test_empty_composer_fails_fast_without_resubmit(self):
        """框已空、没有落点 → 只进静默窗，绝不再点第二次（B21 会审 P0）。

        框空是歧义态：可能已经发出去、只是提问块还没渲染。此时重点就是二次发送。
        """
        idle = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=False, value="", editor=None, copies=[])
        result, submits, clock = self._run([idle])
        self.assertEqual(result["stage"], "submit_not_landed", result)
        self.assertEqual(result["reason"], "composer_empty")
        self.assertEqual(len(submits), 1, "框已空就不许再点")
        self.assertEqual(result["submission"], "unknown")
        self.assertIsNone(result["sent"])
        self.assertTrue(result.get("landing_extended"))
        self.assertLess(clock[0], 12.0, "不许拖到 timeout")

    def test_stuck_draft_retries_exactly_once(self):
        """框里还留着我们这段字 = 端确实没收下 → 允许清空重写并再点一次。"""
        stuck = SimpleNamespace(root=_stuck_root(), url="https://gemini.google.com/app",
                                busy=False, value="提问", editor=None, copies=[])
        idle = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=False, value="", editor=None, copies=[])
        # 发送前是空的（否则会被 draft_present 拦在发送之前），发出后才"堵在框里"
        result, submits, clock = self._run([idle, stuck])
        self.assertEqual(result["stage"], "submit_not_landed", result)
        self.assertEqual(result["reason"], "draft_stuck")
        self.assertEqual(result["submission"], "not_sent")
        self.assertIs(result["sent"], False)
        self.assertEqual(len(submits), 2, "首轮 + 有正证据时的一次重试")
        self.assertLess(clock[0], 12.0)

    def test_quiet_window_accepts_late_busy(self):
        """框空但在静默窗内转成"生成中" → 仍然算发出去，且只点过一次。"""
        idle = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=False, value="", editor=None, copies=[])
        busy = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=True, value="", editor=None, copies=[])
        result, submits, _ = self._run([idle] * 10 + [busy], timeout=30)
        self.assertEqual(result.get("submission"), "accepted", result)
        self.assertIs(result.get("sent"), True)
        self.assertEqual(len(submits), 1)

    def test_busy_counts_as_accepted(self):
        """有生成迹象（停止键在）就算发出去了。"""
        idle = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=False, value="", editor=None, copies=[])
        busy = SimpleNamespace(root=_root(), url="https://gemini.google.com/app",
                               busy=True, value="", editor=None, copies=[])
        result, submits, _ = self._run([idle, busy], timeout=30)
        self.assertEqual(result.get("submission"), "accepted", result)
        self.assertEqual(len(submits), 1)


class NewChatLandingTests(unittest.TestCase):
    class FakeScreen:
        target_hwnd = 1

    def _run(self, url, tree_error=False, clicks=None):
        screen = self.FakeScreen()
        clicks = clicks if clicks is not None else []

        calls = {"n": 0}

        def tree(_control):
            calls["n"] += 1
            # 第一次是"点之前找按钮"，必须正常；之后（落点校验）才模拟读不出来
            if tree_error and calls["n"] >= 2:
                raise RuntimeError("树读不出来")
            return _root()

        with mock.patch.object(gemini, "_ScreenClass", self.FakeScreen), \
                mock.patch.object(gemini.ui, "document", return_value=(None, url)), \
                mock.patch.object(gemini.ui, "tree", side_effect=tree), \
                mock.patch.object(gemini, "new_chat_button", return_value=[Node("ButtonControl", "发起新对话")]), \
                mock.patch.object(gemini.ui, "click", side_effect=lambda *a: clicks.append(1)), \
                mock.patch.object(gemini.time, "sleep"):
            return gemini.open_new_chat(screen, SITE, tries=1, pause=0.0), clicks

    def test_new_chat_verified(self):
        result, clicks = self._run("https://gemini.google.com/app")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("verified"))
        self.assertEqual(len(clicks), 1)

    def test_new_chat_not_landed_retries_then_fails(self):
        result, clicks = self._run("https://gemini.google.com/app/abc123")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "newchat_not_landed")
        self.assertEqual(len(clicks), 2, "应当再点一次")

    def test_unreadable_tree_keeps_ok(self):
        """读不出树 ≠ 没落地：保持 ok，只标 verified=False。"""
        result, clicks = self._run("https://gemini.google.com/app", tree_error=True)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result.get("verified"))
        self.assertEqual(len(clicks), 1)


if __name__ == "__main__":
    unittest.main()
