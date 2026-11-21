#!/usr/bin/env python3
"""Unattended self-heal loop: Gemini -> WorkBuddy -> full unittest -> git gate.

Standard library only. Every subprocess is non-interactive and UTF-8 decoded.
Never imports or launches OpenCode. Runs are capped at --max-retries attempts;
a red suite is never committed, and a fully failed run resets to BASE_COMMIT.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge.py"
RUNS = ROOT / "runs"
PYTHON = sys.executable

TIMEOUT_RC = 124
EXECUTOR_FALLBACK = {
    "workbuddy": "opencode",
    "opencode": "antigravity",
    "antigravity": "none",
}
EXECUTOR_CHAIN: tuple[str, ...] = (
    "workbuddy", "opencode", "antigravity",
)
EXECUTOR_CHOICES: tuple[str, ...] = ("auto",) + EXECUTOR_CHAIN
TASK_RE = re.compile(
    r"<<<WORKBUDDY_TASK_START>>>(.*?)<<<WORKBUDDY_TASK_END>>>", re.S
)
FAIL_NAMED_RE = re.compile(
    r"^(?:FAIL|ERROR):\s+(\S+)\s+\(([^)]+)\)", re.M
)
FAIL_INLINE_RE = re.compile(
    r"^(\S+)\s+\(([^)]+)\)\s*\.\.\.\s*(?:FAIL|ERROR)\b", re.M
)
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")

def scheduler_notice() -> str:
    """Fixed preamble telling WorkBuddy not to kill its serial dispatcher."""
    return (
        "【调度器保护声明】你当前正由后台过夜调度器 scripts/auto_heal_loop.py"
        "（PID=%d）串行调度执行，调度器正在等待你回复，不会与你并发改工作区。"
        "严禁终止/杀掉该 PID，严禁删除 runs/overnight.pid。\n" % os.getpid()
    )


LOG_NAMES: tuple[str, ...] = ("auto_heal_loop.log",)
OVERNIGHT_LOG = "overnight.log"
PID_FILE = "overnight.pid"
BACKLOG_CANDIDATES = (
    "runs/overnight_todo.md",
    "runs/todo.md",
    "TODO.md",
    "runs/auto_logs/overnight_kickoff.txt",
)


# ---------------------------------------------------------------- logging
def log(message: str, level: str = "INFO") -> None:
    line = "[%s][%s] %s" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, message
    )
    print(line, flush=True)
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        for name in LOG_NAMES:
            with (RUNS / name).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        pass


def write_pid() -> int:
    pid = os.getpid()
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / PID_FILE).write_text(str(pid), encoding="utf-8")
    return pid


def clear_pid() -> None:
    try:
        (RUNS / PID_FILE).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- subprocess
def kill_process_tree(pid: int) -> int:
    """Kill the whole Windows process tree; return taskkill's return code."""
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return int(proc.returncode)
    except (OSError, subprocess.SubprocessError):
        return -1


