"""B14-b：`_site_ready` 与 `ui._pick` 同构（并发模式不堆窗）。

**为什么要这条**：Chrome 不在前台时 `DocumentControl` 的 `ValuePattern` 是**空串**
（`ui.documents` 的 docstring 明写：那是"窗口不在前台"，不是"没有这个文档"）。
所以"只按 host 匹配"在 `BRIDGE_UI_CONCURRENT=1` 下**每轮都是 False** →
每次 `ensure_site` 都新开一个窗口 → 窗口越堆越多。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qi import browser, ui  # noqa: E402


class SiteReadyTests(unittest.TestCase):
    def _ready(self, docs, name="gemini", remembered=False):
        with mock.patch.object(ui, "documents", return_value=docs):
            return browser._site_ready(4321, name, remembered=remembered)

    def test_host_match_wins(self):
        docs = [(object(), "https://gemini.google.com/app")]
        self.assertTrue(self._ready(docs))

    def test_empty_url_single_document_is_ready_when_remembered(self):
        """后台窗口 URL 为空 + 只有一个文档 → 就是它（等价 `ui._pick` 的兜底）。"""
        self.assertTrue(self._ready([(object(), "")], remembered=True))

    def test_empty_url_single_document_is_not_ready_when_stranger(self):
        """不是我们记住的窗口，就不许用弱证据 —— 认错窗口会往别人的对话里写字。"""
        self.assertFalse(self._ready([(object(), "")], remembered=False))

    def test_two_empty_documents_are_never_ready(self):
        """两个都读不到 URL 时 `ui._pick` 也不绑（`len(unknown) == 1` 才绑）。"""
        docs = [(object(), ""), (object(), "")]
        self.assertFalse(self._ready(docs, remembered=True))

    def test_another_site_is_not_ready(self):
        docs = [(object(), "https://chatgpt.com/")]
        self.assertFalse(self._ready(docs, name="gemini", remembered=True))

    def test_read_failure_is_not_ready(self):
        with mock.patch.object(ui, "documents", side_effect=RuntimeError("boom")):
            self.assertFalse(browser._site_ready(4321, "gemini", remembered=True))


class ConcurrentEnsureTests(unittest.TestCase):
    def _ensure(self, ready, spawned=777):
        with mock.patch.object(browser, "concurrent", return_value=True), \
             mock.patch.object(browser, "current", return_value=1234), \
             mock.patch.object(browser, "_site_ready", return_value=ready) as ready_mock, \
             mock.patch.object(browser, "_spawn", return_value=spawned) as spawn_mock, \
             mock.patch.object(browser, "_save") as save_mock:
            got = browser.ensure_site("gemini", verbose=False)
        return got, ready_mock, spawn_mock, save_mock

    def test_reuses_the_remembered_window_without_spawning(self):
        got, ready_mock, spawn_mock, save_mock = self._ensure(ready=True)
        self.assertEqual(got, 1234)
        spawn_mock.assert_not_called()
        save_mock.assert_not_called()
        # 复用时**必须**带 remembered=True，否则后台窗口永远判不成就绪
        self.assertEqual(ready_mock.call_args.kwargs.get("remembered"), True)

    def test_spawns_once_when_the_remembered_window_is_not_ready(self):
        got, _ready_mock, spawn_mock, save_mock = self._ensure(ready=False)
        self.assertEqual(got, 777)
        self.assertEqual(spawn_mock.call_count, 1)
        save_mock.assert_called_once_with(777, "gemini")

    def test_does_not_retry_spawn_forever(self):
        got, _ready_mock, spawn_mock, save_mock = self._ensure(ready=False, spawned=None)
        self.assertIsNone(got)
        self.assertEqual(spawn_mock.call_count, 1, "一次 ensure_site 只允许开一个窗")
        save_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
