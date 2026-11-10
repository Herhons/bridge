"""B14-c：choose 子进程的等锁上限。

**为什么**：`_run_choose` 原来只传 `timeout/cwd`，不给子进程任何 `BRIDGE_UI_*`。
于是 choose 走 `uilock.entry` 的**默认整轮持锁**，等锁上限是 `DEFAULT_TIMEOUT=60s`。
4 席并发时后到的 choose 要等前面几个跑完，超过 60s 就变成退出码 8（`ui_busy`）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import server, uilock  # noqa: E402


class Recorder:
    """记下调用参数；不真起进程。"""

    def __init__(self, stdout='{"ok": true, "verified": true}'):
        self.calls = []
        self.stdout = stdout

    def __call__(self, cmd, *, timeout, cwd=None, env=None, tick=None):
        self.calls.append({"cmd": list(cmd), "timeout": timeout, "env": dict(env or {})})
        return {"ok": True, "timeout": False, "exit_code": 0,
                "stdout": self.stdout, "stderr": "", "elapsed": 0.1}


def make_engine(**overrides):
    tmp = Path(tempfile.mkdtemp(prefix="b14-choose-"))
    scripts = tmp / "src" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "grok_choose.py").write_text("", encoding="utf-8")
    runner = Recorder()
    engine = server.Engine(root=tmp, token="fixture", port=0, runner=runner)
    engine.config.update(overrides)
    return engine, runner


class ChooseEnvTests(unittest.TestCase):
    def test_child_gets_the_lock_timeout_but_not_the_concurrency_switches(self):
        engine, runner = make_engine()
        engine._run_choose("grok", "model", "grok-4", time.monotonic() + 30)

        self.assertEqual(len(runner.calls), 1)
        env = runner.calls[0]["env"]
        limit = int(env["BRIDGE_UI_LOCK_TIMEOUT"])
        # 与它自己的预算对齐（留 2s 余量），而不是默认的 60s
        self.assertEqual(limit, max(1, int(runner.calls[0]["timeout"]) - 2))
        self.assertLessEqual(limit, 30)

        # choose 必须**整轮持锁**：不能把分段/不唤醒/一端一窗开给它
        for key in ("BRIDGE_UI_SECTIONED", "BRIDGE_UI_WAKE", "BRIDGE_UI_CONCURRENT"):
            self.assertNotIn(key, env, f"{key} 不该下发给 choose")

    def test_lock_timeout_shrinks_with_the_remaining_budget(self):
        engine, runner = make_engine()
        engine._run_choose("grok", "model", "grok-4", time.monotonic() + 12)
        limit = int(runner.calls[0]["env"]["BRIDGE_UI_LOCK_TIMEOUT"])
        self.assertLessEqual(limit, 10)
        self.assertGreaterEqual(limit, 1, "必须留出时间让它能先报 Busy，而不是被砍掉")

    def test_choose_keeps_the_default_lock_wait_below_sixty(self):
        """默认常量不能被悄悄改小：整轮持锁的脚本本来就该拿到 60s 基准。"""
        self.assertEqual(uilock.DEFAULT_TIMEOUT, 60.0)


class TurnLockDelegationTests(unittest.TestCase):
    """回归：`entry` 先取同端回合锁，而委派凭证只对主锁有效。

    2026-11-08 真机实测：服务端派发的**每个** choose 子进程都在这一步
    `exit 8 / {"stage":"ui_busy","error":"UI delegation root/path mismatch"}`，
    因为 `turn()` 拿主锁的委派路径去比 `runs/turn/<端>.lock`。
    """

    def test_turn_ignores_a_delegation_grant(self):
        grant = json.dumps({"path": str(uilock.lock_path().resolve()),
                            "pid": 1, "process_start_time": 1,
                            "session": "s", "token": "t"})
        with mock.patch.dict(os.environ, {uilock.DELEGATION_ENV: grant}):
            with uilock.turn("probe-endpoint", timeout=2) as meta:
                self.assertIsNotNone(meta, "回合锁必须能正常拿到")

    def test_main_hold_still_rejects_a_foreign_grant(self):
        """主锁那边不能放宽：路径对不上就必须立刻拒绝，绝不能退化成普通取锁。"""
        grant = json.dumps({"path": str(uilock.lock_path().resolve().with_name("other.lock")),
                            "pid": 1, "process_start_time": 1,
                            "session": "s", "token": "t"})
        with mock.patch.dict(os.environ, {uilock.DELEGATION_ENV: grant}):
            with self.assertRaises(uilock.Busy):
                with uilock.hold("probe", timeout=1):
                    pass


if __name__ == "__main__":
    unittest.main()
