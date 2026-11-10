
from offline_guard import ensure_offline
ensure_offline()

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from evidence_contract import workbuddy_restart_complete


class WorkBuddyRestartEvidenceIntegrity(unittest.TestCase):
    def test_current_bundle_keeps_workbuddy_live_gap_and_original_excerpts(self):
        bundle = json.loads((ROOT / "evidence/09e-workbuddy-restart-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["offline_work_status"], "complete")
        self.assertEqual(bundle["live_evidence_status"], "pending")
        self.assertEqual(bundle["new_ui_actions"], 0)
        self.assertFalse(workbuddy_restart_complete(bundle["events"]))
        for ref in bundle["references"]:
            raw = Path(ref["source"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ref["sha256"])
            rows = raw.decode("utf-8-sig").splitlines()
            self.assertEqual("\n".join(rows[ref["first_line"]-1:ref["last_line"]]), ref["excerpt"])

    def test_another_app_preflight_review_or_wrong_step_order_cannot_substitute(self):
        events = [{"kind": "raw_record", "endpoint": "workbuddy", "round_id": "fixture",
                   "action": action, "docs": 1}
                  for action in ("close_requested", "closed", "launched", "window", "docs_ready")]
        self.assertTrue(workbuddy_restart_complete(events))
        variants = [copy.deepcopy(events) for _ in range(4)]
        for row in variants[0]:
            row["endpoint"] = "opencode"
        variants[1][0]["close_skipped"] = True
        variants[2][0]["kind"] = "review_claim"
        variants[3].reverse()
        for variant in variants:
            self.assertFalse(workbuddy_restart_complete(variant))

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/09e-workbuddy-restart-evidence.json',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
