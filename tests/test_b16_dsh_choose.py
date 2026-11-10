"""B16-S3：dsh choose 失败必须可归因，末行 stdout 必须是 JSON。

D4b 现场：`exit_code=1`、`stderr=""`、`error_message="choose exit code 1"`、
elapsed 5.985s。原因链：
- `open_menu` 只返空列表，`menu_not_open` 分支没有 hint / 调试图；
- 服务端只从 stdout 末行 JSON 取 hint，取不到就回退到裸退出码；
- `main` 的 try 从 `if a.list` 才开始，`find_window` / `rect_of` / `Desk()`
  抛异常时只剩 stderr。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCRIPT = ROOT / "src" / "scripts" / "dsh_choose.py"


def load_function(name, namespace):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8-sig"), filename=str(SCRIPT))
    definition = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPT), "exec"), namespace)
    return namespace[name]


class OpenMenuTests(unittest.TestCase):
    def _run(self, *, already=False, rows=(), chevron=None):
        calls = []

        class Desk:
            def click(self, x, y):
                calls.append(("click", x, y))

        namespace = {
            "time": mock.Mock(),
            "menu_open": lambda d, win: already,
            "menu_rows": lambda d, win: list(rows),
            "chip_chevron": lambda d, win: chevron,
        }
        return load_function("open_menu", namespace)(Desk(), (0, 0, 100, 100)), calls

    def test_already_open_reports_why(self):
        (got, why), calls = self._run(already=True, rows=[(10, 20)])
        self.assertEqual(got, [(10, 20)])
        self.assertEqual(why, "already_open")
        self.assertEqual(calls, [])

    def test_missing_chevron_is_distinct(self):
        (_got, why), calls = self._run()
        self.assertEqual(why, "chip_not_found")
        self.assertEqual(calls, [])

    def test_click_without_rows_is_distinct(self):
        (_got, why), calls = self._run(chevron=(50, 60))
        self.assertEqual(why, "rows_empty")
        self.assertEqual(len(calls), 1)

    def test_successful_click_reports_opened(self):
        (got, why), _calls = self._run(chevron=(50, 60), rows=[(10, 20)])
        self.assertEqual(why, "opened")
        self.assertEqual(got, [(10, 20)])


class MenuFailTests(unittest.TestCase):
    def test_failure_carries_hint_debug_shot_and_probe(self):
        written = []

        class Desk:
            def grab(self, *args, **kwargs):
                return object()

            def find(self, *args, **kwargs):
                return None

        namespace = {
            "os": __import__("os"),
            "cv2": mock.Mock(imwrite=lambda path, img: written.append(path) or True),
            "DEBUG_DIR": str(ROOT / "evidence"),
            "TPL_SEND": "dsh_send",
            "_band": lambda win: win,
            "chip_chevron": lambda d, win: None,
        }
        out = load_function("_menu_fail", namespace)(
            Desk(), (10, 20, 300, 400), "chip_not_found", "model")
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "menu_not_open")
        self.assertEqual(out["why"], "chip_not_found")
        self.assertIn("hint", out)
        self.assertTrue(out["debug_shot"].endswith("dsh_menu_not_open_model.png"))
        self.assertEqual(len(written), 1)
        self.assertEqual(out["probe"]["win"], [10, 20, 300, 400])
        self.assertFalse(out["probe"]["send"])
        self.assertFalse(out["probe"]["chevron"])


class SetModelTests(unittest.TestCase):
    def test_menu_not_open_uses_the_diagnostic_helper(self):
        namespace = {
            "MODEL_LABELS": {"deepseek_v41_flash": "DeepSeek-V4.1-Flash"},
            "open_menu": lambda d, win: ([], "rows_empty"),
            "_menu_fail": lambda d, win, why, tag: {
                "ok": False, "stage": "menu_not_open", "why": why, "tag": tag},
        }
        out = load_function("set_model", namespace)(
            object(), (0, 0, 100, 100), "deepseek_v41_flash")
        self.assertEqual(out["why"], "rows_empty")
        self.assertEqual(out["tag"], "model")

    def test_legacy_open_menu_returning_just_rows_still_works(self):
        namespace = {
            "MODEL_LABELS": {"deepseek_v41_flash": "DeepSeek-V4.1-Flash"},
            "open_menu": lambda d, win: [],
            "_menu_fail": lambda d, win, why, tag: {
                "ok": False, "stage": "menu_not_open", "why": why, "tag": tag},
        }
        out = load_function("set_model", namespace)(
            object(), (0, 0, 100, 100), "deepseek_v41_flash")
        self.assertEqual(out["why"], "unknown")


class EntryGuardTests(unittest.TestCase):
    """兜底放在 `__main__`，不包 main：`test_desktop_controls` 按函数名 exec。"""

    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8-sig")

    def test_entry_wraps_lock_call(self):
        self.assertIn("code = _lock.entry(\"dsh_choose\", main)", self.text)
        self.assertIn("except Exception as exc:", self.text)
        self.assertIn('"stage": "internal_error"', self.text)

    def test_uncaught_exception_prints_json_last(self):
        # 兜底分支里的 print 必须在 raise SystemExit(code) 之前。
        self.assertLess(self.text.index('"stage": "internal_error"'),
                        self.text.index("raise SystemExit(code)"))

    def test_main_still_owns_the_idle_guard(self):
        tree = ast.parse(self.text)
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        names = [node.func.id for node in ast.walk(main)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertIn("idle_seconds", names)
        self.assertNotIn("_main", names)


if __name__ == "__main__":
    unittest.main()
