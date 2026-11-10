"""B15-B：单窗多标签（`tabs` 模式）的取标签语义。

三条契约：
1. **幂等**：当前活动标签已经是自己的 → 0 次切换、不碰标签条；
2. **fail-closed**：认不出自己的标签就报 `TabUnbound`，绝不猜一个就往上写字；
3. **不越界**：`per_site` 模式（默认）下 `acquire_document` 与今天的 `ui.document` 等价。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import browser, ui  # noqa: E402


class FakeTab:
    def __init__(self, name):
        self.name = name
        self.control = mock.Mock()

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


class WindowModeTests(unittest.TestCase):
    def test_default_is_per_site(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("BRIDGE_WEB_WINDOW_MODE", None)
            self.assertEqual(browser.window_mode(), "per_site")

    def test_tabs_mode_is_opt_in(self):
        with mock.patch.dict("os.environ", {"BRIDGE_WEB_WINDOW_MODE": "tabs"}):
            self.assertEqual(browser.window_mode(), "tabs")


class AcquireTabTests(unittest.TestCase):
    def test_already_on_my_site_costs_nothing(self):
        with mock.patch.object(browser, "_active_url",
                               return_value=("https://gemini.google.com/app/1", 1)), \
             mock.patch.object(ui, "tabs") as tabs_mock:
            browser.acquire_tab("gemini", 1234)
        tabs_mock.assert_not_called()

    def test_switches_then_verifies(self):
        item = FakeTab("Google Gemini - 内存用量高")
        item.control.GetRuntimeId.return_value = [42, 32768, 3]
        with mock.patch.object(browser, "_active_url",
                               return_value=("https://chatgpt.com/", 1)), \
             mock.patch.object(ui, "tabs", return_value=[item]), \
             mock.patch.object(browser, "_await_site", return_value=True), \
             mock.patch.object(browser, "_save_tab") as save_mock:
            browser.acquire_tab("gemini", 1234)
        item.control.GetSelectionItemPattern.return_value.Select.assert_called_once()
        save_mock.assert_called_once()

    def test_two_candidates_that_never_verify_is_ambiguous(self):
        items = [FakeTab("Google Gemini A"), FakeTab("Google Gemini B")]
        with mock.patch.object(browser, "_active_url",
                               return_value=("https://chatgpt.com/", 1)), \
             mock.patch.object(ui, "tabs", return_value=items), \
             mock.patch.object(browser, "_await_site", return_value=False), \
             mock.patch.object(browser, "_save_tab") as save_mock:
            with self.assertRaises(browser.TabUnbound) as ctx:
                browser.acquire_tab("gemini", 1234)
        self.assertEqual(ctx.exception.stage, "tab_ambiguous")
        save_mock.assert_not_called()

    def test_no_tab_strip_is_reported_not_guessed(self):
        with mock.patch.object(browser, "_active_url", return_value=("", 1)), \
             mock.patch.object(ui, "tabs", return_value=[]):
            with self.assertRaises(browser.TabUnbound) as ctx:
                browser.acquire_tab("gemini", 1234)
        self.assertEqual(ctx.exception.stage, "tab_strip_missing")

    def test_desktop_site_is_a_no_op(self):
        """codex 这类桌面端没有标签概念：直接返回，不读标签条。"""
        with mock.patch.object(ui, "tabs") as tabs_mock:
            browser.acquire_tab("codex", 1234)
        tabs_mock.assert_not_called()


class AcquireDocumentTests(unittest.TestCase):
    def test_per_site_mode_is_the_old_call(self):
        sentinel = (object(), "https://gemini.google.com/app")
        with mock.patch.object(ui, "document", return_value=sentinel) as doc_mock, \
             mock.patch.object(browser, "acquire_tab") as tab_mock:
            got = browser.acquire_document("gemini", 1234, wake=False)
        self.assertEqual(got, sentinel)
        tab_mock.assert_not_called()
        doc_mock.assert_called_once_with(1234, "gemini", wake=False)

    def test_tabs_mode_switches_first(self):
        """切标签现在放在 `ui.document` 里（一处覆盖所有直连调用点）。

        这里直接验 `ui.document`：tabs 模式必须先 acquire_tab 再读。
        """
        sentinel = (object(), "https://gemini.google.com/app")
        fake_pick = mock.patch.object(ui, "_pick", return_value=sentinel)
        with mock.patch.dict("os.environ", {"BRIDGE_WEB_WINDOW_MODE": "tabs"}), \
             mock.patch.object(browser, "acquire_tab") as tab_mock, \
             mock.patch.object(ui, "candidates", return_value=[]), fake_pick:
            ui.document(1234, "gemini")
        tab_mock.assert_called_once_with("gemini", 1234)


class StateFileTests(unittest.TestCase):
    def test_four_sites_share_one_window_and_keep_distinct_tabs(self):
        tmp = Path(tempfile.mkdtemp(prefix="b15-state-")) / ".chrome-window.json"
        with mock.patch.object(browser, "STATE", tmp):
            for site in ("chatgpt", "gemini", "grok", "deepseek"):
                browser._save_tab(4321, site, {"rid": [1, 2, site], "name": site})
            data = json.loads(tmp.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 3)
        self.assertEqual(set(data["by_site"].values()), {4321})
        self.assertEqual(len({tuple(v["rid"]) for v in data["tabs"].values()}), 4)
        self.assertEqual(data["tabs"]["gemini"]["name"], "gemini")


if __name__ == "__main__":
    unittest.main()
