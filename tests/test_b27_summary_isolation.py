"""Gemini 汇总：必须新开对话 + 归档记录会话 ID（Round 0 修复第 5 项）。

规矩（Herhons 2026-11-12 批准）：
* 汇总**先在 gemini 新开对话**，没开成就不发；
* 归档里必须留下 `session_id`（"确实换了会话"的客观证据）；
* 页面没有会话 id → `summary_not_isolated`，结果不可信。
"""
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import importlib.util                              # noqa: E402

from qi import conversation as conversation_mod     # noqa: E402
from qi import ui as ui_mod                          # noqa: E402
from qi import uilock                               # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)                   # noqa: E402

ARGS = SimpleNamespace(round="round-0")
CFG = {"review_timeout": 5, "ui_lock_timeout": 5}


class SummaryIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        rdir = root / "docs" / "iterations" / "round-0"
        rdir.mkdir(parents=True)
        (rdir / "anonymous_pack.md").write_text("## F1\n\n某条意见\n", encoding="utf-8")
        self.root = root
        self.rdir = rdir
        self._old_root = iterate.ROOT
        iterate.ROOT = root

    def tearDown(self):
        iterate.ROOT = self._old_root

    def _run(self, *, new_chat=True, url="https://gemini.google.com/app/abc123def",
             reply="汇总结果", ok=True):
        asked = []
        with mock.patch.object(iterate, "_screen_for", return_value=SimpleNamespace(target_hwnd=1)), \
                mock.patch.object(iterate, "_clear_composer", return_value=True), \
                mock.patch.object(iterate, "_clear_stale_lock"), \
                mock.patch.object(iterate, "_refresh_tier"), \
                mock.patch.object(iterate, "start_new_chat", return_value=new_chat), \
                mock.patch.object(iterate, "_conversation", return_value=SimpleNamespace()), \
                mock.patch.object(conversation_mod, "ask",
                                  side_effect=lambda *a, **k: asked.append(1) or
                                  {"ok": ok, "stage": "answered" if ok else "timeout",
                                   "reply": reply}), \
                mock.patch.object(ui_mod, "document", return_value=(None, url)), \
                mock.patch.object(uilock, "hold",
                                  side_effect=lambda *a, **k: contextlib.nullcontext()):
            code = iterate.cmd_summarize(ARGS, CFG)
        return code, asked

    def test_requires_new_chat_before_sending(self):
        code, asked = self._run(new_chat=False)
        self.assertEqual(code, 1)
        self.assertEqual(asked, [], "新开对话失败就不许发")
        self.assertFalse((self.rdir / "gemini_summary.json").exists())

    def test_session_id_is_archived(self):
        code, asked = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(asked), 1)
        payload = json.loads((self.rdir / "gemini_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["session_id"], "abc123def")
        self.assertEqual(payload["chars"], len("汇总结果"))
        self.assertTrue((self.rdir / "gemini_summary.md").is_file())
        state = json.loads((self.rdir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["steps"]["summarize"]["session_id"], "abc123def")

    def test_missing_session_id_is_flagged(self):
        code, _ = self._run(url="https://gemini.google.com/app")
        self.assertEqual(code, 1)
        payload = json.loads((self.rdir / "gemini_summary.json").read_text(encoding="utf-8"))
        self.assertIsNone(payload["session_id"], "没换会话就必须如实记 None")


if __name__ == "__main__":
    unittest.main()
