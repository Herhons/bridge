"""崩溃续跑与防重发（Round 0 修复第 4 项）。

规矩（Herhons 2026-11-12 批准）：
* 每段材料、最终提问都有**固定身份**（内容变则身份变）；
* **账本优先**：`accepted / submitting / unknown` 只对账，不重发；
  对不上答案就停手等人工，**不自动清账本**；
* `sending` 只表示"上次发送途中被杀"，续跑先对着页面核对，不盲目重发；
* `state.json` 有 `schema` 与 `materials`。
"""
from offline_guard import ensure_offline
ensure_offline()

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

from qi import iteration as it                      # noqa: E402
from qi import submissions                          # noqa: E402

_spec = importlib.util.spec_from_file_location("iterate_tool", ROOT / "tools" / "iterate.py")
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)                   # noqa: E402


class IdentityTests(unittest.TestCase):
    def test_chunk_id_is_stable_and_content_addressed(self):
        a = it.chunk_identity("deepseek", 1, "abc")
        b = it.chunk_identity("deepseek", 1, "abc")
        c = it.chunk_identity("deepseek", 1, "abd")
        d = it.chunk_identity("grok", 1, "abc")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c, "内容变了身份必须变")
        self.assertNotEqual(a, d, "换端必须换身份")

    def test_pack_chunks_with_ids(self):
        chunks = it.pack_chunks_with_ids("行\n" * 200, 120, head="头\n", site="deepseek")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([c["index"] for c in chunks], list(range(1, len(chunks) + 1)))
        self.assertTrue(all(c["id"].startswith("chunk:") for c in chunks))
        # 再跑一次，身份不变（续跑靠它）
        again = it.pack_chunks_with_ids("行\n" * 200, 120, head="头\n", site="deepseek")
        self.assertEqual([c["id"] for c in chunks], [c["id"] for c in again])

    def test_prompt_identity_is_round_scoped(self):
        a = it.prompt_identity("gemini", "round-0", "P")
        b = it.prompt_identity("gemini", "round-1", "P")
        self.assertNotEqual(a, b)


class StateSchemaTests(unittest.TestCase):
    def test_state_has_schema_and_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = it.State.load(tmp, "round-0")
            state.mark_chunk("chunk:abc", "sent", chars=10)
            raw = json.loads((Path(tmp) / "docs/iterations/round-0/state.json")
                             .read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], it.SCHEMA)
            self.assertEqual(raw["materials"]["chunk:abc"]["status"], "sent")

    def test_old_state_without_schema_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/iterations/round-0/state.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"steps": {"review:chatgpt": {"at": "x"}}}', encoding="utf-8")
            state = it.State.load(tmp, "round-0")
            self.assertEqual(state.data["schema"], it.SCHEMA)
            self.assertTrue(state.done("review:chatgpt"), "老状态不能被判坏")


class ResumeTests(unittest.TestCase):
    def test_sent_chunks_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = it.State.load(tmp, "round-0")
            chunks = it.pack_chunks_with_ids("行\n" * 200, 120, head="头\n", site="deepseek")
            state.mark_chunk(chunks[0]["id"], "sent")
            state.mark_chunk(chunks[1]["id"], "sent")
            pending = it.resume_chunks(chunks, state)
            self.assertEqual([c["index"] for c in pending],
                             list(range(3, len(chunks) + 1)))


class LedgerGateTests(unittest.TestCase):
    def test_submitting_without_answer_stops_without_sending(self):
        with mock.patch.object(submissions, "read_state", return_value="submitting"), \
                mock.patch.object(iterate, "read_answer_text", return_value=""):
            out = iterate._reconcile_gate(SimpleNamespace(), "gemini", "提问")
        self.assertIsNotNone(out, "有账本记录就必须接管，不许继续发")
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "needs_manual_clear")
        self.assertEqual(out["ledger"], "submitting")

    def test_accepted_with_answer_is_reused(self):
        with mock.patch.object(submissions, "read_state", return_value="accepted"), \
                mock.patch.object(iterate, "read_answer_text", return_value="答" * 400):
            out = iterate._reconcile_gate(SimpleNamespace(), "chatgpt", "提问")
        self.assertTrue(out["ok"])
        self.assertEqual(out["stage"], "already_answered")

    def test_clean_ledger_allows_send(self):
        with mock.patch.object(submissions, "read_state", return_value="failed_retryable"):
            self.assertIsNone(iterate._reconcile_gate(SimpleNamespace(), "grok", "提问"))


class CrashResumeTests(unittest.TestCase):
    """模拟"发送途中被杀"：`sending` 的段只核对，不盲目重发。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = it.State.load(self._tmp.name, "round-0")
        self.cfg = {"chunk_chars": 120, "chunk_timeout": 5, "review_timeout": 5,
                    "chunk_chars_by_site": {}, "ui_lock_timeout": 5}
        self.runs = []

    def _fake_run(self, *args, **kwargs):
        self.runs.append(args[0])
        return SimpleNamespace(stdout='{"ok": true, "reply": "收到"}\n',
                               stderr="", returncode=0)

    def _call(self, page_has):
        with mock.patch.object(iterate, "_screen_for", return_value=SimpleNamespace()), \
                mock.patch.object(iterate, "_clear_composer", return_value=True), \
                mock.patch.object(iterate, "_refresh_tier"), \
                mock.patch.object(iterate, "_page_has", return_value=page_has), \
                mock.patch.object(iterate.subprocess, "run", side_effect=self._fake_run):
            return iterate._ask_via_script("deepseek", "审规格", "行\n" * 200,
                                           self.cfg, self.state)

    def test_sending_chunk_already_on_page_is_not_resent(self):
        chunks = it.pack_chunks_with_ids("行\n" * 200, 120, head="审规格", site="deepseek")
        self.state.mark_chunk(chunks[0]["id"], "sending")
        out = self._call(page_has=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.state.chunk(chunks[0]["id"])["status"], "sent")
        # 第 1 段没重发；后面的段照发
        self.assertEqual(len(self.runs), len(chunks) - 1)
        self.assertNotIn("第 1/", " ".join(" ".join(map(str, r)) for r in self.runs))

    def test_sending_chunk_missing_on_page_is_resent(self):
        chunks = it.pack_chunks_with_ids("行\n" * 200, 120, head="审规格", site="deepseek")
        self.state.mark_chunk(chunks[0]["id"], "sending")
        out = self._call(page_has=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self.runs), len(chunks), "页面上没有就必须补发")

    def test_all_chunks_sent_reads_answer_instead_of_resending(self):
        chunks = it.pack_chunks_with_ids("行\n" * 200, 120, head="审规格", site="deepseek")
        for chunk in chunks:
            self.state.mark_chunk(chunk["id"], "sent")
        with mock.patch.object(iterate, "_screen_for", return_value=SimpleNamespace()), \
                mock.patch.object(iterate, "read_answer_text", return_value="答" * 400), \
                mock.patch.object(iterate.subprocess, "run", side_effect=self._fake_run):
            out = iterate._ask_via_script("deepseek", "审规格", "行\n" * 200,
                                          self.cfg, self.state)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stage"], "resumed_read")
        self.assertEqual(self.runs, [], "全部发过就只读，不许再发")


if __name__ == "__main__":
    unittest.main()
