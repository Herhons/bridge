"""B14-a：后端常驻（DETACHED_PROCESS + breakaway 回退）。

**为什么有这批**：2026-11-08 实测 `bridge_server.py start` 起的后端会随父 shell
退出被杀（`server.pid` 残留、1876x 无监听、`server.log` 无 traceback ⇒ 没走
`serve()` 的 `finally`）。根因是 `Popen` 只带了 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`，
没有 `DETACHED_PROCESS`、也没有 `CREATE_BREAKAWAY_FROM_JOB`。

离线只能钉住「我们传了什么」，`DETACHED` 的真实效果必须真机验（P0-1）。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = SRC / "scripts"
for entry in (str(SRC), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qi import uilock  # noqa: E402

import bridge_server as bridge_cli  # noqa: E402

DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _oserror(winerror):
    exc = OSError(winerror, "spawn refused")
    exc.winerror = winerror
    return exc


def _emit_json(func, *args, **kwargs):
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = func(*args, **kwargs)
    return code, json.loads(stream.getvalue())


class CreationFlagTests(unittest.TestCase):
    def test_plain_mode_is_detached_only(self):
        flags = bridge_cli._creationflags(breakaway=False)
        if os.name != "nt":
            self.assertEqual(flags, 0)
            return
        self.assertEqual(flags, DETACHED_PROCESS,
                         "只有 DETACHED 才能让子进程不继承父控制台")

    def test_breakaway_mode_adds_the_job_escape_flag(self):
        flags = bridge_cli._creationflags(breakaway=True)
        if os.name != "nt":
            self.assertEqual(flags, 0)
            return
        self.assertEqual(flags, DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB)

    def test_no_window_and_process_group_flags_are_gone(self):
        """这两个是**故意删掉**的：前者与 DETACHED 同用会被忽略，后者对杀树无效。"""
        flags = bridge_cli._creationflags(breakaway=True)
        self.assertFalse(flags & CREATE_NO_WINDOW, "CREATE_NO_WINDOW 与 DETACHED 同用会被忽略")
        self.assertFalse(flags & CREATE_NEW_PROCESS_GROUP, "它只影响 Ctrl+C 分发组")


class SpawnFallbackTests(unittest.TestCase):
    def test_access_denied_falls_back_to_plain_detached(self):
        """父 job 不允许 breakaway 时，系统会拒绝；必须降级重试一次。"""
        seen = []

        def fake_popen(argv, **kwargs):
            seen.append(kwargs.get("creationflags"))
            if len(seen) == 1:
                raise _oserror(5)
            return "child-handle"

        with mock.patch.object(bridge_cli.subprocess, "Popen", side_effect=fake_popen):
            child, how = bridge_cli._spawn_detached(["py", "-m", "qi.server"], None)

        self.assertEqual(child, "child-handle")
        self.assertEqual(how, "plain")
        self.assertEqual(len(seen), 2, "只允许降级重试一次")
        if os.name == "nt":
            self.assertEqual(seen[0], DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB)
            self.assertEqual(seen[1], DETACHED_PROCESS)

    def test_breakaway_success_reports_breakaway(self):
        with mock.patch.object(bridge_cli.subprocess, "Popen",
                               return_value="child") as popen:
            child, how = bridge_cli._spawn_detached(["py"], None)
        self.assertEqual((child, how), ("child", "breakaway"))
        self.assertEqual(popen.call_count, 1)

    def test_other_oserror_is_not_swallowed(self):
        """不是 ERROR_ACCESS_DENIED 的失败必须原样抛，不能假装降级能救。"""
        with mock.patch.object(bridge_cli.subprocess, "Popen",
                               side_effect=_oserror(2)) as popen:
            with self.assertRaises(OSError):
                bridge_cli._spawn_detached(["py"], None)
        self.assertEqual(popen.call_count, 1)


class StartPayloadTests(unittest.TestCase):
    def _patches(self, tmp, health, child):
        return [
            mock.patch.object(bridge_cli.server, "load_config",
                              return_value={"port": 18765}),
            mock.patch.object(bridge_cli.server, "read_pid", return_value=None),
            mock.patch.object(bridge_cli, "_port_free", return_value=(True, "")),
            mock.patch.object(bridge_cli, "_health", return_value=health),
            mock.patch.object(bridge_cli, "LOG_PATH", Path(tmp) / "server.log"),
            mock.patch.object(bridge_cli, "_spawn_detached", return_value=child),
            mock.patch.object(bridge_cli.server, "terminate_tree"),
        ]

    def test_start_reports_the_real_pid(self):
        """`/health` 只回 {ok, version} 是契约，所以 pid 只能取自 child。

        改动前这里写的是 `health.get("pid")`，恒为 null —— 真机日志里就是这个现象。
        """
        tmp = tempfile.mkdtemp(prefix="b14-start-")
        child = mock.Mock(pid=4242, poll=lambda: None)
        patches = self._patches(tmp, (True, {"ok": True, "version": "0.0.9"}, ""), (child, "breakaway"))
        for item in patches[:-1]:
            item.start()
            self.addCleanup(item.stop)
        code, payload = _emit_json(bridge_cli.start)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pid"], 4242)
        self.assertEqual(payload["detached"], "breakaway")
        self.assertEqual(payload["port"], 18765)

    def test_unhealthy_child_is_killed_and_reported(self):
        tmp = tempfile.mkdtemp(prefix="b14-start-")
        child = mock.Mock(pid=7, poll=lambda: 1)
        patches = self._patches(tmp, (False, None, "connection refused"), (child, "plain"))
        with mock.patch.object(bridge_cli.server, "terminate_tree") as kill:
            for item in patches[:-1]:
                item.start()
                self.addCleanup(item.stop)
            code, payload = _emit_json(bridge_cli.start)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "start_failed")
        kill.assert_called_once_with(child)


class StatusPayloadTests(unittest.TestCase):
    def _run(self, info, health, alive):
        with mock.patch.object(bridge_cli.server, "read_pid", return_value=info), \
             mock.patch.object(bridge_cli, "_health", return_value=health), \
             mock.patch.object(bridge_cli.uilock, "pid_alive", return_value=alive), \
             mock.patch.object(bridge_cli.server, "load_config",
                               return_value={"port": 18765}):
            return _emit_json(bridge_cli.status)

    def test_healthy_matching_pid(self):
        code, payload = self._run({"pid": 11, "port": 18765},
                                  (True, {"ok": True}, ""), True)
        self.assertEqual(code, 0)
        self.assertTrue(payload["running"])
        self.assertTrue(payload["pid_alive"])
        self.assertFalse(payload["mismatch"])
        self.assertFalse(payload["stale_pid_file"])

    def test_dead_pid_file_is_marked_stale(self):
        code, payload = self._run({"pid": 11, "port": 18765},
                                  (False, None, "refused"), False)
        self.assertEqual(code, 1)
        self.assertFalse(payload["running"])
        self.assertTrue(payload["stale_pid_file"])
        self.assertFalse(payload["mismatch"])

    def test_health_up_but_pid_dead_is_a_mismatch(self):
        """端口有人应答、但不是 pid 文件里那个进程 —— 最常见是另一份 checkout。"""
        code, payload = self._run({"pid": 11, "port": 18765},
                                  (True, {"ok": True}, ""), False)
        self.assertEqual(code, 0)
        self.assertTrue(payload["mismatch"])

    def test_unknown_liveness_is_not_treated_as_alive(self):
        """`pid_alive` 返回 None（问不动）不等于活着。"""
        _code, payload = self._run({"pid": 11, "port": 18765},
                                   (True, {"ok": True}, ""), None)
        self.assertIsNone(payload["pid_alive"])
        self.assertTrue(payload["mismatch"])
        self.assertTrue(payload["stale_pid_file"])


class LockTimeoutConstantTests(unittest.TestCase):
    def test_section_timeout_covers_a_choose_run(self):
        """并发时 choose 整轮持锁；发送段只等 20s 会白丢一次发送，所以抬到 60。"""
        self.assertGreaterEqual(uilock.SECTION_TIMEOUT, 60.0)


if __name__ == "__main__":
    unittest.main()
