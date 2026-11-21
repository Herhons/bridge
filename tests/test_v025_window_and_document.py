"""Offline tests for the two v0.2.5 fixes found during the Antigravity live round.

1. ``engine.uia._pick_document_index`` -- Electron apps expose a blank
   ``data:text/html`` background document *before* the real page, so the old
   "first document with a URL" rule snapshotted an empty tree.
2. ``EndpointDriver._find_target_window`` -- Antigravity renames its window to
   the active conversation, so a title pin must not be able to hide a window
   that process name + WNDCLASS already identify (web endpoints keep the strict
   title filter).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.base as base  # noqa: E402
from engine.uia import _is_placeholder_doc_url, _pick_document_index  # noqa: E402
from engine.window import WindowInfo  # noqa: E402

BLANK_DOC = (
    "data:text/html,<!doctype html><html style=\"margin:0;height:100%;"
    "background:%23070708\"><body></body></html>"
)
REAL_DOC = "https://127.0.0.1:7642/c/80b8815a-f0de-41c9-885f-c9cd377d1dc6?project=x"


class DocumentPickTests(unittest.TestCase):
    def test_background_data_url_is_skipped(self):
        self.assertEqual(_pick_document_index([BLANK_DOC, REAL_DOC]), 1)
        self.assertEqual(_pick_document_index([BLANK_DOC, "", REAL_DOC]), 2)

    def test_explicit_host_still_wins(self):
        other = "https://example.com/app"
        self.assertEqual(
            _pick_document_index([BLANK_DOC, other, REAL_DOC], "127.0.0.1:7642"),
            2,
        )

    def test_all_placeholder_falls_back_to_first_document(self):
        # Legacy behaviour: never return "no document" when documents exist.
        self.assertEqual(_pick_document_index([BLANK_DOC, "about:blank"]), 0)

    def test_no_documents_is_minus_one(self):
        self.assertEqual(_pick_document_index([]), -1)

    def test_placeholder_classification(self):
        for url in ("", "   ", "data:text/html,x", "about:blank", "devtools://x",
                    "chrome-error://x"):
            self.assertTrue(_is_placeholder_doc_url(url), url)
        for url in (REAL_DOC, "file:///C:/app/index.html", "https://gemini.google.com/app"):
            self.assertFalse(_is_placeholder_doc_url(url), url)


class TitleFallbackTests(unittest.TestCase):
    def _driver(self, tmp: str, **cfg_overrides):
        cfg = {
            "kind": "desktop",
            "process_names": ["Antigravity.exe"],
            "title_contains": ["Antigravity"],
            "window_class": "Chrome_WidgetWin_1",
        }
        cfg.update(cfg_overrides)
        return base.EndpointDriver(
            "antigravity",
            config=cfg,
            bridge_config={"poll_interval": 0.0, "default_timeout": 5.0},
            ledger_path=Path(tmp) / "ledger.json",
            evidence_dir=Path(tmp) / "fail",
            sleep_fn=lambda _s: None,
        )

    def test_title_drift_retries_without_the_title_filter(self):
        calls: list[dict] = []
        found = WindowInfo(hwnd=4459532, pid=39112, process_name="Antigravity.exe",
                           title="Ignoring Instructions Trap Response",
                           class_name="Chrome_WidgetWin_1")

        def fake_find_window(**kwargs):
            calls.append(kwargs)
            if kwargs.get("title_contains") is None:
                return found
            return None  # title is now the conversation name

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(base, "find_window", side_effect=fake_find_window):
                win = drv.locate_window(auto_open=False)

        self.assertIs(win, found)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["title_contains"], ["Antigravity"])
        self.assertIsNone(calls[1]["title_contains"])
        self.assertEqual(calls[1]["window_class"], "Chrome_WidgetWin_1")
        self.assertEqual(calls[1]["process_names"], ["Antigravity.exe"])

    def test_title_hit_does_not_retry(self):
        calls: list[dict] = []
        found = WindowInfo(hwnd=1, pid=2, process_name="Antigravity.exe",
                           title="Antigravity", class_name="Chrome_WidgetWin_1")

        def fake_find_window(**kwargs):
            calls.append(kwargs)
            return found

        with tempfile.TemporaryDirectory() as tmp:
            drv = self._driver(tmp)
            with mock.patch.object(base, "find_window", side_effect=fake_find_window):
                win = drv.locate_window(auto_open=False)

        self.assertIs(win, found)
        self.assertEqual(len(calls), 1)

    def test_web_endpoint_without_window_class_never_drops_the_title(self):
        calls: list[dict] = []

        def fake_find_window(**kwargs):
            calls.append(kwargs)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            drv = base.EndpointDriver(
                "gemini",
                config={
                    "kind": "web",
                    "process_names": ["chrome.exe"],
                    "title_contains": ["Gemini"],
                },
                bridge_config={"poll_interval": 0.0},
                ledger_path=Path(tmp) / "ledger.json",
                evidence_dir=Path(tmp) / "fail",
                sleep_fn=lambda _s: None,
            )
            with mock.patch.object(base, "find_window", side_effect=fake_find_window):
                self.assertIsNone(drv.locate_window(auto_open=False))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["title_contains"], ["Gemini"])


if __name__ == "__main__":
    unittest.main()
