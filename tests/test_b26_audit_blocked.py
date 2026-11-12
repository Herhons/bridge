"""B26：被账本拦住的记录必须一眼看得见，并给出可直接复制的人工出口。"""
from offline_guard import ensure_offline
ensure_offline()

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import submission_audit                        # noqa: E402
from qi import submissions                     # noqa: E402


def _record(state, *, age_hours=10.0):
    stamp = time.time() - age_hours * 3600
    return {"v": 2, "state": state, "v2_state": state, "level": None,
            "submission_count": 1, "attempts": [{"kind": "submit", "at": stamp}],
            "at": stamp, "updated_at": stamp, "evidence": {"l1": {"job_id": "j"}}}


class AuditBlockedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / "runs" / "submissions"
        self.folder.mkdir(parents=True)

    def put(self, name, payload):
        (self.folder / name).write_text(json.dumps(payload), encoding="utf-8")

    def run_audit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            submission_audit.main(["--root", str(self.root)])
        return json.loads(out.getvalue())

    def test_unknown_gets_a_copyable_command(self):
        self.put("workbuddy-abc.json", _record(submissions.STATE_UNKNOWN))
        report = self.run_audit()
        self.assertEqual(report["blocked_total"], 1)
        entry = report["blocked"][0]
        self.assertEqual(entry["v2_state"], "unknown")
        self.assertIn("--state unknown", entry["command"])
        self.assertIn("--confirm", entry["command"])
        self.assertGreater(entry["age_hours"], 9)

    def test_submitting_is_listed_without_a_command(self):
        """`submitting` 默认不给解锁命令 —— 这条约束**仍然有效**。

        2026-11-12（B38）把它精细化了：只有"写账进程已死 + 没写过 scope_after +
        超过 1 小时"三条全中的孤儿才给 `--state submitting` 的命令。
        这里构造的记录不是孤儿，所以照旧没有 `command` 字段。
        """
        self.put("codex-def.json", _record(submissions.STATE_SUBMITTING))
        report = self.run_audit()
        self.assertEqual(report["blocked_total"], 1)
        self.assertNotIn("command", report["blocked"][0],
                         "submitting 没有既定解法，不许给会误导的命令")

    def test_accepted_is_not_listed(self):
        self.put("grok-ghi.json", _record(submissions.STATE_ACCEPTED, age_hours=1))
        report = self.run_audit()
        self.assertEqual(report["blocked_total"], 0)


if __name__ == "__main__":
    unittest.main()
