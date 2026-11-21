"""Offline coverage for M4-8: brain-gated ALL_DONE, GUI timeout, git encoding.

The scheduler may no longer declare ``ALL_DONE`` on its own: when the roadmap
owes nothing it must report to the brain and wait for the sign-off token. These
tests pin that gate (marker / next task / silence), keep the legacy switch
working, and pin the two smaller fixes (GUI ``--timeout`` default, UTF-8 git log
output).

No process, window or network is touched: the brain call is a mocked function,
``git`` is a stub, and ``tkinter`` is never imported (the GUI module is loaded
with its documented lazy-import behaviour).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402

ROADMAP_EMPTY = "# roadmap\n- [x] M4-7 已完成\n- [x] M4-8 已完成\n"
ROADMAP_SIGNED = ROADMAP_EMPTY + "\nALL_DONE\n"
CARD_REPLY = (
    "<<<WORKBUDDY_TASK_START>>>\n"
    "step: tidy one helper and run the full unittest suite\n"
    "<<<WORKBUDDY_TASK_END>>>"
)

BASE = "abcdef0123456789"


def _load_gui(name: str = "bridge_gui_m48"):
    """Load ``bridge_gui.pyw`` without importing tkinter or creating a window."""
    path = ROOT / "bridge_gui.pyw"
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class SignoffMarkerTests(unittest.TestCase):
    def test_marker_is_recognised_only_as_a_literal(self):
        self.assertTrue(
            ahl.brain_signed_off_all_done(ahl.ALL_DONE_SIGN_OFF_MARKER)
        )
        self.assertTrue(
            ahl.brain_signed_off_all_done(
                "验收通过。\n%s\n" % ahl.ALL_DONE_SIGN_OFF_MARKER
            )
        )
        # Words, a half marker or an empty reply must never sign the run off.
        for reply in (
            "",
            "ALL_DONE",
            "BRIDGE_ALL_DONE",
            "<<BRIDGE_ALL_DONE>>",
            "可以收口，请记 ALL_DONE",
            None,
        ):
            self.assertFalse(ahl.brain_signed_off_all_done(reply), reply)

    def test_signoff_prompt_never_contains_the_marker(self):
        roadmap = "- [x] M4-7\n- [ ] M4-8\n"
        # The real roadmap documents the token itself, so the excerpt must be
        # redacted: otherwise a brain echoing the prompt would sign itself off.
        roadmap_with_token = (
            roadmap + "\n说明：主脑回 %s 才算签收。\n" % ahl.ALL_DONE_SIGN_OFF_MARKER
        )
        with mock.patch.object(ahl, "git_text", return_value="90d43c5 docs"):
            prompt = ahl.build_signoff_prompt(3, BASE, "rc=0\nOK", roadmap)
            prompt_with_token = ahl.build_signoff_prompt(
                3, BASE, "rc=0\nOK", roadmap_with_token
            )
        self.assertNotIn(ahl.ALL_DONE_SIGN_OFF_MARKER, prompt)
        self.assertNotIn(ahl.ALL_DONE_SIGN_OFF_MARKER, prompt_with_token)
        self.assertNotIn("<<<", prompt_with_token)
        self.assertNotIn(">>>", prompt_with_token)
        # The brain is told the token in words, and the counts are reported.
        self.assertIn("BRIDGE_ALL_DONE", prompt)
        self.assertIn("三个小于号", prompt)
        self.assertIn("未勾选 1 项 / 共 2 项", prompt)
        self.assertIn(BASE, prompt)
        # No task/patch example block, so an echo cannot become a task card.
        self.assertIsNone(ahl.TASK_RE.search(prompt))
        self.assertIsNone(ahl.PATCH_RE.search(prompt))
        self.assertFalse(ahl.brain_signed_off_all_done(prompt))
        self.assertFalse(ahl.brain_signed_off_all_done(prompt_with_token))


class OvernightSignoffTests(unittest.TestCase):
    """``run_overnight`` must ask the brain before it ever records ALL_DONE."""

    def _fake_git(self, calls: list[tuple[str, ...]], *, dirty: bool = False):
        def fake(*args):
            calls.append(args)
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(returncode=0, stdout=BASE + "\n", stderr="")
            if args == ("status", "--porcelain"):
                return SimpleNamespace(
                    returncode=0, stdout=" M x.py" if dirty else "", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return fake

    def _run(self, roadmap_text: str, *, reply: str, **kwargs):
        """Drive one overnight round against a throwaway root; return evidence."""
        calls: list[tuple[str, ...]] = []
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name)
        runs = root / "runs"
        runs.mkdir()
        (root / "docs").mkdir()
        (root / "docs" / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
        with mock.patch.object(ahl, "ROOT", root), \
                mock.patch.object(ahl, "RUNS", runs), \
                mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                mock.patch.object(ahl, "git", side_effect=self._fake_git(calls)), \
                mock.patch.object(ahl, "log"), \
                mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                mock.patch.object(
                    ahl, "request_signoff_from_brain", return_value=reply
                ) as signoff, \
                mock.patch.object(
                    ahl, "request_task_from_gemini", return_value=CARD_REPLY
                ) as ask, \
                mock.patch.object(
                    ahl, "execute_brain_reply",
                    return_value={
                        "mode": "task", "applied": False,
                        "task": "step: tidy one helper", "executor_reply": "done",
                    },
                ) as dispatch, \
                mock.patch.object(ahl.time, "sleep"):
            rc = ahl.run_overnight(
                max_rounds=kwargs.pop("max_rounds", 1),
                no_push=True, sleep=0, **kwargs
            )
        state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
        return rc, state, signoff, ask, dispatch

    def test_brain_marker_exits_all_done(self):
        rc, state, signoff, ask, dispatch = self._run(
            ROADMAP_EMPTY, reply=ahl.ALL_DONE_SIGN_OFF_MARKER
        )
        self.assertEqual(rc, 0)
        signoff.assert_called_once()
        ask.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(state["state"], "ALL_DONE")
        self.assertIn("brain signed off", state["reason"])

    def test_brain_task_continues_instead_of_exiting(self):
        rc, state, signoff, ask, dispatch = self._run(
            ROADMAP_EMPTY, reply=CARD_REPLY, max_rounds=1
        )
        self.assertEqual(rc, 0)
        signoff.assert_called_once()
        # The sign-off reply already carries the next step: no second brain call.
        ask.assert_not_called()
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0], CARD_REPLY)
        history = [item["state"] for item in state["history"]]
        self.assertNotIn("ALL_DONE", history)
        self.assertIn("DISPATCH_EXECUTOR", history)

    def test_brain_silence_awaits_signoff_and_counts_idle(self):
        rc, state, signoff, ask, dispatch = self._run(
            ROADMAP_EMPTY, reply="", max_rounds=1
        )
        self.assertEqual(rc, 0)
        signoff.assert_called_once()
        ask.assert_not_called()
        dispatch.assert_not_called()
        history = [item["state"] for item in state["history"]]
        self.assertIn("AWAITING_BRAIN_SIGNOFF", history)
        self.assertNotIn("ALL_DONE", history)

    def test_legacy_explicit_all_done_line_skips_the_brain_when_disabled(self):
        rc, state, signoff, ask, dispatch = self._run(
            ROADMAP_SIGNED, reply="", require_brain_signoff=False
        )
        self.assertEqual(rc, 0)
        signoff.assert_not_called()
        ask.assert_not_called()
        self.assertEqual(state["state"], "ALL_DONE")
        self.assertIn("roadmap owes nothing", state["reason"])


class GuiDefaultTimeoutTests(unittest.TestCase):
    def test_default_daemon_timeout_is_600(self):
        gui = _load_gui()
        self.assertEqual(gui.DEFAULT_DAEMON_TIMEOUT, 600)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = gui.build_daemon_cmd(tmp, python_exe="py.exe")
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "600")
        # An explicit value still wins (operator override stays possible).
        with tempfile.TemporaryDirectory() as tmp:
            explicit = gui.build_daemon_cmd(tmp, python_exe="py.exe", timeout=90)
        self.assertEqual(explicit[explicit.index("--timeout") + 1], "90")
        self.assertIsNone(gui.tk, "loading the GUI must not import tkinter")


class GitEncodingTests(unittest.TestCase):
    def test_git_forces_utf8_log_output_and_unquoted_paths(self):
        with mock.patch.object(
            ahl.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as runner:
            ahl.git("log", "-n", "1", "--oneline")
        argv = list(runner.call_args.args[0])
        self.assertEqual(
            argv[:5],
            [
                "git",
                "-c", "i18n.logOutputEncoding=utf-8",
                "-c", "core.quotepath=false",
            ],
        )
        self.assertEqual(argv[5:], ["log", "-n", "1", "--oneline"])
        self.assertEqual(runner.call_args.kwargs["encoding"], "utf-8")


if __name__ == "__main__":
    unittest.main()
