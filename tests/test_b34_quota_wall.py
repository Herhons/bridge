"""B34：用量硬墙要快速失败，不能当"还在生成"等满整档预算。

2026-11-12 真机：chatgpt 的 Work 额度用完时，页面上写着

    你暂时已用完工作用量
    升级订阅或添加额度，即可立即继续使用 Work；也可以等到用量在 明天 20:36 重置

收卷侧看不出差别 —— 没有回复键、也不 busy —— 于是白等到 `REVIEW_TIMEOUT`。
Round 4 那次 chatgpt 一席跑了 582s、0 字，就是这条（当时预算还是配置里的 480）。

这不是绕过额度：一个点击都不做，只是"当场认输"。所以判据必须严：
  · 只对明确词条的端生效（现在只有 chatgpt）；
  · **有回复键就不判**（有产出时额度提示可能是侧栏残留）；
  · 只在"还没被判接受"时收工（接受过就说明确实上屏了，改口会造成重复发送）。
"""
from offline_guard import ensure_offline
ensure_offline()

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from test_conversation import FakeConversation, node, view       # noqa: E402
from qi.conversation import ask, quota_wall                      # noqa: E402

PROMPT = "first line\nsecond line"
WALL = "你暂时已用完工作用量"


def wall_view(**kw):
    v = view(**kw)
    v.root.children.append(node("TextControl", WALL, 77))
    return v


class WallDetection(unittest.TestCase):
    def test_chatgpt_wall_is_detected(self):
        self.assertEqual(quota_wall("chatgpt", wall_view()), WALL)

    def test_reply_beats_the_wall(self):
        """有回复键就说明这一轮有产出，侧面残留的额度提示不能盖掉答案。"""
        self.assertIsNone(quota_wall("chatgpt", wall_view(reply=True)))

    def test_other_sites_ignore_it(self):
        for site in ("grok", "deepseek", "gemini", None, ""):
            self.assertIsNone(quota_wall(site, wall_view()), site)

    def test_clean_page_is_not_a_wall(self):
        self.assertIsNone(quota_wall("chatgpt", view()))


class FastFail(unittest.TestCase):
    def run_turn(self, snapshots, timeout=40):
        clock = [0.0]
        conv = FakeConversation(snapshots)
        conv.site = "chatgpt"
        # `purpose="review"` 会开提交账本，账本落在 `tier_guard.ROOT/runs/submissions`。
        # 不隔离的话：① 测试往工作区写真实记录；② 同一 PROMPT 的第二条用例会被
        # 上一条留下的 accepted 记录挡住（2026-11-12 实测，报 `interrupted`）。
        tmp = tempfile.mkdtemp(prefix="b34-ledger-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with patch("qi.tier_guard.ROOT", Path(tmp)), \
                patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                patch("qi.conversation.time.sleep",
                      side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout, purpose="review")
        return result, conv

    def test_wall_ends_the_turn_without_waiting(self):
        """一路都是额度墙：当场收工，不等到 timeout。"""
        result, conv = self.run_turn(
            [wall_view(), wall_view(value=PROMPT), wall_view()] + [wall_view()] * 999)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], "quota_exhausted")
        self.assertEqual(result["reason"], "quota_wall")
        self.assertEqual(result["submission"], "not_sent")
        self.assertFalse(result["sent"])
        self.assertEqual(conv.submits, 1, "只点了一次发送，没有重试")

    def test_wall_does_not_overwrite_an_accepted_turn(self):
        """已经接受过：照样当场收工，但**不许**改口成 not_sent，否则下次会重发。

        实测这条记录长这样：`v2_state=accepted / level=L2 / evidence.l2.basis=timeout`、
        `scope.url` 还是站点根路径 —— 额度墙把输入框清了，收卷侧看着像"发了没答"。
        保留 accepted 会让这条提问一直被拦（要人工清账），但比重复发送安全。
        """
        result, conv = self.run_turn(
            [view(), view(value=PROMPT), view(prompt=True)]
            + [wall_view(prompt=True)] * 3)
        self.assertEqual(result["stage"], "quota_exhausted", result)
        self.assertEqual(result["submission"], "accepted", "已经接受的记录不许改口")
        self.assertIn("submission_clear", result["hint"])


if __name__ == "__main__":
    unittest.main()


class MissingSendButton(unittest.TestCase):
    """额度墙把输入区整个换掉时，`Send` 控件**不存在**（不是 disabled）。

    旧代码把它报成 `RuntimeError: 可用发送按钮: 找到 0 个控件` → 外层成 `interrupted`
    → 账本记 `unknown`（BLOCKING_STATES）→ 这条提问永久发不出去，只能人工清账。
    而实际上一个字节都没发。有额度墙证据时应当归到 `send_unavailable`（可重试态）。
    """

    def _conv(self, site):
        from qi import conversation as conv_mod

        conv = conv_mod.Conversation.__new__(conv_mod.Conversation)
        conv.site = site
        conv.screen = SimpleNamespace()
        conv._input_mark = None
        return conv

    def _view(self, *buttons, wall=False):
        from qi import ui

        root = ui.Node("DocumentControl", "", SimpleNamespace(
            IsEnabled=True, IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=0, top=0, right=100, bottom=100)))
        root.children = list(buttons)
        for b in root.children:
            b.parent = root
        if wall:
            root.children.append(node("TextControl", WALL, 91))
        return SimpleNamespace(root=root, busy=False, value="", sends=(), copies=())

    def _run(self, site, page):
        from qi import conversation as conv_mod

        with patch.object(conv_mod.guard, "user_moved_since", return_value=False), \
                patch.object(conv_mod.time, "sleep"), \
                patch.object(conv_mod.Conversation, "read", return_value=page):
            self._conv(site)._submit(page)

    def test_wall_without_any_send_button_is_retryable(self):
        from qi import conversation as conv_mod

        with self.assertRaises(conv_mod.SendUnavailable) as caught:
            self._run("chatgpt", self._view(wall=True))
        self.assertEqual(caught.exception.stage, "send_unavailable")
        self.assertEqual(caught.exception.present, 0)

    def test_wall_with_a_disabled_send_button_is_retryable(self):
        """真机就是这个形态：额度墙下"发送"按钮还在，只是 disabled。

        判据必须用 `not buttons`（一个能点的都没有）而不是 `not named`。
        """
        from qi import conversation as conv_mod
        from qi import ui

        btn = ui.Node("ButtonControl", "发送", SimpleNamespace(
            IsEnabled=False, IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=0, top=0, right=10, bottom=10)))
        with self.assertRaises(conv_mod.SendUnavailable) as caught:
            self._run("chatgpt", self._view(btn, wall=True))
        self.assertEqual(caught.exception.stage, "send_unavailable")
        self.assertEqual(caught.exception.present, 1)

    def test_without_wall_evidence_keeps_the_original_error(self):
        """没有额度证据时保持原样：那可能真的是页面改版。"""
        from qi import conversation as conv_mod

        with self.assertRaises(RuntimeError) as caught:
            self._run("chatgpt", self._view())
        self.assertNotIsInstance(caught.exception, conv_mod.SendUnavailable)
        self.assertIn("找到 0 个控件", str(caught.exception))

    def test_wall_on_another_site_does_not_change_the_error(self):
        from qi import conversation as conv_mod

        with self.assertRaises(RuntimeError) as caught:
            self._run("grok", self._view(wall=True))
        self.assertNotIsInstance(caught.exception, conv_mod.SendUnavailable)
