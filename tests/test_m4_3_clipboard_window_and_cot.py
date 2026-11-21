"""Offline coverage for M4-3: clipboard plan A, icebreaker, focus guard,
DeepSeek CoT stripping and secondary-screen tiling.

No real clipboard, window or keystroke is touched: ``set_clipboard`` /
``send_hotkey`` / ``_physical_click`` / the SetWindowPos call and the
foreground-window probe are all injected or mocked. The tiling assertions pin
the exact coordinates and the ``SWP_NOACTIVATE`` flag so a future refactor
cannot silently start activating windows.
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

import engine.uia as uia  # noqa: E402
import engine.window as win_mod  # noqa: E402
from endpoints import get_driver  # noqa: E402
from endpoints.deepseek import strip_thinking_sections  # noqa: E402
from engine.uia import Snapshot, UINode, assign_refs  # noqa: E402

SECONDARY = (-1440, -653, 0, 1907)


def _blank_snap() -> Snapshot:
    nodes = [UINode(role="EditControl", name="Ask Gemini", rect=(10, 700, 500, 40))]
    return Snapshot(
        snapshot_id="blank",
        hwnd=101,
        title="Gemini",
        url="",
        nodes=nodes,
        refs=assign_refs(nodes, "blank"),
        window_rect=(-1400, -500, 1432, 850),
    )


class ClipboardPlanATests(unittest.TestCase):
    def test_success_path_restores_the_original_text(self):
        store = {"clip": "用户原本的剪贴板"}

        def write(text):
            store["clip"] = text

        with uia.preserved_clipboard(
            read_fn=lambda: store["clip"],
            has_text_fn=lambda: True,
            is_empty_fn=lambda: False,
            write_fn=write,
        ) as backup:
            self.assertTrue(backup["restorable"])
            self.assertEqual(backup["text"], "用户原本的剪贴板")
            store["clip"] = "自动化粘贴的内容"

        self.assertEqual(store["clip"], "用户原本的剪贴板")

    def test_exception_path_still_restores(self):
        store = {"clip": "重要内容"}

        def write(text):
            store["clip"] = text

        with self.assertRaises(RuntimeError):
            with uia.preserved_clipboard(
                read_fn=lambda: store["clip"],
                has_text_fn=lambda: True,
                is_empty_fn=lambda: False,
                write_fn=write,
            ):
                store["clip"] = "写了一半"
                raise RuntimeError("paste blew up")

        self.assertEqual(store["clip"], "重要内容")

    def test_empty_clipboard_is_restorable_to_empty(self):
        backup = uia.backup_clipboard(
            read_fn=lambda: "", has_text_fn=lambda: False, is_empty_fn=lambda: True
        )
        self.assertTrue(backup["restorable"])
        self.assertTrue(backup["empty"])
        written: list[str] = []
        self.assertTrue(uia.restore_clipboard(backup, write_fn=written.append))
        self.assertEqual(written, [""])

    def test_non_text_clipboard_is_reported_not_wiped(self):
        backup = uia.backup_clipboard(
            read_fn=lambda: "", has_text_fn=lambda: False, is_empty_fn=lambda: False
        )
        self.assertFalse(backup["restorable"])
        self.assertEqual(backup["reason"], "non_text_content")
        written: list[str] = []
        self.assertFalse(uia.restore_clipboard(backup, write_fn=written.append))
        self.assertEqual(written, [], "an image on the clipboard must survive")

    def test_heading_paste_backs_up_before_and_restores_after(self):
        events: list[tuple] = []
        node = UINode(role="EditControl", name="composer", rect=(100, 100, 200, 20))
        with mock.patch.object(
            uia, "backup_clipboard",
            side_effect=lambda **kw: events.append(("backup",)) or {"restorable": True, "text": "orig"},
        ), mock.patch.object(
            uia, "restore_clipboard",
            side_effect=lambda b, **kw: events.append(("restore", b.get("text"))),
        ), mock.patch.object(
            uia, "set_clipboard", side_effect=lambda t: events.append(("set", t)),
        ), mock.patch.object(
            uia, "send_hotkey", side_effect=lambda *k: events.append(("hotkey",) + k),
        ), mock.patch.object(uia, "_physical_click", lambda *a: None), \
                mock.patch.object(uia.time, "sleep", lambda *_: None):
            uia._headed_paste_text(node, "hello world")

        self.assertEqual(events[-1], ("restore", "orig"))
        self.assertIn(("set", "hello world"), events)
        self.assertIn(("hotkey", "ctrl", "v"), events)
        self.assertLess(
            events.index(("backup",)),
            events.index(("set", "hello world")),
            "the backup must happen before the first clipboard write",
        )
        self.assertLess(
            events.index(("set", "hello world")),
            events.index(("restore", "orig")),
        )

    def test_ratio_paste_also_restores(self):
        events: list[tuple] = []
        with mock.patch.object(
            uia, "backup_clipboard",
            side_effect=lambda **kw: events.append(("backup",)) or {"restorable": True, "text": "orig"},
        ), mock.patch.object(
            uia, "restore_clipboard",
            side_effect=lambda b, **kw: events.append(("restore", b.get("text"))),
        ), mock.patch.object(
            uia, "set_clipboard", side_effect=lambda t: events.append(("set", t)),
        ), mock.patch.object(
            uia, "send_hotkey", side_effect=lambda *k: events.append(("hotkey",) + k),
        ), mock.patch.object(uia, "_physical_click", lambda *a: None), \
                mock.patch.object(uia.time, "sleep", lambda *_: None):
            uia.click_ratio_and_paste((0, 0, 1000, 800), (0.5, 0.5), "ratio text")

        self.assertEqual(events[-1], ("restore", "orig"))
        self.assertIn(("hotkey", "enter"), events)
        self.assertLess(
            events.index(("backup",)), events.index(("set", "ratio text"))
        )


class IcebreakerTests(unittest.TestCase):
    def _run(self, *, icebreaker: bool) -> list[tuple]:
        events: list[tuple] = []
        node = UINode(role="EditControl", name="composer", rect=(100, 100, 200, 20))
        with mock.patch.object(
            uia, "backup_clipboard",
            side_effect=lambda **kw: {"restorable": True, "text": ""},
        ), mock.patch.object(
            uia, "restore_clipboard", side_effect=lambda b, **kw: None,
        ), mock.patch.object(
            uia, "set_clipboard", side_effect=lambda t: events.append(("set", t)),
        ), mock.patch.object(
            uia, "send_hotkey", side_effect=lambda *k: events.append(("hotkey",) + k),
        ), mock.patch.object(uia, "_physical_click", lambda *a: None), \
                mock.patch.object(uia.time, "sleep", lambda *_: None):
            uia._headed_paste_text(node, "x", icebreaker=icebreaker)
        return events

    def test_space_and_backspace_precede_the_paste(self):
        events = self._run(icebreaker=True)
        space = events.index(("hotkey", "space"))
        backspace = events.index(("hotkey", "backspace"))
        paste = events.index(("hotkey", "ctrl", "v"))
        self.assertLess(space, backspace)
        self.assertLess(backspace, paste)

    def test_icebreaker_is_opt_in(self):
        events = self._run(icebreaker=False)
        self.assertNotIn(("hotkey", "space"), events)
        self.assertNotIn(("hotkey", "backspace"), events)
        self.assertIn(("hotkey", "ctrl", "v"), events)

    def test_set_node_text_forwards_the_flag(self):
        node = UINode(role="EditControl", name="composer", rect=(1, 1, 10, 10))
        with mock.patch.object(uia, "_headed_paste_text") as paste:
            uia.set_node_text(
                node, "text", headed=True, prefer_paste=True, icebreaker=True
            )
        self.assertTrue(paste.call_args.kwargs["icebreaker"])


class FocusGuardTests(unittest.TestCase):
    _PRIMARY = (100, 100, 900, 700)
    _SECONDARY_A = (-1300, 100, -300, 700)
    _SECONDARY_B = (-1200, 700, -200, 1200)

    def _guard(self, before: int, after: int, *, strict: bool = False) -> win_mod.FocusGuard:
        rects = {
            0x1111: self._PRIMARY,
            0x2222: self._SECONDARY_A,
            0x3333: self._SECONDARY_B,
        }
        seq = [before, after]
        return win_mod.FocusGuard(
            strict=strict,
            fg_fn=lambda: seq.pop(0) if seq else after,
            rect_fn=lambda hwnd: rects.get(hwnd),
        )

    def test_primary_focus_lost_to_the_automation_screen_is_recorded(self):
        guard = self._guard(0x1111, 0x2222)
        with guard:
            pass
        self.assertTrue(guard.taken)
        self.assertEqual(
            guard.warnings(), ["focus_guard:primary_lost:0x1111->0x2222"]
        )

    def test_strict_guard_raises(self):
        guard = self._guard(0x1111, 0x2222, strict=True)
        with self.assertRaises(win_mod.FocusTakenError):
            with guard:
                pass

    def test_same_window_is_not_a_takeover(self):
        guard = self._guard(0x1111, 0x1111)
        with guard:
            pass
        self.assertFalse(guard.taken)

    def test_secondary_to_secondary_is_not_a_takeover(self):
        guard = self._guard(0x2222, 0x3333)
        with guard:
            pass
        self.assertFalse(guard.taken)

    def test_is_on_primary_screen_uses_the_horizontal_centre(self):
        self.assertTrue(win_mod.is_on_primary_screen(self._PRIMARY))
        self.assertFalse(win_mod.is_on_primary_screen(self._SECONDARY_A))
        self.assertFalse(win_mod.is_on_primary_screen(None))

    def test_guarded_write_surfaces_the_event_without_blocking_by_default(self):
        from engine.base import EndpointDriver

        drv = EndpointDriver("gemini", config={}, bridge_config={"headless_default": True})
        rects = {0x1111: self._PRIMARY, 0x2222: self._SECONDARY_A}
        seq = [0x1111, 0x2222]
        warnings: list[str] = []
        with mock.patch.object(
            win_mod, "foreground_hwnd", side_effect=lambda: seq.pop(0) if seq else 0x2222
        ), mock.patch.object(
            win_mod, "window_rect", side_effect=lambda hwnd: rects.get(hwnd)
        ):
            drv.guarded_write(lambda: None, warnings=warnings)

        self.assertEqual(warnings, ["focus_guard:primary_lost:0x1111->0x2222"])
        self.assertEqual(drv.last_focus_event, "focus_guard:primary_lost:0x1111->0x2222")
        self.assertFalse(drv.focus_guard_strict())

    def test_strict_config_blocks_the_ask_before_submit(self):
        """focus_guard_strict=True must refuse to send after stealing focus."""
        import engine.base as base_mod
        from engine.base import EndpointDriver
        from engine.config import get_endpoint_config

        cfg = dict(get_endpoint_config("gemini"))
        cfg["focus_guard_strict"] = True
        rects = {0x1111: self._PRIMARY, 0x2222: self._SECONDARY_A}
        seq = [0x1111, 0x2222]
        with tempfile.TemporaryDirectory() as tmp:
            drv = EndpointDriver(
                "gemini",
                config=cfg,
                bridge_config={"headless_default": False, "default_timeout": 5.0,
                               "lock_timeout": 5.0, "poll_interval": 0.0},
                window_fn=lambda: win_mod.WindowInfo(
                    hwnd=101, pid=4242, process_name="chrome.exe", title="Gemini"
                ),
                snapshot_fn=lambda hwnd: _blank_snap(),
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=Path(tmp) / "fail",
                lock_path=Path(tmp) / "ui.lock",
                sleep_fn=lambda _s: None,
            )
            with mock.patch.object(base_mod, "focus_window", return_value=True), \
                    mock.patch.object(
                        win_mod, "foreground_hwnd",
                        side_effect=lambda: seq.pop(0) if seq else 0x2222,
                    ), mock.patch.object(
                        win_mod, "window_rect",
                        side_effect=lambda hwnd: rects.get(hwnd),
                    ):
                res = drv.ask("只回复：收到", timeout=5)
            entry = drv.ledger.get("只回复：收到", "gemini")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "focus_taken")
        self.assertIn("focus_guard:primary_lost", res["focus_event"])
        self.assertEqual(entry["state"], "failed_retryable")
        self.assertTrue(drv.ledger.check("只回复：收到", "gemini")[0])


class SecondaryTileTests(unittest.TestCase):
    def test_tiles_vertically_inside_the_secondary_bounds(self):
        calls: list[tuple] = []
        results = win_mod.tile_windows_on_secondary_screen(
            [11, 22, 33],
            bounds=SECONDARY,
            set_pos_fn=lambda *a: calls.append(a) or True,
        )

        self.assertEqual([c[1] for c in calls], [-1440, -1440, -1440])
        self.assertEqual([c[2] for c in calls], [-653, 200, 1054])
        self.assertEqual([c[3] for c in calls], [1440, 1440, 1440])
        self.assertEqual(sum(c[4] for c in calls), 2560)
        self.assertTrue(all(r["ok"] for r in results))

    def test_flags_never_activate_or_reorder(self):
        calls: list[tuple] = []
        win_mod.tile_windows_on_secondary_screen(
            [11], bounds=SECONDARY, set_pos_fn=lambda *a: calls.append(a) or True
        )
        flags = calls[0][5]
        self.assertEqual(flags, win_mod.SWP_NOACTIVATE | win_mod.SWP_NOZORDER)
        self.assertTrue(flags & win_mod.SWP_NOACTIVATE)
        self.assertFalse(flags & win_mod.SWP_SHOWWINDOW)

    def test_no_window_is_placed_at_a_positive_origin(self):
        calls: list[tuple] = []
        win_mod.tile_windows_on_secondary_screen(
            [11, 22], bounds=SECONDARY, set_pos_fn=lambda *a: calls.append(a) or True
        )
        for call in calls:
            self.assertLess(call[1], 0, "x must stay on the negative-x screen")
            self.assertLessEqual(call[1] + call[3], 0)

    def test_empty_and_invalid_handles_are_ignored(self):
        calls: list[tuple] = []
        results = win_mod.tile_windows_on_secondary_screen(
            [], bounds=SECONDARY, set_pos_fn=lambda *a: calls.append(a) or True
        )
        self.assertEqual(results, [])
        self.assertEqual(calls, [])
        results = win_mod.tile_windows_on_secondary_screen(
            [0, -5], bounds=SECONDARY, set_pos_fn=lambda *a: calls.append(a) or True
        )
        self.assertEqual(results, [])

    def test_default_bounds_come_from_the_monitor_not_a_positive_origin(self):
        with mock.patch.object(
            win_mod, "secondary_screen_bounds", return_value=(-1440, -653, 0, 1907)
        ):
            calls: list[tuple] = []
            win_mod.tile_windows_on_secondary_screen(
                [11], set_pos_fn=lambda *a: calls.append(a) or True
            )
        self.assertEqual(calls[0][1], -1440)


class DeepSeekCotTests(unittest.TestCase):
    def test_two_paragraph_reasoning_block_is_dropped(self):
        lines = [
            "已思考（用时 12 秒）",
            "用户问的是接口设计，先分析约束。",
            "再考虑兼容性。",
            "",
            "建议用状态机收敛提交路径。",
        ]
        self.assertEqual(strip_thinking_sections(lines), ["建议用状态机收敛提交路径。"])

    def test_explicit_final_answer_marker_wins(self):
        lines = ["思考过程", "推理一", "推理二", "最终回答", "答案是 42"]
        self.assertEqual(strip_thinking_sections(lines), ["答案是 42"])

    def test_single_indistinguishable_blob_is_kept(self):
        lines = ["Thinking…", "只有这一段，无法判断是不是推理"]
        self.assertEqual(strip_thinking_sections(lines), ["只有这一段，无法判断是不是推理"])

    def test_lines_without_a_header_are_untouched(self):
        lines = ["普通回答第一段", "", "第二段"]
        self.assertEqual(strip_thinking_sections(lines), lines)

    def test_driver_strips_an_expanded_reasoning_block(self):
        drv = get_driver("deepseek")
        raw = [
            "只回复：收到-ds-001",
            "已思考（用时 5 秒）",
            "用户只要一个短确认。",
            "那就直接回确认语。",
            "",
            "收到-ds-001",
        ]
        cleaned = drv._extract_reply_lines(raw, prompt="只回复：收到-ds-001")
        self.assertEqual(cleaned, ["收到-ds-001"])

    def test_driver_keeps_the_answer_when_no_header_is_present(self):
        drv = get_driver("deepseek")
        cleaned = drv._extract_reply_lines(["收到", ""], prompt="只回复：收到")
        self.assertEqual(cleaned, ["收到"])


if __name__ == "__main__":
    unittest.main()
