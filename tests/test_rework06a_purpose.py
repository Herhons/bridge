
from offline_guard import ensure_offline
ensure_offline()

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qi import tier_guard as tg, workbuddy as wb


class WorkBuddyPurpose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = tg.load_tiers()
        self.path = self.root / "tiers.json"
        self.screen = SimpleNamespace(target_hwnd=None)

    def run_entry(self, purpose, model="fixture-low", table=None):
        self.path.write_text(json.dumps(table if table is not None else self.table), encoding="utf-8")
        tg.write_receipt("workbuddy", target=model, readback=model, root=self.root)
        _, args = wb.parse_args(["offline-fixture", "--continue", "--purpose", purpose], sending=True)
        with mock.patch.object(tg, "ROOT", self.root), \
                mock.patch.object(tg, "TIERS_FILE", self.path), \
                mock.patch.object(wb, "open_app", return_value=self.screen), \
                mock.patch.object(wb, "controls", return_value=[]), \
                mock.patch.object(wb, "current_model", return_value=model), \
                mock.patch.object(wb, "ReplyConversation", return_value=SimpleNamespace(read_via=None, copy_note=None)), \
                mock.patch.object(wb, "ask", return_value={"ok": True, "stage": "answered"}) as ask, \
                mock.patch("builtins.print"):
            code = wb._run(args, sending=True)
        return code, ask.call_count

    def configured(self):
        data = copy.deepcopy(self.table)
        data["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=["fixture-low"], review=["fixture-high"], accept=["fixture-high"])
        return data

    def unconfigured(self):
        """显式把三个用途清空。

        不再依赖"生产 docs/tiers.json 里 workbuddy 恰好是空表"这个事实：
        T4-1 之后生产配置已经有值了，但**门禁语义**（空表必拒发）必须继续被覆盖。
        """
        data = copy.deepcopy(self.table)
        data["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=[], review=[], accept=[])
        return data

    def test_unconfigured_actual_workbuddy_entry_rejects_every_purpose_without_ask(self):
        data = self.unconfigured()
        for purpose in ("test", "review", "accept"):
            with self.subTest(purpose=purpose):
                self.assertEqual(self.run_entry(purpose, table=data), (5, 0))
                self.assertTrue(tg.read_receipt("workbuddy", root=self.root)["consumed"])

    def test_configured_actual_entry_enforces_both_directions_of_purpose(self):
        data = self.configured()
        for purpose, model, expected in (
            ("test", "fixture-low", (0, 1)), ("review", "fixture-high", (0, 1)),
            ("accept", "fixture-high", (0, 1)), ("review", "fixture-low", (5, 0)),
            ("accept", "fixture-low", (5, 0)), ("test", "fixture-high", (5, 0)),
            ("accept", "unknown", (5, 0))):
            with self.subTest(purpose=purpose, model=model):
                self.assertEqual(self.run_entry(purpose, model, data), expected)

    def test_missing_and_malformed_purpose_table_are_exit5_at_the_actual_entry(self):
        for value in (None, "fixture-low", {}, [None], [1], [""], ["   "]):
            with self.subTest(value=value):
                data = self.configured()
                entry = data["endpoints"]["workbuddy"]["dimensions"]["model"]
                if value is None:
                    entry.pop("accept")
                else:
                    entry["accept"] = value
                self.assertEqual(self.run_entry("accept", table=data), (5, 0))

    def test_check_reports_unconfigured_and_nonconsuming_precheck_stays_read_only(self):
        data = self.unconfigured()
        tg.write_receipt("workbuddy", target="anything", readback="anything", root=self.root)
        got = tg.check("workbuddy", "accept", root=self.root, tiers=data)
        self.assertFalse(got["ok"])
        self.assertEqual(got["reasons"], ["table_unconfigured"])
        with self.assertRaises(tg.TierUnconfirmed) as caught:
            tg.gate("workbuddy", "accept", root=self.root, tiers=data, do_consume=False)
        self.assertEqual(caught.exception.code, 5)
        self.assertFalse(tg.read_receipt("workbuddy", root=self.root).get("consumed"))

    def test_missing_purpose_is_still_argparse_exit2(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            wb.parse_args(["offline-fixture", "--continue"], sending=True)
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
