"""任务卡 05：工作流状态机 + 命令行的离线测试。

全部在临时目录里跑：``progress-test.json`` 和 ``workflow.json`` 都是副本，
不碰真的 ``progress.json``；不开窗口、不发消息、不调用任何端的 CLI。
命令行用例多数不传 ``--workflow``，走的是默认路径（只读真的
``docs\workflow\workflow.json``），改的永远只有临时目录里那份 ``progress-test.json``。

覆盖卡里点名的十一条：每条合法边、3 条非法跳转（文件不变）、block 生成 05b/05c、
hold 的进/拒/回、连推 3 次判 stuck 且中途 move 清零、顶层 status 同步、
原有 7 个顶层字段全保留、写前备份、BOM 可读、退出码 0/2/3/4。
"""
from __future__ import annotations

from offline_guard import ensure_offline
ensure_offline()


import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import workflow_use                                    # noqa: E402
from qi import workflow                                # noqa: E402

DEFINITION = ROOT / "docs" / "workflow" / "workflow.json"
SCRIPT = ROOT / "src" / "scripts" / "workflow_use.py"

TOP = {                                                # 真 progress.json 的 7 个顶层字段
    "card_id": "05",
    "status": "running",
    "note": "CARD-05 施工中",
    "updated_at": "2026-11-01T10:20:10+08:00",
    "running": ["05"],
    "verdicts": {"01": "PASS", "04+04b": "BLOCK"},
    "queue": [],
}
TOP_FIELDS = set(TOP)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(state: str = "draft", *, no_progress: int = 0, missing=None,
           parent=None, history=None) -> dict:
    return {"state": state, "no_progress": no_progress, "parent": parent,
            "missing": list(missing or []), "history": list(history or [])}


