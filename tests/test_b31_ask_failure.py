"""B31：走兜底时必须保留 ask 的失败细节。

Round 3 的 grok 只剩 `stage=text_extracted`，看不出它是 timeout 还是
conversation_changed，`diagnostics` 也丢了——等于每次兜底都自毁证据。
"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

source = (ROOT / "tools" / "iterate.py").read_text(encoding="utf-8-sig")


class AskFailureKeptTests(unittest.TestCase):
    def test_failure_snapshot_is_taken_before_fallback(self):
        self.assertIn('failure = {key: result.get(key) for key in', source)
        self.assertIn('"ask_failure": failure', source)

    def test_both_fallback_paths_carry_it(self):
        self.assertIn('result = {**harvested, "ask_failure": failure}', source)
        self.assertIn('"stage": "text_extracted", "reply": text,', source)


if __name__ == "__main__":
    unittest.main()
