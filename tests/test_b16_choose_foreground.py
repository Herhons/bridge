"""B16-S3：三个桌面 choose 的 `activate` 与 use 走同一条抢前台路径。

B15 只把四个 `*_use.py` 换成 `desk.ensure_foreground`（section + SW_RESTORE +
Alt 解前台锁 + topmost 闪），三个 `*_choose.py` 还留着旧的
`SetForegroundWindow` 循环——前台是桌面 WorkerW 时一律抢不回来。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

CHOICE_SCRIPTS = (
    "src/scripts/codex_choose.py",
    "src/scripts/dsh_choose.py",
    "src/scripts/opencode_choose.py",
)


def activate_source(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8-sig"), filename=rel)
    return next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "activate")


def load_activate(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8-sig"), filename=rel)
    definition = activate_source(rel)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [definition], type_ignores=[])
    namespace = {"__name__": "choose_under_test"}
    exec(compile(ast.fix_missing_locations(module), rel, "exec"), namespace)
    return namespace["activate"]


class ActivateTests(unittest.TestCase):
    def test_every_choose_delegates_to_ensure_foreground(self):
        for rel in CHOICE_SCRIPTS:
            with self.subTest(file=rel):
                fake_desk = mock.Mock()
                fake_desk.ensure_foreground.return_value = True
                fake_qi = types.ModuleType("qi")
                fake_qi.desk = fake_desk
                with mock.patch.dict(sys.modules, {"qi": fake_qi}):
                    got = load_activate(rel)(5678, tries=7)
                self.assertTrue(got)
                fake_desk.ensure_foreground.assert_called_once_with(5678, tries=7)

    def test_return_value_is_passed_through(self):
        fake_desk = mock.Mock()
        fake_desk.ensure_foreground.return_value = False
        fake_qi = types.ModuleType("qi")
        fake_qi.desk = fake_desk
        with mock.patch.dict(sys.modules, {"qi": fake_qi}):
            self.assertFalse(load_activate("src/scripts/dsh_choose.py")(1))

    def test_activate_body_has_no_direct_foreground_calls(self):
        for rel in CHOICE_SCRIPTS:
            with self.subTest(file=rel):
                body = ast.unparse(activate_source(rel))
                self.assertIn("ensure_foreground", body)
                self.assertNotIn("SetForegroundWindow", body)
                self.assertNotIn("AttachThreadInput", body)


if __name__ == "__main__":
    unittest.main()
