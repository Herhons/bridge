"""Offline tests for the v0.2.5 generic --attach primitive.

Everything is mocked: no real GUI, no clipboard, no focus stealing, no bridge
subprocess. The Windows file-dialog chain is exercised through a fake user32.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bridge
import engine.base as base
import engine.server as server
from engine.base import EndpointDriver
from engine.uia import Snapshot, UINode, assign_refs
from engine.window import WindowInfo


def _snap(nodes: list[UINode], snap_id: str = "s1") -> Snapshot:
    return Snapshot(
        snapshot_id=snap_id, hwnd=101, title="T", url="", nodes=nodes,
        refs=assign_refs(nodes, snap_id), window_rect=(0, 0, 1000, 800),
    )


class _FakeUser32:
    """Minimal user32 surface: no SetForegroundWindow, no clipboard calls."""

    def __init__(self, pid: int = 4242, suppress_direct: bool = False):
        self.pid = pid
        self.suppress_direct = suppress_direct
        self.messages: list[tuple[int, int, object]] = []
        self.closed = False

    def FindWindowExW(self, parent, after, cls, name):
        if parent is None:
            return 100 if not after else 0
        if cls == "ComboBoxEx32":
            return 101
        if cls == "ComboBox":
            return 102
        if cls == "Edit":
            return 1148
        return 0

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        pid_ref.value = self.pid
        return 1

    def GetDlgItem(self, hwnd, ctrl_id):
        if ctrl_id == base.FILE_NAME_CONTROL_ID and not self.suppress_direct:
            return 1148
        if ctrl_id == base.OPEN_BUTTON_CONTROL_ID:
            return 2001
        return 0

    def SendMessageW(self, hwnd, msg, wparam, lparam):
        self.messages.append((int(hwnd), int(msg), lparam))
        if int(hwnd) == 2001:
            self.closed = True
        return 0

    def IsWindow(self, hwnd):
        return 0 if self.closed else 1


class _StubDriver:
    def __init__(self, ep: str):
        self.ep = ep
        self.last_attach = None

    def ask(self, prompt: str, **kwargs):
        self.last_attach = kwargs.get("attach")
        return {"ok": True, "endpoint": self.ep, "reply": "ok"}


class TestAttachValidation(unittest.TestCase):
    def test_missing_file_is_rejected_before_any_ui(self):
        with self.assertRaises(FileNotFoundError):
            bridge.normalize_attach_paths(["missing_attach_file.txt"])
        with self.assertRaises(FileNotFoundError):
            server.normalize_request_attach("missing_attach_file.txt")

        created: dict[str, _StubDriver] = {}

        def factory(ep: str):
            created[ep] = _StubDriver(ep)
            return created[ep]

        rc = bridge.main(
            ["ask", "gemini", "hi", "--attach", "missing_attach_file.txt", "--json"],
            driver_factory=factory,
        )
        self.assertEqual(rc, 3)
        self.assertFalse(created)

    def test_cli_and_http_normalize_and_forward_attach_paths(self):
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "a.txt"
            f1.write_text("a", encoding="utf-8")
            original_cwd = os.getcwd()
            os.chdir(td)
            try:
                created: dict[str, _StubDriver] = {}

                def factory(ep: str):
                    created[ep] = _StubDriver(ep)
                    return created[ep]

                rc = bridge.main(
                    ["ask", "gemini", "hi", "--attach", "a.txt", "--json"],
                    driver_factory=factory,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(created["gemini"].last_attach, [str(f1)])

                forwarded: list[list[str] | None] = []

                def dispatcher(action: str, endpoint: str, opts: dict):
                    forwarded.append(opts.get("attach"))
                    return {"ok": True, "endpoint": endpoint, "reply": "ok"}

                srv = server.create_server(
                    dispatcher=dispatcher, endpoint_names=["gemini"],
                    host="127.0.0.1", port=0,
                )
                thread = threading.Thread(target=srv.serve_forever, daemon=True)
                thread.start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{srv.server_address[1]}/ask",
                        data=json.dumps({
                            "endpoint": "gemini", "prompt": "check",
                            "files": ["a.txt"],
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertEqual(resp.status, 200)
                finally:
                    srv.shutdown()
                    srv.server_close()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(forwarded, [[str(f1)]])


class TestWindowsFileDialogPrimitive(unittest.TestCase):
    def test_find_fill_and_confirm_32770_dialog(self):
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "one.txt"
            f2 = Path(td) / "two.txt"
            f1.write_text("1", encoding="utf-8")
            f2.write_text("2", encoding="utf-8")
            fake_ctypes = SimpleNamespace(
                c_ulong=lambda value=0: SimpleNamespace(value=value),
                byref=lambda holder: holder,
                c_wchar_p=lambda text: text,
            )
            fake_user32 = _FakeUser32()
            with mock.patch.object(base, "ctypes", fake_ctypes), \
                    mock.patch.object(base.time, "sleep"), \
                    mock.patch.object(
                        base, "_win32_user32",
                        side_effect=AssertionError("real user32 must not be used"),
                    ):
                method = base.fill_windows_file_dialog(
                    [str(f1), str(f2)], owner_pid=4242,
                    timeout=1, poll=0, user32=fake_user32,
                )

        self.assertEqual(method, "#32770:file_dialog")
        text_msgs = [m for m in fake_user32.messages if m[1] == base.WM_SETTEXT]
        click_msgs = [m for m in fake_user32.messages if m[1] == base.BM_CLICK]
        self.assertEqual(len(text_msgs), 1)
        self.assertEqual(text_msgs[0][0], base.FILE_NAME_CONTROL_ID)
        self.assertEqual(text_msgs[0][2], '"%s" "%s"' % (str(f1), str(f2)))
        self.assertEqual(len(click_msgs), 1)
        self.assertEqual(click_msgs[0][0], 2001)
        self.assertTrue(fake_user32.closed)

        chain_fake = _FakeUser32(suppress_direct=True)
        self.assertEqual(base._dialog_edit_hwnd(chain_fake, 100), 1148)


class TestAttachHook(unittest.TestCase):
    def test_endpoint_without_attach_button_fails_fast(self):
        win = WindowInfo(hwnd=101, pid=1, process_name="hermes.exe", title="Hermes")
        snap = _snap([UINode(role="EditControl", name="消息", rect=(100, 700, 400, 40))])
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.txt"
            f.write_text("x", encoding="utf-8")
            drv = EndpointDriver("hermes")
            with self.assertRaisesRegex(NotImplementedError, "no attach_button"):
                drv.attach_files(win, snap, [str(f)], headed=False)

            looked_up: list[int] = []
            drv2 = EndpointDriver(
                "hermes",
                window_fn=lambda: (looked_up.append(1) or win),
                snapshot_fn=lambda _h: snap,
            )
            with self.assertRaisesRegex(NotImplementedError, "no attach_button"):
                drv2.ask("x", attach=[str(f)], force=True, headed=False)
            self.assertFalse(looked_up)


if __name__ == "__main__":
    unittest.main()
