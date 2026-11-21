"""Offline coverage for M4-4 focus option A and the full-page DocumentControl filter.

What is pinned here:

* ``preserved_focus`` -- borrow the foreground inside a block, hand the
  operator's primary-screen window back in ``finally``, and say honestly whether
  that worked (``restored`` / ``restore_failed``);
* ``FocusGuard(restore_on_exit=True)`` -- a restore that sticks is recorded as
  ``focus_guard:restored:0x..`` and never raises, a restore that does not stick
  is still a hard ``FocusTakenError`` under ``strict=True``;
* ``restore_focus`` -- a vanished handle is never poked with
  ``SetForegroundWindow``;
* ``is_fullpage_document_node`` -- the whole-page ``DocumentControl`` of a
  sleeping Chrome tab is not a composer, so ``status`` stops reporting
  ``ready=true`` for a backgrounded ``deepseek`` / ``grok`` tab;
* the driver -- submit borrows the foreground and gives it back, and a restore
  that fails after the click still surfaces as ``focus_taken``.

No real window, clipboard or keystroke is touched: every probe is injected or
mocked.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import engine.base as base_mod  # noqa: E402
import engine.window as win_mod  # noqa: E402
from engine.base import EndpointDriver, is_fullpage_document_node  # noqa: E402
from engine.config import get_endpoint_config  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402
from engine.window import WindowInfo  # noqa: E402

PRIMARY = (0, 0, 2560, 1440)
SECONDARY = (-1440, -653, 0, 1907)
PRIMARY_HWND = 0x1111
CHROME_HWND = 101

#: Measured on 2026-11-21: a backgrounded DeepSeek tab exposes exactly one node,
#: the whole-page container (window 1432x850 -> node 1416x721).
SLEEPING_DOC_RECT = (-1428, 324, 1416, 721)
DEEPSEEK_WIN_RECT = (-1436, 203, 1432, 850)


def _composer_snap(*, with_send: bool = True, lines: tuple[str, ...] = ()) -> Snapshot:
    nodes = [
        UINode(role="EditControl", name="输入 Google 提示", rect=(10, 700, 400, 60)),
    ]
    if with_send:
        nodes.append(UINode(role="ButtonControl", name="发送", rect=(420, 700, 40, 40)))
    for line in lines:
        nodes.append(UINode(role="TextControl", name=line))
    return Snapshot(
        snapshot_id="composer",
        hwnd=CHROME_HWND,
        title="Gemini",
        url="",
        nodes=nodes,
        refs=assign_refs(nodes, "composer"),
        window_rect=(-1436, 200, 1440, 850),
    )


class PreservedFocusTests(unittest.TestCase):
    def _fg(self, store):
        return lambda: store["fg"]

    def test_primary_window_is_put_back_after_the_block(self):
        store = {"fg": PRIMARY_HWND}
        restored: list[int] = []

        def put_back(hwnd):
            restored.append(hwnd)
            store["fg"] = hwnd
            return True

        with win_mod.preserved_focus(
            fg_fn=self._fg(store),
            rect_fn=lambda h: PRIMARY if h == PRIMARY_HWND else SECONDARY,
            restore_fn=put_back,
        ) as state:
            self.assertEqual(state["before"], PRIMARY_HWND)
            self.assertTrue(state["before_on_primary"])
            store["fg"] = CHROME_HWND          # automation borrowed the foreground

        self.assertEqual(restored, [PRIMARY_HWND])
        self.assertEqual(store["fg"], PRIMARY_HWND)
        self.assertTrue(state["restored"])
        self.assertFalse(state["restore_failed"])

    def test_failed_restore_is_reported_not_swallowed(self):
        store = {"fg": PRIMARY_HWND}

        with win_mod.preserved_focus(
            fg_fn=self._fg(store),
            rect_fn=lambda h: PRIMARY if h == PRIMARY_HWND else SECONDARY,
            restore_fn=lambda hwnd: False,
        ) as state:
            store["fg"] = CHROME_HWND

        self.assertFalse(state["restored"])
        self.assertTrue(state["restore_failed"])
        self.assertEqual(store["fg"], CHROME_HWND)

    def test_block_started_on_the_secondary_screen_restores_nothing(self):
        store = {"fg": CHROME_HWND}
        restored: list[int] = []

        with win_mod.preserved_focus(
            fg_fn=self._fg(store),
            rect_fn=lambda h: SECONDARY,
            restore_fn=lambda h: restored.append(h) or True,
        ) as state:
            store["fg"] = 0x2222

        self.assertEqual(restored, [])
        self.assertFalse(state["restored"])
        self.assertFalse(state["restore_failed"])

    def test_allow_restore_false_leaves_the_foreground_alone(self):
        store = {"fg": PRIMARY_HWND}
        restored: list[int] = []

        with win_mod.preserved_focus(
            allow_restore=False,
            fg_fn=self._fg(store),
            rect_fn=lambda h: PRIMARY,
            restore_fn=lambda h: restored.append(h) or True,
        ):
            store["fg"] = CHROME_HWND

        self.assertEqual(restored, [])

    def test_restore_focus_never_pokes_a_vanished_handle(self):
        calls: list[int] = []
        with mock.patch.object(win_mod, "foreground_hwnd", return_value=0x2222), \
                mock.patch.object(win_mod, "_is_window", return_value=False), \
                mock.patch.object(win_mod, "_set_foreground", side_effect=lambda h: calls.append(h) or True):
            self.assertFalse(win_mod.restore_focus(PRIMARY_HWND))
        self.assertEqual(calls, [], "SetForegroundWindow must not run for a dead handle")

    def test_restore_focus_confirms_the_result_instead_of_trusting_the_call(self):
        store = {"fg": 0x2222}

        def set_fg(hwnd):
            store["fg"] = hwnd
            return True

        with mock.patch.object(win_mod, "foreground_hwnd", side_effect=lambda: store["fg"]), \
                mock.patch.object(win_mod, "_is_window", return_value=True), \
                mock.patch.object(win_mod, "_set_foreground", side_effect=set_fg):
            self.assertTrue(win_mod.restore_focus(PRIMARY_HWND))
            self.assertEqual(store["fg"], PRIMARY_HWND)

        # A SetForegroundWindow that reports success but does nothing is a failure.
        with mock.patch.object(win_mod, "foreground_hwnd", return_value=0x2222), \
                mock.patch.object(win_mod, "_is_window", return_value=True), \
                mock.patch.object(win_mod, "_set_foreground", return_value=True):
            self.assertFalse(win_mod.restore_focus(PRIMARY_HWND))


class GuardRestoreTests(unittest.TestCase):
    def _guard(self, *, strict, restore_ok, restore_on_exit=True):
        store = {"fg": PRIMARY_HWND}
        restored: list[int] = []

        def put_back(hwnd):
            restored.append(hwnd)
            if restore_ok:
                store["fg"] = hwnd
            return restore_ok

        guard = win_mod.FocusGuard(
            strict=strict,
            restore_on_exit=restore_on_exit,
            fg_fn=lambda: store["fg"],
            rect_fn=lambda h: PRIMARY if h == PRIMARY_HWND else SECONDARY,
            restore_fn=put_back,
        )
        return guard, store, restored

    def test_restore_success_is_recorded_and_never_raises(self):
        guard, store, restored = self._guard(strict=True, restore_ok=True)
        with guard:
            store["fg"] = CHROME_HWND

        self.assertEqual(restored, [PRIMARY_HWND])
        self.assertEqual(store["fg"], PRIMARY_HWND)
        self.assertFalse(guard.taken)
        self.assertTrue(guard.restored)
        self.assertEqual(guard.warnings(), ["focus_guard:restored:0x1111"])

    def test_restore_failure_still_raises_under_strict(self):
        guard, store, restored = self._guard(strict=True, restore_ok=False)
        with self.assertRaises(win_mod.FocusTakenError):
            with guard:
                store["fg"] = CHROME_HWND
        self.assertEqual(restored, [PRIMARY_HWND])
        self.assertTrue(guard.taken)
        self.assertTrue(guard.restore_failed)
        self.assertIn("focus_guard:primary_lost:0x1111->0x65", guard.warnings())

    def test_guard_without_restore_keeps_the_legacy_diagnostic(self):
        guard, store, restored = self._guard(
            strict=False, restore_ok=True, restore_on_exit=False
        )
        with guard:
            store["fg"] = CHROME_HWND

        self.assertEqual(restored, [], "restore_on_exit=False must not touch focus")
        self.assertTrue(guard.taken)
        self.assertEqual(guard.warnings(), ["focus_guard:primary_lost:0x1111->0x65"])


class FullPageDocumentTests(unittest.TestCase):
    def test_sleeping_tab_container_is_a_fullpage_document(self):
        node = UINode(
            role="DocumentControl",
            name="El Psy Kongroo梗 - DeepSeek",
            rect=SLEEPING_DOC_RECT,
        )
        self.assertTrue(is_fullpage_document_node(node, DEEPSEEK_WIN_RECT))

    def test_bottom_composer_strip_is_not_a_fullpage_document(self):
        node = UINode(
            role="DocumentControl",
            name="输入你的问题",
            rect=(-1420, 980, 1400, 60),
        )
        self.assertFalse(is_fullpage_document_node(node, DEEPSEEK_WIN_RECT))

    def test_only_document_controls_can_be_fullpage(self):
        edit = UINode(role="EditControl", name="输入你的问题", rect=SLEEPING_DOC_RECT)
        self.assertFalse(is_fullpage_document_node(edit, DEEPSEEK_WIN_RECT))
        self.assertFalse(is_fullpage_document_node(None, DEEPSEEK_WIN_RECT))
        no_rect = UINode(role="DocumentControl", name="整页", rect=None)
        self.assertFalse(is_fullpage_document_node(no_rect, DEEPSEEK_WIN_RECT))
        self.assertFalse(
            is_fullpage_document_node(
                UINode(role="DocumentControl", name="整页", rect=SLEEPING_DOC_RECT), None
            ),
            "without a window rect the ratios are unknowable",
        )

    def test_status_stops_claiming_ready_for_a_sleeping_tab(self):
        sleeping = Snapshot(
            snapshot_id="sleep",
            hwnd=CHROME_HWND,
            title="El Psy Kongroo梗 - DeepSeek",
            url="",
            nodes=[UINode(
                role="DocumentControl",
                name="El Psy Kongroo梗 - DeepSeek",
                rect=SLEEPING_DOC_RECT,
            )],
            refs={},
            window_rect=DEEPSEEK_WIN_RECT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            drv = EndpointDriver(
                "deepseek",
                snapshot_fn=lambda _h: sleeping,
                window_fn=lambda: WindowInfo(
                    hwnd=CHROME_HWND, pid=1, process_name="chrome.exe",
                    title="El Psy Kongroo梗 - DeepSeek",
                ),
                sleep_fn=lambda _s: None,
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=Path(tmp) / "fail",
                lock_path=Path(tmp) / "ui.lock",
            )
            self.assertIsNone(drv._find_composer(sleeping))
            res = drv.status()

        self.assertFalse(res["ready"], "a whole-page DocumentControl is not a composer")

    def test_a_real_composer_next_to_the_page_container_wins(self):
        nodes = [
            UINode(
                role="DocumentControl",
                name="El Psy Kongroo梗 - DeepSeek",
                rect=SLEEPING_DOC_RECT,
            ),
            UINode(role="DocumentControl", name="输入你的问题", rect=(-1420, 980, 1400, 60)),
        ]
        snap = Snapshot(
            snapshot_id="both",
            hwnd=CHROME_HWND,
            title="DeepSeek",
            url="",
            nodes=nodes,
            refs=assign_refs(nodes, "both"),
            window_rect=DEEPSEEK_WIN_RECT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            drv = EndpointDriver(
                "deepseek",
                snapshot_fn=lambda _h: snap,
                window_fn=lambda: WindowInfo(hwnd=CHROME_HWND, pid=1, process_name="chrome.exe", title="DeepSeek"),
                sleep_fn=lambda _s: None,
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=Path(tmp) / "fail",
                lock_path=Path(tmp) / "ui.lock",
            )
            composer = drv._find_composer(snap)
            res = drv.status()

        self.assertIsNotNone(composer)
        self.assertEqual(composer.name, "输入你的问题")
        self.assertTrue(res["ready"])


class AttachMenuWaitTests(unittest.TestCase):
    def test_raise_window_lifts_without_activating(self):
        calls: list[tuple] = []
        with mock.patch.object(
            win_mod, "_set_window_pos", side_effect=lambda *a: calls.append(a) or True
        ):
            self.assertTrue(win_mod.raise_window(0x2222))
        self.assertEqual(len(calls), 1)
        hwnd, x, y, w, h, flags = calls[0]
        self.assertEqual(hwnd, 0x2222)
        self.assertEqual((x, y, w, h), (0, 0, 0, 0))
        self.assertEqual(flags, win_mod.SWP_NOMOVE | win_mod.SWP_NOSIZE | win_mod.SWP_NOACTIVATE)
        self.assertFalse(flags & win_mod.SWP_SHOWWINDOW, "raising must not touch visibility")

    def test_menu_is_polled_until_it_renders(self):
        without = _composer_snap()
        nodes = [
            UINode(role="EditControl", name="输入 Google 提示", rect=(10, 700, 400, 60)),
            UINode(role="MenuItemControl", name="上传文件. 文档、数据、代码文件", rect=(-1008, 1634, 233, 36)),
        ]
        with_item = Snapshot(
            snapshot_id="menu", hwnd=CHROME_HWND, title="Gemini", url="",
            nodes=nodes, refs=assign_refs(nodes, "menu"),
            window_rect=(-1448, -661, 1456, 2536),
        )
        seq = [without, without, with_item]
        state = {"i": 0}

        def next_snap(_h):
            index = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return seq[index]

        with tempfile.TemporaryDirectory() as tmp:
            drv = EndpointDriver(
                "gemini",
                config=dict(get_endpoint_config("gemini")),
                bridge_config={"attach_menu_poll": 0.0},
                snapshot_fn=next_snap,
                sleep_fn=lambda _s: None,
                ledger_path=Path(tmp) / "ledger.json",
                lock_path=Path(tmp) / "ui.lock",
            )
            item, snap = drv._wait_attach_menu(
                WindowInfo(hwnd=CHROME_HWND, pid=1, process_name="chrome.exe", title="Gemini")
            )

        self.assertIsNotNone(item)
        self.assertIn("上传文件", item.name)
        self.assertEqual(snap.snapshot_id, "menu")
        self.assertEqual(state["i"], 3, "the fixed sleep would have given up after one snapshot")


class ForeignDialogTests(unittest.TestCase):
    """Gemini's picker is a #32770 owned by a *different* chrome.exe pid."""

    WIN = WindowInfo(hwnd=CHROME_HWND, pid=30812, process_name="chrome.exe", title="Gemini")

    def _driver(self, tmp: str):
        return EndpointDriver(
            "gemini",
            config=dict(get_endpoint_config("gemini")),
            bridge_config={"attach_dialog_poll": 0.0},
            snapshot_fn=lambda _h: _composer_snap(),
            sleep_fn=lambda _s: None,
            ledger_path=Path(tmp) / "ledger.json",
            lock_path=Path(tmp) / "ui.lock",
        )

    def test_sibling_chrome_dialog_is_accepted_and_reported(self):
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(base_mod, "list_file_dialogs", return_value=[(4242, 39744)]), \
                    mock.patch.object(win_mod, "process_image_name", return_value="chrome.exe"):
                hwnd = drv._new_file_dialog(self.WIN, {111}, warnings)
        self.assertEqual(hwnd, 4242)
        self.assertEqual(warnings, ["attach_dialog_foreign_pid:39744"])

    def test_other_applications_dialog_is_refused(self):
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(base_mod, "list_file_dialogs", return_value=[(4242, 18776)]), \
                    mock.patch.object(win_mod, "process_image_name", return_value="banktool.exe"):
                hwnd = drv._new_file_dialog(self.WIN, {111}, warnings, timeout=0.0)
        self.assertEqual(hwnd, 0)
        self.assertEqual(warnings, [])

    def test_pre_existing_dialog_is_not_mistaken_for_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(base_mod, "list_file_dialogs", return_value=[(4242, 39744)]), \
                    mock.patch.object(win_mod, "process_image_name", return_value="chrome.exe"):
                hwnd = drv._new_file_dialog(self.WIN, {4242}, None, timeout=0.0)
        self.assertEqual(hwnd, 0)

    def test_pid_miss_falls_back_to_the_sibling_dialog(self):
        from engine.uia import UIAError

        filled: list[tuple] = []
        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(
                base_mod, "fill_windows_file_dialog",
                side_effect=UIAError("DIALOG_NOT_FOUND", "no dialog for pid 30812"),
            ), mock.patch.object(base_mod, "list_file_dialogs", return_value=[(4242, 39744)]), \
                    mock.patch.object(win_mod, "process_image_name", return_value="chrome.exe"), \
                    mock.patch.object(
                        base_mod, "fill_file_dialog",
                        side_effect=lambda hwnd, paths, **kw: filled.append((hwnd, paths)) or "#32770:file_dialog",
                    ):
                method = drv._fill_raised_file_dialog(self.WIN, ["a.txt"], set())

        self.assertEqual(method, "#32770:file_dialog")
        self.assertEqual(filled, [(4242, ["a.txt"])])


