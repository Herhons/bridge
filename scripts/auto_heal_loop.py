#!/usr/bin/env python3
"""Unattended self-heal loop: Gemini -> WorkBuddy -> full unittest -> git gate.

Standard library only. Every subprocess is non-interactive and UTF-8 decoded.
Never imports or launches OpenCode. Runs are capped at --max-retries attempts;
a red suite is never committed, and a fully failed run resets to BASE_COMMIT.

M4-1 (v0.2.5) adds the control plane around that loop:

* ``docs/ROADMAP.md`` is the first backlog source; when it holds no ``- [ ]``
  item (or carries a standalone ``ALL_DONE`` line) the run records ``ALL_DONE``
  and exits instead of spinning three idle rounds.
* ``runs/STOP`` is honoured before a round, before a dispatch and while
  sleeping; it records ``STOPPED_BY_USER`` and cleans ``runs/STOP`` +
  ``runs/overnight.pid``.
* ``StateMachine`` writes through a ``.tmp`` file + ``os.replace`` and never
  overwrites the state of a *live* foreign scheduler: that history is kept in
  ``runs/state_machine.json`` and the newcomer writes
  ``runs/state_machine.<pid>.json``.
* Every task card gets a stable ``card_id`` (``round + sha256(card)[:12]``)
  recorded under ``runs/dispatched/``; a card already ``dispatched`` or
  ``timed_out`` is never blindly re-dispatched to the next executor.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
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
#: Returned instead of a reply when a level wrote files but never answered; it
#: is non-empty on purpose so the caller keeps the round and lets the test gate
#: decide, while ``executor_dirty=`` in the log says why no fallback happened.
EXECUTOR_DIRTY_MARKER = (
    "[auto_heal] worktree changed by executor %s without a reply; "
    "fallback suppressed, verifying via tests"
)
BRAIN_CHAIN: tuple[str, ...] = ("gemini", "deepseek", "grok")
BRAIN_CHOICES: tuple[str, ...] = ("auto",) + BRAIN_CHAIN
BRAIN_FALLBACK: dict[str, str] = {
    "gemini": "deepseek",
    "deepseek": "grok",
    "grok": "none",
}
PROFILE_CHOICES: tuple[str, ...] = ("test", "routine", "council", "none")
TASK_RE = re.compile(
    r"<<<WORKBUDDY_TASK_START>>>(.*?)<<<WORKBUDDY_TASK_END>>>", re.S
)
PATCH_RE = re.compile(
    r"<<<BRIDGE_PATCH_START>>>(.*?)<<<BRIDGE_PATCH_END>>>", re.S
)
#: Bodies that are prompt-example echoes, never real answers. A brain that
#: repeats the instruction template used to win the first-match race and get
#: dispatched verbatim ("...任务卡内容..." was literally sent as a task card on
#: round 1 of 2026-11-21 13:36), so both extractors now skip these.
PLACEHOLDER_MARKERS: tuple[str, ...] = ("任务卡内容", "unified diff")


def _is_placeholder_block(text: str) -> bool:
    """True for empty / prompt-example bodies that must never be acted on."""
    body = (text or "").strip()
    if not body:
        return True
    if any(marker in body for marker in PLACEHOLDER_MARKERS):
        return True
    stripped = body.strip("`").strip()
    return bool(
        len(stripped) < 40
        and stripped.startswith("...")
        and stripped.endswith("...")
    )


def _last_valid_block(pattern: re.Pattern[str], reply: str) -> str:
    """Return the last matching block whose body is not a placeholder.

    Brains echo the prompt template *before* their real answer, so first-match
    is the worst possible choice; taking the last valid block prefers the real
    answer and falls back to ``""`` when every block was an example.
    """
    best = ""
    for match in pattern.finditer(reply or ""):
        body = match.group(1)
        if _is_placeholder_block(body):
            continue
        best = body
    return best

STATE_FILE = "state_machine.json"
HANDOFF_FILE = "brain_handoff.md"
PATCH_FILE = "brain_patch.diff"
ATTACH_INLINE_LIMIT = 20000
STOP_FILE = "STOP"
DISPATCH_DIR = "dispatched"
COOLDOWN_FILE = "opencode_cooldown.json"
ROADMAP_PATH = "docs/ROADMAP.md"
ALL_DONE_MARKER = "ALL_DONE"
STATE_NAMES: tuple[str, ...] = (
    "IDLE",
    "PICK_TASK",
    "BRAIN_PLAN",
    "APPLY_PATCH",
    "DISPATCH_EXECUTOR",
    "VERIFY_TESTS",
    "BUG_REPORTED",
    "STOPPED_ON_BUG",
    "STUCK_TRIAGE",
    "COMMIT_AND_PUSH",
    "BLOCKED",
    "STOPPED",
    "STOPPED_BY_USER",
    "ALL_DONE",
)
# States whose history must survive the ``finally`` cleanup untouched.
TERMINAL_STATES: tuple[str, ...] = ("STOPPED_ON_BUG", "STOPPED_BY_USER", "ALL_DONE")
# ``- [ ]`` anywhere at the start of a line (indented list items included).
ROADMAP_PENDING_RE = re.compile(r"^[ \t]*-[ \t]+\[ \]", re.M)
ROADMAP_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+\[[ xX]\]", re.M)
#: First pending item *with* its body, used to name the round in prompts/commits.
ROADMAP_FIRST_PENDING_RE = re.compile(r"^[ \t]*-[ \t]+\[ \][ \t]*(.+)$", re.M)
TASK_TITLE_MAX = 48
DEFAULT_TASK_TITLE = "自动迭代"
# A card stays blocked while a dispatch is still owed or died on the wire.
BLOCKING_DISPATCH_STATES: tuple[str, ...] = ("dispatched", "timed_out")
# Slice length for the interruptible sleep (STOP is honoured within this lag).
STOP_POLL_SECONDS = 2.0
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
    ROADMAP_PATH,
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


# ---------------------------------------------------------------- control plane
def _pid_alive_windows(pid: int) -> bool:
    """PROCESS_QUERY_LIMITED_INFORMATION probe: read-only, never signals."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Liveness probe that can never kill the target (``os.kill`` can on nt)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def stop_path() -> Path:
    return RUNS / STOP_FILE


def stop_requested() -> bool:
    """True when an operator dropped ``runs/STOP`` (never raises)."""
    try:
        return stop_path().is_file()
    except OSError:
        return False


def clear_stop_files() -> None:
    """Remove ``runs/STOP`` and ``runs/overnight.pid`` (idempotent)."""
    for name in (STOP_FILE, PID_FILE):
        try:
            (RUNS / name).unlink(missing_ok=True)
        except OSError:
            pass


