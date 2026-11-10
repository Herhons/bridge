"""CARD-WEB：网页端新窗口绑定失败 + 目标文档数量为 0。

钉住三件在 2026-11-01 巡检里真实发生的事：

1. **缺 pycaw 时不能静默当成"在播放"。** 本机没装 pycaw，`chrome_playing_audio()`
   永远 True，网页端每轮都新开窗口，日志里只有一行「Chrome 在播音频: True」，
   看不出是检测不可用。
2. **新窗口绑定失败要把自己开的窗口关掉。** 06:0x 那轮留下了 2 个 Gemini +
   1 个 Grok 窗口没人关。
3. **"URL 读不到"不等于"没有文档"。** Chrome/Electron 的页面文档在窗口不在前台时
   ValuePattern 是空串（实测 Gemini 后台 '' → 前台 'https://gemini.google.com/app'，
   OpenCode 后台 '' → 前台 'oc://renderer/index.html'）。旧代码把空值当"没有文档"
   跳过，于是 opencode --new 报「目标文档数量为 0」，WorkBuddy 空闲后也报同一个错。

全部打桩：不开窗口、不读真实 UIA 树、不写 .chrome-window.json。
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qi import audio, browser, ui  # noqa: E402


# ---------------------------------------------------------------- 打桩工具
class FakeClock:
    """把等待变成瞬移：await_page 的 12 秒轮询在测试里一秒都不用真等。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_control(name="Doc"):
    return SimpleNamespace(Name=name)


def fake_session(process_name="chrome.exe", state=0):
    return SimpleNamespace(Process=SimpleNamespace(name=lambda: process_name),
                           State=state)


def install_fake_pycaw(sessions):
    """装一个假的 pycaw，测试不依赖本机装没装。"""
    session_module = types.ModuleType("pycaw.pycaw")
    session_module.AudioUtilities = SimpleNamespace(
        GetAllSessions=lambda: list(sessions))
    package = types.ModuleType("pycaw")
    package.pycaw = session_module
    return mock.patch.dict(sys.modules, {"pycaw": package, "pycaw.pycaw": session_module})


# ---------------------------------------------------------------- 1. pycaw
class AudioDetectionTests(unittest.TestCase):
    def test_missing_pycaw_is_reported_instead_of_silently_playing(self):
        with mock.patch.dict(sys.modules, {"pycaw": None, "pycaw.pycaw": None}):
            info = audio.probe()
            conservative = audio.chrome_playing_audio()

        self.assertFalse(info["available"])
        self.assertIsNone(info["playing"])
        self.assertIn("音频检测不可用", info["hint"])
        self.assertIn("pycaw", info["hint"])
        # 保守语义保留：测不了就当在播放，免得去动用户可能正看着的东西
        self.assertTrue(conservative)

    def test_probe_reports_the_real_state_when_pycaw_works(self):
        with install_fake_pycaw([fake_session("chrome.exe", 0)]):
            idle = audio.probe()
        self.assertTrue(idle["available"])
        self.assertFalse(idle["playing"])
        self.assertEqual(idle["hint"], "")

        with install_fake_pycaw([fake_session("chrome.exe", 1)]):
            playing = audio.probe()
        self.assertTrue(playing["available"])
        self.assertTrue(playing["playing"])

    def test_other_processes_do_not_count_as_chrome_audio(self):
        with install_fake_pycaw([fake_session("Weixin.exe", 1)]):
            self.assertFalse(audio.probe()["playing"])


# ---------------------------------------------------------------- 2. 新窗口收尾
class SpawnCleanupTests(unittest.TestCase):
    def test_failed_new_window_is_closed_instead_of_left_on_desktop(self):
        clock = FakeClock()
        closed = []

        with mock.patch.object(browser, "current", return_value=None), \
             mock.patch.object(browser, "_save") as saved, \
             mock.patch.object(browser, "_spawn", return_value=999), \
             mock.patch.object(browser, "close", side_effect=closed.append), \
             mock.patch.object(browser, "time", clock), \
             mock.patch.object(audio, "probe",
                               return_value={"available": True, "playing": True, "hint": ""}), \
             mock.patch.object(ui, "documents", return_value=[]), \
             mock.patch.object(ui, "window_facts", return_value="hwnd=999 前台=是"):
            result = browser.ensure_site("gemini", verbose=False)

        self.assertIsNone(result)
        self.assertEqual(closed, [999], "绑定失败要把自己开的窗口关掉")
        saved.assert_not_called()

    def test_spawn_that_never_shows_a_window_closes_late_arrivals(self):
        """Launch 慢半拍时，事后冒出来的新窗口也不能留在桌面上。"""
        clock = FakeClock()
        closed = []
        seen = [{7}, {7, 8}]               # 开之前只有 7；超时后 8 才冒出来

        with mock.patch.object(browser, "shell32") as shell, \
             mock.patch.object(browser, "_chrome_hwnds",
                               side_effect=lambda: seen.pop(0)), \
             mock.patch.object(browser, "close", side_effect=closed.append), \
             mock.patch.object(browser, "time", clock):
            shell.ShellExecuteW.return_value = 42
            result = browser._spawn("https://gemini.google.com/app",
                                    (0, 0), (100, 100), timeout=0.0, verbose=False)

        self.assertIsNone(result)
        self.assertEqual(closed, [8])

    def test_reusing_a_user_window_never_closes_it(self):
        """复用用户的窗口失败时只报现场，不许关人家的窗口。"""
        clock = FakeClock()
        closed = []

        class FakeScreen:
            def __init__(self, verbose=True):
                pass

            def bind(self, hwnd):
                pass

            def focus(self):
                return True

        with mock.patch.object(browser, "current", return_value=555), \
             mock.patch.object(browser, "close", side_effect=closed.append), \
             mock.patch.object(browser, "time", clock), \
             mock.patch.object(browser, "_open_url"), \
             mock.patch("qi.screen.Screen", FakeScreen), \
             mock.patch.object(ui, "documents", return_value=[]), \
             mock.patch.object(ui, "tabs", return_value=[]), \
             mock.patch.object(ui, "window_facts", return_value="hwnd=555 前台=否"):
            result = browser.ensure_site("gemini", verbose=False)

        self.assertIsNone(result)
        self.assertEqual(closed, [], "用户的窗口不许关")


