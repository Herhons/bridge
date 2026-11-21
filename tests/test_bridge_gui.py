"""Offline coverage for ``bridge_gui.pyw`` (M4-GUI desktop console).

Loaded through ``SourceFileLoader`` because ``.pyw`` is not importable by name.
Two hard rules are pinned here:

* **No real Tk window.** ``tkinter`` is lazily imported inside the module, so
  ``gui.tk`` is still ``None`` after import — asserted explicitly. Nothing in
  this file ever calls ``launch_gui`` or builds a widget.
* **No real process.** ``subprocess.run`` / ``subprocess.Popen`` are always
  mocked; the assertions cover the argv, the ``CREATE_NO_WINDOW`` /
  ``DETACHED_PROCESS`` flags and the log redirection, so a future edit cannot
  silently start popping console windows or spawning a scheduler during tests.

Geometry assertions pin ``x < 0`` and ``x + width <= 0``: the panel must never
reach into the main screen (``0..2560``).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / "bridge_gui.pyw"


def _load_gui():
    """Load ``bridge_gui.pyw`` under a private module name without running it."""
    loader = importlib.machinery.SourceFileLoader("bridge_gui_under_test", str(GUI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


gui = _load_gui()


class ModuleHygieneTests(unittest.TestCase):
    def test_import_does_not_load_tkinter(self):
        """Importing the console module must not import tkinter / open a window."""
        self.assertIsNone(gui.tk)
        self.assertIsNone(gui.ttk)

    def test_main_guard_present(self):
        """``launch_gui`` is only reachable through the ``__main__`` guard."""
        self.assertNotEqual(gui.__name__, "__main__")
        self.assertTrue(callable(gui.launch_gui))


class RoadmapParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_only_real_checkbox_lines(self):
        path = self.tmp / "ROADMAP.md"
        path.write_text(
            "# 路线图\n"
            "\n"
            "- [x] **M1 / M2 / M3**：基础框架\n"
            "- [ ] **M4-4（P0）**：Gemini 附件真机闭环\n"
            "  - 子说明没有复选框，不应计入\n"
            "- [X] 大写勾选同样算完成\n",
            encoding="utf-8",
        )
        doc = gui.parse_roadmap(path)
        self.assertFalse(doc["missing"])
        self.assertEqual(doc["total"], 3)
        self.assertEqual(doc["done"], 2)
        self.assertEqual(doc["pending"], 1)
        self.assertFalse(doc["all_done"])
        self.assertEqual([item["done"] for item in doc["items"]], [True, False, True])
        self.assertTrue(doc["items"][0]["text"].startswith("**M1 / M2 / M3**"))
        self.assertEqual(doc["items"][1]["line"], 4)

    def test_all_done_marker_must_be_a_standalone_line(self):
        path = self.tmp / "ROADMAP.md"
        path.write_text(
            "- [x] 收口项\n本文件末尾另起一行写 ALL_DONE，调度器即退出\n",
            encoding="utf-8",
        )
        self.assertFalse(gui.parse_roadmap(path)["all_done"])

        path.write_text("- [x] 收口项\n\nALL_DONE\n", encoding="utf-8")
        doc = gui.parse_roadmap(path)
        self.assertTrue(doc["all_done"])
        self.assertEqual(doc["pending"], 0)

    def test_missing_file_is_empty_not_fatal(self):
        doc = gui.parse_roadmap(self.tmp / "nope.md")
        self.assertTrue(doc["missing"])
        self.assertEqual((doc["total"], doc["done"], doc["pending"]), (0, 0, 0))
        self.assertEqual(doc["items"], [])
        self.assertFalse(doc["all_done"])


class LogAndCooldownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_log_tail_keeps_last_lines(self):
        path = self.tmp / "overnight.log"
        path.write_text(
            "".join("line%d\n" % i for i in range(1, 101)), encoding="utf-8"
        )
        tail = gui.read_log_tail(path, 40)
        self.assertEqual(len(tail), 40)
        self.assertEqual(tail[0], "line61")
        self.assertEqual(tail[-1], "line100")
        self.assertEqual(gui.read_log_tail(path, 3), ["line98", "line99", "line100"])
        self.assertEqual(gui.read_log_tail(path, 0), [])

    def test_read_log_tail_missing_file_is_empty(self):
        self.assertEqual(gui.read_log_tail(self.tmp / "absent.log", 40), [])

    def test_cooldown_active_then_expired(self):
        path = self.tmp / "opencode_cooldown.json"
        now = datetime(2026, 11, 21, 12, 0, 0)
        path.write_text(
            json.dumps(
                {
                    "endpoint": "opencode",
                    "hint": "429",
                    "until": (now + timedelta(seconds=125)).isoformat(timespec="seconds"),
                    "written_at": now.isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )
        active = gui.read_opencode_cooldown(path, now=now)
        self.assertFalse(active["available"])
        self.assertEqual(active["status"], "cooldown")
        self.assertAlmostEqual(active["remaining_s"], 125.0, places=3)
        self.assertEqual(active["label"], "冷却 02:05")
        self.assertEqual(active["hint"], "429")

        expired = gui.read_opencode_cooldown(path, now=now + timedelta(minutes=10))
        self.assertTrue(expired["available"])
        self.assertEqual(expired["status"], "available")
        self.assertEqual(expired["remaining_s"], 0.0)
        self.assertEqual(expired["label"], gui.OPENCODE_READY_LABEL)

    def test_cooldown_missing_or_malformed_is_available(self):
        self.assertTrue(gui.read_opencode_cooldown(self.tmp / "none.json")["available"])
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertTrue(gui.read_opencode_cooldown(bad)["available"])
        nountil = self.tmp / "no_until.json"
        nountil.write_text(json.dumps({"endpoint": "opencode"}), encoding="utf-8")
        self.assertTrue(gui.read_opencode_cooldown(nountil)["available"])


class DaemonCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_daemon_cmd_always_passes_profile(self):
        cmd = gui.build_daemon_cmd(
            self.tmp,
            python_exe="py.exe",
            brain="auto",
            executor="auto",
            executor_profile="none",
            stop_on_bug=True,
            timeout=300,
            sleep=60,
        )
        self.assertEqual(cmd[0], "py.exe")
        self.assertEqual(cmd[1], str(self.tmp / "scripts" / "auto_heal_loop.py"))
        self.assertIn("--daemon", cmd)
        # ``none`` must be explicit: the scheduler's own default is ``test``.
        profile_at = cmd.index("--executor-profile")
        self.assertEqual(cmd[profile_at + 1], "none")
        self.assertEqual(cmd[cmd.index("--brain") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--executor") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "300")
        self.assertEqual(cmd[cmd.index("--sleep") + 1], "60")
        self.assertIn("--stop-on-bug", cmd)

    def test_build_daemon_cmd_optional_flag_and_fractions(self):
        cmd = gui.build_daemon_cmd(
            self.tmp,
            python_exe="py.exe",
            brain="gemini",
            executor="opencode",
            executor_profile="test",
            stop_on_bug=False,
            timeout=12.5,
            sleep=5,
        )
        self.assertNotIn("--stop-on-bug", cmd)
        self.assertEqual(cmd[cmd.index("--executor-profile") + 1], "test")
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "12.5")
        self.assertEqual(cmd[cmd.index("--sleep") + 1], "5")

    def test_detached_flags_contain_all_three_windows_bits(self):
        flags = gui._detached_flags()
        if os.name != "nt":
            self.assertEqual(flags, 0)
            return
        expected = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        self.assertEqual(flags, expected)
        self.assertNotEqual(flags, 0)

    def test_launch_daemon_detached_redirects_to_log(self):
        log = self.tmp / "runs" / "overnight.log"
        fake = mock.Mock()
        fake.pid = 4242
        with mock.patch.object(gui.subprocess, "Popen", return_value=fake) as popen:
            result = gui.launch_daemon_detached(
                ["py.exe", "scripts/auto_heal_loop.py", "--daemon"],
                root=self.tmp,
                log_path=log,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["log"], str(log))
        self.assertTrue(log.parent.is_dir())

        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], gui._detached_flags())
        self.assertEqual(kwargs["cwd"], str(self.tmp))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["close_fds"])

    def test_launch_daemon_detached_survives_spawn_error(self):
        with mock.patch.object(gui.subprocess, "Popen", side_effect=OSError("boom")):
            result = gui.launch_daemon_detached(
                ["py.exe"], root=self.tmp, log_path=self.tmp / "runs" / "overnight.log"
            )
        self.assertFalse(result["ok"])
        self.assertIn("spawn", result["error"])


class BridgeJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _completed(self, stdout: str, rc: int = 0):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_run_bridge_json_parses_status_and_flags(self):
        payload = {"ok": True, "action": "daemon-status", "running": False, "state_round": 3}
        completed = self._completed(json.dumps(payload, indent=2))
        with mock.patch.object(gui.subprocess, "run", return_value=completed) as run:
            result = gui.run_bridge_json("daemon-status", root=self.tmp, python_exe="py.exe")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "daemon-status")
        self.assertEqual(result["returncode"], 0)

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "py.exe")
        self.assertEqual(cmd[1], str(self.tmp / "bridge.py"))
        self.assertEqual(cmd[2], "daemon-status")
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.tmp))
        if os.name == "nt":
            self.assertEqual(
                run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW
            )

    def test_run_bridge_json_ignores_stdout_noise(self):
        completed = self._completed('warning: something\n{"ok": true, "issues": []}\n')
        with mock.patch.object(gui.subprocess, "run", return_value=completed):
            result = gui.run_bridge_json("doctor", root=self.tmp)
        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"], [])

    def test_run_bridge_json_reports_invalid_json(self):
        completed = self._completed("Traceback (most recent call last):\n", rc=1)
        with mock.patch.object(gui.subprocess, "run", return_value=completed):
            result = gui.run_bridge_json("stop", root=self.tmp)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_json")
        self.assertEqual(result["returncode"], 1)

    def test_run_bridge_json_reports_timeout(self):
        with mock.patch.object(
            gui.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="bridge.py", timeout=1),
        ):
            result = gui.run_bridge_json("doctor", root=self.tmp, timeout=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")


class GeometryAndBadgeTests(unittest.TestCase):
    def test_default_geometry_docks_off_the_main_screen(self):
        spec = gui.compute_secondary_geometry()
        self.assertEqual(spec, "480x700-500+40")
        width, height, x, y = gui.parse_geometry(spec)
        self.assertEqual((width, height), (480, 700))
        self.assertLess(x, 0, "panel must start left of the main screen")
        self.assertLessEqual(x + width, 0, "panel must not reach x >= 0 (main screen)")
        self.assertEqual(y, 40)

    def test_custom_geometry_round_trips(self):
        spec = gui.compute_secondary_geometry(420, 640, -900, 10)
        self.assertEqual(spec, "420x640-900+10")
        self.assertEqual(gui.parse_geometry(spec), (420, 640, -900, 10))
        with self.assertRaises(ValueError):
            gui.compute_secondary_geometry(0, 640)
        with self.assertRaises(ValueError):
            gui.parse_geometry("480x700")

    def test_status_badge_precedence_and_colors(self):
        running = gui.status_badge({"running": True, "state": "COMMIT_AND_PUSH"})
        self.assertEqual(running["key"], "running")
        self.assertEqual(running["color"], gui.BADGE_COLORS["running"])

        bug = gui.status_badge({"running": False, "state": "STOPPED_ON_BUG"})
        self.assertEqual(bug["key"], "bug")
        self.assertEqual(bug["color"], gui.BADGE_COLORS["bug"])

        done = gui.status_badge({"running": False, "state": "ALL_DONE"})
        self.assertEqual(done["key"], "all_done")

        roadmap_done = gui.status_badge({"running": False, "state": "STOPPED"},
                                        {"all_done": True})
        self.assertEqual(roadmap_done["key"], "all_done")

        stopped = gui.status_badge({"running": False, "state": "STOPPED"})
        self.assertEqual(stopped["key"], "stopped")
        self.assertEqual(stopped["color"], gui.BADGE_COLORS["stopped"])

        # A bug stop outranks a stale ALL_DONE marker.
        both = gui.status_badge({"running": False, "state": "STOPPED_ON_BUG"},
                                {"all_done": True})
        self.assertEqual(both["key"], "bug")

    def test_status_badge_accepts_plain_state_string(self):
        self.assertEqual(gui.status_badge("STOPPED_ON_BUG")["key"], "bug")
        self.assertEqual(gui.status_badge("ALL_DONE")["key"], "all_done")
        self.assertEqual(gui.status_badge(None)["key"], "stopped")


if __name__ == "__main__":
    unittest.main()
