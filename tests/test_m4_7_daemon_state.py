"""Offline coverage for M4-7: ``engine/daemon_state.py`` as the single copy.

The scheduler (``scripts/auto_heal_loop.py``), the CLI (``bridge.py``) and the
GUI (``bridge_gui.pyw``) all used to carry their own pid probe / STOP handling /
roadmap parser / atomic state writer. This file pins the shared module and the
fact that the callers really delegate to it instead of keeping a private copy.

Nothing here touches a real scheduler: processes are never spawned or signalled
(``pid_alive`` is only asked about this very process and obvious non-pids), and
every file operation stays inside a ``TemporaryDirectory``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import auto_heal_loop as ahl  # noqa: E402
import bridge  # noqa: E402
from engine import daemon_state  # noqa: E402


class PidProbeTests(unittest.TestCase):
    def test_own_pid_is_alive(self):
        self.assertTrue(daemon_state.pid_alive(os.getpid()))

    def test_non_pids_are_never_alive(self):
        for value in (0, -1, -4242, "not-a-pid", None, ""):
            self.assertFalse(daemon_state.pid_alive(value), value)
        # A cleaned-up pid file reads as "no scheduler" instead of raising.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(daemon_state.scheduler_pid(tmp))
            (Path(tmp) / daemon_state.PID_FILE).write_text("garbage\n", encoding="utf-8")
            self.assertIsNone(daemon_state.scheduler_pid(tmp))
            (Path(tmp) / daemon_state.PID_FILE).write_text("4242\n", encoding="utf-8")
            self.assertEqual(daemon_state.scheduler_pid(tmp), 4242)


class AtomicWriteTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_tmp_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            target.write_text('{"old": true}', encoding="utf-8")
            daemon_state._atomic_write_text(target, '{"new": true}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"new": true}')
            self.assertEqual(
                sorted(item.name for item in Path(tmp).iterdir()), ["state.json"]
            )

    def test_machine_never_overwrites_a_live_foreign_pid(self):
        other = os.getpid() + 4242
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            canonical = runs / daemon_state.STATE_FILE
            canonical.write_text(
                json.dumps({"state": "DISPATCH_EXECUTOR", "pid": other}),
                encoding="utf-8",
            )
            machine = daemon_state.StateMachine(
                runs_dir=runs, pid_alive_fn=lambda pid: True
            )
            machine.set("PICK_TASK", round_no=1)

            sidecar = runs / ("state_machine.%d.json" % os.getpid())
            self.assertEqual(json.loads(canonical.read_text(encoding="utf-8"))["pid"], other)
            self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8"))["state"], "PICK_TASK")
            self.assertEqual(machine.written_to, sidecar)

    def test_dead_foreign_pid_is_reclaimed_and_explicit_path_is_never_isolated(self):
        other = os.getpid() + 4242
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / daemon_state.STATE_FILE).write_text(
                json.dumps({"state": "STOPPED", "pid": other}), encoding="utf-8"
            )
            machine = daemon_state.StateMachine(
                runs_dir=runs, pid_alive_fn=lambda pid: False
            )
            machine.set("PICK_TASK", round_no=1)
            self.assertEqual(machine.written_to, runs / daemon_state.STATE_FILE)
            self.assertEqual(
                json.loads((runs / daemon_state.STATE_FILE).read_text(encoding="utf-8"))["pid"],
                os.getpid(),
            )

            explicit = runs / "custom_state.json"
            explicit.write_text(json.dumps({"state": "IDLE", "pid": other}), encoding="utf-8")
            direct = daemon_state.StateMachine(
                path=explicit, pid_alive_fn=lambda pid: True
            )
            direct.set("PICK_TASK", round_no=2)
            self.assertEqual(direct.written_to, explicit)
            self.assertEqual(
                json.loads(explicit.read_text(encoding="utf-8"))["pid"], os.getpid()
            )

    def test_unknown_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            machine = daemon_state.StateMachine(runs_dir=Path(tmp))
            with self.assertRaises(ValueError):
                machine.set("NOT_A_REAL_STATE")


class RoadmapParsingTests(unittest.TestCase):
    def test_counts_ignore_plain_sub_bullets(self):
        text = (
            "- [ ] pending one\n"
            "- [x] done one\n"
            "  - [X] nested done\n"
            "  - plain sub-note (not a checkbox)\n"
            "- [ ] pending two\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ROADMAP.md"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(daemon_state.roadmap_counts(path), (2, 4))
            self.assertEqual(daemon_state.roadmap_counts(Path(tmp) / "absent.md"), (0, 0))
            self.assertTrue(daemon_state.has_pending_roadmap_items(text))
            self.assertFalse(daemon_state.has_pending_roadmap_items("- [x] only done\n"))

    def test_all_done_needs_its_own_line(self):
        self.assertTrue(daemon_state.has_all_done_marker("work\nALL_DONE\n"))
        self.assertTrue(daemon_state.has_all_done_marker("  `ALL_DONE`  "))
        self.assertFalse(daemon_state.has_all_done_marker("write ALL_DONE when finished"))
        self.assertFalse(daemon_state.has_all_done_marker("- [ ] ALL_DONE later"))

    def test_roadmap_all_done_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ROADMAP.md"
            self.assertEqual(
                daemon_state.roadmap_all_done(path), (False, "roadmap_missing")
            )
            path.write_text("- [ ] still open\n", encoding="utf-8")
            self.assertEqual(
                daemon_state.roadmap_all_done(path), (False, "pending items remain")
            )
            path.write_text("- [x] closed\n", encoding="utf-8")
            self.assertEqual(
                daemon_state.roadmap_all_done(path), (True, "no pending checkbox")
            )
            path.write_text("- [ ] open but ALL_DONE\nALL_DONE\n", encoding="utf-8")
            self.assertEqual(
                daemon_state.roadmap_all_done(path), (True, "ALL_DONE line present")
            )


class StopFileTests(unittest.TestCase):
    def test_stop_requested_tracks_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            self.assertFalse(daemon_state.stop_requested(runs))
            daemon_state.stop_path(runs).write_text("pid=1\n", encoding="utf-8")
            self.assertTrue(daemon_state.stop_requested(runs))
            (runs / daemon_state.PID_FILE).write_text("1\n", encoding="utf-8")
            daemon_state.clear_stop_files(runs)
            self.assertFalse(daemon_state.stop_requested(runs))
            self.assertEqual(sorted(item.name for item in runs.iterdir()), [])
            daemon_state.clear_stop_files(runs)  # idempotent


class CooldownMarkerTests(unittest.TestCase):
    NOW = datetime(2026, 11, 21, 12, 0, 0)

    def _write(self, tmp: Path, payload: dict) -> Path:
        path = tmp / daemon_state.COOLDOWN_FILE
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_both_key_spellings_are_honoured(self):
        until = (self.NOW + timedelta(hours=2)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            for key in ("until", "cooldown_until"):
                path = self._write(Path(tmp), {key: until, "reason": "quota"})
                self.assertEqual(
                    daemon_state.read_opencode_cooldown_until(path, now=self.NOW),
                    until,
                    key,
                )

    def test_expired_malformed_and_keyless_markers_read_as_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            past = (self.NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
            for payload in (
                {"until": past},
                {"cooldown_until": past},
                {"until": "not-a-date"},
                {"endpoint": "opencode"},
            ):
                path = self._write(root, payload)
                self.assertEqual(
                    daemon_state.read_opencode_cooldown_until(path, now=self.NOW), "",
                    payload,
                )
            self.assertEqual(
                daemon_state.read_opencode_cooldown_until(root / "absent.json", now=self.NOW),
                "",
            )
            broken = root / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertEqual(
                daemon_state.read_opencode_cooldown_until(broken, now=self.NOW), ""
            )


class SharedImplementationTests(unittest.TestCase):
    """The callers must delegate, not keep a private copy that can drift."""

    def test_module_objects_are_shared(self):
        self.assertTrue(issubclass(ahl.StateMachine, daemon_state.StateMachine))
        # Both callers keep the same objects for the control files they use.
        for name in (
            "STOP_FILE",
            "PID_FILE",
            "STATE_FILE",
            "ROADMAP_PATH",
            "ROADMAP_PENDING_RE",
            "ROADMAP_ITEM_RE",
        ):
            self.assertIs(getattr(ahl, name), getattr(daemon_state, name), name)
            self.assertIs(getattr(bridge, name), getattr(daemon_state, name), name)
        # Scheduler-only names stay re-exported from the shared module too.
        self.assertIs(ahl.COOLDOWN_FILE, daemon_state.COOLDOWN_FILE)
        self.assertIs(ahl.ALL_DONE_MARKER, daemon_state.ALL_DONE_MARKER)
        self.assertIs(ahl.STATE_NAMES, daemon_state.STATE_NAMES)

    def test_cli_reads_the_pid_file_through_the_shared_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / daemon_state.PID_FILE
            pid_path.write_text(" 4242 extra\n", encoding="utf-8")
            self.assertEqual(bridge.scheduler_pid(pid_path), 4242)
            self.assertEqual(
                bridge.scheduler_pid(pid_path), daemon_state.read_pid_file(pid_path)
            )
            self.assertIsNone(bridge.scheduler_pid(Path(tmp) / "nope.pid"))

    def test_scheduler_honours_the_operator_spelling_of_the_cooldown(self):
        until = (datetime.now() + timedelta(hours=3)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / daemon_state.COOLDOWN_FILE).write_text(
                json.dumps({"cooldown_until": until, "reason": "quota"}), encoding="utf-8"
            )
            with mock.patch.object(ahl, "RUNS", runs):
                self.assertEqual(ahl.opencode_cooldown_until(), until)

    def test_cli_status_counts_the_roadmap_with_the_shared_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (root / daemon_state.ROADMAP_PATH).parent.mkdir(parents=True)
            (root / daemon_state.ROADMAP_PATH).write_text(
                "- [ ] open\n- [x] done\n", encoding="utf-8"
            )
            status = bridge.daemon_status(runs=runs, root=root)
        self.assertTrue(status["roadmap_present"])
        self.assertEqual(status["roadmap_pending"], 1)
        self.assertEqual(status["roadmap_total"], 2)
        self.assertFalse(status["stop_requested"])
        self.assertFalse(status["running"])


if __name__ == "__main__":
    unittest.main()
