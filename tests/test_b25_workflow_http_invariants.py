"""B25：HTTP 版 `/workflow` 不许把 hold 的证据链抹掉。

现场（四端会审 2026-11-11，ChatGPT 第 5 条）：`server.py` 的 `workflow_move`
无条件写 `updated["missing"] = []`，入参里的 `missing` 被丢弃。于是 CLI 侧
两条不变量在 HTTP 路径上被绕开：

  · `hold ⇒ missing 非空`
  · `hold → verifying ⇒ 原 missing 已清`

配套的 `block ⇒ 存在 parent 修复子卡` 也只在 CLI 里做，HTTP 造出的 block 卡没有主。
"""
from offline_guard import ensure_offline
ensure_offline()

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import server, workflow                       # noqa: E402


def _progress(card="05", state="verifying", missing=None):
    return {
        "card_id": card,
        "status": state,
        "updated_at": "2026-11-11T00:00:00+08:00",
        "workflow": {
            "cards": {
                card: {"state": state, "no_progress": 0, "parent": None,
                       "missing": list(missing or []), "history": []},
            }
        },
    }


class CheckMoveUnit(unittest.TestCase):
    """纯函数层：两条不变量。"""

    def test_hold_requires_missing(self):
        with self.assertRaises(workflow.MoveRejected):
            workflow.check_move({"state": "verifying", "missing": []}, "hold", [])

    def test_hold_accepts_and_drops_blanks(self):
        items = workflow.check_move({"state": "verifying"}, "hold", ["a", "", "  ", "b"])
        self.assertEqual(items, ["a", "b"])

    def test_hold_exit_requires_clearing(self):
        with self.assertRaises(workflow.MoveRejected):
            workflow.check_move({"state": "hold", "missing": ["a"]}, "verifying", [])

    def test_other_moves_unaffected(self):
        self.assertEqual(workflow.check_move({"state": "verifying"}, "block", []), [])


class HttpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "docs" / "workflow").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "docs" / "workflow" / "workflow.json",
                    self.root / "docs" / "workflow" / "workflow.json")
        self.engine = server.Engine(root=self.root, token="t", port=0,
                                    runner=lambda *a, **k: None)

    def write(self, payload):
        (self.root / "progress.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def cards(self):
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        return (data.get("workflow") or {}).get("cards") or {}

    def test_hold_without_missing_is_rejected(self):
        self.write(_progress(state="verifying"))
        with self.assertRaises(server.RequestError) as hit:
            self.engine.workflow_move({"card": "05", "to": "hold"})
        self.assertEqual(hit.exception.status, 422)
        self.assertEqual(self.cards()["05"]["state"], "verifying", "被拒就不许写盘")

    def test_hold_keeps_the_missing_list(self):
        self.write(_progress(state="verifying"))
        out = self.engine.workflow_move({"card": "05", "to": "hold",
                                         "missing": ["截图缺失", "日志缺失"]})
        self.assertEqual(out["state"], "hold")
        self.assertEqual(self.cards()["05"]["missing"], ["截图缺失", "日志缺失"])

    def test_leaving_hold_with_evidence_open_is_rejected(self):
        self.write(_progress(state="hold", missing=["截图缺失"]))
        with self.assertRaises(server.RequestError) as hit:
            self.engine.workflow_move({"card": "05", "to": "verifying"})
        self.assertEqual(hit.exception.status, 422)

    def test_block_creates_repair_card(self):
        self.write(_progress(state="verifying"))
        out = self.engine.workflow_move({"card": "05", "to": "block"})
        self.assertEqual(out["state"], "block")
        self.assertEqual(out.get("repair_card"), "05b")
        self.assertEqual(self.cards()["05b"]["parent"], "05")

    # ---- B26：顶层 status 必须跟着卡片走（否则 autopush 继续推已结束的卡） ----

    def test_top_level_status_follows_the_card(self):
        payload = _progress(state="verifying")
        payload["status"] = "running"                 # 顶层与卡片故意不一致
        self.write(payload)
        self.engine.workflow_move({"card": "05", "to": "block"})
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "block", "顶层 status 没跟着改")

    def test_other_cards_do_not_touch_top_level(self):
        payload = _progress(card="05", state="verifying")
        payload["status"] = "running"
        payload["workflow"]["cards"]["06"] = {
            "state": "verifying", "no_progress": 0, "parent": None,
            "missing": [], "history": []}
        self.write(payload)
        self.engine.workflow_move({"card": "06", "to": "block"})
        data = json.loads((self.root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "running", "改了别的卡却动了顶层状态")


if __name__ == "__main__":
    unittest.main()
