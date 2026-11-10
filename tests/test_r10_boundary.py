"""R10：WorkBuddy 回答的 **START/END 边界必须由结构给出**（不是按字符串剔）。

Hy4 R9 验收 §C.1/C.2 的两个反例（都已在未改源码上复现）：

  X3  本轮 nonce 之后**再追加一份上一轮长报告**（文档序里排在回答气泡之后）：
      旧实现取"提问之后所有兄弟的叶子"，只约束了 START、没有 END，
      于是 full reply = nonce + 旧报告（exact_equal_nonce=false）。
  X1  回答之后有一行**裸模型名**、又读不到 "Select model:" 控件：
      旧实现靠 `_is_model_marker` 的字符串判据剔，剔不掉就留在答案里。

R10 的判据：回答在 UIA 里是**一个消息容器**（有子节点）。紧邻提问气泡之后的
那一个容器，它的子树范围同时给出 START 和 END；容器之后的东西（追加的历史、
裸模型名、下一轮）不再进答案。裸文本节点没有子树范围 → END 不可证 → fail-closed，
绝不回落到"整页/之后全部文本"。

同时钉住"不要为了干净而砍正文"：
  · 回答正文里出现模型名（哪怕单独一行）、出现上一轮报告的字样 → **原样保留**；
  · 回答正文中间引用本提问 → **不截断**；只有气泡开头回显本提问时才切这个前缀。

全离线：假控件树 + 假 Conversation，不碰真窗口、不截图、不发送。
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

from qi import ui, workbuddy as wb  # noqa: E402

NONCE = "BRIDGE-R10-BOUNDARY-20261107-0740-H4"
PROMPT = f"R10 structural boundary test. Reply ONLY exactly: {NONCE}"
PRIOR = "R10-PRIOR-BUILDER-REPORT\n" + ("old paragraph line that must never be collected. " * 20)
MARK = "模型已切换到 DeepSeek-V4.1-Flash"
BARE_MODEL = "DeepSeek-V4.1-Flash"
MODEL_COMBO = "Select model: DeepSeek-V4.1-Flash"
URL_A = "https://gemini.google.com/app/r10sessA"
URL_B = "https://gemini.google.com/app/r10sessB"
SITE = "workbuddy"


# ---------------------------------------------------------------- 假控件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def nd(kind, name="", *children, top=0, height=20, rid=None):
    control = SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                              BoundingRectangle=box(100, height, 0, top))
    if rid is not None:
        control.GetRuntimeId = lambda: [rid]
    item = ui.Node(kind, name, control, children=list(children))
    for child in item.children:
        child.parent = item
    return item


def txt(value, top=0):
    return nd("TextControl", value, top=top, height=20)


def copy_bar(top=0):
    return nd("ToolBarControl", "", nd("ButtonControl", "复制", top=top, height=24),
              top=top, height=30)


def prompt_bubble(prompt=PROMPT, top=200):
    return nd("GroupControl", "", txt(prompt, top=top), copy_bar(top=top + 40),
              top=top, height=70)


def answer_bubble(*lines, top=300, with_copy=False):
    bits = [txt(line, top=top + 30 * i) for i, line in enumerate(lines)]
    if with_copy:
        bits.append(copy_bar(top=top + 30 * len(lines)))
    return nd("GroupControl", "", *bits, top=top, height=30 * (len(lines) + 1))


def document(*children, rid=None):
    return nd("DocumentControl", "", *children, top=0, height=900, rid=rid)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WbConv:
    """按顺序吐同一棵树；复制走剪贴板替身。"""

    def __init__(self, root, copy_value=NONCE, copy_error=None, url=URL_A):
        self.root = root
        self.prompt = PROMPT
        self.copy_value = copy_value
        self.copy_error = copy_error
        self.read_via = None
        self.copy_note = None
        self.copy_calls = 0
        self.site = SITE
        self.screen = None
        self.url = url

    def read(self, attempts=4, delay=0.4):
        return SimpleNamespace(root=self.root, busy=False, value="", url=self.url,
                               editor=None, copies=[], site=self.site)

    def copy(self, node_):
        self.copy_calls += 1
        if self.copy_error is not None:
            raise self.copy_error
        self.read_via = "clipboard"
        return self.copy_value


def only_copy(root):
    copies = wb.reply_copies(list(root.walk()))
    return copies[0]


# ================================================================ 1. 结构边界
class AnswerBoundaryProvenance(unittest.TestCase):
    def test_x3_appended_prior_report_after_answer_is_not_collected(self):
        """Hy4 X3：回答气泡之后再追加一份旧报告 → 仍只交本轮 nonce。"""
        root = document(txt(PRIOR, top=0), txt(MARK, top=100), prompt_bubble(top=200),
                        answer_bubble(NONCE, top=300), txt(PRIOR, top=400))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)

    def test_x1_bare_model_name_line_after_answer_is_not_collected(self):
        """Hy4 X1：回答之后有一行裸模型名、读不到 Select model 控件 → 不并入答案。"""
        root = document(txt(MARK, top=0), prompt_bubble(top=200),
                        answer_bubble(NONCE, top=300), txt(BARE_MODEL, top=400))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)

    def test_content_before_and_after_is_excluded_together(self):
        """前有旧报告、后有旧报告+裸模型名：两头都不进答案。"""
        root = document(txt(PRIOR, top=0), txt(MARK, top=100), prompt_bubble(top=200),
                        answer_bubble(NONCE, top=300),
                        txt(PRIOR, top=400), txt(BARE_MODEL, top=500))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)

    def test_second_turn_after_the_answer_is_not_collected(self):
        """回答之后还有下一轮问答：只交紧邻提问的那一个气泡。"""
        other = "R10-SECOND-TURN-PROMPT"
        root = document(prompt_bubble(top=0), answer_bubble(NONCE, top=100),
                        prompt_bubble(prompt=other, top=200),
                        answer_bubble("R10-SECOND-TURN-ANSWER", top=300))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)

    def test_loose_text_after_prompt_without_a_bubble_fails_closed(self):
        """提问之后只有裸文本节点（没有消息容器）：END 不可证 → None，不回落整页。"""
        root = document(prompt_bubble(top=0), txt(NONCE, top=100))
        self.assertIsNone(wb.reply_text(only_copy(root), PROMPT))

    def test_answer_bubble_with_its_own_copy_bar_is_still_bounded(self):
        root = document(prompt_bubble(top=0),
                        answer_bubble(NONCE, top=100, with_copy=True),
                        txt(PRIOR, top=200))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)


# ================================================================ 2. 不砍正文
class ValidContentIsPreserved(unittest.TestCase):
    def test_model_name_and_prior_wording_inside_answer_are_preserved(self):
        """正文里出现模型名（单独一行）和上一轮报告字样：原样保留。"""
        lines = [BARE_MODEL, "R10-PRIOR-BUILDER-REPORT", "wording quoted by the answer",
                 NONCE]
        root = document(prompt_bubble(top=0), answer_bubble(*lines, top=100),
                        nd("ComboBoxControl", MODEL_COMBO, top=400))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), "\n".join(lines))

    def test_answer_quoting_the_prompt_midway_is_not_truncated(self):
        """回答中间引用本提问：不许在引用处截断（valid content 要保留）。"""
        lines = ["first line of the answer", PROMPT, NONCE]
        root = document(prompt_bubble(top=0), answer_bubble(*lines, top=100))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), "\n".join(lines))

    def test_prompt_echo_at_the_start_of_the_answer_bubble_is_dropped(self):
        """气泡开头回显本提问（Hy4 X4）：切掉这个前缀，正文照样交出来。"""
        root = document(prompt_bubble(top=0), answer_bubble(PROMPT, NONCE, top=100))
        self.assertEqual(wb.reply_text(only_copy(root), PROMPT), NONCE)


# ================================================================ 3. 端到端三条读法
class ReadPathsStayExact(unittest.TestCase):
    def test_last_reply_is_exact(self):
        root = document(txt(PRIOR, top=0), prompt_bubble(top=100),
                        answer_bubble(NONCE, top=200), txt(PRIOR, top=300),
                        txt(BARE_MODEL, top=400))
        nodes = list(root.walk())
        at = wb.prompt_at(nodes, PROMPT)
        got, _node = wb.last_reply(nodes, PROMPT, at)
        self.assertEqual(got, NONCE)

    def test_wait_reply_is_exact(self):
        root = document(txt(PRIOR, top=0), prompt_bubble(top=100),
                        answer_bubble(NONCE, top=200), txt(PRIOR, top=300))
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(WbConv(root), PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["reply"], NONCE)

    def test_stale_clipboard_falls_back_to_bounded_uia_text(self):
        root = document(txt(PRIOR, top=0), prompt_bubble(top=100),
                        answer_bubble(NONCE, top=200), txt(PRIOR, top=300))
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(WbConv(root, copy_value=PRIOR), PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["reply"], NONCE)
        self.assertEqual(got["read_via"], "uia")

    def test_final_read_is_exact(self):
        root = document(txt(PRIOR, top=0), prompt_bubble(top=100),
                        answer_bubble(NONCE, top=200), txt(PRIOR, top=300))
        got = wb.final_read(WbConv(root), PROMPT,
                            {"ok": False, "submission": "unknown", "sent": None})
        self.assertIsNotNone(got)
        self.assertEqual(got["reply"], NONCE)

    def test_answer_quoting_the_prompt_is_still_delivered_exact(self):
        """回答正文引用了本提问：锚点仍认提问气泡，答案照样只交本轮正文。"""
        root = document(prompt_bubble(top=0), answer_bubble(PROMPT, NONCE, top=100))
        nodes = list(root.walk())
        at = wb.prompt_at(nodes, PROMPT)
        got, _node = wb.last_reply(nodes, PROMPT, at)
        self.assertEqual(got, NONCE)
        with mock.patch.object(wb, "time", Clock()):
            waited = wb.wait_reply(WbConv(root), PROMPT, 2.0)
        self.assertTrue(waited["ok"], waited)
        self.assertEqual(waited["reply"], NONCE)


# ================================================================ 4. 跨会话同文
class ForeignSameTextIsRefused(unittest.TestCase):
    def test_same_prompt_and_answer_in_another_document_is_refused(self):
        """同一条提问 + 同一个回答文本，出现在另一个文档：不认（归属证据是文档身份）。"""
        seed = document(prompt_bubble(top=0), answer_bubble(NONCE, top=100), rid=101)
        baseline = wb.reply_baseline(SimpleNamespace(root=seed, url=URL_A), PROMPT)
        baseline["accepted_scope"] = baseline["scope"]

        foreign = document(prompt_bubble(top=0), answer_bubble(NONCE, top=100), rid=202)
        view = SimpleNamespace(root=foreign, busy=False, value="", url=URL_A,
                               copies=[], editor=None)
        text_out, _node, why = wb.attributed_reply(
            view, PROMPT, baseline, baseline.get("scope"))
        self.assertIsNone(text_out, f"另一个文档里的同文回答不许认领（{why}）")

    def test_same_document_positive_still_returns_the_answer(self):
        seed = document(prompt_bubble(top=0), answer_bubble(NONCE, top=100), rid=303)
        baseline = wb.reply_baseline(SimpleNamespace(root=seed, url=URL_B), PROMPT)
        baseline["accepted_scope"] = baseline["scope"]
        view = SimpleNamespace(root=seed, busy=False, value="", url=URL_B,
                               copies=[], editor=None)
        text_out, _node, why = wb.attributed_reply(
            view, PROMPT, baseline, baseline.get("scope"))
        self.assertEqual(text_out, NONCE, why)


# ================================================================ 5. 长答案完整性
LONG_PATH = (ROOT / "evidence" / "r10_reply_boundary_20261107_0732"
             / "LONG_EXPECTED.txt")


class LongAnswerIntegrity(unittest.TestCase):
    """Owner 07:35 附条：只验短 nonce 不够，长答案必须整段逐字回来。

    期望文本就是卡片目录里的 LONG_EXPECTED.txt（30 个编号段、真模型名、分隔符）。
    断言是**全串相等**（不是 contains / count）：少一段、乱序、被截断、混进旧报告
    都算失败。前后各放一份旧报告 + 裸模型名，确认边界两头都挡住。
    """

    def expected(self):
        self.assertTrue(LONG_PATH.exists(), f"缺少长答案期望文本：{LONG_PATH}")
        return LONG_PATH.read_text(encoding="utf-8").rstrip("\n")

    def long_tree(self):
        expected = self.expected()
        lines = expected.split("\n")
        after_top = 300 + 30 * len(lines)
        return document(txt(PRIOR, top=0), txt(MARK, top=100),
                        prompt_bubble(top=200),
                        answer_bubble(*lines, top=300),
                        txt(PRIOR, top=after_top),
                        txt(BARE_MODEL, top=after_top + 30)), expected

    def test_full_long_answer_is_returned_verbatim(self):
        root, expected = self.long_tree()
        got = wb.reply_text(only_copy(root), PROMPT)
        self.assertEqual(got, expected)
        self.assertEqual(got.count("SECTION-"), 60)          # 30 段 × BEGIN/END
        self.assertIn("MODELS-30|Hy4; DeepSeek-V4.1-Flash; Gemini Argon; Claude Opus 5.5.",
                      got)
        self.assertTrue(got.startswith("BRIDGE-R10-LONG-START-20261107-A1"))
        self.assertTrue(got.endswith("BRIDGE-R10-LONG-END-20261107-C3"))

    def test_full_long_answer_survives_the_wait_path(self):
        root, expected = self.long_tree()
        with mock.patch.object(wb, "time", Clock()):
            got = wb.wait_reply(WbConv(root, copy_value=PRIOR), PROMPT, 2.0)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["reply"], expected)
        self.assertEqual(got["read_via"], "uia")


if __name__ == "__main__":
    unittest.main()

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/r10_reply_boundary_20261107_0732/LONG_EXPECTED.txt',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
