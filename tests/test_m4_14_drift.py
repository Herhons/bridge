"""Tests for M4-14: Model and UI drift self-detection, diagnostic handoff, and === markers."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from engine.base import EndpointDriver
from engine.drift import (
    DEFAULT_DRIFT_DIR,
    LATEST_FILENAME,
    detect_model_drift,
    format_drift_handoff_section,
    load_latest_drift_report,
    summarize_ui_controls,
    write_drift_report,
)
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo
from scripts import auto_heal_loop


def _snap(nodes: list[UINode], *, snap_id: str = "s1", rect=(0, 0, 1400, 900)) -> Snapshot:
    refs = assign_refs(nodes, snap_id)
    return Snapshot(snapshot_id=snap_id, hwnd=101, title="Test", url="", nodes=nodes, refs=refs, window_rect=rect)


class TestM414DriftDetection(unittest.TestCase):
    def test_01_detect_model_drift(self):
        # 1. New model detected (e.g. Gemini introduced a new tier beyond configured)
        cfg_models = ["Gemini 2.0 Flash", "Gemini 1.5 Pro"]
        cfg_aliases = {"flash": "Gemini 2.0 Flash", "pro": "Gemini 1.5 Pro"}
        observed = ["Gemini 2.0 Flash", "Gemini 1.5 Pro", "Gemini 2.5 Flash Ultra"]

        res = detect_model_drift("gemini", observed, cfg_models, cfg_aliases)
        self.assertTrue(res["drift_detected"])
        self.assertEqual(res["new_models"], ["Gemini 2.5 Flash Ultra"])
        self.assertEqual(res["missing_models"], [])
        self.assertIn("new live models: Gemini 2.5 Flash Ultra", res["reason"])

        # 2. Missing configured model detected
        observed_missing = ["Gemini 2.0 Flash"]
        res_missing = detect_model_drift("gemini", observed_missing, cfg_models, cfg_aliases)
        self.assertTrue(res_missing["drift_detected"])
        self.assertEqual(res_missing["new_models"], [])
        self.assertIn("Gemini 1.5 Pro", res_missing["missing_models"])

        # 3. Clean match with aliases
        res_clean = detect_model_drift("gemini", ["Gemini 2.0 Flash", "Gemini 1.5 Pro"], cfg_models, cfg_aliases)
        self.assertFalse(res_clean["drift_detected"])
        self.assertEqual(res_clean["new_models"], [])
        self.assertEqual(res_clean["missing_models"], [])
        self.assertEqual(res_clean["reason"], "no model drift detected")

    def test_02_summarize_ui_controls(self):
        # Mixed dicts, objects, empty nodes, limit truncation
        class DummyNode:
            def __init__(self, role, name, rect):
                self.role = role
                self.name = name
                self.rect = rect

        nodes = [
            {"role": "ButtonControl", "name": "Send", "rect": [10, 20, 30, 40]},
            DummyNode("EditControl", "Composer", (50, 60, 70, 80)),
            {"role": "", "name": ""},  # Should be omitted
            DummyNode("", "", None),    # Should be omitted
            {"role": "TextControl", "name": "Extra Item", "rect": [0, 0, 10, 10]},
        ]
        summary = summarize_ui_controls(nodes, limit=2)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["role"], "ButtonControl")
        self.assertEqual(summary[0]["name"], "Send")
        self.assertEqual(summary[0]["rect"], [10, 20, 30, 40])
        self.assertEqual(summary[1]["role"], "EditControl")
        self.assertEqual(summary[1]["name"], "Composer")
        self.assertEqual(summary[1]["rect"], [50, 60, 70, 80])

    def test_03_write_and_load_latest_drift_report(self):
        with tempfile.TemporaryDirectory() as td:
            drift_dir = Path(td) / "drift"
            t0 = datetime(2026, 11, 21, 15, 30, 0)
            report_file = write_drift_report(
                "gemini",
                kind="model_drift",
                reason="new live models: Gemini 2.5 Flash",
                observed_options=["Gemini 2.5 Flash"],
                new_models=["Gemini 2.5 Flash"],
                missing_models=[],
                ui_controls=[{"role": "RadioButtonControl", "name": "Gemini 2.5 Flash"}],
                extra={"trace_id": "test-123"},
                drift_dir=drift_dir,
                now=t0,
            )
            self.assertTrue(report_file.is_file())
            latest_file = drift_dir / LATEST_FILENAME
            self.assertTrue(latest_file.is_file())

            # Load latest unacknowledged
            latest = load_latest_drift_report(drift_dir=drift_dir, only_unacked=True)
            self.assertIsNotNone(latest)
            self.assertEqual(latest["endpoint"], "gemini")
            self.assertEqual(latest["kind"], "model_drift")
            self.assertFalse(latest["acknowledged"])
            self.assertEqual(latest["new_models"], ["Gemini 2.5 Flash"])

            # Acknowledge and ensure only_unacked=True filters it
            latest_payload = json.loads(latest_file.read_text(encoding="utf-8"))
            latest_payload["acknowledged"] = True
            latest_file.write_text(json.dumps(latest_payload), encoding="utf-8")

            self.assertIsNone(load_latest_drift_report(drift_dir=drift_dir, only_unacked=True))
            # But loading without only_unacked returns it
            self.assertIsNotNone(load_latest_drift_report(drift_dir=drift_dir, only_unacked=False))

    def test_04_format_drift_handoff_section(self):
        self.assertEqual(format_drift_handoff_section(None), "")
        report = {
            "endpoint": "workbuddy",
            "kind": "model_drift",
            "timestamp": "2026-11-21T15:30:00",
            "reason": "new live models: Neon V5",
            "observed_options": ["Hy4", "Neon V5"],
            "new_models": ["Neon V5"],
            "missing_models": ["Hy3"],
            "ui_controls": [{"role": "MenuItemControl", "name": "Neon V5"}],
            "report_file": "runs/drift/workbuddy_model_drift.json",
        }
        text = format_drift_handoff_section(report)
        self.assertIn("## Model / UI Drift Diagnostic Report", text)
        self.assertIn("- Endpoint: workbuddy", text)
        self.assertIn("- Kind: model_drift", text)
        self.assertIn("- Reason: new live models: Neon V5", text)
        self.assertIn("- Observed live options: Hy4, Neon V5", text)
        self.assertIn("- New unconfigured models: Neon V5", text)
        self.assertIn("- Missing configured models: Hy3", text)
        self.assertIn("- Visible UI controls sample: MenuItemControl:Neon V5", text)
        self.assertIn("- Report file: runs/drift/workbuddy_model_drift.json", text)

    def test_05_driver_models_live_detects_drift_and_persists_report(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        sel_btn = UINode(role="ButtonControl", name="Select model", rect=(400, 820, 200, 30))
        s1 = _snap([sel_btn])
        # Options include an unconfigured model label
        s2 = _snap([
            sel_btn,
            UINode(role="MenuItemControl", name="Hy4 flash 限时免费 0.00x", rect=(400, 640, 220, 30)),
            UINode(role="MenuItemControl", name="Totally-Unconfigured-Brand-New-Model", rect=(400, 680, 220, 30)),
        ], snap_id="s2")
        snaps = [s1, s2]
        i = {"v": 0}
        esc_sent = []

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "workbuddy",
                snapshot_fn=lambda _h: snaps[min(i.__setitem__("v", i["v"] + 1) or i["v"] - 1, 1)],
                window_fn=lambda: win,
                invoke_fn=lambda _n, _h: "stub:click",
                hotkey_fn=lambda *k: esc_sent.append(k),
                sleep_fn=lambda _s: None,
            )
            drv.drift_dir = Path(td) / "drift"

            res = drv.models(live=True, headed=True)
            self.assertTrue(res["ok"])
            self.assertIn("drift", res)
            self.assertTrue(res["drift"]["drift_detected"])
            self.assertIn("drift_report", res)
            self.assertTrue(Path(res["drift_report"]).is_file())
            self.assertTrue((drv.drift_dir / LATEST_FILENAME).is_file())

    def test_06_driver_switch_model_option_not_found_records_drift(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        sel_btn = UINode(role="ButtonControl", name="Select model", rect=(400, 820, 200, 30))
        s1 = _snap([sel_btn])
        s2 = _snap([
            sel_btn,
            UINode(role="MenuItemControl", name="Existing Model A", rect=(400, 640, 220, 30)),
        ], snap_id="s2")
        snaps = [s1, s2]
        i = {"v": 0}
        esc_sent = []

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "workbuddy",
                snapshot_fn=lambda _h: snaps[min(i.__setitem__("v", i["v"] + 1) or i["v"] - 1, 1)],
                window_fn=lambda: win,
                invoke_fn=lambda _n, _h: "stub:click",
                hotkey_fn=lambda *k: esc_sent.append(k),
                sleep_fn=lambda _s: None,
            )
            drv.drift_dir = Path(td) / "drift"

            res = drv.switch_model(win, s1, "NoSuchModelWanted", headed=True)
            self.assertFalse(res["switched"])
            self.assertEqual(res["reason"], "option_not_found")
            self.assertIn("drift_report", res)
            report_path = Path(res["drift_report"])
            self.assertTrue(report_path.is_file())
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "option_not_found")

    def test_06b_driver_switch_model_selector_not_found_records_drift(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="WorkBuddy.exe", title="WorkBuddy")
        # Snapshot has no model selector button
        s1 = _snap([])

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "workbuddy",
                snapshot_fn=lambda _h: s1,
                window_fn=lambda: win,
                sleep_fn=lambda _s: None,
            )
            drv.drift_dir = Path(td) / "drift"

            res = drv.switch_model(win, s1, "any-model", headed=True)
            self.assertFalse(res["switched"])
            self.assertEqual(res["reason"], "selector_not_found")
            self.assertIn("drift_report", res)
            report_path = Path(res["drift_report"])
            self.assertTrue(report_path.is_file())
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "selector_not_found")

    def test_07_driver_ask_composer_not_found_records_drift(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="gemini.exe", title="Gemini")
        # Empty snapshot (no composer edit control)
        s1 = _snap([])

        with tempfile.TemporaryDirectory() as td:
            drv = EndpointDriver(
                "gemini",
                snapshot_fn=lambda _h: s1,
                window_fn=lambda: win,
                sleep_fn=lambda _s: None,
                lock_path=Path(td) / "ui.lock",
                ledger_path=Path(td) / "ledger.json",
            )
            drv.drift_dir = Path(td) / "drift"
            drv._evidence_dir = Path(td) / "fail"

            res = drv.ask("test prompt", headed=False)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "composer_not_found")
            self.assertIn("drift_report", res)
            report_path = Path(res["drift_report"])
            self.assertTrue(report_path.is_file())
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "composer_not_found")

    def test_08_auto_heal_loop_build_handoff_bundle_includes_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            drift_dir = root / "runs" / "drift"
            write_drift_report(
                "gemini",
                kind="model_drift",
                reason="new live models: Gemini 3.0 Ultra",
                observed_options=["Gemini 3.0 Ultra"],
                new_models=["Gemini 3.0 Ultra"],
                missing_models=[],
                drift_dir=drift_dir,
            )
            bundle_file = root / "runs" / "brain_handoff.md"
            # Point auto_heal_loop to our isolated directory
            orig_runs = auto_heal_loop.RUNS
            try:
                auto_heal_loop.RUNS = root / "runs"
                res_path = auto_heal_loop.build_handoff_bundle("test-drift-trigger", path=bundle_file)
                self.assertTrue(res_path.is_file())
                content = res_path.read_text(encoding="utf-8")
                self.assertIn("## Model / UI Drift Diagnostic Report", content)
                self.assertIn("- Endpoint: gemini", content)
                self.assertIn("new live models: Gemini 3.0 Ultra", content)
            finally:
                auto_heal_loop.RUNS = orig_runs

    def test_09_auto_heal_loop_task_and_patch_markers_support_both_syntaxes(self):
        # 1. Equals syntax ===WORKBUDDY_TASK_START===
        task_eq = (
            "Here is the task:\n"
            "===WORKBUDDY_TASK_START===\n"
            "Do step 1 and step 2\n"
            "===WORKBUDDY_TASK_END===\n"
        )
        self.assertEqual(auto_heal_loop.extract_task_card(task_eq), "Do step 1 and step 2")

        # 2. Angle brackets syntax <<<WORKBUDDY_TASK_START>>>
        task_angle = (
            "Here is the task:\n"
            "<<<WORKBUDDY_TASK_START>>>\n"
            "Do step A and step B\n"
            "<<<WORKBUDDY_TASK_END>>>\n"
        )
        self.assertEqual(auto_heal_loop.extract_task_card(task_angle), "Do step A and step B")

        # 3. Equals syntax ===BRIDGE_PATCH_START===
        patch_eq = (
            "Here is the diff:\n"
            "===BRIDGE_PATCH_START===\n"
            "diff --git a/test.py b/test.py\n"
            "===BRIDGE_PATCH_END===\n"
        )
        self.assertEqual(
            auto_heal_loop.extract_patch(patch_eq),
            "diff --git a/test.py b/test.py",
        )

        # 4. Angle brackets syntax <<<BRIDGE_PATCH_START>>>
        patch_angle = (
            "Here is the diff:\n"
            "<<<BRIDGE_PATCH_START>>>\n"
            "diff --git a/old.py b/old.py\n"
            "<<<BRIDGE_PATCH_END>>>\n"
        )
        self.assertEqual(
            auto_heal_loop.extract_patch(patch_angle),
            "diff --git a/old.py b/old.py",
        )

    def test_10_auto_heal_loop_all_done_sign_off_support_both_syntaxes(self):
        # Sign-off with ===BRIDGE_ALL_DONE===
        self.assertTrue(auto_heal_loop.brain_signed_off_all_done("===BRIDGE_ALL_DONE==="))
        self.assertTrue(auto_heal_loop.brain_signed_off_all_done("Done! ===BRIDGE_ALL_DONE=== Great job!"))

        # Sign-off with <<<BRIDGE_ALL_DONE>>>
        self.assertTrue(auto_heal_loop.brain_signed_off_all_done("<<<BRIDGE_ALL_DONE>>>"))
        self.assertTrue(auto_heal_loop.brain_signed_off_all_done("All finished <<<BRIDGE_ALL_DONE>>>"))

        # False negatives
        self.assertFalse(auto_heal_loop.brain_signed_off_all_done("No marker here"))
        self.assertFalse(auto_heal_loop.brain_signed_off_all_done(""))

        # Redaction strips both tokens
        text = "Check <<<BRIDGE_ALL_DONE>>> and ===BRIDGE_ALL_DONE=== in text"
        redacted = auto_heal_loop._redact_signoff_marker(text)
        self.assertNotIn("<<<BRIDGE_ALL_DONE>>>", redacted)
        self.assertNotIn("===BRIDGE_ALL_DONE===", redacted)

    def test_11_marker_hints_do_not_match_regexes(self):
        # The syntax hint must never match task/patch/signoff regexes
        hint = auto_heal_loop.MARKER_SYNTAX_HINT
        self.assertIsNone(auto_heal_loop.TASK_RE.search(hint))
        self.assertIsNone(auto_heal_loop.PATCH_RE.search(hint))
        self.assertIsNone(auto_heal_loop.ALL_DONE_SIGN_OFF_RE.search(hint))


if __name__ == "__main__":
    unittest.main()
