
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
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/scripts")]
from qi import chip, conversation, failure, flow, sendgate, tier_guard as tg, ui, zoom
import chatgpt_choose as choose


def node(kind, name="", children=(), state=None):
    control = SimpleNamespace(IsOffscreen=False, IsEnabled=True)
    if state is not None:
        control.GetTogglePattern = lambda: SimpleNamespace(ToggleState=state)
    item = ui.Node(kind, name, control)
    item.children = list(children)
    for child in children:
        child.parent = item
    return item


def tree_for(writer="chat", model="GPT-6", effort="高"):
    return node("DocumentControl", children=[
        node("ButtonControl", "聊天", state=1 if writer == "chat" else 0),
        node("ButtonControl", "工作", state=1 if writer == "work" else 0),
        node("ButtonControl", chip.CHATGPT_CHIP,
             children=[node("TextControl", (model + " " + effort).strip())])])


class CompositeTier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fact = {"hwnd": None, "pid": None, "process_start_time": None}
        self.screen = SimpleNamespace(target_hwnd=None, bind=lambda hwnd: None, focus=lambda: True)
        self.patchers = [mock.patch.object(tg, "ROOT", self.root),
                         mock.patch.object(tg, "fingerprint", return_value=self.fact)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def receipt(self, model="gpt6", effort="high", writer="chat"):
        return tg.write_receipt("chatgpt", target=effort, readback=effort, dimension="effort",
                                writer=writer, root=self.root, dimensions={
                                    "model": {"target": model, "readback": model},
                                    "effort": {"target": effort, "readback": effort}})

    def test_actual_choose_writes_one_complete_receipt_without_axis_overwrite(self):
        tree = tree_for()
        library = SimpleNamespace(report=lambda need: "offline", missing=lambda need: [])
        with mock.patch.object(choose.sys, "argv", ["offline", "--model", "gpt6", "--effort", "high"]), \
                mock.patch.object(choose, "Library", return_value=library), \
                mock.patch.object(choose.guard, "guard", return_value=True), \
                mock.patch.object(choose, "Screen", return_value=self.screen), \
                mock.patch.object(choose, "ensure_site", return_value=1), \
                mock.patch.object(choose.time, "sleep"), \
                mock.patch.object(choose, "_zoom_guard"), \
                mock.patch.object(choose, "set_model", return_value={"ok": True, "after": "gpt6", "writer": "chat"}), \
                mock.patch.object(choose, "set_effort", return_value={"ok": True, "after": "high", "writer": "chat"}), \
                mock.patch.object(chip, "_tree", return_value=tree), \
                mock.patch.object(tg, "_atomic_write", wraps=tg._atomic_write) as writer, \
                contextlib.redirect_stdout(io.StringIO()):
            code = choose.main()
        self.assertEqual(code, 0)
        self.assertEqual(writer.call_count, 1)
        saved = tg.read_receipt("chatgpt", root=self.root)
        self.assertEqual(saved["writer"], "chat")
        self.assertEqual(saved["composite_version"], 1)
        self.assertEqual(saved["dimensions"]["model"]["readback"], "gpt6")
        self.assertEqual(saved["dimensions"]["effort"]["readback"], "high")
        self.assertTrue(tg.check("chatgpt", "review", root=self.root)["ok"])

    def test_joint_purpose_checks_reject_each_wrong_axis(self):
        for model, effort in (("gpt56", "high"), ("gpt6", "mid")):
            with self.subTest(model=model, effort=effort):
                self.receipt(model, effort)
                out = tg.check("chatgpt", "review", root=self.root)
                self.assertFalse(out["ok"])
                self.assertEqual(out["reasons"], ["purpose_mismatch"])

    def test_single_axis_rewrite_cannot_reuse_prior_joint_confirmation(self):
        self.receipt()
        tg.write_receipt("chatgpt", target="high", readback="high", writer="chat",
                         dimension="effort", root=self.root)
        out = tg.check("chatgpt", "review", root=self.root)
        self.assertEqual(out["reasons"], ["composite_missing"])

    def test_lowest_and_highest_combinations_pass_correct_purpose_only(self):
        for purpose, model, effort in (("test", "gpt56", "instant"),
                                       ("review", "gpt6", "high"), ("accept", "gpt6", "high")):
            self.receipt(model, effort)
            got = tg.gate("chatgpt", purpose, root=self.root, reread=lambda dim: {
                "writer": "chat", "dimensions": {"model": model, "effort": effort}})
            self.assertTrue(got["ok"])
            self.assertTrue(tg.read_receipt("chatgpt", root=self.root)["consumed"])

    def test_live_snapshot_drift_or_missing_dimension_fails_even_with_unsupported_override(self):
        for value, expected in (({"writer": "chat", "dimensions": {"model": "gpt56", "effort": "high"}}, "tier_drift"),
                                ({"writer": "chat", "dimensions": {"model": "gpt6", "effort": "mid"}}, "tier_drift"),
                                ({"writer": "work", "dimensions": {"model": "gpt6", "effort": "high"}}, "tier_drift"),
                                ({"writer": "chat", "dimensions": {"effort": "high"}}, "tier_unreadable"),
                                ({"dimensions": {"model": "gpt6", "effort": "high"}}, "tier_unreadable"),
                                ({"writer": "chat", "dimensions": [1]}, "tier_unreadable")):
            with self.subTest(value=value):
                self.receipt()
                with self.assertRaises(tg.TierUnconfirmed) as caught:
                    tg.gate("chatgpt", "review", root=self.root, require_readback=False,
                            reread=lambda dim: value)
                self.assertEqual(caught.exception.reason, expected)
                self.assertTrue(tg.read_receipt("chatgpt", root=self.root)["consumed"])

    def test_snapshot_uses_one_tree_and_rejects_chip_with_only_effort(self):
        with mock.patch.object(chip, "_tree", return_value=tree_for(model="")) as reader:
            got = chip.chatgpt_snapshot(self.screen)
        self.assertIsNone(got["dimensions"]["model"])
        self.assertIsNone(got["value"])
        reader.assert_called_once()

    def test_real_web_sending_entry_uses_joint_gate_after_write(self):
        for changed in ("model", "effort", "writer", "none"):
            with self.subTest(changed=changed):
                self.receipt()
                self._flow_entry(changed)

    def _flow_entry(self, changed):
        live_tree = tree_for()
        state = {"value": "", "events": []}
        screen = self.screen
        class FixtureConversation:
            site = "chatgpt"
            def __init__(self, *a, **k):
                self.screen = screen
            def read(self, **k):
                return SimpleNamespace(root=live_tree, value=state["value"], busy=False,
                                       copies=[], url="app://offline")
            def empty(self, v):
                return v.value == ""
            def write(self, view, prompt):
                state["events"].append("write")
                state["value"] = prompt
                tree = tree_for(writer="work" if changed == "writer" else "chat",
                                model="GPT-5.6 Sol" if changed == "model" else "GPT-6",
                                effort="中" if changed == "effort" else "高")
                live_tree.children = tree.children
            def submit(self, view):
                state["events"].append("submit")
        site = flow.Site("chatgpt", "offline", "c", "s", "a", "copy", (0, 0))
        with mock.patch.object(flow, "Screen", return_value=self.screen), \
                mock.patch.object(flow, "ensure_site", return_value=1), \
                mock.patch.object(zoom, "ensure"), \
                mock.patch.object(flow, "_empty_probe", return_value=lambda v: v.value == ""), \
                mock.patch.object(conversation, "Conversation", FixtureConversation), \
                mock.patch.object(chip, "_tree", return_value=live_tree), \
                mock.patch.object(ui, "document", return_value=(live_tree, "app://offline")), \
                mock.patch.object(ui, "tree", side_effect=lambda root: root), \
                mock.patch.object(ui, "title_of", return_value=""), \
                mock.patch.object(sendgate, "page_stamp", return_value={"doc_rid": [1]}), \
                mock.patch.object(flow, "uia_answer", return_value=None), \
                mock.patch.object(failure, "FAIL_DIR", self.root / "fail"):
            got = flow.ask(site, "offline-fixture", mode="continue", timeout=0, purpose="review")
        self.assertEqual(state["events"][0], "write")
        if changed == "none":
            self.assertIn("submit", state["events"])
        else:
            self.assertNotIn("submit", state["events"])
            self.assertEqual(got["stage"], "tier_unconfirmed", got)
            self.assertEqual(got["reason"], "tier_drift")


if __name__ == "__main__":
    unittest.main()