class Case(unittest.TestCase):
    """每个用例一套临时目录：definition 副本 + progress-test.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.definition = self.root / "workflow.json"
        shutil.copyfile(DEFINITION, self.definition)
        self.file = self.root / "progress-test.json"
        self.write(copy.deepcopy(TOP))

    def tearDown(self):
        self._tmp.cleanup()

    # -- 文件小工具 ---------------------------------------------------------
    def write(self, data: dict) -> None:
        self.file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def cards(self) -> dict:
        return self.read()["workflow"]["cards"]

    def seed(self, state: str = "draft", card: str = "05", *, others=None,
             payload: dict | None = None, **kwargs) -> dict:
        """直接把卡摆到某个状态（测试摆盘用，不走状态机）。"""
        data = copy.deepcopy(payload if payload is not None else TOP)
        cards = {card: record(state, **kwargs)}
        for cid, extra in (others or {}).items():
            cards[cid] = extra if isinstance(extra, dict) else record(extra)
        data["workflow"] = {"cards": cards}
        self.write(data)
        return data

    # -- 动作小工具 ---------------------------------------------------------
    def move(self, card, state, **kwargs):
        return workflow.move(card, state, progress_path=self.file,
                             workflow_path=self.definition, **kwargs)

    def advance(self, card="05"):
        return workflow.advance(card, progress_path=self.file, workflow_path=self.definition)

    def resolve(self, card="05"):
        return workflow.resolve_hold(card, progress_path=self.file,
                                     workflow_path=self.definition)

    def summary(self):
        return workflow.summary(progress_path=self.file)

    def cli(self, *argv):
        """跑命令行，返回 (退出码, stdout, stderr)。"""
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workflow_use.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class LegalEdges(Case):
    """跳转表里的每一条合法边都要能走通。"""

    def test_every_legal_edge_moves(self):
        definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
        edges = [(old, new) for old, targets in definition["transitions"].items()
                 for new in targets]
        self.assertEqual(len(edges), 15)              # 1+2+3+1+2+2+3+0+1+0

        for old, new in edges:
            with self.subTest(edge=f"{old}->{new}"):
                self.seed(old)
                extra = {"missing": ["复验截图"]} if new == "hold" else {}
                result = self.move("05", new, **extra)
                self.assertEqual((result["from"], result["to"]), (old, new))
                card = self.cards()["05"]
                self.assertEqual(card["state"], new)
                self.assertEqual(card["no_progress"], 0)
                self.assertEqual(card["history"][-1]["from"], old)
                self.assertEqual(card["history"][-1]["to"], new)
                self.assertRegex(card["history"][-1]["at"],
                                 r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")

    def test_definition_matches_card(self):
        data = json.loads(DEFINITION.read_text(encoding="utf-8"))
        self.assertEqual(data["states"],
                         ["draft", "sent", "running", "done", "failed", "stuck",
                          "verifying", "pass", "block", "hold"])
        self.assertEqual(data["terminal"], ["pass"])
        self.assertEqual(data["max_no_progress"], 3)
        self.assertEqual(data["seats"], {"plan": "红莉栖", "build": "workbuddy",
                                         "verify": "codex"})
        self.assertEqual(data["transitions"]["block"], [])
        self.assertEqual(data["transitions"]["pass"], [])
        self.assertEqual(data["transitions"]["done"], ["verifying"])


class IllegalEdges(Case):
    """非法跳转：拒绝、消息固定、文件一个字节都不动。"""

    def test_three_illegal_moves_rejected_and_file_untouched(self):
        cases = [("draft", "pass"), ("running", "running"), ("block", "sent")]
        for old, new in cases:
            with self.subTest(edge=f"{old}->{new}"):
                self.seed(old)
                before = sha(self.file)
                with self.assertRaises(workflow.IllegalTransition) as caught:
                    self.move("05", new)
                self.assertEqual(str(caught.exception), f"非法跳转: 05 {old} -> {new}")
                self.assertEqual(sha(self.file), before)

    def test_rejected_move_leaves_no_backup(self):
        self.seed("draft")
        with self.assertRaises(workflow.IllegalTransition):
            self.move("05", "pass")
        self.assertEqual(list(self.root.glob("_backup_*")), [])

    def test_unknown_state_and_unknown_card(self):
        self.seed("draft")
        with self.assertRaises(workflow.UnknownState):
            self.move("05", "flying")
        with self.assertRaises(workflow.CardNotFound):
            self.move("99", "sent")
        self.assertEqual(self.cards()["05"]["state"], "draft")


class BlockAndRepair(Case):
    """block 生成修复卡；原卡停在 block 不再动。"""

    def test_block_creates_05b_then_05c(self):
        self.seed("verifying")
        first = self.move("05", "block")
        self.assertEqual(first["repair_card"], "05b")
        cards = self.cards()
        self.assertEqual(cards["05"]["state"], "block")
        self.assertEqual(cards["05b"]["state"], "sent")
        self.assertEqual(cards["05b"]["parent"], "05")
        self.assertEqual(cards["05b"]["no_progress"], 0)
        self.assertEqual(cards["05b"]["missing"], [])

        for state in ("running", "done", "verifying"):     # 修复卡被再次打回
            self.move("05b", state)
        second = self.move("05b", "block")
        self.assertEqual(second["repair_card"], "05c")
        self.assertEqual(self.cards()["05c"]["parent"], "05b")
        self.assertEqual(self.cards()["05b"]["state"], "block")

    def test_block_skips_existing_suffix(self):
        self.seed("verifying", others={"05b": record("done")})
        result = self.move("05", "block")
        self.assertEqual(result["repair_card"], "05c")

    def test_block_stays_blocked(self):
        self.seed("verifying")
        self.move("05", "block")
        before = sha(self.file)
        with self.assertRaises(workflow.IllegalTransition):
            self.move("05", "sent")
        self.assertEqual(sha(self.file), before)
        self.assertEqual(self.cards()["05"]["state"], "block")


class HoldFlow(Case):
    """hold：没清单不许进；有清单进；没清空不许回 verifying；清空后自动回。"""

    def test_hold_requires_missing(self):
        self.seed("verifying")
        before = sha(self.file)
        with self.assertRaises(workflow.MoveRejected) as caught:
            self.move("05", "hold")
        self.assertIn("缺少证据清单", str(caught.exception))
        with self.assertRaises(workflow.MoveRejected):
            self.move("05", "hold", missing=["   "])       # 全是空白也算没给
        self.assertEqual(sha(self.file), before)
        self.assertEqual(self.cards()["05"]["state"], "verifying")

    def test_hold_with_missing_then_resolve(self):
        self.seed("verifying")
        result = self.move("05", "hold", missing=["复验截图", "退出码原文"])
        self.assertEqual(result["to"], "hold")
        self.assertEqual(self.cards()["05"]["missing"], ["复验截图", "退出码原文"])

        with self.assertRaises(workflow.MoveRejected) as caught:
            self.move("05", "verifying")
        self.assertIn("证据未补齐", str(caught.exception))
        self.assertEqual(self.cards()["05"]["state"], "hold")

        back = self.resolve()
        self.assertEqual((back["from"], back["to"]), ("hold", "verifying"))
        card = self.cards()["05"]
        self.assertEqual(card["state"], "verifying")
        self.assertEqual(card["missing"], [])
        self.assertEqual([x["to"] for x in card["history"]], ["hold", "verifying"])

    def test_resolve_hold_rejects_card_not_in_hold(self):
        self.seed("verifying")
        with self.assertRaises(workflow.MoveRejected) as caught:
            self.resolve()
        self.assertIn("卡不在 hold", str(caught.exception))


class ProgressWatch(Case):
    """advance：连推 3 次判 stuck，中途 move 成功清零。"""

    def test_three_advances_mark_stuck(self):
        self.seed("running")
        self.assertEqual(self.advance(), {"need_supervisor": False})
        self.assertEqual(self.cards()["05"]["no_progress"], 1)
        self.assertEqual(self.advance(), {"need_supervisor": False})
        self.assertEqual(self.cards()["05"]["no_progress"], 2)
        self.assertEqual(self.advance(), {"need_supervisor": True})
        card = self.cards()["05"]
        self.assertEqual(card["state"], "stuck")
        self.assertEqual(card["no_progress"], 0)          # 转 stuck 也是一次成功的 move
        self.assertEqual(card["history"][-1], {"from": "running", "to": "stuck",
                                               "at": card["history"][-1]["at"]})

    def test_successful_move_resets_counter(self):
        self.seed("running")
        self.advance()
        self.advance()
        self.assertEqual(self.cards()["05"]["no_progress"], 2)
        self.move("05", "done")
        self.assertEqual(self.cards()["05"]["no_progress"], 0)
        self.assertEqual(self.advance(), {"need_supervisor": False})
        self.assertEqual(self.cards()["05"]["state"], "done")
        self.assertEqual(self.cards()["05"]["no_progress"], 1)

    def test_advance_without_stuck_edge_only_reports(self):
        self.seed("verifying")
        results = [self.advance() for _ in range(3)]
        self.assertEqual(results, [{"need_supervisor": False},
                                   {"need_supervisor": False},
                                   {"need_supervisor": True}])
        card = self.cards()["05"]
        self.assertEqual(card["state"], "verifying")      # 去不了 stuck 就不动
        self.assertEqual(card["no_progress"], 3)


class ProgressShape(Case):
    """数据形状：顶层 7 个字段不丢，workflow 键只在需要时出现。"""

    def test_seven_top_fields_survive(self):
        self.seed("running")
        self.move("05", "done")
        data = self.read()
        self.assertEqual(set(data), TOP_FIELDS | {"workflow"})
        self.assertEqual(data["card_id"], "05")
        self.assertEqual(data["note"], TOP["note"])
        self.assertEqual(data["running"], ["05"])
        self.assertEqual(data["verdicts"], TOP["verdicts"])
        self.assertEqual(data["queue"], [])
        self.assertIsInstance(data["queue"], list)
        self.assertIsInstance(data["verdicts"], dict)
        self.assertIsInstance(data["running"], list)

    def test_top_card_status_and_updated_at_sync(self):
        self.seed("running")
        self.move("05", "done")
        data = self.read()
        self.assertEqual(data["status"], "done")
        self.assertNotEqual(data["updated_at"], TOP["updated_at"])
        self.assertRegex(data["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")

    def test_other_card_does_not_touch_top(self):
        self.seed("verifying", "06")
        self.move("06", "block")
        data = self.read()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["updated_at"], TOP["updated_at"])

    def test_card_without_workflow_entry_seeds_from_top_status(self):
        # 真 progress.json 现在只有顶层 card_id/status，没有 workflow 键
        self.write(copy.deepcopy(TOP))
        self.assertEqual(self.summary(), [{"card": "05", "state": "running", "parent": None,
                                           "missing": [], "no_progress": None,
                                           "source": "status"}])
        self.move("05", "done")
        data = self.read()
        self.assertEqual(data["workflow"]["cards"]["05"]["state"], "done")
        self.assertEqual(data["status"], "done")

    def test_summary_lists_all_cards_sorted(self):
        self.seed("verifying", others={"05c": record("sent", parent="05b"),
                                       "05b": record("block", parent="05")})
        rows = self.summary()
        self.assertEqual([r["card"] for r in rows], ["05", "05b", "05c"])
        self.assertEqual([r["state"] for r in rows], ["verifying", "block", "sent"])
        self.assertEqual(rows[2]["parent"], "05b")

    def test_backup_written_before_save(self):
        self.seed("running")
        before = self.file.read_bytes()
        self.move("05", "done")

        folders = list(self.root.glob("_backup_*"))
        self.assertEqual(len(folders), 1)
        self.assertRegex(folders[0].name, r"^_backup_\d{8}$")
        backups = list(folders[0].glob("progress-test.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"^progress-test\.json\.bak-\d{6}$")
        self.assertEqual(backups[0].read_bytes(), before)     # 存的就是写前那一版

    def test_second_write_same_second_keeps_both_backups(self):
        self.seed("running")
        self.move("05", "done")
        self.move("05", "verifying")
        backups = sorted((self.root / list(self.root.glob("_backup_*"))[0].name)
                         .glob("progress-test.json.bak-*"))
        self.assertEqual(len(backups), 2)                     # 同名加 -2，不覆盖

    def test_bom_file_readable_and_written_clean(self):
        self.seed("running")
        raw = self.file.read_text(encoding="utf-8")
        self.file.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
        self.assertEqual(self.summary()[0]["state"], "running")
        self.move("05", "done")
        self.assertFalse(self.file.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.cards()["05"]["state"], "done")

    def test_broken_progress_raises(self):
        self.file.write_text("{ 这不是 JSON", encoding="utf-8")
        with self.assertRaises(workflow.ProgressError):
            workflow.summary(progress_path=self.file)
        with self.assertRaises(workflow.ProgressError):
            self.move("05", "sent")


class CommandLine(Case):
    """退出码：0 正常 / 2 拒绝 / 3 需要监工 / 4 其他错误。"""

    def test_0_status_and_legal_move(self):
        code, out, _ = self.cli("status", "--file", str(self.file))
        self.assertEqual(code, 0)
        self.assertIn("05 running", out)

        self.seed("running")
        code, out, _ = self.cli("move", "05", "done", "--file", str(self.file))
        self.assertEqual(code, 0)
        self.assertIn("05 running -> done", out)

        # --file 放在子命令前面也行
        code, out, _ = self.cli("--file", str(self.file), "status")
        self.assertEqual(code, 0)

    def test_0_resolve_hold(self):
        self.seed("verifying")
        self.cli("move", "05", "hold", "--file", str(self.file), "--missing", "截图")
        code, out, _ = self.cli("resolve-hold", "05", "--file", str(self.file))
        self.assertEqual(code, 0)
        self.assertIn("hold -> verifying", out)

    def test_2_illegal_move(self):
        self.seed("draft")
        code, out, err = self.cli("move", "05", "pass", "--file", str(self.file))
        self.assertEqual(code, 2)
        self.assertIn("非法跳转: 05 draft -> pass", err)
        self.assertEqual(out, "")

    def test_2_hold_without_missing_and_resolve_outside_hold(self):
        self.seed("verifying")
        code, _, err = self.cli("move", "05", "hold", "--file", str(self.file))
        self.assertEqual(code, 2)
        self.assertIn("缺少证据清单", err)

        code, _, err = self.cli("resolve-hold", "05", "--file", str(self.file))
        self.assertEqual(code, 2)
        self.assertIn("卡不在 hold", err)

    def test_3_needs_supervisor(self):
        self.seed("running")
        self.assertEqual(self.cli("advance", "05", "--file", str(self.file))[0], 0)
        self.assertEqual(self.cli("advance", "05", "--file", str(self.file))[0], 0)
        code, out, _ = self.cli("advance", "05", "--file", str(self.file))
        self.assertEqual(code, 3)
        self.assertIn("需要监工: 05", out)
        self.assertEqual(self.cards()["05"]["state"], "stuck")

    def test_4_missing_file_and_bad_json(self):
        code, _, err = self.cli("status", "--file", str(self.root / "nope.json"))
        self.assertEqual(code, 4)
        self.assertIn("找不到进度文件", err)

        self.file.write_text("{ 坏掉了", encoding="utf-8")
        code, _, err = self.cli("status", "--file", str(self.file))
        self.assertEqual(code, 4)
        self.assertIn("不是合法 JSON", err)

    def test_4_unknown_card_state_and_args(self):
        self.seed("draft")
        code, _, err = self.cli("move", "99", "sent", "--file", str(self.file))
        self.assertEqual(code, 4)
        self.assertIn("卡号不存在", err)

        code, _, err = self.cli("move", "05", "flying", "--file", str(self.file))
        self.assertEqual(code, 4)
        self.assertIn("状态名不存在", err)

        code, _, err = self.cli("status")                    # 缺 --file
        self.assertEqual(code, 4)
        self.assertIn("缺少 --file", err)

        code, _, err = self.cli("move", "05", "--file", str(self.file))   # 缺状态
        self.assertEqual(code, 4)

    def test_4_broken_workflow_definition(self):
        self.seed("running")
        self.definition.write_text('{"version": 1}', encoding="utf-8")
        code, _, err = self.cli("move", "05", "done", "--file", str(self.file),
                                "--workflow", str(self.definition))
        self.assertEqual(code, 4)
        self.assertIn("工作流定义", err)

    def test_real_process_exit_codes(self):
        self.seed("running")
        done = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPT),
                               "status", "--file", str(self.file)],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("05 running", done.stdout)

        bad = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPT),
                              "move", "05", "pass", "--file", str(self.file)],
                             cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8")
        self.assertEqual(bad.returncode, 2, bad.stderr)
        self.assertIn("非法跳转", bad.stderr)


if __name__ == "__main__":
    unittest.main()