class CopyReplyClipboardTests(unittest.TestCase):
    """Reading the reply via 复制 is a clipboard write; it must not leak."""

    def test_copy_button_payload_is_read_inside_preserved_clipboard(self):
        from contextlib import contextmanager

        events: list[str] = []

        @contextmanager
        def fake_preserved():
            events.append("backup")
            try:
                yield {"restorable": True, "text": "用户原本的剪贴板"}
            finally:
                events.append("restore")

        seq = [1, 2]
        nodes = [UINode(role="ButtonControl", name="复制", rect=(300, 500, 30, 30))]
        snap = Snapshot(
            snapshot_id="copy", hwnd=CHROME_HWND, title="Gemini", url="",
            nodes=nodes, refs=assign_refs(nodes, "copy"),
            window_rect=(-1436, 200, 1440, 850),
        )
        with tempfile.TemporaryDirectory() as tmp:
            drv = EndpointDriver(
                "gemini",
                config=dict(get_endpoint_config("gemini")),
                bridge_config={"poll_interval": 0.0},
                snapshot_fn=lambda _h: snap,
                sleep_fn=lambda _s: None,
                ledger_path=Path(tmp) / "ledger.json",
                lock_path=Path(tmp) / "ui.lock",
            )
            with mock.patch.object(base_mod, "preserved_clipboard", fake_preserved), \
                    mock.patch.object(
                        base_mod, "invoke_node",
                        side_effect=lambda _n, headed=False: events.append("click") or "stub:click",
                    ), \
                    mock.patch.object(base_mod, "get_clipboard_sequence", side_effect=lambda: seq.pop(0) if seq else 2), \
                    mock.patch.object(base_mod, "get_clipboard", return_value="OK"):
                reply = drv._try_copy_button_reply(
                    snap,
                    WindowInfo(hwnd=CHROME_HWND, pid=1, process_name="chrome.exe", title="Gemini"),
                    headed=True,
                )

        self.assertEqual(reply, "OK")
        self.assertEqual(events[0], "backup", "back up before the copy click")
        self.assertEqual(events[-1], "restore", "put the operator's text back")
        self.assertIn("click", events)


