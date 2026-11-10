
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
from evidence_contract import web2_complete


class Web2EvidenceIntegrity(unittest.TestCase):
    def test_current_bundle_preserves_source_hashes_and_missing_live_status(self):
        bundle = json.loads((ROOT / "evidence/WEB2-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["offline_work_status"], "complete")
        self.assertEqual(bundle["live_evidence_status"], "pending")
        self.assertEqual(bundle["new_ui_actions"], 0)
        self.assertFalse(web2_complete(bundle["events"]))
        for ref in bundle["references"]:
            raw = Path(ref["source"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ref["sha256"])
            rows = raw.decode("utf-8-sig").splitlines()
            for excerpt in ref["excerpts"]:
                self.assertEqual(rows[excerpt["line"]-1], excerpt["text"])

    def test_expert_other_endpoint_mismatched_token_and_review_claim_cannot_fill_gap(self):
        events = [{"kind": "raw_record", "endpoint": "grok", "round_id": "fixture", "exit": 0,
                   "action": action, "model": "fast", "purpose": "test", "token": "fixture-token"}
                  for action in ("choose", "send_new", "read")]
        self.assertTrue(web2_complete(events))
        variants = [copy.deepcopy(events) for _ in range(4)]
        variants[0][0]["model"] = "expert"
        variants[1][0]["endpoint"] = "gemini"
        variants[2][2]["token"] = "different"
        variants[3][0]["kind"] = "review_claim"
        for variant in variants:
            self.assertFalse(web2_complete(variant))

def setUpModule():
    """B22：公开仓库不带 evidence 夹具，缺了就整模块跳过（本地有夹具时照常跑）。

    这些夹具是运行现场抓下来的 dump，含真实对话地址；不进公开仓库。
    """
    missing = [ROOT / p for p in ('evidence/WEB2-evidence.json',) if not (ROOT / p).exists()]
    if missing:
        raise unittest.SkipTest("缺少夹具：" + ", ".join(str(p) for p in missing))
