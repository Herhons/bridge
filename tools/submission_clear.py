"""人工解锁一条被账本拦住的提交。**不删原记录。**

两种状态需要人工介入，且含义不同：

* `failed_terminal`（登录失效 / 额度耗尽 / 目标端不可用 / 结构不合法被拒）——
  重试没有意义，解锁是"允许再试一次"。
* `unknown`（上次提交状态不明，**可能已经发出去了**）——解锁就是允许重发，
  **存在重复提交风险**；只在确认页面上没有那条提问时才做。

真需要再发一次时，走这个工具，并且：

* **必须** `--confirm`（防手滑）；只想预览用 `--dry-run`
* **必须** `--reason`（写进审计，谁也不能悄悄解锁）
* `--state` 选要解锁的状态（默认 `failed_terminal`，行为与旧版逐字节一致）
* 原记录保留，追加一条 `kind="clear"` 的 attempt，`cleared_at/cleared_by` 落 evidence
* 解锁后状态变 `failed_retryable`（可重试），`submission_count` 归 0

用法：
    python tools/submission_clear.py --file runs/submissions/workbuddy-<sha>.json \
        --reason "额度已恢复，owner 批准重发" --confirm
    python tools/submission_clear.py --file runs/submissions/workbuddy-<sha>.json \
        --state unknown --reason "页面确认没有这条提问" --confirm
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
    ap = argparse.ArgumentParser(description="人工解锁被账本拦住的提交")
    ap.add_argument("--file", required=True, help="runs/submissions/<endpoint>-<sha256>.json")
    ap.add_argument("--reason", required=True, help="解锁原因（写进审计）")
    ap.add_argument("--state", choices=(submissions.STATE_FAILED_TERMINAL,
                                        submissions.STATE_UNKNOWN,
                                        submissions.STATE_SUBMITTING),
                    default=submissions.STATE_FAILED_TERMINAL,
                    help="要解锁的状态；默认 failed_terminal（与旧版行为一致）")
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
    if before != args.state:
        hint = ("只有 failed_terminal 需要解锁；其它状态本来就能重发"
                if args.state == submissions.STATE_FAILED_TERMINAL else
                f"当前是 {before!r}；要解锁 unknown 请显式 --state unknown"
                "（注意：unknown 可能已经发出去过，解锁会允许重复提交）")
        print(json.dumps({"ok": False, "error": "state_mismatch",
                          "expected": args.state, "current_state": before,
                          "hint": hint}, ensure_ascii=False))
        return 2

    if before in (submissions.STATE_UNKNOWN, submissions.STATE_SUBMITTING) and not args.dry_run:
        # unknown 的语义是"可能已经发出去了"，写盘前必须让人看清记录里有什么。
        print(json.dumps({
            "summary": {"file": str(path), "v2_state": before,
                        "attempts": len(view.get("attempts") or []),
                        "evidence_keys": sorted((view.get("evidence") or {}).keys()),
                        "updated_at": view.get("updated_at"),
                        "submission_count": view.get("submission_count")},
            "warning": "解锁 unknown 会允许重发同一提问，存在重复提交风险",
        }, ensure_ascii=False, indent=2))

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
