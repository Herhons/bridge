"""只读审计 runs/submissions 账本。**不改任何文件。**

用途（T2-1）：施工方与验收方都能一眼看清账本里到底有什么，
特别是"有多少条是 v1 老记录、它们缺哪些字段"——这些是既定事实，
不重建、不回填，只如实列出。

用法：
    python tools/submission_audit.py
    python tools/submission_audit.py --verbose
    python tools/submission_audit.py --root <某个 checkout 根>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import submissions, uilock  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读审计提交账本")
    ap.add_argument("--root", default=str(ROOT), help="checkout 根（默认本仓库）")
    ap.add_argument("--verbose", action="store_true", help="逐条列出")
    args = ap.parse_args(argv)

    folder = Path(args.root) / "runs" / "submissions"
    if not folder.is_dir():
        print(json.dumps({"ok": True, "folder": str(folder), "total": 0,
                          "note": "账本目录不存在（从未提交过）"}, ensure_ascii=False))
        return 0

    files = sorted(folder.glob("*.json"))
    states = Counter()
    levels = Counter()
    legacy = 0
    missing_scope = 0
    missing_session = 0
    unreadable = []
    rows = []
    # B26：被账本拦住、又不会自己恢复的记录，必须能被一眼看见，
    # 并附上**可直接复制**的人工出口命令（之前只能靠人翻目录猜）。
    blocked = []
    now = time.time()

    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unreadable.append({"file": path.name, "error": type(exc).__name__})
            continue
        view = submissions.normalize(record) or {}
        info = submissions.summary(record)
        states[info.get("v2_state")] += 1
        levels[info.get("level")] += 1
        if info.get("legacy"):
            legacy += 1
        if not info.get("has_scope"):
            missing_scope += 1
        if not info.get("session_id"):
            missing_session += 1
        # `accepted` 也在 BLOCKING_STATES 里，但它是**正常终态**（就是它该拦重发），
        # 列出来只会淹掉真问题。这里只报"卡住了、需要人看一眼"的三类。
        if info.get("v2_state") in (submissions.STATE_UNKNOWN,
                                    submissions.STATE_FAILED_TERMINAL,
                                    submissions.STATE_SUBMITTING):
            updated = record.get("updated_at") or record.get("at")
            age = None
            if isinstance(updated, (int, float)):
                age = round((now - float(updated)) / 3600.0, 1)
            entry = {"file": path.name, "v2_state": info.get("v2_state"),
                     "age_hours": age, "updated_at": updated,
                     "submission_count": info.get("submission_count")}
            if info.get("v2_state") == submissions.STATE_UNKNOWN:
                entry["hint"] = ("unknown 可能已经发出去过；确认页面上没有这条提问后，"
                                 "用下面的命令解锁（会允许重复提交）")
                entry["command"] = (f"python tools/submission_clear.py --file {path} "
                                    f"--state unknown --reason \"<为什么确认没发出去>\" "
                                    f"--confirm")
            elif info.get("v2_state") == submissions.STATE_FAILED_TERMINAL:
                entry["command"] = (f"python tools/submission_clear.py --file {path} "
                                    f"--reason \"<为什么可以再试>\" --confirm")
            elif info.get("v2_state") == submissions.STATE_SUBMITTING:
                # `submitting` 是"发送前落账、发送后该有终态"的中间态。正常情况下
                # 几秒内就变成 accepted/unknown；一直停在这里，就是写账进程被杀。
                # 判孤儿要三个条件同时成立，缺一个都不算（2026-11-12 实测账本里
                # 积了 15 条：pid 全死、`scope_after` 全缺、17~125 小时前）。
                dead = uilock.pid_alive(record.get("pid"))
                orphan = (dead is False
                          and not record.get("scope_after")
                          and (age or 0) >= 1.0)
                entry["pid"] = record.get("pid")
                entry["writer_alive"] = dead   # True=还活着 / False=已死 / None=读不到
                entry["has_scope_after"] = bool(record.get("scope_after"))
                entry["orphan_submitting"] = orphan
                entry["hint"] = (
                    "submitting 卡住 = 写账进程在发送途中被杀。孤儿判据（三条同时成立）："
                    "写账进程已死 + 从没写过 scope_after（那是 accepted 才写的）"
                    "+ 超过 1 小时。孤儿多半没发出去，但**不能证明**没发出去 —— "
                    "解锁前先看页面上有没有这条提问。")
                # B26 定的规矩是"submitting 没有既定解法，不许给会误导的命令"。
                # 那条约束继续有效：**只有孤儿**才给命令。进程还活着、状态读不到、
                # 或记录还年轻的时候，给命令等于怂恿人去解锁一条可能正在发送的记录。
                if orphan:
                    entry["command"] = (f"python tools/submission_clear.py --file {path} "
                                        f"--state submitting "
                                        f"--reason \"<确认页面上没有这条提问>\" --confirm")
            blocked.append(entry)
        if args.verbose:
            rows.append({"file": path.name, **info})

    report = {
        "ok": True,
        "folder": str(folder),
        "total": len(files),
        "by_v2_state": dict(states),
        "by_level": {str(k): v for k, v in levels.items()},
        "legacy_records": legacy,
        "missing_scope": missing_scope,
        "missing_session_id": missing_session,
        "unreadable": unreadable,
        "blocked": blocked,
        "blocked_total": len(blocked),
        "orphan_submitting": sum(1 for e in blocked if e.get("orphan_submitting")),
        "note": ("legacy/missing_* 是既定事实，不重建、不回填；"
                 "老记录在恢复时被 reconcile() 判 fail-closed 是有意设计"),
    }
    if args.verbose:
        report["records"] = rows
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
