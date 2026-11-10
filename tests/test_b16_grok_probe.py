"""B16-S5：grok 的 `menu_not_open` 要能分步留证，并有进程内菜单截图。

现场：`choose.open_menu` 只返一个 bool，anchor 找不到和菜单开了没一行匹上
完全分不清；重截模板时又不能从控制台另起 `snip.py --shot`（一抢焦点菜单就关）。
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

CHOOSE = ROOT / "src" / "qi" / "choose.py"
GROK = ROOT / "src" / "scripts" / "grok_choose.py"


def load_function(path, name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    definition = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == name)
    future = ast.parse("from __future__ import annotations\n").body
    module = ast.Module(body=future + [definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def open_menu_fixture(*, anchor=True, item_hit=None):
    class Screen:
        last_best = 0.42

        def __init__(self):
            self.clicks = []

        def find(self, name, **kwargs):
            return object() if anchor else None

        def click(self, *args, **kwargs):
            self.clicks.append(args)

    def unique(s, name, **kwargs):
        hit = object() if (item_hit and name.endswith(item_hit)) else None
        return {"match": hit, "ambiguous": False, "best": 0.91 if hit else 0.40,
                "scale": 1.0}

    cfg = mock.Mock()
    cfg.open_by_chip = False
    cfg.anchor = "grok_composer"
    cfg.open_offset = (530, 0)
    cfg.options = {"fast": "Fast", "expert": "Expert"}
    cfg.has_chip = ()
    cfg.prefix = "grok_"
    namespace = {
        "time": mock.Mock(),
        "_unique": unique,
        "_chip": lambda c, k: f"{c.prefix}chip_{k}",
        "_item": lambda c, k: f"{c.prefix}item_{k}",
        "_win": lambda s, c: (0, 0, 100, 100),
    }
    screen = Screen()
    fn = load_function(CHOOSE, "open_menu", namespace)
    return fn(screen, cfg), namespace.get("_LAST_OPEN", {}), screen


class OpenMenuDiagnosticsTests(unittest.TestCase):
    def test_missing_anchor_is_named(self):
        ok, info, screen = open_menu_fixture(anchor=False)
        self.assertFalse(ok)
        self.assertEqual(info["why"], "anchor_not_found")
        self.assertFalse(info["anchor"]["hit"])
        self.assertEqual(screen.clicks, [])

    def test_menu_without_item_matches_is_named(self):
        ok, info, screen = open_menu_fixture()
        self.assertFalse(ok)
        self.assertEqual(info["why"], "menu_empty")
        self.assertEqual(len(screen.clicks), 1)
        self.assertEqual(set(info["items"]), {"fast", "expert"})
        self.assertFalse(info["items"]["fast"]["hit"])

    def test_successful_open_reports_which_item_hit(self):
        ok, info, _screen = open_menu_fixture(item_hit="expert")
        self.assertTrue(ok)
        self.assertEqual(info["why"], "opened")
        self.assertTrue(info["items"]["expert"]["hit"])
        self.assertEqual(info["win"], [0, 0, 100, 100])

    def test_choose_failure_carries_why_and_probe(self):
        namespace = {
            "read_current": lambda s, c: {"state": "unknown", "score": 0.4,
                                          "template": {"name": "grok_chip_fast"}},
            "open_menu": lambda s, c: False,
        }
        choose = load_function(CHOOSE, "choose", namespace)
        cfg = mock.Mock(options={"fast": "Fast"})
        out = choose(object(), cfg, "fast")
        self.assertEqual(out["stage"], "menu_not_open")
        self.assertEqual(out["why"], "unknown")
        self.assertEqual(out["probe"], {})
        self.assertIn("hint", out)


class ProbeWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = GROK.read_text(encoding="utf-8-sig")

    def test_chip_offset_matches_the_live_probe(self):
        """2026-11-09 实测：锚点中心 (464,580) + (584,8) 才点到 Fast 芯片。"""
        self.assertIn("open_offset=(584, 8)", self.text)
        chip = (ROOT / "src" / "qi" / "chip.py").read_text(encoding="utf-8-sig")
        self.assertIn('open_offset=(584, 8)', chip)

    def test_probe_takes_a_snapshot_in_process(self):
        self.assertIn('s.snapshot("grok-menu")', self.text)
        self.assertIn("choose_mod.open_menu(s, CFG)", self.text)
        self.assertIn("scales=zoom_mod.SCALES", self.text)
        self.assertIn('s.tab_site = "grok"', self.text)

    def test_main_routes_probe_before_library_gate(self):
        calls = []
        namespace = {
            "sys": mock.Mock(argv=["grok_choose.py", "--probe", "--force"]),
            "probe": lambda argv: calls.append(list(argv)) or 0,
            "cli": lambda cfg, argv: 99,
            "Library": mock.Mock(),
            "CFG": object(),
        }
        main = load_function(GROK, "main", namespace)
        self.assertEqual(main(), 0)
        self.assertEqual(calls, [["--force"]])

    def test_normal_argv_still_goes_to_cli(self):
        lib = mock.Mock()
        lib.report.return_value = ""
        lib.missing.return_value = []
        namespace = {
            "sys": mock.Mock(argv=["grok_choose.py", "--list"]),
            "probe": lambda argv: 77,
            "cli": lambda cfg, argv: 0,
            "Library": lambda: lib,
            "CFG": object(), "REQUIRED": [],
        }
        self.assertEqual(load_function(GROK, "main", namespace)(), 0)


if __name__ == "__main__":
    unittest.main()
