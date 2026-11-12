"""推进器（西西弗斯）：只做交接与续跑，不做决策。

职责（Herhons 2026-11-12 定的分工）：
  · 读本轮 `plan.md` 的条目表，按 ID 断点续跑（状态落 `docs/iterations/round-N/sisyphus.json`）；
  · 交接：把条目交给施工方/会审方，记录前后 SHA、条目 ID、回执；
  · 硬停：条目里出现「花钱 / 登录 / 删数据 / v0.0.8」关键字时拒绝执行。

它**不**决定做什么、不核实意见、不改规格。计划由 Gemini 汇总 + 推进器核实产出。

用法：
    py -3.10 -B tools/sisyphus.py status --round round-1
    py -3.10 -B tools/sisyphus.py next   --round round-1
    py -3.10 -B tools/sisyphus.py done   --round round-1 --id S1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARD_STOP_WORDS = ("花钱", "付费", "登录", "注册", "删除用户数据", "回收站", "v0.0.8")


def plan_path(round_name: str) -> Path:
    return ROOT / "docs" / "iterations" / round_name / "plan.md"


def state_path(round_name: str) -> Path:
    return ROOT / "docs" / "iterations" / round_name / "sisyphus.json"


def parse_plan(text: str) -> list[dict]:
    """把 plan.md 的表格行解析成条目：| ID | 项 | 落点 | 验收 |。"""
    items = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        ident = cells[0]
        if not re.fullmatch(r"[A-Za-z]+[0-9]+", ident):
            continue
        items.append({"id": ident, "title": cells[1], "where": cells[2],
                      "accept": cells[3] if len(cells) > 3 else ""})
    return items


def load_state(round_name: str) -> dict:
    path = state_path(round_name)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"schema": "sisyphus/1", "items": {}}


def save_state(round_name: str, data: dict) -> None:
    path = state_path(round_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def hard_stop(item: dict) -> str | None:
    blob = " ".join(str(item.get(k, "")) for k in ("title", "where", "accept"))
    for word in HARD_STOP_WORDS:
        if word in blob:
            return word
    return None


def cmd_status(args) -> int:
    path = plan_path(args.round)
    if not path.is_file():
        print(json.dumps({"ok": False, "error": "plan_missing", "path": str(path)},
                         ensure_ascii=False))
        return 2
    items = parse_plan(path.read_text(encoding="utf-8"))
    state = load_state(args.round)
    for item in items:
        item["status"] = (state["items"].get(item["id"]) or {}).get("status", "pending")
    print(json.dumps({"ok": True, "items": items,
                      "done": sum(1 for i in items if i["status"] == "done"),
                      "lock": {k: args.lock.get(k) for k in ("cleared", "reason")}},
                     ensure_ascii=False, indent=2))
    return 0


def verification_ok(round_name: str, ident: str) -> tuple[bool, str]:
    """条目必须先在本轮 `verification.md` 里被核实为成立/部分成立。"""
    vpath = ROOT / "docs" / "iterations" / round_name / "verification.md"
    if not vpath.is_file():
        return False, "verification_missing"
    text = vpath.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or cells[0].lstrip("# ").strip() != ident:
            continue
        blob = " ".join(cells)
        if "成立" in blob:
            return True, "verified"
        return False, "not_verified"
    return False, "not_in_verification"


def cmd_next(args) -> int:
    path = plan_path(args.round)
    items = parse_plan(path.read_text(encoding="utf-8")) if path.is_file() else []
    state = load_state(args.round)
    for item in items:
        if (state["items"].get(item["id"]) or {}).get("status") == "done":
            continue
        ok, why = verification_ok(args.round, item["id"])
        if not ok:
            print(json.dumps({"ok": False, "stage": "not_verified", "id": item["id"],
                              "reason": why,
                              "hint": "先在本轮 verification.md 核实为成立/部分成立"},
                             ensure_ascii=False))
            return 4
        tries = attempt(args.round, item["id"])
        if tries >= MAX_ATTEMPTS:
            print(json.dumps({"ok": False, "stage": "no_progress", "id": item["id"],
                              "attempts": tries,
                              "hint": "同一计划连续 3 次无进展，按 §12 B3 停轮"},
                             ensure_ascii=False))
            return 5
        blocked = hard_stop(item)
        if blocked:
            print(json.dumps({"ok": False, "stage": "hard_stop", "id": item["id"],
                              "word": blocked,
                              "hint": "命中四种硬停之一，交 Herhons"},
                             ensure_ascii=False))
            return 3
        print(json.dumps({"ok": True, "next": item}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"ok": True, "next": None, "hint": "计划已跑完"}, ensure_ascii=False))
    return 0


MAX_ATTEMPTS = 3


def attempt(round_name: str, ident: str) -> int:
    return int((load_state(round_name)["items"].get(ident) or {}).get("attempts", 0))


def bump_attempt(round_name: str, ident: str) -> int:
    state = load_state(round_name)
    record = state["items"].setdefault(ident, {"status": "pending"})
    record["attempts"] = int(record.get("attempts", 0)) + 1
    save_state(round_name, state)
    return record["attempts"]


def cmd_done(args) -> int:
    state = load_state(args.round)
    state["items"][args.id] = {"status": "done", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "note": args.note or ""}
    save_state(args.round, state)
    print(json.dumps({"ok": True, "id": args.id}, ensure_ascii=False))
    return 0


def lock_housekeeping() -> dict:
    """开跑前处理「持有者已死」的残留 UI 锁。

    为什么挂在推进器入口：它是每轮第一个被人工/自动化调用的命令，孤儿锁在这里
    清一次，后面 review / use 脚本就不会先撞一次 Busy 再自愈。
    **清理失败不阻断**：推进器自己不动网页，清不掉只影响提示，不影响交接。
    """
    try:
        from qi import uilock
    except Exception as exc:              # noqa: BLE001 - 导入失败只记录
        return {"cleared": False, "reason": f"import_failed:{type(exc).__name__}"}
    try:
        info = uilock.clear_stale()
    except Exception as exc:              # noqa: BLE001
        return {"cleared": False, "reason": f"failed:{type(exc).__name__}"}
    if info.get("cleared"):
        print(f"[sisyphus] 清掉残留 UI 锁（pid={info.get('pid')} owner={info.get('owner')}）",
              file=__import__("sys").stderr)
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="推进器：只做交接与续跑")
    ap.add_argument("command", choices=("status", "next", "done"))
    ap.add_argument("--round", required=True)
    ap.add_argument("--id")
    ap.add_argument("--note")
    args = ap.parse_args(argv)
    args.lock = lock_housekeeping()
    if args.command == "status":
        return cmd_status(args)
    if args.command == "next":
        return cmd_next(args)
    if not args.id:
        print(json.dumps({"ok": False, "error": "id_required"}, ensure_ascii=False))
        return 2
    return cmd_done(args)


if __name__ == "__main__":
    raise SystemExit(main())
