"""B26：重启后不许留下永久 running/queued 的僵尸 job。

现场（四端会审 2026-11-11，ChatGPT 第 4 条）：`_patch` 对不在内存的 job 直接
return，`get_job` 原样返回磁盘内容，所以被 kill 留下的 running 既不推进也不结束；
同 `request_id` 又直接复用，客户端永远拿回同一条僵尸。
"""
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import server                            # noqa: E402


def _job(job_id, status, request_id=None, endpoints=None):
    return {
        "job_id": job_id, "request_id": request_id, "status": status,
        "purpose": "test", "prompt": "p", "created_at": "2026-11-11T00:00:00+08:00",
        "started_at": "2026-11-11T00:00:01+08:00" if status == "running" else None,
        "finished_at": None, "order": list(endpoints or {"chatgpt": {}}),
        "endpoints": {name: {"status": (endpoints or {}).get(name, "queued")}
                      for name in (endpoints or {"chatgpt": "queued"})},
    }


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "runs" / "jobs"
        self.folder.mkdir(parents=True, exist_ok=True)

    def put(self, payload, name=None):
        path = self.folder / f"{name or payload['job_id']}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def engine(self, **config):
        return server.Engine(root=self.root, token="t", port=0,
                             runner=lambda *a, **k: None, config=config or None)

    def test_running_becomes_failed(self):
        path = self.put(_job("j1", "running", endpoints={"chatgpt": "running",
                                                         "grok": "queued"}))
        self.engine()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error_code"], "interrupted_by_restart")
        self.assertTrue(data["retryable"])
        self.assertIsNotNone(data["finished_at"])
        for name in ("chatgpt", "grok"):
            self.assertEqual(data["endpoints"][name]["status"], "failed")
            self.assertEqual(data["endpoints"][name]["error_code"],
                             "interrupted_by_restart")

    def test_queued_becomes_failed(self):
        path = self.put(_job("j2", "queued"))
        self.engine()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "failed")

    def test_terminal_jobs_are_untouched(self):
        done = self.put(_job("j3", "done", endpoints={"chatgpt": "done"}))
        part = self.put(_job("j4", "partial", endpoints={"chatgpt": "done"}))
        before = (done.read_bytes(), part.read_bytes())
        self.engine()
        self.assertEqual((done.read_bytes(), part.read_bytes()), before)

    def test_broken_files_are_skipped_not_raised(self):
        broken = self.folder / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        mismatched = self.put(_job("other-id", "running"), name="wrong-name")
        engine = self.engine()                      # 不许抛
        self.assertIsNotNone(engine)
        self.assertEqual(broken.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(json.loads(mismatched.read_text(encoding="utf-8"))["status"],
                         "running", "job_id 与文件名不符的不该被改")

    def test_same_request_id_still_duplicates_but_carries_retryable(self):
        self.put(_job("j5", "running", request_id="req-1"))
        engine = self.engine()
        found, duplicate = engine.submit(["chatgpt"], "p", "test", request_id="req-1")
        self.assertTrue(duplicate)
        self.assertEqual(found["status"], "failed")
        self.assertTrue(found.get("retryable"))

    def test_second_start_changes_nothing(self):
        path = self.put(_job("j6", "running"))
        self.engine()
        after_first = path.read_bytes()
        self.engine()
        self.assertEqual(path.read_bytes(), after_first, "第二次启动不该再改一遍")


class SwitchTests(unittest.TestCase):
    def test_switch_off_keeps_them_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "runs" / "jobs"
            folder.mkdir(parents=True)
            path = folder / "j7.json"
            path.write_text(json.dumps(_job("j7", "running")), encoding="utf-8")
            server.Engine(root=Path(tmp), token="t", port=0,
                          runner=lambda *a, **k: None,
                          config={"recover_interrupted_jobs": False})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"],
                             "running")


if __name__ == "__main__":
    unittest.main()


class ExplicitRetryTests(unittest.TestCase):
    """B26：同一个 request_id 允许**显式**重排，且旧记录要标 superseded_by。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "runs" / "jobs"
        self.folder.mkdir(parents=True, exist_ok=True)

    def put(self, payload):
        path = self.folder / f"{payload['job_id']}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def engine(self):
        engine = server.Engine(root=self.root, token="t", port=0,
                               runner=lambda *a, **k: None)
        engine._ensure_worker = lambda: None
        return engine

    def test_explicit_retry_supersedes_the_interrupted_job(self):
        old = self.put(_job("j10", "running", request_id="req-2"))
        engine = self.engine()
        self.assertEqual(json.loads(old.read_text(encoding="utf-8"))["status"], "failed")

        job, duplicate = engine.submit(["chatgpt"], "p", "test",
                                       request_id="req-2", retry=True)
        self.assertFalse(duplicate, "显式重试应当排一条新 job")
        self.assertEqual(job["status"], "queued")
        self.assertNotEqual(job["job_id"], "j10")

        old_data = json.loads(old.read_text(encoding="utf-8"))
        self.assertEqual(old_data["superseded_by"], job["job_id"], "旧记录必须标出被谁取代")

        found = engine._find_request("req-2")
        self.assertEqual(found["job_id"], job["job_id"], "同一个 request_id 只认新的那条")

    def test_retry_without_the_flag_still_deduplicates(self):
        self.put(_job("j11", "running", request_id="req-3"))
        engine = self.engine()
        job, duplicate = engine.submit(["chatgpt"], "p", "test", request_id="req-3")
        self.assertTrue(duplicate)
        self.assertEqual(job["status"], "failed")

    def test_retry_refused_for_other_failures(self):
        """只有"被重启打断"才允许重排；额度/登录失败仍要人工处理。"""
        payload = _job("j12", "failed", request_id="req-4")
        payload["error_code"] = "quota"
        self.put(payload)
        engine = self.engine()
        job, duplicate = engine.submit(["chatgpt"], "p", "test",
                                       request_id="req-4", retry=True)
        self.assertTrue(duplicate)
        self.assertEqual(job["job_id"], "j12")
