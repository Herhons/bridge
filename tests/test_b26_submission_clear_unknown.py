"""B26：账本 `unknown` 的人工出口。

`unknown` 的含义是"上次提交状态不明，**可能已经发出去了**"，所以它一直
fail-closed 地拦着。默认不改这个判定，只给运维一个显式出口：
必须同时说清 `--state unknown`、`--reason`、`--confirm`，并且先把记录摘要打出来。
"""
from offline_guard import ensure_offline
ensure_offline()

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import submission_clear                       # noqa: E402
from qi import submissions                    # noqa: E402


def _record(state, **extra):
    record = {"v": 2, "state": state, "v2_state": state, "level": None,
              "submission_count": 0 if state in (submissions.STATE_FAILED_RETRYABLE,
                                                 submissions.STATE_DRAFT) else 1,
              "attempts": [{"kind": "submit", "at": 1.0}],
              "at": 1.0, "updated_at": 1.0,
              "evidence": {"l1": {"job_id": "j1"}}}
    record.update(extra)
    return record


class ClearTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, state, **extra):
        path = self.root / f"workbuddy-{state}.json"
        path.write_text(json.dumps(_record(state, **extra)), encoding="utf-8")
        return path

    def run_tool(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = submission_clear.main(list(argv))
        return code, out.getvalue()

    def state_of(self, path):
        return json.loads(path.read_text(encoding="utf-8"))["v2_state"]

    def test_unknown_is_refused_by_default(self):
        path = self.write(submissions.STATE_UNKNOWN)
        code, out = self.run_tool("--file", str(path), "--reason", "x", "--confirm")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.state_of(path), submissions.STATE_UNKNOWN)

    def test_unknown_needs_confirm(self):
        path = self.write(submissions.STATE_UNKNOWN)
        code, out = self.run_tool("--file", str(path), "--state", "unknown",
                                  "--reason", "页面确认没有这条提问")
        self.assertEqual(code, 2, out)
        self.assertIn("missing_confirm", out)
        self.assertEqual(self.state_of(path), submissions.STATE_UNKNOWN)

    def test_unknown_cleared_with_all_flags(self):
        path = self.write(submissions.STATE_UNKNOWN)
        code, out = self.run_tool("--file", str(path), "--state", "unknown",
                                  "--reason", "页面确认没有这条提问", "--confirm")
        self.assertEqual(code, 0, out)
        self.assertIn("summary", out, "写盘前必须打印记录摘要")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["v2_state"], submissions.STATE_FAILED_RETRYABLE)
        self.assertEqual(record["submission_count"], 0)
        self.assertEqual(record["evidence"]["cleared"]["from_state"], "unknown")
        self.assertEqual(record["attempts"][-1]["kind"], "clear")

    def test_failed_terminal_still_works(self):
        path = self.write(submissions.STATE_FAILED_TERMINAL)
        code, out = self.run_tool("--file", str(path), "--reason", "额度恢复", "--confirm")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state_of(path), submissions.STATE_FAILED_RETRYABLE)

    def test_other_states_still_rejected(self):
        path = self.write(submissions.STATE_ACCEPTED, level="L3")
        code, out = self.run_tool("--file", str(path), "--reason", "x", "--confirm")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.state_of(path), submissions.STATE_ACCEPTED)


if __name__ == "__main__":
    unittest.main()
