"""B19：deepseek 的"空"判据与自留残草。

两个真机坑：
1. 粘贴失败会留下"提问的前 3 个字"，旧判据认为输入框非空 → 之后每次 draft_present；
2. 判空原本只看发送键可用态，而 deepseek 有草稿时按钮本来就深蓝，于是
   "自己留下的残草"和"用户真的写了一句话"在代码里长得一模一样。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import flow                                  # noqa: E402
from qi.conversation import ask                      # noqa: E402
from test_conversation import FakeConversation, view  # noqa: E402

PROMPT = "你是资深软件架构评审。下面是一个真实系统的统计。"


def _site(**kw):
    base = dict(name="deepseek", title_hint="Google Chrome", composer="ds_composer",
                send="ds_send_on", actions="ds_actions", copy="ds_copy",
                composer_offset=(0, 0))
    base.update(kw)
    return flow.Site(**base)


class _Node:
    def __init__(self, kind="TextControl", name=""):
        self.kind = kind
        self.name = name
        self.children = []
        self.control = SimpleNamespace(IsEnabled=True, IsOffscreen=False)

    def walk(self):
        return [self]


class AdoptDraftTests(unittest.TestCase):
    def _run(self, snapshots, adopt=True):
        class Conv(FakeConversation):
            pass

        conv = Conv(snapshots)
        conv.adopt_self_draft = adopt

        def clear(_view):
            return None

        conv.clear = clear
        clock = [0.0]
        with mock.patch("qi.conversation.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("qi.conversation.time.sleep",
                           side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)):
            result = ask(conv, PROMPT, timeout=2)
        return result, conv

    def test_self_prefix_draft_is_adopted(self):
        """输入框里是本提问的前 3 个字 → 清掉继续，不报 draft_present。"""
        result, conv = self._run([view(value=PROMPT[:3]), view()])
        self.assertNotEqual(result["stage"], "draft_present", result)
        self.assertTrue(result.get("adopted_self_draft"))
        self.assertEqual(conv.writes, [PROMPT], "清掉之后应当照常写入并发送")

    def test_user_draft_still_blocks(self):
        """用户自己写的内容不许动。"""
        result, conv = self._run([view(value="我自己写了一半的话")])
        self.assertEqual(result["stage"], "draft_present", result)
        self.assertEqual(conv.writes, [])

    def test_adopt_off_blocks_even_prefix(self):
        """没打开开关的端：前缀也按草稿处理（行为不变）。"""
        result, conv = self._run([view(value=PROMPT[:3])], adopt=False)
        self.assertEqual(result["stage"], "draft_present", result)
        self.assertEqual(conv.writes, [])


class EmptyProbeTests(unittest.TestCase):
    def _probe(self, value, **site_kw):
        screen = mock.Mock()
        site = _site(**site_kw)
        probe = flow._empty_probe(screen, site)
        fake_view = SimpleNamespace(value=value, root=_Node("DocumentControl"), editor=None)
        # 模拟"发送键看起来可用"（旧判据会因此认定输入框非空）
        with mock.patch.object(flow, "_send_enabled", return_value=True):
            return probe(fake_view)

    def test_empty_by_text_ignores_send_button(self):
        """文本为空、但发送键"可用" → 仍然判空（这正是卡死后的状态）。"""
        self.assertTrue(self._probe("", empty_by_text=True))

    def test_non_empty_text_is_still_not_empty(self):
        self.assertFalse(self._probe("用户真的写了一句话", empty_by_text=True))

    def test_send_button_mode_unchanged_by_default(self):
        """没开 empty_by_text 的端：仍然按发送键判（旧行为）。"""
        self.assertFalse(self._probe("", empty_by_text=False))


class PlaceholderTests(unittest.TestCase):
    def test_looks_empty_accepts_site_placeholders(self):
        self.assertTrue(flow.looks_empty("问问 Gemini\n", None,
                                         placeholders=("问问 Gemini",)))
        self.assertFalse(flow.looks_empty("用户写的话", None,
                                          placeholders=("问问 Gemini",)))


if __name__ == "__main__":
    unittest.main()
