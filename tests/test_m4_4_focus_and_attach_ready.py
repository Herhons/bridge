"""Offline coverage for M4-4: placeholder-safe card extraction, strict focus
guard by default, pre-activation focus probe and composer readiness gating.

Everything here is offline:

* the scheduler module is exercised as a pure library (no git, no subprocess);
* :class:`EndpointDriver` runs with injected window/snapshot/scroll doubles and
  a mocked ``focus_window`` — no real window is touched, no clipboard is used.

The 2026-11-21 13:36 incident is pinned as a regression test: the brain echoed
the prompt template, ``extract_task_card`` returned the literal
``...任务卡内容...`` example and the daemon dispatched it verbatim to an executor.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402
import engine.base as base_mod  # noqa: E402
import engine.window as win_mod  # noqa: E402
from engine.base import EndpointDriver  # noqa: E402
from engine.config import get_endpoint_config  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402

TASK_START = "<<<WORKBUDDY_TASK_START>>>"
TASK_END = "<<<WORKBUDDY_TASK_END>>>"
PATCH_START = "<<<BRIDGE_PATCH_START>>>"
PATCH_END = "<<<BRIDGE_PATCH_END>>>"

REAL_CARD = "请把 tests/test_demo.py 里 test_a 的断言改成 >= 2，然后跑全量单测确认 rc=0。"
REAL_DIFF = (
    "diff --git a/demo.txt b/demo.txt\n"
    "--- a/demo.txt\n"
    "+++ b/demo.txt\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new"
)

PRIMARY = (0, 0, 2560, 1440)
SECONDARY = (-1440, -653, 0, 1907)


def _echo_template() -> str:
    """Exactly what Gemini echoed on 2026-11-21 13:36 (the bug trigger)."""
    return (
        "标记说明：\n"
        "%s\n...unified diff...\n%s\n\n%s\n...任务卡内容...\n%s\n\n"
        % (PATCH_START, PATCH_END, TASK_START, TASK_END)
    )


def _snap(nodes: list[UINode], snap_id: str = "s1") -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=101,
        title="T",
        url="",
        nodes=nodes,
        refs=assign_refs(nodes, snap_id),
        window_rect=(0, 0, 1000, 800),
    )


def _composer_snap(extra: list[UINode] | None = None, snap_id: str = "s1") -> Snapshot:
    nodes = [UINode(role="EditControl", name="Ask Gemini", rect=(10, 700, 500, 40))]
    nodes.extend(extra or [])
    return _snap(nodes, snap_id)


ATTACH_BUTTON = UINode(role="ButtonControl", name="上传和工具", rect=(300, 720, 40, 30))


class PlaceholderExtractionTests(unittest.TestCase):
    def test_task_card_skips_the_echoed_placeholder(self):
        reply = _echo_template() + "\n%s\n%s\n%s\n" % (TASK_START, REAL_CARD, TASK_END)
        self.assertEqual(ahl.extract_task_card(reply), REAL_CARD)
        # The trigger case: template echo only, no real answer.
        self.assertEqual(ahl.extract_task_card(_echo_template()), "")

    def test_patch_skips_the_echoed_placeholder(self):
        reply = _echo_template() + "\n%s\n%s\n%s\n" % (PATCH_START, REAL_DIFF, PATCH_END)
        self.assertEqual(ahl.extract_patch(reply), REAL_DIFF)
        self.assertEqual(ahl.extract_patch(_echo_template()), "")

    def test_last_valid_block_wins_over_an_earlier_real_one(self):
        first = "%s\n第一版任务卡内容已作废但足够长足够长足够长足够长\n%s" % (TASK_START, TASK_END)
        second = "%s\n%s\n%s" % (TASK_START, REAL_CARD, TASK_END)
        self.assertEqual(ahl.extract_task_card(first + second), REAL_CARD)

    def test_placeholder_predicate(self):
        for body in ("", "   ", "\n", "...任务卡内容...", "...unified diff...", "...x..."):
            self.assertTrue(ahl._is_placeholder_block(body), body)
        # A short-but-real card has no ellipsis: must survive.
        self.assertFalse(ahl._is_placeholder_block("跑一遍全量单测"))
        self.assertFalse(ahl._is_placeholder_block(REAL_CARD))

    def test_contract_rejects_a_pure_echo(self):
        self.assertFalse(ahl.reply_has_contract(_echo_template()))
        self.assertTrue(
            ahl.reply_has_contract("%s\n%s\n%s" % (TASK_START, REAL_CARD, TASK_END))
        )

    def test_prompts_are_not_matchable_by_their_own_markers(self):
        task_prompt = ahl.build_task_prompt(1, "- [ ] M4-4（P0）：真机闭环", "016ec4c")
        gemini_prompt = ahl.build_gemini_prompt(1, Path("runs/crash_report.md"))
        for prompt in (task_prompt, gemini_prompt):
            self.assertIsNone(ahl.TASK_RE.search(prompt))
            self.assertIsNone(ahl.PATCH_RE.search(prompt))
        # The marker names must still be discoverable by the brain.
        self.assertIn("WORKBUDDY_TASK_START", task_prompt)
        self.assertIn("BRIDGE_PATCH_START", gemini_prompt)


class FocusStrictTests(unittest.TestCase):
    def _driver(self, *, config=None, bridge_config=None, snap=None, tmpdir=None, **extra):
        return EndpointDriver(
            "gemini",
            config=config,
            bridge_config=bridge_config if bridge_config is not None else {},
            snapshot_fn=lambda _h: snap if snap is not None else _composer_snap(),
            window_fn=lambda: WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini"),
            sleep_fn=lambda _s: None,
            ledger_path=Path(tmpdir) / "ledger.json",
            evidence_dir=Path(tmpdir) / "fail",
            lock_path=Path(tmpdir) / "ui.lock",
            **extra,
        )

    def test_strict_is_on_by_default_and_config_can_relax_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_drv = self._driver(config={}, tmpdir=tmp)
            self.assertTrue(default_drv.focus_guard_strict())

            relaxed = self._driver(config={"focus_guard_strict": False}, tmpdir=tmp)
            self.assertFalse(relaxed.focus_guard_strict())

            relaxed_bridge = self._driver(
                config={}, bridge_config={"focus_guard_strict": False}, tmpdir=tmp
            )
            self.assertFalse(relaxed_bridge.focus_guard_strict())

    def test_primary_foreground_is_never_activated(self):
        """A window on x >= 0 owns focus → skip SetForegroundWindow entirely."""
        calls: list[int] = []
        rects = {0x1111: PRIMARY}
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(config={}, snap=_snap([]), tmpdir=tmp)
            with mock.patch.object(
                base_mod, "focus_window", side_effect=lambda hwnd: calls.append(hwnd) or True
            ), mock.patch.object(win_mod, "foreground_hwnd", return_value=0x1111), \
                mock.patch.object(win_mod, "window_rect", side_effect=lambda h: rects.get(h)):
                res = drv.ask("只回复：收到", headed=True, timeout=5, force=True)
        self.assertEqual(calls, [], "focus_window must not be called while the primary owns focus")
        self.assertEqual(res["error"], "composer_not_found")
        self.assertNotEqual(res["error"], "focus_taken")

    def test_stealing_primary_focus_still_aborts_under_the_default(self):
        """No explicit config: the new default strictness must catch a takeover."""
        cfg = dict(get_endpoint_config("gemini"))
        cfg.pop("focus_guard_strict", None)
        rects = {0x1111: PRIMARY, 0x2222: SECONDARY}
        seq = [0, 0x1111, 0x2222]  # probe sees "primary free", then the guard runs
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(config=cfg, snap=_composer_snap(), tmpdir=tmp)
            with mock.patch.object(base_mod, "focus_window", return_value=True), \
                mock.patch.object(
                    win_mod, "foreground_hwnd",
                    side_effect=lambda: seq.pop(0) if seq else 0x2222,
                ), mock.patch.object(win_mod, "window_rect", side_effect=lambda h: rects.get(h)):
                res = drv.ask("只回复：收到", headed=True, timeout=5, force=True)
            entry = drv.ledger.get("只回复：收到", "gemini")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "focus_taken")
        self.assertIn("focus_guard:primary_lost", res["focus_event"])
        self.assertEqual(entry["state"], "failed_retryable")
        self.assertTrue(drv.ledger.check("只回复：收到", "gemini")[0])


class ComposerReadyTests(unittest.TestCase):
    def _driver(self, snaps, *, scroll_calls=None, tmpdir=None, config=None, **extra):
        seq = list(snaps)
        state = {"i": 0}

        def _next_snap(_hwnd):
            index = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[index]

        return EndpointDriver(
            "gemini",
            config=config if config is not None else dict(get_endpoint_config("gemini")),
            bridge_config={"poll_interval": 0.0},
            snapshot_fn=_next_snap,
            window_fn=lambda: WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini"),
            scroll_fn=lambda rect: (scroll_calls.append(rect), True)[1] if scroll_calls is not None else True,
            sleep_fn=lambda _s: None,
            ledger_path=Path(tmpdir) / "ledger.json",
            evidence_dir=Path(tmpdir) / "fail",
            lock_path=Path(tmpdir) / "ui.lock",
            **extra,
        )

    def test_scroll_and_resnapshot_finds_the_attach_button(self):
        first = _composer_snap()                     # composer only: 上传和工具 not rendered yet
        second = _composer_snap([ATTACH_BUTTON], "s2")
        scroll_calls: list = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            # ``first`` is handed in directly, so the injector only has to serve
            # the re-snapshot that follows the scroll.
            drv = self._driver([second], scroll_calls=scroll_calls, tmpdir=tmp)
            snap, payload = drv._ensure_composer_ready(
                WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini"),
                first,
                warnings=warnings,
            )
        self.assertIsNone(payload)
        self.assertEqual(snap.snapshot_id, "s2")
        self.assertIsNotNone(drv._find_attach_control(snap, "attach_button"))
        self.assertEqual(len(scroll_calls), 1, "must scroll to the bottom once before re-snapshotting")
        self.assertEqual(warnings, ["composer_ready_after_retry:1"])

    def test_focus_theft_during_attach_aborts_before_submit(self):
        """An attach click that raises the window must not be followed by a send."""

        def _theft(*_args, **_kwargs):
            drv.last_focus_event = "focus_guard:primary_lost:0x1->0x2"
            return ["stub:dialog"]

        with tempfile.TemporaryDirectory() as tmp:
            att = Path(tmp) / "a.txt"
            att.write_text("x", encoding="utf-8")
            drv = self._driver(
                [_composer_snap([ATTACH_BUTTON])],
                tmpdir=tmp,
                config=dict(get_endpoint_config("gemini")),
                dialog_fn=lambda _p: "stub:dialog",
                write_fn=lambda *_a: "stub:write",
            )
            with mock.patch.object(drv, "attach_files", side_effect=_theft):
                res = drv.ask("只回复：收到", attach=[str(att)], timeout=5, force=True)
            entry = drv.ledger.get("只回复：收到", "gemini")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "focus_taken")
        self.assertEqual(res["reply"], "")
        self.assertEqual(entry["state"], "failed_retryable")

    def test_never_visible_fails_structurally_without_clicking(self):
        always_missing = [_composer_snap(snap_id="s%d" % i) for i in range(4)]
        scroll_calls: list = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(always_missing, scroll_calls=scroll_calls, tmpdir=tmp)
            snap, payload = drv._ensure_composer_ready(
                WindowInfo(hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini"),
                always_missing[0],
                warnings=warnings,
                attempts=3,
            )

        self.assertIsNotNone(payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "composer_not_ready")
        self.assertEqual(payload["missing"], "attach_button")
        self.assertEqual(payload["attempts"], 3)
        self.assertEqual(payload["reply"], "")
        self.assertEqual(len(scroll_calls), 2, "attempts-1 scrolls, no blind clicking")
        self.assertEqual(warnings, [])
        self.assertIsNotNone(snap)


if __name__ == "__main__":
    unittest.main()
