"""S3：会话锚点的判定。"""
from offline_guard import ensure_offline
ensure_offline()

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import session_anchor as sa                 # noqa: E402


class AnchorTests(unittest.TestCase):
    def test_no_anchor_allows_new(self):
        self.assertEqual(sa.resolve(None, "https://x/app")["action"], "new_allowed")

    def test_url_match_stays(self):
        a = sa.SessionAnchor("gemini", "round-1", "https://x/app/abc", "h")
        self.assertEqual(sa.resolve(a, "https://x/app/abc?hl=zh")["action"], "stay")

    def test_drifted_before_accepted_navigates_back(self):
        a = sa.SessionAnchor("gemini", "round-1", "https://x/app/abc", "h", accepted=False)
        out = sa.resolve(a, "https://x/app")
        self.assertEqual(out["action"], "navigate")
        self.assertEqual(out["url"], "https://x/app/abc")

    def test_drifted_after_accepted_is_lost_not_new(self):
        a = sa.SessionAnchor("gemini", "round-1", "https://x/app/abc", "h", accepted=True)
        out = sa.resolve(a, "https://x/app")
        self.assertEqual(out["action"], "session_lost")
        self.assertNotEqual(out["action"], "new_allowed")

    def test_round_trip(self):
        a = sa.SessionAnchor("grok", "round-2", "https://g/c/1", "h", accepted=True)
        self.assertEqual(sa.SessionAnchor.from_dict(a.to_dict()), a)


if __name__ == "__main__":
    unittest.main()
