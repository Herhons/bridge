"""B16-S1：网页像素路径必须绑自己的标签，桌面端不能绑。

B15 造了 `Screen.tab_site` + `_ensure_tab()`，但全仓只有 `Conversation.__init__`
会赋值。choose / prepare / 像素档位这几条路径全是空操作——tabs 模式下
`s.find()` / `s.grab()` 会拍到别的席位那一页（W4g：gemini `prepare_failed`、
grok `menu_not_open`、chatgpt `no_writer` 都沾这条）。

实现刻意**不改 `bind()` 签名**：仓库里大量测试桩的 `bind(self, hwnd)` 只收一个
参数，加 `site=` 会把它们全打红。入口统一在 bind/focus 成功之后直接写
`screen.tab_site = <站点>`，和 `Conversation.__init__` 的写法一致。

这里钉住：
1. `bind()` 不碰 tab_site（旧调用零变化）；
2. `_ensure_tab` 在 tabs 模式下进 `section("tab:<site>")` 再切标签；
3. `per_site` 模式下一次都不切；
4. 八个网页入口真的写了 tab_site，桌面脚本一个都没写。
"""
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import browser, screen as screen_mod, uilock, window  # noqa: E402

WEB_ASSIGNMENTS = {
    "src/qi/choose.py": "s.tab_site = cfg.name",
    "src/qi/flow.py": "screen.tab_site = site.name",
    "src/qi/gemini.py": "screen.tab_site = site.name",
    "src/scripts/chatgpt_choose.py": 's.tab_site = "chatgpt"',
    "src/scripts/deepseek_choose.py": 's.tab_site = "deepseek"',
    "src/scripts/gemini_choose.py": 'sc.tab_site = "gemini"',
    "src/scripts/grok_use.py": 'screen.tab_site = "grok"',
}
DESKTOP_NO_BINDING = (
    "src/scripts/codex_use.py",
    "src/scripts/dsh_use.py",
    "src/scripts/opencode_use.py",
    "src/qi/workbuddy.py",
    "src/qi/desk_flow.py",
)


class BindTests(unittest.TestCase):
    def setUp(self):
        # bind() 会问 IsWindow 并顺手 restore；离线里不碰真实窗口 API。
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(screen_mod, "user32",
                          types.SimpleNamespace(IsWindow=lambda hwnd: 1)).start()
        mock.patch.object(window, "restore", return_value=True).start()

    def test_bind_leaves_tab_site_untouched(self):
        s = screen_mod.Screen(verbose=False)
        self.assertTrue(s.bind(4321))
        self.assertIsNone(s.tab_site)
        s.tab_site = "gemini"
        self.assertTrue(s.bind(4321))
        self.assertEqual(s.tab_site, "gemini")

    def test_bind_rejects_dead_window(self):
        with mock.patch.object(screen_mod, "user32",
                               types.SimpleNamespace(IsWindow=lambda hwnd: 0)):
            s = screen_mod.Screen(verbose=False)
            self.assertFalse(s.bind(0))
            self.assertIsNone(s.target_hwnd)


class EnsureTabTests(unittest.TestCase):
    def _screen(self):
        s = screen_mod.Screen(verbose=False)
        s.target_hwnd = 4321
        s.tab_site = "gemini"
        return s

    def test_per_site_never_switches(self):
        with mock.patch.object(browser, "window_mode", return_value="per_site"), \
             mock.patch.object(browser, "acquire_tab") as acquire:
            self._screen()._ensure_tab()
        acquire.assert_not_called()

    def test_tabs_switches_inside_named_section(self):
        section = mock.MagicMock(return_value=contextlib.nullcontext())
        with mock.patch.object(browser, "window_mode", return_value="tabs"), \
             mock.patch.object(uilock, "section", section), \
             mock.patch.object(browser, "acquire_tab") as acquire:
            self._screen()._ensure_tab()
        self.assertEqual(section.call_args.args[0], "tab:gemini")
        acquire.assert_called_once_with("gemini", 4321)

    def test_no_site_is_no_op_even_in_tabs_mode(self):
        with mock.patch.object(browser, "window_mode", return_value="tabs") as mode, \
             mock.patch.object(browser, "acquire_tab") as acquire:
            s = self._screen()
            s.tab_site = None
            s._ensure_tab()
        acquire.assert_not_called()
        mode.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_web_entry_points_set_tab_site(self):
        for rel, expected in WEB_ASSIGNMENTS.items():
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_desktop_entry_points_never_set_tab_site(self):
        for rel in DESKTOP_NO_BINDING:
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn("tab_site", text)


if __name__ == "__main__":
    unittest.main()
