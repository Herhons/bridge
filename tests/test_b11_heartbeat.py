"""B11：任务进度心跳。

**为什么有这个**：`_run_use` 是阻塞的 `proc.communicate()`，子进程 stdout 要等它退出
才被读；`elapsed` 也只在结束时写一次。于是从提交到结束，面板只有
`queued → running` 一次变化，中间二十分钟一片静默，看起来像卡死。

心跳必须满足三条：**没有 job 上下文时完全 no-op**、**任何异常都不许带崩主流程**、
**只写自己那一个文件**。
"""
import json
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import heartbeat  # noqa: E402

JOB = "20261108T101500-ab12cd34"
EP = "gemini"


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-"))
        self._patches = [
            unittest.mock.patch.object(heartbeat, "ROOT", self.tmp),
            unittest.mock.patch.dict(os.environ, {}, clear=False),
        ]
        for item in self._patches:
            item.start()
        os.environ.pop(heartbeat.JOB_ENV, None)
        os.environ.pop(heartbeat.ENDPOINT_ENV, None)
        heartbeat._last_at, heartbeat._last_stage = 0.0, None

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()

    def with_job(self):
        os.environ[heartbeat.JOB_ENV] = JOB
        os.environ[heartbeat.ENDPOINT_ENV] = EP

    def path(self):
        return heartbeat.path_for(JOB, EP, self.tmp)


class NoContextTests(_Base):
    def test_no_env_is_a_no_op(self):
        heartbeat.beat("waiting", elapsed=1)
        self.assertFalse(heartbeat.dir_for(self.tmp).exists())

    def test_clear_without_env_does_not_raise(self):
        heartbeat.clear()


class WriteTests(_Base):
    def test_writes_one_frame(self):
        self.with_job()
        heartbeat.beat("waiting", elapsed=12.5, busy=True, copies=2)
        data = json.loads(self.path().read_text(encoding="utf-8"))
        self.assertEqual(data["stage"], "waiting")
        self.assertEqual(data["endpoint"], EP)
        self.assertEqual(data["job_id"], JOB)
        self.assertEqual(data["schema_version"], heartbeat.SCHEMA_VERSION)
        self.assertEqual(data["elapsed"], 12.5)
        self.assertTrue(data["busy"])

    def test_same_stage_is_throttled_but_stage_change_is_not(self):
        self.with_job()
        heartbeat.beat("waiting", elapsed=1)
        first = self.path().stat().st_mtime_ns
        heartbeat.beat("waiting", elapsed=2)          # 1.5s 内同 stage → 不重写
        self.assertEqual(self.path().stat().st_mtime_ns, first)
        heartbeat.beat("copying", elapsed=3)          # stage 变了 → 立即写
        self.assertEqual(json.loads(self.path().read_text(encoding="utf-8"))["stage"],
                         "copying")

    def test_force_bypasses_throttle(self):
        self.with_job()
        heartbeat.beat("waiting")
        heartbeat.beat("waiting", force=True, stage_extra=1)
        self.assertEqual(json.loads(self.path().read_text(encoding="utf-8"))["stage_extra"], 1)

    def test_read_roundtrip_and_bad_json(self):
        self.with_job()
        heartbeat.beat("waiting", elapsed=7)
        self.assertEqual(heartbeat.read(JOB, EP, self.tmp)["elapsed"], 7)
        self.path().write_text("{not json", encoding="utf-8")
        self.assertIsNone(heartbeat.read(JOB, EP, self.tmp))

    def test_clear_removes_only_own_frame(self):
        self.with_job()
        heartbeat.beat("waiting")
        other = heartbeat.path_for(JOB, "grok", self.tmp)
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("{}", encoding="utf-8")
        heartbeat.clear()
        self.assertFalse(self.path().exists())
        self.assertTrue(other.exists(), "不许删别人的帧")

    def test_unsafe_names_cannot_escape_the_directory(self):
        path = heartbeat.path_for("../../etc", "a/b", self.tmp)
        self.assertEqual(path.parent, heartbeat.dir_for(self.tmp))
        self.assertNotIn("..", path.name)


class BeatViewTests(_Base):
    def test_tolerates_a_view_without_copies(self):
        """假件没有 copies —— 心跳不许因此带崩主流程。"""
        self.with_job()
        heartbeat.beat_view("waiting", SimpleNamespace(busy=True))
        data = json.loads(self.path().read_text(encoding="utf-8"))
        self.assertTrue(data["busy"])
        self.assertNotIn("copies", data)

    def test_none_view_is_fine(self):
        self.with_job()
        heartbeat.beat_view("preparing", None, elapsed=0.4)
        self.assertEqual(json.loads(self.path().read_text(encoding="utf-8"))["elapsed"], 0.4)


class SweepTests(_Base):
    def test_removes_only_old_frames(self):
        folder = heartbeat.dir_for(self.tmp)
        folder.mkdir(parents=True, exist_ok=True)
        old, fresh = folder / "a.gemini.json", folder / "b.grok.json"
        old.write_text("{}", encoding="utf-8")
        fresh.write_text("{}", encoding="utf-8")
        past = time.time() - 3 * 86400
        os.utime(old, (past, past))
        self.assertEqual(heartbeat.sweep(self.tmp, max_age=86400), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
