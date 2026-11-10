
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import unittest
from unittest import mock

from qi import audio, browser, ui
import test_cardweb as fixtures


class OwnedSpawnCleanup(unittest.TestCase):
    def fixture(self, stack):
        stack.enter_context(mock.patch.object(browser, "shell32")) .ShellExecuteW.return_value = 42
        stack.enter_context(mock.patch.object(browser, "_chrome_hwnds", side_effect=[{1}, {1, 2}]))
        stack.enter_context(mock.patch.object(browser, "time", fixtures.FakeClock()))
        # T1-1/F2（2026-11-07 独立复核）：现在**只有 nonce 证过归属**的窗口才允许关。
        # 本组用例要测的正是"我们自己开的窗口失败时必须收尾"，所以显式给 nonce 并让取证通过；
        # "弱证据不许关"由 test_cardweb / test_b4_spawn_attribution 覆盖。
        stack.enter_context(mock.patch.object(browser, "_spawn_proves_nonce", return_value=True))
        place = stack.enter_context(mock.patch.object(browser, "place"))
        focus = stack.enter_context(mock.patch.object(browser, "_focus", return_value=True))
        ready = stack.enter_context(mock.patch.object(browser, "_wait_ready", return_value=True))
        close = stack.enter_context(mock.patch.object(browser, "close"))
        return place, focus, ready, close

    def test_each_exception_after_creation_cleans_only_the_created_window(self):
        for stage in range(3):
            with self.subTest(stage=stage), contextlib.ExitStack() as stack:
                functions = self.fixture(stack)
                error = RuntimeError("fixture failure")
                functions[stage].side_effect = error
                with self.assertRaises(RuntimeError) as caught:
                    browser._spawn("https://example.invalid/", (0, 0), (100, 100), 1, False,
                                   nonce="n1")
                self.assertIs(caught.exception, error)
                functions[-1].assert_called_once_with(2)

    def test_cleanup_exception_does_not_replace_original_failure(self):
        with contextlib.ExitStack() as stack:
            place, _, _, close = self.fixture(stack)
            error = RuntimeError("original")
            place.side_effect = error
            close.side_effect = OSError("cleanup")
            with self.assertRaises(RuntimeError) as caught:
                browser._spawn("https://example.invalid/", (0, 0), (100, 100), 1, False,
                               nonce="n1")
            self.assertIs(caught.exception, error)

    def test_success_keeps_created_window_and_focus_failure_cleans(self):
        for success in (True, False):
            with self.subTest(success=success), contextlib.ExitStack() as stack:
                _, focus, _, close = self.fixture(stack)
                focus.return_value = success
                value = browser._spawn("https://example.invalid/", (0, 0), (100, 100), 1, False,
                                       nonce="n1")
                self.assertEqual(value, 2 if success else None)
                self.assertEqual(close.call_count, 0 if success else 1)

    def test_ambiguous_concurrent_new_windows_are_not_guessed_or_closed(self):
        with mock.patch.object(browser, "shell32") as shell, \
                mock.patch.object(browser, "_chrome_hwnds", side_effect=[{1}, {1, 2, 3}]), \
                mock.patch.object(browser, "time", fixtures.FakeClock()), \
                mock.patch.object(browser, "close") as close, \
                mock.patch.object(browser, "place") as place:
            shell.ShellExecuteW.return_value = 42
            self.assertIsNone(browser._spawn("https://example.invalid/", (0, 0), (100, 100), 1, False))
        close.assert_not_called()
        place.assert_not_called()

    def test_bind_save_exception_closes_new_window(self):
        with mock.patch.object(browser, "current", return_value=None), \
                mock.patch.object(audio, "probe", return_value={"available": True, "playing": True}), \
                mock.patch.object(browser, "_spawn", return_value=999), \
                mock.patch.object(ui, "documents", return_value=[(object(), "https://gemini.google.com/app")]), \
                mock.patch.object(browser, "_save", side_effect=RuntimeError("save")), \
                mock.patch.object(browser, "close") as close:
            with self.assertRaises(RuntimeError):
                browser.ensure_site("gemini", verbose=False)
        close.assert_called_once_with(999)


if __name__ == "__main__":
    unittest.main()
