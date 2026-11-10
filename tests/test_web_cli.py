"""CLI workflow regressions without importing Win32-backed application modules."""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import argparse
import ast
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    source = ROOT / path
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    definition = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[name]


class FakeForegroundLost(RuntimeError):
    pass


class FakeZoomNot100(RuntimeError):
    stage = "zoom_not_100"
    code = 1

    def as_result(self):
        return {"ok": False, "stage": self.stage, "code": self.code}


class Fixture:
    def __init__(self, script, argv):
        self.events = []
        self.results = {}
        self.script = script

        def operation(name, result):
            def run(*args):
                self.events.append(name if name == "new" else f"{name}:{args[-1]}")
                return self.results.get(name, result)
            return run

        def ensure_site(name, *, tab):
            self.events.append(("ensure_site", name, tab))
            return 123

        def read_writer(*args):
            self.events.append("read_writer")
            return self.results.get("read_writer", {"writer": "work" if script == "chatgpt" else "task"})

        def read_model(*args):
            self.events.append("read_model")
            return self.results.get("read_model", {"model": "gpt61_sol" if script == "chatgpt" else "argon"})

        def read_current(*args):
            self.events.append("read_current")
            return self.results.get("read_current", {"state": "argon"})

        class FakeScreen:
            def __init__(_self):
                self.events.append("screen")

            def bind(_self, hwnd):
                self.events.append("bind")

            def focus(_self):
                self.events.append("focus")
                return True

        class FakeLibrary:
            def report(_self, required):
                return "test templates"

            def missing(_self, required):
                return []

        cfg = types.SimpleNamespace(name="gemini" if script == "gemini" else "grok",
                                    title_hint="Google Chrome",
                                    options={"argon": "Argon", "flash": "Flash"},
                                    writers={"dialog": 0, "task": 1})
        self.cfg = cfg
        self.namespace = {
            "argparse": argparse, "json": json,
            "sys": types.SimpleNamespace(argv=[script + "_choose.py", *argv]),
            "time": types.SimpleNamespace(sleep=lambda seconds: None),
            "guard": types.SimpleNamespace(parse_guard_args=lambda values: (2, 120, False, values),
                                            guard=lambda *args: True),
            "Screen": FakeScreen, "Library": FakeLibrary,
            "ForegroundLost": FakeForegroundLost, "ensure_site": ensure_site,
            "CFG": cfg, "REQUIRED": [], "TITLE": "Google Chrome",
            "Site": lambda **kwargs: types.SimpleNamespace(**kwargs),
            "_open_new_chat": operation("new", {"ok": True}),
            "choose_writer": operation("writer", {"ok": True}),
            "set_model": operation("model", {"ok": True}),
            "set_effort": operation("effort", {"ok": True}),
            "choose": operation("model", {"ok": True}),
            "read_writer": read_writer, "read_model": read_model,
            "read_current": read_current,
            "read_effort": lambda *args: {"effort": "max"},
            "TAB_WORDS": {"chat": "chat_tpl", "work": "work_tpl"},
            "MODELS": {"chat": {"gpt6": ("GPT-6", "gpt6_tpl")},
                       "work": {"gpt61_sol": ("GPT-6.1 Sol", "gpt61_sol_tpl")}},
            "EFFORTS": {"chat": {"instant": ("Instant", 0)},
                        "work": {"max": ("Max", 1)}},
            "COMPOSER": "composer", "CHECK": "check",
            # CARD-06b 新增的模块级依赖：这里给最小替身，测的是"步骤顺序"，
            # 不是缩放/留证本身（那两条在 test_card06b.py 里覆盖）。
            "zoom_mod": types.SimpleNamespace(ensure=lambda *a, **k: None,
                                              ZoomNot100=FakeZoomNot100,
                                              SCALES=(1.0,)),
            "failure": types.SimpleNamespace(record=lambda *a, **k: None),
            "WindowMinimized": FakeForegroundLost,
            "_fail": lambda endpoint, screen, hwnd, result, code: code,
            "_evidence": lambda *a, **k: None,
            "_zoom_guard": lambda screen: None,
            "_CURRENT_HWND": {"v": None},
        }
        if script == "common":
            callback = load_function("src/qi/choose.py", "cli", self.namespace)
            self.invoke = lambda: callback(cfg, argv)
        else:
            self.invoke = load_function(f"src/scripts/{script}_choose.py", "main", self.namespace)

    def run(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return self.invoke()
            except SystemExit as exc:
                return exc.code

    @property
    def workflow(self):
        return [event for event in self.events
                if isinstance(event, str) and event not in ("screen", "bind", "focus")]


class WebCliTests(unittest.TestCase):
    def test_gemini_combined_command_applies_every_requested_step_in_order(self):
        fixture = Fixture("gemini", ["--model", "argon", "--new", "--writer", "task"])
        self.assertEqual(fixture.run(), 0)
        self.assertEqual(fixture.workflow, ["new", "writer:task", "model:argon"])
        self.assertEqual(fixture.events.count("screen"), 1)

    def test_new_and_list_reads_the_new_conversation(self):
        for script in ("gemini", "chatgpt"):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--new", "--list"])
                self.assertEqual(fixture.run(), 0)
                self.assertEqual(fixture.workflow[:3], ["new", "read_writer", "read_model"])

    def test_list_alone_does_not_create_a_conversation(self):
        for script in ("gemini", "chatgpt", "common"):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--list"])
                self.assertEqual(fixture.run(), 0)
                self.assertNotIn("new", fixture.workflow)

    def test_read_and_mutation_flags_are_rejected_before_opening_the_browser(self):
        for script, flag, value in (("gemini", "--writer", "task"),
                                    ("gemini", "--model", "argon"),
                                    ("chatgpt", "--writer", "work"),
                                    ("chatgpt", "--effort", "max"),
                                    ("common", "--model", "argon")):
            with self.subTest(script=script, flag=flag):
                fixture = Fixture(script, ["--list", flag, value])
                self.assertEqual(fixture.run(), 2)
                self.assertEqual(fixture.events, [])

    def test_failure_to_open_new_chat_stops_all_following_actions(self):
        for script, writer, model in (("gemini", "task", "argon"),
                                      ("chatgpt", "work", "gpt61_sol")):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--new", "--writer", writer, "--model", model])
                fixture.results["new"] = {"ok": False, "stage": "newchat_not_found"}
                self.assertEqual(fixture.run(), 1)
                self.assertEqual(fixture.workflow, ["new"])

    def test_writer_failure_never_selects_a_model_in_the_wrong_mode(self):
        for script, writer, model in (("gemini", "task", "argon"),
                                      ("chatgpt", "work", "gpt61_sol"),
                                      ("common", "task", "argon")):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--writer", writer, "--model", model])
                fixture.results["writer"] = {"ok": False, "stage": "unverified"}
                self.assertEqual(fixture.run(), 1)
                self.assertNotIn(f"model:{model}", fixture.workflow)

    def test_model_failure_never_applies_effort_to_the_wrong_model(self):
        fixture = Fixture("chatgpt", ["--model", "gpt61_sol", "--effort", "max"])
        fixture.results["model"] = {"ok": False, "stage": "menu_not_open"}
        self.assertEqual(fixture.run(), 1)
        self.assertEqual(fixture.workflow, ["model:gpt61_sol"])

    def test_chatgpt_successful_combination_preserves_dependency_order(self):
        fixture = Fixture("chatgpt", ["--effort", "max", "--model", "gpt61_sol", "--writer", "work"])
        self.assertEqual(fixture.run(), 0)
        self.assertEqual(fixture.workflow, ["new", "writer:work", "model:gpt61_sol", "effort:max"])

    def test_tab_selection_is_passed_to_the_browser(self):
        for script in ("gemini", "chatgpt", "common"):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--list", "--tab", "3"])
                self.assertEqual(fixture.run(), 0)
                self.assertIn(("ensure_site", fixture.cfg.name if script == "common" else script, 3),
                              fixture.events)

    def test_invalid_command_line_cannot_start_ui_work(self):
        for script in ("gemini", "chatgpt", "common"):
            for argv in (["--list", "--typo"], ["--model"], ["--list", "--tab", "0"]):
                with self.subTest(script=script, argv=argv):
                    fixture = Fixture(script, argv)
                    self.assertEqual(fixture.run(), 2)
                    self.assertEqual(fixture.events, [])

    def test_chatgpt_incompatible_writer_and_model_are_rejected_before_ui(self):
        fixture = Fixture("chatgpt", ["--writer", "chat", "--model", "gpt61_sol"])
        self.assertEqual(fixture.run(), 2)
        self.assertEqual(fixture.events, [])

    def test_unreadable_state_does_not_report_success(self):
        for script in ("gemini", "chatgpt", "common"):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--list"])
                fixture.results.update(read_writer={"writer": "unknown"},
                                       read_model={"model": "unknown"},
                                       read_current={"state": "unknown"})
                self.assertEqual(fixture.run(), 1)

    def test_unreadable_writer_is_not_success_even_if_model_reads(self):
        for script in ("gemini", "chatgpt"):
            with self.subTest(script=script):
                fixture = Fixture(script, ["--list"])
                fixture.results.update(read_writer={"writer": "unknown"},
                                       read_model={"model": "argon"},
                                       read_current={"state": "argon"})
                self.assertEqual(fixture.run(), 1)

    def test_foreground_loss_stops_a_multi_step_command(self):
        fixture = Fixture("gemini", ["--writer", "task", "--model", "argon"])

        def lost(*args):
            raise FakeForegroundLost("user selected another window")

        fixture.namespace["choose_writer"] = lost
        self.assertEqual(fixture.run(), 4)
        self.assertNotIn("model:argon", fixture.workflow)


if __name__ == "__main__":
    unittest.main()
