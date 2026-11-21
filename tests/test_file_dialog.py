"""Offline tests for ``engine/file_dialog.py`` (v0.2.5 split).

No real GUI: a fake user32 covers the ``#32770`` walk. The point of these tests
is the Windows 10 defect found live — ``GetDlgItem(dialog, 1148)`` returns the
outer ``ComboBoxEx32``, so ``WM_SETTEXT`` never reached the inner ``Edit`` and
the attach dialog sat there empty.
"""
from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import engine.base as base
import engine.file_dialog as fd
from engine.uia import UIAError

OUTER_COMBO_EX = 999
INNER_EDIT = 4242
OPEN_BUTTON = 2001


class _FakeDialogUser32:
    """Fake user32 exposing the Win10 combo layout (outer handle != inner edit)."""

    def __init__(self, *, inner_edit: int = INNER_EDIT, direct: int = OUTER_COMBO_EX,
                 combo_layout: bool = True, open_button: int = OPEN_BUTTON):
        self.inner_edit = inner_edit
        self.direct = direct
        self.combo_layout = combo_layout
        self.open_button = open_button
        self.messages: list[tuple[int, int, object]] = []

    def FindWindowExW(self, parent, after, cls, name):
        if parent is None:
            return 100 if not after else 0
        if not self.combo_layout:
            return 0
        return {"ComboBoxEx32": 101, "ComboBox": 102, "Edit": self.inner_edit}.get(cls, 0)

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        pid_ref.value = 4242
        return 1

    def GetDlgItem(self, hwnd, ctrl_id):
        if ctrl_id == fd.FILE_NAME_CONTROL_ID:
            return self.direct
        if ctrl_id == fd.OPEN_BUTTON_CONTROL_ID:
            return self.open_button
        return 0

    def SendMessageW(self, hwnd, msg, wparam, lparam):
        self.messages.append((int(hwnd), int(msg), lparam))
        return 0

    def IsWindow(self, hwnd):
        return 0


class EditControlResolutionTests(unittest.TestCase):
    def test_inner_edit_wins_over_outer_combo_ex(self):
        fake = _FakeDialogUser32()
        self.assertEqual(fd._dialog_edit_hwnd(fake, 100), INNER_EDIT)
        self.assertNotEqual(fd._dialog_edit_hwnd(fake, 100), OUTER_COMBO_EX)

    def test_direct_control_id_is_the_fallback(self):
        fake = _FakeDialogUser32(combo_layout=False, direct=OUTER_COMBO_EX)
        self.assertEqual(fd._dialog_edit_hwnd(fake, 100), OUTER_COMBO_EX)

    def test_no_edit_control_resolves_to_zero(self):
        fake = _FakeDialogUser32(combo_layout=False, direct=0)
        self.assertEqual(fd._dialog_edit_hwnd(fake, 100), 0)


class FillDialogTests(unittest.TestCase):
    def test_wm_settext_targets_inner_edit_not_outer_handle(self):
        marshaller = SimpleNamespace(c_wchar_p=lambda text: text)
        fake = _FakeDialogUser32()
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "one.txt"
            f1.write_text("1", encoding="utf-8")
            method = fd.fill_windows_file_dialog(
                [str(f1)], owner_pid=4242, timeout=1, poll=0,
                user32=fake, ctypes_module=marshaller,
            )

        self.assertEqual(method, "#32770:file_dialog")
        texts = [m for m in fake.messages if m[1] == fd.WM_SETTEXT]
        clicks = [m for m in fake.messages if m[1] == fd.BM_CLICK]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0][0], INNER_EDIT)
        self.assertEqual(texts[0][2], '"%s"' % str(f1))
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0][0], OPEN_BUTTON)

    def test_missing_edit_control_raises_target_not_found(self):
        marshaller = SimpleNamespace(c_wchar_p=lambda text: text)
        fake = _FakeDialogUser32(combo_layout=False, direct=0)
        with self.assertRaises(UIAError) as ctx:
            fd.fill_windows_file_dialog(
                ["x.txt"], owner_pid=4242, timeout=0.1, poll=0,
                user32=fake, ctypes_module=marshaller,
            )
        self.assertEqual(ctx.exception.code, "TARGET_NOT_FOUND")


