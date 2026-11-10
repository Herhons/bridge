
from offline_guard import ensure_offline
ensure_offline()

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CurrentPlanConsistency(unittest.TestCase):
    def test_all_original_verdicts_are_supported_by_unchanged_report_copies(self):
        data = json.loads((ROOT / "REWORK_STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["cards"]), 16)
        for entry in data["cards"]:
            raw = (ROOT / entry["copy"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
            verdict = re.search(r"^VERDICT (PASS|FAIL)\r?$", raw.decode("utf-8-sig"), re.M).group(1)
            self.assertEqual(entry["recheck_verdict"], verdict)
            self.assertEqual(entry["independent_acceptance"], "pending")

    def test_history_is_preserved_verbatim_and_current_table_is_explicit(self):
        data = json.loads((ROOT / "REWORK_STATUS.json").read_text(encoding="utf-8"))
        text = (ROOT / "PLAN.md").read_text(encoding="utf-8")
        historical = text.split("<!-- ORIGINAL_PLAN_SNAPSHOT_START -->\n", 1)[1].split("<!-- ORIGINAL_PLAN_SNAPSHOT_END -->", 1)[0]
        self.assertEqual(hashlib.sha256(historical.encode("utf-8")).hexdigest(), data["historical_text_sha256"])
        current = text.split("## 历史快照", 1)[0]
        for value in ("CARD-05已存在", "CARD-08实现", "09a/09b正式卡", "待不同厂商独立验收", "本人完整重启原始证据待补"):
            self.assertIn(value, current)

    def test_seventeen_rework_items_keep_index_order_and_selftests_have_changes(self):
        data = json.loads((ROOT / "REWORK_STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["rework"]), 17)
        self.assertEqual(data["rework"][0]["card"], "09a-lock-integration")
        self.assertEqual(data["rework"][-1]["card"], "TEST-ISOLATION")
        for entry in data["rework"]:
            if entry["status"] == "selftested":
                changes = (ROOT / entry["changes"]).read_text(encoding="utf-8")
                self.assertIn("EXIT=0", changes)
                self.assertIn("GUARD_EVENTS=0", changes)

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/recheck_sources',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
