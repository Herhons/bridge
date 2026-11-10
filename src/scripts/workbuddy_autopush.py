"""自动推：WorkBuddy 停了就补一句「继续按卡施工」（CARD-08 第 2 条 / PLAN 第 7 项子卡 CARD-07b）。

判定和记账在 `qi/autopush.py`，这里只管命令行和「真去发」。

用法：

    python src\\scripts\\workbuddy_autopush.py --dry-run          只判定：不发消息、不写日志、不通知
    python src\\scripts\\workbuddy_autopush.py                    判定该推就真发
    python src\\scripts\\workbuddy_autopush.py --idle-minutes 10 --dry-run

判定（`qi.autopush.decide`）：

    progress.json 是 running
    + runlog 超过 N 分钟没有新行（N 默认 20，`--idle-minutes` 可配；autopush 自己写的行不算）
    + 没有「生成中」标志（runlog 最后一行的 `stage=generating`，或命令行 `--generating`）
    → 推一次 `workbuddy_use --continue "继续按卡施工"`

防重复：

    同一张卡 30 分钟内最多推 1 次（`--cooldown-minutes`）；
    连推 3 次 `progress.json` 的 `updated_at` 还没动过 → 停推，调用通知脚本叫红莉栖
    （`--no-notify` 只用来验流程，同一个 progress 只叫一次）。

真推成功后会在 `runs\\YYYYMMDD.jsonl` 记一行 `result=autopush_push`；推失败记
`autopush_push_failed`（**不算**一次推，30 分钟冷却也不占用——没发出去就没打搅过谁）。

退出码：

    0  判定跑完：推成功 / 该等 / 不用动
    1  该推，但 `workbuddy_use` 没成功（它的退出码和最后一行输出记在 runlog 的 error 里）
    2  参数错
    3  连推 3 次没有进展：已停推并叫过人（同一张卡同一次 progress 只叫一次）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi import autopush, console, progress, server, tier_guard, uilock  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_PUSH_FAILED = 1
EXIT_USAGE = 2
EXIT_NEEDS_HUMAN = 3
PUSH_TIMEOUT = 900          # workbuddy_use 自身 --timeout 之外的总闸（秒）


def run_command(cmd: list[str], *, timeout: float = PUSH_TIMEOUT) -> tuple[int, str, str]:
    # Shares the active parent UI transaction through the verified 09a protocol.
    result = server.run_process(cmd, timeout=timeout, cwd=SCRIPTS.parent.parent)
    return (result.get("exit_code") if result.get("exit_code") is not None else 127,
            result.get("stdout") or "", result.get("stderr") or "")


def _last_line(text: str) -> str:
    rows = [row for row in (text or "").splitlines() if row.strip()]
    return rows[-1] if rows else ""


def push_command(prompt: str, *, purpose: str, timeout: float) -> list[str]:
    """真去发的那条命令：`workbuddy_use --continue <一句话>`。"""
    return [sys.executable, str(SCRIPTS / "workbuddy_use.py"), prompt,
            "--continue", "--purpose", purpose, "--timeout", str(timeout)]


def notify_command(card: str, note: str) -> list[str]:
    """叫人的那条命令：通知脚本自己会先写 progress.json 再发消息。"""
    return [sys.executable, str(SCRIPTS / "notify_done.py"),
            "--card", card, "--status", "stuck", "--note", note]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workbuddy_autopush.py",
        description="WorkBuddy 停了就推一句「继续按卡施工」（判定默认 20 分钟、同卡 30 分钟 1 次）")
    parser.add_argument("--dry-run", action="store_true", help="只判定：不发消息、不写日志、不通知")
    parser.add_argument("--progress", default=str(progress.path()),
                        help="progress.json 的完整路径（默认项目根\\progress.json）")
    parser.add_argument("--runs", default=str(autopush.RUNS),
                        help="运行日志目录（默认项目根\\runs）")
    parser.add_argument("--idle-minutes", type=float, default=autopush.Config.idle_minutes,
                        help="多久没新行算停了（默认 20 分钟）")
    parser.add_argument("--cooldown-minutes", type=float, default=autopush.Config.cooldown_minutes,
                        help="同一张卡最短推送间隔（默认 30 分钟）")
    parser.add_argument("--max-pushes", type=int, default=autopush.Config.max_pushes,
                        help="连推几次没进展就停（默认 3）")
    parser.add_argument("--prompt", default=autopush.PROMPT, help="推过去的那句话")
    parser.add_argument("--generating", action="store_true",
                        help="「生成中」标志（workbuddy_use --list 判定出来的）")
    parser.add_argument("--purpose", default="review", choices=list(tier_guard.PURPOSES),
                        help="推过去时用的档位用途（默认 review=最高档，见 docs\\tiers.json）")
    parser.add_argument("--timeout", type=float, default=180, help="workbuddy_use 的等待秒数")
    parser.add_argument("--no-notify", action="store_true", help="停推时不叫红莉栖（只验流程）")
    parser.add_argument("--now", help="把「现在」固定成这个时刻（复现用，ISO 8601）")
    return parser


def _main(argv: list[str] | None = None, *, runner=run_command, now=None) -> int:
    args = build_parser().parse_args(argv)
    config = autopush.Config(idle_minutes=args.idle_minutes,
                             cooldown_minutes=args.cooldown_minutes,
                             max_pushes=args.max_pushes, prompt=args.prompt)
    moment = autopush.parse_time(args.now) if args.now else now
    moment = autopush._aware(moment, default=autopush.now_beijing())

    record = autopush.read_progress(args.progress)
    events = autopush.read_events(args.runs)
    decision = autopush.decide(record, events, now=moment, config=config,
                               generating=args.generating,
                               fallback_time=autopush.modified_at(args.progress))
    out = {"ok": True, "dry_run": bool(args.dry_run), **decision.as_dict(),
           "status": record.get("status"), "progress_updated_at": record.get("updated_at"),
           "idle_threshold_minutes": config.idle_minutes,
           "cooldown_minutes": config.cooldown_minutes,
           "max_pushes": config.max_pushes, "prompt": config.prompt}
    if decision.should_push:
        out["would_run"] = " ".join(push_command(config.prompt, purpose=args.purpose,
                                                 timeout=args.timeout)[1:])

    if decision.action in ("skip", "wait") or args.dry_run:
        print(json.dumps(out, ensure_ascii=False))
        return EXIT_OK

    if decision.needs_human:
        mark = autopush.progress_mark(record)
        already = autopush.stop_notified(events, decision.card, mark)
        out.update({"needs_human": True, "notified": False, "already_notified": already})
        if already or args.no_notify:
            print(json.dumps(out, ensure_ascii=False))
            return EXIT_NEEDS_HUMAN
        note = (f"自动推连续 {config.max_pushes} 次没有进展，已停推，请红莉栖看："
                f"卡 {decision.card}（progress 仍 running）")
        code, stdout, stderr = runner(notify_command(decision.card, note))
        out.update({"notified": code == 0, "notify_exit": code,
                    "notify_error": None if code == 0 else (_last_line(stderr) or _last_line(stdout)) or f"EXIT={code}"})
        autopush.append(autopush.event_for(decision, record, result=autopush.STOP,
                                           ok=code == 0, error=out["notify_error"],
                                           prompt=config.prompt), root=args.runs)
        print(json.dumps(out, ensure_ascii=False))
        return EXIT_NEEDS_HUMAN

    # Decide/count is serialized by main; UI choose and use share one hold.
    code, stdout, stderr = _push_with_receipt(args, config.prompt, record, runner)
    payload = server.parse_last_json(stdout) or {}
    already = payload.get("stage") == "already_answered" and payload.get("ok") is True
    pushed = not already and (payload.get("submission") == "accepted" or payload.get("sent") is True)
    ok = code == 0 and payload.get("ok") is True
    error = None if ok else ((_last_line(stderr) or _last_line(stdout)) or f"EXIT={code}")
    result = (autopush.PUSH if pushed else "autopush_already_answered" if already else autopush.PUSH_FAILED)
    autopush.append(autopush.event_for(decision, record, result=result, ok=ok,
                                     error=error, prompt=config.prompt), root=args.runs)
    out.update({"ok": ok, "pushed": pushed, "already_answered": already,
                "push_exit": code, "push_error": error,
                "push_reply": _last_line(stdout) if ok else None})
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_OK if ok else EXIT_PUSH_FAILED


def _push_with_receipt(args, prompt, record, runner):
    app_root = SCRIPTS.parent.parent
    try:
        with uilock.hold("autopush:workbuddy", root=app_root):
            current = autopush.read_progress(args.progress)
            if any(current.get(key) != record.get(key) for key in ("card_id", "status", "updated_at")):
                return 5, "", "progress_changed"
            tiers = tier_guard.load_tiers(app_root / "docs/tiers.json")
            allowed = tier_guard.allowed_tiers("workbuddy", args.purpose, "model", tiers=tiers)
            if not allowed:
                return 5, "", "table_unconfigured: WorkBuddy purpose has no verified tier"
            before = tier_guard.read_receipt("workbuddy", root=app_root)
            choose = [sys.executable, str(SCRIPTS / "workbuddy_choose.py"), "--model", allowed[0]]
            code, stdout, stderr = runner(choose, timeout=120)
            if code:
                return code, stdout, stderr
            after = tier_guard.read_receipt("workbuddy", root=app_root)
            if not after or (before and after.get("created_monotonic") == before.get("created_monotonic")):
                return 5, "", "choose produced no fresh receipt"
            verdict = tier_guard.check("workbuddy", args.purpose, root=app_root, tiers=tiers)
            if not verdict["ok"]:
                return 5, "", "choose receipt rejected: " + ",".join(verdict["reasons"])
            current = autopush.read_progress(args.progress)
            if any(current.get(key) != record.get(key) for key in ("card_id", "status", "updated_at")):
                return 5, "", "progress_changed"
            reply_timeout = tier_guard.floor_timeout(args.purpose, args.timeout)
            return runner(push_command(prompt, purpose=args.purpose, timeout=reply_timeout),
                          timeout=reply_timeout + 30)
    except (uilock.Busy, tier_guard.TierUnconfirmed, OSError, ValueError) as exc:
        return getattr(exc, "code", 5), "", f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None, *, runner=run_command, now=None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        return _main(argv, runner=runner, now=now)
    # Re-read decision and append count inside one cross-process transaction.
    with tier_guard.file_lock(Path(args.runs) / "autopush.lock", timeout=PUSH_TIMEOUT + 180):
        return _main(argv, runner=runner, now=now)


if __name__ == "__main__":
    console.setup()
    raise SystemExit(main())
