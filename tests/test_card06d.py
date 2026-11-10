"""CARD-06d：`--new` 保持回执模型 / 读回复剪贴板回退 UIA。

全部离线：不建窗口、不点桌面、不发消息、不写 runs/。卡面三条逐条对应：

    1  --new 之后模型被重置 → 按回执重选 → 再读回；仍不一致 → stage=model_reset_after_new / 5
    2  复制键后剪贴板没更新 → 改读 UIA 树（read_via=uia）；两条都失败才报错 + 写 runlog
    3  以上四条路径都有用例（重选成功 / 重选失败 / 剪贴板未更新→UIA / 两者都失败）

思路：`Conversation` 是五端共用的，本卡不许改它，所以第 2 条的退路落在
`workbuddy.ReplyConversation` 上——这里用假节点造出"回复气泡 + 动作条"，
再用假 `copy_at` 分别演"剪贴板正常"和"剪贴板没更新"。
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qi import ui  # noqa: E402
from qi import workbuddy  # noqa: E402

MODEL = "DeepSeek-V4.1-Flash"
WANT = "Hy3"
PROMPT = "ping"


# ---------------------------------------------------------------- 假控件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=(), width=100, height=20,
         left=0, top=0):
    """最小 UIA 节点：够 walk / current_model / _leaf_texts 用（矩形要真给）。"""
    item = ui.Node(kind, name, SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                               BoundingRectangle=box(width, height,
                                                                     left, top)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def text(value, left=0, top=0):
    return node("TextControl", value, width=90, height=20, left=left, top=top)


def button(name, left=0, top=0):
    return node("ButtonControl", name, width=24, height=24, left=left, top=top)


def reply_bubble(answer_lines, prompt_line=None, quip="这题我会，让我组织下语言"):
    """造一条回复，形状照 2026-11-02 真机 RawView 抄：

        [提问行]                       ← 只有爬过气泡边界时才会一起读进来
        WorkBuddy 预计消耗 0.41~18.23 已处理 1m40s      ← 头一行并排四个叶子
        <正文第 1 块>
        <正文第 2 块>
        已消耗 1.97 <转场话术> 不再显示                  ← 尾一行并排
        ToolBar[09:31, 复制, 分享]

    状态和正文**混在同一个气泡**里，而状态是"一行里并排好几个叶子"——
    这正是 `_clean_bubble` 要按行连坐的原因。返回 (根节点, 复制键)。
    """
    head = [text("WorkBuddy", top=0), text("预计消耗", left=100, top=0),
            text("0.41 ~ 18.23", left=200, top=0), text("已处理 1m40s", left=300, top=0)]
    body = [text(line, top=40 + 30 * i) for i, line in enumerate(answer_lines)]
    foot = [text("已消耗", top=200), text("1.97", left=60, top=200),
            text(quip, left=120, top=200), button("不再显示", left=400, top=200)]
    lines = list(head) + body + foot
    if prompt_line is not None:
        lines.insert(0, text(prompt_line, top=-40))
    bar = node("ToolBarControl", "", [text("09:31", top=240),
                                      button("复制", left=40, top=240),
                                      button("分享", left=70, top=240)])
    bubble = node("GroupControl", "", lines + [bar], height=300)
    wrap = node("GroupControl", "", [bubble], height=320)
    root = node("DocumentControl", "", [wrap], height=400)
    copy = bar.children[1]
    return root, copy


# ---------------------------------------------------------------- 1. 剪贴板序列号
class ClipboardSeqTests(unittest.TestCase):
    """序列号只用来留证：读得到给整数，读不到给 None（不猜）。"""

    def test_reads_sequence_number(self):
        fake = SimpleNamespace(windll=SimpleNamespace(
            user32=SimpleNamespace(GetClipboardSequenceNumber=lambda: 4321)))
        with mock.patch.object(workbuddy, "ctypes", fake):
            self.assertEqual(workbuddy.clipboard_seq(), 4321)

    def test_unreadable_returns_none(self):
        def boom():
            raise OSError("no clipboard")

        fake = SimpleNamespace(windll=SimpleNamespace(
            user32=SimpleNamespace(GetClipboardSequenceNumber=boom)))
        with mock.patch.object(workbuddy, "ctypes", fake):
            self.assertIsNone(workbuddy.clipboard_seq())


# ---------------------------------------------------------------- 2. UIA 读正文
class ReplyTextTests(unittest.TestCase):
    """从复制键往上读气泡正文：状态行剔掉、提问不算回复、读不到就 None。"""

    def test_status_lines_are_dropped(self):
        """头行四个叶子（WorkBuddy/预计消耗/数值/已处理）和尾行（已消耗/数值/转场话术）都不留。"""
        root, copy = reply_bubble(["pong"])
        self.assertEqual(workbuddy.reply_text(copy, PROMPT), "pong")

    def test_answer_line_starting_with_status_word_is_lost(self):
        """已知代价（写在 reply_text 的 docstring 里）：正文以状态词开头会被剔掉。"""
        root, copy = reply_bubble(["已处理完这批数据", "pong"])
        self.assertEqual(workbuddy.reply_text(copy, PROMPT), "pong")

    def test_per_character_nodes_are_glued_back_into_one_line(self):
        """真机：`BR9-06D-LIVE-OK` 被拆成 7 个文本节点，必须按矩形拼回一行。"""
        row = 40
        pieces = [text("BR9-06D-L", left=0, top=row)]
        x = 90
        for i, letter in enumerate("IVE-OK"):
            pieces.append(text(letter, left=x + 12 * i, top=row))
        bar = node("ToolBarControl", "", [button("复制", top=240)])
        bubble = node("GroupControl", "", [text("WorkBuddy", top=0),
                                           text("已完成 9s", top=10), *pieces, bar],
                      height=300)
        root = node("DocumentControl", "", [bubble], height=400)
        self.assertEqual(workbuddy.reply_text(bar.children[0], PROMPT),
                         "BR9-06D-LIVE-OK")

    def test_multi_line_answer_keeps_order(self):
        root, copy = reply_bubble(["第一行", "第二行", "第三行"])
        self.assertEqual(workbuddy.reply_text(copy, PROMPT),
                         "第一行\n第二行\n第三行")

    def test_prompt_bubble_is_not_an_answer(self):
        """提问气泡上也有复制键：读出来的如果就是提问，必须继续往上，不许当回答。"""
        root, copy = reply_bubble([], prompt_line=PROMPT)
        self.assertIsNone(workbuddy.reply_text(copy, PROMPT))

    def test_status_only_bubble_is_unreadable(self):
        """只剩状态行时宁可返回 None，也不能把「已消耗 1.97」交出去。"""
        root, copy = reply_bubble([])
        self.assertIsNone(workbuddy.reply_text(copy, PROMPT))

    def test_prompt_contaminated_body_is_cleaned(self):
        """爬过了气泡边界时，正文里会夹进提问：够长就按整行剔出去。"""
        long_prompt = "这是我自己写的一段足够长的提问，用来验证剔行逻辑"
        root, copy = reply_bubble(["pong"], prompt_line=long_prompt)
        self.assertEqual(workbuddy.reply_text(copy, long_prompt), "pong")

    def test_short_prompt_is_not_prefix_stripped(self):
        """短提问（< 12 字）不按"包含"剔行——否则回答里带 ping 也会被吃掉。"""
        root, copy = reply_bubble(["ping 收到，pong"])
        self.assertEqual(workbuddy.reply_text(copy, "ping"), "ping 收到，pong")

    def test_answer_equal_to_prompt_is_refused(self):
        root, copy = reply_bubble(["pong"])
        self.assertIsNone(workbuddy.reply_text(copy, "pong"))

    def test_detached_copy_button_returns_none(self):
        """没有父节点（元素已失效）时不能抛，只能返回 None。"""
        self.assertIsNone(workbuddy.reply_text(button("复制"), PROMPT))


# ---------------------------------------------------------------- 3. 复制回退
class CopyFallbackTests(unittest.TestCase):
    """剪贴板优先、UIA 兜底、两条都失败才报错；每条都记 read_via。"""

    def setUp(self):
        self.root, self.copy_node = reply_bubble(["pong"])
        self.screen = SimpleNamespace()

    def conversation(self):
        conv = workbuddy.ReplyConversation(self.screen, "workbuddy")
        conv.prompt = PROMPT
        return conv

    def test_clipboard_path_is_preferred(self):
        conv = self.conversation()
        with mock.patch.object(workbuddy.Conversation, "copy_at",
                               return_value="从剪贴板来的答复") as at:
            self.assertEqual(conv.copy(self.copy_node), "从剪贴板来的答复")
        self.assertEqual(conv.read_via, "clipboard")
        self.assertIsNone(conv.copy_note)
        self.assertEqual(at.call_count, 1)

    def test_clipboard_stale_falls_back_to_uia(self):
        conv = self.conversation()
        with mock.patch.object(workbuddy.Conversation, "copy_at",
                               side_effect=RuntimeError("复制回复后剪贴板未更新")), \
             mock.patch.object(workbuddy, "clipboard_seq", side_effect=[10, 12]):
            self.assertEqual(conv.copy(self.copy_node), "pong")
        self.assertEqual(conv.read_via, "uia")
        self.assertEqual(conv.copy_note["uia"], "ok")
        self.assertEqual(conv.copy_note["seq"], [10, 12])
        self.assertIn("剪贴板未更新", conv.copy_note["clipboard"])

    def test_both_paths_failing_raises_and_says_why(self):
        conv = self.conversation()
        empty_root, copy = reply_bubble([])          # 只有状态行，UIA 也读不到
        with mock.patch.object(workbuddy.Conversation, "copy_at",
                               side_effect=RuntimeError("复制回复后剪贴板未更新")), \
             mock.patch.object(workbuddy, "clipboard_seq", side_effect=[3, 5]):
            with self.assertRaises(RuntimeError) as ctx:
                conv.copy(copy)
        message = str(ctx.exception)
        self.assertIn("剪贴板未更新", message)
        self.assertIn("UIA 也读不到", message)
        self.assertIn("3→5", message)
        self.assertIsNone(conv.read_via)
        self.assertEqual(conv.copy_note["uia"], "unreadable")

    def test_note_only_describes_the_last_attempt(self):
        """候选会试好几个：这一轮读成功了，就不能带着上一轮的失败留证。"""
        conv = self.conversation()
        conv.copy_note = {"clipboard": "上一轮的失败", "seq": [1, 2]}
        with mock.patch.object(workbuddy.Conversation, "copy_at", return_value="pong"):
            self.assertEqual(conv.copy(self.copy_node), "pong")
        self.assertIsNone(conv.copy_note)
        self.assertEqual(conv.read_via, "clipboard")

    def test_write_remembers_the_prompt(self):
        """ask() 先 write 再复制；回退时靠这个字段认领"这一轮"。"""
        conv = workbuddy.ReplyConversation(self.screen, "workbuddy")
        with mock.patch.object(workbuddy.Conversation, "write"):
            conv.write(None, "换个提问")
        self.assertEqual(conv.prompt, "换个提问")


# ---------------------------------------------------------------- 3b. 选档路径
class SelectModelTests(unittest.TestCase):
    """`--model` 点选项：同名标签要挑到"能点的那一行"（真机踩过，见 model_rows）。"""

    def setUp(self):
        self.screen = SimpleNamespace(target_hwnd=1)

    def build(self, *rows, model=MODEL):
        combo = node("ComboBoxControl", f"Select model: {model}", width=220, height=32)
        return node("DocumentControl", "", [combo, *rows], height=600)

    def run_select(self, root, wanted):
        clicks = []
        with mock.patch.object(workbuddy.ui, "document", return_value=(object(), "app://wb")), \
             mock.patch.object(workbuddy.ui, "tree", return_value=root), \
             mock.patch.object(workbuddy.ui, "click",
                               side_effect=lambda screen, item: clicks.append(item)), \
             mock.patch.object(workbuddy.time, "sleep"):
            result = workbuddy.select_model(self.screen, wanted)
        return result, clicks

    def test_list_row_wins_over_chat_text_with_the_same_name(self):
        """下拉里的选项 vs 会话正文里的同名行内芯片：只能点前者。"""
        chat_chip = node("GroupControl", "", [text("Hy3", top=600)], top=600)
        row = node("ListItemControl", "Hy3 限时免费 0.00x",
                   [text("Hy3", top=100), text("限时免费", left=60, top=100)])
        result, clicks = self.run_select(self.build(chat_chip, row), "Hy3")
        self.assertEqual(len(clicks), 2)          # 一次开下拉、一次点选项
        self.assertIs(clicks[-1], row)            # 点的是列表项那一行，不是正文
        self.assertFalse(result["ok"])            # 假控件不会真的改名：读回还是旧模型
        self.assertEqual(result["before"], MODEL)

    def test_two_rows_with_the_same_name_still_refuse_to_guess(self):
        """真的有两行同名（结构变了）：宁可报错，不瞎点一个。"""
        one = node("ListItemControl", "Hy3", [text("Hy3", top=100)])
        two = node("ListItemControl", "Hy3", [text("Hy3", top=160)], top=160)
        with self.assertRaises(RuntimeError) as ctx:
            self.run_select(self.build(one, two), "Hy3")
        self.assertIn("2 个控件", str(ctx.exception))

    def test_same_row_duplicate_labels_collapse(self):
        row = node("ListItemControl", "Hy3",
                   [text("Hy3", top=100), text("Hy3", left=40, top=100)])
        result, clicks = self.run_select(self.build(row), "Hy3")
        self.assertIs(clicks[-1], row)


# ---------------------------------------------------------------- 4. 模型按回执复位
class RestoreAfterNewTests(unittest.TestCase):
    """第 1 条的核心：读回、重选、再读回；仍不一致就 fail-closed。"""

    def setUp(self):
        self.screen = SimpleNamespace(target_hwnd=1)

    def run_restore(self, *, current, want, select=None, receipt=True):
        root = node("DocumentControl", "", [
            node("ComboBoxControl", f"Select model: {current}", width=220, height=32)])
        with mock.patch.object(workbuddy, "receipt_model",
                               return_value=want if receipt else None), \
             mock.patch.object(workbuddy.ui, "document", return_value=(object(), "app://wb")), \
             mock.patch.object(workbuddy.ui, "tree", return_value=root), \
             mock.patch.object(workbuddy, "select_model",
                               side_effect=select) as select_model:
            info, failure = workbuddy.restore_after_new(self.screen, "test")
        return info, failure, select_model

    def test_same_model_needs_no_reselect(self):
        info, failure, select_model = self.run_restore(current=WANT, want=WANT)
        self.assertIsNone(failure)
        self.assertEqual(info, {"model": WANT, "before": WANT, "after": WANT,
                                "reselect": False})
        select_model.assert_not_called()

    def test_reset_is_reselected_and_read_back(self):
        def select(screen, wanted):
            # 模拟真机：点完菜单，控件名跟着变
            for item in workbuddy.controls(screen):
                if item.kind == "ComboBoxControl":
                    item.name = f"Select model: {wanted}"
            return {"ok": True, "verified": True, "after": wanted}

        info, failure, select_model = self.run_restore(current=MODEL, want=WANT,
                                                       select=select)
        self.assertIsNone(failure)
        self.assertEqual(info["before"], MODEL)
        self.assertEqual(info["after"], WANT)
        self.assertTrue(info["reselect"])
        self.assertTrue(info["selected"])
        select_model.assert_called_once()

    def test_reselect_not_taking_effect_is_fail_closed(self):
        """点了但读回来还是默认档 → stage=model_reset_after_new / 5，不发送。"""
        info, failure, select_model = self.run_restore(
            current=MODEL, want=WANT,
            select=lambda screen, wanted: {"ok": True, "verified": True, "after": wanted})
        self.assertIsNone(info)
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["stage"], "model_reset_after_new")
        self.assertEqual(failure["code"], 5)
        self.assertEqual(failure["before"], MODEL)
        self.assertEqual(failure["after"], MODEL)
        self.assertEqual(failure["submission"], "not_sent")
        self.assertFalse(failure["sent"])
        self.assertIn("workbuddy_choose", failure["hint"])

    def test_reselect_error_is_fail_closed_too(self):
        info, failure, _ = self.run_restore(
            current=MODEL, want=WANT,
            select=RuntimeError("目标模型标签: 找到 0 个控件"))
        self.assertIsNone(info)
        self.assertEqual(failure["stage"], "model_reset_after_new")
        self.assertIn("找到 0 个控件", failure["hint"])

    def test_no_receipt_leaves_it_to_the_gate(self):
        info, failure, select_model = self.run_restore(current=MODEL, want=None,
                                                       receipt=False)
        self.assertEqual((info, failure), (None, None))
        select_model.assert_not_called()

    def test_unreadable_model_defers_to_the_gate(self):
        """连当前模型都读不出来时不许抛栈：交给闸门的 reread 去拒（tier_unreadable）。"""
        with mock.patch.object(workbuddy, "receipt_model", return_value=WANT), \
             mock.patch.object(workbuddy, "controls",
                               side_effect=RuntimeError("WorkBuddy 模型控件: 找到 0 个控件")):
            info, failure = workbuddy.restore_after_new(self.screen, "test")
        self.assertEqual((info, failure), (None, None))


# ---------------------------------------------------------------- 5. 入口接线
def fake_main_tree(model=MODEL):
    return node("DocumentControl", "", [
        node("ComboBoxControl", f"Select model: {model}", width=220, height=32)])


@contextlib.contextmanager
def harness(tree, *, receipt=WANT, ask=None, gate=None):
    """把入口外面那一圈都打桩：闸门、绑窗口、控件树、新对话、发送前的档位闸。

    `ask` 不传时用一个最小替身：像真 ask 那样先 write 再 copy，好让 read_via
    / copy_note 这两个字段真的被走到（不传就是"发了一句、答了一句"）。
    """
    screen = SimpleNamespace(target_hwnd=1)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(workbuddy.guard, "guard", return_value=True), \
         mock.patch.object(workbuddy, "open_app", return_value=screen), \
         mock.patch.object(workbuddy.ui, "document", return_value=(object(), "app://wb")), \
         mock.patch.object(workbuddy.ui, "tree", return_value=tree), \
         mock.patch.object(workbuddy, "new_chat") as fresh, \
         mock.patch.object(workbuddy, "receipt_model", return_value=receipt), \
         mock.patch.object(workbuddy, "select_model") as select_model, \
         mock.patch.object(workbuddy.tier_guard, "record_choice") as recorded, \
         mock.patch.object(workbuddy.tier_guard, "gate_or_exit",
                           return_value=gate) as gate_or_exit, \
         mock.patch.object(workbuddy.Conversation, "write",
                           lambda self, view, prompt: setattr(self, "prompt", prompt)), \
         mock.patch("qi.runlog.record") as logged, \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with mock.patch.object(workbuddy, "ask", side_effect=ask) as asked:
            captured = SimpleNamespace(out=out, err=err, fresh=fresh, asked=asked,
                                       gate=gate_or_exit, select=select_model,
                                       recorded=recorded, logged=logged, tree=tree)
            yield captured


def last_json(captured):
    return json.loads(captured.out.getvalue().strip().splitlines()[-1])


def ok_ask(conversation, prompt, timeout=180.0, pre_send=None, purpose=None):
    conversation.write(None, prompt)
    return {"ok": True, "stage": "answered", "submission": "accepted", "sent": True,
            "reply": "pong"}


def reserve_and_select(tree, wanted):
    """给 select_model 用的替身：真的把控件名改掉（= 真机点完菜单的效果）。"""
    def select(screen, target):
        for item in tree.walk():
            if item.kind == "ComboBoxControl":
                item.name = f"Select model: {target}"
        return {"ok": True, "verified": True, "before": wanted, "after": target}
    return select


class EntryTests(unittest.TestCase):
    """main() 的接线：什么时候重选、什么时候退 5、什么时候不发送。"""

    def test_new_reselects_and_reports_it(self):
        tree = fake_main_tree()
        with harness(tree, ask=ok_ask) as cap:
            cap.select.side_effect = reserve_and_select(tree, MODEL)
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"],
                                  sending=True)
        self.assertEqual(code, 0)
        data = last_json(cap)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model_after_new"]["after"], WANT)
        self.assertTrue(data["model_after_new"]["reselect"])
        cap.gate.assert_called_once()
        cap.asked.assert_called_once()

    def test_new_reselect_failure_exits_5_without_sending(self):
        tree = fake_main_tree()
        with harness(tree, ask=ok_ask) as cap:
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"], sending=True)
        self.assertEqual(code, 5)
        data = last_json(cap)
        self.assertEqual(data["stage"], "model_reset_after_new")
        self.assertEqual(data["code"], 5)
        self.assertFalse(data["sent"])
        cap.asked.assert_not_called()     # 没发送
        cap.gate.assert_not_called()      # 也没消费回执

    def test_new_keeps_model_when_not_reset(self):
        tree = fake_main_tree(model=WANT)
        with harness(tree, ask=ok_ask) as cap:
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"], sending=True)
        self.assertEqual(code, 0)
        cap.select.assert_not_called()
        self.assertNotIn("model_after_new", last_json(cap))

    def test_explicit_model_is_not_overridden(self):
        """给了 --model 就不插手：那是调用方明确点的档，马上会写新回执。

        `select_model` 只被 --model 那条路调用一次（restore 一次都没走）。
        """
        tree = fake_main_tree()
        with harness(tree, ask=ok_ask) as cap:
            cap.select.return_value = {"ok": True, "verified": True,
                                       "before": MODEL, "after": WANT}
            code = workbuddy.main([PROMPT, "--new", "--model", WANT, "--purpose", "test"],
                                  sending=True)
        self.assertEqual(code, 0)
        self.assertEqual(cap.select.call_count, 1)
        self.assertEqual(cap.select.call_args.args[1], WANT)
        cap.recorded.assert_called_once()
        self.assertNotIn("model_after_new", last_json(cap))

    def test_continue_does_not_touch_the_model(self):
        tree = fake_main_tree()
        with harness(tree, ask=ok_ask) as cap:
            code = workbuddy.main([PROMPT, "--continue", "--purpose", "test"], sending=True)
        self.assertEqual(code, 0)
        cap.fresh.assert_not_called()
        cap.select.assert_not_called()

    def test_list_never_reselects(self):
        tree = fake_main_tree()
        with harness(tree) as cap:
            code = workbuddy.main(["--list"], sending=True)
        self.assertEqual(code, 0)
        self.assertEqual(last_json(cap)["model"], MODEL)
        cap.fresh.assert_not_called()
        cap.select.assert_not_called()

    def test_missing_receipt_does_not_block_the_gate(self):
        tree = fake_main_tree()
        with harness(tree, receipt=None, ask=ok_ask) as cap:
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"], sending=True)
        self.assertEqual(code, 0)
        cap.select.assert_not_called()    # 没有回执就不重选
        cap.gate.assert_called_once()     # 闸门照旧去报 receipt_missing

    def test_uia_fallback_is_marked_in_output(self):
        """第 2 条接线：剪贴板没更新时 read_via=uia，并且真的用树里的正文当回复。"""
        tree = fake_main_tree(model=WANT)
        root, copy_node = reply_bubble(["pong"])

        def ask(conversation, prompt, timeout=180.0, pre_send=None, purpose=None):
            conversation.write(None, prompt)
            reply = conversation.copy(copy_node)
            return {"ok": True, "stage": "answered", "submission": "accepted",
                    "sent": True, "reply": reply}

        with harness(tree, ask=ask) as cap, \
             mock.patch.object(workbuddy.Conversation, "copy_at",
                               side_effect=RuntimeError("复制回复后剪贴板未更新")), \
             mock.patch.object(workbuddy, "clipboard_seq", side_effect=[7, 9]):
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"], sending=True)
        self.assertEqual(code, 0)
        data = last_json(cap)
        self.assertEqual(data["reply"], "pong")
        self.assertEqual(data["read_via"], "uia")
        self.assertEqual(data["copy_note"]["uia"], "ok")

    def test_both_paths_failing_exits_1_and_writes_runlog(self):
        """第 2 条兜底：两条都失败 → interrupted / 退出码 1 / runlog 有错因。"""
        tree = fake_main_tree(model=WANT)
        root, copy_node = reply_bubble([])          # 只有状态行

        def ask(conversation, prompt, timeout=180.0, pre_send=None, purpose=None):
            conversation.write(None, prompt)
            try:
                conversation.copy(copy_node)
            except RuntimeError as exc:             # 真 ask 也是这么收的口
                return {"ok": False, "stage": "interrupted", "submission": "accepted",
                        "sent": True, "hint": str(exc)}
            raise AssertionError("两条路都失败时不该成功")

        with harness(tree, ask=ask) as cap, \
             mock.patch.object(workbuddy.Conversation, "copy_at",
                               side_effect=RuntimeError("复制回复后剪贴板未更新")), \
             mock.patch.object(workbuddy, "clipboard_seq", side_effect=[11, 13]):
            code = workbuddy.main([PROMPT, "--new", "--purpose", "test"], sending=True)
        self.assertEqual(code, 1)
        data = last_json(cap)
        self.assertEqual(data["stage"], "interrupted")
        self.assertIn("UIA 也读不到", data["hint"])
        cap.logged.assert_called_once()
        logged = cap.logged.call_args.args[1]
        self.assertIn("UIA 也读不到", logged["hint"])


if __name__ == "__main__":
    unittest.main()