def sleep_with_stop_watch(seconds: float, poll: float | None = None) -> bool:
    """Sleep in slices so ``runs/STOP`` interrupts the wait.

    Returns True when a STOP request appeared during the sleep. A zero/negative
    duration still issues one no-op ``time.sleep(0)`` call, which keeps the
    historic call pattern of the round loop intact.
    """
    remaining = float(seconds)
    if remaining <= 0:
        time.sleep(0)
        return stop_requested()
    chunk_max = float(poll if poll is not None else STOP_POLL_SECONDS)
    while remaining > 0:
        if stop_requested():
            return True
        chunk = min(chunk_max, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return stop_requested()


def halt_for_stop(
    machine: "StateMachine",
    round_no: int = 0,
    where: str = "",
) -> None:
    """Record STOPPED_BY_USER and drop ``runs/STOP`` + ``runs/overnight.pid``."""
    spot = where or "before start"
    machine.set(
        "STOPPED_BY_USER",
        reason="runs/STOP present %s" % spot,
        round_no=round_no,
    )
    log("runs/STOP present (%s); stopping by user request" % spot, "WARN")
    clear_stop_files()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``path`` via a sibling ``.tmp`` file + ``os.replace``.

    Same-directory temp keeps the replace atomic on NTFS; a crash mid-write
    leaves the previous complete file untouched instead of a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def roadmap_path() -> Path:
    return ROOT / ROADMAP_PATH


def has_pending_roadmap_items(text: str) -> bool:
    """True when the roadmap still holds at least one ``- [ ]`` item."""
    return bool(ROADMAP_PENDING_RE.search(text or ""))


def has_all_done_marker(text: str) -> bool:
    """True only for a standalone ``ALL_DONE`` line (prose mentions ignored)."""
    for line in (text or "").splitlines():
        if line.strip().strip("`").strip() == ALL_DONE_MARKER:
            return True
    return False


def roadmap_counts(text: str) -> tuple[int, int]:
    """Return ``(pending, total)`` checkbox counts for a roadmap body."""
    body = text or ""
    return (
        len(ROADMAP_PENDING_RE.findall(body)),
        len(ROADMAP_ITEM_RE.findall(body)),
    )


def current_roadmap_task_title(backlog: str) -> str:
    """Title of the first unchecked roadmap item, for prompts and commit subjects.

    ``- [ ] **M4-6（DeepSeek CoT 正文净化）**：修复 …`` becomes
    ``M4-6（DeepSeek CoT 正文净化）``: the checkbox and the emphasis markers are
    removed and only the part before the first colon is kept, so the title stays
    readable inside a one-line prompt header or commit subject. A backlog with
    no checkbox item at all (the derived git-log fallback) reports the neutral
    ``自动迭代`` instead of an empty label.
    """
    match = ROADMAP_FIRST_PENDING_RE.search(backlog or "")
    if match is None:
        return DEFAULT_TASK_TITLE
    title = match.group(1).replace("**", "").strip()
    for separator in ("：", ":"):
        if separator in title:
            title = title.split(separator, 1)[0].strip()
    return title[:TASK_TITLE_MAX] or DEFAULT_TASK_TITLE


def roadmap_all_done() -> tuple[bool, str]:
    """``(done, reason)`` — done when the roadmap exists and owes nothing."""
    path = roadmap_path()
    if not path.is_file():
        return False, "roadmap_missing"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, "roadmap_unreadable:%r" % exc
    if has_all_done_marker(text):
        return True, "ALL_DONE line present"
    if not has_pending_roadmap_items(text):
        return True, "no pending checkbox"
    return False, "pending items remain"


# ---------------------------------------------------------------- dispatch ledger
def canonical_card(card: str) -> str:
    """Normalize a card body so the marked and extracted forms share one id.

    ``execute_brain_reply`` hands over the already-extracted card while a brain
    reply may still carry the ``<<<WORKBUDDY_TASK_*>>>`` markers; keying the
    ledger on the raw text would give those two forms different ids and let the
    same task through twice.
    """
    text = card or ""
    extracted = extract_task_card(text)
    return (extracted or text).strip()


def card_digest(card: str) -> str:
    """First 12 hex chars of sha256 over the canonical (trimmed) card."""
    return hashlib.sha256(
        canonical_card(card).encode("utf-8")
    ).hexdigest()[:12]


def card_id_for(card: str, round_no: int) -> str:
    """Stable dispatch id: ``round<N>_<sha256(card)[:12]>``."""
    return "round%d_%s" % (int(round_no), card_digest(card))


def dispatch_record_path(card_id: str) -> Path:
    return RUNS / DISPATCH_DIR / ("%s.json" % card_id)


def read_dispatch_record(card_id: str) -> dict[str, Any]:
    try:
        data = json.loads(
            dispatch_record_path(card_id).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_dispatch_record(card_id: str, record: dict[str, Any]) -> Path:
    path = dispatch_record_path(card_id)
    try:
        _atomic_write_text(
            path, json.dumps(record, ensure_ascii=False, indent=2)
        )
    except OSError as exc:
        log("dispatch record write failed: %r" % exc, "ERROR")
    return path


def prepare_dispatch(card: str, round_no: int) -> tuple[str, str]:
    """Reserve a card for dispatch; return ``(card_id, blocked_status)``.

    An empty ``blocked_status`` means the card may go out. Otherwise the record
    is flagged ``needs_review`` and nothing is sent anywhere: a card still
    ``dispatched`` (reply never came back) or already ``timed_out`` must never
    be blindly re-handed to the next executor, or WorkBuddy and Antigravity
    would run the same task concurrently.
    """
    card_id = card_id_for(card, round_no)
    stamp = datetime.now().isoformat(timespec="seconds")
    record = read_dispatch_record(card_id)
    status = str(record.get("status") or "")
    if status in BLOCKING_DISPATCH_STATES:
        attempts = list(record.get("attempts") or [])
        attempts.append({"endpoint": "-", "status": "blocked", "at": stamp})
        record.update({
            "card_id": card_id,
            "status": status,
            "needs_review": True,
            "blocked_reason": "already_%s" % status,
            "updated_at": stamp,
            "attempts": attempts[-50:],
        })
        _save_dispatch_record(card_id, record)
        return card_id, status

    record.update({
        "card_id": card_id,
        "round": int(round_no),
        "card_sha256": card_digest(card),
        "card_head": (card or "").strip()[:200],
        "status": "dispatched",
        "needs_review": False,
        "pid": os.getpid(),
        "created_at": record.get("created_at") or stamp,
        "updated_at": stamp,
    })
    record.setdefault("attempts", [])
    _save_dispatch_record(card_id, record)
    return card_id, ""


def finish_dispatch(
    card_id: str,
    *,
    completed: bool,
    endpoint: str = "",
    detail: str = "",
) -> Path:
    """Close a dispatch record as ``completed`` or ``timed_out``."""
    stamp = datetime.now().isoformat(timespec="seconds")
    record = read_dispatch_record(card_id)
    record.setdefault("card_id", card_id)
    attempts = list(record.get("attempts") or [])
    if endpoint:
        attempts.append({
            "endpoint": endpoint,
            "status": "completed" if completed else "timed_out",
            "at": stamp,
        })
    record.update({
        "status": "completed" if completed else "timed_out",
        "updated_at": stamp,
        "detail": (detail or "")[:300],
        "attempts": attempts[-50:],
    })
    return _save_dispatch_record(card_id, record)


# ---------------------------------------------------------------- subprocess
def _no_window_flags() -> int:
    """``CREATE_NO_WINDOW`` on Windows so no console flashes in the tray.

    Every subprocess this loop starts (bridge.py, opencode, git, taskkill) is
    captured through pipes, so the console window has nothing to show and only
    steals attention from whatever the operator is doing on the primary screen.
    Off Windows the flag does not exist and 0 is the neutral value.
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


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
            creationflags=_no_window_flags(),
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
        creationflags=_no_window_flags(),
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
        creationflags=_no_window_flags(),
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


# ---------------------------------------------------------------- state machine
class StateMachine:
    """Tiny explicit state machine that persists every transition to JSON.

    The file is rewritten on each transition so an operator (or the next
    round) can tell exactly where an unattended run stopped, even if the
    process was killed without unwinding.

    Two M4-1 guarantees: writes go through a sibling ``.tmp`` + ``os.replace``
    so a crash can never leave a truncated JSON, and a scheduler never
    overwrites the file of another scheduler that is still alive.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._effective: Path | None = None
        self.state = "IDLE"
        self.reason = ""
        self.round = 0
        self.history: list[dict[str, Any]] = []

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else RUNS / STATE_FILE

    @property
    def written_to(self) -> Path | None:
        """File actually written (sidecar included); None until the first save."""
        return self._effective

    def _resolve_target(self) -> Path:
        """Choose the write target, isolating from a live foreign scheduler.

        ``runs/state_machine.json`` belongs to whoever wrote it last. When it
        records a different pid that is still running, that process keeps the
        canonical file and this one writes ``runs/state_machine.<pid>.json``
        so the live scheduler's history is never clobbered. An explicit
        ``path=`` is never isolated: the caller owns that file.
        """
        if self._effective is not None:
            return self._effective
        base = self.path
        if self._path is not None:
            self._effective = base
            return base
        foreign = 0
        try:
            existing = json.loads(base.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict):
            try:
                foreign = int(existing.get("pid") or 0)
            except (TypeError, ValueError):
                foreign = 0
        if foreign and foreign != os.getpid() and pid_alive(foreign):
            self._effective = base.with_name(
                "%s.%d%s" % (base.stem, os.getpid(), base.suffix)
            )
            log(
                "state machine isolated: pid=%d is alive and holds %s; "
                "writing %s" % (foreign, base.name, self._effective.name),
                "WARN",
            )
        else:
            self._effective = base
        return self._effective

    def set(
        self,
        state: str,
        *,
        reason: str = "",
        round_no: int | None = None,
    ) -> str:
        if state not in STATE_NAMES:
            raise ValueError("unknown state %r" % state)
        self.state = state
        if reason:
            self.reason = reason
        if round_no is not None:
            self.round = int(round_no)
        stamp = datetime.now().isoformat(timespec="seconds")
        self.history.append({"state": state, "at": stamp, "reason": self.reason})
        if len(self.history) > 200:
            self.history = self.history[-200:]
        self.save(stamp=stamp)
        return state

    def snapshot(self, stamp: str = "") -> dict[str, Any]:
        return {
            "state": self.state,
            "updated_at": stamp or datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "round": self.round,
            "reason": self.reason,
            "history": list(self.history),
        }

    def save(self, stamp: str = "") -> None:
        target = self._resolve_target()
        try:
            _atomic_write_text(
                target,
                json.dumps(self.snapshot(stamp), ensure_ascii=False, indent=2),
            )
        except OSError as exc:
            log("state machine write failed: %r" % exc, "ERROR")


def load_endpoint_config(endpoint: str) -> dict[str, Any]:
    """Read one endpoint block from config/endpoints.json (never raises)."""
    path = ROOT / "config" / "endpoints.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    endpoints = data.get("endpoints") or {}
    block = endpoints.get(endpoint)
    return dict(block) if isinstance(block, dict) else {}


def endpoint_supports_attach(endpoint: str) -> bool:
    """True only when the endpoint declares an ``attach_button`` selector.

    Web Grok has no attach selector, and ``bridge.py ask --attach`` raises
    NotImplementedError for it, so the caller must inline the text instead.
    """
    selectors = load_endpoint_config(endpoint).get("selectors") or {}
    return bool(selectors.get("attach_button"))



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


# ---------------------------------------------------------------- handoff
HANDOFF_RULES: tuple[str, ...] = (
    "严禁删除、跳过或弱化任何既有单测断言。",
    "只改任务所需文件；改动后必须运行 py -3.10 -m unittest discover -s tests -v 且 rc=0。",
    "严禁抢占主屏焦点、严禁污染系统剪贴板；需要人机交互时只使用副屏 -1440..0。",
    "严禁 force push、严禁删分支、严禁碰 v0.0.8 / v0.0.9。",
    "补丁写在 <<<BRIDGE_PATCH_START>>> ... <<<BRIDGE_PATCH_END>>> 之间；"
    "无法直出补丁时写 <<<WORKBUDDY_TASK_START>>> ... <<<WORKBUDDY_TASK_END>>> 任务卡。",
)


def build_handoff_bundle(
    reason: str,
    report: str | Path | None = None,
    *,
    path: str | Path | None = None,
    todo: str = "",
) -> Path:
    """Write runs/brain_handoff.md: everything the next brain needs, standalone."""
    target = Path(path) if path is not None else RUNS / HANDOFF_FILE
    if not target.is_absolute() and path is not None:
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)

    branch = git_text("rev-parse", "--abbrev-ref", "HEAD").strip() or "?"
    commits = git_text("log", "-n", "8", "--oneline").strip() or "(no commits)"
    # Keep the leading column: " M" (unstaged) and "M " (staged) differ.
    status = git_text("status", "--short").rstrip("\n") or "(clean)"
    if not todo:
        try:
            todo = load_backlog()[0].strip()
        except Exception:  # noqa: BLE001 - handoff must never fail on backlog
            todo = ""
    error_summary = ""
    if report:
        report_path = Path(report)
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        if report_path.is_file():
            text = report_path.read_text(encoding="utf-8", errors="replace")
            error_summary = text[:4000]

    content = (
        "# Bridge brain handoff\n\n"
        "- 仓库路径: %s\n- 分支: %s\n- HEAD: %s\n- 触发原因: %s\n"
        "- 生成时间: %s\n- 调度进程: PID=%d\n\n"
        "## 硬约束\n\n%s\n\n"
        "## 最近 8 条 commit\n\n```text\n%s\n```\n\n"
        "## git status --short\n\n```text\n%s\n```\n\n"
        "## 待办\n\n%s\n\n"
        "## 报错摘要\n\n```text\n%s\n```\n"
        % (
            ROOT,
            branch,
            head_commit() or "?",
            reason or "(unspecified)",
            datetime.now().isoformat(timespec="seconds"),
            os.getpid(),
            "\n".join("- " + item for item in HANDOFF_RULES),
            commits,
            status,
            todo[:4000] or "(none)",
            error_summary or "(none)",
        )
    )
    target.write_text(content, encoding="utf-8")
    log("handoff bundle written: %s" % target)
    return target


# ---------------------------------------------------------------- bridge calls
def extract_task_card(reply: str) -> str:
    """Return the last real ``<<<WORKBUDDY_TASK_*>>>`` block (placeholder-safe)."""
    return _last_valid_block(TASK_RE, reply).strip()


MARKER_SYNTAX_HINT = (
    "标记写法固定为「三个小于号 + 名字 + 三个大于号」（例如名字 `BRIDGE_PATCH_START`），"
    "尖括号与名字之间不留空格、每个标记独占一行；标记之外不要输出任何文字。"
    "不要照抄本说明里的示例文字，直接写真实内容。\n"
)

#: Deliberately spelled without any contiguous marker token: the prompt itself
#: must never be matchable by ``TASK_RE`` / ``PATCH_RE``, otherwise a brain that
#: echoes the instructions wins the extraction race with an empty example block.
PATCH_HINT = (
    "优先直接给出可 `git apply` 的统一 diff，用 `BRIDGE_PATCH_START` / "
    "`BRIDGE_PATCH_END` 两个标记夹住它"
    "（diff --git 格式，路径相对仓库根，不要加代码围栏、不要解释）；"
    "如果你更愿意让本地执行器自己动手，就把要求写成任务卡"
    "（`WORKBUDDY_TASK_START` / `WORKBUDDY_TASK_END`）。\n"
)


def build_gemini_prompt(attempt: int, report: Path) -> str:
    return (
        "【Bridge 无人值守自愈 round=%d】\n"
        "附件是完整的 runs/crash_report.md，包含失败堆栈、git diff 与失败用例源码。\n"
        "请分析根因并给出可直接落地的修复：\n"
        "%s%s"
        "- 每个标记块只能出现一次，标记外不要输出任何文字；\n"
        "- 严禁删除、跳过或弱化任何既有断言；\n"
        "- 改完必须让 py -3.10 -m unittest discover -s tests -v 全绿（rc=0）。\n\n"
        "报告路径：%s\n" % (attempt, PATCH_HINT, MARKER_SYNTAX_HINT, report)
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
    recent = git_text("log", "-n", "8", "--oneline").strip() or "(no commits)"
    return (
        "【Bridge 自动迭代 round=%d | %s | BASE_COMMIT=%s】\n"
        "主线来源：docs/ROADMAP.md 是唯一权威主线；只挑其中未勾选的 `- [ ]` "
        "小步推进，不要另立主线、不要跳到后面的项。\n"
        "现有自动循环/待办清单（若某项已完成，请挑下一项未完成的小步）：\n"
        "%s\n\n"
        "最近 8 条 commit（已完成的不要重复提交）：\n%s\n\n"
        "请给出下一项小步、可独立测试、可由全量 unittest 验证的改进任务。\n"
        "%s%s"
        "只输出一个标记块（补丁或任务卡），不要寒暄或解释。\n"
        "任务卡/补丁要写明：改动文件、具体修复点、禁止删除或弱化既有断言、"
        "以及最后必须运行全量单测；任务完成后请把 runs/overnight_todo.md "
        "里对应条目标为 [已完成]。\n"
        % (
            round_no,
            current_roadmap_task_title(backlog),
            base_commit,
            backlog[:6000],
            recent,
            PATCH_HINT,
            MARKER_SYNTAX_HINT,
        )
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


def _absolute(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else ROOT / candidate


def inline_attach_text(path: str | Path) -> str:
    """Read an attachment so it can be pasted into the prompt body instead."""
    source = _absolute(path)
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "【附件读取失败：%s (%r)】\n" % (source, exc)
    if len(text) > ATTACH_INLINE_LIMIT:
        text = text[:ATTACH_INLINE_LIMIT] + "\n...(truncated)"
    return "【附件内联：%s】\n%s\n" % (source.name, text)


def reply_has_contract(reply: str) -> bool:
    """A usable brain reply carries a *real* patch block or task card.

    Placeholder-only replies (a pure echo of the prompt template) no longer
    satisfy the contract, so the brain chain falls through to the next endpoint
    instead of dispatching an empty card downstream.
    """
    body = reply or ""
    return bool(extract_patch(body) or extract_task_card(body))


def _ask_brain_endpoint(
    endpoint: str,
    prompt: str,
    attempt: int,
    timeout: float,
    *,
    attach: str | Path | None = None,
    tag: str = "brain",
) -> tuple[int, str]:
    """One brain call; degrades --attach to inlined text when unsupported."""
    prompt_file = RUNS / ("%s_prompt_%s%d.txt" % (endpoint, tag, attempt))
    saved = RUNS / ("%s_reply_%s%d.txt" % (endpoint, tag, attempt))
    prompt_file.parent.mkdir(parents=True, exist_ok=True)

    body = prompt
    use_attach = False
    if attach:
        if endpoint_supports_attach(endpoint):
            use_attach = True
        else:
            body = "%s\n\n%s" % (prompt, inline_attach_text(attach))
            log("brain attach degraded: %s has no attach_button; text inlined"
                % endpoint, "WARN")

    prompt_file.write_text(body, encoding="utf-8")
    bridge_timeout = max(30, int(timeout - 30))
    cmd = [
        PYTHON, str(BRIDGE), "ask", endpoint, "",
        "--file", str(prompt_file),
    ]
    if use_attach:
        cmd += ["--attach", str(_absolute(attach))]
    cmd += [
        "--profile", "test", "--headed",
        "--timeout", str(bridge_timeout), "--json", "--save", str(saved),
    ]
    rc, output = run_cmd(cmd, timeout=timeout, cwd=ROOT)
    reply = parse_reply(rc, output, saved) if rc == 0 else ""
    if rc == 0 and not reply:
        reply = read_reply(endpoint, saved, timeout=60)
    log("%s rc=%s reply_chars=%d" % (endpoint, rc, len(reply)))
    return rc, reply


def call_brain(
    prompt: str,
    attempt: int,
    timeout: float,
    *,
    brain: str = "auto",
    attach: str | Path | None = None,
    tag: str = "brain",
) -> tuple[str, str]:
    """Ask the brain chain in order; return ``(endpoint, reply)``.

    ``auto`` walks gemini -> deepseek -> grok and skips any level that returns a
    non-zero rc, times out, raises, answers empty or omits both marker blocks.
    An explicit ``brain`` value never falls back, so an operator can pin one.
    """
    mode = (brain or "auto").strip().lower()
    if mode not in BRAIN_CHOICES:
        raise ValueError(
            "unknown brain %r (choose from %s)"
            % (brain, ", ".join(BRAIN_CHOICES))
        )
    order = list(BRAIN_CHAIN) if mode == "auto" else [mode]
    for index, endpoint in enumerate(order):
        try:
            rc, reply = _ask_brain_endpoint(
                endpoint, prompt, attempt, timeout, attach=attach, tag=tag
            )
        except Exception as exc:  # noqa: BLE001 - fall through to next brain
            log("brain_miss=%s round=%d reason=%r" % (endpoint, attempt, exc),
                "ERROR")
            rc, reply = -1, ""
        if rc != 0:
            log("brain_miss=%s round=%d reason=rc_%s" % (endpoint, attempt, rc),
                "ERROR")
        elif not reply:
            log("brain_miss=%s round=%d reason=empty_reply" % (endpoint, attempt),
                "ERROR")
        elif not reply_has_contract(reply):
            log("brain_miss=%s round=%d reason=no_patch_or_task_marker"
                % (endpoint, attempt), "ERROR")
        else:
            log("brain_hit=%s round=%d level=%d/%d"
                % (endpoint, attempt, index + 1, len(order)))
            return endpoint, reply
        if index + 1 < len(order):
            log("brain_fallback -> %s (declared=%s) round=%d"
                % (order[index + 1], BRAIN_FALLBACK.get(endpoint, "none"),
                   attempt))
    log("brain_chain exhausted round=%d" % attempt, "ERROR")
    return "", ""


def request_crash_report(
    report: Path,
    attempt: int,
    timeout: float,
    *,
    brain: str = "auto",
) -> str:
    _, reply = call_brain(
        build_gemini_prompt(attempt, report), attempt, timeout,
        brain=brain, attach=report, tag="crash",
    )
    return reply


def call_gemini(
    report: Path,
    attempt: int,
    timeout: float,
    *,
    brain: str = "auto",
) -> str:
    """Backwards-compatible entry: crash report through the whole brain chain."""
    return request_crash_report(report, attempt, timeout, brain=brain)


def request_task_from_gemini(
    backlog: str,
    round_no: int,
    base_commit: str,
    timeout: float,
    *,
    brain: str = "auto",
) -> str:
    _, reply = call_brain(
        build_task_prompt(round_no, backlog, base_commit), round_no, timeout,
        brain=brain, tag="overnight_",
    )
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
    executor_profile: str = "test",
) -> str:
    profile = (executor_profile or "test").strip().lower()
    if profile not in PROFILE_CHOICES:
        raise ValueError(
            "unknown executor profile %r (choose from %s)"
            % (executor_profile, ", ".join(PROFILE_CHOICES))
        )
    task_file = RUNS / ("%s_task_round%d.md" % (endpoint, attempt))
    saved = RUNS / ("%s_reply_round%d.txt" % (endpoint, attempt))
    task_file.parent.mkdir(parents=True, exist_ok=True)
    header = scheduler_notice()
    if executor:
        header += _executor_header(executor, endpoint)
    if profile == "none":
        header += "[executor-profile] none (keep the endpoint's current model)\n"
    task_file.write_text(header + task_card, encoding="utf-8")
    bridge_timeout = max(30, int(timeout - 30))
    cmd = [
        PYTHON, str(BRIDGE), "ask", endpoint, "",
        "--file", str(task_file),
    ]
    if profile != "none":
        cmd += ["--profile", profile]
    cmd += [
        "--headed",
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
    executor_profile: str = "test",
) -> str:
    return _ask_endpoint(
        "workbuddy", task_card, attempt, timeout,
        executor=executor, executor_profile=executor_profile,
    )


def call_opencode(
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
    executor_profile: str = "test",
) -> str:
    return _ask_endpoint(
        "opencode", task_card, attempt, timeout,
        executor=executor, executor_profile=executor_profile,
    )


def call_antigravity(
    task_card: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "",
    executor_profile: str = "test",
) -> str:
    return _ask_endpoint(
        "antigravity", task_card, attempt, timeout,
        executor=executor, executor_profile=executor_profile,
    )


def call_executor(
    task_card: str,
    attempt: int,
    timeout: float,
    executor: str = "auto",
    *,
    executor_profile: str = "test",
) -> str:
    """Dispatch a task card through the three-level executor pool.

    ``auto`` tries WorkBuddy, then OpenCode, then Antigravity. Any level that
    returns an empty reply, a non-zero bridge rc (including timeout rc=124) or
    raises is skipped with a logged reason, and the next level is tried.
    Explicit executors never fall back, so an operator can pin one target.
    ``executor_profile="none"`` keeps the endpoint's current model and never
    passes ``--profile``.

    The card is reserved under ``runs/dispatched/<card_id>.json`` first. If that
    record is still ``dispatched`` or already ``timed_out`` the call refuses to
    send anything and flags the record ``needs_review`` instead — handing a
    possibly in-flight task to the *next* executor is exactly the concurrent
    double-dispatch this guard exists to stop.

    Two further brakes apply before a fallback is allowed:

    * a level that committed but never answered (``HEAD`` moved) is counted as
      *done* — ``executor_committed`` is logged and the chain stops, because
      re-handing an already committed task to the next executor is the worst
      possible double work;
    * the worktree fingerprint is compared before/after each level — if the
      level wrote files but returned no reply (bridge timeout while the code was
      already saved), the chain stops and returns ``EXECUTOR_DIRTY_MARKER`` so
      the outer test gate judges the work instead of a second executor redoing
      it on top;
    * a cooling opencode lane is skipped outright (``executor_skip=opencode
      reason=cooldown_active``) rather than waiting out a certain failure.
    """
    mode = (executor or "auto").strip().lower()
    if mode not in EXECUTOR_CHOICES:
        raise ValueError(
            "unknown executor %r (choose from %s)"
            % (executor, ", ".join(EXECUTOR_CHOICES))
        )
    card_id, blocked = prepare_dispatch(task_card, attempt)
    if blocked:
        log(
            "dispatch_blocked card_id=%s previous_status=%s round=%d "
            "(no re-dispatch; needs_review=true)"
            % (card_id, blocked, attempt),
            "ERROR",
        )
        return ""

    order = list(EXECUTOR_CHAIN) if mode == "auto" else [mode]
    callers = {
        "workbuddy": call_workbuddy,
        "opencode": call_opencode,
        "antigravity": call_antigravity,
    }
    for index, endpoint in enumerate(order):
        if mode == "auto" and endpoint == "opencode":
            cooling_until = opencode_cooldown_until()
            if cooling_until:
                log(
                    "executor_skip=opencode reason=cooldown_active until=%s "
                    "round=%d" % (cooling_until, attempt),
                    "WARN",
                )
                continue
        before = worktree_fingerprint()
        before_head = head_commit()
        try:
            reply = callers[endpoint](
                task_card, attempt, timeout, executor=mode,
                executor_profile=executor_profile,
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
            finish_dispatch(card_id, completed=True, endpoint=endpoint)
            return reply
        log(
            "executor_miss=%s round=%d reason=empty_or_nonzero rc"
            % (endpoint, attempt),
            "ERROR",
        )
        after_head = head_commit()
        if after_head and after_head != before_head:
            log(
                "executor_committed=%s round=%d: HEAD moved %s -> %s without a "
                "reply; counting the level as done so no second executor redoes "
                "the same task"
                % (endpoint, attempt, (before_head or "?")[:12], after_head[:12]),
                "WARN",
            )
            finish_dispatch(
                card_id, completed=True, endpoint=endpoint,
                detail="HEAD advanced without reply; fallback suppressed",
            )
            return EXECUTOR_DIRTY_MARKER % endpoint
        after = worktree_fingerprint()
        if after and after != before:
            log(
                "executor_dirty=%s round=%d: the worktree changed during this "
                "level but no reply came back; suppressing fallback so the test "
                "gate judges the work instead of a second executor redoing it"
                % (endpoint, attempt),
                "ERROR",
            )
            finish_dispatch(
                card_id, completed=False, endpoint=endpoint,
                detail="worktree changed without reply; fallback suppressed",
            )
            return EXECUTOR_DIRTY_MARKER % endpoint
        if index + 1 < len(order):
            log(
                "executor_fallback -> %s round=%d"
                % (order[index + 1], attempt)
            )
    finish_dispatch(
        card_id, completed=False, detail="all executors returned empty"
    )
    return ""


# ---------------------------------------------------------------- patches
def extract_patch(reply: str) -> str:
    """Return the last real unified diff inside the patch markers.

    Placeholder blocks (``...unified diff...`` and friends) are skipped so an
    echoed template can never reach ``git apply`` as an empty patch.
    """
    body = _last_valid_block(PATCH_RE, reply).strip("\n")
    if not body:
        return ""
    lines = body.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def apply_unified_patch(
    patch: str,
    *,
    path: str | Path | None = None,
) -> tuple[bool, str]:
    """``git apply --check`` first, then apply; never leaves a half-applied tree."""
    body = (patch or "").strip("\n")
    if not body.strip():
        return False, "empty patch"
    target = Path(path) if path is not None else RUNS / PATCH_FILE
    if not target.is_absolute() and path is None:
        target = RUNS / PATCH_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body + "\n", encoding="utf-8")

    check = git("apply", "--check", "--whitespace=nowarn", str(target))
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "").strip()
        log("patch --check failed: %s" % detail[-300:], "ERROR")
        return False, detail or "git apply --check failed"
    applied = git("apply", "--whitespace=nowarn", str(target))
    if applied.returncode != 0:
        detail = (applied.stderr or applied.stdout or "").strip()
        log("patch apply failed: %s" % detail[-300:], "ERROR")
        return False, detail or "git apply failed"
    log("patch applied (%d line(s)) from %s" % (len(body.splitlines()), target))
    return True, "applied"


def execute_brain_reply(
    reply: str,
    attempt: int,
    timeout: float,
    *,
    executor: str = "auto",
    executor_profile: str = "test",
) -> dict[str, Any]:
    """Apply a direct brain patch, else hand the task card to a local executor.

    Returns ``{"mode": "patch"|"task"|"none", "applied": bool, "task": str,
    "executor_reply": str, "detail": str, "card_id": str, "blocked": bool}``
    so callers can log and decide without re-parsing the reply. ``blocked``
    means the idempotency guard refused the dispatch (see ``call_executor``).
    """
    patch = extract_patch(reply)
    if patch:
        ok, detail = apply_unified_patch(patch)
        if ok:
            return {
                "mode": "patch",
                "applied": True,
                "task": "",
                "executor_reply": "",
                "detail": detail,
                "card_id": "",
                "blocked": False,
            }
        log("brain patch rejected (%s); falling back to task card" % detail,
            "ERROR")
    card = extract_task_card(reply)
    if card:
        card_id = card_id_for(card, attempt)
        pending = str(read_dispatch_record(card_id).get("status") or "")
        executor_reply = call_executor(
            card, attempt, timeout, executor=executor,
            executor_profile=executor_profile,
        )
        return {
            "mode": "task",
            "applied": False,
            "task": card,
            "executor_reply": executor_reply,
            "detail": "",
            "card_id": card_id,
            "blocked": pending in BLOCKING_DISPATCH_STATES,
        }
    return {
        "mode": "none",
        "applied": False,
        "task": "",
        "executor_reply": "",
        "detail": "no patch and no task card",
        "card_id": "",
        "blocked": False,
    }


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
    brain: str = "auto",
    executor_profile: str = "test",
    stop_on_bug: bool = False,
    machine: StateMachine | None = None,
) -> int:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    machine = machine or StateMachine()
    base_commit = head_commit()
    if not COMMIT_RE.fullmatch(base_commit):
        log("cannot resolve BASE_COMMIT: %r" % base_commit, "ERROR")
        machine.set("BLOCKED", reason="cannot resolve BASE_COMMIT")
        return 2
    log("BASE_COMMIT=%s max_retries=%d no_push=%s stop_on_bug=%s"
        % (base_commit, max_retries, no_push, stop_on_bug))
    machine.set("VERIFY_TESTS", reason="heal start", round_no=0)

    report = Path(report_path)
    for attempt in range(1, max(1, max_retries) + 1):
        round_base = head_commit()
        if not COMMIT_RE.fullmatch(round_base):
            log("round %d: cannot resolve round base commit" % attempt, "ERROR")
            reset_hard(base_commit)
            machine.set("BLOCKED", reason="cannot resolve round base", round_no=attempt)
            return 2
        log("round %d/%d start base=%s" % (attempt, max_retries, round_base[:12]))
        machine.set(
            "VERIFY_TESTS", reason="round %d pre-check" % attempt,
            round_no=attempt,
        )

        rc, output = run_tests(timeout)
        if rc == 0:
            log("round %d: full unittest green (rc=0)" % attempt)
            if commit_on_green:
                machine.set("COMMIT_AND_PUSH", reason="green suite",
                            round_no=attempt)
                return commit_and_push(
                    "fix(heal): self-healed unit tests via gemini/workbuddy",
                    no_push,
                )
            return 0

        log("round %d: unittest red (rc=%s); building crash report" % (attempt, rc),
            "ERROR")
        report = build_crash_report(output, report)
        machine.set("BUG_REPORTED", reason="unittest rc=%s" % rc, round_no=attempt)

        if stop_on_bug:
            handoff = build_handoff_bundle(
                "unittest red rc=%s (round %d)" % (rc, attempt), report
            )
            log("stop-on-bug: reporting to brain chain before any blind edit",
                "ERROR")
            call_gemini(report, attempt, timeout, brain=brain)
            reset_hard(base_commit)
            machine.set(
                "STOPPED_ON_BUG",
                reason="stop-on-bug: %s + %s, rolled back to %s"
                % (report.name, handoff.name, base_commit[:12]),
                round_no=attempt,
            )
            log("stop-on-bug: halted after rollback to %s" % base_commit[:12],
                "ERROR")
            return 1

        machine.set("BRAIN_PLAN", reason="crash report request", round_no=attempt)
        reply = call_gemini(report, attempt, timeout, brain=brain)
        result = execute_brain_reply(
            reply, attempt, timeout,
            executor=executor, executor_profile=executor_profile,
        )
        if result["mode"] == "patch":
            log("round %d: brain patch applied directly" % attempt)
            machine.set("APPLY_PATCH", reason="brain direct patch",
                        round_no=attempt)
        elif result["mode"] == "task":
            machine.set("DISPATCH_EXECUTOR", reason="task card dispatched",
                        round_no=attempt)
            (RUNS / ("extracted_task_round%d.md" % attempt)).write_text(
                result["task"], encoding="utf-8"
            )
            if not result["executor_reply"]:
                log("round %d: all executors returned empty replies" % attempt,
                    "ERROR")
        else:
            log("round %d: no patch and no task card; retry counted" % attempt,
                "ERROR")

        if attempt < max(1, max_retries):
            reset_hard(round_base)

    log("all %d heal attempts failed; rolling back to BASE_COMMIT %s"
        % (max_retries, base_commit), "ERROR")
    machine.set("BLOCKED", reason="heal attempts exhausted", round_no=max_retries)
    if not reset_hard(base_commit):
        return 2
    log("kept %s for post-mortem; exiting non-zero" % report, "ERROR")
    return 1


def has_changes() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


def worktree_fingerprint() -> str:
    """``git status --porcelain`` as a comparable string ("" when clean).

    ``call_executor`` compares this before and after a level instead of testing
    absolute dirtiness: the loop frequently starts on a worktree that already
    carries unrelated edits, and only a *delta* proves that the executor itself
    wrote code before dying.
    """
    proc = git("status", "--porcelain")
    return (proc.stdout or "").strip()


def opencode_cooldown_until(now: datetime | None = None) -> str:
    """ISO stamp of an active opencode cooldown, or "" when the lane is clear.

    ``endpoints/opencode.py`` drops ``runs/opencode_cooldown.json`` when the CLI
    reports a quota / rate-limit hit. Handing the next card to a cooling lane
    means burning a full budget on a certain failure, so the auto chain skips it.
    Read-only: an unparsable or expired marker simply reads as "clear".
    """
    try:
        data = json.loads((RUNS / COOLDOWN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    until_raw = str((data or {}).get("until") or "") if isinstance(data, dict) else ""
    if not until_raw:
        return ""
    try:
        until = datetime.fromisoformat(until_raw)
    except ValueError:
        return ""
    if until > (now or datetime.now()):
        return until_raw
    return ""


def run_overnight(
    max_rounds: int = 20,
    *,
    no_push: bool = False,
    timeout: float = 300,
    sleep: float = 60,
    delay_start: float = 0,
    executor: str = "auto",
    brain: str = "auto",
    executor_profile: str = "test",
    stop_on_bug: bool = False,
) -> int:
    """Multi-round overnight loop: red -> self-heal, green -> next task."""
    global LOG_NAMES
    previous_logs = LOG_NAMES
    LOG_NAMES = (OVERNIGHT_LOG,)
    machine = StateMachine()
    machine.set("IDLE", reason="overnight start")
    pid = write_pid()
    idle_streak = 0
    failure_streak = 0
    reason = "MAX_ROUNDS"
    exit_code = 0
    try:
        log("overnight start pid=%d max_rounds=%d sleep=%.1fs delay=%.1fs "
            "no_push=%s brain=%s executor=%s executor_profile=%s stop_on_bug=%s"
            % (pid, max_rounds, sleep, delay_start, no_push, brain, executor,
               executor_profile, stop_on_bug))
        if stop_requested():
            halt_for_stop(machine, 0, "before start")
            return 0
        if delay_start > 0:
            log("delay-start: sleeping %.1fs" % delay_start)
            time.sleep(delay_start)
            if stop_requested():
                halt_for_stop(machine, 0, "after delay-start")
                return 0

        for round_no in range(1, max(1, max_rounds) + 1):
            round_backlog = ""
            if stop_requested():
                halt_for_stop(machine, round_no, "at round start")
                reason = "STOPPED_BY_USER"
                exit_code = 0
                break
            base = head_commit()
            if not COMMIT_RE.fullmatch(base):
                log("round %d: cannot resolve BASE_COMMIT" % round_no, "ERROR")
                machine.set("STUCK_TRIAGE", reason="no BASE_COMMIT",
                            round_no=round_no)
                failure_streak += 1
                idle_streak += 1
                if failure_streak >= 3:
                    reason = "THREE_CONSECUTIVE_FAILURES"
                    break
                if sleep_with_stop_watch(sleep):
                    halt_for_stop(machine, round_no, "during sleep")
                    reason = "STOPPED_BY_USER"
                    exit_code = 0
                    break
                continue

            log("round %d/%d BASE_COMMIT=%s" % (round_no, max_rounds, base[:12]))
            machine.set("VERIFY_TESTS", reason="round pre-check",
                        round_no=round_no)
            rc, _ = run_tests(timeout)

            if rc == 0:
                done, why = roadmap_all_done()
                if done:
                    machine.set(
                        "ALL_DONE",
                        reason="roadmap owes nothing (%s)" % why,
                        round_no=round_no,
                    )
                    log("round %d: suite green and roadmap has no pending item "
                        "(%s); recording ALL_DONE and exiting"
                        % (round_no, why))
                    reason = "ALL_DONE"
                    exit_code = 0
                    break

            if rc != 0:
                log("round %d: red suite; self-heal up to 3 attempts" % round_no,
                    "ERROR")
                heal_rc = heal(
                    max_retries=3, no_push=no_push, timeout=timeout,
                    commit_on_green=False, executor=executor, brain=brain,
                    executor_profile=executor_profile,
                    stop_on_bug=stop_on_bug, machine=machine,
                )
                if stop_on_bug:
                    machine.set(
                        "STOPPED_ON_BUG",
                        reason="stop-on-bug after red suite", round_no=round_no,
                    )
                    reason = "STOPPED_ON_BUG"
                    log("round %d: stop-on-bug set; halting overnight loop"
                        % round_no, "ERROR")
                    exit_code = 1
                    break
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
                    if sleep_with_stop_watch(sleep):
                        halt_for_stop(machine, round_no, "during sleep")
                        reason = "STOPPED_BY_USER"
                        exit_code = 0
                        break
                    continue
            else:
                failure_streak = 0
                backlog, source = load_backlog()
                round_backlog = backlog
                log("round %d: next-task source=%s" % (round_no, source))
                machine.set("PICK_TASK", reason="source=%s" % source,
                            round_no=round_no)
                if stop_requested():
                    halt_for_stop(machine, round_no, "before dispatch")
                    reason = "STOPPED_BY_USER"
                    exit_code = 0
                    break
                machine.set("BRAIN_PLAN", reason="request next task",
                            round_no=round_no)
                reply = request_task_from_gemini(
                    backlog, round_no, base, timeout, brain=brain
                )
                result = execute_brain_reply(
                    reply, round_no, timeout,
                    executor=executor, executor_profile=executor_profile,
                )
                if result["mode"] == "none":
                    log("round %d: no valid task card; skip round" % round_no,
                        "ERROR")
                    idle_streak += 1
                    if idle_streak >= 3:
                        reason = "THREE_IDLE_ROUNDS"
                        break
                    if sleep_with_stop_watch(sleep):
                        halt_for_stop(machine, round_no, "during sleep")
                        reason = "STOPPED_BY_USER"
                        exit_code = 0
                        break
                    continue
                if result.get("blocked"):
                    log("round %d: card %s blocked by the idempotency guard "
                        "(needs_review); skipping round"
                        % (round_no, result.get("card_id") or "?"), "ERROR")
                    idle_streak += 1
                    if idle_streak >= 3:
                        reason = "THREE_IDLE_ROUNDS"
                        break
                    if sleep_with_stop_watch(sleep):
                        halt_for_stop(machine, round_no, "during sleep")
                        reason = "STOPPED_BY_USER"
                        exit_code = 0
                        break
                    continue

                (RUNS / ("overnight_accepted_task_round%d.md" % round_no)).write_text(
                    result["task"] or reply, encoding="utf-8"
                )
                if result["mode"] == "patch":
                    log("round %d: brain patch applied directly" % round_no)
                    machine.set("APPLY_PATCH", reason="brain direct patch",
                                round_no=round_no)
                else:
                    machine.set("DISPATCH_EXECUTOR",
                                reason="task card dispatched", round_no=round_no)
                    log("round %d: workbuddy reply_chars=%d"
                        % (round_no, len(result["executor_reply"])))
                machine.set("VERIFY_TESTS", reason="post-task check",
                            round_no=round_no)
                post_rc, _ = run_tests(timeout)
                if post_rc != 0:
                    log("round %d: new task made suite red; self-heal" % round_no,
                        "ERROR")
                    heal_rc = heal(
                        max_retries=3, no_push=no_push, timeout=timeout,
                        commit_on_green=False, executor=executor, brain=brain,
                        executor_profile=executor_profile,
                        stop_on_bug=stop_on_bug, machine=machine,
                    )
                    if stop_on_bug:
                        machine.set(
                            "STOPPED_ON_BUG",
                            reason="stop-on-bug after red task", round_no=round_no,
                        )
                        reason = "STOPPED_ON_BUG"
                        log("round %d: stop-on-bug set; halting overnight loop"
                            % round_no, "ERROR")
                        exit_code = 1
                        break
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
                        if sleep_with_stop_watch(sleep):
                            halt_for_stop(machine, round_no, "during sleep")
                            reason = "STOPPED_BY_USER"
                            exit_code = 0
                            break
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
                if sleep_with_stop_watch(sleep):
                    halt_for_stop(machine, round_no, "during sleep")
                    reason = "STOPPED_BY_USER"
                    exit_code = 0
                    break
                continue

            if has_changes():
                machine.set("COMMIT_AND_PUSH", reason="gate green",
                            round_no=round_no)
                gate_rc = commit_and_push(
                    "chore(auto): round %d %s"
                    % (round_no, current_roadmap_task_title(round_backlog)),
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
                current_head = head_commit()
                if current_head != base:
                    log("round %d: green, worktree clean but HEAD advanced to %s "
                        "(executor already committed); idle_streak reset"
                        % (round_no, (current_head or "?")[:12]))
                    idle_streak = 0
                else:
                    log("round %d: green but no changes; no commit" % round_no)
                    idle_streak += 1

            if idle_streak >= 3:
                reason = "THREE_IDLE_ROUNDS"
                break
            if round_no < max(1, max_rounds):
                log("sleep %.1fs before next round" % sleep)
                if sleep_with_stop_watch(sleep):
                    halt_for_stop(machine, round_no, "during sleep")
                    reason = "STOPPED_BY_USER"
                    exit_code = 0
                    break
    except Exception as exc:  # noqa: BLE001 - unattended runner must not hang
        log("overnight exception: %s" % exc, "ERROR")
        reason = "EXCEPTION"
        exit_code = 1
    finally:
        log("overnight stop reason=%s pid=%d" % (reason, pid))
        if machine.state not in TERMINAL_STATES:
            machine.set("STOPPED", reason=reason)
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
    parser.add_argument(
        "--overnight", "--daemon", dest="overnight", action="store_true",
        help="run the multi-round unattended daemon loop "
             "(--daemon is an exact alias of --overnight)",
    )
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=60,
                        help="seconds between overnight rounds")
    parser.add_argument("--delay-start", type=float, default=0,
                        help="seconds to wait before the first round")
    parser.add_argument("--executor", choices=EXECUTOR_CHOICES, default="auto",
                        help="overnight task executor (auto falls back)")
    parser.add_argument("--executor-profile", choices=PROFILE_CHOICES,
                        default="test",
                        help="model profile for the executor; 'none' keeps the "
                             "endpoint's current model and omits --profile")
    parser.add_argument("--brain", choices=BRAIN_CHOICES, default="auto",
                        help="planning brain (auto = gemini -> deepseek -> grok)")
    parser.add_argument("--stop-on-bug", action="store_true",
                        help="on a red suite: report to the brain chain, roll "
                             "back to BASE_COMMIT and stop instead of blind edits")
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
                brain=args.brain,
                executor_profile=args.executor_profile,
                stop_on_bug=args.stop_on_bug,
            )
        return heal(
            max_retries=args.max_retries,
            no_push=args.no_push,
            timeout=args.timeout,
            report_path=args.report,
            executor=args.executor,
            brain=args.brain,
            executor_profile=args.executor_profile,
            stop_on_bug=args.stop_on_bug,
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
