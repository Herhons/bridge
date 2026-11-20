"""Deterministic overnight self-iteration runner for Bridge v0.2.5.

Pure standard library. All orchestration lives here; bridge.py stays a thin caller.
Safety invariants: only auto/v0.2.5, never main, no force push, no history dirs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "auto/v0.2.5"
EXECUTORS = ("opencode", "workbuddy")
COUNCIL = ("grok", "deepseek")
DECISIONS = {
    "DISPATCH_OPENCODE", "DISPATCH_WORKBUDDY", "APPLY_PATCH",
    "CONSULT_COUNCIL", "MERGE_READY", "STOP_FOR_USER", "PASS", "STOP",
}
PROFILES = ("test", "routine", "council")
QUOTA_PATTERNS = (
    "quota", "rate limit", "insufficient", "limit exceeded",
    "too many requests", "429", "额度", "余额不足", "超出限制",
)
KEY_RE = re.compile(
    r"^(DECISION|GEMINI_PROFILE_NEXT|TASK_ID|PAYLOAD):[ \t]*(.*)$", re.M
)


# ---------------------------------------------------------------- git helpers
def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode:
        raise RuntimeError(
            "git %s failed: %s" % (" ".join(args), proc.stderr.strip()[-400:])
        )
    return proc


def current_branch() -> str:
    return git("branch", "--show-current").stdout.strip()


def head_commit() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def assert_safe_branch() -> None:
    branch = current_branch()
    if branch != BRANCH:
        raise RuntimeError(
            "refuse to run on branch %r; expected %r" % (branch, BRANCH)
        )


def rollback(start_commit: str) -> str:
    """Only reset to this round's own start commit (explicitly authorized)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", start_commit):
        return "rollback refused: bad commit id"
    proc = git("reset", "--hard", start_commit)
    return "rollback %s: rc=%d %s" % (
        start_commit[:7], proc.returncode, proc.stderr.strip()[-160:]
    )


# ---------------------------------------------------------------- state / logs
def state_path() -> Path:
    return ROOT / "runs" / "executor_state.json"


def friction_path() -> Path:
    return ROOT / "runs" / "overnight_friction_log.md"


