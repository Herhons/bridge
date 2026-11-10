"""B26：workbuddy 接账本 —— 单一 reserve 归属权。

红因不是 R7 语义冲突，而是**双重 reserve**：`_last_gate` 已经在门禁里落过账，
`_Ledger.reserve` 又读一次 `pending()`，把本轮刚写的 submitting 当成"上一次"，
把合法首发自己拦掉。这里让门禁回报 `reserved=True`，账本改为 `adopt()`。
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
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, submissions, tier_guard, ui, workbuddy   # noqa: E402

PROMPT = "B26 workbuddy 账本"


def _node(kind="GroupControl", name="", children=()):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=20)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view(*, prompt=False, reply=False, value=""):
    kids, copies = [], []
    if prompt:
        kids.append(_node("TextControl", PROMPT))
    if reply:
        btn = _node("ButtonControl", "复制")
        kids.append(btn)
        copies.append(btn)
    return SimpleNamespace(root=_node("DocumentControl", "", kids), value=value,
                           url="app://fixture", busy=False, copies=copies, editor=None)


class SeqConv:
    site = "workbuddy"
    settle = 0
    screen = None

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0
        self.submits = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v

    def write(self, before, prompt):
        pass

    def submit(self, v):
        self.submits += 1

    def clear(self, v):
        pass

    def copy(self, n):
        return "回复"


class SingleReserveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "runs" / "submissions").mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(tier_guard, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(conversation.time, "sleep", return_value=None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def seq(self):
        return [view(), view(value=PROMPT), view(prompt=True)] + [view(prompt=True, reply=True)] * 3

    def test_door_reserved_skips_the_second_reserve(self):
        def door():
            submissions.reserve("workbuddy", PROMPT, root=self.root)
            return {"reserved": True}

        conv = SeqConv(self.seq())
        result = conversation.ask(conv, PROMPT, timeout=4.0, purpose="test", pre_send=door)

        self.assertEqual(conv.submits, 1, "首发被自己的账本拦掉了")
        self.assertTrue(result.get("ok"), result)
        state = submissions.read_state("workbuddy", PROMPT, root=self.root)
        self.assertEqual((state or {}).get("v2_state"), submissions.STATE_ACCEPTED)

    def test_second_call_is_blocked_by_the_ledger(self):
        def door_first():
            submissions.reserve("workbuddy", PROMPT, root=self.root)
            return {"reserved": True}

        conversation.ask(SeqConv(self.seq()), PROMPT, timeout=4.0, purpose="test",
                         pre_send=door_first)

        conv2 = SeqConv(self.seq())
        result = conversation.ask(conv2, PROMPT, timeout=4.0, purpose="test",
                                  pre_send=lambda: {"reserved": False})
        self.assertEqual(result.get("reason"), "submission_pending", result)
        self.assertEqual(conv2.submits, 0, "账本拦发之后一个键都不许敲")


class WorkbuddyAskTests(unittest.TestCase):
    def _call(self, purpose):
        conv = SimpleNamespace()
        with mock.patch.object(workbuddy, "_read", return_value=SimpleNamespace()), \
                mock.patch.object(workbuddy, "reply_baseline", return_value=0), \
                mock.patch.object(workbuddy, "send_and_wait",
                                  return_value={"ok": True, "reply": "x"}) as sent:
            workbuddy.ask(conv, "问题", purpose=purpose)
        return sent

    def test_purpose_forwarded(self):
        self.assertEqual(self._call("review").call_args.kwargs.get("purpose"), "review")

    def test_no_purpose_keeps_kwargs_unchanged(self):
        self.assertNotIn("purpose", self._call(None).call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
