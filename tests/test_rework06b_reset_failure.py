"""CARD-06b 返工：`--new` 之后按回执重选模型失败，也必须走统一失败出口。

独立验收反例（2026-11-06）：`workbuddy._run` 里 `restore_after_new` 返回失败结果时
只 `print(reset)` + `return 5`，绕过了 `_finish`——没有 failure.record（PNG/JSON）、
没有 runlog，也没有把失败回执作废。卡面第 20/28 行要求所有失败由实际入口统一留证。

这里跑真实 `wb._run` → 真实 `restore_after_new` / `tier_guard` / `failure.record` /
`runlog.record`，只把窗口、控件树和最终 `ask` 换成假件。
"""
from offline_guard import ensure_offline
ensure_offline()

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qi import failure, runlog, tier_guard as tg, workbuddy as wb

WANT = "fixture-high"
CURRENT = "fixture-low"


class NewChatRestoreFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "tiers.json"
        table = tg.load_tiers()
        table["endpoints"]["workbuddy"]["dimensions"]["model"].update(
            test=[CURRENT], review=[WANT], accept=[WANT])
        self.config.write_text(json.dumps(table), encoding="utf-8")
        self.fail_dir = self.root / "fail"
        self.logs = self.root / "logs"

    def run_entry(self, *, model_after=WANT, current=CURRENT):
        tg.write_receipt("workbuddy", target=WANT, readback=WANT, root=self.root)
        screen = SimpleNamespace(target_hwnd=None,
                                 grab=lambda *a: np.zeros((2, 2, 3), dtype=np.uint8))
        _, args = wb.parse_args(["offline-fixture", "--new", "--purpose", "review"],
                                sending=True)
        out = io.StringIO()
        with mock.patch.object(tg, "ROOT", self.root), \
                mock.patch.object(tg, "TIERS_FILE", self.config), \
                mock.patch.object(wb, "open_app", return_value=screen), \
                mock.patch.object(wb, "controls", return_value=[]), \
                mock.patch.object(wb, "current_model", return_value=current), \
                mock.patch.object(wb, "select_model",
                                  return_value={"ok": False, "before": current,
                                                "after": current, "want": WANT}), \
                mock.patch.object(wb, "new_chat") as new_chat, \
                mock.patch.object(wb, "ReplyConversation",
                                  return_value=SimpleNamespace(read_via=None,
                                                               copy_note=None)), \
                mock.patch.object(wb, "ask",
                                  return_value={"ok": True, "stage": "answered"}) as ask, \
                mock.patch.object(failure, "FAIL_DIR", self.fail_dir), \
                mock.patch.object(failure, "_LAST", {}), \
                mock.patch.object(failure, "_LAST_PATH", {}), \
                mock.patch.object(runlog, "RUNS", self.logs), \
                contextlib.redirect_stdout(out):
            code = wb._run(args, sending=True)
        lines = [line for line in out.getvalue().strip().splitlines() if line.strip()]
        return code, json.loads(lines[-1]), new_chat, ask

    def test_reset_failure_is_exit5_with_unified_evidence_and_no_send(self):
        code, result, new_chat, ask = self.run_entry()
        self.assertEqual(code, 5, result)
        self.assertEqual(result["stage"], "model_reset_after_new")
        self.assertEqual(result["reason"], "reselect_failed")
        self.assertFalse(result["sent"])
        self.assertEqual(result["submission"], "not_sent")
        self.assertEqual(new_chat.call_count, 1, "走的必须是 --new 这条真实分支")
        self.assertEqual(ask.call_count, 0, "重选失败绝不允许继续发送")

        # 统一失败出口：PNG + JSON + runlog 缺一不可（旧代码 print 完就 return 5）
        metas = [json.loads(p.read_text(encoding="utf-8"))
                 for p in self.fail_dir.glob("*.json")]
        self.assertEqual(len(metas), 1, f"没有统一失败证据：{metas}")
        self.assertEqual(metas[0]["stage"], "model_reset_after_new")
        self.assertEqual(metas[0]["reason"], "reselect_failed")
        self.assertFalse(metas[0]["result"]["ok"])
        self.assertEqual(len(list(self.fail_dir.glob("*.png"))), 1,
                         "统一失败出口必须留一张 PNG")
        self.assertTrue(result["evidence"]["json"].endswith(".json"), result["evidence"])

        # 失败回执作废：这张回执不能再被别的进程拿去发
        self.assertTrue(tg.read_receipt("workbuddy", root=self.root).get("consumed"),
                        "失败回执必须作废（CARD-06a/06b 同一条语义）")

        lines = [json.loads(line) for line in
                 (self.logs / f"{__import__('time').strftime('%Y%m%d')}.jsonl")
                 .read_text(encoding="utf-8").splitlines() if line.strip()]
        results = [line["result"] for line in lines]
        self.assertEqual(results.count("model_reset_after_new"), 1, lines)
        run = next(line for line in lines if line["result"] == "model_reset_after_new")
        self.assertEqual(run["submission"], "not_sent")
        self.assertEqual(run["ok"], False)

    def test_reset_that_recovers_still_sends_normally(self):
        code, result, new_chat, ask = self.run_entry(current=WANT)
        self.assertEqual(code, 0, result)
        self.assertEqual(new_chat.call_count, 1)
        self.assertEqual(ask.call_count, 1)
        self.assertFalse(list(self.fail_dir.glob("*.json")) if self.fail_dir.exists() else [],
                         "成功路径不该留失败证据")
        self.assertFalse(tg.read_receipt("workbuddy", root=self.root).get("consumed"),
                         "成功路径由发送前的门禁消费，不在这里作废")


if __name__ == "__main__":
    unittest.main()
