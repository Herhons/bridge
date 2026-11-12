"""把一条 `accepted` 账本记录降为 `unknown`（一次性、留审计）。

**为什么不直接解锁**：会审三方一致——降级不等于放行重发。`unknown` 仍在
`BLOCKING_STATES` 里，是否再发一次由人决定（`submission_clear --state unknown`）。

用法：
    python tools/submission_demote.py --file runs/submissions/gemini-<sha>.json \
        --page-empty --scope-mismatch \
        --reason "页面 /app、0 条消息；scope 与记录不符" --confirm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import submissions  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="accepted -> unknown（一次性降级）")
    ap.add_argument("--file", required=True)
    ap.add_argument("--page-empty", action="store_true", help="页面确实找不到这条消息")
    ap.add_argument("--scope-mismatch", action="store_true", help="记录的 scope 与当前不符")
    ap.add_argument("--no-l3", action="store_true", help="没有完整回答级别的证据")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.confirm and not args.dry_run:
        print(json.dumps({"ok": False, "error": "missing_confirm"}, ensure_ascii=False))
        return 2

    record = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    endpoint = record.get("endpoint") or ""
    prompt_hash = record.get("prompt_hash")
    if not endpoint or not prompt_hash:
        print(json.dumps({"ok": False, "error": "bad_record"}, ensure_ascii=False))
        return 2
    evidence = {"page_empty": bool(args.page_empty),
                "scope_mismatch": bool(args.scope_mismatch),
                "no_l3": bool(args.no_l3)}
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "evidence": evidence,
                          "state": record.get("v2_state")}, ensure_ascii=False))
        return 0
    # 用原记录的 prompt 哈希定位：demote 走 (endpoint, prompt) 接口
    out = submissions.demote(endpoint, record.get("prompt") or prompt_hash,
                             reason=args.reason, evidence=evidence)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
