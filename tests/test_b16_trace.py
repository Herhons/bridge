"""B16-S6：桌面超时必须留下可复盘的量，不能只剩 `stage=None`。

D4b / R3 现场：`reads` 一路涨到 535、`copies=9`、`busy=false`、`accepted=true`，
但 300s 超时后 stdout 被父进程 kill 丢弃，job 里只剩 `use_timeout`。
这里钉住采集点存在、且 live_job 把诊断帧采进证据。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


class TraceWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conversation = (ROOT / "src" / "qi" / "conversation.py").read_text(
            encoding="utf-8-sig")
        cls.live = (ROOT / "tools" / "live_job.py").read_text(encoding="utf-8-sig")

    def test_waiting_frame_carries_baseline_and_composer_len(self):
        self.assertIn('heartbeat.beat_view("waiting", view, reads=hb_reads,\n'
                      '                                accepted=accepted, baseline=baseline,\n'
                      '                                composer_len=len(view.value or ""))',
                      self.conversation)

    def test_gate_branch_names_why(self):
        self.assertIn('heartbeat.beat("gate", reads=hb_reads, busy=busy_now,',
                      self.conversation)
        self.assertIn('why=("busy" if busy_now and not busy_ignored else "no_copies")',
                      self.conversation)

    def test_settle_frame_carries_text_len(self):
        self.assertIn('heartbeat.beat("settle", reads=hb_reads, text_len=sig[3]',
                      self.conversation)

    def test_live_job_samples_the_trace_frame(self):
        self.assertIn('"trace": {n:', self.live)
        for key in ("text_len", "baseline", "why", "sig", "candidates"):
            self.assertIn('"' + key + '"', self.live)
        # 旧证据形状不能破
        self.assertIn('"reads": {n: progress[n].get("reads") for n in names}', self.live)


class PositionalFallbackTests(unittest.TestCase):
    """R4 实测：codex `copies=9 baseline=9` 卡满 300s，回复其实已在窗口里。"""

    def setUp(self):
        from qi import conversation
        self.fn = conversation.positional_ready

    def test_not_before_the_wait(self):
        self.assertFalse(self.fn(busy=False, accepted=True, since=0.0, now=44.9))

    def test_ready_after_the_wait(self):
        self.assertTrue(self.fn(busy=False, accepted=True, since=0.0, now=45.1))

    def test_busy_never_short_circuits(self):
        self.assertFalse(self.fn(busy=True, accepted=True, since=0.0, now=999.0))

    def test_unaccepted_never_short_circuits(self):
        self.assertFalse(self.fn(busy=False, accepted=False, since=0.0, now=999.0))

    def test_no_stall_start_is_never_ready(self):
        self.assertFalse(self.fn(busy=False, accepted=True, since=None, now=999.0))

    def test_source_uses_the_pure_decision(self):
        text = (ROOT / "src" / "qi" / "conversation.py").read_text(encoding="utf-8-sig")
        self.assertIn("positional = positional_ready(", text)
        self.assertIn("view.copies if positional else view.copies[baseline:]", text)


if __name__ == "__main__":
    unittest.main()
