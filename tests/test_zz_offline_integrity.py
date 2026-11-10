"""Final suite check supplements the process-exit audit check."""
from offline_guard import ensure_offline

ensure_offline()

import unittest


class OfflineWholeSuiteIntegrity(unittest.TestCase):
    def test_no_unexpected_events_in_this_parent_or_inherited_children(self):
        events = [path for path in ensure_offline().EVENTS.glob("guard_events_*.jsonl")
                  if path.stat().st_size]
        self.assertEqual(events, [])