class FocusBorrowTests(unittest.TestCase):
    def test_borrow_restore_is_on_by_default_and_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_drv = EndpointDriver(
                "gemini", config={}, bridge_config={}, sleep_fn=lambda _s: None,
                ledger_path=Path(tmp) / "ledger.json",
            )
            self.assertTrue(default_drv.focus_borrow_restore())

            off = EndpointDriver(
                "gemini", config={"focus_borrow_restore": False}, bridge_config={},
                sleep_fn=lambda _s: None, ledger_path=Path(tmp) / "l2.json",
            )
            self.assertFalse(off.focus_borrow_restore())

            off_bridge = EndpointDriver(
                "gemini", config={}, bridge_config={"focus_borrow_restore": False},
                sleep_fn=lambda _s: None, ledger_path=Path(tmp) / "l3.json",
            )
            self.assertFalse(off_bridge.focus_borrow_restore())

    def _driver(self, snaps, tmp: str, **extra):
        state = {"i": 0}

        def next_snap(_h):
            index = min(state["i"], len(snaps) - 1)
            state["i"] += 1
            return snaps[index]

        return EndpointDriver(
            "gemini",
            config=dict(get_endpoint_config("gemini")),
            bridge_config={"poll_interval": 0.0, "stable_seconds": 0.0, "default_timeout": 5.0},
            snapshot_fn=next_snap,
            window_fn=lambda: WindowInfo(hwnd=CHROME_HWND, pid=4242, process_name="chrome.exe", title="Gemini"),
            invoke_fn=lambda _n, _h: "stub:click",
            write_fn=lambda _n, _t, _h, _c: "stub:write",
            sleep_fn=lambda _s: None,
            ledger_path=Path(tmp) / "ledger.json",
            evidence_dir=Path(tmp) / "fail",
            lock_path=Path(tmp) / "ui.lock",
            **extra,
        )

    def test_submit_borrows_the_foreground_and_gives_it_back(self):
        snaps = [
            _composer_snap(),
            _composer_snap(),
            _composer_snap(lines=("收到",)),
            _composer_snap(lines=("收到",)),
        ]
        store = {"fg": PRIMARY_HWND}
        borrowed: list[int] = []

        def activate(hwnd):
            borrowed.append(hwnd)
            store["fg"] = int(hwnd)
            return True

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(snaps, tmp)
            with mock.patch.object(base_mod, "focus_window", side_effect=activate), \
                    mock.patch.object(win_mod, "foreground_hwnd", side_effect=lambda: store["fg"]), \
                    mock.patch.object(win_mod, "window_rect", side_effect=lambda h: PRIMARY if h == PRIMARY_HWND else SECONDARY), \
                    mock.patch.object(
                        win_mod, "restore_focus",
                        side_effect=lambda h: store.__setitem__("fg", int(h)) or True,
                    ):
                res = drv.ask("只回复：收到", headed=True, timeout=5, force=True)

        self.assertTrue(res["ok"], res.get("message"))
        self.assertIn(CHROME_HWND, borrowed, "the submit needs the window foreground")
        self.assertEqual(store["fg"], PRIMARY_HWND, "the operator's window must get focus back")
        self.assertTrue(
            any(w.startswith("focus_guard:restored:0x1111") for w in res["warnings"]),
            res["warnings"],
        )
        self.assertNotEqual(res.get("error"), "focus_taken")

    def test_failed_submit_restore_still_reports_focus_taken(self):
        snaps = [_composer_snap(), _composer_snap(), _composer_snap(lines=("收到",))]
        store = {"fg": PRIMARY_HWND}

        def activate(hwnd):
            store["fg"] = int(hwnd)
            return True

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(snaps, tmp)
            with mock.patch.object(base_mod, "focus_window", side_effect=activate), \
                    mock.patch.object(win_mod, "foreground_hwnd", side_effect=lambda: store["fg"]), \
                    mock.patch.object(win_mod, "window_rect", side_effect=lambda h: PRIMARY if h == PRIMARY_HWND else SECONDARY), \
                    mock.patch.object(win_mod, "restore_focus", return_value=False):
                res = drv.ask("只回复：收到", headed=True, timeout=5, force=True)
            entry = drv.ledger.get("只回复：收到", "gemini")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "focus_taken")
        self.assertTrue(res["submitted"])
        self.assertEqual(res["reply"], "")
        # The message is already out: never let the ledger invite an auto resend.
        self.assertEqual(entry["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