def load_state() -> dict:
    path = state_path()
    state = {
        "opencode": {"calls_today": 0, "failures_today": 0,
                     "cooldown_until": None, "last_error_fp": None,
                     "streak": 0, "stalled": False},
        "workbuddy": {"calls_today": 0, "failures_today": 0,
                      "cooldown_until": None, "last_error_fp": None,
                      "streak": 0, "stalled": False},
        "meta": {"date": datetime.now().strftime("%Y-%m-%d"),
                 "conversation_rounds": 0, "total_rounds": 0,
                 "next_gemini_profile": "test", "force_council": False,
                 "last_round": 0, "last_task_id": "", "last_decision": "",
                 "last_executor": "", "last_rc": 0, "last_reply": "",
                 "last_error_tail": ""},
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in ("opencode", "workbuddy", "meta"):
                if isinstance(loaded.get(key), dict):
                    state[key].update(loaded[key])
        except (ValueError, OSError):
            pass
    return state


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def log_friction(message: str, level: str = "INFO") -> None:
    path = friction_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("- [%s][%s] %s\n" % (stamp, level, message))


def next_midnight() -> str:
    nxt = datetime.now() + timedelta(days=1)
    return nxt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def is_cooled(entry: dict) -> bool:
    until = entry.get("cooldown_until")
    if not until:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(until)
    except ValueError:
        return False


def pick_executor(state: dict) -> str | None:
    for name in ("opencode", "workbuddy"):
        if not is_cooled(state[name]):
            return name
    return None


# ---------------------------------------------------------------- parsing
def parse_instruction(text: str) -> dict:
    result = {
        "decision": "STOP_FOR_USER", "profile_next": "test",
        "task_id": "", "payload": "",
    }
    matches = list(KEY_RE.finditer(text or ""))
    for idx, match in enumerate(matches):
        key, value = match.group(1), match.group(2).strip()
        if key == "DECISION":
            token = value.split()[0].upper() if value else ""
            if token in DECISIONS:
                result["decision"] = token
        elif key == "GEMINI_PROFILE_NEXT":
            token = value.split()[0].lower() if value else ""
            if token in PROFILES:
                result["profile_next"] = token
        elif key == "TASK_ID":
            result["task_id"] = value[:120]
        elif key == "PAYLOAD":
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            # Inline text after "PAYLOAD:" plus every remaining line up to the
            # next recognized key -> one multiline payload.
            tail = text[match.end(2):end].strip("\r\n ")
            result["payload"] = "\n".join(
                part for part in (value, tail) if part
            )
    return result


def fingerprint(text: str) -> str:
    norm = re.sub(r"\d+", "#", (text or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()[:400]
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:12]


def is_quota_error(text: str) -> bool:
    low = (text or "").lower()
    return any(pattern in low for pattern in QUOTA_PATTERNS)


def cap_text(text: str, limit: int = 600) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    suffix = "\n...[truncated]"
    return clean[:limit - len(suffix)] + suffix


# ---------------------------------------------------------------- gates
def architecture_gate(root: Path | None = None) -> list[str]:
    base = Path(root) if root else ROOT
    targets = [base / "bridge.py"]
    engine = base / "engine"
    if engine.is_dir():
        targets.extend(sorted(engine.glob("*.py")))
    violations: list[str] = []
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bexec\s*\(", line):
                violations.append("%s:%d exec()" % (path.name, lineno))
            if path.parent.name == "engine" and re.search(r"self\.name\s*==", line):
                violations.append("%s:%d self.name==" % (path.name, lineno))
    return violations


def run_unittest() -> tuple[bool, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-20:])
    ok = proc.returncode == 0 and bool(re.search(r"\bOK\b", tail))
    return ok, tail


def run_gates() -> tuple[bool, str]:
    ok, tail = run_unittest()
    violations = architecture_gate()
    if violations:
        ok = False
        tail += "\nARCH-GATE FAIL: " + "; ".join(violations[:12])
    return ok, tail


# ---------------------------------------------------------------- bridge calls
def call_bridge(endpoint: str, prompt: str, profile: str = "test",
                extra: tuple[str, ...] = (), timeout: int = 180,
                save: Path | None = None, prompt_file: Path | None = None) -> dict:
    """All executor/council calls are forced to the test profile by callers."""
    cmd = [sys.executable, str(ROOT / "bridge.py"), "ask", endpoint]
    if prompt_file:
        cmd += ["--file", str(prompt_file)]
    else:
        cmd += [prompt]
    cmd += ["--profile", profile, "--headed", "--timeout", str(timeout), "--json"]
    cmd += list(extra)
    if save:
        cmd += ["--save", str(save)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=timeout + 30,
    )
    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (ValueError, IndexError):
        payload = {"ok": False, "raw": raw[-2000:], "stderr": proc.stderr[-1000:]}
    payload["exit_code"] = proc.returncode
    return payload


def call_gemini(prompt: str, profile: str, round_dir: Path,
                new_session: bool = False) -> dict:
    prompt_file = round_dir / "to_gemini.txt"
    prompt_file.write_text(cap_text(prompt), encoding="utf-8")
    extra = ("--new",) if new_session else ()
    return call_bridge(
        "gemini", "", profile=profile, extra=extra, timeout=180,
        save=round_dir / "from_gemini.md", prompt_file=prompt_file,
    )


def call_executor(endpoint: str, prompt: str, round_dir: Path) -> dict:
    """Hard rule: every executor/council call uses --profile test."""
    return call_bridge(
        endpoint, prompt, profile="test", timeout=120,
        save=round_dir / ("from_%s.md" % endpoint),
    )


def call_council(endpoint: str, prompt: str, round_dir: Path) -> dict:
    return call_bridge(
        endpoint, prompt, profile="test", extra=("--new",), timeout=120,
        save=round_dir / ("council_%s.md" % endpoint),
    )


# ---------------------------------------------------------------- patches
def extract_patch(payload: str) -> str:
    blocks = re.findall(r"diff --git .*?(?=\ndiff --git |\Z)", payload or "", re.S)
    return "\n".join(block.strip("\r\n") for block in blocks).strip() + "\n" if blocks else ""


def apply_patch(payload: str, round_dir: Path) -> tuple[bool, str]:
    patch = extract_patch(payload)
    if not patch.strip():
        return False, "no diff --git block found"
    # Gemini frequently emits CRLF inside fenced diffs; normalize to LF and
    # tolerate trailing whitespace so context lines match the working tree.
    patch = patch.replace("\r\n", "\n").replace("\r", "\n")
    path = round_dir / "gemini.patch"
    path.write_text(patch, encoding="utf-8")
    check = git("apply", "--ignore-whitespace", "--check", str(path))
    if check.returncode:
        return False, "git apply --check failed: " + check.stderr.strip()[-500:]
    applied = git("apply", "--ignore-whitespace", str(path))
    if applied.returncode:
        return False, "git apply failed: " + applied.stderr.strip()[-500:]
    return True, "applied %d diff block(s)" % patch.count("diff --git ")


def commit_round(message: str) -> str:
    if not git("status", "--porcelain").stdout.strip():
        return "no changes to commit"
    git("add", "-A", check=True)
    result = git("commit", "-m", message, check=True)
    return (result.stdout or result.stderr).strip().splitlines()[0]


def diff_stat() -> str:
    out = git("diff", "--stat").stdout.strip()
    return out[-800:] if out else "clean"


# ---------------------------------------------------------------- failure path
def record_failure(state: dict, endpoint: str, text: str) -> str:
    entry = state[endpoint]
    fp = ("%s:empty_reply" % endpoint) if not (text or "").strip() else fingerprint(text)
    entry["streak"] = entry.get("streak", 0) + 1 if fp == entry.get("last_error_fp") else 1
    entry["last_error_fp"] = fp
    entry["failures_today"] = entry.get("failures_today", 0) + 1
    action = "recorded fp=%s streak=%d" % (fp, entry["streak"])
    if is_quota_error(text) or entry["streak"] >= 2:
        entry["cooldown_until"] = next_midnight()
        action += " -> cooldown to next 00:00"
    if entry["streak"] >= 2:
        entry["stalled"] = True
        state["meta"]["force_council"] = True
        action += " -> stalled, escalate to council"
    return action


def build_prompt(kickoff: str, state: dict, round_no: int, stats: str) -> str:
    head = head_commit()[:7]
    meta = state["meta"]
    if round_no <= 1:
        context = "启动里程碑：\n" + kickoff[:300]
    else:
        tail = "\n".join(str(meta.get("last_error_tail") or "").splitlines()[-15:])
        context = (
            "上一轮结果（不要再下发相同任务）：\n"
            "last_round=%s last_task_id=%s last_decision=%s "
            "last_executor=%s last_rc=%s\n"
            "last_reply=%s\n"
            "last_error_tail(<=15行)：\n%s"
            % (meta.get("last_round", round_no - 1),
               meta.get("last_task_id", ""), meta.get("last_decision", ""),
               meta.get("last_executor", ""), meta.get("last_rc", 0),
               str(meta.get("last_reply") or "")[:350], tail)
        )
    return cap_text(
        "【过夜自迭代轮次 %d｜%s@%s】\n%s\n\n门禁与状态：\n%s\n"
        "请严格按 DECISION / GEMINI_PROFILE_NEXT / TASK_ID / PAYLOAD 四行块回复。"
        % (round_no, BRANCH, head, context, stats[:100]), 600
    )


def handoff_card(state: dict, round_no: int, reason: str) -> str:
    text = (
        "【交接卡 round=%s branch=%s head=%s reason=%s】\n"
        "opencode calls=%d fail=%d cooldown=%s streak=%d\n"
        "workbuddy calls=%d fail=%d cooldown=%s streak=%d\n"
        "last_fp=%s stalled=%s\n下一步：核对 runs/overnight_friction_log.md 与最新 commit。"
        % (round_no, BRANCH, head_commit()[:7], reason,
           state["opencode"]["calls_today"], state["opencode"]["failures_today"],
           state["opencode"]["cooldown_until"], state["opencode"]["streak"],
           state["workbuddy"]["calls_today"], state["workbuddy"]["failures_today"],
           state["workbuddy"]["cooldown_until"], state["workbuddy"]["streak"],
           state["opencode"]["last_error_fp"] or state["workbuddy"]["last_error_fp"],
           state["meta"]["force_council"])
    )
    return cap_text(text, 400)


def finalize(state: dict, reason: str, round_no: int) -> None:
    assert_safe_branch()
    commit_round("chore(overnight): finalize round %d (%s)" % (round_no, reason))
    push = git("push", "-u", "origin", BRANCH)
    summary = ROOT / "runs" / "overnight_summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "# Bridge v0.2.5 Overnight Summary\n\n"
        "- reason: %s\n- rounds: %d\n- branch: %s\n- head: %s\n"
        "- opencode: calls=%d failures=%d cooldown=%s stalled=%s\n"
        "- workbuddy: calls=%d failures=%d cooldown=%s stalled=%s\n"
        "- push: rc=%d\n\n待用户明早拍板是否合入 main。\n"
        % (reason, round_no, BRANCH, head_commit()[:7],
           state["opencode"]["calls_today"], state["opencode"]["failures_today"],
           state["opencode"]["cooldown_until"], state["opencode"]["stalled"],
           state["workbuddy"]["calls_today"], state["workbuddy"]["failures_today"],
           state["workbuddy"]["cooldown_until"], state["workbuddy"]["stalled"],
           push.returncode),
        encoding="utf-8",
    )
    log_friction("finalize reason=%s push_rc=%d" % (reason, push.returncode))


# ---------------------------------------------------------------- main loop
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--kickoff", default="runs/auto_logs/overnight_kickoff.txt")
    parser.add_argument("--start-delay", type=float, default=0.0)
    args = parser.parse_args()
    if args.start_delay > 0:
        log_friction(
            "detached start: sleep %.1fs before round 1" % args.start_delay
        )
        time.sleep(args.start_delay)
    assert_safe_branch()
    kickoff_path = ROOT / args.kickoff
    kickoff = kickoff_path.read_text(encoding="utf-8") if kickoff_path.exists() else ""
    state = load_state()
    reason = "MAX_ROUNDS"
    round_no = 0

    try:
        for round_no in range(1, args.max_rounds + 1):
            started = time.time()
            start_commit = head_commit()
            rdir = ROOT / "runs" / "auto_logs" / ("round_%d" % round_no)
            rdir.mkdir(parents=True, exist_ok=True)
            assert_safe_branch()
            state["meta"]["total_rounds"] = round_no

            if pick_executor(state) is None:
                reason = "BOTH_EXECUTORS_COOLED"
                break

            new_session = state["meta"]["conversation_rounds"] >= 15
            if new_session:
                card = handoff_card(state, round_no, "15-round handoff")
                (rdir / "handoff_card.txt").write_text(card, encoding="utf-8")
                state["meta"]["conversation_rounds"] = 0

            profile = state["meta"].get("next_gemini_profile", "test")
            if state["meta"].get("force_council"):
                profile = "council"
            stats = "diff_stat=%s" % diff_stat()[:120]
            prompt = build_prompt(kickoff, state, round_no, stats)
            gemini = call_gemini(prompt, profile, rdir, new_session=new_session)
            reply = str(gemini.get("reply") or gemini.get("raw") or "")
            instr = parse_instruction(reply)
            (rdir / "instruction.json").write_text(
                json.dumps(instr, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            state["meta"]["conversation_rounds"] += 1
            state["meta"]["next_gemini_profile"] = instr["profile_next"]
            decision = instr["decision"]
            detail = ""
            executor = ""
            last_rc = 0
            last_reply = ""

            if decision in ("DISPATCH_OPENCODE", "DISPATCH_WORKBUDDY"):
                wanted = "opencode" if decision.endswith("OPENCODE") else "workbuddy"
                executor = wanted if not is_cooled(state[wanted]) else pick_executor(state)
                if not executor:
                    reason = "BOTH_EXECUTORS_COOLED"
                    break
                result = call_executor(executor, instr["payload"][:3000], rdir)
                last_rc = int(result.get("exit_code") or 0)
                state[executor]["calls_today"] += 1
                text = str(result.get("reply") or result.get("raw") or "")
                if not result.get("ok"):
                    detail = record_failure(state, executor, text)
                    other = "workbuddy" if executor == "opencode" else "opencode"
                    if not is_cooled(state[other]):
                        fallback = call_executor(other, instr["payload"][:3000], rdir)
                        executor = other
                        last_rc = int(fallback.get("exit_code") or 0)
                        state[other]["calls_today"] += 1
                        detail += " | fallback %s ok=%s" % (
                            other, bool(fallback.get("ok"))
                        )
                        if last_rc == 0:
                            last_reply = str(
                                fallback.get("reply") or fallback.get("raw") or ""
                            )[:350]
                    if pick_executor(state) is None:
                        reason = "BOTH_EXECUTORS_COOLED"
                elif last_rc == 0:
                    # Successful dispatch: keep the executor's own words for
                    # next round's summary instead of burying them under gates.
                    last_reply = text[:350]

            elif decision == "APPLY_PATCH":
                ok, detail = apply_patch(instr["payload"], rdir)
                last_rc = 0 if ok else 1

            elif decision == "CONSULT_COUNCIL":
                advisor = COUNCIL[round_no % len(COUNCIL)]
                second = call_council(
                    advisor, instr["payload"][:3000] or kickoff[:500], rdir
                )
                second_text = str(second.get("reply") or second.get("raw") or "")[:1200]
                executor = advisor
                last_rc = int(second.get("exit_code") or 0)
                state["meta"]["force_council"] = True
                verdict = call_gemini(
                    "谋士 %s 第二意见：%s\n请给出最终 DECISION 块。" % (advisor, second_text),
                    "council", rdir,
                )
                instr = parse_instruction(
                    str(verdict.get("reply") or verdict.get("raw") or "")
                )
                decision = instr["decision"]
                detail = "council=%s rc=%s final=%s" % (
                    advisor, second.get("exit_code"), decision
                )
                if instr["decision"] == "APPLY_PATCH":
                    ok, detail = apply_patch(instr["payload"], rdir)
                    last_rc = 0 if ok else 1

            elif decision in ("MERGE_READY", "STOP_FOR_USER", "PASS", "STOP"):
                reason = decision
                detail = "stop decision received"

            ok, gate_tail = run_gates()
            (rdir / "gates.txt").write_text(
                "decision=%s\n%s\n%s" % (decision, detail, gate_tail), encoding="utf-8"
            )
            if ok:
                commit_round(
                    "chore(overnight): round %d %s %s"
                    % (round_no, decision, instr["task_id"][:40])
                )
            else:
                rb = rollback(start_commit)
                log_friction("round %d gate FAIL; %s" % (round_no, rb), "ERROR")
                state["meta"]["force_council"] = True
            tail_lines = (detail + "\n" + gate_tail).strip().splitlines()[-15:]
            state["meta"].update({
                "last_round": round_no,
                "last_task_id": instr["task_id"][:80],
                "last_decision": decision,
                "last_executor": executor,
                "last_rc": last_rc,
                "last_reply": last_reply[:350],
                "last_error_tail": "\n".join(tail_lines)[:600],
            })
            log_friction(
                "round %d %s %.1fs gate=%s diff=%s"
                % (round_no, decision, time.time() - started, ok, diff_stat()[:120])
            )
            save_state(state)
            if reason != "MAX_ROUNDS":
                break
    except Exception as exc:  # noqa: BLE001 - overnight runner must self-heal
        log_friction("round %d exception: %s" % (round_no, exc), "ERROR")
        try:
            log_friction(rollback(head_commit()), "ERROR")
        except Exception:  # noqa: BLE001
            pass
        reason = "EXCEPTION"
    finally:
        save_state(state)
        finalize(state, reason, round_no)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
