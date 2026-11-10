"""B16-S4：失败原因不许在服务端被丢掉。

- gemini 的 `prepare_failed` 把 `prepare` 明细放在 stdout，但 `_run_use` 的
  detail 白名单没有 `prepare`，job 里只剩 `detail=None`（W4g）。
- dsh/grok 的 choose 失败若没有 hint，错误就退化成裸 `choose exit code 1`；
  现在把 stdout 末几行带出来，至少能看到脚本自己报的 stage。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import server  # noqa: E402


class Recorder:
    def __init__(self, *, stdout="", stderr="", exit_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.calls = []

    def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
        self.calls.append({"cmd": list(cmd), "env": dict(env or {})})
        return {"ok": self.exit_code == 0, "timeout": False,
                "exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr, "elapsed": 0.1}


def make_engine(script, runner):
    tmp = Path(tempfile.mkdtemp(prefix="b16-detail-"))
    scripts = tmp / "src" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / script).write_text("", encoding="utf-8")
    return server.Engine(root=tmp, token="fixture", port=0, runner=runner)


class UseDetailTests(unittest.TestCase):
    def test_prepare_failure_detail_survives(self):
        stdout = ('{"ok": false, "stage": "prepare_failed", '
                  '"prepare": {"stage": "unreadable", "score": 0.41}}')
        runner = Recorder(stdout=stdout, exit_code=1)
        engine = make_engine("gemini_use.py", runner)
        out = engine._run_use("gemini", "probe", "test", time.monotonic() + 30)
        self.assertEqual(out["error_code"], "prepare_failed")
        self.assertEqual(out["detail"]["prepare"]["stage"], "unreadable")

    def test_normal_use_detail_is_unchanged(self):
        runner = Recorder(stdout='{"ok": true, "stage": "answered", '
                                 '"reply": "R", "settle_basis": "text_stable"}')
        engine = make_engine("gemini_use.py", runner)
        out = engine._run_use("gemini", "probe", "test", time.monotonic() + 30)
        self.assertTrue(out["ok"])
        self.assertEqual(out["detail"]["settle_basis"], "text_stable")


class ChooseMessageTests(unittest.TestCase):
    def test_no_hint_falls_back_to_stdout_tail(self):
        runner = Recorder(stdout='template report\n{"ok": false, "stage": "menu_not_open"}\n',
                          exit_code=1)
        engine = make_engine("dsh_choose.py", runner)
        out = engine._run_choose("dsh", "model", "fast", time.monotonic() + 30)
        self.assertEqual(out["error_code"], "choose_failed")
        self.assertIn("choose exit code 1", out["error_message"])
        self.assertIn("menu_not_open", out["error_message"])

    def test_hint_wins_over_stdout_tail(self):
        runner = Recorder(stdout='{"ok": false, "stage": "menu_not_open", '
                                 '"hint": "窗口不在前台"}\n', exit_code=1)
        engine = make_engine("grok_choose.py", runner)
        out = engine._run_choose("grok", "model", "fast", time.monotonic() + 30)
        self.assertEqual(out["error_message"], "窗口不在前台")

    def test_empty_output_keeps_the_bare_code(self):
        runner = Recorder(stdout="", stderr="", exit_code=1)
        engine = make_engine("dsh_choose.py", runner)
        out = engine._run_choose("dsh", "model", "fast", time.monotonic() + 30)
        self.assertEqual(out["error_message"], "choose exit code 1")


if __name__ == "__main__":
    unittest.main()
