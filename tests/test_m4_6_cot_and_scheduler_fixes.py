"""Offline coverage for M4-6: DeepSeek CoT scrubbing + scheduler label fixes.

Every test here is pure logic or mocked: no bridge.py, no git mutation, no
UIA. The CoT cases pin the *reasoning-opener* secondary boundary (needed once
``已思考（用时 N 秒）`` has been eaten by the base noise filter and the reply
arrives as one unbroken blob), and the dispatch case pins that a level which
committed but never answered is never handed to the next executor.
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

import auto_heal_loop as ahl  # noqa: E402
from endpoints.deepseek import (  # noqa: E402
    REASONING_LINE_RE,
    strip_thinking_sections,
)

CARD = (
    "<<<WORKBUDDY_TASK_START>>>\n"
    "step 1: edit engine/base.py and run the full unittest suite\n"
    "<<<WORKBUDDY_TASK_END>>>"
)


class DeepSeekCotBlobTests(unittest.TestCase):
    def test_headerless_blob_is_trimmed_line_by_line(self):
        """No blank line at all: the usual ``uia_delta`` shape after noise filter."""
        for line in ("嗯，先看输入。", "好的，接着看。", "我们需要确认边界",
                     "用户要求只保留正文", "让我先分析", "Let's check", "The user wants"):
            self.assertTrue(REASONING_LINE_RE.match(line), line)
        for line in ("建议按段落剥离。", "答案是 42。", "M4-6 已落地。"):
            self.assertFalse(REASONING_LINE_RE.match(line), line)

        lines = [
            "已思考（用时 9 秒）",
            "用户问的是单测为什么会红，让我先看断言。",
            "我们需要先确认 uia_delta 的实际形状",
            "建议把整段推理剥掉，只回最终答案。",
        ]
        self.assertEqual(
            strip_thinking_sections(lines),
            ["建议把整段推理剥掉，只回最终答案。"],
        )
        # Never return nothing: a fully matching blob degrades to its last line.
        self.assertEqual(
            strip_thinking_sections(
                ["已思考（用时 4 秒）", "嗯，先看输入。", "我们需要确认边界。"]
            ),
            ["我们需要确认边界。"],
        )

    def test_extra_reasoning_paragraphs_are_dropped(self):
        lines = [
            "思考过程",
            "用户希望只保留最终回答正文。",
            "",
            "好的，先看 strip_thinking_sections 的边界。",
            "",
            "结论：按段落剥离即可。",
        ]
        self.assertEqual(
            strip_thinking_sections(lines), ["结论：按段落剥离即可。"]
        )

    def test_plain_reply_without_a_header_is_untouched(self):
        lines = ["这是一段普通回答，不能被删。", "", "第二段也不能被删。"]
        self.assertEqual(strip_thinking_sections(lines), lines)
        # An opener without a reasoning header has no knowable boundary either.
        opener_only = ["首先，结论是 A。", "其次 B。"]
        self.assertEqual(strip_thinking_sections(opener_only), opener_only)

    def test_explicit_final_answer_label_closes_the_block(self):
        lines = ["深度思考", "嗯，先看提示词。", "最终回答：", "答案是 42。"]
        self.assertEqual(strip_thinking_sections(lines), ["答案是 42。"])


class RoadmapTitleTests(unittest.TestCase):
    def test_first_pending_item_names_the_round(self):
        backlog = (
            "## 4. 里程碑\n\n"
            "- [x] **M4-5（GUI 体验修复）**：已完成\n"
            "- [ ] **M4-6（DeepSeek CoT 正文净化）**：修复推理正文混入\n"
            "- [ ] M4-7（架构瘦身）：消除重复控制逻辑\n"
        )
        self.assertEqual(
            ahl.current_roadmap_task_title(backlog),
            "M4-6（DeepSeek CoT 正文净化）",
        )
        for empty in ("", "no checkbox here", "git log -n 10 --oneline:\nabc1234 x"):
            self.assertEqual(ahl.current_roadmap_task_title(empty), "自动迭代")
        capped = ahl.current_roadmap_task_title("- [ ] " + "长" * 80)
        self.assertEqual(len(capped), 48)
        self.assertEqual(capped, "长" * 48)


class TaskPromptHeaderTests(unittest.TestCase):
    def test_header_names_the_round_with_the_roadmap_item(self):
        with mock.patch.object(ahl, "git_text", return_value="63c3d15 base"):
            prompt = ahl.build_task_prompt(
                7, "- [ ] **M4-6（CoT 净化）**：修复混入", "63c3d15"
            )
        first_line = prompt.splitlines()[0]
        self.assertEqual(
            first_line,
            "【Bridge 自动迭代 round=7 | M4-6（CoT 净化） | BASE_COMMIT=63c3d15】",
        )
        self.assertNotIn("Bridge 过夜迭代", prompt)
        self.assertNotIn("过夜迭代", first_line)
        self.assertIn("WORKBUDDY_TASK_START", prompt)


class ExecutorCommittedBrakeTests(unittest.TestCase):
    def test_committed_head_stops_the_fallback_to_opencode(self):
        calls: list[str] = []

        def fake_run_cmd(cmd, timeout=0, cwd=None):
            calls.append(cmd[3])
            return 0, ""  # workbuddy committed, then died without a reply

        heads = ["a" * 40, "b" * 40]

        def fake_head():
            return heads.pop(0) if heads else "b" * 40

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            card_id = ahl.card_id_for(CARD, 3)
            with mock.patch.object(ahl, "RUNS", runs), \
                    mock.patch.object(ahl, "run_cmd", side_effect=fake_run_cmd), \
                    mock.patch.object(ahl, "read_reply", return_value=""), \
                    mock.patch.object(ahl, "worktree_fingerprint", return_value=""), \
                    mock.patch.object(ahl, "head_commit", side_effect=fake_head), \
                    mock.patch.object(ahl, "log") as logger:
                reply = ahl.call_executor(CARD, 3, 60, "auto")
                record = ahl.read_dispatch_record(card_id)

        self.assertEqual(calls, ["workbuddy"], "no second executor may run")
        self.assertIn("worktree changed by executor workbuddy", reply)
        self.assertEqual(record["status"], "completed")
        log_text = " ".join(str(call) for call in logger.call_args_list)
        self.assertIn("executor_committed=workbuddy", log_text)


if __name__ == "__main__":
    unittest.main()
