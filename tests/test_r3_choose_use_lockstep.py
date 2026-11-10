"""UI-BOOTSTRAP-3 / choose 与 use 必须是**同一套识别**，且首页一律 fail-closed。

两件事：
1. `opencode_use.py` 里那份定位/读取函数是 `opencode_choose.py` 的副本。
   副本走样过一次（use 还停在"距底 170px"的旧实现，于是 04:01 发送前读不回档位）。
   这里逐字核对 AST：以后改一边不改另一边，直接红。
2. 首页/会话列表没有 composer：choose 也要在**点任何东西之前**拒掉，
   并且 `_reread_tier` 只许回"读到了 / 没读到"两种，不许把 unknown 当值。
"""
from offline_guard import ensure_offline
ensure_offline()

import ast
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "src" / "scripts", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import test_r3_opencode_use_path as R3  # noqa: E402
import opencode_choose as CHOOSE  # noqa: E402

USE = R3.USE
SCRIPTS = ROOT / "src" / "scripts"

# use 里那份副本必须与 choose 逐字相同的函数（顺序即文档）
SHARED = [
    "_band", "_content", "_box", "composer_row", "chevrons",
    "_uia_root", "chip_node", "_model_key", "_model_from_uia",
    "composer_edit", "session_state",
    "panel_bbox", "panel_open", "search_box", "open_model_panel", "close_panel",
    "effort_menu_open", "open_effort_menu",
    "read_effort", "read_model", "_search_box", "set_model", "set_effort",
    "list_state", "send_state", "band_top",
]


def functions(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out.setdefault(node.name, []).append(ast.dump(node))
    return out


class DuplicateImplementationStaysInLockstep(unittest.TestCase):
    def test_every_shared_function_is_ast_identical(self):
        choose = functions(SCRIPTS / "opencode_choose.py")
        use = functions(SCRIPTS / "opencode_use.py")
        missing = [name for name in SHARED if name not in choose or name not in use]
        self.assertEqual(missing, [], "两边都得有这些函数")
        diff = [name for name in SHARED if choose[name] != use[name]]
        self.assertEqual(diff, [],
                         "这些函数在 choose/use 里已经走样了；改一边必须同步另一边："
                         f"{diff}")

    def test_the_copy_is_not_defined_twice(self):
        """同名的副本不许出现两份（第一份会被后一份静默覆盖）。"""
        use = functions(SCRIPTS / "opencode_use.py")
        dup = [name for name in SHARED if len(use.get(name, [])) != 1]
        self.assertEqual(dup, [], f"重复定义：{dup}")

    def test_presend_still_uses_the_shared_reader(self):
        """发送前的门必须调 `_reread_tier`，而 `_reread_tier` 必须调共享的读法。"""
        tree = ast.parse((SCRIPTS / "opencode_use.py").read_text(encoding="utf-8"))
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        for name in ("read_model", "read_effort", "session_state"):
            self.assertIn(name, calls, f"use 没有调用 {name}")


class RereadContract(unittest.TestCase):
    """`_reread_tier` 只回两种结果：有值（带 source）或 value=None（拒发）。"""

    def test_unreadable_is_none_value_never_a_placeholder_string(self):
        desk = R3.RecordingDesk(R3.blank_canvas())
        got = USE._reread_tier(desk, R3.WIN, "model")
        self.assertIsInstance(got, dict)
        self.assertIsNone(got["value"])
        self.assertIn("source", got)
        for bad in ("unknown", "none", "null", ""):
            self.assertNotEqual(got["value"], bad,
                                "读不到就是 None，不能拿占位字符串当值")

    def test_readable_value_carries_its_source(self):
        desk = R3.RecordingDesk(R3.centered_canvas())
        with mock.patch.object(USE, "_uia_root", return_value=None):
            got = USE._reread_tier(desk, R3.WIN, "model")
        self.assertEqual(got["value"], "glm_53_flash_free")
        self.assertEqual(got["source"], "template")

    def test_exception_is_folded_into_none(self):
        boom = SimpleNamespace(find=mock.Mock(side_effect=RuntimeError("控件炸了")))
        got = USE._reread_tier(boom, R3.WIN, "model")
        self.assertIsNone(got["value"])
        self.assertIn("RuntimeError", got["reason"])

    def test_unknown_dimension_is_refused(self):
        desk = R3.RecordingDesk(R3.centered_canvas())
        self.assertIsNone(USE._reread_tier(desk, R3.WIN, "writer")["value"])


class ChooseOnHome(unittest.TestCase):
    """choose 在首页：不点、不打字，直接 session_not_open。"""

    def run_choose(self, argv, canvas):
        desk = R3.RecordingDesk(canvas)
        out = io.StringIO()
        with mock.patch.object(CHOOSE, "find_window", return_value=R3.FAKE_HWND), \
                mock.patch.object(CHOOSE, "rect_of", return_value=R3.WIN), \
                mock.patch.object(CHOOSE, "activate", return_value=True), \
                mock.patch.object(CHOOSE, "Desk", return_value=desk), \
                mock.patch.object(CHOOSE, "_uia_root", return_value=None), \
                mock.patch.object(sys, "argv", ["opencode_choose.py"] + argv), \
                mock.patch("time.sleep"), contextlib.redirect_stdout(out):
            code = CHOOSE.main()
        return code, desk, out.getvalue()

    def test_model_selection_is_refused_before_any_click(self):
        code, desk, text = self.run_choose(
            ["--model", "glm_53_flash_free", "--force"], R3.blank_canvas())
        self.assertEqual(code, 1, text)
        self.assertIn("session_not_open", text)
        self.assertEqual(desk.clicks, [], "首页上一个像素都不该点")
        self.assertEqual(desk.keys, [], "也不该打字/按键")

    def test_list_on_home_fails_closed(self):
        code, desk, text = self.run_choose(["--list", "--force"], R3.blank_canvas())
        self.assertEqual(code, 1, text)
        self.assertIn("session_not_open", text)
        self.assertEqual(desk.keys, [])

    def test_a_centered_session_still_works(self):
        """同一套判据在真会话里不能误伤：居中 composer 应被认成 session。"""
        desk = R3.RecordingDesk(R3.centered_canvas())
        with mock.patch.object(CHOOSE, "_uia_root", return_value=None):
            got = CHOOSE.session_state(desk, R3.WIN)
        self.assertEqual(got["state"], "session")
        self.assertEqual(got["source"], "send")


class ProbeDoesNotTouchAnything(unittest.TestCase):
    def test_probe_is_read_only(self):
        desk = R3.RecordingDesk(R3.centered_canvas())
        with mock.patch.object(CHOOSE, "_uia_root", return_value=None):
            CHOOSE.probe(desk, R3.WIN)
            USE.probe(desk, R3.WIN)
        self.assertEqual(desk.clicks, [])
        self.assertEqual(desk.keys, [])


if __name__ == "__main__":
    unittest.main()
