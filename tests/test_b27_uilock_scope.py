"""UI 锁的范围与残留清理（Round 0 修复第 3 项）。

规矩（Herhons 2026-11-12 批准）：
* 锁**只在**动网页（发送 / 收回复 / 上传）时持有；
* 离线步骤（验真、匿名、归档）**一律不取锁**；
* 续跑前检查锁里的 pid，确认持有者已死就清掉残留元数据。
"""
from offline_guard import ensure_offline
ensure_offline()

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import importlib.util                             # noqa: E402

from qi import uilock                             # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)                  # noqa: E402


def _fake_lock(root: Path, meta: dict) -> Path:
    path = root / "runs" / "ui.lock"
    handle = uilock._open(path)
    try:
        uilock._write_meta(handle, meta)
    finally:
        handle.close()
    return path


def _read_meta(root: Path) -> dict:
    handle = uilock._open(root / "runs" / "ui.lock")
    try:
        return uilock._read_meta(handle) or {}
    finally:
        handle.close()


class ClearStaleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_absent(self):
        out = uilock.clear_stale(root=self.root)
        self.assertFalse(out["cleared"])
        self.assertEqual(out["reason"], "absent")

    def test_dead_holder_is_cleared(self):
        # pid 是活的，但 process_start_time 对不上 = pid 复用，按「已死」处理（确定性构造）
        _fake_lock(self.root, {"pid": os.getpid(), "owner": "old-runner",
                               "process_start_time": 1, "acquired_at": "x"})
        out = uilock.clear_stale(root=self.root)
        self.assertTrue(out["cleared"], out)
        self.assertEqual(out["pid"], os.getpid())
        meta = _read_meta(self.root)
        self.assertEqual(meta.get("owner"), "stale")
        self.assertEqual(meta.get("stale_owner"), "old-runner")

    def test_alive_holder_is_not_touched(self):
        _fake_lock(self.root, {"pid": os.getpid(), "owner": "live-runner",
                               "process_start_time": uilock.process_start_time(os.getpid())})
        out = uilock.clear_stale(root=self.root)
        self.assertFalse(out["cleared"], out)
        self.assertEqual(out["reason"], "alive")
        self.assertEqual(_read_meta(self.root).get("owner"), "live-runner")

    def test_hold_does_not_wait_full_timeout_on_dead_meta(self):
        _fake_lock(self.root, {"pid": os.getpid(), "owner": "dead-runner",
                               "process_start_time": 1})
        started = time.monotonic()
        with uilock.hold("test", timeout=5.0, root=self.root) as meta:
            self.assertEqual(meta["owner"], "test")
        self.assertLess(time.monotonic() - started, 3.0, "死进程不该让等锁耗满 timeout")


class OfflineScopeTests(unittest.TestCase):
    """离线命令不许碰 UI 锁。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        rdir = root / "docs" / "iterations" / "round-0"
        (rdir / "replies").mkdir(parents=True)
        (rdir / "tree.txt").write_text("docs/self_iteration_spec.md\n", encoding="utf-8")
        (root / "docs" / "self_iteration_spec.md").write_text(
            "## 5. Round 0\n\n把材料交给四家，走完整管道。\n", encoding="utf-8")
        (rdir / "replies" / "chatgpt.md").write_text(
            "1. 第 5 节「把材料交给四家，走完整管道」\n", encoding="utf-8")
        self.root = root
        self._old_root = iterate.ROOT
        iterate.ROOT = root
        self.calls = []

    def tearDown(self):
        iterate.ROOT = self._old_root

    def test_verify_never_takes_the_lock(self):
        def boom(*args, **kwargs):
            self.calls.append(args)
            raise AssertionError("离线命令不许取 UI 锁")

        with mock.patch.object(uilock, "hold", boom), \
                mock.patch.object(uilock, "section", boom):
            code = iterate.cmd_verify(SimpleNamespace(round="round-0"), iterate.load_config())
        # 退出码只表示"达没达 3 家真读"（这里只有 1 席，正常返回 1），
        # 这条用例关心的是**有没有取锁**。
        self.assertIn(code, (0, 1))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "runs" / "ui.lock").exists(),
                         "验真不该创建 ui.lock")


if __name__ == "__main__":
    unittest.main()
