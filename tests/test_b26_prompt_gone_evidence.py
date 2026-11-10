"""B26：`prompt_gone` 要留下现场，但判定一个字节不许改。

会审指出：这条分支返回里只有 `misses`，事后无法判断"当时页面上到底有没有
回复动作条、提问还在不在、读了几次"。这里只加只读字段 + 一帧终态心跳。
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

from qi import conversation, ui                 # noqa: E402

PROMPT = "B26 prompt_gone"


def _node(kind="GroupControl", name="", children=()):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=20)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view(*, prompt=False, reply=False, value=""):
    kids = []
    copies = []
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


class PromptGoneEvidence(unittest.TestCase):
    def test_returns_the_scene_not_just_misses(self):
        # 预检空 -> 写完读回（有字）-> 提问出现（accepted）-> 之后提问消失 6 帧
        conv = SeqConv([view(), view(value=PROMPT), view(prompt=True)] + [view()] * 6)
        with mock.patch.object(conversation.time, "sleep", return_value=None):
            result = conversation.ask(conv, PROMPT, timeout=6.0)

        self.assertEqual(result.get("reason"), "prompt_gone", result)
        for key in ("copies", "baseline_copies", "prompts", "sent_prompts",
                    "reads", "busy", "misses"):
            self.assertIn(key, result, f"缺少现场字段 {key}")
        self.assertEqual(result["copies"], 0)
        self.assertEqual(result["prompts"], 0)
        self.assertFalse(result["busy"])
        self.assertGreaterEqual(result["misses"], conversation.MISSING_STABLE_READS)

    def test_still_collects_when_the_prompt_stays(self):
        """判定没被动过：提问一直在、回复出现，就正常交卷。"""
        seq = [view(), view(value=PROMPT), view(prompt=True)] + [view(prompt=True, reply=True)] * 3
        conv = SeqConv(seq)
        with mock.patch.object(conversation.time, "sleep", return_value=None):
            result = conversation.ask(conv, PROMPT, timeout=6.0)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("stage"), "answered")


if __name__ == "__main__":
    unittest.main()
