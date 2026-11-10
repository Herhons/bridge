"""B16-S4：`BRIDGE_STALL_BUSY` 必须下发给 `_run_use`。

B15 只在 `_detect_writer`（`choose --list`）里发过这个环境变量；`_run_use`
漏了。于是 `docs/bridge_server.json` 里的 `stall_busy_ignore={"codex":45}`
对 codex 的 use **完全没有作用** —— D4b 那轮 codex 300s 超时，
`conversation.py` 从头到尾没读到过这个开关。
"""
import json
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
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
        self.calls.append({"cmd": list(cmd), "env": dict(env or {})})
        return {"ok": True, "timeout": False, "exit_code": 0,
                "stdout": '{"ok": true, "stage": "answered", "reply": "R"}',
                "stderr": "", "elapsed": 0.1}


def make_engine(**overrides):
    tmp = Path(tempfile.mkdtemp(prefix="b16-stall-"))
    scripts = tmp / "src" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("codex_use.py", "workbuddy_use.py"):
        (scripts / name).write_text("", encoding="utf-8")
    runner = Recorder()
    engine = server.Engine(root=tmp, token="fixture", port=0, runner=runner)
    engine.config.update(overrides)
    return engine, runner


class StallEnvTests(unittest.TestCase):
    def _env(self, endpoint, **overrides):
        defaults = {"endpoint_concurrency": 4,
                    "stall_busy_ignore": {"codex": 45.0}}
        defaults.update(overrides)
        engine, runner = make_engine(**defaults)
        engine._run_use(endpoint, "probe", "test", time.monotonic() + 30)
        self.assertEqual(len(runner.calls), 1)
        return runner.calls[0]["env"]

    def test_configured_endpoint_gets_the_switch(self):
        env = self._env("codex")
        self.assertEqual(env["BRIDGE_STALL_BUSY"], "45.0")

    def test_other_endpoint_gets_nothing(self):
        self.assertNotIn("BRIDGE_STALL_BUSY", self._env("workbuddy"))

    def test_single_seat_mode_does_not_set_it(self):
        self.assertNotIn("BRIDGE_STALL_BUSY",
                         self._env("codex", endpoint_concurrency=1))

    def test_empty_config_does_not_set_it(self):
        self.assertNotIn("BRIDGE_STALL_BUSY",
                         self._env("codex", stall_busy_ignore={}))

    def test_bad_value_is_ignored_without_raising(self):
        self.assertNotIn("BRIDGE_STALL_BUSY",
                         self._env("codex", stall_busy_ignore={"codex": "oops"}))

    def test_shipped_config_enables_dsh_busy_stall(self):
        """R4b：dsh 在 4 席并发下 busy 恒真卡满 120s，需按端忽略误报。"""
        cfg = json.loads((ROOT / "docs" / "bridge_server.json").read_text(
            encoding="utf-8-sig"))
        self.assertGreater(cfg["stall_busy_ignore"]["dsh"], 0)

    def test_use_still_gets_the_section_switches(self):
        env = self._env("codex")
        self.assertEqual(env["BRIDGE_UI_SECTIONED"], "1")
        self.assertEqual(env["BRIDGE_UI_CONCURRENT"], "1")
        # Engine 用的是 DEFAULT_CONFIG（不看 docs/bridge_server.json），别写死 tabs
        self.assertEqual(env["BRIDGE_WEB_WINDOW_MODE"], "per_site")

    def test_window_mode_is_forwarded_from_config(self):
        env = self._env("codex", web_window_mode="tabs")
        self.assertEqual(env["BRIDGE_WEB_WINDOW_MODE"], "tabs")


if __name__ == "__main__":
    unittest.main()
