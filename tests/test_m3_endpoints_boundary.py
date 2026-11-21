"""M3 offline boundary coverage for chatgpt / codex / dsh / opencode.

Everything runs without a GUI: ``window_fn`` / ``snapshot_fn`` / ``invoke_fn`` /
``write_fn`` / ``sleep_fn`` are injected, the UI lock, the submission ledger and
the failure-evidence directory all point at a temp dir. No real window is
opened, no clipboard is touched, and no production ledger or OpenCode DB is
read or written.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
import endpoints.opencode as opencode_mod  # noqa: E402
from endpoints import get_driver  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402

UIA_ENDPOINTS = ("chatgpt", "codex", "dsh")
PROMPT = "m3 边界探测"
BRIDGE_CFG = {
    "poll_interval": 0.0,
    "stable_seconds": 0.0,
    "default_timeout": 5.0,
    "lock_timeout": 5.0,
    "headless_default": True,
}
WINDOW_RECT = (0, 0, 1000, 800)


def _window(title: str = "Boundary") -> WindowInfo:
    return WindowInfo(hwnd=88, pid=55, process_name="fake.exe", title=title)


def _snap(
    snap_id: str = "s1",
    *,
    composer: bool = True,
    send: bool = True,
    stop: bool = False,
    text_lines: tuple[str, ...] = (),
) -> Snapshot:
    kids = [
        UINode(role="ButtonControl", name="新对话", rect=(10, 20, 80, 24)),
    ]
    if composer:
        kids.append(UINode(role="EditControl", name="询问 ChatGPT", rect=(100, 600, 700, 60)))
    if send:
        kids.append(UINode(role="ButtonControl", name="发送", rect=(820, 620, 40, 40)))
    if stop:
        kids.append(UINode(role="ButtonControl", name="停止", rect=(820, 620, 40, 40)))
    for line in text_lines:
        kids.append(UINode(role="TextControl", name=line, rect=(100, 100, 300, 20)))
    root = UINode(role="WindowControl", name="W", children=kids)
    return Snapshot(
        snapshot_id=snap_id,
        hwnd=88,
        title="W",
        url="",
        nodes=[root],
        refs=assign_refs([root], snap_id),
        window_rect=WINDOW_RECT,
    )


def _driver(endpoint: str, tmp: str, *, snap: Snapshot | None = None, **overrides):
    """Build an endpoint driver whose every side effect lands in *tmp*."""
    snapshot = snap if snap is not None else _snap()
    kwargs = {
        "bridge_config": dict(BRIDGE_CFG),
        "snapshot_fn": lambda _hwnd: snapshot,
        "window_fn": lambda: _window(),
        "invoke_fn": lambda node, headed: "stub:click",
        "write_fn": lambda node, text, headed, chunk: "stub:write",
        "sleep_fn": lambda _seconds: None,
        "lock_path": Path(tmp) / "ui.lock",
        "ledger_path": Path(tmp) / "ledger.json",
        "evidence_dir": Path(tmp) / "fail",
    }
    kwargs.update(overrides)
    return get_driver(endpoint, **kwargs)


class StatusBoundaryTests(unittest.TestCase):
    def test_ready_status_when_composer_is_visible(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                drv = _driver(endpoint, tmp)
                res = drv.status()

                self.assertTrue(res["ok"])
                self.assertTrue(res["window_found"])
                self.assertTrue(res["ready"])
                self.assertEqual(res["endpoint"], endpoint)
                self.assertEqual(res["hwnd"], 88)
                self.assertEqual(res["title"], "Boundary")
                self.assertEqual(res["process"], "fake.exe")
                self.assertTrue(res["models"])
                self.assertEqual(res["snapshot_degraded"], False)

    def test_window_not_found_is_reported_not_raised(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                drv = _driver(endpoint, tmp, window_fn=lambda: None)
                res = drv.status()

                self.assertFalse(res["ok"])
                self.assertFalse(res["window_found"])
                self.assertFalse(res["ready"])
                self.assertIsNone(res["hwnd"])
                self.assertIsNone(res["title"])
                self.assertIsNone(res["process"])
                self.assertEqual(res["endpoint"], endpoint)

    def test_status_without_selectors_is_not_ready_but_never_raises(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                drv = _driver(
                    endpoint, tmp,
                    config={"kind": "web"},
                    snap=_snap(composer=False, send=False),
                )
                res = drv.status()

                self.assertTrue(res["ok"])          # window is there
                self.assertFalse(res["ready"])      # but nothing to type into
                self.assertEqual(res["endpoint"], endpoint)


class ConfigBoundaryTests(unittest.TestCase):
    def test_ask_without_composer_returns_structured_error(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                drv = _driver(
                    endpoint, tmp,
                    config={"kind": "web", "selectors": {}},
                    snap=_snap(composer=False, send=False),
                )
                res = drv.ask(PROMPT, force=True, timeout=1)

                self.assertFalse(res["ok"])
                self.assertEqual(res["error"], "composer_not_found")
                self.assertEqual(res["endpoint"], endpoint)
                self.assertEqual(res["reply"], "")
                entry = drv.ledger.get(PROMPT, endpoint)
                self.assertEqual(entry["state"], "failed_retryable")
                self.assertNotEqual(entry["state"], "accepted")
                self.assertTrue(res["evidence"])
                self.assertTrue(Path(res["evidence"]).is_file())

    def test_missing_selectors_key_is_tolerated_by_ask(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = _driver("chatgpt", tmp, config={"kind": "web"})
            res = drv.ask(PROMPT, force=True, timeout=1)
        self.assertIn(res.get("error"), ("composer_not_found", "timeout"))
        self.assertFalse(res["ok"])


class TimeoutBoundaryTests(unittest.TestCase):
    def test_ask_times_out_without_marking_ledger_accepted(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                # A frozen snapshot: the composer is there, the reply never moves.
                drv = _driver(endpoint, tmp, snap=_snap(stop=True))
                res = drv.ask(PROMPT, force=True, timeout=0.05)

                self.assertFalse(res["ok"])
                self.assertEqual(res["error"], "timeout")
                self.assertEqual(res["reason"], "timeout")
                self.assertEqual(res["reply"], "")
                entry = drv.ledger.get(PROMPT, endpoint)
                self.assertIsNotNone(entry)
                self.assertEqual(entry["state"], "unknown")
                self.assertNotEqual(entry["state"], "accepted")

    def test_ask_reports_closed_window_instead_of_raising(self):
        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                drv = _driver(endpoint, tmp, window_fn=lambda: None)
                res = drv.ask(PROMPT, force=True, timeout=1)

                self.assertFalse(res["ok"])
                self.assertEqual(res["error"], "window_not_found")
                self.assertEqual(res["reply"], "")


class AttachBoundaryTests(unittest.TestCase):
    def test_attach_without_button_fails_fast_before_touching_any_window(self):
        looked_up: list[int] = []

        for endpoint in UIA_ENDPOINTS:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                payload = Path(tmp) / "state.json"
                payload.write_text("{}", encoding="utf-8")
                drv = _driver(
                    endpoint, tmp,
                    window_fn=lambda: (looked_up.append(1) or _window()),
                )
                with self.assertRaisesRegex(NotImplementedError, "no attach_button"):
                    drv.ask(PROMPT, attach=[str(payload)], force=True, timeout=1)

        self.assertEqual(looked_up, [])


class OpenCodeBoundaryTests(unittest.TestCase):
    """opencode is CLI-shaped: no window, no snapshot, no attach."""

    def _driver(self, tmp: str, **kwargs):
        kwargs.setdefault("bridge_config", dict(BRIDGE_CFG))
        kwargs.setdefault("ledger_path", Path(tmp) / "ledger.json")
        kwargs.setdefault("evidence_dir", Path(tmp) / "fail")
        kwargs.setdefault("sleep_fn", lambda _seconds: None)
        kwargs.setdefault("db_path", Path(tmp) / "opencode.db")
        return opencode_mod.OpenCodeDriver(**kwargs)

    def test_status_ready_when_cli_is_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(
                opencode_mod.shutil, "which", return_value="C:/fake/opencode.exe"
            ):
                res = drv.status()

        self.assertTrue(res["ok"])
        self.assertTrue(res["ready"])
        self.assertEqual(res["endpoint"], "opencode")
        self.assertEqual(res["kind"], "cli")
        self.assertEqual(res["cli_path"], "C:/fake/opencode.exe")
        self.assertFalse(res["db_exists"])
        # CLI endpoint: the field exists for schema parity, always False.
        self.assertIn("window_found", res)
        self.assertFalse(res["window_found"])

    def test_status_not_ready_without_cli_or_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(opencode_mod.shutil, "which", return_value=None):
                res = drv.status()

        self.assertFalse(res["ok"])
        self.assertFalse(res["ready"])
        self.assertIsNone(res["cli_path"])
        self.assertFalse(res["db_exists"])

    def test_cli_timeout_is_structured_and_not_accepted(self):
        def boom(cmd, budget):
            raise subprocess.TimeoutExpired(cmd, budget)

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp, cli_runner=boom)
            with mock.patch.object(opencode_mod.opencode_db, "current_max_seq", return_value=0):
                res = drv.ask(PROMPT, timeout=0.05, force=True)
            entry = drv.ledger.get(PROMPT, "opencode")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "timeout")
        self.assertEqual(res["reply"], "")
        self.assertEqual(entry["state"], "unknown")
        self.assertNotEqual(entry["state"], "accepted")

    def test_missing_cli_returns_cli_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(opencode_mod.shutil, "which", return_value=None):
                res = drv.ask(PROMPT, force=True)

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cli_not_found")
        self.assertEqual(res["reply"], "")

    def test_dispatch_contract_now_reaches_opencode(self):
        """The v0.2.5 defect is fixed: dispatch() must not blow up on opencode.

        ``bridge.dispatch()`` always passes ``profile`` / ``free_only`` /
        ``attach``; the driver accepts all three now, so the opencode executor
        level in the overnight loop is reachable again.
        """
        seen_cmd: list[str] = []

        def fake_runner(cmd, budget):
            seen_cmd.extend(cmd)
            return 0, "opencode ok\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp, cli_runner=fake_runner)
            opts = {
                "prompt": PROMPT,
                "timeout": 5,
                "new_chat": False,
                "headed": None,
                "model": None,
                "profile": "test",
                "free_only": True,
                "confirm": False,
                "force": True,
                "attach": None,
            }
            with mock.patch.object(opencode_mod.opencode_db, "current_max_seq", return_value=0):
                res = bridge.dispatch(
                    "ask", "opencode", opts, driver_factory=lambda _name: drv
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "opencode ok")
        self.assertEqual(res["profile"], "test")
        self.assertIn("--prompt", seen_cmd)
        self.assertIn(PROMPT, seen_cmd)

    def test_attach_returns_structured_error_instead_of_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "state.json"
            payload.write_text("{}", encoding="utf-8")
            drv = self._driver(tmp, cli_runner=lambda cmd, budget: (0, "x", ""))
            res = drv.ask(PROMPT, attach=[str(payload)], force=True)

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "attach_unsupported")
        self.assertEqual(res["attachments"], [str(payload)])
        self.assertEqual(res["reply"], "")

    def test_cli_maps_notimplementederror_to_json(self):
        class _Raising:
            def ask(self, *_args, **_kwargs):
                raise NotImplementedError(
                    "endpoint chatgpt has no attach_button selector; --attach is not supported"
                )

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "state.json"
            payload.write_text("{}", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bridge.main(
                    ["ask", "chatgpt", "hi", "--attach", str(payload), "--json"],
                    driver_factory=lambda _name: _Raising(),
                )
            out = buf.getvalue()

        self.assertEqual(rc, 1)
        parsed = json.loads(out)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"], "attach_unsupported")
        self.assertEqual(parsed["endpoint"], "chatgpt")
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
