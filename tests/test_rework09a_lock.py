"""09a: actual Engine/Popen/CLI-lock integration; all business bodies are no-UI fixtures."""

from offline_guard import ensure_offline
ensure_offline()

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
from qi import server, uilock


class DelegationIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ)
        self.env.pop(uilock.DELEGATION_ENV, None)
        self.env["PYTHONPATH"] = str(SRC) + os.pathsep + self.env.get("PYTHONPATH", "")

    def command(self, body="print('ENTERED', flush=True)", root=None):
        root = root or self.root
        return [sys.executable, "-X", "utf8", "-B", "-c",
                "from qi import uilock\nimport sys,time,os,json\n"
                "def body():\n" + "\n".join("    " + s for s in body.splitlines()) +
                f"\n    return 0\nsys.exit(uilock.run('no-ui-fixture', body, root={str(root)!r}, timeout=.15))"]

    def ordinary(self):
        return subprocess.run(self.command(), env=self.env, capture_output=True,
                              text=True, timeout=8)

    def delegated(self, cmd=None, mutate=None, bind=True):
        with uilock.child_launch(root=self.root, env=self.env) as launch:
            env = dict(launch.env)
            if mutate:
                mutate(env)
            child = subprocess.Popen(cmd or self.command(), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                if bind:
                    launch.bind(child.pid)
                out, err = child.communicate(timeout=8)
                return child.returncode, out, err, env
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_actual_engine_choose_receipt_use_and_ordinary_cli_exclusion(self):
        (self.root / "docs").mkdir()
        shutil.copy2(ROOT / "docs/tiers.json", self.root / "docs/tiers.json")
        scripts = self.root / "src/scripts"
        scripts.mkdir(parents=True)
        code = f'''# Generated NO-UI business fixture, not the product's UI script.
import sys,json,os
from pathlib import Path
from qi import tier_guard,uilock
root=Path({str(self.root)!r})
def main():
    with (root/'events.txt').open('a') as f: f.write(Path(__file__).stem+'\\n')
    if 'choose' in __file__:
        tier_guard.write_receipt('grok',target=sys.argv[-1],readback=sys.argv[-1],root=root)
    print(json.dumps({{'ok':True,'stage':'answered','reply':'offline-fixture'}}))
    return 0
sys.exit(uilock.run('offline-fixture', main,root=root,timeout=.15))
'''
        for name in ("grok_choose.py", "grok_use.py"):
            (scripts / name).write_text(code, encoding="utf-8")
        engine = server.Engine(root=self.root, token="offline", port=0,
                               config={"ui_lock_timeout": 1, "choose_timeout": 8,
                                       "use_timeout": 8, "endpoint_timeout": 20})
        def close_engine():
            engine.stop()
            if engine._worker is not None:
                engine._worker.join(timeout=25)
        self.addCleanup(close_engine)
        checked = []
        actual = engine._run_use

        def between(*args, **kwargs):
            # Actual independent CLI is excluded between choose and use too.
            contender = self.ordinary()
            self.assertEqual(contender.returncode, 8, contender.stderr + contender.stdout)
            self.assertNotIn("ENTERED", contender.stdout)
            checked.append(True)
            # T2-1 起 `_run_use` 多了一个 keyword-only 的 job_id，替身要原样透传。
            return actual(*args, **kwargs)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(engine, "_run_use", side_effect=between):
            submitted, _ = engine.submit(["grok"], "offline-only", "test")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                job = engine.get_job(submitted["job_id"])
                if job["status"] in server.TERMINAL_JOB_STATES:
                    break
                time.sleep(.02)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual((self.root / "events.txt").read_text().splitlines(),
                         ["grok_choose", "grok_use"])
        self.assertEqual(checked, [True])
        self.assertEqual(self.ordinary().returncode, 0)

    def test_two_authorized_children_in_sequence_and_no_global_environment_leak(self):
        before = os.environ.get(uilock.DELEGATION_ENV)
        with uilock.hold("server", root=self.root):
            for _ in range(2):
                code, out, err, _ = self.delegated()
                self.assertEqual(code, 0, out + err)
                self.assertIn("ENTERED", out)
            self.assertNotIn("delegate", uilock.describe(self.root))
            self.assertEqual(self.ordinary().returncode, 8)
        self.assertEqual(os.environ.get(uilock.DELEGATION_ENV), before)
        self.assertEqual(self.ordinary().returncode, 0)

    def test_forged_capability_wrong_root_and_parent_start_never_enter(self):
        def change(field, value):
            def mutate(env):
                grant = json.loads(env[uilock.DELEGATION_ENV])
                grant[field] = value
                env[uilock.DELEGATION_ENV] = json.dumps(grant)
            return mutate
        with uilock.hold("server", root=self.root):
            for field, value in (("token", "forged"), ("pid", 99999999),
                                 ("process_start_time", 1), ("session", "stale"),
                                 ("path", str(self.root / "wrong/ui.lock"))):
                with self.subTest(field=field):
                    code, out, err, _ = self.delegated(mutate=change(field, value))
                    self.assertEqual(code, 8, out + err)
                    self.assertNotIn("ENTERED", out)

    def test_stale_grant_after_release_does_not_become_an_ordinary_lock(self):
        with uilock.hold("server", root=self.root):
            _, _, _, env = self.delegated()
        # Real parent remains the same, but session and child registration expired.
        got = subprocess.run(self.command(), env=env, capture_output=True, text=True, timeout=8)
        self.assertEqual(got.returncode, 8, got.stdout + got.stderr)
        self.assertNotIn("ENTERED", got.stdout)
        self.assertEqual(self.ordinary().returncode, 0)

    def test_missing_kernel_lock_and_missing_metadata_cannot_authorize(self):
        with uilock.hold("server", root=self.root):
            context = uilock._active(uilock.lock_path(self.root).resolve())
            uilock._unlock(context["handle"])
            try:
                code, out, err, _ = self.delegated()
                self.assertEqual(code, 8, out + err)
                self.assertNotIn("ENTERED", out)
            finally:
                uilock._try_lock(context["handle"])
            code, out, err, _ = self.delegated(
                mutate=lambda env: context["meta"].pop("pid"))
            self.assertEqual(code, 8, out + err)
            self.assertNotIn("ENTERED", out)

    def test_child_pid_registration_is_required_and_bound_to_one_launch(self):
        with uilock.hold("server", root=self.root):
            code, out, err, _ = self.delegated(bind=False)
            self.assertEqual(code, 8, out + err)
            self.assertNotIn("ENTERED", out)
            with uilock.child_launch(root=self.root, env=self.env) as launch:
                child = subprocess.Popen(self.command("print('ENTERED',flush=True)\ntime.sleep(.5)"),
                                         env=launch.env, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                try:
                    launch.bind(child.pid)
                    self.assertEqual(child.stdout.readline().strip(), "ENTERED")
                    sibling = subprocess.run(self.command(), env=launch.env, capture_output=True,
                                             text=True, timeout=8)
                    self.assertEqual(sibling.returncode, 8)
                    self.assertNotIn("ENTERED", sibling.stdout)
                    child.communicate(timeout=8)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)

    def test_authorized_child_cannot_redelegate_to_grandchild(self):
        with uilock.hold("server", root=self.root):
            code, out, err, _ = self.delegated(self.command(
                f"with uilock.child_launch(root={str(self.root)!r}):\n    print('BAD')"))
            self.assertEqual(code, 8, out + err)
            self.assertNotIn("BAD", out)

    def test_same_thread_different_roots_have_distinct_locks(self):
        other = self.root / "other"
        with uilock.hold("first", root=self.root):
            with uilock.hold("second", root=other):
                self.assertTrue(uilock.describe(other)["locked"])
                self.assertEqual(uilock.describe(other)["owner"], "second")
            self.assertTrue(uilock.describe(self.root)["locked"])

    def test_exception_and_timeout_release_launch_and_kernel_locks(self):
        with uilock.hold("server", root=self.root):
            failed = server.run_process(self.command("raise RuntimeError('fixture')"),
                                        cwd=self.root, env=self.env, timeout=5)
            self.assertNotEqual(failed["exit_code"], 0)
            timed = server.run_process(self.command("time.sleep(30)"), cwd=self.root,
                                       env=self.env, timeout=.5)
            self.assertTrue(timed["timeout"], timed)
            self.assertNotIn("delegate", uilock.describe(self.root))
        self.assertEqual(self.ordinary().returncode, 0)

    def test_parent_death_keeps_live_child_exclusive_then_releases(self):
        # Parent and grandchild are real no-UI processes. The grandchild lease
        # must still exclude this test process after its owning parent dies.
        ready = self.root / "child-ready"
        finish = self.root / "finish-child"
        child_cmd = self.command(
            f"from pathlib import Path\nPath({str(ready)!r}).write_text(str(os.getpid()))\n"
            f"while not Path({str(finish)!r}).exists(): time.sleep(.02)")
        parent_code = (
            "from qi import uilock\nimport subprocess,time\n"
            f"with uilock.hold('parent-fixture',root={str(self.root)!r}):\n"
            f"    with uilock.child_launch(root={str(self.root)!r}) as grant:\n"
            f"        child=subprocess.Popen({child_cmd!r},env=grant.env,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "        grant.bind(child.pid)\n        time.sleep(30)\n")
        parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + 8
            while not ready.exists() and time.monotonic() < end and parent.poll() is None:
                time.sleep(.02)
            self.assertTrue(ready.exists(), "no-UI child failed to enter")
            parent.kill()
            parent.communicate(timeout=5)
            self.assertEqual(self.ordinary().returncode, 8)
            self.assertTrue(uilock.describe(self.root)["child_lease"])
        finally:
            finish.write_text("done")
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if self.ordinary().returncode == 0:
                break
        else:
            self.fail("orphaned child lease did not release")


if __name__ == "__main__":
    unittest.main()
