
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src/scripts")]
from qi import autopush, tier_guard as tg
import workbuddy_autopush as cli


class AutoPushIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "src/scripts"
        self.scripts.mkdir(parents=True)
        (self.root / "docs").mkdir()
        tiers = tg.load_tiers()
        tiers["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=["fixture-low"], review=["fixture-high"], accept=["fixture-high"])
        (self.root / "docs/tiers.json").write_text(json.dumps(tiers), encoding="utf-8")
        self.progress = self.root / "progress.json"
        self.progress.write_text(json.dumps({"card_id": "08", "status": "running",
                                            "updated_at": "2026-11-01T00:00:00+08:00"}), encoding="utf-8")
        self.argv = ["--progress", str(self.progress), "--runs", str(self.root / "runs"), "--purpose", "test"]
        patcher = mock.patch.object(cli, "SCRIPTS", self.scripts)
        patcher.start()
        self.addCleanup(patcher.stop)
        template = f'''# Generated no-UI fixture.
from qi import tier_guard as tg,uilock
from pathlib import Path
import json,sys
root=Path({str(self.root)!r})
def body():
    with (root/'events').open('a') as out: out.write(Path(__file__).stem+'\\n')
    tiers=json.loads((root/'docs/tiers.json').read_text(encoding='utf-8'))
    if 'choose' in __file__:
        tg.write_receipt('workbuddy',target=sys.argv[-1],readback=sys.argv[-1],root=root)
        result={{'ok':True}}
    else:
        tg.gate('workbuddy','test',root=root,tiers=tiers,reread=lambda dim:'fixture-low')
        result={{'ok':True,'stage':'answered','submission':'accepted','sent':True}}
    print(json.dumps(result))
    return 0
sys.exit(uilock.run('offline-autopush',body,root=root,timeout=.2))
'''
        for name in ("workbuddy_choose.py", "workbuddy_use.py"):
            (self.scripts / name).write_text(template, encoding="utf-8")

    def invoke(self, runner=cli.run_command, extra=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(self.argv + list(extra), runner=runner)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_real_no_ui_children_refresh_expired_consumed_receipt_and_do_not_deadlock(self):
        tg.write_receipt("workbuddy", target="fixture-low", readback="fixture-low", root=self.root,
                         now="2020-01-01T00:00:00+00:00")
        tg.consume("workbuddy", root=self.root)
        code, got = self.invoke()
        self.assertEqual(code, 0, got)
        self.assertTrue(got["pushed"])
        self.assertEqual((self.root / "events").read_text().splitlines(),
                         ["workbuddy_choose", "workbuddy_use"])
        self.assertTrue(tg.read_receipt("workbuddy", root=self.root)["consumed"])

    def test_already_answered_is_not_counted_as_a_new_push(self):
        actual = cli.run_command
        def runner(cmd, **kwargs):
            if "choose" in Path(cmd[1]).name:
                return actual(cmd, **kwargs)
            return 0, '{"ok":true,"stage":"already_answered","sent":false}', ""
        code, got = self.invoke(runner)
        self.assertEqual(code, 0)
        self.assertFalse(got["pushed"])
        self.assertTrue(got["already_answered"])
        self.assertEqual(autopush.pushes(autopush.read_events(self.root / "runs"), "08"), [])

    def test_missing_new_receipt_refuses_use_and_dry_run_writes_nothing(self):
        calls = []
        def runner(cmd, **kwargs):
            calls.append(cmd)
            return 0, '{"ok":true}', ""
        code, got = self.invoke(runner, extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "runs").exists())
        self.assertFalse(calls)
        code, got = self.invoke(runner)
        self.assertEqual(code, 1)
        self.assertFalse(got["pushed"])
        self.assertEqual(len(calls), 1)

    def test_two_actual_instances_submit_only_one_push(self):
        codes = []
        with mock.patch("builtins.print"):
            threads = [threading.Thread(target=lambda: codes.append(cli.main(self.argv))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertEqual(codes, [0, 0])
        self.assertEqual((self.root / "events").read_text().splitlines(),
                         ["workbuddy_choose", "workbuddy_use"])
        self.assertEqual(len(autopush.pushes(autopush.read_events(self.root / "runs"), "08")), 1)


if __name__ == "__main__":
    unittest.main()
