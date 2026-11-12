"""B19：deepseek 的写入必须"确认落地"，否则抛 WriteNotLanded 并清空重写。

现场（2026-11-09 job_review4）：2450 字提问只写进了前 3 个字（`你是资`），
`_paste_via_clipboard` 等 2.5s 没看到正文就静默返回，最后由 `input_mismatch` 顶锅，
从日志上分不清"没写进去"和"页面改了文本"。
"""
from offline_guard import ensure_offline
ensure_offline()

import itertools
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import conversation                       # noqa: E402
from qi.conversation import (                     # noqa: E402
    Conversation, WriteNotLanded, ask,
)

PROMPT = "你是资深软件架构评审。" * 40            # > PASTE_LONG_CHARS，走长文本窗口


def _clock():
    now = [0.0]

    def tick():
        return now[0]

    def sleep(dt):
        now[0] += float(dt or 0)

    return tick, sleep


def _make(verify=True):
    screen = mock.Mock()
    # 假剪贴板要能"写进去再读出来"：B30 起粘贴前会读回核对内容，
    # 固定返回旧值会让它正确地判成"内容不是我们的"。
    board = {"text": "旧剪贴板"}
    screen.clipboard.side_effect = lambda: board["text"]
    screen.set_clipboard.side_effect = lambda value: board.__setitem__("text", value)
    # 每次粘贴读两次序列号（前/后）：必须依次是 100 → 101，否则就是"剪贴板没更新"
    screen.clipboard_seq.side_effect = itertools.cycle([100, 101])
    conv = Conversation(screen, "deepseek", write_verify=verify)
    editor = SimpleNamespace(name="给 DeepSeek 发送消息", control=mock.Mock())
    view = SimpleNamespace(editor=editor, value="", root=None)
    return conv, screen, view


class PasteVerifyTests(unittest.TestCase):
    def test_never_landing_raises(self):
        """读回永远是前缀 → 抛 WriteNotLanded（旧代码这里会静默返回）。"""
        conv, screen, _ = _make()
        tick, sleep = _clock()
        with mock.patch.object(conv, "read_value", return_value="你是资"), \
                mock.patch.object(conversation.time, "monotonic", side_effect=tick), \
                mock.patch.object(conversation.time, "sleep", side_effect=sleep):
            with self.assertRaises(WriteNotLanded) as ctx:
                conv._paste_via_clipboard(PROMPT[3:])
        self.assertEqual(ctx.exception.stage, "input_not_landed")
        screen.hotkey.assert_called_once_with("ctrl", "v")

    def test_lands_on_third_poll(self):
        """第 3 次读回命中 → 不抛，且只读到第 3 次。"""
        conv, _screen, _ = _make()
        tick, sleep = _clock()
        seen = ["你是资", "你是资深软件", PROMPT]
        with mock.patch.object(conv, "read_value", side_effect=seen) as reader, \
                mock.patch.object(conversation.time, "monotonic", side_effect=tick), \
                mock.patch.object(conversation.time, "sleep", side_effect=sleep):
            conv._paste_via_clipboard(PROMPT[3:])
        self.assertEqual(reader.call_count, 3)

    def test_clipboard_restored_when_not_landed(self):
        """失败也要把用户的剪贴板还原。"""
        conv, screen, _ = _make()
        tick, sleep = _clock()
        with mock.patch.object(conv, "read_value", return_value="你是资"), \
                mock.patch.object(conversation.time, "monotonic", side_effect=tick), \
                mock.patch.object(conversation.time, "sleep", side_effect=sleep):
            with self.assertRaises(WriteNotLanded):
                conv._paste_via_clipboard(PROMPT[3:])
        self.assertEqual(screen.set_clipboard.call_args_list[-1][0][0], "旧剪贴板")

    def test_verify_off_keeps_old_quiet_behaviour(self):
        """没打开开关的端一个字节都不变：超时也照常返回。"""
        conv, _screen, _ = _make(verify=False)
        tick, sleep = _clock()
        with mock.patch.object(conv, "read_value", return_value="你是资"), \
                mock.patch.object(conv, "read",
                                  return_value=SimpleNamespace(value="你是资")), \
                mock.patch.object(conversation.time, "monotonic", side_effect=tick), \
                mock.patch.object(conversation.time, "sleep", side_effect=sleep):
            conv._paste_via_clipboard(PROMPT[3:])     # 不抛


class WriteRetryTests(unittest.TestCase):
    def test_write_clears_before_retry(self):
        """第一轮没落地 → 清空重写；不清的话前缀会叠两遍。"""
        conv, _screen, view = _make()
        calls = []

        def paste(_text, attempts=0, expect=None):
            calls.append(attempts)
            if attempts == 0:
                raise WriteNotLanded("没落地", attempts=0)

        with mock.patch.object(conversation.ui, "click"), \
                mock.patch.object(conv, "_paste_via_clipboard", side_effect=paste), \
                mock.patch.object(conv, "clear") as clearer, \
                mock.patch.object(conv, "read", return_value=view), \
                mock.patch.object(conversation.guard, "last_input_tick", return_value=1):
            conv.write(view, PROMPT)
        self.assertEqual(calls, [0, 1], "应当重试一次")
        clearer.assert_called_once()


class ChunkTests(unittest.TestCase):
    def test_long_text_is_pasted_in_chunks(self):
        """单次粘贴超过上限会被整段吞掉 → 分块粘，最后一块才用整段提问校验。"""
        conv, _screen, view = _make()
        conv.paste_chunk = 100
        calls = []
        with mock.patch.object(conversation.ui, "click"), \
                mock.patch.object(conv, "_paste_via_clipboard",
                                  side_effect=lambda t, **kw: calls.append((len(t), kw))) as _p, \
                mock.patch.object(conversation.guard, "last_input_tick", return_value=1):
            conv.write(view, "甲" * 250)
        self.assertEqual([c[0] for c in calls], [100, 100, 47])
        self.assertIsNone(calls[0][1].get("expect"), "中间块不做整段比较")
        self.assertEqual(calls[-1][1].get("expect"), "甲" * 250)

    def test_no_chunk_when_below_limit(self):
        conv, _screen, view = _make()
        conv.paste_chunk = 1000
        calls = []
        with mock.patch.object(conversation.ui, "click"), \
                mock.patch.object(conv, "_paste_via_clipboard",
                                  side_effect=lambda t, **kw: calls.append((len(t), kw))), \
                mock.patch.object(conversation.guard, "last_input_tick", return_value=1):
            conv.write(view, "乙" * 300)
        self.assertEqual(len(calls), 1)


class AskMappingTests(unittest.TestCase):
    def test_ask_maps_to_input_not_landed(self):
        """经真实 ask：写入没落地 → stage=input_not_landed，一个字都没发。"""
        from test_conversation import FakeConversation, view

        class NotLanding(FakeConversation):
            adopt_self_draft = True

            def write(self, before, prompt):
                raise WriteNotLanded("粘贴没落地：8.0s 内输入框里没有这段文字", attempts=2)

            def clear(self, _view):
                return None

        conv = NotLanding([view(), view()])
        clock = [0.0]
        with mock.patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("qi.conversation.time.sleep",
                           side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, "我的提问", timeout=2)
        self.assertEqual(result["stage"], "input_not_landed", result)
        self.assertEqual(result["submission"], "not_sent")
        self.assertEqual(conv.submits, 0)
        self.assertEqual(result.get("attempts"), 2)


if __name__ == "__main__":
    unittest.main()
