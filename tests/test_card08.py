"""CARD-08：编码修复 + 第 7b 项自动推。全离线：不开窗口、不截图、不发消息、不写真的 runs\\。

三组用例：

    1  入口编码（qi.console）
       · 含 \\u200b 的回复在 cp936 流下不崩（真起一个 cp936 子进程，外加流级重放）
       · reconfigure 不可用 → 退回包装器；有 buffer 的按 utf-8 落字节
       · 重复 setup 不叠包装；打印去掉零宽，但结果对象/原文一个字不改
    2  自动推判定（qi.autopush.decide）
       · 非 running / 没卡号 / 生成中 / 刚有动静 → 不推
       · 停了 → 推；autopush 自己写的行不算「有新行」
       · 同卡 30 分钟 1 次；别的卡不算；3 次没进展 → 停推
       · 推失败不算一次推；progress 动过之后旧推不计数
    3  命令行（src\\scripts\\workbuddy_autopush.py）
       · --dry-run 只判定：不发命令、不写日志、不通知
       · 真推就调 workbuddy_use --continue，成功记 autopush_push
       · 推失败退 1、记 autopush_push_failed，且不占冷却
       · 停推退 3、调通知脚本；同一张卡同一次 progress 只叫一次
    4  接线：每个 *_use.py / *_choose.py / workflow_use.py 的 __main__ 都调了 console.setup()
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = SRC / "scripts"
for entry in (str(SRC), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import autopush, console  # noqa: E402

import workbuddy_autopush as autopush_cli  # noqa: E402

UPDATED = "2026-11-02T07:00:00+08:00"
NOW = "2026-11-02T09:00:00+08:00"
ZERO = "\u200b"


# ---------------------------------------------------------------- 小工具
def record(status="running", card="08", updated=UPDATED):
    return {"card_id": card, "status": status, "note": "", "updated_at": updated}


def line(when, endpoint="workbuddy", result="answered", **extra):
    return {"time": when, "endpoint": endpoint, "ok": True, "result": result,
            "timeout_kind": None, "submission": "accepted", "elapsed": 1.0, "error": None,
            **extra}


def push_line(when, card="08", result="autopush_push", mark=UPDATED):
    return line(when, endpoint="autopush", result=result, card=card, action="push",
                reason="stalled", progress_mark=mark)


def write_events(folder, rows):
    path = Path(folder) / "20261102.jsonl"
    with console.utf8_open(path, "a") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_lines(folder):
    rows = []
    for path in sorted(Path(folder).glob("*.jsonl")):
        rows += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows


@contextlib.contextmanager
def streams(out, err=None):
    """把 sys.stdout/stderr 换成指定流，退出时一定还原（不污染其它用例）。"""
    with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err or out):
        yield


def decide(rows=(), *, now=NOW, gen=False, status="running", updated=UPDATED, card="08",
           config=None):
    return autopush.decide(record(status=status, card=card, updated=updated), list(rows),
                           now=now, generating=gen, config=config)


# ================================================================ 1. 编码
class ZeroWidthTests(unittest.TestCase):
    def test_strip_removes_only_zero_width(self):
        raw = "回复" + ZERO + "里\u200c有\u200d零宽\ufeff字符"
        self.assertEqual(console.strip_zero_width(raw), "回复里有零宽字符")

    def test_strip_leaves_normal_text_alone(self):
        self.assertEqual(console.strip_zero_width("收到，OK\n"), "收到，OK\n")

    def test_strip_passes_non_string(self):
        self.assertIsNone(console.strip_zero_width(None))
        self.assertEqual(console.strip_zero_width(7), 7)


class Cp936StreamTests(unittest.TestCase):
    """真机那条崩法：GBK 控制台写 \\u200b → UnicodeEncodeError。"""

    def test_cp936_strict_stream_really_crashes(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936", errors="strict")
        with self.assertRaises(UnicodeEncodeError):
            stream.write("带" + ZERO + "零宽")

    def test_setup_makes_cp936_stream_print_fine(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp936", errors="strict")
        with streams(stream):
            report = console.setup()
            self.assertEqual(report, {"stdout": "reconfigure", "stderr": "reconfigure"})
            print("收到" + ZERO + "OK")
            sys.stdout.flush()
        self.assertEqual(raw.getvalue().decode("utf-8").strip(), "收到OK")

    def test_fallback_wrapper_when_reconfigure_missing(self):
        class Raw:
            encoding = "cp936"

            def __init__(self):
                self.text = ""

            def write(self, value):
                value.encode("cp936")          # 严格：编不出来就抛
                self.text += value
                return len(value)

            def flush(self):
                pass

        raw = Raw()
        with streams(raw):
            report = console.setup()
            self.assertEqual(report["stdout"], "wrapper")
            sys.stdout.write("回复" + ZERO + "里")
        self.assertEqual(raw.text, "回复里")

    def test_fallback_uses_utf8_bytes_when_buffer_available(self):
        class Raw:
            encoding = "cp936"

            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, value):
                raise UnicodeEncodeError("cp936", value, 0, 1, "boom")

            def flush(self):
                pass

        raw = Raw()
        with streams(raw):
            self.assertEqual(console.setup()["stdout"], "wrapper")
            sys.stdout.write("中文" + ZERO + "\n")
        self.assertEqual(raw.buffer.getvalue().decode("utf-8"), "中文\n")

    def test_setup_twice_does_not_stack(self):
        base = io.StringIO()
        with streams(base):
            console.setup()
            first = sys.stdout
            self.assertEqual(console.setup(), {"stdout": "already", "stderr": "already"})
            self.assertIs(sys.stdout, first)
            self.assertIs(sys.stdout._raw, base)

    def test_printed_reply_loses_zero_width_but_result_keeps_it(self):
        base = io.StringIO()
        result = {"ok": True, "stage": "answered", "reply": "答" + ZERO + "案"}
        with streams(base):
            console.setup()
            print(json.dumps(result, ensure_ascii=False))
        self.assertNotIn(ZERO, base.getvalue())
        self.assertEqual(result["reply"], "答" + ZERO + "案")   # 交给调用方的原文没动

    def test_utf8_open_writes_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.txt"
            with console.utf8_open(target, "w") as handle:
                handle.write("中文" + ZERO + "\n")
            self.assertEqual(target.read_bytes().decode("utf-8"), "中文" + ZERO + "\n")

    def test_real_process_with_cp936_stdio(self):
        """真起一个进程：PYTHONIOENCODING=cp936，跑 setup 后打印带零宽的回复。"""
        code = ("import sys; sys.path.insert(0, r'%s'); "
                "from qi import console; console.setup(); "
                "print('收到\\u200bOK')" % SRC)
        env = {**os.environ, "PYTHONIOENCODING": "cp936"}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout.decode("utf-8").strip(), "收到OK")


# ================================================================ 2. 判定
class DecideTests(unittest.TestCase):
    def test_not_running_is_skipped(self):
        self.assertEqual(decide(status="done").action, "skip")
        self.assertEqual(decide(status="done").reason, "not_running")

    def test_running_without_card_is_skipped(self):
        self.assertEqual(decide(card="").reason, "no_card")

    def test_generating_wins_over_idle(self):
        got = decide([line("2026-11-02T08:00:00+0800")], gen=True)
        self.assertEqual((got.action, got.reason), ("wait", "generating"))

    def test_generating_flag_from_runlog(self):
        rows = [line("2026-11-02T08:00:00+0800", result="generating")]
        self.assertEqual(decide(rows).reason, "generating")

    def test_still_generating_timeout_does_not_block(self):
        rows = [line("2026-11-02T08:00:00+0800", result="timeout",
                     timeout_kind="still_generating")]
        self.assertEqual(decide(rows).action, "push")

    def test_recent_activity_waits(self):
        got = decide([line("2026-11-02T08:55:00+0800")])          # 5 分钟前
        self.assertEqual((got.action, got.reason), ("wait", "recent_activity"))
        self.assertAlmostEqual(got.idle_minutes, 5.0, places=1)

    def test_stalled_pushes(self):
        got = decide([line("2026-11-02T08:00:00+0800")])          # 60 分钟前
        self.assertEqual((got.action, got.reason), ("push", "stalled"))
        self.assertAlmostEqual(got.idle_minutes, 60.0, places=1)
        self.assertEqual(got.pushes, 0)

    def test_autopush_lines_do_not_reset_the_idle_clock(self):
        rows = [line("2026-11-02T08:00:00+0800"),
                push_line("2026-11-02T08:58:00+0800", card="07")]   # 2 分钟前，但是别的卡
        got = decide(rows)
        self.assertEqual(got.action, "push")                       # 自己推过也不算有新行
        self.assertAlmostEqual(got.idle_minutes, 60.0, places=1)

    def test_threshold_is_configurable(self):
        rows = [line("2026-11-02T08:50:00+0800")]                  # 10 分钟前
        self.assertEqual(decide(rows).action, "wait")
        self.assertEqual(decide(rows, config=autopush.Config(idle_minutes=5)).action, "push")

    def test_cooldown_blocks_a_second_push(self):
        rows = [line("2026-11-02T08:00:00+0800"), push_line("2026-11-02T08:50:00+0800")]
        got = decide(rows, now="2026-11-02T09:10:00+0800")         # 上次推是 20 分钟前
        self.assertEqual((got.action, got.reason), ("wait", "cooldown"))
        self.assertEqual(got.pushes, 1)
        self.assertEqual(got.next_allowed_at, "2026-11-02T09:20:00+08:00")

    def test_cooldown_expires_after_thirty_minutes(self):
        rows = [line("2026-11-02T08:00:00+0800"), push_line("2026-11-02T08:50:00+0800")]
        got = decide(rows, now="2026-11-02T09:21:00+0800")
        self.assertEqual(got.action, "push")

    def test_other_cards_pushes_do_not_block(self):
        rows = [line("2026-11-02T08:00:00+0800"),
                push_line("2026-11-02T08:55:00+0800", card="07")]
        self.assertEqual(decide(rows).action, "push")

    def test_three_pushes_without_progress_stop(self):
        rows = [line("2026-11-02T07:10:00+0800"),
                push_line("2026-11-02T07:30:00+0800"),
                push_line("2026-11-02T08:10:00+0800"),
                push_line("2026-11-02T08:50:00+0800")]
        got = decide(rows, now="2026-11-02T09:30:00+0800")
        self.assertEqual((got.action, got.reason), ("stop", "no_progress"))
        self.assertEqual(got.pushes, 3)
        self.assertTrue(got.needs_human)

    def test_pushes_before_progress_update_do_not_count(self):
        rows = [push_line("2026-11-02T06:00:00+0800"),
                push_line("2026-11-02T06:10:00+0800"),
                push_line("2026-11-02T06:20:00+0800"),
                line("2026-11-02T07:30:00+0800")]
        got = decide(rows, now="2026-11-02T09:00:00+0800")          # progress 07:00 动过
        self.assertEqual(got.action, "push")
        self.assertEqual(got.pushes, 0)

    def test_failed_push_is_not_a_push(self):
        rows = [line("2026-11-02T08:00:00+0800"),
                push_line("2026-11-02T08:10:00+0800", result="autopush_push_failed"),
                push_line("2026-11-02T08:20:00+0800", result="autopush_push_failed")]
        got = decide(rows, now="2026-11-02T09:00:00+0800")
        self.assertEqual(got.action, "push")
        self.assertEqual(got.pushes, 0)

    def test_no_signal_at_all_still_pushes(self):
        got = autopush.decide(record(updated=None), [], now=NOW)
        self.assertEqual((got.action, got.reason), ("push", "stalled_no_signal"))
        self.assertIsNone(got.idle_minutes)

    def test_fallback_time_is_the_last_resort(self):
        got = autopush.decide(record(updated=None), [], now=NOW,
                              fallback_time="2026-11-02T08:00:00+08:00")
        self.assertEqual(got.action, "push")
        self.assertAlmostEqual(got.idle_minutes, 60.0, places=1)


class EventFileTests(unittest.TestCase):
    def test_parse_time_accepts_both_shapes(self):
        self.assertEqual(autopush.parse_time("2026-11-02T09:43:52+0800").hour, 9)
        self.assertEqual(autopush.parse_time("2026-11-02T09:46:00+08:00").minute, 46)
        self.assertIsNotNone(autopush.parse_time("2026-11-02T01:00:00Z"))
        self.assertIsNone(autopush.parse_time("not a time"))
        self.assertIsNone(autopush.parse_time(""))

    def test_read_events_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "20261102.jsonl"
            path.write_text('{"time": "2026-11-02T08:00:00+0800", "endpoint": "workbuddy"}\n'
                            'not json\n\n'
                            '{"time": "2026-11-02T07:00:00+0800", "endpoint": "grok"}\n',
                            encoding="utf-8")
            rows = autopush.read_events(tmp)
            self.assertEqual([row["endpoint"] for row in rows], ["grok", "workbuddy"])

    def test_read_progress_takes_a_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "progress.json"
            target.write_text(json.dumps(record()), encoding="utf-8")
            self.assertEqual(autopush.read_progress(target)["card_id"], "08")
            self.assertEqual(autopush.read_progress(Path(tmp) / "missing.json"), {})


# ================================================================ 3. 命令行
class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.progress = self.root / "progress.json"
        self.progress.write_text(json.dumps(record()), encoding="utf-8")
        self.scripts = self.root / "src/scripts"
        self.scripts.mkdir(parents=True)
        (self.root / "docs").mkdir()
        tiers = autopush_cli.tier_guard.load_tiers()
        tiers["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=["fixture-low"], review=["fixture-high"], accept=["fixture-high"])
        (self.root / "docs/tiers.json").write_text(json.dumps(tiers), encoding="utf-8")
        patcher = mock.patch.object(autopush_cli, "SCRIPTS", self.scripts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.reply = (0, '{"ok": true, "stage": "answered", "submission": "accepted", "sent": true}', "")

    def runner(self, cmd, **_kwargs):
        self.calls.append(cmd)
        if Path(cmd[1]).name == "workbuddy_choose.py":
            target = cmd[-1]
            autopush_cli.tier_guard.write_receipt("workbuddy", target=target, readback=target, root=self.root)
            return 0, '{"ok":true}', ""
        return self.reply

    def run_cli(self, *extra, now=NOW):
        buffer = io.StringIO()
        argv = ["--progress", str(self.progress), "--runs", str(self.runs), "--now", now]
        with contextlib.redirect_stdout(buffer):
            code = autopush_cli.main(argv + list(extra), runner=self.runner)
        return code, json.loads(buffer.getvalue().strip().splitlines()[-1])

    def test_dry_run_decides_without_touching_anything(self):
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["action"], "push")
        self.assertIn("workbuddy_use.py", out["would_run"])
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.runs.glob("*.jsonl")), [])

    def test_dry_run_on_a_stop_still_sends_nothing(self):
        write_events(self.runs, [line("2026-11-02T07:10:00+0800")] + [
            push_line("2026-11-02T07:30:00+0800"),
            push_line("2026-11-02T08:10:00+0800"),
            push_line("2026-11-02T08:50:00+0800")])
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual((out["action"], out["reason"]), ("stop", "no_progress"))
        self.assertEqual(self.calls, [])

    def test_real_push_calls_workbuddy_use_continue(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(out["pushed"])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("workbuddy_choose.py", self.calls[0][1])
        cmd = self.calls[1]
        self.assertIn("workbuddy_use.py", cmd[1])
        self.assertIn("--continue", cmd)
        self.assertIn(autopush.PROMPT, cmd)
        rows = read_lines(self.runs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "autopush_push")
        self.assertEqual(rows[0]["card"], "08")
        self.assertEqual(rows[0]["progress_mark"], UPDATED)

    def test_push_failure_exits_1_and_does_not_count(self):
        self.reply = (5, '{"ok": false, "stage": "tier_unconfirmed"}', "")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        rows = read_lines(self.runs)
        self.assertEqual(rows[0]["result"], "autopush_push_failed")
        decisions = autopush.decide(record(), rows, now="2026-11-02T09:00:00+0800")
        self.assertEqual(decisions.action, "push")           # 失败不占冷却，也不算一次推

    def test_wait_exits_0_without_calling_anything(self):
        write_events(self.runs, [line("2026-11-02T08:55:00+0800")])
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(out["reason"], "recent_activity")
        self.assertEqual(self.calls, [])

    def test_stop_notifies_once_and_exits_3(self):
        write_events(self.runs, [
            line("2026-11-02T07:10:00+0800"),
            push_line("2026-11-02T07:30:00+0800"),
            push_line("2026-11-02T08:10:00+0800"),
            push_line("2026-11-02T08:50:00+0800")])
        code, out = self.run_cli(now="2026-11-02T09:30:00+0800")
        self.assertEqual(code, 3)
        self.assertTrue(out["needs_human"])
        self.assertTrue(out["notified"])
        cmd = self.calls[0]
        self.assertIn("notify_done.py", cmd[1])
        self.assertEqual(cmd[cmd.index("--card") + 1], "08")
        self.assertEqual(cmd[cmd.index("--status") + 1], "stuck")
        self.assertIn("红莉栖", cmd[cmd.index("--note") + 1])
        stops = [row for row in read_lines(self.runs) if row["result"] == "autopush_stop"]
        self.assertEqual(len(stops), 1)

        code, out = self.run_cli(now="2026-11-02T09:31:00+0800")
        self.assertEqual(code, 3)
        self.assertTrue(out["already_notified"])
        self.assertEqual(len(self.calls), 1)                 # 第二次不再叫人

    def test_stop_can_skip_notification(self):
        write_events(self.runs, [
            line("2026-11-02T07:10:00+0800"),
            push_line("2026-11-02T07:30:00+0800"),
            push_line("2026-11-02T08:10:00+0800"),
            push_line("2026-11-02T08:50:00+0800")])
        code, out = self.run_cli("--no-notify", now="2026-11-02T09:30:00+0800")
        self.assertEqual(code, 3)
        self.assertFalse(out["notified"])
        self.assertEqual(self.calls, [])

    def test_missing_progress_is_a_quiet_skip(self):
        self.progress.unlink()
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out["reason"], "not_running")

    def test_push_command_purpose_is_configurable(self):
        self.run_cli("--purpose", "test")
        self.assertIn("--purpose", self.calls[-1])
        self.assertEqual(self.calls[-1][self.calls[-1].index("--purpose") + 1], "test")


# ================================================================ 4. 接线
class EntryWiringTests(unittest.TestCase):
    def test_every_entry_calls_console_setup(self):
        entries = sorted(list(SCRIPTS.glob("*_use.py")) + list(SCRIPTS.glob("*_choose.py")))
        self.assertEqual(len(entries), 17, [path.name for path in entries])
        for path in entries:
            text = path.read_text(encoding="utf-8")
            tail = "\n".join(text.splitlines()[-24:])
            wired = "console.setup()" in tail or "_setup_console()" in tail
            self.assertTrue(wired, f"{path.name} 的入口没装编码")
            self.assertNotIn("runpy", text, f"{path.name} 别用 runpy 叠壳")

    def test_autopush_script_also_sets_up_console(self):
        text = (SCRIPTS / "workbuddy_autopush.py").read_text(encoding="utf-8")
        self.assertIn("console.setup()", text)
        self.assertNotIn("runpy", text)


if __name__ == "__main__":
    unittest.main()
