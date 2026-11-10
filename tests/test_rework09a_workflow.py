
from offline_guard import ensure_offline
ensure_offline()

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from qi import progress, server, workflow
from test_card09a import make_root, write_progress


class SharedWorkflowTransaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))
        self.path = self.root / "progress.json"
        write_progress(self.root, {"T8": {"state": "draft", "no_progress": 0,
                        "parent": None, "missing": [], "history": []}})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.update(card_id="05", note="OLD_NOTE", verdicts={"keep": True}, queue=["keep"])
        server.atomic_write_json(self.path, data)
        self.engine = server.Engine(root=self.root, token="fixture", port=0)
        self.addCleanup(self.engine.stop)

    def move(self):
        return self.engine.workflow_move({"file": "progress.json", "card": "T8", "to": "sent"})

    def assert_preserved(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["card_id"], "ANOTHER-CARD")
        self.assertEqual(data["note"], "NEW_NOTIFY_NOTE")
        self.assertEqual(data["verdicts"], {"keep": True})
        self.assertEqual(data["queue"], ["keep"])
        self.assertEqual(data["workflow"]["cards"]["T8"]["state"], "sent")

    def test_real_child_notify_writer_waits_during_server_read_modify_write(self):
        actual = server.atomic_write_json
        children = []
        def interleave(path, data):
            code = ("from qi import progress\nprint('START',flush=True)\n"
                    f"progress.write('ANOTHER-CARD','done','NEW_NOTIFY_NOTE',root={str(self.root)!r})\n"
                    "print('DONE',flush=True)")
            child = subprocess.Popen([sys.executable, "-B", "-c", code],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(child)
            self.assertEqual(child.stdout.readline().strip(), "START")
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=.1)
            return actual(path, data)
        try:
            with mock.patch.object(server, "atomic_write_json", side_effect=interleave):
                self.move()
            out, err = children[0].communicate(timeout=8)
            self.assertEqual(children[0].returncode, 0, out + err)
            self.assertIn("DONE", out)
            self.assert_preserved()
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_server_reloads_after_notify_writer_releases_the_same_lock(self):
        entered, errors = threading.Event(), []
        def move():
            try:
                entered.set()
                self.move()
            except Exception as exc:
                errors.append(exc)
        with progress.locked(self.root):
            thread = threading.Thread(target=move)
            thread.start()
            self.assertTrue(entered.wait(3))
            thread.join(timeout=.1)
            self.assertTrue(thread.is_alive())
            data = progress.merge(progress.read(self.root), "ANOTHER-CARD", "done", "NEW_NOTIFY_NOTE")
            progress.save(data, self.root)
        thread.join(timeout=8)
        self.assertFalse(errors, errors)
        self.assertFalse(thread.is_alive())
        self.assert_preserved()

    def test_workflow_cli_mutation_shares_notify_lock_too(self):
        actual = workflow.save_progress
        finished = threading.Event()
        errors = []
        def notify():
            try:
                progress.write("ANOTHER-CARD", "done", "NEW_NOTIFY_NOTE", root=self.root)
            except Exception as exc:
                errors.append(exc)
            finished.set()
        threads = []
        def interleave(data, target, *args, **kwargs):
            thread = threading.Thread(target=notify)
            threads.append(thread)
            thread.start()
            self.assertFalse(finished.wait(.1))
            return actual(data, target, *args, **kwargs)
        with mock.patch.object(workflow, "save_progress", side_effect=interleave):
            workflow.move("T8", "sent", progress_path=self.path,
                          workflow_path=self.root / "docs/workflow/workflow.json")
        for thread in threads:
            thread.join(timeout=8)
        self.assertFalse(errors, errors)
        self.assertTrue(finished.is_set())
        self.assert_preserved()

    def test_invalid_transition_and_write_error_release_the_lock_without_lost_fields(self):
        before = self.path.read_bytes()
        with self.assertRaises(server.RequestError) as caught:
            self.engine.workflow_move({"file": "progress.json", "card": "T8", "to": "done"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch.object(server, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.move()
        with progress.locked(self.root, timeout=.2):
            pass
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
