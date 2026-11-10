"""Desktop control contracts, with Win32 and all UI actions replaced by fakes.

The production functions are loaded from AST to avoid module-level DPI/Win32 setup.
No test starts an app, changes the clipboard, clicks the desktop, or sends a message.
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import argparse
import ast
import contextlib
import ctypes
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = tuple(f"{app}_{action}.py" for app in ("codex", "dsh", "opencode")
                for action in ("choose", "use"))


def source_tree(script):
    path = ROOT / "src" / "scripts" / script
    return path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def load_function(script, name, namespace, *, method=False):
    path, tree = source_tree(script)
    body = next(node.body for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "Desk") if method else tree.body
    node = next(node for node in body if isinstance(node, ast.FunctionDef) and node.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    code = ast.fix_missing_locations(ast.Module(body=future + [node], type_ignores=[]))
    exec(compile(code, str(path), "exec"), namespace)
    return namespace[name]


def model_labels(script):
    _, tree = source_tree(script)
    node = next(node for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "MODEL_LABELS"
                        for target in node.targets))
    return ast.literal_eval(node.value)


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeDesk:
    def __init__(self):
        self.events = []

    def click(self, *point):
        self.events.append(("click", point))

    def hotkey(self, *keys):
        self.events.append(("hotkey", keys))

    def press(self, key):
        self.events.append(("press", key))

    def type_text(self, value):
        self.events.append(("text", value))

    def find(self, *args):
        return None

    def grab(self, *args, **kwargs):
        return None


class DesktopControlTests(unittest.TestCase):
    def test_multiline_and_non_bmp_text_are_preserved_without_submission(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                events = []

                def send_input(count, pointer, size):
                    data = pointer._obj.ki
                    if not data.dwFlags & 2:
                        events.append(("unicode", data.wScan))
                    return count

                sink = types.SimpleNamespace(
                    _assert_fg=lambda: None,
                    hotkey=lambda *keys: events.append(("hotkey", keys)),
                    _invalidate=lambda: None,
                    _mark_input=lambda: None,
                )
                fn = load_function(script, "type_text", {
                    "ctypes": ctypes, "time": Clock(),
                    "user32": types.SimpleNamespace(SendInput=send_input),
                }, method=True)
                fn(sink, "A\r\nB\n中\r🙂", interval=0)
                data = bytearray()
                for kind, value in events:
                    if kind == "hotkey":
                        self.assertEqual(value, ("shift", "enter"))
                        data.extend(b"\n\x00")
                    else:
                        data.extend(value.to_bytes(2, "little"))
                self.assertEqual(data.decode("utf-16-le"), "A\nB\n中\n🙂")

    def test_foreground_loss_stops_without_reactivation(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                attempts = []
                fn = load_function(script, "_assert_fg", {
                    "user32": types.SimpleNamespace(GetForegroundWindow=lambda: 200),
                    "activate": lambda *args, **kwargs: attempts.append(args) or True,
                }, method=True)
                with self.assertRaisesRegex(RuntimeError, "目标窗口不在前台"):
                    fn(types.SimpleNamespace(hwnd=100))
                self.assertEqual(attempts, [])

    def test_foreground_loss_during_typing_stops_before_next_character(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                foreground = [100]
                events = []

                def send_input(count, pointer, size):
                    item = pointer._obj.ki
                    events.append((item.wScan, item.dwFlags))
                    if len(events) == 2:
                        foreground[0] = 200
                    return count

                namespace = {
                    "ctypes": ctypes, "time": Clock(),
                    "user32": types.SimpleNamespace(GetForegroundWindow=lambda: foreground[0],
                                                     SendInput=send_input),
                }
                foreground_check = load_function(script, "_assert_fg", namespace, method=True)
                sink = types.SimpleNamespace(hwnd=100, _invalidate=lambda: None)
                sink._assert_fg = lambda: foreground_check(sink)
                fn = load_function(script, "type_text", namespace, method=True)
                with self.assertRaises(RuntimeError):
                    fn(sink, "AB", interval=0)
                self.assertEqual(events, [(ord("A"), 4), (ord("A"), 6)])

    def set_model_fixture(self, script, actual):
        panels = iter([(0, 0, 400, 600), None])
        desk = FakeDesk()
        namespace = {
            "time": Clock(), "MODEL_LABELS": model_labels(script),
            "open_model_panel": lambda *args: True,
            "open_menu": lambda *args: [(100, 140)],
            "panel_bbox": lambda *args: next(panels),
            "_search_box": lambda panel: (200, 30),
            # 2026-11-07：set_model 改成"按占位模板定位搜索框 / 用 panel_open 判面板"，
            # 这里把新增依赖一起打桩；断言（精确读回、事件序列）一个字没改。
            "search_box": lambda *args: (200, 30),
            "panel_open": lambda *args: False,
            "close_panel": lambda *args: True,
            "TPL_SEARCH": "oc_search_ph",
            "_content": lambda win: win,
            "read_model": lambda *args: {"model": actual, "score": 1.0},
        }
        return load_function(script, "set_model", namespace), desk

    def test_model_requires_exact_readback_for_both_key_and_display_name(self):
        for script in SCRIPTS:
            if script.startswith("codex"):
                continue
            labels = model_labels(script)
            for target, label in labels.items():
                for requested in (target, label.upper()):
                    for actual in (*labels, "unknown"):
                        with self.subTest(script=script, requested=requested, actual=actual):
                            fn, desk = self.set_model_fixture(script, actual)
                            result = fn(desk, (0, 0, 1380, 1340), requested)
                            self.assertEqual(result["ok"], actual == target)
                            self.assertEqual(result["verified"], actual == target)
                            self.assertEqual(result["want"], target)
                            self.assertIn(("text", label), desk.events)

    def test_unsupported_model_does_not_change_selection(self):
        for script in SCRIPTS:
            if script.startswith("codex"):
                continue
            with self.subTest(script=script):
                fn, desk = self.set_model_fixture(script, "unknown")
                result = fn(desk, (0, 0, 1380, 1340), "unlisted model")
                self.assertFalse(result["ok"])
                self.assertEqual(result["stage"], "unsupported_model")
                self.assertEqual(desk.events, [])

    def run_main(self, script, model_ok, effort_ok):
        calls = []
        namespace = {
            "argparse": argparse, "json": json, "time": Clock(),
            "find_window": lambda: 100,
            "rect_of": lambda hwnd: (0, 0, 1380, 1340),
            "Desk": lambda hwnd: FakeDesk(), "activate": lambda hwnd: True,
            "locate": lambda *args, **kwargs: {"missing": []},
            "WIN_SIZE": (1380, 1340), "WIN_POS": (-1440, -560), "MIN_W": 1050,
            "set_model": lambda *args: calls.append("model") or {"ok": model_ok},
            "set_effort": lambda *args: calls.append("effort") or {"ok": effort_ok},
            "ask": lambda *args: calls.append("ask") or {"ok": True},
            # 2026-11-07：main 先认"现在是不是一个能输入的会话"（首页会拦成
            # session_not_open）。这里给一个"就在会话里"的桩；断言（调用顺序、
            # 退出码）一个字没改。
            "session_state": lambda *args: {"state": "session"},
            "_CURRENT_HWND": {"v": None},
        }
        argv = [script, "--model", "target", "--effort", "low", "--force"]
        if script.endswith("_use.py"):
            # 2026-11-02 CARD-06a：发送必须带 --purpose（无默认值）
            argv += ["test question", "--purpose", "test"]
        fn = load_function(script, "main", namespace)
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            result = fn()
        return result, calls

    def test_failed_model_blocks_effort_and_message(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls = self.run_main(script, False, True)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ["model"])

    def test_failed_effort_blocks_message(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls = self.run_main(script, True, False)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ["model", "effort"])

    def test_successful_configuration_runs_in_order(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, calls = self.run_main(script, True, True)
                expected = ["model", "effort"] + (["ask"] if script.endswith("_use.py") else [])
                self.assertEqual(result, 0)
                self.assertEqual(calls, expected)


if __name__ == "__main__":
    unittest.main()
