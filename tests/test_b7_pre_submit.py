"""T2-2：PRE_SUBMIT 探测期 ≤5s + 提交前受控重试（至多 1 次、总 ≤10s）。

这是独立复核 FINDINGS F3 指出的缺口：
    「PRE_SUBMIT ≤5s、仅未提交时允许重试 1 次、总耗时 ≤10s」在 src 下没有任何实现，
     且 docs/known_gaps.md 未登记。

实现刻意**不额外读一次树**：判定挂在主循环已有的"输入框还非空"分支上——
那里每轮本来就有 `view`；多插一次读会打乱假件测试的读序列，真机上也多一轮 UIA。

判据：
    · 框里还有文字（= 点了发送但没落下去）+ 提问条数没变 + 落在 5~10s 窗口
      → 允许重试 1 次
    · 框已空 / 读不到 / 超过 10s → 一律不重发（POST_SUBMIT 严禁重发）
"""
import sys
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui  # noqa: E402

PROMPT = "我的提问"


def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=()):
    item = ui.Node(kind, name, SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                               BoundingRectangle=box()))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view(value="", *, with_prompt=False, with_reply=False):
    kids = []
    copies = []
    if with_prompt:
        kids.append(node("TextControl", PROMPT))
    if with_reply:
        btn = node("ButtonControl", "复制")
        kids.append(btn)
        copies.append(btn)
    return SimpleNamespace(root=node("DocumentControl", "", kids), value=value,
                           url="https://chatgpt.com/c/1", busy=False,
                           copies=copies, editor=None)


EMPTY = view("")                                        # 空框、无提问
WRITTEN = view(PROMPT)                                  # 框里还有文字、无提问
REPLIED = view("", with_prompt=True, with_reply=True)   # 已提交且已出回复


class FakeConv:
    site = "chatgpt"
    settle = 0
    screen = None

    def __init__(self, tail):
        self._tail = tail
        self._n = 0
        self.submits = 0
        self.writes = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        self._n += 1
        if self._n == 1:
            return EMPTY          # 预检
        if self._n == 2:
            return WRITTEN        # 写完之后读回
        return self._tail()

    def write(self, before, prompt):
        self.writes += 1

    def submit(self, v):
        self.submits += 1

    def clear(self, v):
        pass

    def copy(self, n):
        return "回复正文"


class PreSubmitProbe(unittest.TestCase):
    def setUp(self):
        # 窗口放宽成 [0s, 10s]：第一轮循环就该命中重试判定，不用真等 5 秒。
        patches = [
            unittest.mock.patch.object(conversation, "PRE_SUBMIT_PROBE", 0.0),
            unittest.mock.patch.object(conversation, "SUBMIT_RETRY_TOTAL", 10.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_retries_once_when_composer_still_holds_our_text(self):
        conv = FakeConv(lambda: WRITTEN)
        result = conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 2, "有正证据（文字还在框里）时应重试 1 次")
        self.assertTrue(result.get("submit_retried"))

    def test_never_retries_when_composer_is_empty(self):
        """框已空 -> 证明不了"没提交成功" -> 绝不重发（POST_SUBMIT 严禁重发）。"""
        conv = FakeConv(lambda: EMPTY)
        result = conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 1)
        self.assertFalse(result.get("submit_retried"))

    def test_never_retries_when_reading_fails(self):
        def boom():
            raise RuntimeError("读不到树")

        conv = FakeConv(boom)
        conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 1, "读不到就不许猜，更不许重发")

    def test_at_most_one_retry(self):
        conv = FakeConv(lambda: WRITTEN)
        conversation.ask(conv, PROMPT, timeout=1.5)
        self.assertLessEqual(conv.submits, 2, "受控重试至多 1 次")

    def test_no_retry_after_the_total_budget(self):
        """窗口外（已过 10s）即使框里还有字，也不许再发。"""
        with unittest.mock.patch.object(conversation, "SUBMIT_RETRY_TOTAL", -1.0):
            conv = FakeConv(lambda: WRITTEN)
            conversation.ask(conv, PROMPT, timeout=1.0)
        self.assertEqual(conv.submits, 1, "超过总预算就当状态不明，绝不重发")

    def test_already_submitted_marks_accepted_and_never_resubmits(self):
        conv = FakeConv(lambda: REPLIED)
        result = conversation.ask(conv, PROMPT, timeout=5.0)
        self.assertEqual(result["stage"], "answered")
        self.assertEqual(result["submission"], "accepted")
        self.assertEqual(conv.submits, 1, "已经进去了就不许再点一次")
        self.assertEqual(conv.writes, 1)


if __name__ == "__main__":
    unittest.main()
