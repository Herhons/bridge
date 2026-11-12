"""CARD-09f：UI 并发锁 / 输入方式 / 重发防重的离线测试。

全离线：不开窗口、不截图、不点桌面、不发消息，所有桌面动作都用假件替掉。
真机只冒烟本卡改动（证据见 evidence\\card09f_live.txt）。

覆盖卡面第 1/3/4/5/6 条：

    1 uilock        独立退出码 8、元数据带进程启动时间、持有者死亡自动回收
    3 concurrent_ui 发送前页面指纹被换 → 清草稿 + concurrent_ui（退出码 9）
    4 两进程抢锁只有一个进入（沿用 09a 的字节锁，此处验独立码 + 死亡回收）
    5 输入方式       先键入前缀再粘贴；剪贴板序列号没变 → 不粘贴报错；
                     恢复路径覆盖异常
    6 重发防重       already_answered 只读不发送；中断后重跑不重复提交；
                     高档 review/accept 等待下限 900s
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (str(SRC), str(SRC / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import conversation, exit_codes, gemini, sendgate, submissions, tier_guard, ui, uilock  # noqa: E402
from qi import workbuddy as wb  # noqa: E402
from qi.conversation import Conversation  # noqa: E402
from qi.screen import Screen  # noqa: E402
import grok_use  # noqa: E402


# ---------------------------------------------------------------- 假件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=(), width=100, height=20,
         left=0, top=0, enabled=True, offscreen=False):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=enabled, IsOffscreen=offscreen, BoundingRectangle=box(width, height, left, top)))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def text(value, left=0, top=0):
    return node("TextControl", value, width=90, height=20, left=left, top=top)


def button(name, left=0, top=0, enabled=True):
    return node("ButtonControl", name, width=24, height=24, left=left, top=top, enabled=enabled)


# ================================================================ 1. uilock
class UiLock09f(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_busy_code_is_independent(self):
        """第 1 条：锁忙退出码独立于「让位」的 4，是 8。"""
        self.assertEqual(uilock.Busy.code, exit_codes.UI_LOCK_BUSY)
        self.assertEqual(exit_codes.UI_LOCK_BUSY, 8)
        self.assertNotEqual(exit_codes.UI_LOCK_BUSY, exit_codes.BUSY)

    def test_meta_records_process_start_time(self):
        """第 1 条：元数据带 pid + 进程启动时间（防 pid 复用误判）。"""
        with uilock.hold("unit", timeout=2, root=self.root):
            meta = uilock.describe(self.root)
            self.assertEqual(meta["pid"], os.getpid())
            self.assertIn("process_start_time", meta)
            self.assertIsNotNone(meta["process_start_time"])

    def test_holder_dead_detection(self):
        """第 1 条：持有者死亡 / pid 复用，都能判出来（供死锁回收用）。"""
        with mock.patch.object(uilock, "pid_alive", return_value=False):
            self.assertTrue(uilock._holder_dead({"pid": 123, "process_start_time": 1}))
        with mock.patch.object(uilock, "pid_alive", return_value=True), \
             mock.patch.object(uilock, "process_start_time", return_value=2):
            self.assertTrue(uilock._holder_dead({"pid": 123, "process_start_time": 1}))
        with mock.patch.object(uilock, "pid_alive", return_value=None):
            self.assertFalse(uilock._holder_dead({"pid": 123, "process_start_time": 1}))


# ================================================================ 5. 输入方式
class PasteViaClipboard(unittest.TestCase):
    def make_conv(self):
        screen = mock.Mock()
        # B30：粘贴前会读回核对内容、恢复前会确认"还是我们写的那一份"，
        # 所以假剪贴板必须能"写进去再读出来"（固定返回旧值会正确地判成不匹配）。
        board = {"text": "旧剪贴板"}
        screen.clipboard.side_effect = lambda: board["text"]
        screen.set_clipboard.side_effect = lambda value: board.__setitem__("text", value)
        screen.clipboard_seq.return_value = 100
        return Conversation(screen, "test"), screen

    def test_write_types_prefix_then_pastes_rest(self):
        """第 5 条：先键入前几个字符激活编辑器，其余走剪贴板粘贴。"""
        conv, screen = self.make_conv()
        editor = node("EditControl", "")
        view = SimpleNamespace(editor=editor)
        with mock.patch.object(conversation.ui, "click") as clicker, \
             mock.patch.object(conv, "_paste_via_clipboard") as paste, \
             mock.patch.object(conversation.guard, "last_input_tick", return_value=999):
            conv.write(view, "这是很长的一段提问内容")
        screen.type_text.assert_called_once_with("这是很")
        # B19：写入现在带 attempts（`WriteNotLanded` 要报"第几次失败"）
        paste.assert_called_once_with("长的一段提问内容", attempts=0,
                                     expect="这是很长的一段提问内容")

    def test_seq_unchanged_raises_and_does_not_paste(self):
        """第 5 条：剪贴板序列号没变 → 不粘贴并报错，且恢复原剪贴板。"""
        conv, screen = self.make_conv()
        screen.clipboard_seq.side_effect = [100, 100]
        with self.assertRaises(RuntimeError):
            conv._paste_via_clipboard("要粘贴的内容")
        screen.hotkey.assert_not_called()
        screen.set_clipboard.assert_any_call("旧剪贴板")

    def test_restore_on_error(self):
        """第 5 条：粘贴中途异常，也要恢复原剪贴板。"""
        conv, screen = self.make_conv()
        screen.clipboard_seq.side_effect = [100, 101]
        screen.hotkey.side_effect = RuntimeError("hotkey boom")
        with self.assertRaises(RuntimeError):
            conv._paste_via_clipboard("内容")
        screen.set_clipboard.assert_any_call("旧剪贴板")

    def test_set_clipboard_error_does_not_touch_the_board(self):
        """写剪贴板就失败了 → 没有"我们的内容"要撤，**不许**再写一次。

        B30 起恢复是"按归属恢复"：只有当前剪贴板仍是我们写进去的那一份才写回旧值。
        写入本身失败时，剪贴板还是用户原来那份，再写一次等于多此一举、还可能覆盖
        用户在这一瞬间复制的东西。
        """
        conv, screen = self.make_conv()
        screen.clipboard_seq.side_effect = [100, 101]
        screen.set_clipboard.side_effect = [RuntimeError("write boom"), None]
        with self.assertRaises(RuntimeError):
            conv._paste_via_clipboard("内容")
        self.assertEqual(screen.set_clipboard.call_count, 1)


# ================================================================ 3/6. 发送逻辑
class SendGuard(unittest.TestCase):
    def test_existing_reply_returns_already_answered(self):
        """第 6 条：对话已有本提问之后的回复 → 只读返回，不发送。

        R8：必须同时是**本事务、同一会话**（记录里的 session id 与 URL 一致），
        才允许认领；没有证明的旧回答一律 fail-closed（见 test_r8_existing_reply）。
        """
        prompt_node = text("我的提问", top=0)
        copy_btn = button("复制", top=40)
        bubble = node("GroupControl", "", [prompt_node, copy_btn], height=100)
        tree = node("DocumentControl", "", [bubble], height=200)
        view = SimpleNamespace(root=tree, value="", url="https://chatgpt.com/c/1",
                               busy=False, copies=[copy_btn], editor=None)
        conv = mock.Mock()
        conv.site = "chatgpt"
        conv.empty = lambda v: True
        conv.read = mock.Mock(return_value=view)
        conv.copy = mock.Mock(return_value="回复正文")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)):
            submissions.reserve("chatgpt", "我的提问",
                                scope={"site": "chatgpt", "url": "https://chatgpt.com/c/1",
                                       "doc_rid": [1], "hwnd": 4242, "session_id": "1"})
            submissions.finish("chatgpt", "我的提问", {"submission": "accepted"})
            got = conversation.ask(conv, "我的提问", timeout=5)
        self.assertEqual(got["stage"], "already_answered")
        self.assertEqual(got["reply"], "回复正文")
        self.assertTrue(got["ok"])
        self.assertFalse(conv.write.called)

    def test_concurrent_ui_clears_draft_and_exits(self):
        """第 3 条：发送前页面指纹被换 → 清草稿 + concurrent_ui，不点发送。"""
        before = SimpleNamespace(root=node("DocumentControl", ""), value="",
                                 url="https://chatgpt.com/c/1", busy=False, copies=[])
        written = SimpleNamespace(root=node("DocumentControl", ""), value="我的提问",
                                  url="https://chatgpt.com/c/1", busy=False, copies=[])
        recheck = SimpleNamespace(root=node("DocumentControl", ""), value="",
                                  url="https://chatgpt.com/c/1", busy=False, copies=[])
        conv = mock.Mock()
        conv.empty = lambda v: not v.value.strip()
        conv.site = "test"
        conv.screen = Screen(verbose=False)      # 真 Screen 实例，让 concurrent_ui 检测生效
        conv.read = mock.Mock(side_effect=[before, written, recheck])
        conv.write = mock.Mock()
        conv.submit = mock.Mock()
        conv.clear = mock.Mock()
        with mock.patch.object(sendgate, "page_stamp", return_value={"stamp": 1}), \
             mock.patch.object(sendgate, "page_confirm",
                               return_value={"ok": False, "changed": ["doc_rid"]}):
            got = conversation.ask(conv, "我的提问", timeout=5)
        self.assertEqual(got["stage"], "concurrent_ui")
        self.assertTrue(got["cleared_draft"])
        self.assertTrue(conv.clear.called)
        self.assertFalse(conv.submit.called)

    def test_concurrent_ui_exit_code(self):
        """第 3 条：concurrent_ui 映射到独立退出码 9。"""
        self.assertEqual(exit_codes.CONCURRENT_UI, 9)
        self.assertEqual(exit_codes.code_for("concurrent_ui"), 9)

    def test_floor_timeout(self):
        """第 6 条：高档 review/accept 等待下限 1500s（B12），其余用途原样。"""
        self.assertEqual(tier_guard.floor_timeout("review", 180), 1500)
        self.assertEqual(tier_guard.floor_timeout("accept", 1000), 1500)
        self.assertEqual(tier_guard.floor_timeout("test", 180), 180)
        self.assertEqual(tier_guard.floor_timeout("accept_lite", 180), 180)


# ================================================================ 7. 重发防重
class ResendGuard(unittest.TestCase):
    def test_rerun_after_accepted_clears_refilled_and_does_not_resubmit(self):
        """第 6/7 条：第一次已 accepted 后中断、第二次重跑，输入框被回填提问文本
        → 只读返回 already_answered，清掉回填草稿并读回为空，不再提交。"""
        prompt_node = text("我的提问", top=0)
        copy_btn = button("复制", top=40)
        bubble = node("GroupControl", "", [prompt_node, copy_btn], height=100)
        tree = node("DocumentControl", "", [bubble], height=200)
        before = SimpleNamespace(root=tree, value="我的提问", url="https://chatgpt.com/c/1",
                                 busy=False, copies=[copy_btn], editor=None)
        empty = SimpleNamespace(root=node("DocumentControl", ""), value="",
                                url="https://chatgpt.com/c/1", busy=False,
                                copies=[], editor=None)
        conv = mock.Mock()
        conv.site = "chatgpt"
        conv.empty = lambda v: not v.value.strip()
        conv.read = mock.Mock(side_effect=[before, empty])
        conv.copy = mock.Mock(return_value="回复正文")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)):
            # R8：本事务 + 同一会话（URL 会话 id 与记录一致），才是合法已答。
            submissions.reserve("chatgpt", "我的提问",
                                scope={"site": "chatgpt", "url": "https://chatgpt.com/c/1",
                                       "doc_rid": [1], "hwnd": 4242, "session_id": "1"})
            submissions.finish("chatgpt", "我的提问", {"submission": "accepted"})
            got = conversation.ask(conv, "我的提问", timeout=5)
        self.assertEqual(got["stage"], "already_answered")
        self.assertEqual(got["reply"], "回复正文")
        self.assertTrue(got["cleared_draft"])
        self.assertTrue(conv.clear.called)
        self.assertFalse(conv.write.called)
        self.assertFalse(conv.submit.called)

    def test_restart_retry_does_not_resend_when_answered(self):
        """第 7 条：workbuddy UiDocsLost 重启重跑前，已答完就只读返回，不重发。"""
        args = SimpleNamespace(no_restart=False, prompt="我的提问")
        lost = SimpleNamespace(as_result=lambda: {"ok": False, "stage": "uia_docs_lost"},
                               code=6)
        with mock.patch("qi.restart.restart") as restart, \
             mock.patch("qi.runlog.record_restart") as record_restart, \
             mock.patch("qi.runlog.record") as record, \
             mock.patch.object(wb, "_already_replied",
                               return_value={"ok": True, "stage": "already_answered",
                                             "reply": "已答", "cleared_draft": True}) as replied, \
             mock.patch.object(wb, "_run") as run:
            restart.return_value = {"ok": True, "docs": 1, "steps": 1}
            rc = wb._restart_and_retry(args, sending=True, lost=lost)
        self.assertEqual(rc, 0)
        self.assertTrue(replied.called)
        self.assertFalse(run.called)
        record.assert_called()


# ================================================================ 4. 选模型滚动
class SelectModelScroll(unittest.TestCase):
    def _combo(self, name="Select model: Hy3"):
        return ui.Node("ComboBoxControl", name, SimpleNamespace(
            IsEnabled=True, IsOffscreen=False, BoundingRectangle=box(100, 20)))

    def _item(self, name="Hy3", offscreen=False):
        return ui.Node("ListItemControl", name, SimpleNamespace(
            IsEnabled=True, IsOffscreen=offscreen, BoundingRectangle=box(100, 20)))

    def test_select_model_scrolls_offscreen_target_into_view(self):
        """第 4 条：目标模型项 IsOffscreen 时先 ScrollIntoView 再点。"""
        combo = self._combo()
        target = self._item(offscreen=True)
        opened = [combo, target]
        with mock.patch.object(wb, "controls", return_value=opened), \
             mock.patch.object(wb, "current_model", side_effect=["Hy2", "Hy3"]), \
             mock.patch.object(wb, "model_control", return_value=combo), \
             mock.patch.object(ui, "click") as click, \
             mock.patch.object(ui, "scroll_into_view") as scroll, \
             mock.patch("time.sleep"):
            got = wb.select_model(None, "Hy3")
        self.assertTrue(got["ok"])
        scroll.assert_called_once_with(target)
        self.assertIn(mock.call(None, target), click.call_args_list)

    def test_select_model_retries_when_dropdown_not_open(self):
        """第 4 条：下拉没打开（找不到目标项）就重开下拉再试。"""
        combo = self._combo()
        target = self._item()
        with mock.patch.object(wb, "controls", side_effect=[
                [combo],            # before 的 current_model
                [combo],            # 第 1 次循环：model_control 的 controls
                [combo],            # 第 1 次循环：items（下拉没开）
                [combo],            # 第 2 次循环：model_control 的 controls
                [combo, target],    # 第 2 次循环：items（出现目标项）
                [combo, target],    # after 的 current_model
        ]), \
             mock.patch.object(wb, "current_model", side_effect=["Hy2", "Hy3"]), \
             mock.patch.object(wb, "model_control", return_value=combo), \
             mock.patch.object(ui, "click") as click, \
             mock.patch("time.sleep"):
            got = wb.select_model(None, "Hy3")
        self.assertTrue(got["ok"])
        combo_clicks = [c for c in click.call_args_list if c.args and c.args[1] is combo]
        self.assertGreaterEqual(len(combo_clicks), 2)

    def test_scroll_into_view_calls_scroll_item_pattern(self):
        """第 4 条：scroll_into_view 走 ScrollItemPattern.ScrollIntoView。"""
        pat = mock.Mock()
        ctl = SimpleNamespace(GetPattern=mock.Mock(return_value=pat))
        nd = ui.Node("ListItemControl", "x", ctl)
        self.assertTrue(ui.scroll_into_view(nd))
        pat.ScrollIntoView.assert_called_once()

    def test_scroll_into_view_returns_false_without_pattern(self):
        """第 4 条：控件不支持 ScrollItemPattern → 返回 False，不抛。"""
        ctl = SimpleNamespace(GetPattern=mock.Mock(return_value=None))
        nd = ui.Node("ListItemControl", "x", ctl)
        self.assertFalse(ui.scroll_into_view(nd))


# ================================================================ 7b. web 端已答先于草稿
class WebResendGuard(unittest.TestCase):
    """第 6/7 条：gemini / grok 发送前，已答判定必须先于草稿判定。

    中断后重跑时页面已答完、提问被回填进输入框（输入框非空 → 发送键可点）。
    若先判 draft_present 就会卡在草稿、永远走不到 already_answered。上一轮返工
    已把顺序调成「先 _already_answered 再 draft_present」，这里用真树验：
    回填草稿存在时返回 already_answered（而非 draft_present）、清掉回填草稿并读回为空、
    且 write_prompt / _submit 均未被调用（不重发）。
    """

    def _gemini_tree(self, prompt="我的提问", reply="回复正文", with_send=True):
        children = [text(f"你说 {prompt}", top=0), text(f"Gemini 说 {reply}", top=40)]
        if with_send:
            children.append(button("发送", top=80))
        return node("DocumentControl", "", children, height=200)

    def _grok_tree(self, prompt="我的提问", reply="回复正文", with_send=True):
        children = [text("你", top=0), text(prompt, top=20),
                    text("Grok", top=40), text(reply, top=60)]
        if with_send:
            children.append(button("发送", top=80))
        return node("DocumentControl", "", children, height=200)

    def _assert_answered_before_draft(self, module, site_name, url, *, seeded=False):
        tree = self._gemini_tree() if site_name == "gemini" else self._grok_tree()
        before = SimpleNamespace(root=tree, value="", url=url, busy=False)
        empty = SimpleNamespace(root=node("DocumentControl", ""), value="", url=url, busy=False)
        conv = mock.Mock()
        conv.read = mock.Mock(side_effect=[before, empty])
        conv.clear = mock.Mock()
        site = SimpleNamespace(name=site_name)
        with mock.patch.object(module, "Conversation", return_value=conv), \
             mock.patch.object(module, "write_prompt") as wp, \
             mock.patch.object(module, "_submit") as submit:
            if seeded:
                # R9：Gemini 发送前认领旧回答必须先证明本事务 + 同一会话 id。
                # 真实发送一定先 reserve，这里按真实时序补一条 accepted。
                tmp = tempfile.mkdtemp(prefix="r9_gemini_refill_")
                with mock.patch.object(tier_guard, "ROOT", Path(tmp)):
                    submissions.reserve(site_name, "我的提问",
                                        scope=submissions.scope_from(
                                            site=site_name, url=url,
                                            doc_rid=[900]))
                    submissions.finish(site_name, "我的提问",
                                       {"submission": "accepted"})
                    got = module._send_and_wait(mock.Mock(), site, "我的提问", timeout=5)
            else:
                got = module._send_and_wait(mock.Mock(), site, "我的提问", timeout=5)
        self.assertEqual(got["stage"], "already_answered")
        self.assertEqual(got["reply"], "回复正文")
        self.assertTrue(got["cleared_draft"])
        self.assertTrue(conv.clear.called)
        wp.assert_not_called()
        submit.assert_not_called()

    def test_gemini_answered_before_draft_and_clears_refill(self):
        self._assert_answered_before_draft(
            gemini, "gemini", "https://gemini.google.com/c/1", seeded=True)

    def test_grok_answered_before_draft_and_clears_refill(self):
        self._assert_answered_before_draft(grok_use, "grok", "https://grok.com/c/1")


# ================================================================ 7c. _already_replied 真实判据
class AlreadyRepliedGuard(unittest.TestCase):
    """第 7 条：workbuddy._already_replied 必须真实执行，不 mock 它本身。

    只伪造它读的数据：open_app 返回假 screen、ReplyConversation 返回假 conv，
    树用真 Node 构造，让 prompt_at / last_reply / reply_text / reply_copy 真实跑。
    覆盖已答（返回 already_answered 结果并清回填草稿）与未答（返回 None）两种情况。
    """

    HWND = 4242
    RID = 900
    # R7：恢复现在必须带"与标题无关的会话 id"。这里用一个带会话路径的地址当身份载体
    # （`_already_replied` 本身与端无关；这不是对 WorkBuddy 真机的断言）。
    CHAT_URL = "https://gemini.google.com/app/s-09f"

    def _scope(self, **over):
        base = dict(site="workbuddy", url=self.CHAT_URL, doc_rid=[self.RID],
                    hwnd=self.HWND)
        base.update(over)
        return submissions.scope_from(**base)

    def _answered_root(self):
        prompt_leaf = text("我的提问", top=0)
        reply_leaf = text("回复正文", top=40)
        copy_btn = button("复制", top=60)
        toolbar = node("ToolBarControl", "", [copy_btn], height=40, top=60)
        bubble = node("GroupControl", "", [reply_leaf, toolbar], height=100, top=40)
        root = node("DocumentControl", "", [prompt_leaf, bubble], height=200)
        # R5：恢复要比对文档身份，这里给一个稳定的 runtime id
        root.control.GetRuntimeId = lambda: [self.RID]
        return root

    def test_already_replied_answered_returns_result(self):
        # 2026-11-07 起"已答"还要求**本事务的提交记录**（没有记录 = 不是我们发的）。
        # 真实发送一定先 reserve，所以这里按真实时序先造一条 accepted。
        before = SimpleNamespace(root=self._answered_root(), value="我的提问", busy=False,
                                 url=self.CHAT_URL)
        recheck = SimpleNamespace(root=node("DocumentControl", ""), value="", busy=False,
                                  url=self.CHAT_URL)
        conv = mock.Mock()
        conv.read = mock.Mock(side_effect=[before, recheck])
        conv.empty = mock.Mock(side_effect=[False, True])
        conv.clear = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)), \
             mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
             mock.patch.object(wb, "ReplyConversation", return_value=conv):
            submissions.reserve("workbuddy", "我的提问", scope=self._scope())
            submissions.finish("workbuddy", "我的提问", {"submission": "accepted"})
            got = wb._already_replied("我的提问")
        self.assertIsNotNone(got)
        self.assertEqual(got["stage"], "already_answered")
        self.assertEqual(got["reply"], "回复正文")
        self.assertIsNone(got["submission"])
        self.assertFalse(got["sent"])
        self.assertTrue(got["cleared_draft"])
        self.assertTrue(conv.clear.called)

    def test_already_replied_not_answered_returns_none(self):
        root = node("DocumentControl", "", [text("别的内容", top=0)], height=200)
        root.control.GetRuntimeId = lambda: [self.RID]   # 同一个文档，只是没这条提问
        before = SimpleNamespace(root=root, value="", busy=False, url=self.CHAT_URL)
        conv = mock.Mock()
        conv.read = mock.Mock(return_value=before)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)), \
             mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
             mock.patch.object(wb, "ReplyConversation", return_value=conv), \
             mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)):
            submissions.reserve("workbuddy", "我的提问", scope=self._scope())
            got = wb._already_replied("我的提问")
        self.assertIsNone(got, "有提交记录、但页面上没有这条提问 -> 不能认已答")
        self.assertEqual(wb.recovery_note()["reason"], "prompt_not_unique")

    def test_already_replied_without_transaction_record_returns_none(self):
        """没有本事务提交记录时，即使页面上有同文提问+回答也不认（防跨会话误归属）。"""
        before = SimpleNamespace(root=self._answered_root(), value="我的提问", busy=False)
        conv = mock.Mock()
        conv.read = mock.Mock(return_value=before)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)), \
             mock.patch.object(wb, "open_app",
                               return_value=SimpleNamespace(target_hwnd=self.HWND)), \
             mock.patch.object(wb, "ReplyConversation", return_value=conv):
            got = wb._already_replied("我的提问")
        self.assertIsNone(got)
        self.assertEqual(wb.recovery_note()["reason"], "no_journal")


# ================================================================ 7d. 两次调用同一流程
class TwoCallRerun(unittest.TestCase):
    """第 7 条：一条流程两次调用——第一次 accepted，第二次重跑只读不重发。

    第一次 ask 真实走完「写 → 提交 → 等到已答」，第二次 ask 用同一个 conv 再跑，
    此时提问被回填进输入框。断言 submit 只被调用一次（第二次不重发）、
    第二次返回 already_answered 且回填草稿被清空读回为空。
    """

    def test_two_calls_submit_once_and_clear_refill(self):
        prompt = "我的提问"
        prompt_node = text(prompt, top=0)
        copy_btn = button("复制", top=40)
        answered_root = node("DocumentControl", "", [
            node("GroupControl", "", [prompt_node, copy_btn], height=100)], height=200)
        url = "https://chatgpt.com/c/1"
        empty = SimpleNamespace(root=node("DocumentControl", ""), value="",
                                url=url, busy=False, copies=[], editor=None)
        written = SimpleNamespace(root=node("DocumentControl", ""), value=prompt,
                                  url=url, busy=False, copies=[], editor=None)
        answered = SimpleNamespace(root=answered_root, value="", url=url,
                                   busy=False, copies=[copy_btn], editor=None)
        refilled = SimpleNamespace(root=answered_root, value=prompt, url=url,
                                   busy=False, copies=[copy_btn], editor=None)

        conv = mock.Mock()
        conv.empty = lambda v: not v.value.strip()
        conv.settle = 0
        conv.screen = None
        conv.site = "chatgpt"
        # T2-2 之后，点完发送会先做一轮 PRE_SUBMIT 探测（≤5s），"已提交"那一帧会被多读
        # 一次。读取次数不再写死：第一次发送按 empty→written→answered 发放，之后稳定返回
        # answered（**框是空的**，主循环只在这时才认"已提交"）。
        first = [empty, written, answered]

        def read_first(*args, **kwargs):
            return first.pop(0) if first else answered

        conv.read = mock.Mock(side_effect=read_first)
        conv.write = mock.Mock()
        conv.submit = mock.Mock()
        conv.clear = mock.Mock()
        conv.copy = mock.Mock(return_value="回复正文")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(tier_guard, "ROOT", Path(tmp)):
            got1 = conversation.ask(conv, prompt, timeout=5)
            # R8：第一次发送在真实入口会先 reserve；这里补上同一事务 + 同一会话，
            # 第二次才有资格按 already_answered 只读返回。
            submissions.reserve("chatgpt", prompt,
                                scope={"site": "chatgpt", "url": url,
                                       "doc_rid": [1], "hwnd": 4242, "session_id": "1"})
            submissions.finish("chatgpt", prompt, {"submission": "accepted"})
            # 第二次：输入框里回填着上一轮的提问（refilled），清掉之后读回空框。
            second = [refilled]

            def read_second(*args, **kwargs):
                return second.pop(0) if second else empty

            conv.read = mock.Mock(side_effect=read_second)
            got2 = conversation.ask(conv, prompt, timeout=5)

        self.assertEqual(got1["stage"], "answered")
        self.assertEqual(got1["submission"], "accepted")
        self.assertEqual(conv.submit.call_count, 1)
        self.assertEqual(conv.write.call_count, 1)
        self.assertEqual(got2["stage"], "already_answered")
        self.assertTrue(got2["cleared_draft"])
        self.assertEqual(conv.submit.call_count, 1)   # 第二次没再提交
        self.assertTrue(conv.clear.called)             # 第二次清了回填草稿


if __name__ == "__main__":
    unittest.main()