# ---------------------------------------------------------------- 3. 文档查找
class DocumentLookupTests(unittest.TestCase):
    def test_zero_documents_reports_what_it_actually_saw(self):
        with mock.patch.object(ui, "candidates", return_value=[]), \
             mock.patch.object(ui, "window_facts",
                               return_value="hwnd=1234 标题='WorkBuddy' 前台=否 最小化=否"):
            with self.assertRaises(RuntimeError) as caught:
                ui.document(1234, "workbuddy")

        message = str(caught.exception)
        self.assertIn("目标文档数量为 0，无法绑定", message)
        self.assertIn("hwnd=1234", message)
        self.assertIn("前台=否", message)
        self.assertIn("没有可用的 DocumentControl", message)

    def test_mismatched_document_lists_every_candidate(self):
        docs = [(fake_control("Grok"), "https://grok.com/")]
        with mock.patch.object(ui, "candidates", return_value=docs):
            with self.assertRaises(RuntimeError) as caught:
                ui.document(42, "gemini")

        message = str(caught.exception)
        self.assertIn("目标文档数量为 0", message)
        self.assertIn("https://grok.com/", message)

    def test_unknown_url_with_a_single_document_still_binds(self):
        """OpenCode / WorkBuddy 空闲后就是这个形态：文档在，URL 读不到。"""
        control = fake_control("OpenCode")
        with mock.patch.object(ui, "candidates", return_value=[(control, "")]):
            bound, url = ui.document(7, "opencode")

        self.assertIs(bound, control)
        self.assertEqual(url, "")

    def test_unknown_url_for_a_web_site_binds_the_only_document(self):
        control = fake_control("Google Gemini")
        with mock.patch.object(ui, "candidates", return_value=[(control, "")]):
            bound, url = ui.document(8, "gemini")

        self.assertIs(bound, control)
        self.assertEqual(url, "")

    def test_two_desktop_documents_are_not_guessed(self):
        docs = [(fake_control("A"), "file:///a"), (fake_control("B"), "file:///b")]
        with mock.patch.object(ui, "candidates", return_value=docs):
            with self.assertRaises(RuntimeError) as caught:
                ui.document(9, "workbuddy")
        self.assertIn("目标文档数量为 2", str(caught.exception))

    def test_empty_url_is_retried_before_giving_up(self):
        """accessibility 树首次访问会只给空 URL，隔一次就好了（实测如此）。"""
        control = fake_control("WorkBuddy")
        reads = [[(control, "")], [(control, "file:///D:/WorkBuddy/index.html")]]

        with mock.patch.object(ui, "documents", side_effect=lambda hwnd: reads.pop(0)), \
             mock.patch.object(ui.time, "sleep"):
            found = ui.candidates(11)

        self.assertEqual(found, [(control, "file:///D:/WorkBuddy/index.html")])

    def test_no_document_triggers_recovery_once(self):
        control = fake_control("WorkBuddy")
        reads = [[], [(control, "file:///D:/WorkBuddy/index.html")]]

        with mock.patch.object(ui, "documents", side_effect=lambda hwnd: reads.pop(0)), \
             mock.patch.object(ui, "_recover") as recovered:
            found = ui.candidates(12)

        self.assertEqual(found, [(control, "file:///D:/WorkBuddy/index.html")])
        recovered.assert_called_once_with(12)


# ---------------------------------------------------------------- 4. 输出提示
class BrowserOutputTests(unittest.TestCase):
    def test_unavailable_audio_detection_is_announced_not_silent(self):
        import contextlib
        import io

        clock = FakeClock()
        output = io.StringIO()
        control = fake_control("Google Gemini")

        with mock.patch.object(browser, "current", return_value=None), \
             mock.patch.object(browser, "_save"), \
             mock.patch.object(browser, "_spawn", return_value=321), \
             mock.patch.object(browser, "time", clock), \
             mock.patch.object(audio, "probe",
                               return_value={"available": False, "playing": None,
                                             "hint": "音频检测不可用：没有安装 pycaw"}), \
             mock.patch.object(ui, "documents",
                               return_value=[(control, "https://gemini.google.com/app")]), \
             contextlib.redirect_stdout(output):
            hwnd = browser.ensure_site("gemini", verbose=True)

        self.assertEqual(hwnd, 321)
        printed = output.getvalue()
        self.assertIn("音频检测不可用", printed)
        self.assertIn("pycaw", printed)
        self.assertNotIn("Chrome 在播音频: True", printed)


if __name__ == "__main__":
    unittest.main()
