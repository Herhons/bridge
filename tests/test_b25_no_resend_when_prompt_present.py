"""B25：提问已经出现在页面上，就不许再点一次发送。

现场（四端会审 2026-11-11，ChatGPT 第 3 条）：`_maybe_retry` 的判据只有
「重试过没 / 在 5–10s 窗口内 / 输入框非空」，不看提问是否已经渲染出来。
而它的第二个调用点恰好落在「`len(prompts) > sent_prompts`（提问已出现）、
但 `accepted` 还没置真」的分支里 —— opencode 每轮结束会把提问回填输入框，
短回答（<10s）正好落进窗口，于是同一提问被发两遍。

判据补充：**提问在控件树里 = 这一下确实发出去了**，比"框非空"更硬。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui                # noqa: E402

PROMPT = "这是一条会被回填的提问"


def _box():
    return SimpleNamespace(left=0, top=0, right=100, bottom=20)


def _node(kind="GroupControl", name="", children=()):
    item = ui.Node(kind, name, SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                               BoundingRectangle=_box()))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view(value="", *, with_prompt=False):
    kids = [_node("TextControl", PROMPT)] if with_prompt else []
    return SimpleNamespace(root=_node("DocumentControl", "", kids), value=value,
                           url="https://chatgpt.com/c/1", busy=False, copies=[],
                           editor=None)


EMPTY = view("")
WRITTEN = view(PROMPT)


class FakeConv:
    site = "chatgpt"
    settle = 0
    screen = None

    def __init__(self, tail):
        self._tail = tail
        self._n = 0
        self.submits = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        self._n += 1
        if self._n == 1:
            return EMPTY          # 预检：框空、无提问
        if self._n == 2:
            return WRITTEN        # 写完之后读回
        return self._tail()

    def write(self, before, prompt):
        pass

    def submit(self, v):
        self.submits += 1

    def clear(self, v):
        pass

    def copy(self, n):
        return "回复"


class PromptAlreadyPresentTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("PRE_SUBMIT_PROBE", 0.0), ("SUBMIT_RETRY_TOTAL", 10.0)):
            patcher = unittest.mock.patch.object(conversation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_never_resends_when_prompt_is_already_on_the_page(self):
        """框被回填、但提问已经渲染出来 —— 这就是发出去了，绝不能再点。"""
        conv = FakeConv(lambda: view(PROMPT, with_prompt=True))
        conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 1, "提问已在树上还点了第二次")

    def test_still_retries_when_prompt_is_absent(self):
        """框里有字、提问确实没出现 —— 这是真没发出去，重试一次是应该的。"""
        conv = FakeConv(lambda: view(PROMPT))
        result = conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 2)
        self.assertTrue(result.get("submit_retried"))


if __name__ == "__main__":
    unittest.main()
