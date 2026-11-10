"""B26：错峰步长必须有限幅。

`endpoint_stagger` 配成 999 甚至 Infinity 时，`index * stagger` 只是一次
`time.sleep`，**不消耗任何超时预算**，job 会挂在 running 上几十分钟；
而 `json.loads` 接受 `Infinity`、`float("Infinity")` 也成功。
"""
from offline_guard import ensure_offline
ensure_offline()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import server                            # noqa: E402


def _engine(root, **config):
    engine = server.Engine(root=root, token="t", port=0, runner=lambda *a, **k: None)
    engine.config.update(config)
    return engine


class LoadConfigTests(unittest.TestCase):
    def _load(self, raw):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True, exist_ok=True)
            (root / "docs" / "bridge_server.json").write_text(raw, encoding="utf-8")
            return server.load_config(root)

    def test_huge_value_is_capped(self):
        cfg = self._load(json.dumps({"endpoint_stagger": 999}))
        self.assertEqual(cfg["endpoint_stagger"], server.STAGGER_CEILING)

    def test_infinity_is_capped_not_kept(self):
        cfg = self._load('{"endpoint_stagger": Infinity}')
        self.assertEqual(cfg["endpoint_stagger"], server.STAGGER_CEILING)

    def test_negative_falls_back_to_zero(self):
        cfg = self._load(json.dumps({"endpoint_stagger": -5}))
        self.assertEqual(cfg["endpoint_stagger"], 0.0)

    def test_normal_value_untouched(self):
        cfg = self._load(json.dumps({"endpoint_stagger": 2.5}))
        self.assertEqual(cfg["endpoint_stagger"], 2.5)


class RunJobDelayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.engine = _engine(self.root, endpoint_concurrency=4, endpoint_stagger=999)
        job = {"job_id": "j1", "status": "queued", "purpose": "test", "prompt": "p",
               "order": ["a", "b", "c", "d"],
               "endpoints": {n: {"status": "queued"} for n in "abcd"}}
        self.engine.jobs["j1"] = job

    def test_delays_are_capped(self):
        seen = []
        with mock.patch.object(self.engine, "_start_delayed",
                               side_effect=lambda fn, name, delay: seen.append(delay)) as patch:
            with mock.patch.object(self.engine, "_run_endpoint", return_value=None):
                self.engine._run_job("j1")
        self.assertEqual(len(seen), 4, "四席都要被提交")
        self.assertLessEqual(max(seen), 3 * server.STAGGER_CEILING,
                             f"错峰总延迟没夹住：{max(seen)}")
        self.assertEqual(sorted(seen), [0.0, 30.0, 60.0, 90.0],
                         "每一档都应是 index * STAGGER_CEILING")


if __name__ == "__main__":
    unittest.main()
