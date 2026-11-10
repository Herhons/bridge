"""人工解锁一条被 `failed_terminal` 拦住的提交账本。**不删原记录。**

`failed_terminal`（登录失效 / 额度耗尽 / 目标端不可用 / 结构不合法被拒）会永久
拦住同一 (endpoint, prompt) 的重发。这是有意的：宁可不动，也不要盲目重发。

真需要再发一次时，走这个工具，并且：

* **必须** `--confirm`（防手滑）
* **必须** `--reason`（写进审计，谁也不能悄悄解锁）
* 只解锁 `failed_terminal`；其它状态说明本来就能发，不该用它
* 原记录保留，追加一条 `kind="clear"` 的 attempt，`cleared_at/cleared_by` 落 evidence
* 解锁后状态变 `failed_retryable`（可重试），`submission_count` 归 0

用法：
    python tools/submission_clear.py --file runs/submissions/workbuddy-<sha>.json \
        --reason "额度已恢复，owner 批准重发" --confirm
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import submissions  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人工解锁 failed_terminal 账本")
    ap.add_argument("--file", required=True, help="runs/submissions/<endpoint>-<sha256>.json")
    ap.add_argument("--reason", required=True, help="解锁原因（写进审计）")
    ap.add_argument("--confirm", action="store_true", help="确认执行（必填）")
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么，不写盘")
    args = ap.parse_args(argv)

    if not args.confirm and not args.dry_run:
        print(json.dumps({"ok": False, "error": "missing_confirm",
                          "hint": "加 --confirm 才会真的写；只想预览用 --dry-run"},
                         ensure_ascii=False))
        return 2
    reason = str(args.reason or "").strip()
    if not reason:
        print(json.dumps({"ok": False, "error": "empty_reason"}, ensure_ascii=False))
        return 2

    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"ok": False, "error": "file_not_found", "path": str(path)},
                         ensure_ascii=False))
        return 2

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "unreadable",
                          "detail": type(exc).__name__}, ensure_ascii=False))
        return 2

    view = submissions.normalize(record) or {}
    before = view.get("v2_state")
    if before != submissions.STATE_FAILED_TERMINAL:
        print(json.dumps({"ok": False, "error": "not_failed_terminal",
                          "current_state": before,
                          "hint": "只有 failed_terminal 需要解锁；其它状态本来就能重发"},
                         ensure_ascii=False))
        return 2

    stamp = time.time()
    upgraded = submissions._upgrade(record, submissions.STATE_FAILED_RETRYABLE, now=stamp)
    upgraded["submission_count"] = 0
    upgraded["level"] = view.get("level")
    upgraded.setdefault("attempts", []).append(
        {"kind": "clear", "at": stamp, "by": os.environ.get("USERNAME") or "operator",
         "reason": reason})
    evidence = dict(upgraded.get("evidence") or {})
    evidence["cleared"] = {"at": stamp, "reason": reason,
                           "from_state": before,
                           "by": os.environ.get("USERNAME") or "operator"}
    upgraded["evidence"] = evidence
    upgraded["updated_at"] = stamp

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "before": before,
                          "after": upgraded["v2_state"],
                          "would_write": str(path)}, ensure_ascii=False, indent=2))
        return 0

    with submissions.tier_guard.file_lock(path.with_suffix(".lock")):
        submissions._write(path, upgraded)

    print(json.dumps({"ok": True, "before": before, "after": upgraded["v2_state"],
                      "file": str(path), "reason": reason}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
