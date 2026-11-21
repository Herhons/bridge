"""Offline coverage for the Bridge v0.2.5 M4-1 control plane.

Pins four new contracts and one CLI surface:

* ``docs/ROADMAP.md`` is the first backlog source and an empty roadmap ends the
  run with ``ALL_DONE`` on the first green round (no three-round spin);
* ``runs/STOP`` stops the loop at round start, before a dispatch and inside the
  sleep slices, recording ``STOPPED_BY_USER`` and cleaning STOP + pid file;
* ``StateMachine`` writes atomically (``.tmp`` + ``os.replace``) and never
  overwrites the state file of a *live* foreign scheduler;
* a task card already ``dispatched`` / ``timed_out`` is never re-dispatched;
* ``bridge.py stop`` / ``bridge.py daemon-status`` behave sanely with no daemon.

Nothing here starts the daemon, spawns a real executor, opens a window, touches
the clipboard or runs ``taskkill``: subprocesses, sleeps and liveness probes are
all mocked. Safe to run on a developer machine.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(ROOT_DIR))
import auto_heal_loop as ahl  # noqa: E402
import bridge  # noqa: E402

BASE = "0123456789abcdef0123456789abcdef01234567"
CARD = (
    "<<<WORKBUDDY_TASK_START>>>\n"
    "step 1: patch engine/base.py\n"
    "step 2: run the full unittest suite\n"
    "<<<WORKBUDDY_TASK_END>>>"
)


def _fake_git(*args, dirty: bool = False):
    if args[:2] == ("rev-parse", "HEAD"):
        return SimpleNamespace(returncode=0, stdout=BASE + "\n", stderr="")
    if args == ("status", "--porcelain"):
        return SimpleNamespace(
            returncode=0, stdout=" M engine/base.py" if dirty else "", stderr=""
        )
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def _reply(text: str) -> str:
    return json.dumps({"ok": True, "reply": text}, ensure_ascii=False)


class RoadmapSourceTests(unittest.TestCase):
    def test_roadmap_is_the_first_backlog_candidate(self):
        self.assertEqual(ahl.BACKLOG_CANDIDATES[0], "docs/ROADMAP.md")
        self.assertEqual(ahl.BACKLOG_CANDIDATES[0], ahl.ROADMAP_PATH)

    def test_load_backlog_prefers_roadmap_over_other_todos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            (root / "docs").mkdir(parents=True)
            (root / "docs" / "ROADMAP.md").write_text(
                "- [ ] M4-2 model calibration\n", encoding="utf-8"
            )
            runs.mkdir()
            (runs / "todo.md").write_text("legacy todo\n", encoding="utf-8")
            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs):
                text, source = ahl.load_backlog()

        self.assertEqual(source, "docs/ROADMAP.md")
        self.assertIn("M4-2 model calibration", text)

    def test_pending_checkbox_detection(self):
        self.assertTrue(ahl.has_pending_roadmap_items("- [ ] open\n- [x] done\n"))
        self.assertTrue(ahl.has_pending_roadmap_items("   - [ ] nested item"))
        self.assertFalse(ahl.has_pending_roadmap_items("- [x] done\n- [X] DONE\n"))
        self.assertFalse(ahl.has_pending_roadmap_items(""))

    def test_all_done_line_must_stand_alone(self):
        self.assertTrue(ahl.has_all_done_marker("work\nALL_DONE\n"))
        self.assertTrue(ahl.has_all_done_marker("  `ALL_DONE`  "))
        self.assertFalse(ahl.has_all_done_marker("write ALL_DONE when finished"))
        self.assertFalse(ahl.has_all_done_marker("- [ ] ALL_DONE later"))

    def test_roadmap_counts(self):
        text = "- [x] a\n- [ ] b\n  - [ ] c\nnot a box [ ]\n"
        self.assertEqual(ahl.roadmap_counts(text), (2, 3))


class AllDoneExitTests(unittest.TestCase):
    def test_roadmap_without_pending_items_exits_all_done_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (root / "docs").mkdir()
            (root / "docs" / "ROADMAP.md").write_text(
                "# roadmap\n- [x] M1\n- [x] M4-1\n", encoding="utf-8"
            )
            # M4-8: an empty roadmap no longer exits locally — the loop must get
            # the brain's sign-off marker first. Nothing about the assertions
            # below changes; only the brain is stubbed.
            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(ahl, "git", side_effect=_fake_git), \
                    mock.patch.object(
                        ahl, "run_tests", return_value=(0, "OK")
                    ) as tests, \
                    mock.patch.object(
                        ahl, "request_task_from_gemini"
                    ) as ask, \
                    mock.patch.object(
                        ahl, "request_signoff_from_brain",
                        return_value=ahl.ALL_DONE_SIGN_OFF_MARKER,
                    ) as signoff, \
                    mock.patch.object(ahl, "execute_brain_reply") as dispatch, \
                    mock.patch.object(ahl.time, "sleep"):
                rc = ahl.run_overnight(max_rounds=20, no_push=True, sleep=0)
            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(tests.call_count, 1)
        ask.assert_not_called()
        signoff.assert_called_once()
        dispatch.assert_not_called()
        self.assertEqual(state["state"], "ALL_DONE")
        history = [item["state"] for item in state["history"]]
        self.assertIn("ALL_DONE", history)
        self.assertNotIn("STOPPED", history)

    def test_all_done_marker_wins_over_pending_boxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ROADMAP.md").write_text(
                "- [ ] leftover\nALL_DONE\n", encoding="utf-8"
            )
            with mock.patch.object(ahl, "ROOT", root):
                done, why = ahl.roadmap_all_done()

        self.assertTrue(done)
        self.assertIn("ALL_DONE", why)

    def test_roadmap_with_pending_items_keeps_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ROADMAP.md").write_text(
                "- [x] M4-1\n- [ ] M4-2\n", encoding="utf-8"
            )
            with mock.patch.object(ahl, "ROOT", root):
                done, why = ahl.roadmap_all_done()

        self.assertFalse(done)
        self.assertEqual(why, "pending items remain")

    def test_missing_roadmap_is_not_all_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "ROOT", Path(tmp)):
                self.assertEqual(
                    ahl.roadmap_all_done(), (False, "roadmap_missing")
                )


class StopControlTests(unittest.TestCase):
    def test_stop_file_before_start_exits_without_running_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / ahl.STOP_FILE).write_text("1\n", encoding="utf-8")
            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(ahl, "git", side_effect=_fake_git), \
                    mock.patch.object(ahl, "run_tests") as tests, \
                    mock.patch.object(ahl.time, "sleep"):
                rc = ahl.run_overnight(max_rounds=5, no_push=True, sleep=0)
            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
            stop_left = (runs / ahl.STOP_FILE).exists()
            pid_left = (runs / ahl.PID_FILE).exists()

        self.assertEqual(rc, 0)
        tests.assert_not_called()
        self.assertEqual(state["state"], "STOPPED_BY_USER")
        self.assertIn("before start", state["reason"])
        self.assertFalse(stop_left)
        self.assertFalse(pid_left)

    def test_stop_appearing_mid_round_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()

            def green_then_stop(timeout):
                (runs / ahl.STOP_FILE).write_text("1\n", encoding="utf-8")
                return 0, "OK"

            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(ahl, "git", side_effect=_fake_git), \
                    mock.patch.object(ahl, "run_tests", side_effect=green_then_stop), \
                    mock.patch.object(
                        ahl, "load_backlog", return_value=("todo", "test-todo")
                    ), \
                    mock.patch.object(ahl, "request_task_from_gemini") as ask, \
                    mock.patch.object(ahl.time, "sleep"):
                rc = ahl.run_overnight(max_rounds=3, no_push=True, sleep=0)
            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
            stop_left = (runs / ahl.STOP_FILE).exists()

        self.assertEqual(rc, 0)
        ask.assert_not_called()
        self.assertEqual(state["state"], "STOPPED_BY_USER")
        self.assertIn("before dispatch", state["reason"])
        self.assertFalse(stop_left)

    def test_stop_during_sleep_ends_the_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()

            def stop_on_sleep(seconds):
                (runs / ahl.STOP_FILE).write_text("1\n", encoding="utf-8")

            with mock.patch.object(ahl, "ROOT", root), \
                    mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "LOG_NAMES", ("auto_heal_loop.log",)), \
                    mock.patch.object(
                        ahl, "git",
                        side_effect=lambda *a: _fake_git(*a, dirty=False),
                    ), \
                    mock.patch.object(ahl, "run_tests", return_value=(0, "OK")), \
                    mock.patch.object(
                        ahl, "load_backlog", return_value=("todo", "test-todo")
                    ), \
                    mock.patch.object(
                        ahl, "request_task_from_gemini", return_value=CARD
                    ), \
                    mock.patch.object(ahl, "call_executor", return_value="done"), \
                    mock.patch.object(ahl.time, "sleep", side_effect=stop_on_sleep):
                rc = ahl.run_overnight(max_rounds=3, no_push=True, sleep=5)
            state = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(state["state"], "STOPPED_BY_USER")
        self.assertIn("during sleep", state["reason"])

    def test_sleep_is_interrupted_as_soon_as_stop_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)

            def stop_during_sleep(seconds):
                (runs / ahl.STOP_FILE).write_text("1\n", encoding="utf-8")

            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(
                        ahl.time, "sleep", side_effect=stop_during_sleep
                    ):
                interrupted = ahl.sleep_with_stop_watch(5.0)

        self.assertTrue(interrupted)

    def test_zero_sleep_keeps_one_noop_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl.time, "sleep") as sleeper:
                interrupted = ahl.sleep_with_stop_watch(0)

        self.assertFalse(interrupted)
        sleeper.assert_called_once_with(0)


class StateMachineIsolationTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_tmp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "log"):
                machine = ahl.StateMachine()
                machine.set("PICK_TASK", reason="roadmap", round_no=1)
            data = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
            leftovers = sorted(item.name for item in runs.iterdir())

        self.assertEqual(data["state"], "PICK_TASK")
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(machine.written_to, runs / ahl.STATE_FILE)
        self.assertEqual(leftovers, [ahl.STATE_FILE])

    def test_torn_tmp_file_never_corrupts_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / (ahl.STATE_FILE + ".tmp")).write_text("TORN{", encoding="utf-8")
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "log"):
                machine = ahl.StateMachine()
                machine.set("BRAIN_PLAN", round_no=2)
            data = json.loads((runs / ahl.STATE_FILE).read_text(encoding="utf-8"))
            tmp_left = (runs / (ahl.STATE_FILE + ".tmp")).exists()

        self.assertEqual(data["state"], "BRAIN_PLAN")
        self.assertFalse(tmp_left)

    def test_live_foreign_pid_keeps_the_canonical_file(self):
        other = os.getpid() + 4242
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            seed = {
                "state": "DISPATCH_EXECUTOR",
                "pid": other,
                "round": 7,
                "reason": "other scheduler",
                "updated_at": "2026-01-01T00:00:00",
                "history": [
                    {
                        "state": "DISPATCH_EXECUTOR",
                        "at": "2026-01-01T00:00:00",
                        "reason": "other scheduler",
                    }
                ],
            }
            (runs / ahl.STATE_FILE).write_text(
                json.dumps(seed), encoding="utf-8"
            )
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "pid_alive", return_value=True), \
                    mock.patch.object(ahl, "log"):
                machine = ahl.StateMachine()
                machine.set("PICK_TASK", reason="newcomer", round_no=1)
            canonical = json.loads(
                (runs / ahl.STATE_FILE).read_text(encoding="utf-8")
            )
            sidecar_path = runs / ("state_machine.%d.json" % os.getpid())
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

        self.assertEqual(canonical["pid"], other)
        self.assertEqual(canonical["state"], "DISPATCH_EXECUTOR")
        self.assertEqual(sidecar["state"], "PICK_TASK")
        self.assertEqual(sidecar["pid"], os.getpid())
        self.assertEqual(machine.written_to, sidecar_path)

    def test_dead_foreign_pid_is_reclaimed(self):
        other = os.getpid() + 4242
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / ahl.STATE_FILE).write_text(
                json.dumps({"state": "STOPPED", "pid": other}), encoding="utf-8"
            )
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "pid_alive", return_value=False), \
                    mock.patch.object(ahl, "log"):
                machine = ahl.StateMachine()
                machine.set("PICK_TASK", round_no=1)
            canonical = json.loads(
                (runs / ahl.STATE_FILE).read_text(encoding="utf-8")
            )
            sidecars = sorted(
                item.name for item in runs.iterdir() if "." in item.stem
            )

        self.assertEqual(canonical["state"], "PICK_TASK")
        self.assertEqual(canonical["pid"], os.getpid())
        self.assertEqual(machine.written_to, runs / ahl.STATE_FILE)
        self.assertEqual(sidecars, [])

    def test_explicit_path_is_never_isolated(self):
        other = os.getpid() + 4242
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "custom_state.json"
            target.write_text(
                json.dumps({"state": "IDLE", "pid": other}), encoding="utf-8"
            )
            with mock.patch.object(ahl, "pid_alive", return_value=True), \
                    mock.patch.object(ahl, "log"):
                machine = ahl.StateMachine(path=target)
                machine.set("PICK_TASK", round_no=1)
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(data["state"], "PICK_TASK")
        self.assertEqual(machine.written_to, target)


class DispatchIdempotencyTests(unittest.TestCase):
    def test_card_id_is_stable_and_round_scoped(self):
        first = ahl.card_id_for(CARD, 3)
        self.assertEqual(first, ahl.card_id_for(CARD, 3))
        self.assertNotEqual(first, ahl.card_id_for(CARD, 4))
        self.assertNotEqual(
            first, ahl.card_id_for(CARD.replace("step 1", "step 9"), 3)
        )
        self.assertTrue(first.startswith("round3_"))
        self.assertEqual(len(first.split("_", 1)[1]), 12)

    def test_marked_and_extracted_card_share_one_id(self):
        extracted = ahl.extract_task_card(CARD)
        self.assertEqual(
            ahl.card_id_for(CARD, 3), ahl.card_id_for(extracted, 3)
        )
        self.assertEqual(ahl.card_digest(CARD), ahl.card_digest(extracted))

    def test_timeout_blocks_redispatch_of_the_same_card(self):
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return 0, _reply("done")

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "log"):
                first = ahl.call_executor(CARD, 5, 30)
                card_id = ahl.card_id_for(CARD, 5)
                completed = ahl.read_dispatch_record(card_id)
                ahl.finish_dispatch(card_id, completed=False,
                                    detail="simulated bridge timeout")
                sent_before = len(calls)
                second = ahl.call_executor(CARD, 5, 30)
                blocked = ahl.read_dispatch_record(card_id)

        self.assertEqual(first, "done")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(second, "")
        self.assertEqual(len(calls), sent_before)
        self.assertEqual(blocked["status"], "timed_out")
        self.assertTrue(blocked["needs_review"])
        self.assertEqual(blocked["blocked_reason"], "already_timed_out")

    def test_still_dispatched_card_is_never_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            card_id = ahl.card_id_for(CARD, 2)
            record_path = runs / ahl.DISPATCH_DIR / ("%s.json" % card_id)
            record_path.parent.mkdir(parents=True)
            record_path.write_text(
                json.dumps({"card_id": card_id, "status": "dispatched"}),
                encoding="utf-8",
            )
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd") as runner, \
                    mock.patch.object(ahl, "log"):
                reply = ahl.call_executor(CARD, 2, 30)
                record = ahl.read_dispatch_record(card_id)

        self.assertEqual(reply, "")
        runner.assert_not_called()
        self.assertTrue(record["needs_review"])
        self.assertEqual(record["blocked_reason"], "already_dispatched")

    def test_completed_card_may_be_dispatched_again(self):
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, timeout=300, cwd=None):
            calls.append([str(part) for part in cmd])
            return 0, _reply("done")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ahl, "RUNS", Path(tmp)), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "log"):
                ahl.call_executor(CARD, 6, 30)
                second = ahl.call_executor(CARD, 6, 30)

        self.assertEqual(second, "done")
        self.assertEqual(len(calls), 2)

    def test_execute_brain_reply_reports_a_blocked_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "log"):
                ahl.prepare_dispatch(CARD, 4)
                ahl.finish_dispatch(
                    ahl.card_id_for(CARD, 4), completed=False
                )
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd") as runner, \
                    mock.patch.object(ahl, "log"):
                result = ahl.execute_brain_reply(CARD, 4, 30)

        self.assertEqual(result["mode"], "task")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["executor_reply"], "")
        self.assertEqual(result["card_id"], ahl.card_id_for(CARD, 4))
        runner.assert_not_called()


class DaemonFlagTests(unittest.TestCase):
    def test_daemon_is_an_exact_alias_of_overnight(self):
        parser = ahl.build_parser()
        self.assertTrue(parser.parse_args(["--daemon"]).overnight)
        self.assertTrue(parser.parse_args(["--overnight"]).overnight)
        self.assertFalse(parser.parse_args([]).overnight)

    def test_daemon_alias_keeps_the_other_flags_working(self):
        args = ahl.build_parser().parse_args(
            ["--daemon", "--brain", "deepseek", "--sleep", "30"]
        )
        self.assertTrue(args.overnight)
        self.assertEqual(args.brain, "deepseek")
        self.assertEqual(args.sleep, 30)


class BridgeControlTests(unittest.TestCase):
    def test_stop_and_daemon_status_are_registered(self):
        parser = bridge.build_parser()
        stop_args = parser.parse_args(["stop", "--force-wait", "2"])
        self.assertEqual(stop_args.command, "stop")
        self.assertEqual(stop_args.force_wait, 2.0)
        self.assertEqual(parser.parse_args(["stop"]).force_wait, 5.0)
        self.assertEqual(
            parser.parse_args(["daemon-status"]).command, "daemon-status"
        )

    def test_stop_without_a_scheduler_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            res = bridge.stop_scheduler(0, runs=runs)
            stop_left = (runs / bridge.STOP_FILE).exists()

        self.assertTrue(res["ok"])
        self.assertFalse(res["running"])
        self.assertIsNone(res["pid"])
        self.assertFalse(res["stop_written"])
        self.assertFalse(stop_left)

    def test_stale_pid_file_is_cleaned_without_writing_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / bridge.PID_FILE).write_text("54321", encoding="utf-8")
            with mock.patch.object(bridge, "pid_alive", return_value=False):
                res = bridge.stop_scheduler(0, runs=runs)
            stop_left = (runs / bridge.STOP_FILE).exists()
            pid_left = (runs / bridge.PID_FILE).exists()

        self.assertTrue(res["ok"])
        self.assertTrue(res["pid_file_removed"])
        self.assertFalse(res["stop_written"])
        self.assertFalse(stop_left)
        self.assertFalse(pid_left)

    def test_stop_writes_the_file_then_force_kills(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / bridge.PID_FILE).write_text("54321", encoding="utf-8")
            alive = {"now": True}
            killed: list[int] = []

            def fake_alive(pid):
                return alive["now"]

            def fake_kill(pid):
                killed.append(pid)
                alive["now"] = False
                return 0

            with mock.patch.object(bridge, "pid_alive", side_effect=fake_alive), \
                    mock.patch.object(
                        bridge, "kill_scheduler_tree", side_effect=fake_kill
                    ):
                res = bridge.stop_scheduler(0, runs=runs)
            stop_left = (runs / bridge.STOP_FILE).exists()
            pid_left = (runs / bridge.PID_FILE).exists()

        self.assertTrue(res["stop_written"])
        self.assertTrue(res["killed"])
        self.assertEqual(res["taskkill_rc"], 0)
        self.assertEqual(killed, [54321])
        self.assertTrue(res["ok"])
        self.assertFalse(res["running"])
        self.assertFalse(res["exited_gracefully"])
        self.assertFalse(stop_left)
        self.assertFalse(pid_left)

    def test_stop_exits_gracefully_when_the_loop_honours_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            (runs / bridge.PID_FILE).write_text("54321", encoding="utf-8")
            alive = {"now": True}

            def fake_alive(pid):
                # The "loop" notices runs/STOP on its next poll and exits.
                if (runs / bridge.STOP_FILE).exists():
                    alive["now"] = False
                return alive["now"]

            with mock.patch.object(bridge, "pid_alive", side_effect=fake_alive), \
                    mock.patch.object(bridge, "kill_scheduler_tree") as killer, \
                    mock.patch.object(bridge.time, "sleep"):
                res = bridge.stop_scheduler(5, runs=runs)

        self.assertTrue(res["ok"])
        self.assertTrue(res["exited_gracefully"])
        self.assertFalse(res["killed"])
        self.assertIsNone(res["taskkill_rc"])
        killer.assert_not_called()

    def test_daemon_status_reports_pid_state_and_roadmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (root / "docs").mkdir()
            (root / "docs" / "ROADMAP.md").write_text(
                "- [x] M4-1\n- [ ] M4-2\n- [ ] M4-3\n", encoding="utf-8"
            )
            (runs / bridge.PID_FILE).write_text(
                str(os.getpid()), encoding="utf-8"
            )
            (runs / bridge.STATE_FILE).write_text(
                json.dumps({
                    "state": "PICK_TASK",
                    "pid": 999,
                    "round": 3,
                    "reason": "source=docs/ROADMAP.md",
                    "updated_at": "2026-11-21T12:00:00",
                }),
                encoding="utf-8",
            )
            with mock.patch.object(bridge, "pid_alive", return_value=True):
                res = bridge.daemon_status(runs=runs, root=root)

        self.assertTrue(res["ok"])
        self.assertEqual(res["pid"], os.getpid())
        self.assertTrue(res["running"])
        self.assertEqual(res["state"], "PICK_TASK")
        self.assertEqual(res["state_round"], 3)
        self.assertEqual(res["state_pid"], 999)
        self.assertTrue(res["roadmap_present"])
        self.assertEqual(res["roadmap_pending"], 2)
        self.assertEqual(res["roadmap_total"], 3)
        self.assertFalse(res["stop_requested"])

    def test_daemon_status_is_quiet_when_nothing_ran_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = bridge.daemon_status(runs=root / "runs", root=root)

        self.assertTrue(res["ok"])
        self.assertFalse(res["running"])
        self.assertIsNone(res["pid"])
        self.assertIsNone(res["state"])
        self.assertFalse(res["roadmap_present"])
        self.assertIsNone(res["roadmap_pending"])
        self.assertEqual(res["action"], "daemon-status")

    def test_cli_daemon_status_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            buf = io.StringIO()
            with mock.patch.object(bridge, "ROOT", root), \
                    redirect_stdout(buf):
                rc = bridge.main(["daemon-status"])
            data = json.loads(buf.getvalue())

        self.assertEqual(rc, 0)
        self.assertEqual(data["action"], "daemon-status")
        self.assertFalse(data["running"])
        self.assertFalse(data["roadmap_present"])


if __name__ == "__main__":
    unittest.main()
