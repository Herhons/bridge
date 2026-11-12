"""B38：审计要能认出「卡在 submitting 的孤儿记录」，并给出人工出口。

`submitting` 是"发送前落账、发送后该有终态"的中间态，正常情况下几秒内就变成
`accepted` / `unknown`。一直停在这里只有一个原因：**写账进程在发送途中被杀**。

2026-11-12 实测账本里积了 15 条，pid 全死、`scope_after` 全缺、17~125 小时前。
这些记录在 `BLOCKING_STATES` 里 —— 对应提问**永远发不出去**，而原来 `audit`
只把它们列出来，不给出口（`unknown` / `failed_terminal` 都给了命令）。

三个条件必须**同时**成立才算孤儿，缺一个都可能误伤正在发送的进程：
  · 写账进程已死（`pid_alive` 返回 False）；
  · 从没写过 `scope_after`（那是被判 accepted 时才写的）；
  · 超过 1 小时。
"""
from offline_guard import ensure_offline
ensure_offline()

import importlib.util
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tools")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_spec = importlib.util.spec_from_file_location("audit_tool", ROOT / "tools" / "submission_audit.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

from qi import submissions                            # noqa: E402


class OrphanSubmitting(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="b38-audit-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.folder = self.root / "runs" / "submissions"
        self.folder.mkdir(parents=True)

    def _write(self, name, *, state=submissions.STATE_SUBMITTING, pid=999999,
               age_hours=5.0, scope_after=None):
        record = {
            "v": 2, "state": "unknown", "v2_state": state, "level": None,
            "submission_count": 1,
            "attempts": [{"kind": "submit", "at": time.time() - age_hours * 3600}],
            "pid": pid,
            "at": time.time() - age_hours * 3600,
            "updated_at": time.time() - age_hours * 3600,
            "scope": {"v": 1, "site": "codex"},
        }
        if scope_after:
            record["scope_after"] = scope_after
        path = self.folder / name
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return path

    def _run(self, alive):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch.object(audit.uilock, "pid_alive", return_value=alive), \
                redirect_stdout(buf):
            audit.main(["--root", str(self.root)])
        return json.loads(buf.getvalue())

    def test_dead_writer_without_evidence_is_an_orphan(self):
        self._write("codex-aaa.json")
        report = self._run(alive=False)
        self.assertEqual(report["orphan_submitting"], 1)
        entry = report["blocked"][0]
        self.assertTrue(entry["orphan_submitting"])
        self.assertFalse(entry["writer_alive"])
        self.assertFalse(entry["has_scope_after"])
        self.assertIn("--state submitting", entry["command"])
        self.assertIn("submission_clear", entry["command"])

    def test_live_writer_is_not_an_orphan(self):
        """进程还活着：它可能正卡在发送里，绝不能提示人去解锁。

        B26 定的规矩（"submitting 不许给会误导的命令"）继续有效：
        **非孤儿一律没有 command 字段**。
        """
        self._write("codex-live.json")
        report = self._run(alive=True)
        self.assertEqual(report["orphan_submitting"], 0)
        self.assertFalse(report["blocked"][0]["orphan_submitting"])
        self.assertNotIn("command", report["blocked"][0], "非孤儿不许给解锁命令")

    def test_unreadable_pid_is_not_an_orphan(self):
        """读不到进程状态（权限/跨用户）时不给结论。"""
        self._write("codex-unknown.json")
        report = self._run(alive=None)
        self.assertEqual(report["orphan_submitting"], 0)

    def test_scope_after_present_is_not_an_orphan(self):
        """写过 scope_after 说明被判过 accepted，不是"没走完"的记录。"""
        self._write("codex-after.json", scope_after={"v": 1, "site": "codex"})
        report = self._run(alive=False)
        self.assertEqual(report["orphan_submitting"], 0)

    def test_too_recent_is_not_an_orphan(self):
        """还不到 1 小时：可能只是一次很慢的发送。"""
        self._write("codex-young.json", age_hours=0.2)
        report = self._run(alive=False)
        self.assertEqual(report["orphan_submitting"], 0)

    def test_accepted_records_stay_out_of_blocked(self):
        """accepted 是正常终态，不该混进"需要人看一眼"的清单。"""
        self._write("codex-ok.json", state=submissions.STATE_ACCEPTED, age_hours=99)
        report = self._run(alive=False)
        self.assertEqual(report["blocked_total"], 0)


if __name__ == "__main__":
    unittest.main()
