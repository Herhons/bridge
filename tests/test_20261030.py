"""2026-10-30 批准的第3-7项的离线测试。不碰真实窗口、剪贴板、键鼠。"""

from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from qi import conversation, guard, runlog
from qi.conversation import Conversation, UserTookOver, ask
from test_conversation import FakeConversation, view

PROMPT = "first line\nsecond line"


def run(snapshots, timeout=5.0, conv=None):
    clock = [0.0]
    conv = conv or FakeConversation(snapshots)
    with patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
         patch("qi.conversation.time.sleep", side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
        return ask(conv, PROMPT, timeout), conv


class GuardInRun(unittest.TestCase):   # 第3项
    def test_moved_since_mark(self):
        with patch.object(guard, "last_input_tick", return_value=105):
            self.assertTrue(guard.user_moved_since(100))
            self.assertFalse(guard.user_moved_since(105))
            self.assertFalse(guard.user_moved_since(None))

    def test_submit_aborts_before_clicking(self):
        c = Conversation.__new__(Conversation)
        c.site, c.screen, c._input_mark = "deepseek", MagicMock(), 100
        with patch.object(guard, "last_input_tick", return_value=200):
            with self.assertRaises(UserTookOver):
                c._submit(None)
        c.screen.find.assert_not_called()
        c.screen.click.assert_not_called()

    def test_ask_reports_not_sent(self):
        conv = FakeConversation([view(), view(value=PROMPT)])
        def boom(_):
            raise UserTookOver("moved")
        conv.submit = boom
        result, _ = run(None, conv=conv)
        self.assertEqual(result["stage"], "user_took_over")
        self.assertEqual(result["submission"], "not_sent")
        self.assertFalse(result["sent"])


class RunLog(unittest.TestCase):   # 第4项
    def test_one_line_without_reply_text(self):
        with tempfile.TemporaryDirectory() as d:
            runlog.record("grok", {"ok": True, "stage": "answered", "reply": "秘密正文",
                                   "submission": "accepted", "elapsed": 3.2}, root=Path(d))
            runlog.record("dsh", {"ok": False, "stage": "timeout", "timeout_kind": "no_reply"}, root=Path(d))
            files = list(Path(d).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^\d{8}\.jsonl$")
            text = files[0].read_text(encoding="utf-8")
            self.assertNotIn("秘密正文", text)
            lines = [json.loads(x) for x in text.splitlines()]
            self.assertEqual([x["endpoint"] for x in lines], ["grok", "dsh"])
            self.assertEqual(lines[0]["result"], "answered")
            self.assertEqual(lines[1]["timeout_kind"], "no_reply")

    def test_write_failure_is_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("x")
            runlog.record("grok", {"ok": True}, root=blocker)   # 不抛异常即可


class Settle(unittest.TestCase):   # 第5项
    def test_busy_blip_restarts_window(self):
        snaps = [view(), view(value=PROMPT),
                 view(prompt=True, reply=True),            # 第一次看到
                 view(prompt=True, reply=True, busy=True),  # 又忙了：重新计时
                 view(prompt=True, reply=True)]            # 之后一直稳定
        result, conv = run(snaps)
        self.assertTrue(result["ok"])
        self.assertEqual(conv.copied, [(30,)])

    def test_candidate_shorter_than_window_is_not_copied(self):
        conv = FakeConversation([view(), view(value=PROMPT), view(prompt=True, reply=True)])
        conv.settle = 10.0
        result, conv = run(None, timeout=3.0, conv=conv)
        self.assertEqual(conv.copied, [])
        self.assertEqual(result["stage"], "timeout")

    def test_copies_only_after_settle(self):
        clock = [0.0]
        conv = FakeConversation([view(), view(value=PROMPT), view(prompt=True, reply=True)])
        copied_at = []
        orig = conv.copy
        conv.copy = lambda t: (copied_at.append(clock[0]), orig(t))[1]
        with patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
             patch("qi.conversation.time.sleep", side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, 5.0)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(copied_at[0], 1.5)


class TimeoutKind(unittest.TestCase):   # 第6项
    def test_still_generating(self):
        result, _ = run([view(), view(value=PROMPT), view(prompt=True, reply=True, busy=True)], timeout=3)
        self.assertEqual((result["stage"], result["timeout_kind"]), ("timeout", "still_generating"))

    def test_no_reply(self):
        result, _ = run([view(), view(value=PROMPT)], timeout=3)
        self.assertEqual((result["stage"], result["timeout_kind"]), ("timeout", "no_reply"))


class DeskCli(unittest.TestCase):   # 第7项
    def setUp(self):
        from qi import desk_flow
        self.df = desk_flow
        self.site = SimpleNamespace(name="dsh")

    def call(self, argv):
        with patch.object(self.df, "open_bind", return_value=object()), \
             patch.object(self.df.guard, "guard", return_value=True), \
             patch.object(self.df.runlog, "record") as rec, \
             patch.object(self.df, "ask", return_value={"ok": True, "stage": "answered"}) as a, \
             patch("builtins.print"):
            code = self.df.cli(self.site, argv)
        return code, a, rec

    def test_flag_before_prompt(self):
        code, a, rec = self.call(["--timeout", "5", "你好", "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(a.call_args.args[2], "你好")
        self.assertEqual(a.call_args.kwargs["timeout"], 5.0)
        rec.assert_called_once()

    def test_bad_timeout_rejected(self):
        for bad in (["你好", "--timeout", "abc"], ["你好", "--timeout", "0"], ["--timeout", "5"]):
            with patch("sys.stderr"):
                code, a, _ = self.call(bad)
            self.assertEqual(code, 2, bad)
            a.assert_not_called()


if __name__ == "__main__":
    unittest.main()
