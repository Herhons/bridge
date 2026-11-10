
from offline_guard import ensure_offline
ensure_offline()

import ast
import ctypes
import json
import tempfile
import types
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from qi import guard
import test_desktop_controls as fixtures

SCRIPTS = tuple(f"{endpoint}_{action}.py" for endpoint in ("codex", "dsh", "opencode") for action in ("choose", "use"))


class AllInputEntries(unittest.TestCase):
    def test_all_six_actual_idle_functions_ignore_own_tick_but_respect_later_user_input(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                idle = fixtures.load_function(script, "idle_seconds", {})
                with mock.patch.object(guard, "_last_input_tick", return_value=99000), \
                        mock.patch.object(guard, "synthetic_tick", return_value=99000):
                    self.assertEqual(idle(), guard.OWN_IDLE)
                with mock.patch.object(guard, "_last_input_tick", return_value=99500), \
                        mock.patch.object(guard, "synthetic_tick", return_value=99000), \
                        mock.patch.object(guard._kernel32, "GetTickCount64", return_value=100000):
                    self.assertEqual(idle(), .5)

    def test_all_six_actual_desk_primitives_write_the_shared_marker_with_fake_os_input(self):
        for script in SCRIPTS:
            for method, args in (("click", (10, 10)), ("press", ("delete",)),
                                 ("hotkey", ("ctrl", "a")), ("type_text", ("x",))):
                with self.subTest(script=script, method=method), tempfile.TemporaryDirectory() as tmp:
                    def cursor(pointer):
                        pointer._obj.x, pointer._obj.y = 10, 10
                        return 1
                    fake = types.SimpleNamespace(SetCursorPos=lambda *a: 1, GetCursorPos=cursor,
                         mouse_event=lambda *a: None, keybd_event=lambda *a: None, SendInput=lambda *a: 1)
                    ns = {"ctypes": ctypes, "wintypes": wintypes, "time": fixtures.Clock(),
                          "user32": fake, "CLICK_MAX": 16, "CLICK_GAP": .35,
                          "VK": {"delete": 46, "ctrl": 17, "a": 65, "shift": 16, "enter": 13}}
                    sink = types.SimpleNamespace(_assert_fg=lambda: None, _invalidate=lambda: None,
                                                 _clicks=[], CLICK_MAX=16, CLICK_GAP=.35)
                    mark = fixtures.load_function(script, "_mark_input", ns, method=True)
                    sink._mark_input = types.MethodType(mark, sink)
                    for name in ("press", "hotkey"):
                        setattr(sink, name, types.MethodType(fixtures.load_function(script, name, ns, method=True), sink))
                    call = fixtures.load_function(script, method, ns, method=True)
                    path = Path(tmp) / "marker.json"
                    with mock.patch.object(guard, "MARK_FILE", path), \
                            mock.patch.object(guard, "_last_input_tick", return_value=99000):
                        call(sink, *args)
                        self.assertEqual(guard.synthetic_tick(), 99000)
                        self.assertEqual(json.loads(path.read_text())["kind"], "desk")

    def test_marker_expiry_future_corruption_and_tick_wrap_are_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marker.json"
            for data in ({"tick": 123, "wall": 1}, {"tick": 123, "wall": 500},
                         {"tick": "bad", "wall": 100}, {"tick": 123}, {"tick": -1, "wall": 100}):
                path.write_text(json.dumps(data), encoding="utf-8")
                with mock.patch.object(guard, "_MARK_CACHE", {}), mock.patch.object(guard.time, "time", return_value=100):
                    self.assertIsNone(guard.synthetic_tick(path=path))
            with mock.patch.object(guard, "_last_input_tick", return_value=0xFFFFFFF0), \
                    mock.patch.object(guard, "synthetic_tick", return_value=None), \
                    mock.patch.object(guard._kernel32, "GetTickCount64", return_value=0x100000020):
                self.assertAlmostEqual(guard.idle_seconds(), .048)

    def test_all_main_functions_still_use_the_repaired_idle_entry(self):
        for script in SCRIPTS:
            _, tree = fixtures.source_tree(script)
            main = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "main")
            names = [x.func.id for x in ast.walk(main) if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)]
            self.assertIn("idle_seconds", names, script)


if __name__ == "__main__":
    unittest.main()
