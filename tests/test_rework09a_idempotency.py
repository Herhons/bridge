
from offline_guard import ensure_offline
ensure_offline()

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from qi import server
from test_card09a import FakeRunner, make_root


class DurableIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))
        self.engines = []

    def engine(self, **kwargs):
        value = server.Engine(root=self.root, token="fixture", port=0, **kwargs)
        self.engines.append(value)
        self.addCleanup(self.stop, value)
        return value

    @staticmethod
    def stop(engine):
        engine.stop()
        if engine._worker:
            engine._worker.join(timeout=10)

    def history(self, count):
        folder = self.root / "runs/jobs"
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(count):
            (folder / f"h{n:03}.json").write_text(json.dumps({
                "job_id": f"h{n:03}", "request_id": f"r{n:03}", "status": "done"}), encoding="utf-8")

    def test_oldest_of_251_still_gettable_and_deduplicated_after_restart(self):
        self.history(251)
        runner = mock.Mock(side_effect=AssertionError("duplicate must not dispatch"))
        for _ in range(2):
            engine = self.engine(runner=runner, config={"memory_jobs": 1})
            self.assertEqual(engine.get_job("h000")["request_id"], "r000")
            job, duplicate = engine.submit(["grok"], "offline", "test", request_id="r000")
            self.assertTrue(duplicate)
            self.assertEqual(job["job_id"], "h000")
        runner.assert_not_called()
        self.assertEqual(len(list((self.root / "runs/jobs").glob("*.json"))), 251)

    def test_disk_failure_cannot_accept_or_leave_a_phantom_in_memory(self):
        runner = FakeRunner(self.root)
        engine = self.engine(runner=runner)
        with mock.patch.object(server, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(server.RequestError) as caught:
                engine.submit(["grok"], "offline", "test", request_id="same")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(engine.jobs, {})
        self.assertTrue(engine._queue.empty())
        self.assertEqual(runner.calls, [])
        job, duplicate = engine.submit(["grok"], "offline", "test", request_id="same")
        self.assertFalse(duplicate)
        self.assertEqual(engine.wait_job(job["job_id"], 5)["status"], "done")

    def test_two_engines_concurrently_submit_one_id_only_once(self):
        runner = FakeRunner(self.root)
        engines = [self.engine(runner=runner), self.engine(runner=runner)]
        barrier = threading.Barrier(2)
        results, errors = [], []
        def submit(engine):
            try:
                barrier.wait(timeout=3)
                results.append(engine.submit(["grok"], "offline", "test", request_id="concurrent"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=submit, args=(engine,)) for engine in engines]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(len({result[0]["job_id"] for result in results}), 1)
        self.assertEqual(sorted(result[1] for result in results), [False, True])
        job_id = results[0][0]["job_id"]
        owner = next(engine for engine in engines if job_id in engine.jobs)
        self.assertEqual(owner.wait_job(job_id, 5)["status"], "done")
        self.assertEqual(len(runner.calls), 2)

    def test_corrupt_history_refuses_new_dispatch_instead_of_assuming_unique(self):
        engine = self.engine(runner=mock.Mock())
        (self.root / "runs/jobs/broken.json").write_text("broken", encoding="utf-8")
        with self.assertRaises(server.RequestError) as caught:
            engine.submit(["grok"], "offline", "test", request_id="could-have-been-in-broken-record")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(engine.jobs, {})
        engine.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
