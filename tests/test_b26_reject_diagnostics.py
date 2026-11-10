"""B26：判据没开的端也要留下"看到了什么"。

会审的抱怨是"其余 5 端只能表现为超时"。判定不改（没有实测证据就打开判据
等于猜），但**事实**要留下来 —— 否则永远攒不到能打开它的证据。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, reject, ui         # noqa: E402

PROMPT = "B26 诊断用提问"


def _node(kind="GroupControl", name="", enabled=True):
    return ui.Node(kind, name, SimpleNamespace(
        IsEnabled=enabled, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=20)))


def view(*, value="", sends=(), prompt=False):
    kids = []
    if prompt:
        kids.append(_node("TextControl", PROMPT))
    root = _node("DocumentControl", "")
    root.children = kids
    for child in kids:
        child.parent = root
    return SimpleNamespace(root=root, value=value, busy=False, copies=[],
                           url="https://example.com/c/1", sends=list(sends),
                           editor=None)


class SeqConv:
    site = "gemini"          # PROFILES 里没有 = 判据全关
    settle = 0
    screen = None

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v

    def write(self, before, prompt):
        pass

    def submit(self, v):
        pass

    def clear(self, v):
        pass

    def copy(self, n):
        return "回复"


class SignalsUnit(unittest.TestCase):
    def test_echo_back(self):
        facts = reject.observe(view(value=PROMPT), PROMPT, prompt_here=False)
        self.assertEqual(reject.signals(facts).get("echo_back"), True)

    def test_send_disabled(self):
        facts = reject.observe(view(sends=(False,)), PROMPT)
        self.assertEqual(facts.sends_total, 1)
        self.assertEqual(facts.sends_on, 0)
        self.assertTrue(reject.signals(facts).get("send_disabled"))

    def test_quiet_when_nothing_seen(self):
        facts = reject.observe(view(), PROMPT)
        self.assertEqual(reject.signals(facts), {})


class TimeoutCarriesFacts(unittest.TestCase):
    def test_timeout_includes_reject_facts(self):
        seq = [view(), view(value=PROMPT)] + [view(value=PROMPT)] * 3
        with mock.patch.object(conversation.time, "sleep", return_value=None):
            result = conversation.ask(SeqConv(seq), PROMPT, timeout=0.6)

        self.assertEqual(result.get("stage"), "timeout", result)
        self.assertTrue(result.get("reject_facts"), "超时结果必须带现场")
        self.assertTrue(result["reject_facts"].get("echo_back"))

    def test_no_facts_means_no_extra_key(self):
        seq = [view(), view(value=PROMPT)] + [view()] * 3
        with mock.patch.object(conversation.time, "sleep", return_value=None):
            result = conversation.ask(SeqConv(seq), PROMPT, timeout=0.6)
        self.assertEqual(result.get("stage"), "timeout", result)
        self.assertNotIn("reject_facts", result)


if __name__ == "__main__":
    unittest.main()