class User32WiringTests(unittest.TestCase):
    class _CtypesStub:
        c_void_p = "void_p"
        c_uint = "uint"
        c_ssize_t = "ssize_t"
        c_int = "int"
        c_ulong = "ulong"
        c_wchar_p = "wchar_p"

        @staticmethod
        def POINTER(kind):  # noqa: N802 - mirrors ctypes spelling
            return ("ptr", kind)

    def test_sendmessage_gets_64bit_safe_signature(self):
        user32 = SimpleNamespace(
            FindWindowExW=SimpleNamespace(),
            GetDlgItem=SimpleNamespace(),
            GetWindowThreadProcessId=SimpleNamespace(),
            IsWindow=SimpleNamespace(),
            SendMessageW=SimpleNamespace(),
        )
        stub = self._CtypesStub()
        with mock.patch.object(fd, "ctypes", SimpleNamespace(
            **{name: getattr(stub, name) for name in dir(stub)
               if not name.startswith("__")},
            windll=SimpleNamespace(user32=user32),
        )):
            resolved = fd._win32_user32()

        self.assertIs(resolved, user32)
        self.assertEqual(
            user32.SendMessageW.argtypes,
            ["void_p", "uint", "void_p", "void_p"],
        )
        self.assertEqual(user32.SendMessageW.restype, "ssize_t")
        self.assertEqual(user32.GetDlgItem.argtypes, ["void_p", "int"])

    def test_missing_attributes_are_tolerated(self):
        class _Hostile:
            def __setattr__(self, name, value):
                raise AttributeError(name)

        with mock.patch.object(fd, "ctypes", SimpleNamespace(
            **{name: getattr(self._CtypesStub, name)
               for name in ("c_void_p", "c_uint", "c_ssize_t", "c_int",
                            "c_ulong", "c_wchar_p")},
            POINTER=self._CtypesStub.POINTER,
            windll=SimpleNamespace(user32=_Hostile()),
        )):
            self.assertIsInstance(fd._win32_user32(), _Hostile)


class BaseCompatibilityTests(unittest.TestCase):
    def test_base_reexports_every_symbol(self):
        for name in (
            "FILE_DIALOG_CLASS", "FILE_NAME_CONTROL_ID", "OPEN_BUTTON_CONTROL_ID",
            "WM_SETTEXT", "BM_CLICK", "_win32_user32", "_window_pid",
            "_dialog_edit_hwnd", "find_existing_file_dialog",
            "find_file_dialog_window", "fill_windows_file_dialog",
        ):
            self.assertTrue(hasattr(base, name), name)
        self.assertIs(base.find_existing_file_dialog, fd.find_existing_file_dialog)
        self.assertIs(base._dialog_edit_hwnd, fd._dialog_edit_hwnd)
        self.assertEqual(base.FILE_DIALOG_CLASS, fd.FILE_DIALOG_CLASS)

    def test_base_wrapper_keeps_its_own_ctypes_marshaller(self):
        with mock.patch.object(base, "_fill_windows_file_dialog",
                               return_value="#32770:shim") as impl:
            result = base.fill_windows_file_dialog(
                ["a.txt"], owner_pid=7, timeout=3, poll=0.1, user32="fake",
            )
        self.assertEqual(result, "#32770:shim")
        kwargs = impl.call_args.kwargs
        self.assertEqual(kwargs["owner_pid"], 7)
        self.assertIs(kwargs["ctypes_module"], base.ctypes)
        self.assertIs(kwargs["user32"], "fake")

    def test_base_no_longer_defines_the_dialog_class_constant(self):
        source = Path(base.__file__).read_text(encoding="utf-8")
        self.assertNotIn('FILE_DIALOG_CLASS = "#32770"', source)
        self.assertNotIn("FILE_NAME_CONTROL_ID = 1148", source)
        self.assertIn('FILE_DIALOG_CLASS = "#32770"',
                      Path(fd.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
