"""CARD-09e：后端/UIA 稳定性修复的离线测试。

全离线：不开窗口、不截图、不点桌面、不发消息，所有桌面动作都用假件替掉。

    1  progress.json  读-改-写合并：写完工状态不许抹掉 verdicts / queue；
                       同目录临时文件 + os.replace；文件锁互斥
    2  workbuddy      读不到 UIA 文档 → 重启一次 → 动作重来；只重启一次；
                       --no-restart；重启行写进 runlog
    3  chatgpt        超时/打断后按「最新一条回复/回答已完成」取最后一条回复；
                       --read 只读；还在生成/没有新回复时不交卷
    4  chatgpt        --new 之后清残留草稿：按 UIA 名字认领输入框，读回为空才算清掉
    5  输入归一       标题/列表/链接/零宽字符/合空白；打字核对不上改粘贴纯文本
    6  gemini 写入器   逐像素差判态：同态 9 过、异态 32 不过；两态都高时按模板兜底
    7  空闲闸         脚本自己的合成输入不算"用户在用电脑"
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (str(SRC), str(SRC / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import notify_done  # noqa: E402
from qi import conversation, flow, guard, lastreply, progress, restart, runlog, textcmp, ui  # noqa: E402


# ---------------------------------------------------------------- 假件
def box(width=100, height=20, left=0, top=0):
    return SimpleNamespace(left=left, top=top, right=left + width, bottom=top + height)


def node(kind="GroupControl", name="", children=(), width=100, height=20,
         left=0, top=0, enabled=True, offscreen=False, cls=""):
    item = ui.Node(kind, name, SimpleNamespace(
        IsEnabled=enabled, IsOffscreen=offscreen, BoundingRectangle=box(width, height, left, top),
        ClassName=cls))
    item.children = list(children)
    for child in item.children:
        child.parent = item
    return item


def text(value, left=0, top=0):
    return node("TextControl", value, width=90, height=20, left=left, top=top)


def button(name, left=0, top=0, enabled=True):
    return node("ButtonControl", name, width=24, height=24, left=left, top=top, enabled=enabled)


def reply_bubble(answer, top=100, prompt=None):
    """一条 ChatGPT 回复：正文 + 动作条（复制 / 评价回复 / 朗读 …）。

    `prompt` 给了就在上面再加一条提问气泡（带自己的复制消息按钮）。
    """
    bar = node("GroupControl", "", [button("复制", left=0, top=top + 40),
                                    button("评价回复", left=40, top=top + 40),
                                    button("重新生成回复", left=80, top=top + 40)],
               height=60, top=top + 40)
    lines = [text("回答已完成", top=top - 20)] if answer is None else []
    body = node("GroupControl", "", lines + [text(answer, top=top)], height=100, top=top)
    wrapper = node("GroupControl", "", [body if answer is not None else node("GroupControl", "", lines),
                                        bar], height=120, top=top)
    parts = [wrapper]
    if prompt is not None:
        parts.insert(0, node("GroupControl", "", [
            text(prompt, top=top - 300),
            button("复制消息", top=top - 260)], height=80, top=top - 300))
    return parts


def chatgpt_tree(*bubbles, done=False):
    children = [part for bubble in bubbles for part in bubble]
    if done:
        children.append(text("回答已完成", top=9999))
    return node("DocumentControl", "", children, height=2000)


# ================================================================ 1. progress.json
class ProgressMerge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.seed = {
            "card_id": "09b", "status": "done", "note": "上一张卡", "updated_at": "2026-11-02T16:32:46+08:00",
            "running": ["09a"],
            "verdicts": {"01": "PASS", "04+04b": "BLOCK"},
            "queue": [{"card": "10"}],
        }
        (self.root / "progress.json").write_text(
            json.dumps(self.seed, ensure_ascii=False, indent=2), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_keeps_other_fields(self):
        record = progress.write("09e", "done", "卡 09e 完成", root=self.root)
        self.assertEqual((record["card_id"], record["status"]), ("09e", "done"))
        self.assertEqual(record["note"], "卡 09e 完成")
        self.assertEqual(record["verdicts"], self.seed["verdicts"])     # 判决表原样保留
        self.assertEqual(record["queue"], self.seed["queue"])
        self.assertEqual(record["running"], ["09a"])
        self.assertEqual(json.loads((self.root / "progress.json").read_text(encoding="utf-8")), record)

    def test_write_drops_stale_notify_failed_only(self):
        progress.mark_notify_failed(self.seed, root=self.root)
        self.assertTrue(progress.read(root=self.root)["notify_failed"])
        record = progress.write("09e", "running", "开工", root=self.root)
        self.assertNotIn("notify_failed", record)                       # 新一次写入不带旧标记
        self.assertEqual(record["verdicts"], self.seed["verdicts"])

    def test_mark_notify_failed_keeps_verdicts(self):
        record = progress.write("09e", "done", "写完了", root=self.root)
        updated = progress.mark_notify_failed(record, root=self.root)
        self.assertTrue(updated["notify_failed"])
        self.assertEqual(updated["verdicts"], self.seed["verdicts"])
        on_disk = progress.read(root=self.root)
        self.assertEqual(on_disk["note"], "写完了")
        self.assertEqual(on_disk["queue"], self.seed["queue"])

    def test_no_temp_file_left_behind(self):
        progress.write("09e", "running", "开工", root=self.root)
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_lock_is_mutually_exclusive(self):
        events = []
        with progress.locked(self.root):
            self.assertTrue(progress.lock_path(self.root).exists())
            # 另一个"进程"（线程）在这把锁上应该等不到（超时 0.2s）
            with self.assertRaises(TimeoutError):
                with progress.locked(self.root, timeout=0.2):
                    events.append("不该进来")
        self.assertEqual(events, [])

    def test_parallel_writers_keep_every_field(self):
        """两个写者同时改：verdicts 与 queue 不许因为并发丢一个。"""
        errors = []

        def writer(card):
            try:
                for _ in range(5):
                    progress.write(card, "running", "并发", root=self.root)
            except Exception as exc:                    # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        final = progress.read(root=self.root)
        self.assertEqual(final["verdicts"], self.seed["verdicts"])
        self.assertEqual(final["queue"], self.seed["queue"])
        # 4 个本卡字段 + verdicts / queue / running 三个别人的字段
        self.assertEqual(len(json.loads((self.root / "progress.json").read_text(encoding="utf-8"))), 7)


class NotifyDoneKeepsVerdicts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "progress.json").write_text(json.dumps({
            "card_id": "09b", "status": "done", "note": "x", "updated_at": "2026-11-02T16:00:00+08:00",
            "verdicts": {"05": "PASS(GLM,待Codex复验)"}, "queue": [], "running": ["09e"],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_notify_done_merges(self):
        with mock.patch.object(progress, "ROOT", self.root), \
             mock.patch.dict("os.environ", {"BRIDGE_NOTIFY_URL": ""}), \
             mock.patch("builtins.print"):
            code = notify_done.main(["--card", "09e", "--status", "done", "--note", "收工"])
        self.assertEqual(code, 0)
        record = progress.read(root=self.root)
        self.assertEqual((record["card_id"], record["status"]), ("09e", "done"))
        self.assertEqual(record["verdicts"], {"05": "PASS(GLM,待Codex复验)"})
        self.assertEqual(record["running"], ["09e"])


# ================================================================ 2. WorkBuddy 重启
class RestartChannel(unittest.TestCase):
    def fake(self, **over):
        base = {"find_app": 111, "exe_path": r"D:\WorkBuddy\WorkBuddy.exe",
                "close_window": True, "wait_closed": True, "launch_exe": True,
                "launch": True, "wait_window": 222}
        base.update(over)
        return base

    def run_restart(self, fakes, docs=(1,)):
        patches = [
            mock.patch.object(restart.desk, "find_app", return_value=fakes["find_app"]),
            mock.patch.object(restart.desk, "exe_path", return_value=fakes["exe_path"]),
            mock.patch.object(restart.desk, "close_window", return_value=fakes["close_window"]),
            mock.patch.object(restart.desk, "wait_closed", return_value=fakes["wait_closed"]),
            mock.patch.object(restart.desk, "launch_exe", return_value=fakes["launch_exe"]),
            mock.patch.object(restart.desk, "launch", return_value=fakes["launch"]),
            mock.patch.object(restart.desk, "wait_window", return_value=fakes["wait_window"]),
            mock.patch.object(restart.ui, "documents", side_effect=[list(range(n)) for n in docs]),
            mock.patch.object(restart.time, "sleep", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 超时压到毫秒级：这些用例只验顺序/分支，不验真实等待
        return restart.restart("workbuddy", verbose=False, close_wait=0.01,
                               window_wait=0.01, docs_wait=0.02, settle=0)

    def test_happy_path_order(self):
        info = self.run_restart(self.fake())
        self.assertTrue(info["ok"])
        self.assertEqual(info["steps"], ["close_requested", "closed", "launched", "window", "docs_ready"])
        self.assertEqual(info["hwnd"], 222)
        self.assertEqual(info["docs"], 1)
        self.assertEqual(info["exe"], r"D:\WorkBuddy\WorkBuddy.exe")

    def test_close_timeout_does_not_launch(self):
        with mock.patch.object(restart.desk, "launch_exe") as launch:
            info = self.run_restart(self.fake(wait_closed=False))
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "close_timeout")
        launch.assert_not_called()                  # 没关掉就不许再拉一份

    def test_no_exe_does_not_close(self):
        with mock.patch.object(restart.desk, "close_window") as closer:
            info = self.run_restart(self.fake(exe_path=""))
        self.assertEqual(info["error"], "no_exe")
        closer.assert_not_called()

    def test_docs_still_lost(self):
        info = self.run_restart(self.fake(), docs=(0, 0))
        self.assertEqual(info["error"], "docs_lost")
        self.assertFalse(info["ok"])


class WorkBuddyRestartWiring(unittest.TestCase):
    """workbuddy.main：读不到文档 → 重启一次 → 重来；只重启一次。"""

    def setUp(self):
        from qi import workbuddy
        self.workbuddy = workbuddy
        self.lost = ui.UiDocsLost("workbuddy: 目标文档数量为 0", rounds=[{"round": 1, "docs": 0}])

    def run_main(self, argv, open_app, restart_info, run=None):
        printed = []
        with mock.patch.object(self.workbuddy.guard, "guard", return_value=True), \
             mock.patch.object(self.workbuddy, "open_app", side_effect=open_app), \
             mock.patch.object(restart, "restart", return_value=restart_info) as restarter, \
             mock.patch.object(runlog, "record_restart") as logger, \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(a[0] if a else "")):
            code = self.workbuddy.main(argv)
        return code, printed, restarter, logger

    def test_restart_once_then_succeed(self):
        info = {"ok": True, "steps": ["closed", "launched", "docs_ready"], "docs": 2, "elapsed": 9.1}
        with mock.patch.object(self.workbuddy, "read_state", return_value={"ok": True, "model": "Hy4"}):
            code, printed, restarter, logger = self.run_main(
                ["--list"], [self.lost, None], info)
        self.assertEqual(code, 0)
        self.assertEqual(restarter.call_count, 1)
        logger.assert_called_once()
        self.assertIn("Hy4", "".join(printed))

    def test_restart_then_still_lost_exits_6(self):
        info = {"ok": True, "steps": ["docs_ready"], "docs": 2, "elapsed": 9.1}
        code, printed, restarter, _logger = self.run_main(
            ["--list"], [self.lost, self.lost], info)
        self.assertEqual(code, 6)
        self.assertEqual(restarter.call_count, 1)          # 卡面：只重启一次
        self.assertIn("不第二次重启", "".join(printed))

    def test_restart_failed_exits_6(self):
        info = {"ok": False, "error": "close_timeout", "steps": ["close_timeout"], "docs": 0}
        code, printed, restarter, _logger = self.run_main(["--list"], [self.lost], info)
        self.assertEqual(code, 6)
        self.assertEqual(restarter.call_count, 1)
        self.assertIn("close_timeout", "".join(printed))

    def test_no_restart_flag_skips(self):
        code, printed, restarter, _logger = self.run_main(["--list", "--no-restart"], [self.lost], {})
        self.assertEqual(code, 6)
        restarter.assert_not_called()
        self.assertIn("--no-restart", "".join(printed))

    def test_restart_line_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            runlog.record_restart("workbuddy", {"ok": False, "error": "docs_lost", "steps": ["docs_lost"],
                                                "docs": 0, "elapsed": 30.0, "hwnd": 1}, root=Path(tmp))
            row = json.loads([p for p in Path(tmp).iterdir()][0].read_text(encoding="utf-8").strip())
        self.assertEqual(row["result"], "uia_restart")
        self.assertEqual(row["error"], "docs_lost")
        self.assertFalse(row["ok"])


# ================================================================ 3. ChatGPT 兜底 + --read
class LastReplyMarkers(unittest.TestCase):
    def test_finds_last_reply_only(self):
        tree = chatgpt_tree(reply_bubble("第一条回答", top=100),
                            reply_bubble("第二条回答", top=400))
        self.assertEqual(lastreply.count(tree), 2)
        text, marker = lastreply.last_answer(tree, "")
        self.assertEqual(text, "第二条回答")
        self.assertEqual(marker.name, "复制")

    def test_bubble_text_skips_button_names(self):
        tree = chatgpt_tree(reply_bubble("最终回答", top=100))
        text, _ = lastreply.last_answer(tree, "")
        self.assertNotIn("评价回复", text)
        self.assertEqual(text, "最终回答")

    def test_asks_not_mistaken_for_answer(self):
        """读到的正文等于我们自己的提问 → 往上爬/放弃，绝不把提问当回答。"""
        tree = chatgpt_tree(reply_bubble("我的提问原文", top=100, prompt="我的提问原文"))
        text, _ = lastreply.last_answer(tree, "我的提问原文")
        self.assertNotEqual(text, "我的提问原文")

    def test_role_labels_trimmed_to_reply(self):
        """容器是整个回合时只留「ChatGPT 说：」之后的内容（--read 的正文就是这个）。"""
        bar = node("GroupControl", "", [button("复制", top=40), button("评价回复", left=40, top=40)],
                   height=60, top=40)
        wrapper = node("GroupControl", "", [
            text("你说：", top=0), text("我的提问", top=20),
            text("ChatGPT 说：", top=40), text("回答正文", top=60), bar], height=120)
        tree = node("DocumentControl", "", [wrapper], height=400)
        text_got, _node = lastreply.last_answer(tree, "我的提问")
        self.assertEqual(text_got, "回答正文")
        self.assertEqual(lastreply._drop_role_prefix("你说：\n提问\nChatGPT 说：\n回答"), "回答")
        self.assertIsNone(lastreply._drop_role_prefix("没有标签"))

    def test_done_signal(self):
        self.assertEqual(lastreply.done_signal(chatgpt_tree(done=True)), "回答已完成")
        self.assertIsNone(lastreply.done_signal(chatgpt_tree(reply_bubble("x"))))

    def test_prompt_bubble_is_not_a_reply(self):
        tree = chatgpt_tree(reply_bubble("回答", top=100, prompt="提问"))
        # 提问气泡上只有「复制消息」，不在 COPY 集合里
        self.assertEqual(lastreply.count(tree), 1)


class FlowUiaFallback(unittest.TestCase):
    SITE = flow.Site(name="chatgpt", title_hint="Google Chrome", composer="gpt_composer",
                     send="gpt_send_on", actions="gpt_actions", copy="gpt_copy",
                     composer_offset=(240, -5), uia_fallback=True)

    def run_fallback(self, result, tree, baseline=0, site=None):
        screen = SimpleNamespace(target_hwnd=1)
        with mock.patch.object(ui, "document", return_value=(object(), "https://chatgpt.com/c/1")), \
             mock.patch.object(ui, "tree", return_value=tree):
            return flow.uia_answer(screen, site or self.SITE, "我的提问", result, baseline)

    def test_timeout_with_new_reply_is_rescued(self):
        tree = chatgpt_tree(reply_bubble("页面早答完了", top=100), done=True)
        result = {"ok": False, "stage": "timeout", "submission": "accepted", "sent": True,
                  "timeout_kind": "no_reply"}
        got = self.run_fallback(result, tree)
        self.assertTrue(got["ok"])
        self.assertEqual(got["reply"], "页面早答完了")
        self.assertEqual(got["read_via"], "uia")
        self.assertEqual(got["basis"], "uia_last_reply_marker")
        self.assertEqual(got["fallback_from"], "timeout")

    def test_interrupted_with_new_reply_is_rescued(self):
        tree = chatgpt_tree(reply_bubble("光标挪不过去也读得到", top=100))
        got = self.run_fallback({"ok": False, "stage": "interrupted",
                                 "submission": "accepted", "sent": True}, tree)
        self.assertTrue(got["ok"])
        self.assertEqual(got["fallback_from"], "interrupted")

    def test_no_new_reply_is_not_rescued(self):
        tree = chatgpt_tree(reply_bubble("上一轮的回答", top=100))
        self.assertIsNone(self.run_fallback({"ok": False, "stage": "timeout",
                                             "submission": "accepted"}, tree, baseline=1))

    def test_still_generating_is_not_rescued(self):
        bar = node("GroupControl", "", [button("停止生成")])
        tree = node("DocumentControl", "", [bar] + chatgpt_tree(reply_bubble("半截话", top=100)).children)
        self.assertIsNone(self.run_fallback({"ok": False, "stage": "timeout",
                                             "submission": "accepted"}, tree))

    def test_never_sent_is_not_rescued(self):
        tree = chatgpt_tree(reply_bubble("别人的回答", top=100))
        self.assertIsNone(self.run_fallback({"ok": False, "stage": "input_mismatch",
                                             "submission": "not_sent"}, tree))

    def test_site_without_flag_is_untouched(self):
        site = flow.Site(name="deepseek", title_hint="Google Chrome", composer="ds_composer",
                         send="ds_send_on", actions="ds_actions", copy="ds_copy",
                         composer_offset=(0, 0))
        tree = chatgpt_tree(reply_bubble("回答", top=100))
        self.assertIsNone(self.run_fallback({"ok": False, "stage": "timeout",
                                             "submission": "accepted"}, tree, site=site))


class WatchHook(unittest.TestCase):
    """页面自己说"答完了"就不再干等：连续两轮一样才交卷。"""

    def make(self, tree, baseline=0, prompt="我的提问"):
        return flow._watch_for(lastreply, prompt, baseline)

    def view(self, tree, busy=False):
        return SimpleNamespace(root=tree, busy=busy, value="", url="https://chatgpt.com/c/1",
                               copies=[], editor=None)

    def test_needs_two_identical_reads(self):
        tree = chatgpt_tree(reply_bubble("答完了", top=100))
        watch = self.make(tree)
        self.assertIsNone(watch(self.view(tree)))            # 第一轮：只记下
        got = watch(self.view(tree))                          # 第二轮：一样，交卷
        self.assertTrue(got["ok"])
        self.assertEqual(got["reply"], "答完了")
        self.assertTrue(got["early"])

    def test_busy_never_returns(self):
        tree = chatgpt_tree(reply_bubble("半截话", top=100))
        watch = self.make(tree)
        self.assertIsNone(watch(self.view(tree, busy=True)))
        self.assertIsNone(watch(self.view(tree, busy=True)))

    def test_text_change_resets(self):
        first = chatgpt_tree(reply_bubble("写到一半", top=100))
        second = chatgpt_tree(reply_bubble("写完了", top=100))
        watch = self.make(second)
        self.assertIsNone(watch(self.view(first)))
        self.assertIsNone(watch(self.view(second)))           # 变了：重新计数
        self.assertTrue(watch(self.view(second))["ok"])

    def test_no_new_marker_never_returns(self):
        tree = chatgpt_tree(reply_bubble("上一轮的", top=100))
        watch = self.make(tree, baseline=1)
        self.assertIsNone(watch(self.view(tree)))
        self.assertIsNone(watch(self.view(tree)))

    def test_ask_returns_watch_result_early(self):
        tree = chatgpt_tree(reply_bubble("早答完了", top=100))
        written = self.view(node("DocumentControl", ""))
        written.value = "提问"                     # 写完读回来和提问一致，才会进等待循环
        views = [self.view(node("DocumentControl", "")), written, self.view(tree)]
        conv = mock.Mock()
        conv.empty = lambda view: True
        conv.read = mock.Mock(side_effect=views)
        conv.write = mock.Mock()
        conv.submit = mock.Mock()
        got = conversation.ask(conv, "提问", timeout=5,
                               watch=lambda view: {"ok": True, "stage": "answered",
                                                   "reply": "早答完了", "submission": "accepted",
                                                   "sent": True, "early": True})
        self.assertEqual(got["stage"], "answered")
        self.assertEqual(got["reply"], "早答完了")
        self.assertEqual(got["submission"], "accepted")


class ReadOnlyMode(unittest.TestCase):
    def test_read_last_returns_stage_read(self):
        tree = chatgpt_tree(reply_bubble("最后一条回复", top=100), done=True)
        site = FlowUiaFallback.SITE
        with mock.patch.object(flow, "ensure_site", return_value=1), \
             mock.patch.object(flow.Screen, "bind"), \
             mock.patch.object(flow.Screen, "focus", return_value=True), \
             mock.patch.object(ui, "document", return_value=(object(), "https://chatgpt.com/c/9")), \
             mock.patch.object(ui, "tree", return_value=tree):
            got = flow.read_last(site, verbose=False)
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "read")
        self.assertEqual(got["reply"], "最后一条回复")
        self.assertEqual(got["url"], "https://chatgpt.com/c/9")

    def test_read_last_without_reply(self):
        site = FlowUiaFallback.SITE
        with mock.patch.object(flow, "ensure_site", return_value=1), \
             mock.patch.object(flow.Screen, "bind"), \
             mock.patch.object(flow.Screen, "focus", return_value=True), \
             mock.patch.object(ui, "document", return_value=(object(), "")), \
             mock.patch.object(ui, "tree", return_value=chatgpt_tree()):
            got = flow.read_last(site, verbose=False)
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "no_reply")

    def test_cli_read_flag(self):
        with mock.patch.object(flow, "read_last", return_value={"ok": True, "stage": "read",
                                                                "reply": "x"}) as reader, \
             mock.patch.object(flow.guard, "guard", return_value=True), \
             mock.patch.object(runlog, "record"), \
             mock.patch("builtins.print"):
            code = flow.cli(FlowUiaFallback.SITE, ["--read"])
        self.assertEqual(code, 0)
        self.assertTrue(reader.called)

    def test_cli_read_rejects_prompt(self):
        with mock.patch.object(flow.guard, "guard", return_value=True), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                flow.cli(FlowUiaFallback.SITE, ["--read", "还有提问"])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_read_needs_fallback_site(self):
        site = flow.Site(name="deepseek", title_hint="Google Chrome", composer="c", send="s",
                         actions="a", copy="c", composer_offset=(0, 0))
        with mock.patch.object(flow.guard, "guard", return_value=True), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                flow.cli(site, ["--read"])
        self.assertEqual(caught.exception.code, 2)


# ================================================================ 4. 清残留草稿
class ClearDraft(unittest.TestCase):
    SITE = flow.Site(name="chatgpt", title_hint="Google Chrome", composer="gpt_composer",
                     send="gpt_send_on", actions="gpt_actions", copy="gpt_copy",
                     composer_offset=(240, -5), composer_name="询问 ChatGPT",
                     clear_draft_on_new=True)

    def make_tree(self, value, name="询问 ChatGPT", empty=False):
        editor = node("EditControl", "" if empty else value, width=555, height=36)
        editor.name = name
        placeholder = text(name if not empty else name, left=0, top=0)
        return node("DocumentControl", "", [editor, placeholder])

    def run_clear(self, values):
        screen = SimpleNamespace(target_hwnd=1, hotkey=mock.Mock(), press=mock.Mock())
        trees = [self.make_tree(v) for v in values]
        with mock.patch.object(ui, "document", return_value=(object(), "")), \
             mock.patch.object(ui, "tree", side_effect=trees), \
             mock.patch.object(ui, "edit_value", side_effect=list(values)), \
             mock.patch.object(ui, "click") as clicker, \
             mock.patch.object(flow.time, "sleep"):
            got = flow.clear_composer(screen, self.SITE)
        return got, screen, clicker

    def test_clears_and_reads_back_empty(self):
        got, screen, clicker = self.run_clear(["上一条没发出去的草稿", ""])
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "cleared")
        self.assertTrue(clicker.called)
        screen.hotkey.assert_called_once_with("ctrl", "a")
        screen.press.assert_called_once_with("delete")

    def test_already_empty_does_not_press_keys(self):
        got, screen, clicker = self.run_clear([""])
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "already_empty")
        screen.hotkey.assert_not_called()
        screen.press.assert_not_called()
        clicker.assert_not_called()

    def test_failed_clear_is_reported(self):
        got, _screen, _clicker = self.run_clear(["旧草稿", "还有残留"])
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "draft_clear_failed")

    def test_ask_stops_when_clear_fails(self):
        screen = SimpleNamespace(target_hwnd=1, bind=mock.Mock(), focus=mock.Mock(return_value=True))
        with mock.patch.object(flow, "ensure_site", return_value=1), \
             mock.patch.object(flow.Screen, "bind"), \
             mock.patch.object(flow.Screen, "focus", return_value=True), \
             mock.patch("qi.zoom.ensure"), \
             mock.patch.object(flow, "_open_new_chat", return_value={"ok": True}):
            with mock.patch.object(flow, "clear_composer",
                                   return_value={"ok": False, "stage": "draft_clear_failed"}), \
                 mock.patch.object(flow, "Screen", return_value=screen):
                got = flow.ask(self.SITE, "提问", verbose=False)
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "draft_clear_failed")
        self.assertEqual(got["submission"], "not_sent")


# ================================================================ 5. 输入归一 + 粘贴
class Normalize(unittest.TestCase):
    def test_heading_marker_removed(self):
        self.assertTrue(textcmp.equal("# CARD-09 草稿\n正文", "CARD-09 草稿\n正文"))

    def test_list_marker_still_removed(self):
        self.assertTrue(textcmp.equal("1. 第一条\n- 第二条", "第一条\n第二条"))

    def test_inline_markdown_to_plain(self):
        self.assertTrue(textcmp.equal("**粗** 和 `code` 与 ~~删~~", "粗 和 code 与 删"))
        self.assertTrue(textcmp.equal("见 [文档](https://x/y)", "见 文档"))

    def test_autolink_and_zero_width(self):
        self.assertTrue(textcmp.equal("https://127.0.0.1:18765/ 下所有路径",
                                      "https://127.0.0.1:18765/\u200b 下所有路径"))

    def test_whitespace_collapsed(self):
        self.assertTrue(textcmp.equal("a  \u00a0 b", "a b"))

    def test_real_difference_still_detected(self):
        self.assertFalse(textcmp.equal("发送成功", "发送失败"))
        self.assertIn("不一致", textcmp.describe("发送成功", "发送失败"))

    def test_decimal_not_treated_as_list(self):
        self.assertEqual(textcmp.normalize("比例 1.5 倍"), "比例 1.5 倍")


class PasteFallback(unittest.TestCase):
    def view(self, value):
        return SimpleNamespace(url="https://chatgpt.com/c/1", root=node("DocumentControl", ""),
                               editor=None, value=value, busy=False, copies=[])

    def test_mismatch_rewrites_with_paste(self):
        views = [self.view(""), self.view("被吃掉标记的文本"),
                 self.view("被吃掉标记的文本"), self.view("原文")]
        conv = mock.Mock()
        conv.empty = lambda view: not view.value.strip()
        conv.read = mock.Mock(side_effect=views)
        conv.write = mock.Mock()
        conv.clear = mock.Mock()
        conv.paste = mock.Mock()
        conv.submit = mock.Mock(side_effect=conversation.UserTookOver("让位"))
        got = conversation.ask(conv, "原文", timeout=5, plain=True)
        self.assertEqual(got["stage"], "user_took_over")
        self.assertEqual(got["write_via"], "paste")
        self.assertTrue(conv.clear.called)
        self.assertTrue(conv.paste.called)

    def test_plain_off_keeps_input_mismatch(self):
        views = [self.view(""), self.view("被吃掉标记的文本")]
        conv = mock.Mock()
        conv.empty = lambda view: not view.value.strip()
        conv.read = mock.Mock(side_effect=views)
        conv.write = mock.Mock()
        got = conversation.ask(conv, "原文", timeout=5, plain=False)
        self.assertEqual(got["stage"], "input_mismatch")
        self.assertNotIn("write_via", got)


# ================================================================ 6. gemini 写入器判态
class WriterJudgement(unittest.TestCase):
    def cfg(self):
        from qi.choose import Chooser
        return Chooser(name="gemini", anchor="gem_composer", open_offset=(0, 0), prefix="gem_",
                       writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),
                       writers={"dialog": 0, "task": 1})

    def test_confident_thresholds(self):
        from qi.choose import _writer_confident
        self.assertTrue(_writer_confident("dialog", {"dialog": 9.15, "task": 32.24}))
        self.assertTrue(_writer_confident("dialog", {"dialog": 13.0, "task": 52.0}))
        self.assertFalse(_writer_confident("dialog", {"dialog": 13.0, "task": 20.0}))
        self.assertFalse(_writer_confident("dialog", {"dialog": 40.0, "task": 100.0}))

    def test_read_writer_parks_mouse_before_measuring(self):
        from qi import choose
        cfg = self.cfg()
        screen = mock.Mock()
        screen.target_hwnd = None                     # 不让它去读控件树
        screen.window_rect.return_value = (-1440, -560, 1380, 1340)
        screen.find.return_value = SimpleNamespace(x=0, y=0, w=104, h=42)
        screen.last_best = 0.994
        with mock.patch.object(choose, "_writer_diffs",
                               return_value={"gem_writer_tabs_dialog": 9.15,
                                             "gem_writer_tabs_task": 32.24}):
            got = choose.read_writer(screen, cfg)
        self.assertEqual(got["writer"], "dialog")
        self.assertEqual(got["basis"], "pixel_diff")
        self.assertTrue(screen.move_to.called)          # 量之前先把鼠标挪开

    def test_two_states_far_apart_falls_back_to_template(self):
        from qi import choose
        cfg = self.cfg()
        cur = {"writer": "unknown", "diff": {"gem_writer_tabs_dialog": 22.0,
                                             "gem_writer_tabs_task": 22.5},
               "locate": {"gem_writer_tabs_dialog": 0.994, "gem_writer_tabs_task": 0.971},
               "hint": "两种模式都差得远"}
        got = choose.assume_writer(cur, cfg, "dialog")
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "assumed")
        self.assertTrue(got["degraded"])
        self.assertEqual(got["basis"], "template_hit")

    def test_template_fallback_refuses_close_scores(self):
        """2026-11-02 真机反例：任务态下两张模板 0.950 / 0.937，差 0.013，必须拒。"""
        from qi import choose
        cfg = self.cfg()
        cur = {"writer": "unknown",
               "locate": {"gem_writer_tabs_dialog": 0.950, "gem_writer_tabs_task": 0.937}}
        self.assertIsNone(choose.assume_writer(cur, cfg, "dialog"))

    def test_uia_slider_reads_selected_segment(self):
        """分段控件的选中胶囊压在谁身上，谁就是当前模式（Gemini 真机结构）。"""
        from qi import choose
        cfg = self.cfg()
        slider = node("GroupControl", "", width=55, height=37, left=0, top=0,
                      cls="gem-segmented-button-row-slider")
        dialog = button("对话", left=0, top=0)
        task = button("任务", left=55, top=0)
        for item in (slider, dialog, task):
            item.control.BoundingRectangle = box(55, 37, left=item.control.BoundingRectangle.left)
            item.control.ClassName = ("gem-segmented-button-row-slider" if item is slider
                                      else "mat-ripple gem-segmented-button-action ng-star-inserted")
        group = node("GroupControl", "", [slider, dialog, task], width=110, height=37)
        tree = node("DocumentControl", "", [group], height=400)
        screen = SimpleNamespace(target_hwnd=1)
        with mock.patch.object(ui, "document", return_value=(object(), "")), \
             mock.patch.object(ui, "tree", return_value=tree):
            got = choose._writer_from_uia(screen, cfg)
        self.assertIsNotNone(got)
        self.assertEqual(got["writer"], "dialog")
        self.assertEqual(got["basis"], "uia_slider")
        self.assertEqual(got["box"], [0, 0, 110, 37])

        # 胶囊挪到「任务」上：读出来就该是 task
        slider.control.BoundingRectangle = box(55, 37, left=55)
        with mock.patch.object(ui, "document", return_value=(object(), "")), \
             mock.patch.object(ui, "tree", return_value=tree):
            got = choose._writer_from_uia(screen, cfg)
        self.assertEqual(got["writer"], "task")

    def test_template_fallback_refuses_ambiguous(self):
        from qi import choose
        cfg = self.cfg()
        cur = {"writer": "unknown", "locate": {"gem_writer_tabs_dialog": 0.971,
                                               "gem_writer_tabs_task": 0.994}}
        self.assertIsNone(choose.assume_writer(cur, cfg, "dialog"))   # 另一态更像，不认
        cur2 = {"writer": "unknown", "locate": {"gem_writer_tabs_dialog": 0.93,
                                                "gem_writer_tabs_task": 0.80}}
        self.assertIsNone(choose.assume_writer(cur2, cfg, "dialog"))  # 分数不够，不认

    def test_choose_writer_reports_unreadable_without_fallback(self):
        from qi import choose
        cfg = self.cfg()
        screen = mock.Mock()
        with mock.patch.object(choose, "read_writer",
                               return_value={"writer": "unknown", "locate": {},
                                             "hint": "两种模式都差得远"}):
            got = choose.choose_writer(screen, cfg, "dialog")
        self.assertFalse(got["ok"])
        self.assertEqual(got["stage"], "unreadable")

    def test_choose_writer_already_in_mode(self):
        from qi import choose
        cfg = self.cfg()
        screen = mock.Mock()
        with mock.patch.object(choose, "read_writer",
                               return_value={"writer": "dialog", "basis": "pixel_diff",
                                             "diff": {"gem_writer_tabs_dialog": 9.15}}):
            got = choose.choose_writer(screen, cfg, "dialog")
        self.assertTrue(got["ok"])
        self.assertEqual(got["stage"], "already")
        self.assertFalse(screen.click_xy.called)


# ================================================================ 7. 空闲闸
class OwnInputIsNotUser(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mark = Path(self._tmp.name) / "synthetic_input.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_marked_tick_counts_as_idle(self):
        with mock.patch.object(guard, "_last_input_tick", return_value=424242), \
             mock.patch.object(guard, "MARK_FILE", self.mark):
            self.assertEqual(guard.mark_synthetic("test"), 424242)
            self.assertEqual(guard.synthetic_tick(), 424242)
            self.assertEqual(guard.idle_seconds(), guard.OWN_IDLE)

    def test_user_input_after_ours_is_not_ignored(self):
        with mock.patch.object(guard, "MARK_FILE", self.mark), \
             mock.patch.object(guard, "_last_input_tick", return_value=424242):
            guard.mark_synthetic("test")
        with mock.patch.object(guard, "MARK_FILE", self.mark), \
             mock.patch.object(guard, "_last_input_tick", return_value=424999), \
             mock.patch.object(guard._kernel32, "GetTickCount64", return_value=425000):
            self.assertLess(guard.idle_seconds(), 5.0)

    def test_wait_for_idle_does_not_wait_after_our_own_input(self):
        with mock.patch.object(guard, "MARK_FILE", self.mark), \
             mock.patch.object(guard, "_last_input_tick", return_value=777):
            guard.mark_synthetic("test")
        with mock.patch.object(guard, "MARK_FILE", self.mark), \
             mock.patch.object(guard, "_last_input_tick", return_value=777), \
             mock.patch.object(guard.time, "sleep") as sleeper, \
             mock.patch("builtins.print"):
            ok, cur = guard.wait_for_idle(2.0, 30.0)
        self.assertTrue(ok)
        self.assertEqual(cur, guard.OWN_IDLE)
        sleeper.assert_not_called()

    def test_missing_mark_file_is_harmless(self):
        with mock.patch.object(guard, "MARK_FILE", self.mark):
            self.assertIsNone(guard.synthetic_tick())


class ScreenMarksOwnInput(unittest.TestCase):
    def test_input_primitives_mark(self):
        from qi import screen as screen_mod
        s = screen_mod.Screen.__new__(screen_mod.Screen)
        s.target_hwnd = None
        s.require_foreground = False
        with mock.patch.object(guard, "mark_synthetic") as marker, \
             mock.patch.object(screen_mod, "_send"):
            s.press("delete")
            s.hotkey("ctrl", "a")
            s.type_text("ab", interval=0)
        self.assertGreaterEqual(marker.call_count, 3)


if __name__ == "__main__":
    unittest.main()