def run_cmd(
    cmd: list[str],
    timeout: float = 300,
    cwd: str | Path | None = None,
) -> tuple[int, str]:
    """Run a command without a console; kill the process tree on timeout.

    Returns (returncode, captured_output). TIMEOUT_RC is returned for a
    timeout together with every byte captured before the kill.
    """
    proc = subprocess.Popen(
        list(cmd),
        cwd=str(cwd or ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return int(proc.returncode), output or ""
    except subprocess.TimeoutExpired as exc:
        captured = exc.output or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", errors="replace")
        kill_rc = kill_process_tree(proc.pid)
        try:
            tail, _ = proc.communicate(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            tail = ""
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", errors="replace")
        detail = (
            "\n[auto_heal] TIMEOUT after %ss; taskkill rc=%s pid=%s\n"
            % (timeout, kill_rc, proc.pid)
        )
        return TIMEOUT_RC, "%s%s%s" % (captured, tail or "", detail)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def git_text(*args: str) -> str:
    proc = git(*args)
    text = proc.stdout or ""
    if proc.returncode and proc.stderr:
        text += "\n[git stderr]\n" + proc.stderr
    return text


def head_commit() -> str:
    proc = git("rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def reset_hard(commit: str) -> bool:
    if not COMMIT_RE.fullmatch(commit or ""):
        log("reset refused: bad commit id %r" % commit, "ERROR")
        return False
    proc = git("reset", "--hard", commit)
    ok = proc.returncode == 0
    log("git reset --hard %s rc=%s" % (commit[:12], proc.returncode),
        "INFO" if ok else "ERROR")
    return ok


# ---------------------------------------------------------------- crash report
def extract_failed_tests(output: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for regex in (FAIL_NAMED_RE, FAIL_INLINE_RE):
        for match in regex.finditer(output or ""):
            item = (match.group(1), match.group(2))
            if item not in found:
                found.append(item)
    return found


def test_source_snippet(name: str, module: str) -> str:
    """Locate `def <name>` from the failed test's module and show its source."""
    rels: list[Path] = []
    parts = [part for part in (module or "").split(".") if part]
    for index in range(len(parts), 0, -1):
        rels.append(Path(*parts[:index]).with_suffix(".py"))
    bare = name.split(".")[-1]
    rels.extend([Path("tests") / (bare + ".py"), Path(bare + ".py")])

    source_path = None
    for rel in rels:
        candidate = ROOT / rel
        if candidate.is_file():
            source_path = candidate
            break
    if source_path is None:
        return "# source file not found for %s (%s)" % (name, module)

    text = source_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    match = re.search(r"^(\s*)def\s+" + re.escape(bare) + r"\s*\(", text, re.M)
    if match is None:
        head = "\n".join(
            "%4d: %s" % (i + 1, lines[i]) for i in range(min(60, len(lines)))
        )
        return (
            "# no `def %s(` in %s; showing file head\n%s"
            % (bare, rels[0], head)
        )

    indent = len(match.group(1))
    start = text.count("\n", 0, match.start())
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped:
            current = len(lines[end]) - len(lines[end].lstrip())
            if current <= indent and (
                stripped.startswith("def ")
                or stripped.startswith("class ")
                or stripped.startswith("@")
            ):
                break
        end += 1
    body = "\n".join(
        "%4d: %s" % (i + 1, lines[i]) for i in range(start, end)
    )
    return "# %s :: %s\n%s" % (source_path, bare, body)


def build_crash_report(
    test_output: str,
    report_path: str | Path = "runs/crash_report.md",
) -> Path:
    """Write a complete, untruncated failure report and return its path."""
    path = Path(report_path)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)

    failed = extract_failed_tests(test_output)
    snippets = []
    for name, module in failed:
        snippets.append(
            "### %s (%s)\n```python\n%s\n```"
            % (name, module, test_source_snippet(name, module))
        )
    if not snippets:
        snippets.append("_No FAIL:/ERROR: test name was matched._")

    content = (
        "# Bridge auto-heal crash report\n\n"
        "- generated: %s\n- base_commit: %s\n\n"
        "## 1. Full unittest output (FAIL/ERROR and Traceback, untruncated)\n\n"
        "```text\n%s\n```\n\n"
        "## 2. Failed test source snippets\n\n%s\n\n"
        "## 3. git status --short\n\n"
        "```text\n%s\n```\n\n"
        "## 4. git diff\n\n"
        "```diff\n%s\n```\n"
        % (
            datetime.now().isoformat(timespec="seconds"),
            head_commit(),
            test_output or "",
            "\n\n".join(snippets),
            git_text("status", "--short"),
            git_text("diff"),
        )
    )
    path.write_text(content, encoding="utf-8")
    log("crash report written: %s (%d failed test(s))" % (path, len(failed)))
    return path


# ---------------------------------------------------------------- bridge calls
def extract_task_card(reply: str) -> str:
    match = TASK_RE.search(reply or "")
    return match.group(1).strip() if match else ""


def build_gemini_prompt(attempt: int, report: Path) -> str:
    return (
        "【Bridge 无人值守自愈 round=%d】\n"
        "附件是完整的 runs/crash_report.md，包含失败堆栈、git diff 与失败用例源码。\n"
        "请分析根因，只输出一段可以直接交给 WorkBuddy 执行的修复任务卡：\n"
        "- 不要输出解释、寒暄或 markdown 代码围栏外的任何文字；\n"
        "- 任务卡必须写在下面两个标记之间，且只出现一次；\n"
        "- 任务卡要写明改动文件、具体修复点、禁止删除或弱化既有断言、"
        "以及最后必须运行全量单测。\n\n"
        "<<<WORKBUDDY_TASK_START>>>\n"
        "...任务卡内容...\n"
        "<<<WORKBUDDY_TASK_END>>>\n\n"
        "报告路径：%s\n" % (attempt, report)
    )


def load_backlog() -> tuple[str, str]:
    """Reuse an existing todo/kickoff file, else derive work from git state."""
    for rel in BACKLOG_CANDIDATES:
        path = ROOT / rel
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text, rel
    derived = (
        "git log -n 10 --oneline:\n%s\n\ngit status:\n%s"
        % (git_text("log", "-n", "10", "--oneline"), git_text("status"))
    )
    return derived, "git-log+status"


def build_task_prompt(round_no: int, backlog: str, base_commit: str) -> str:
    return (
        "【Bridge 过夜迭代 round=%d | BASE_COMMIT=%s】\n"
        "现有自动循环/待办清单（若已完成请挑下一项未完成的小步）：\n"
        "%s\n\n"
        "请给出下一项小步、可独立测试、可由全量 unittest 验证的改进任务。\n"
        "只输出任务卡，不要寒暄或解释；任务卡必须只出现一次且在标记之间。\n"
        "任务卡要写明：改动文件、具体修复点、禁止删除或弱化既有断言、"
        "以及最后必须运行全量单测。\n\n"
        "<<<WORKBUDDY_TASK_START>>>\n"
        "...任务卡内容...\n"
        "<<<WORKBUDDY_TASK_END>>>\n"
        % (round_no, base_commit, backlog[:6000])
    )


def parse_reply(rc: int, output: str, saved: Path) -> str:
    text = output or ""
    try:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            payload = json.loads(text[start:end + 1])
            if isinstance(payload, dict) and payload.get("reply"):
                return str(payload["reply"])
    except (ValueError, TypeError):
        pass
    if saved.is_file():
        return saved.read_text(encoding="utf-8", errors="replace").strip()
    return text.strip()


def read_reply(endpoint: str, saved: Path, timeout: float) -> str:
    """bridge.py read <endpoint> --json --save <path> (documented read path)."""
    cmd = [
        PYTHON, str(BRIDGE), "read", endpoint,
        "--json", "--save", str(saved),
    ]
    rc, output = run_cmd(cmd, timeout=timeout, cwd=ROOT)
    return parse_reply(rc, output, saved)


def call_gemini(report: Path, attempt: int, timeout: float) -> str:
    prompt_file = RUNS / ("gemini_prompt_round%d.txt" % attempt)
    saved = RUNS / ("gemini_reply_round%d.txt" % attempt)
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(build_gemini_prompt(attempt, report), encoding="utf-8")
    bridge_timeout = max(30, int(timeout - 30))
    cmd = [
        PYTHON, str(BRIDGE), "ask", "gemini", "",
        "--file", str(prompt_file),
        "--attach", str(report),
        "--profile", "test", "--headed",
        "--timeout", str(bridge_timeout), "--json", "--save", str(saved),
    ]
    rc, output = run_cmd(cmd, timeout=timeout, cwd=ROOT)
    reply = parse_reply(rc, output, saved)
    if not reply:
        reply = read_reply("gemini", saved, timeout=60)
    log("gemini rc=%s reply_chars=%d" % (rc, len(reply)))
    return reply


def request_task_from_gemini(
    backlog: str,
    round_no: int,
    base_commit: str,
    timeout: float,
) -> str:
    prompt_file = RUNS / ("overnight_prompt_round%d.txt" % round_no)
    saved = RUNS / ("overnight_task_round%d.txt" % round_no)
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(
        build_task_prompt(round_no, backlog, base_commit), encoding="utf-8"
    )
    bridge_timeout = max(30, int(timeout - 30))
    cmd = [
        PYTHON, str(BRIDGE), "ask", "gemini", "",
        "--file", str(prompt_file),
        "--profile", "test", "--headed",
        "--timeout", str(bridge_timeout), "--json", "--save", str(saved),
    ]
    rc, output = run_cmd(cmd, timeout=timeout, cwd=ROOT)
    reply = parse_reply(rc, output, saved)
    if not reply:
        reply = read_reply("gemini", saved, timeout=60)
    log("overnight task request rc=%s reply_chars=%d" % (rc, len(reply)))
    return reply


def _executor_header(executor: str, endpoint: str) -> str:
    """One-line dispatch hint so the agent knows who else may pick this up."""
    fallback = EXECUTOR_FALLBACK.get(endpoint, "none")
    return "[executor] mode=%s endpoint=%s fallback=%s\n" % (
        executor, endpoint, fallback
    )


def _ask_endpoint(
    endpoint: str,
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
) -> str:
    task_file = RUNS / ("%s_task_round%d.md" % (endpoint, attempt))
    saved = RUNS / ("%s_reply_round%d.txt" % (endpoint, attempt))
    task_file.parent.mkdir(parents=True, exist_ok=True)
    header = scheduler_notice()
    if executor:
        header += _executor_header(executor, endpoint)
    task_file.write_text(header + task_card, encoding="utf-8")
    bridge_timeout = max(30, int(timeout - 30))
    cmd = [
        PYTHON, str(BRIDGE), "ask", endpoint, "",
        "--file", str(task_file),
        "--profile", "test", "--headed",
        "--timeout", str(bridge_timeout), "--json", "--save", str(saved),
    ]
    rc, output = run_cmd(cmd, timeout=timeout, cwd=ROOT)
    if rc != 0:
        # A non-zero bridge rc means the UI run failed; never let partial
        # stdout masquerade as a usable reply.
        log("%s rc=%s (no reply accepted)" % (endpoint, rc), "ERROR")
        return ""
    reply = parse_reply(rc, output, saved)
    if not reply:
        reply = read_reply(endpoint, saved, timeout=60)
    log("%s rc=%s reply_chars=%d" % (endpoint, rc, len(reply)))
    return reply


def call_workbuddy(
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
) -> str:
    return _ask_endpoint(
        "workbuddy", task_card, attempt, timeout, executor=executor
    )


def call_opencode(
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
) -> str:
    return _ask_endpoint(
        "opencode", task_card, attempt, timeout, executor=executor
    )


def call_antigravity(
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
) -> str:
    return _ask_endpoint(
        "antigravity", task_card, attempt, timeout, executor=executor
    )


def call_executor(
    task_card: str,
    attempt: int,
    timeout: float,
    executor: str = "auto",
) -> str:
    """Dispatch a task card through the three-level executor pool.

    ``auto`` tries WorkBuddy, then OpenCode, then Antigravity. Any level that
    returns an empty reply, a non-zero bridge rc (including timeout rc=124) or
    raises is skipped with a logged reason, and the next level is tried.
    Explicit executors never fall back, so an operator can pin one target.
    """
    mode = (executor or "auto").strip().lower()
    if mode not in EXECUTOR_CHOICES:
        raise ValueError(
            "unknown executor %r (choose from %s)"
            % (executor, ", ".join(EXECUTOR_CHOICES))
        )
    order = list(EXECUTOR_CHAIN) if mode == "auto" else [mode]
    callers = {
        "workbuddy": call_workbuddy,
        "opencode": call_opencode,
        "antigravity": call_antigravity,
    }
    for index, endpoint in enumerate(order):
        try:
            reply = callers[endpoint](
                task_card, attempt, timeout, executor=mode
            )
        except Exception as exc:  # noqa: BLE001 - fall through to next level
            log(
                "executor_miss=%s round=%d reason=%r"
                % (endpoint, attempt, exc),
                "ERROR",
            )
            reply = ""
        if reply:
            log(
                "executor_hit=%s round=%d level=%d/%d"
                % (endpoint, attempt, index + 1, len(order))
            )
            return reply
        log(
            "executor_miss=%s round=%d reason=empty_or_nonzero rc"
            % (endpoint, attempt),
            "ERROR",
        )
        if index + 1 < len(order):
            log(
                "executor_fallback -> %s round=%d"
                % (order[index + 1], attempt)
            )
    return ""


def run_tests(timeout: float) -> tuple[int, str]:
    cmd = [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-v"]
    return run_cmd(cmd, timeout=timeout, cwd=ROOT)


# ---------------------------------------------------------------- main loop
def commit_and_push(message: str, no_push: bool) -> int:
    status = git("status", "--porcelain").stdout.strip()
    if not status:
        log("git tree clean; nothing to commit")
        return 0
    added = git("add", "-A")
    if added.returncode:
        log("git add failed: %s" % added.stderr.strip()[-300:], "ERROR")
        return 1
    committed = git("commit", "-m", message)
    if committed.returncode:
        log("git commit failed: %s" % committed.stderr.strip()[-300:], "ERROR")
        return 1
    commit_lines = (committed.stdout or committed.stderr or "").strip().splitlines()
    log("committed: %s" % (commit_lines[0] if commit_lines else "ok"))
    if no_push:
        log("--no-push set; push skipped")
        return 0
    pushed = git("push")
    if pushed.returncode:
        log("git push failed: %s" % pushed.stderr.strip()[-300:], "ERROR")
        return 1
    log("git push ok")
    return 0


def heal(
    max_retries: int = 3,
    *,
    no_push: bool = False,
    timeout: float = 300,
    report_path: str | Path = "runs/crash_report.md",
    commit_on_green: bool = True,
    executor: str = "auto",
) -> int:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    base_commit = head_commit()
    if not COMMIT_RE.fullmatch(base_commit):
        log("cannot resolve BASE_COMMIT: %r" % base_commit, "ERROR")
        return 2
    log("BASE_COMMIT=%s max_retries=%d no_push=%s"
        % (base_commit, max_retries, no_push))

    report = Path(report_path)
    for attempt in range(1, max(1, max_retries) + 1):
        round_base = head_commit()
        if not COMMIT_RE.fullmatch(round_base):
            log("round %d: cannot resolve round base commit" % attempt, "ERROR")
            reset_hard(base_commit)
            return 2
        log("round %d/%d start base=%s" % (attempt, max_retries, round_base[:12]))

        rc, output = run_tests(timeout)
        if rc == 0:
            log("round %d: full unittest green (rc=0)" % attempt)
            if commit_on_green:
                return commit_and_push(
                    "fix(heal): self-healed unit tests via gemini/workbuddy",
                    no_push,
                )
            return 0

        log("round %d: unittest red (rc=%s); building crash report" % (attempt, rc),
            "ERROR")
        report = build_crash_report(output, report)

        reply = call_gemini(report, attempt, timeout)
        card = extract_task_card(reply)
        if not card:
            log("round %d: task card markers missing; retry counted" % attempt,
                "ERROR")
        else:
            (RUNS / ("extracted_task_round%d.md" % attempt)).write_text(
                card, encoding="utf-8"
            )
            workbuddy_reply = call_executor(
                card, attempt, timeout, executor=executor
            )
            if not workbuddy_reply:
                log("round %d: all executors returned empty replies" % attempt,
                    "ERROR")

        if attempt < max(1, max_retries):
            reset_hard(round_base)

    log("all %d heal attempts failed; rolling back to BASE_COMMIT %s"
        % (max_retries, base_commit), "ERROR")
    if not reset_hard(base_commit):
        return 2
    log("kept %s for post-mortem; exiting non-zero" % report, "ERROR")
    return 1


def has_changes() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


def run_overnight(
    max_rounds: int = 20,
    *,
    no_push: bool = False,
    timeout: float = 300,
    sleep: float = 60,
    delay_start: float = 0,
    executor: str = "auto",
) -> int:
    """Multi-round overnight loop: red -> self-heal, green -> next task."""
    global LOG_NAMES
    previous_logs = LOG_NAMES
    LOG_NAMES = (OVERNIGHT_LOG,)
    pid = write_pid()
    idle_streak = 0
    failure_streak = 0
    reason = "MAX_ROUNDS"
    exit_code = 0
    try:
        log("overnight start pid=%d max_rounds=%d sleep=%.1fs delay=%.1fs no_push=%s"
            % (pid, max_rounds, sleep, delay_start, no_push))
        if delay_start > 0:
            log("delay-start: sleeping %.1fs" % delay_start)
            time.sleep(delay_start)

        for round_no in range(1, max(1, max_rounds) + 1):
            base = head_commit()
            if not COMMIT_RE.fullmatch(base):
                log("round %d: cannot resolve BASE_COMMIT" % round_no, "ERROR")
                failure_streak += 1
                idle_streak += 1
                if failure_streak >= 3:
                    reason = "THREE_CONSECUTIVE_FAILURES"
                    break
                time.sleep(sleep)
                continue

            log("round %d/%d BASE_COMMIT=%s" % (round_no, max_rounds, base[:12]))
            rc, _ = run_tests(timeout)

            if rc != 0:
                log("round %d: red suite; self-heal up to 3 attempts" % round_no,
                    "ERROR")
                heal_rc = heal(
                    max_retries=3, no_push=no_push, timeout=timeout,
                    commit_on_green=False,
                )
                final_rc, _ = run_tests(timeout)
                if heal_rc == 0 and final_rc == 0:
                    log("round %d: self-heal green" % round_no)
                    failure_streak = 0
                else:
                    log("round %d: self-heal failed; rollback to %s"
                        % (round_no, base[:12]), "ERROR")
                    reset_hard(base)
                    failure_streak += 1
                    idle_streak += 1
                    if failure_streak >= 3:
                        reason = "THREE_CONSECUTIVE_FAILURES"
                        break
                    log("sleep %.1fs before next round" % sleep)
                    time.sleep(sleep)
                    continue
            else:
                failure_streak = 0
                backlog, source = load_backlog()
                log("round %d: next-task source=%s" % (round_no, source))
                reply = request_task_from_gemini(backlog, round_no, base, timeout)
                card = extract_task_card(reply)
                if not card:
                    log("round %d: no valid task card; skip round" % round_no,
                        "ERROR")
                    idle_streak += 1
                    if idle_streak >= 3:
                        reason = "THREE_IDLE_ROUNDS"
                        break
                    time.sleep(sleep)
                    continue

                (RUNS / ("overnight_accepted_task_round%d.md" % round_no)).write_text(
                    card, encoding="utf-8"
                )
                wb_reply = call_executor(
                    card, round_no, timeout, executor=executor
                )
                log("round %d: workbuddy reply_chars=%d" % (round_no, len(wb_reply)))
                post_rc, _ = run_tests(timeout)
                if post_rc != 0:
                    log("round %d: new task made suite red; self-heal" % round_no,
                        "ERROR")
                    heal_rc = heal(
                        max_retries=3, no_push=no_push, timeout=timeout,
                        commit_on_green=False,
                    )
                    post_rc, _ = run_tests(timeout)
                    if heal_rc != 0 or post_rc != 0:
                        log("round %d: self-heal failed; rollback to %s"
                            % (round_no, base[:12]), "ERROR")
                        reset_hard(base)
                        failure_streak += 1
                        idle_streak += 1
                        if failure_streak >= 3:
                            reason = "THREE_CONSECUTIVE_FAILURES"
                            break
                        time.sleep(sleep)
                        continue
                    failure_streak = 0
                rc = post_rc

            if rc != 0:
                reset_hard(base)
                failure_streak += 1
                idle_streak += 1
                if failure_streak >= 3:
                    reason = "THREE_CONSECUTIVE_FAILURES"
                    break
                time.sleep(sleep)
                continue

            if has_changes():
                gate_rc = commit_and_push(
                    "chore(overnight): round %d auto improvement" % round_no,
                    no_push,
                )
                if gate_rc == 0:
                    idle_streak = 0
                    log("round %d: gate green, committed" % round_no)
                else:
                    log("round %d: commit/push gate failed" % round_no, "ERROR")
                    idle_streak += 1
                    failure_streak += 1
                    if failure_streak >= 3:
                        reason = "THREE_CONSECUTIVE_FAILURES"
                        break
            else:
                log("round %d: green but no changes; no commit" % round_no)
                idle_streak += 1

            if idle_streak >= 3:
                reason = "THREE_IDLE_ROUNDS"
                break
            if round_no < max(1, max_rounds):
                log("sleep %.1fs before next round" % sleep)
                time.sleep(sleep)
    except Exception as exc:  # noqa: BLE001 - unattended runner must not hang
        log("overnight exception: %s" % exc, "ERROR")
        reason = "EXCEPTION"
        exit_code = 1
    finally:
        log("overnight stop reason=%s pid=%d" % (reason, pid))
        clear_pid()
        LOG_NAMES = previous_logs
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--no-push", action="store_true",
                        help="local test mode: never run git push")
    parser.add_argument("--timeout", type=float, default=300,
                        help="per-subprocess timeout in seconds")
    parser.add_argument("--report", default="runs/crash_report.md")
    parser.add_argument("--overnight", action="store_true",
                        help="run multi-round unattended overnight loop")
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=60,
                        help="seconds between overnight rounds")
    parser.add_argument("--delay-start", type=float, default=0,
                        help="seconds to wait before the first round")
    parser.add_argument("--executor", choices=EXECUTOR_CHOICES, default="auto",
                        help="overnight task executor (auto falls back)")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.overnight:
            return run_overnight(
                max_rounds=args.max_rounds,
                no_push=args.no_push,
                timeout=args.timeout,
                sleep=args.sleep,
                delay_start=args.delay_start,
                executor=args.executor,
            )
        return heal(
            max_retries=args.max_retries,
            no_push=args.no_push,
            timeout=args.timeout,
            report_path=args.report,
            executor=args.executor,
        )
    except Exception:  # noqa: BLE001 - hidden console must not swallow crashes
        detail = traceback.format_exc()
        try:
            RUNS.mkdir(parents=True, exist_ok=True)
            with (RUNS / OVERNIGHT_LOG).open("a", encoding="utf-8") as handle:
                handle.write(detail)
        except OSError:
            pass
        print(detail, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
