"""B10：opencode 的会话身份判定。

**为什么改**：`opencode_audit_20261107_1300` 那一轮，Bridge 在 13:12:52 报
`conversation_changed / session_changed` 撤走，而 opencode 自己的库里记着这轮
`succeeded`、终答复 1326 字符、13:12:43 就写完了。**答案在页面上，是判定提前放弃。**

真根因（有实测转储 `evidence/r5_identity_collect_20261107_0451/logs/live_tree_text.txt`）：
长任务滚到底时，消息正文会落进文档顶端 0~48px 的"标签带"和 48~96px 的"页头带"，
于是 `tabs` 里混进 `'——与施工方自述一致。'`、`'Ran 1159 tests / OK / EXIT=0'` 这类正文残片，
`heads != {title}` 不成立 → `proven` 从 true 掉成 false → `_session_same` 要求两边都 proven
→ 被判成"换了会话"。

**这次不是放宽 identity，是把"读不出"和"变了"分开。**
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import conversation, ui  # noqa: E402

TITLE = "oc-audit-20261107-1300 只读复核"
WINDOW = f"{TITLE} · OpenCode"


def _node(kind, name, *, left=0, top=0, right=100, bottom=20, children=()):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=True, IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(left=left, top=top, right=right, bottom=bottom)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def _view(*, tabs=(), heads=(), title=WINDOW, url="oc://renderer/index.html"):
    """构造一棵 opencode 形状的树：文档顶在这里是 y=0。"""
    kids = []
    for i, text in enumerate(tabs):
        kids.append(_node("TextControl", text, left=10 + i * 200, top=10,
                          right=180 + i * 200, bottom=30))
    for i, text in enumerate(heads):
        kids.append(_node("TextControl", text, left=10 + i * 200, top=60,
                          right=180 + i * 200, bottom=80))
    root = _node("DocumentControl", title, left=0, top=0, right=1200, bottom=800,
                 children=kids)
    return SimpleNamespace(root=root, url=url, value="", busy=False, copies=[],
                           editor=None, sends=())


class TabTextTests(unittest.TestCase):
    def test_rejects_prose_fragments(self):
        for bad in ("——与施工方自述一致。", "出错了！", "多行\n正文", "x" * 80):
            self.assertFalse(conversation._looks_like_tab(bad), bad)

    def test_accepts_real_labels(self):
        for good in ("B", TITLE, "原样回复 BR9-opencode 标识", "Ran 1159 tests / OK / EXIT=0"):
            self.assertTrue(conversation._looks_like_tab(good), good)


class SessionStampTests(unittest.TestCase):
    def test_clean_page_is_proven(self):
        stamp = conversation.session_stamp(_view(tabs=("B", TITLE), heads=(TITLE,)))
        self.assertTrue(stamp["proven"], stamp)

    def test_prose_in_the_band_no_longer_breaks_it(self):
        """正文残片混进标签带 —— 这就是 13:12:52 那次的现场。"""
        stamp = conversation.session_stamp(_view(
            tabs=("B", TITLE, "—与施工方自述一致。"),
            heads=(TITLE, "全量单测", "Ran 1159 tests / OK / EXIT=0")))
        self.assertTrue(stamp["proven"], stamp)

    def test_title_missing_from_the_header_still_fails(self):
        """标题在标签条里、页头带里看不到 —— 这是**真的**可疑，仍然 fail-closed。"""
        stamp = conversation.session_stamp(_view(tabs=("B", TITLE), heads=("别的会话",)))
        self.assertFalse(stamp["proven"])
        self.assertEqual(stamp["why"], "header_mismatch")

    def test_no_tab_strip_is_still_unproven(self):
        stamp = conversation.session_stamp(_view(tabs=(TITLE,), heads=(TITLE,)))
        self.assertFalse(stamp["proven"])
        self.assertEqual(stamp["why"], "no_tab_strip")


class IdentityBranchTests(unittest.TestCase):
    """端到端：提问消失 + 标签带读不出时，**不许**立刻判"换了会话"。"""

    class Conv:
        settle = 0
        screen = None

        def __init__(self, total=40):
            self.site = "opencode"
            self.n = 0
            self.total = total

        def empty(self, v):
            return not (v.value or "").strip()

        def read(self, attempts=1, delay=0.2):
            self.n += 1
            n = self.n
            if n == 1:                      # 预检
                return _view(tabs=("B", TITLE), heads=(TITLE,), title=WINDOW)
            if n == 2:                      # 写完读回
                v = _view(tabs=("B", TITLE), heads=(TITLE,), title=WINDOW)
                v.value = PROMPT
                return v
            if n <= 4:                      # 接受：提问在树上、标签带干净
                v = _view(tabs=("B", TITLE), heads=(TITLE,), title=WINDOW)
                v.root.children.append(_node("TextControl", PROMPT, top=200, bottom=220))
                return v
            # 之后：提问气泡被虚拟化掉，且标签带被正文污染（proven=False）
            return _view(tabs=("B", TITLE, "—与施工方自述一致。"),
                         heads=("全量测试",), title=WINDOW)

        def write(self, before, prompt):
            pass

        def submit(self, v):
            pass

        def clear(self, v):
            pass

        def copy(self, n):
            return ""

    def test_unreadable_identity_is_not_treated_as_a_session_change(self):
        from unittest import mock
        with mock.patch.object(conversation, "IDENTITY_UNREADABLE_READS", 2):
            got = conversation.ask(self.Conv(), PROMPT, timeout=6.0)
        self.assertEqual(got["stage"], "conversation_changed")
        self.assertEqual(got["reason"], "identity_unreadable",
                         "读不出必须是 identity_unreadable，不能混成 session_changed")
        self.assertNotEqual(got.get("session"), "changed")


PROMPT = "请只原样回复这一行：BR10-identity-test"


if __name__ == "__main__":
    unittest.main()
