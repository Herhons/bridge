"""B25：chatgpt 的 UIA 兜底不许跨会话提前交卷。

现场（四端会审 2026-11-11，ChatGPT 第 2 条）：`conversation.ask` 里的
`watch(view)` 排在身份判定之前，而 `flow._watch_for` 只看「回复动作条变多 +
正文两次相同」，不看 URL。页面被切到另一个对话时，会把别人的回答当本轮结果，
还标成 `submitted/accepted`。

判据：提前交卷只认**发车时那个会话 id**；拿不到 id 就不提前，退回正常等待。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui                # noqa: E402

PROMPT = "这条提问"


def _node(kind="GroupControl", name="", children=()):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=20)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def view(url, *, copies=0, value=""):
    buttons = [_node("ButtonControl", "复制") for _ in range(copies)]
    return SimpleNamespace(root=_node("DocumentControl", "", buttons), value=value,
                           url=url, busy=False, copies=buttons, editor=None)


class FakeConv:
    site = "chatgpt"
    settle = 0
    screen = None

    def __init__(self, start_url, tail_url, copies=3):
        self._start = start_url
        self._tail = view(tail_url, copies=copies)
        self._n = 0

    def empty(self, v):
        return not (v.value or "").strip()

    def read(self, attempts=1, delay=0.2):
        self._n += 1
        if self._n == 1:
            return view(self._start)                     # 预检：框空
        if self._n == 2:
            return view(self._start, value=PROMPT)       # 写完之后读回：框里有字
        return self._tail

    def write(self, before, prompt):
        pass

    def submit(self, v):
        pass

    def clear(self, v):
        pass

    def copy(self, n):
        return "回复"


def early_watch(v):
    """模拟 flow._watch_for：只看"动作条变多"就交卷。"""
    return {"ok": True, "stage": "answered", "early": True} if len(v.copies) > 1 else None


class WatchIdentityTests(unittest.TestCase):
    def test_does_not_submit_early_when_session_changed(self):
        conv = FakeConv("https://chatgpt.com/c/aaa", "https://chatgpt.com/c/bbb")
        result = conversation.ask(conv, PROMPT, timeout=0.6, watch=early_watch)
        self.assertNotEqual(result.get("stage"), "answered", "切到别的会话也提前交卷了")
        self.assertFalse(result.get("early"))

    def test_still_submits_early_in_the_same_session(self):
        conv = FakeConv("https://chatgpt.com/c/aaa", "https://chatgpt.com/c/aaa")
        result = conversation.ask(conv, PROMPT, timeout=0.6, watch=early_watch)
        self.assertEqual(result.get("stage"), "answered")
        self.assertTrue(result.get("early"))

    def test_no_session_id_means_no_early_exit(self):
        """URL 取不到会话 id（例如首页）时不提前交卷 —— 宁可慢 1–2 秒。"""
        conv = FakeConv("https://chatgpt.com/", "https://chatgpt.com/")
        result = conversation.ask(conv, PROMPT, timeout=0.6, watch=early_watch)
        self.assertNotEqual(result.get("stage"), "answered")


if __name__ == "__main__":
    unittest.main()
