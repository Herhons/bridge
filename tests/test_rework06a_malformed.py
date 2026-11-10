"""CARD-06a 返工：档位表**格式错误**必须和"没配置"一样，从真实入口按 5 拒发。

独立验收反例（2026-11-06）：`{"endpoints": ["workbuddy"]}` / `{"endpoints": "workbuddy"}`
以前直接对 list/string 调 `.get` → AttributeError 从 `_run` 漏出，既不是
`tier_unconfirmed`/5，也没有把失败回执作废；无效 JSON / 根节点非对象同理。

这里走 `wb._run`（真实入口）+ 真实 `gate_or_exit`/`check`/`consume`，
只把 UI 与最终 `ask` 换成假件——证明的是入口调用链，不是某个纯函数。
"""
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qi import failure, runlog, tier_guard as tg, workbuddy as wb

INVALID_JSON = object()
MISSING = object()

# (名字, 配置内容, 允许的 reason) —— 全部是"外层配置格式错误"，不是某个用途档位的问题。
# `entry_is_list` 走的是"这个端登记不是对象"，按 endpoint_not_in_table 拒也算对，
# 但同样必须是 5、必须不发送、必须作废回执。
BROKEN = [
    ("endpoints_is_list", {"endpoints": ["workbuddy"]}, ("table_malformed",)),
    ("endpoints_is_string", {"endpoints": "workbuddy"}, ("table_malformed",)),
    ("endpoints_is_number", {"endpoints": 3}, ("table_malformed",)),
    ("invalid_json", INVALID_JSON, ("table_malformed",)),
    ("root_is_list", [], ("table_malformed",)),
    ("root_is_string", "workbuddy", ("table_malformed",)),
    ("missing_file", MISSING, ("table_unreadable",)),
    ("entry_is_list", {"endpoints": {"workbuddy": ["model"]}},
     ("endpoint_not_in_table",)),
]


class MalformedTierTableAtEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "tiers.json"
        self.screen = SimpleNamespace(target_hwnd=None)
        self.table = tg.load_tiers()

    def write_config(self, payload):
        if payload is MISSING:
            self.path.unlink(missing_ok=True)
            return
        if payload is INVALID_JSON:
            self.path.write_text("{ this is not json", encoding="utf-8")
            return
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def run_entry(self, payload, purpose="review", model="fixture-model"):
        self.write_config(payload)
        tg.write_receipt("workbuddy", target=model, readback=model, root=self.root)
        _, args = wb.parse_args(["offline-fixture", "--continue", "--purpose", purpose],
                                sending=True)
        out = io.StringIO()
        with mock.patch.object(tg, "ROOT", self.root), \
                mock.patch.object(tg, "TIERS_FILE", self.path), \
                mock.patch.object(wb, "open_app", return_value=self.screen), \
                mock.patch.object(wb, "controls", return_value=[]), \
                mock.patch.object(wb, "current_model", return_value=model), \
                mock.patch.object(wb, "ReplyConversation",
                                  return_value=SimpleNamespace(read_via=None,
                                                               copy_note=None)), \
                mock.patch.object(wb, "ask",
                                  return_value={"ok": True, "stage": "answered"}) as ask, \
                mock.patch.object(failure, "FAIL_DIR", self.root / "fail"), \
                mock.patch.object(failure, "_LAST", {}), \
                mock.patch.object(failure, "_LAST_PATH", {}), \
                mock.patch.object(runlog, "RUNS", self.root / "logs"), \
                contextlib.redirect_stdout(out):
            code = wb._run(args, sending=True)
        lines = [line for line in out.getvalue().strip().splitlines() if line.strip()]
        result = json.loads(lines[-1]) if lines else None
        return code, result, ask

    def test_every_malformed_outer_config_is_exit5_without_sending(self):
        for name, payload, reasons in BROKEN:
            with self.subTest(case=name):
                code, result, ask = self.run_entry(payload)
                self.assertEqual(code, 5, (name, result))
                self.assertEqual(result["stage"], "tier_unconfirmed", name)
                self.assertEqual(result["code"], 5, name)
                self.assertIn(result["reason"], reasons, name)
                self.assertEqual(ask.call_count, 0, f"{name}: 不许走到业务发送")
                receipt = tg.read_receipt("workbuddy", root=self.root)
                self.assertTrue(receipt["consumed"], f"{name}: 失败回执必须作废")

    def test_malformed_config_keeps_exit5_and_consumes_even_for_every_purpose(self):
        for purpose in ("test", "review", "accept"):
            with self.subTest(purpose=purpose):
                code, result, ask = self.run_entry({"endpoints": ["workbuddy"]},
                                                   purpose=purpose)
                self.assertEqual((code, ask.call_count), (5, 0))
                self.assertEqual(result["reason"], "table_malformed")
                self.assertTrue(tg.read_receipt("workbuddy", root=self.root)["consumed"])

    def test_wellformed_table_still_sends_when_purpose_is_configured(self):
        data = json.loads(json.dumps(self.table))
        data["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            review=["fixture-model"])
        code, result, ask = self.run_entry(data)
        self.assertEqual(code, 0, result)
        self.assertEqual(ask.call_count, 1)

    def test_check_reports_table_malformed_and_read_only_precheck_keeps_receipt(self):
        self.write_config({"endpoints": "workbuddy"})
        tg.write_receipt("workbuddy", target="fixture-model", readback="fixture-model",
                         root=self.root)
        with mock.patch.object(tg, "TIERS_FILE", self.path):
            got = tg.check("workbuddy", "review", root=self.root)
            self.assertFalse(got["ok"])
            self.assertEqual(got["reasons"], ["table_malformed"])
            with self.assertRaises(tg.TierUnconfirmed) as caught:
                tg.gate("workbuddy", "review", root=self.root, do_consume=False)
            self.assertEqual(caught.exception.code, 5)
            self.assertFalse(tg.read_receipt("workbuddy", root=self.root).get("consumed"))

    def test_gate_failure_invalidates_the_failed_receipt(self):
        self.write_config({"endpoints": ["workbuddy"]})
        tg.write_receipt("workbuddy", target="fixture-model", readback="fixture-model",
                         root=self.root)
        with mock.patch.object(tg, "TIERS_FILE", self.path):
            with self.assertRaises(tg.TierUnconfirmed) as caught:
                tg.gate("workbuddy", "review", root=self.root)
        self.assertEqual((caught.exception.code, caught.exception.reason),
                         (5, "table_malformed"))
        self.assertTrue(tg.read_receipt("workbuddy", root=self.root)["consumed"])


if __name__ == "__main__":
    unittest.main()
