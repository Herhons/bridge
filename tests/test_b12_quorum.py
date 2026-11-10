"""B12：并发调度与回收线（quorum）。

用户要的是「并发 4 席、最少回收 3 席，两个数都可调」。

**关键约束**：`min_success` 缺省 1 时，判定必须**逐条等价**于原来的
`all done → done / any done → partial / else failed`。否则既有 job 语义会被悄悄改掉。
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

NAMES = ["chatgpt", "gemini", "deepseek", "grok"]


def make_job(names=NAMES):
    return {
        "job_id": "j1", "status": "queued", "order": list(names),
        "purpose": "review", "prompt": "x", "created_at": "now",
        "endpoints": {name: {"status": "queued"} for name in names},
    }


def make_engine(**overrides):
    """只造一个够用的 Engine：不碰脚本、不碰 UI。"""
    tmp = tempfile.mkdtemp(prefix="b12-")
    engine = server.Engine(root=Path(tmp), token="fixture", port=0,
                           runner=lambda *a, **k: {})
    engine.config.update(overrides)
    return engine


class QuorumSemanticsTests(unittest.TestCase):
    def _q(self, statuses, **cfg):
        engine = make_engine(**cfg)
        job = make_job(list(statuses))
        for name, status in zip(job["order"], statuses):
            job["endpoints"][name]["status"] = status
        return engine._quorum(job)

    def test_default_one_success_equals_the_old_any_done(self):
        """min_success=1 时：有成功就 met，全失败就不 met —— 与旧口径一致。"""
        self.assertTrue(self._q(["done", "failed"])["met"])
        self.assertTrue(self._q(["done", "done"])["met"])
        self.assertFalse(self._q(["failed", "failed"])["met"])
        self.assertEqual(self._q(["done", "failed"])["required"], 1)

    def test_four_seats_need_three(self):
        self.assertTrue(self._q(["done", "done", "done", "failed"],
                                min_success=3)["met"])
        got = self._q(["done", "done", "failed", "failed"], min_success=3)
        self.assertFalse(got["met"])
        self.assertEqual(got["recovered"], 2)

    def test_line_above_seat_count_is_clamped_not_rejected(self):
        """勾 2 端却把线设成 3：夹到 2 并标 clamped，不报错（否则面板会莫名 400）。"""
        got = self._q(["done", "failed"], min_success=3)
        self.assertEqual(got["required"], 2)
        self.assertTrue(got["clamped"])
        # 夹到 2 之后仍然要求 2 席都成 —— 1 席不算达标
        self.assertFalse(got["met"])

    def test_recovered_counts_only_done(self):
        got = self._q(["done", "failed", "queued", "running"], min_success=1)
        self.assertEqual(got["recovered"], 1)


class SchedulingTests(unittest.TestCase):
    def _run(self, **cfg):
        engine = make_engine(**cfg)
        job = make_job()
        engine.jobs["j1"] = job
        spans = {}

        def fake(job_id, name):
            start = time.monotonic()
            time.sleep(0.25)
            spans[name] = (start, time.monotonic())
            engine._patch(job_id, name, status="done")

        engine._run_endpoint = fake
        started = time.monotonic()
        engine._run_job("j1")
        return spans, time.monotonic() - started, engine.get_job("j1")

    def test_concurrency_one_stays_serial(self):
        spans, wall, job = self._run(endpoint_concurrency=1, min_success=1)
        self.assertGreater(wall, 4 * 0.25, "串行时总墙钟至少是各端之和")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["quorum"]["recovered"], 4)

    def test_concurrency_four_overlaps(self):
        """判据用"有没有同时在跑"，不用总墙钟。

        总墙钟会被 `_patch` 的 fsync 与线程启动开销拖长（全量跑时尤甚），
        那是磁盘成本，不是"没并发"。真正的判据是：四个端在同一瞬间都在执行。
        """
        spans, _wall, job = self._run(endpoint_concurrency=4, min_success=3)
        latest_start = max(start for start, _end in spans.values())
        earliest_end = min(end for _start, end in spans.values())
        self.assertLess(latest_start, earliest_end,
                        f"四席没有时间重叠：最晚开跑 {latest_start:.2f}s，"
                        f"最早结束 {earliest_end:.2f}s")
        # 只保留重叠判据：墙钟受 fsync / 线程启动 / 机器负载影响（实测同一台机器
        # 从 1.07s 抖到 3.17s），它不区分"没并发"和"磁盘慢"，留着只会假红。
        self.assertEqual(job["status"], "done")

    def test_exclusive_endpoints_run_after_the_pool(self):
        spans, _wall, job = self._run(endpoint_concurrency=4, min_success=3,
                                      exclusive_endpoints=["grok"])
        pool_end = max(end for name, (_s, end) in spans.items() if name != "grok")
        grok_start = spans["grok"][0]
        self.assertGreaterEqual(grok_start, pool_end - 0.01,
                                "读屏的端（grok）必须排在其他端之后单独跑")
        self.assertEqual(job["status"], "done")

    def _with_recovered(self, good):
        engine = make_engine(endpoint_concurrency=4, min_success=3)
        engine.jobs["j1"] = make_job()

        def fake(job_id, name):
            ok = name in good
            engine._patch(job_id, name, status="done" if ok else "failed")

        engine._run_endpoint = fake
        engine._run_job("j1")
        return engine.get_job("j1")

    def test_partial_when_line_is_met_but_not_all(self):
        job = self._with_recovered(("chatgpt", "gemini", "deepseek"))   # 3/4
        self.assertEqual(job["status"], "partial")
        self.assertTrue(job["quorum"]["met"])
        self.assertEqual(job["quorum"]["recovered"], 3)

    def test_failed_when_line_is_not_met(self):
        job = self._with_recovered(("chatgpt", "gemini"))               # 2/4，线是 3
        self.assertEqual(job["status"], "failed",
                         "回收线没到就是这一轮没成立，不能算 partial")
        self.assertFalse(job["quorum"]["met"])
        self.assertEqual(job["quorum"]["recovered"], 2)


if __name__ == "__main__":
    unittest.main()


class ExclusiveLockTests(unittest.TestCase):
    """B26：exclusive 端在并发模式下也必须整段持锁，且子进程不能再按 section 抢。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "src" / "scripts").mkdir(parents=True, exist_ok=True)
        for name in ("codex", "dsh"):
            (self.root / "src" / "scripts" / f"{name}_use.py").write_text("", encoding="utf-8")
        self.engine = server.Engine(
            root=self.root, token="t", port=0, runner=lambda *a, **k: None,
            config={"endpoint_concurrency": 4, "exclusive_endpoints": ["dsh"]})
        self.engine._patch = lambda *a, **k: None

    def test_exclusive_takes_the_whole_round_lock(self):
        import contextlib
        from unittest import mock

        calls = []

        def fake_hold(name, **kwargs):
            calls.append(name)
            return contextlib.nullcontext()

        with mock.patch.object(server.uilock, "hold", side_effect=fake_hold), \
                mock.patch.object(self.engine, "_run_choose", return_value={"ok": True}), \
                mock.patch.object(self.engine, "_run_use",
                                  return_value={"ok": True, "stage": "answered", "reply": "r"}), \
                mock.patch.object(self.engine, "get_job",
                                  return_value={"purpose": "test", "prompt": "p"}):
            self.engine._run_endpoint("j1", "dsh")        # exclusive
            self.engine._run_endpoint("j1", "chatgpt")    # 普通池

        self.assertEqual(len(calls), 1, f"只该有一席拿到整轮锁：{calls}")
        self.assertIn("dsh", calls[0])

    def test_exclusive_child_is_not_sectioned(self):
        seen = {}

        def runner(cmd, timeout=None, cwd=None, env=None):
            seen.update(env or {})
            return {"exit_code": 0, "timeout": False, "elapsed": 0.1, "stderr": "",
                    "stdout": '{"ok": true, "stage": "answered", "reply": "x"}'}

        self.engine.runner = runner
        self.engine._run_use("dsh", "p", "test", deadline=time.monotonic() + 30)
        self.assertNotIn("BRIDGE_UI_SECTIONED", seen,
                         "exclusive 端不该再被要求分段持锁")

        seen.clear()
        self.engine._run_use("codex", "p", "test", deadline=time.monotonic() + 30)
        self.assertEqual(seen.get("BRIDGE_UI_SECTIONED"), "1",
                         "普通端仍要分段持锁")
