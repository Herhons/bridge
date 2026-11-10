
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qi import failure, runlog, tier_guard as tg, ui, workbuddy as wb


def node(kind, name="", children=()):
    n = ui.Node(kind, name, SimpleNamespace(IsOffscreen=False, IsEnabled=True))
    n.children = list(children)
    for c in n.children:
        c.parent = n
    return n


class PostWriteGate(unittest.TestCase):
    def exercise(self, fault=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table = tg.load_tiers()
            table["endpoints"]["workbuddy"]["dimensions"]["model"].update(
                test=["fixture-low"], review=["fixture-high"], accept=["fixture-high"])
            config = root / "tiers.json"
            config.write_text(json.dumps(table), encoding="utf-8")
            tg.write_receipt("workbuddy", target="fixture-low", readback="fixture-low", root=root)
            events = []
            model = node("ComboBoxControl", "Select model: fixture-low")
            tree = node("DocumentControl", children=[model])
            state = {"value": "", "fact": {"hwnd": None, "pid": None, "process_start_time": None}}
            screen = SimpleNamespace(target_hwnd=None, grab=lambda *a: np.zeros((2, 2, 3), dtype=np.uint8))

            class FixtureConversation:
                read_via = copy_note = None
                site = "workbuddy"

                def __init__(self, *args):
                    self.screen = screen

                def read(self, **kwargs):
                    return SimpleNamespace(root=tree, value=state["value"], busy=False,
                                           copies=[], editor=None, url="app://wb")

                def empty(self, view):
                    return view.value == ""

                def write(self, view, prompt):
                    events.append("write")
                    self_test.assertFalse(tg.read_receipt("workbuddy", root=root).get("consumed"))
                    state["value"] = "wrong input" if fault == "input" else prompt
                    if fault == "tier":
                        model.name = "Select model: fixture-high"
                    if fault == "identity":
                        state["fact"]["pid"] = 99
                    if fault == "hwnd":
                        state["fact"]["hwnd"] = 77
                    if fault == "start":
                        state["fact"]["process_start_time"] = 999
                    if fault == "human":
                        tree.children.append(node("TextControl", "Verify you are human"))
                    if fault == "expired":
                        path = tg.receipt_path("workbuddy", root=root)
                        data = json.loads(path.read_text())
                        data["created_at"] = "2020-01-01T00:00:00+00:00"
                        path.write_text(json.dumps(data), encoding="utf-8")

                def submit(self, view):
                    self_test.assertTrue(tg.read_receipt("workbuddy", root=root)["consumed"])
                    events.append("submit")

            self_test = self
            original_consume = tg.consume

            def consume(*args, **kwargs):
                events.append("consume")
                return original_consume(*args, **kwargs)

            stamp = {"hwnd": None, "doc_rid": [1], "foreground": True}
            pages = {"n": 0}

            def page_stamp(*args):
                events.append("stamp")
                pages["n"] += 1
                if fault == "document":
                    return {"error": "docs lost"}
                data = dict(stamp)
                # 文档身份漂移：同一 URL 下 SPA 换了文档（新对话常只换树不换地址）
                if fault == "doc_rid" and pages["n"] > 1:
                    data["doc_rid"] = [2, 3]
                return data

            _, args = wb.parse_args(["offline-fixture", "--continue", "--purpose", "test"], sending=True)
            out = io.StringIO()
            with mock.patch.object(tg, "ROOT", root), \
                    mock.patch.object(tg, "TIERS_FILE", config), \
                    mock.patch.object(tg, "fingerprint", side_effect=lambda hwnd: dict(state["fact"])), \
                    mock.patch.object(tg, "consume", side_effect=consume), \
                    mock.patch.object(wb, "open_app", return_value=screen), \
                    mock.patch.object(wb, "controls", return_value=list(tree.walk())), \
                    mock.patch.object(wb, "ReplyConversation", FixtureConversation), \
                    mock.patch.object(wb, "wait_reply", return_value={"ok": True, "stage": "answered"}), \
                    mock.patch.object(wb.sendgate, "page_stamp", side_effect=page_stamp), \
                    mock.patch.object(failure, "FAIL_DIR", root / "fail"), \
                    mock.patch.object(failure, "_LAST", {}), \
                    mock.patch.object(failure, "_LAST_PATH", {}), \
                    mock.patch.object(runlog, "RUNS", root / "logs"), contextlib.redirect_stdout(out):
                code = wb._run(args, sending=True)
            result = json.loads(out.getvalue().splitlines()[-1])
            evidence = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "fail").glob("*.json")]
            pngs = list((root / "fail").glob("*.png"))
            return code, result, events, evidence, len(pngs)

    def test_actual_entry_write_readback_consume_submit_order(self):
        code, result, events, evidence, _ = self.exercise()
        self.assertEqual(code, 0, result)
        self.assertEqual(events.count("submit"), 1)
        self.assertEqual(events.count("consume"), 1)
        self.assertLess(events.index("write"), events.index("consume"))
        self.assertLess(events.index("consume"), events.index("submit"))
        self.assertEqual(evidence, [])

    def test_tier_drift_during_write_is_rejected_and_actual_evidence_is_written(self):
        self.assert_rejected("tier", "tier_drift")

    def test_identity_drift_during_write_is_rejected(self):
        self.assert_rejected("identity", "receipt_identity")

    def test_window_handle_drift_during_write_is_rejected(self):
        """只改 PID 不够：hwnd 换了（窗口被换掉）同样必须拒发并留证。"""
        self.assert_rejected("hwnd", "receipt_identity")

    def test_process_start_drift_during_write_is_rejected(self):
        """同一 PID 重新拉起（启动时间变了）也算换了窗口。"""
        self.assert_rejected("start", "receipt_identity")

    def test_document_identity_drift_before_submit_is_rejected(self):
        """同 URL 但文档 runtime id 变了：复核之后、点击之前必须停手。"""
        self.assert_rejected("doc_rid", "page_changed")

    def test_human_after_write_returns_without_wait_or_submit(self):
        self.assert_rejected("human", "cloudflare", code=1, stage="human_check")

    def test_input_mismatch_has_one_screenshot_and_json(self):
        self.assert_rejected("input", None, code=1, stage="input_mismatch")

    def test_expiry_during_write_is_rejected(self):
        self.assert_rejected("expired", "expired")

    def test_last_moment_document_loss_is_rejected(self):
        self.assert_rejected("document", "page_changed")

    def assert_rejected(self, fault, reason, code=5, stage="tier_unconfirmed"):
        actual, result, events, evidence, pngs = self.exercise(fault)
        self.assertEqual(actual, code, result)
        self.assertEqual(result["stage"], stage)
        if reason:
            self.assertEqual(result["reason"], reason)
        self.assertNotIn("submit", events)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(pngs, 1)
        self.assertEqual(evidence[0]["stage"], stage)


if __name__ == "__main__":
    unittest.main()
