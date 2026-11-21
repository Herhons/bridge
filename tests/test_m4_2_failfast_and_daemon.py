"""Offline coverage for the daemon / fail-fast half of M4-2.

Three groups, all headless and deterministic:

* ``scripts/auto_heal_loop.py`` starts every subprocess with
  ``CREATE_NO_WINDOW`` on Windows, refuses to fall back once a level has already
  written code, and skips a cooling opencode lane;
* ``engine/base.py`` aborts a wait the moment a paywall / permission popup
  appears, instead of burning the full 300s budget, and leaves the ledger
  retryable so a human can clear the dialog and resend;
* the Gemini ``council`` profile no longer resolves to the paywalled ``carbon``.

No window is opened or clicked: ``window_fn`` / ``snapshot_fn`` are injected and
the lock, ledger and evidence paths all point at a temp dir.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402
from engine.base import DEFAULT_BLOCKING_POPUP_PATTERNS, EndpointDriver  # noqa: E402
from engine.config import get_endpoint_config, resolve_profile  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402

BRIDGE_CFG = {
    "poll_interval": 0.0,
    "stable_seconds": 0.0,
    "default_timeout": 5.0,
    "lock_timeout": 5.0,
    "headless_default": True,
}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _snap(nodes: list[UINode], snap_id: str = "s1") -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=101,
        title="probe",
        url="",
        nodes=nodes,
        refs=assign_refs(nodes, snap_id),
        window_rect=(0, 0, 1200, 900),
    )


class WindowsNoWindowTests(unittest.TestCase):
    """Every subprocess the loop starts must be console-less on Windows."""

    def test_run_cmd_passes_create_no_window(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.returncode = 0
        proc.communicate.return_value = ("out", None)
        with mock.patch.object(ahl, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(
                    ahl.subprocess, "Popen", return_value=proc
                ) as popen:
            rc, out = ahl.run_cmd(["bridge.py", "ask"], timeout=1)

        kwargs = popen.call_args.kwargs
        self.assertEqual(rc, 0)
        self.assertEqual(out, "out")
        self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertIs(kwargs["stdin"], ahl.subprocess.DEVNULL)

    def test_git_passes_create_no_window(self):
        done = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(ahl, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(ahl.subprocess, "run", return_value=done) as runner:
            ahl.git("status", "--porcelain")

        self.assertEqual(runner.call_args.kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertIs(runner.call_args.kwargs["stdin"], ahl.subprocess.DEVNULL)

    def test_kill_process_tree_passes_create_no_window(self):
        done = SimpleNamespace(returncode=0)
        with mock.patch.object(ahl, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(ahl.subprocess, "run", return_value=done) as runner:
            rc = ahl.kill_process_tree(4242)

        self.assertEqual(rc, 0)
        self.assertEqual(runner.call_args.kwargs["creationflags"], CREATE_NO_WINDOW)

    def test_posix_keeps_a_neutral_flag(self):
        with mock.patch.object(ahl, "os", SimpleNamespace(name="posix")):
            self.assertEqual(ahl._no_window_flags(), 0)


class PreFallbackDirtyCheckTests(unittest.TestCase):
    """A level that wrote files must never be followed by another executor."""

    def test_dirty_worktree_stops_the_chain(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            calls.append(cmd[3])
            return 0, ""  # workbuddy answered nothing

        seq = ["", " M engine/base.py"]

        def fake_fingerprint():
            return seq.pop(0) if seq else " M engine/base.py"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(
                        ahl, "worktree_fingerprint", side_effect=fake_fingerprint
                    ), \
                    mock.patch.object(ahl, "log") as logger:
                reply = ahl.call_executor("step: patch code", 7, 60, "auto")

        self.assertEqual(calls, ["workbuddy"], "no second executor may run")
        self.assertIn("worktree changed by executor workbuddy", reply)
        log_text = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("executor_dirty=workbuddy", log_text)

    def test_clean_worktree_still_falls_back(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            if endpoint == "workbuddy":
                return 0, ""
            return 0, json.dumps({"ok": True, "reply": "opencode done"})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "worktree_fingerprint", return_value=""), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step: patch code", 11, 60, "auto")

        self.assertEqual(reply, "opencode done")
        self.assertEqual(calls, ["workbuddy", "opencode"])

    def test_preexisting_dirt_is_not_attributed_to_the_executor(self):
        """A worktree that was already dirty must not block the fallback."""
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            if endpoint == "workbuddy":
                return 0, ""
            return 0, json.dumps({"ok": True, "reply": "opencode done"})

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(
                        ahl, "worktree_fingerprint",
                        return_value=" M engine/ledger.py",
                    ), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step: patch code", 12, 60, "auto")

        self.assertEqual(reply, "opencode done")
        self.assertEqual(calls, ["workbuddy", "opencode"])


class OpenCodeCooldownSkipTests(unittest.TestCase):
    def _write_cooldown(self, runs: Path, *, minutes: float) -> str:
        until = datetime.now() + timedelta(minutes=minutes)
        (runs / ahl.COOLDOWN_FILE).write_text(
            json.dumps({
                "endpoint": "opencode",
                "hint": "rate limit",
                "until": until.isoformat(timespec="seconds"),
            }),
            encoding="utf-8",
        )
        return until.isoformat(timespec="seconds")

    def test_auto_chain_skips_a_cooling_opencode(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            if endpoint == "workbuddy":
                return 0, ""
            return 0, json.dumps({"ok": True, "reply": "antigravity ok"})

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            until = self._write_cooldown(runs, minutes=5)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "worktree_fingerprint", return_value=""), \
                    mock.patch.object(ahl, "log") as logger:
                reply = ahl.call_executor("step: patch code", 8, 60, "auto")

        self.assertEqual(calls, ["workbuddy", "antigravity"])
        self.assertEqual(reply, "antigravity ok")
        log_text = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("executor_skip=opencode reason=cooldown_active", log_text)
        self.assertIn(until, log_text)

    def test_expired_cooldown_does_not_skip(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            endpoint = cmd[3]
            calls.append(endpoint)
            if endpoint == "workbuddy":
                return 0, ""
            return 0, json.dumps({"ok": True, "reply": "opencode done"})

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self._write_cooldown(runs, minutes=-5)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "worktree_fingerprint", return_value=""), \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor("step: patch code", 9, 60, "auto")

        self.assertEqual(reply, "opencode done")
        self.assertEqual(calls, ["workbuddy", "opencode"])

    def test_unreadable_or_missing_marker_reads_as_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs):
                self.assertEqual(ahl.opencode_cooldown_until(), "")
                (runs / ahl.COOLDOWN_FILE).write_text("not json", encoding="utf-8")
                self.assertEqual(ahl.opencode_cooldown_until(), "")
                (runs / ahl.COOLDOWN_FILE).write_text(
                    json.dumps({"until": "not-a-timestamp"}), encoding="utf-8"
                )
                self.assertEqual(ahl.opencode_cooldown_until(), "")


class BlockingPopupFailFastTests(unittest.TestCase):
    """A popup means "waiting is pointless": return at once, stay retryable."""

    def _driver(self, tmp: str, name: str, nodes: list[UINode], **cfg_extra):
        cfg = dict(get_endpoint_config(name))
        cfg.update(cfg_extra)
        return EndpointDriver(
            name,
            config=cfg,
            bridge_config=dict(BRIDGE_CFG),
            window_fn=lambda: WindowInfo(
                hwnd=101, pid=4242, process_name="probe.exe", title=name
            ),
            snapshot_fn=lambda hwnd: _snap(nodes),
            ledger_path=Path(tmp) / "ledger.json",
            evidence_dir=Path(tmp) / "fail",
            lock_path=Path(tmp) / "ui.lock",
            sleep_fn=lambda _seconds: None,
        )

    def _assert_blocked(self, driver, prompt: str, expected: str) -> dict:
        res = driver.ask(prompt, timeout=300, force=True)
        entry = driver.ledger.get(prompt, driver.name)
        allowed, _ = driver.ledger.check(prompt, driver.name)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "blocking_popup")
        self.assertEqual(res["matched"], expected)
        self.assertEqual(res["reply"], "")
        self.assertLess(res["elapsed"], 5.0, "must not wait out the 300s budget")
        self.assertIn("Blocking popup detected", res["message"])
        self.assertEqual(entry["state"], "failed_retryable")
        self.assertEqual(entry["note"], "blocking_popup")
        self.assertTrue(allowed, "a cleared popup must allow the same prompt again")
        return res

    def test_gemini_paywall_aborts_the_wait(self):
        nodes = [
            UINode(role="TextControl", name="解锁 4 Carbon 的全部能力", rect=(10, 10, 300, 24)),
            UINode(role="TextControl", name="仅需 US$99.99/月", rect=(10, 40, 300, 24)),
            UINode(role="EditControl", name="问问 Gemini", rect=(10, 700, 500, 40)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            driver = self._driver(tmp, "gemini", nodes)
            self._assert_blocked(driver, "帮我审一下这段代码", "解锁 4 carbon")

    def test_antigravity_request_review_aborts_the_wait(self):
        # Only the dialog title is present, so this proves "Request Review"
        # alone is enough to trip the detector (not just the body wording).
        nodes = [
            UINode(role="TextControl", name="Request Review", rect=(10, 10, 200, 24)),
            UINode(role="ButtonControl", name="Continue", rect=(10, 70, 160, 30)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            driver = self._driver(tmp, "antigravity", nodes)
            self._assert_blocked(driver, "修一下这个函数", "request review")

    def test_normal_snapshot_is_not_flagged(self):
        nodes = [
            UINode(role="TextControl", name="这是正常回复，Allow 一下再继续", rect=(10, 10, 300, 24)),
            UINode(role="EditControl", name="问问 Gemini", rect=(10, 700, 500, 40)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            driver = self._driver(tmp, "gemini", nodes)
            self.assertEqual(driver.detect_blocking_popup(_snap(nodes)), "")

    def test_endpoint_specific_patterns_extend_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = self._driver(
                tmp, "gemini", [],
                blocking_popup_patterns=["自定义付费墙"],
            )
            patterns = driver.blocking_popup_patterns()
            self.assertIn("自定义付费墙", patterns, "config entries are honoured")
            self.assertIn(
                "us$99.99", patterns, "defaults are extended, never replaced"
            )
            hit = driver.detect_blocking_popup(
                _snap([UINode(role="TextControl", name="自定义付费墙 提示", rect=(0, 0, 10, 10))])
            )
            self.assertEqual(hit, "自定义付费墙")

    def test_defaults_cover_the_live_paywall_and_permission_wording(self):
        for needle in ("解锁 4 carbon", "us$99.99", "waiting for user input", "request review"):
            self.assertIn(needle, DEFAULT_BLOCKING_POPUP_PATTERNS)


class GeminiCouncilPolicyTests(unittest.TestCase):
    def test_council_no_longer_targets_the_paywalled_carbon(self):
        model, free_only = resolve_profile("gemini", "council")
        self.assertEqual(model, "flash")
        self.assertNotEqual(model, "carbon")
        self.assertFalse(free_only)

    def test_both_models_stay_configured_for_manual_use(self):
        cfg = get_endpoint_config("gemini")
        self.assertIn("flash", cfg["models"])
        self.assertIn("carbon", cfg["models"], "carbon stays reachable by --model")


if __name__ == "__main__":
    unittest.main()
