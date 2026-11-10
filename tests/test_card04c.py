"""任务卡 04c：把 04b 复验打回的两条钉住。

打回的是这两条（都属第 4 项「运行日志」）：

1. **异常仍可能漏记**：三端的「空闲等待」还压在异常保护区外面。往 `idle_seconds()`
   注入异常后，三端 `runlog.record` 的调用次数都是 0——一次运行连一行日志都没有。
2. **已有的错误信息被丢弃**：`codex_use --list` 打不开卡片时，打印出去的 JSON 里
   明明有 `hint="打不开卡片"`、退出码 1，写进运行日志的却只有 ok/stage/elapsed，
   `error` 是 null。

这里的测试对每个端各注入一次「等空闲」异常，断言 record 恰好 1 次、落盘那行是
stage=exception 且 error 里带异常类型和原文、异常照原样抛出；再对 codex 的
`--list` 失败路径断言 error 非空且包含那句 hint。

全部打桩：不开窗口、不截图、不点击、不往真 `runs\\` 写（日志落在临时目录）。
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import runlog  # noqa: E402

FILES = {"codex": "codex_use.py", "dsh": "dsh_use.py", "opencode": "opencode_use.py"}
RECORD = runlog.record          # 打桩前抓住原函数：桩里再调 runlog.record 会递归


def load(endpoint: str):
    """按路径把单文件脚本装成模块（它们不在包里，也不自己 import 项目代码）。"""
    path = ROOT / "src" / "scripts" / FILES[endpoint]
    name = f"card04c_{endpoint}_use"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULES = {endpoint: load(endpoint) for endpoint in FILES}


def read_log(folder) -> list[dict]:
    rows = []
    for path in sorted(Path(folder).glob("*.jsonl")):
        rows += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    return rows


@contextlib.contextmanager
def bench(endpoint: str, folder, *, idle=None, extra=None):
    """把窗口/前台/Desk/日志落地位置全换成假的；`idle` 给了就顶掉 idle_seconds。"""
    module = MODULES[endpoint]

    def write(name, result, *, root=None):
        return RECORD(name, result, root=Path(folder))

    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("time.sleep"))
        stack.enter_context(mock.patch.object(module, "find_window", return_value=1))
        stack.enter_context(mock.patch.object(module, "rect_of",
                                             return_value=(0, 0, 1400, 1340)))
        stack.enter_context(mock.patch.object(module, "activate", return_value=True))
        stack.enter_context(mock.patch.object(module, "Desk", return_value=mock.MagicMock()))
        if hasattr(module, "locate"):                  # 只有 codex 有模板定位
            stack.enter_context(mock.patch.object(module, "locate",
                                                  return_value={"missing": []}))
        if idle is None:
            stack.enter_context(mock.patch.object(module, "idle_seconds",
                                                  return_value=999.0))
        else:
            stack.enter_context(mock.patch.object(module, "idle_seconds", side_effect=idle))
        for name, kwargs in (extra or {}).items():
            # 支持 "cv2.imwrite" 这种带点号的：先在模块里取到 cv2，再patch它的属性
            holder, _, attr = name.partition(".")
            if attr:
                stack.enter_context(mock.patch.object(getattr(module, holder), attr,
                                                      **kwargs))
            else:
                stack.enter_context(mock.patch.object(module, name, **kwargs))
        recorded = stack.enter_context(mock.patch.object(runlog, "record", side_effect=write))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        yield SimpleNamespace(module=module, out=out, recorded=recorded)


class IdleWaitIsProtected(unittest.TestCase):
    """「等你空闲」也必须留痕：这一条就是打回的第 1 条。"""

    def test_idle_exception_records_exactly_once(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), \
                    tempfile.TemporaryDirectory() as tmp:
                error = OSError("注入：读空闲时间炸了")

                def boom(*_args, **_kwargs):
                    raise error

                with bench(endpoint, tmp, idle=boom) as harness:
                    with self.assertRaises(OSError) as ctx:
                        # 不带 --force 才会走「等你空闲」那段循环
                        harness.module.main(["--list"])
                    # 异常是在碰窗口之前抛的：证明命中的是等待那一段
                    harness.module.find_window.assert_not_called()

                self.assertIs(ctx.exception, error, "异常被包过就不是原样退出了")
                self.assertEqual(harness.recorded.call_count, 1)
                self.assertEqual(harness.recorded.call_args.args[0], endpoint)

                rows = read_log(tmp)
                self.assertEqual(len(rows), 1, "等空闲时炸了也应当正好记一行")
                self.assertEqual(rows[0]["endpoint"], endpoint)
                self.assertFalse(rows[0]["ok"])
                self.assertEqual(rows[0]["result"], "exception")
                self.assertIn("OSError", rows[0]["error"])
                self.assertIn("读空闲时间炸了", rows[0]["error"])

    def test_idle_failure_prints_nothing_but_still_logs(self):
        """等待阶段炸了不该往 stdout 冒成功 JSON——日志归日志，输出别骗人。"""
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                def boom(*_args, **_kwargs):
                    raise RuntimeError("注入：等空闲炸了")

                with bench(endpoint, tmp, idle=boom) as harness:
                    with self.assertRaises(RuntimeError):
                        harness.module.main(["--list"])
                    printed = harness.out.getvalue().strip()

                self.assertEqual(printed, "")
                self.assertEqual(len(read_log(tmp)), 1)

    def test_each_run_gets_its_own_line(self):
        """闸门是「一次运行一行」，不能串到下一次运行去。"""
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp) as harness:
                    self.assertEqual(harness.module.main([]), 2)
                    self.assertEqual(harness.module.main([]), 2)
                    self.assertEqual(harness.recorded.call_count, 2)
                self.assertEqual(len(read_log(tmp)), 2)


class ListFailureKeepsHint(unittest.TestCase):
    """codex `--list` 失败：打印出去的那句 hint 必须也落在 error 里（打回的第 2 条）。"""

    def run_list(self, tmp, extra):
        with bench("codex", tmp, extra=extra) as harness:
            code = harness.module.main(["--list"])
        return code, harness, read_log(tmp)

    def test_open_card_failure_hint_reaches_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, harness, rows = self.run_list(
                tmp, {"_open_card": dict(return_value=None)})
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["result"], "list")
        self.assertIsNotNone(rows[0]["error"], "error 不能是 null")
        self.assertIn("打不开卡片", rows[0]["error"])
        # 打印出去的那份 JSON 仍然带 hint，退出码也没变
        self.assertEqual(json.loads(harness.out.getvalue().strip())["hint"], "打不开卡片")

    def test_missing_model_list_hint_reaches_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _harness, rows = self.run_list(tmp, {
                "_open_card": dict(return_value="effort"),
                "find_slider": dict(return_value=(802, 1046, 1100, 992)),
                "_effort_now": dict(return_value={"effort": "max", "label": "Max",
                                                  "from": "text"}),
                "model_row": dict(return_value=1180),
                "items_visible": dict(return_value=False),
                "cv2.imwrite": dict(return_value=True),
            })
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("打不开模型列表", rows[0]["error"])

    def test_unreadable_model_still_has_error(self):
        """这一路本来就没有 hint：error 也不能留成 null。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, _harness, rows = self.run_list(tmp, {
                "_open_card": dict(return_value="effort"),
                "find_slider": dict(return_value=(802, 1046, 1100, 992)),
                "_effort_now": dict(return_value={"effort": "max", "label": "Max",
                                                  "from": "text"}),
                "model_row": dict(return_value=1180),
                "items_visible": dict(return_value=True),
                "read_model": dict(return_value={"model": "unknown"}),
                "cv2.imwrite": dict(return_value=True),
            })
        self.assertEqual(code, 1)
        self.assertTrue(rows[0]["error"], "失败行必须有 error")
        self.assertIn("unknown", rows[0]["error"])


class FailureNote(unittest.TestCase):
    """runlog 里那个兜底：先 hint、再 error、成功不写。"""

    def test_hint_wins_over_error(self):
        self.assertEqual(runlog.failure_note({"ok": False, "hint": "甲的提示",
                                              "error": "乙的错误"}), "甲的提示")

    def test_error_used_when_hint_missing(self):
        self.assertEqual(runlog.failure_note({"ok": False, "error": "炸了"}), "炸了")

    def test_stage_is_last_resort(self):
        note = runlog.failure_note({"ok": False, "stage": "no_window"})
        self.assertTrue(note)
        self.assertIn("no_window", note)

    def test_success_stays_empty(self):
        self.assertIsNone(runlog.failure_note({"ok": True, "stage": "answered"}))


if __name__ == "__main__":
    unittest.main()
