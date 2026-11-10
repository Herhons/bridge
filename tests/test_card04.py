"""任务卡 04：codex / dsh / opencode 三个单文件脚本接运行日志和超时分型。

全部打桩：不开窗口、不截图、不点击、不往真 `runs\\` 写（日志全落在临时目录）。
每个端至少覆盖三种情况——成功写一行、超时写一行且 timeout_kind 正确、
runlog 写失败时输出和退出码不变。
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
PROMPT = "只回复两个字：收到"
ANSWERED = {"ok": True, "stage": "answered", "submission": "accepted", "elapsed": 1.0}
TIMEOUT = {"ok": False, "stage": "timeout", "submission": "accepted", "sent": True}
# --list / --probe 只读模式的桩：三个端要假的东西不同。
# codex 的 --list 是「开卡片 -> 点模型行 -> 读列表」，所以桩要一路给到读模型那一步：
# 卡片能打开、模型行找得到、列表读得出 model。三端都该退 0。
READONLY = {"dsh": {"list_state": {"model": "deepseek_v41_flash"}, "probe": {}},
            "opencode": {"list_state": {"model": "deepseek_v41_flash_free"},
                         "panel_bbox": None, "probe": {},
                         # 2026-11-07：main 先认会话（首页 -> session_not_open）。
                         # 这是新增依赖的桩，断言一个字没改。
                         "session_state": {"state": "session"}},
            "codex": {"_open_card": "effort", "probe": [],
                      "find_slider": (100, 340, 1100, 200),
                      "_effort_now": {"effort": "max", "label": "Max"},
                      "model_row": 1180,
                      "items_visible": True,
                      "read_model": {"model": "gpt61_sol"}}}


def load(endpoint: str):
    """按路径把单文件脚本装成模块（它们不在包里，也不自己 import 项目代码）。"""
    path = ROOT / "src" / "scripts" / FILES[endpoint]
    name = f"card04_{endpoint}_use"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULES = {endpoint: load(endpoint) for endpoint in FILES}
RECORD = runlog.record          # 打桩前抓住原函数（见 write_to）


def read_log(folder) -> list[dict]:
    lines = []
    for path in sorted(Path(folder).glob("*.jsonl")):
        lines += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return lines


def log_text(folder) -> str:
    return "".join(path.read_text(encoding="utf-8")
                   for path in Path(folder).glob("*.jsonl"))


def last_json(text: str) -> dict:
    rows = [x for x in text.strip().splitlines() if x.strip()]
    return json.loads(rows[-1])


def write_to(folder):
    """把真的 record 指到临时目录：验的是真函数，只换了落地位置。

    先抓住原始函数再打桩——桩里再调 `runlog.record` 会调回桩自己，无限递归。
    """
    def record(endpoint, result, *, root=None):
        return RECORD(endpoint, result, root=Path(folder))
    return record


@contextlib.contextmanager
def bench(endpoint, folder, *, ask=None, record=None, extra=None):
    """把模块外面的世界全换成假的：窗口、前台、Desk、动作、日志落地位置。"""
    module = MODULES[endpoint]
    ask = ask or (lambda *args, **kwargs: dict(ANSWERED))
    record = record or write_to(folder)
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(module, "idle_seconds", return_value=999.0))
        stack.enter_context(mock.patch.object(module, "find_window", return_value=1))
        stack.enter_context(mock.patch.object(module, "rect_of", return_value=(0, 0, 1400, 1340)))
        stack.enter_context(mock.patch.object(module, "activate", return_value=True))
        stack.enter_context(mock.patch.object(module, "Desk", return_value=mock.MagicMock()))
        stack.enter_context(mock.patch("time.sleep"))      # 桩台不真等（0.7/0.8/1.3 秒那种）
        if hasattr(module, "locate"):               # 只有 codex 有模板定位
            stack.enter_context(mock.patch.object(module, "locate", return_value={"missing": []}))
        asked = stack.enter_context(mock.patch.object(module, "ask", side_effect=ask))
        recorded = stack.enter_context(mock.patch.object(runlog, "record", side_effect=record))
        for name, value in (extra or {}).items():
            stack.enter_context(mock.patch.object(module, name, return_value=value))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        yield SimpleNamespace(module=module, out=out, asked=asked, recorded=recorded)


class SuccessLogsOneLine(unittest.TestCase):
    """成功：一次运行落一行，带 ok / stage / elapsed，不写提问和回复正文。"""

    def test_one_line_per_run(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                answer = dict(ANSWERED, reply="秘密正文")
                with bench(endpoint, tmp, ask=lambda *a, **k: dict(answer)) as harness:
                    self.assertEqual(harness.module.main([PROMPT, "--purpose", "test"]), 0)
                harness.recorded.assert_called_once()
                self.assertEqual(harness.recorded.call_args.args[0], endpoint)
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["endpoint"], endpoint)
                self.assertTrue(lines[0]["ok"])
                self.assertEqual(lines[0]["result"], "answered")
                self.assertEqual(lines[0]["elapsed"], 1.0)
                text = log_text(tmp)
                self.assertNotIn(PROMPT, text)          # 提问不落盘
                self.assertNotIn("秘密正文", text)       # 回复正文不落盘

    def test_ask_adds_elapsed_and_normalises_kind(self):
        """ask() 自己补 elapsed / timeout_kind：conversation 只给阶段时也不留空。"""
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint):
                screen = mock.MagicMock()
                with mock.patch("qi.screen.Screen", return_value=screen), \
                     mock.patch("qi.conversation.ask",
                                return_value={"ok": True, "stage": "answered"}):
                    result = MODULES[endpoint].ask(
                        SimpleNamespace(hwnd=1), (0, 0, 100, 100), PROMPT, 5.0)
                self.assertEqual(result["stage"], "answered")
                self.assertIsInstance(result["elapsed"], float)

                with mock.patch("qi.screen.Screen", return_value=screen), \
                     mock.patch("qi.conversation.ask",
                                return_value={"ok": False, "stage": "timeout", "sent": True}):
                    result = MODULES[endpoint].ask(
                        SimpleNamespace(hwnd=1), (0, 0, 100, 100), PROMPT, 5.0)
                self.assertEqual((result["stage"], result["timeout_kind"]),
                                 ("timeout", "no_reply"))

                # conversation 判成"还在生成"时原样带出来，不覆盖
                with mock.patch("qi.screen.Screen", return_value=screen), \
                     mock.patch("qi.conversation.ask",
                                return_value={"ok": False, "stage": "timeout",
                                              "timeout_kind": "still_generating",
                                              "sent": True}):
                    result = MODULES[endpoint].ask(
                        SimpleNamespace(hwnd=1), (0, 0, 100, 100), PROMPT, 5.0)
                self.assertEqual(result["timeout_kind"], "still_generating")


class TimeoutKindRecorded(unittest.TestCase):
    """超时：stage 仍是 timeout（兼容旧调用），另给 timeout_kind。"""

    def test_still_generating_is_kept(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                answer = dict(TIMEOUT, timeout_kind="still_generating")
                with bench(endpoint, tmp, ask=lambda *a, **k: dict(answer)) as harness:
                    self.assertEqual(harness.module.main([PROMPT, "--purpose", "test"]), 1)
                printed = last_json(harness.out.getvalue())
                self.assertEqual((printed["stage"], printed["timeout_kind"]),
                                 ("timeout", "still_generating"))
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["result"], "timeout")
                self.assertEqual(lines[0]["timeout_kind"], "still_generating")
                self.assertFalse(lines[0]["ok"])

    def test_missing_kind_falls_back_to_no_reply(self):
        """结果里没这个字段时按 no_reply 记——宁可说没回复，也不冒充还在生成。

        打印的 stage 仍是 timeout（兼容旧调用）；补字段是日志这一侧的事。
        """
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp, ask=lambda *a, **k: dict(TIMEOUT)) as harness:
                    self.assertEqual(harness.module.main([PROMPT, "--purpose", "test"]), 1)
                self.assertEqual(last_json(harness.out.getvalue())["stage"], "timeout")
                self.assertEqual(read_log(tmp)[0]["timeout_kind"], "no_reply")


class RunlogFailureIsInvisible(unittest.TestCase):
    """写日志失败：打印内容和退出码一个字符都不能变。"""

    def test_output_and_exit_code_unchanged(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), \
                 tempfile.TemporaryDirectory() as good_dir, \
                 tempfile.TemporaryDirectory() as bad_dir:
                with bench(endpoint, good_dir) as harness:
                    good_code = harness.module.main([PROMPT, "--purpose", "test"])
                    good_out = harness.out.getvalue()

                def boom(endpoint_name, result, *, root=None):
                    raise OSError("disk full")

                with bench(endpoint, bad_dir, record=boom) as harness:
                    bad_code = harness.module.main([PROMPT, "--purpose", "test"])
                    bad_out = harness.out.getvalue()

                self.assertEqual((bad_code, bad_out), (good_code, good_out))
                self.assertEqual(read_log(bad_dir), [])      # 真的没写进去
                self.assertNotEqual(read_log(good_dir), [])


class ReadOnlyModes(unittest.TestCase):
    """--help 不记；--list / --probe / 参数错各记一行。"""

    def test_help_records_nothing(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp) as harness:
                    with self.assertRaises(SystemExit) as ctx:
                        harness.module.main(["--help"])
                self.assertEqual(ctx.exception.code, 0)
                harness.recorded.assert_not_called()
                self.assertIn("usage:", harness.out.getvalue())
                self.assertEqual(read_log(tmp), [])

    def test_list_records_one_line(self):
        # 三端都是 0：CARD-04b 删掉了「Codex 返回 1 算预期」那条断言——
        # 它把 CARD-02 修好之前的状态写成了预期，等于给失败盖章。
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp, extra=READONLY[endpoint]) as harness:
                    code = harness.module.main(["--list"])
                self.assertEqual(code, 0)
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["result"], "list")
                self.assertEqual(lines[0]["ok"], code == 0)

    def test_probe_records_one_line(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp, extra=READONLY[endpoint]) as harness:
                    self.assertEqual(harness.module.main(["--probe"]), 0)
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual((lines[0]["result"], lines[0]["ok"]), ("probe", True))

    def test_no_action_records_bad_args(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp) as harness:
                    self.assertEqual(harness.module.main([]), 2)
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual((lines[0]["result"], lines[0]["ok"]), ("bad_args", False))

    def test_argparse_error_records_bad_args(self):
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp) as harness:
                    with self.assertRaises(SystemExit) as ctx:
                        harness.module.main(["--不存在的选项"])
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(read_log(tmp)[0]["result"], "bad_args")


class ErrorsAreLogged(unittest.TestCase):
    def test_runtime_error_becomes_aborted(self):
        def boom(*args, **kwargs):
            raise RuntimeError("窗口不见了")

        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp, ask=boom) as harness:
                    self.assertEqual(harness.module.main([PROMPT, "--purpose", "test"]), 4)
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["result"], "aborted")
                self.assertIn("窗口不见了", lines[0]["error"])

    def test_unexpected_exception_is_logged_then_reraised(self):
        def boom(*args, **kwargs):
            raise ValueError("模板炸了")

        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                with bench(endpoint, tmp, ask=boom) as harness:
                    with self.assertRaises(ValueError):
                        harness.module.main([PROMPT, "--purpose", "test"])
                lines = read_log(tmp)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]["result"], "exception")
                self.assertIn("ValueError: 模板炸了", lines[0]["error"])


class WithTimeoutKind(unittest.TestCase):
    """runlog 里那个兜底函数：只补字段，不动已有值和别的阶段。"""

    def test_keeps_value_from_conversation(self):
        for kind in runlog.TIMEOUT_KINDS:
            result = runlog.with_timeout_kind({"stage": "timeout", "timeout_kind": kind})
            self.assertEqual(result["timeout_kind"], kind)

    def test_fills_only_timeouts(self):
        self.assertEqual(runlog.with_timeout_kind({"stage": "timeout"})["timeout_kind"],
                         "no_reply")
        self.assertEqual(runlog.with_timeout_kind(
            {"stage": "timeout", "timeout_kind": "乱写的"})["timeout_kind"], "no_reply")

    def test_other_stages_untouched(self):
        result = {"ok": True, "stage": "answered", "elapsed": 2.5}
        self.assertIs(runlog.with_timeout_kind(result), result)


if __name__ == "__main__":
    unittest.main()
