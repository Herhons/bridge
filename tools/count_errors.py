"""按 CALIBER §9.2 的口径数「用户可见错误行」。**只读**，不改任何日志。

口径（不是这里发明的，是 CALIBER §9.2 定的）：

    一次失败在日志里天然会留下不止一行。只有 `ok == false` 且
    `result` **不属于**诊断 kind 的那一行，才算「用户可见错误」。

    result ∈ {fail_evidence, uia_probe, uia_restart}  →  诊断行，**不计入**

所以 F5 要求「恰好新增 1 条错误记录」时，数的是本脚本的 `errors`，不是日志行数。

用法：
    py -3.10 tools/count_errors.py                     # 今天的日志
    py -3.10 tools/count_errors.py --date 20261107
    py -3.10 tools/count_errors.py --since 13:00 --until 13:30
    py -3.10 tools/count_errors.py --endpoint opencode
    py -3.10 tools/count_errors.py --json              # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

DIAGNOSTIC_KINDS = frozenset({"fail_evidence", "uia_probe", "uia_restart"})
ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


def _minutes(hhmm: str) -> int:
    """'13:05' -> 785。解析不了就报错退出，别猜。"""
    try:
        hh, mm = hhmm.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        raise SystemExit(f"时间要写成 HH:MM，收到 {hhmm!r}")


def _row_minute(row: dict) -> int | None:
    """日志里的 time 形如 2026-11-07T13:05:02+0800。"""
    stamp = row.get("time") or ""
    if "T" not in stamp or len(stamp.split("T", 1)[1]) < 5:
        return None
    hhmm = stamp.split("T", 1)[1][:5]
    try:
        return _minutes(hhmm)
    except SystemExit:
        return None


def load(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"  跳过第 {n} 行（不是合法 JSON）", file=sys.stderr)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--date", default=time.strftime("%Y%m%d"), help="日志日期，默认今天")
    ap.add_argument("--file", default=None, help="直接给一个 jsonl 路径（覆盖 --date）")
    ap.add_argument("--since", default=None, help="只看这个时刻之后（HH:MM）")
    ap.add_argument("--until", default=None, help="只看这个时刻之前（HH:MM，含）")
    ap.add_argument("--endpoint", default=None, help="只看某一端")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    path = Path(args.file) if args.file else RUNS / f"{args.date}.jsonl"
    if not path.is_file():
        print(f"找不到日志：{path}", file=sys.stderr)
        return 1

    lo = _minutes(args.since) if args.since else None
    hi = _minutes(args.until) if args.until else None

    rows = load(path)
    diagnostic = 0
    errors: list[dict] = []
    for row in rows:
        if args.endpoint and row.get("endpoint") != args.endpoint:
            continue
        minute = _row_minute(row)
        if lo is not None and (minute is None or minute < lo):
            continue
        if hi is not None and (minute is None or minute > hi):
            continue
        if row.get("result") in DIAGNOSTIC_KINDS:
            diagnostic += 1
            continue
        if row.get("ok") is False:
            errors.append(row)

    by_endpoint = Counter(r.get("endpoint") or "?" for r in errors)
    by_code = Counter(
        (r.get("error") or "?").split(" ")[0][:40] for r in errors
    )
    per_endpoint_total = defaultdict(int)
    for row in rows:
        if row.get("result") in DIAGNOSTIC_KINDS:
            continue
        if args.endpoint and row.get("endpoint") != args.endpoint:
            continue
        per_endpoint_total[row.get("endpoint") or "?"] += 1

    if args.json:
        print(json.dumps({
            "file": str(path),
            "window": {"since": args.since, "until": args.until, "endpoint": args.endpoint},
            "total_rows": len(rows),
            "diagnostic_rows_excluded": diagnostic,
            "errors": len(errors),
            "errors_by_endpoint": dict(by_endpoint),
            "errors_by_code": dict(by_code),
            "non_diagnostic_rows": dict(per_endpoint_total),
            "error_rows": errors,
        }, ensure_ascii=False, indent=2))
        return 0 if not errors else 2

    print(f"日志：{path}")
    print(f"总计 {len(rows)} 行；其中诊断行（不计入）{diagnostic} 行")
    print(f"窗口：since={args.since} until={args.until} endpoint={args.endpoint}")
    print()
    print(f"用户可见错误：{len(errors)} 条")
    for row in errors:
        print(f"  - {row.get('time')} [{row.get('endpoint')}] "
              f"{row.get('result')} :: {row.get('error')}")
    print()
    print("各端非诊断行数（分母）：")
    for name, count in sorted(per_endpoint_total.items()):
        print(f"  {name:12s} {count:4d} 行，其中错误 {by_endpoint.get(name, 0)} 条")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
