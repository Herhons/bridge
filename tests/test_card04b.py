"""任务卡 04b：三端的**初始化**异常也必须留一行运行日志。

CARD-04 被打回的第 2 条：初始化（打开窗口、建会话）在异常保护区外面，
离线注入初始化异常时三端 runlog.record 的调用次数都是 0。

这里对每个端各注入两个位置的异常（找窗口 / 建 Desk），断言三件事：
    1. runlog.record 恰好被调用 1 次（不是 0，也不是 2）；
    2. 落盘那一行是 stage=exception，error 里带异常类型和原文；
    3. 异常照原样抛出去——同一个实例、同一个类型，没被包成别的 stage/退出码。

全部打桩：不开窗口、不截图、不点击、不往真 `runs\` 写（日志落在临时目录）。
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
    name = f"card04b_{endpoint}_use"
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
def bench(endpoint: str, folder, *, break_name: str, error: Exception):
    """把窗口/前台/Desk/日志落地位置全换成假的，只让 `break_name` 那一步抛 `error`。"""
    module = MODULES[endpoint]

    def boom(*_args, **_kwargs):
        raise error

    stubs = {"find_window": dict(return_value=1),
             "rect_of": dict(return_value=(0, 0, 1400, 1340)),
             "activate": dict(return_value=True),
             "Desk": dict(return_value=mock.MagicMock()),
             "locate": dict(return_value={"missing": []})}
    stubs[break_name] = dict(side_effect=boom)

    def write(name, result, *, root=None):
        return RECORD(name, result, root=Path(folder))

    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("time.sleep"))
        for name, kwargs in stubs.items():
            if hasattr(module, name):           # 只有 codex 有 locate，dsh/opencode 没有
                stack.enter_context(mock.patch.object(module, name, **kwargs))
        recorded = stack.enter_context(mock.patch.object(runlog, "record", side_effect=write))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        yield SimpleNamespace(module=module, out=out, recorded=recorded)


class InitFailureLeavesOneLine(unittest.TestCase):
    """初始化抛异常：记一行，然后照原样退出。"""

    def test_init_exception_records_exactly_once(self):
        for endpoint in FILES:
            # 两个位置各来一次：找窗口（最开始）、建 Desk（窗口已经找到之后）
            for break_name, error in (("find_window", OSError("注入：找窗口炸了")),
                                      ("Desk", RuntimeError("注入：建会话炸了"))):
                with self.subTest(endpoint=endpoint, at=break_name), \
                        tempfile.TemporaryDirectory() as tmp:
                    with bench(endpoint, tmp, break_name=break_name,
                               error=error) as harness:
                        with self.assertRaises(type(error)) as ctx:
                            # --force 跳过"等你空闲"那一段；--list 是最轻的动作
                            harness.module.main(["--list", "--force"])

                    self.assertIs(ctx.exception, error, "异常被包过就不是原样退出了")
                    self.assertEqual(harness.recorded.call_count, 1)
                    self.assertEqual(harness.recorded.call_args.args[0], endpoint)

                    rows = read_log(tmp)
                    self.assertEqual(len(rows), 1, "初始化异常应当只记一行")
                    self.assertEqual(rows[0]["endpoint"], endpoint)
                    self.assertFalse(rows[0]["ok"])
                    self.assertEqual(rows[0]["result"], "exception")
                    self.assertIn(type(error).__name__, rows[0]["error"])
                    self.assertIn(str(error), rows[0]["error"])

    def test_init_failure_prints_nothing_but_still_logs(self):
        """初始化就炸时 stdout 不该冒出成功 JSON——日志归日志，输出别骗人。"""
        for endpoint in FILES:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                error = OSError("注入：找窗口炸了")
                with bench(endpoint, tmp, break_name="find_window", error=error) as harness:
                    with self.assertRaises(OSError):
                        harness.module.main(["--probe", "--force"])
                self.assertEqual(harness.out.getvalue().strip(), "")
                self.assertEqual(len(read_log(tmp)), 1)


if __name__ == "__main__":
    unittest.main()
