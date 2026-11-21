#!/usr/bin/env python3
"""Shared daemon control plane — stdlib only, single source of truth.

``scripts/auto_heal_loop.py`` (the scheduler) and ``bridge.py`` (the CLI) grew
two copies of the same unattended-run plumbing: PID liveness, ``runs/STOP``
handling, ``docs/ROADMAP.md`` parsing, atomic state writes and the persisted
``StateMachine``. ``bridge_gui.pyw`` carried a third partial copy of the
roadmap/cooldown readers. This module owns that logic once; the callers keep
thin compatibility wrappers so their historic names and monkeypatch points
(``auto_heal_loop.RUNS``, ``bridge.ROOT``, ``auto_heal_loop.pid_alive`` …)
keep working unchanged.

Design rules
------------
* Every path is an explicit argument (``runs_dir`` / ``path``). Nothing here
  reads a module-level "current runs dir", so a caller's patched ``RUNS`` still
  decides where files land.
* Read-only by default; the only writers are ``_atomic_write_text``,
  ``StateMachine.save`` and ``clear_stop_files`` — all named as such.
* Never signals a process: the Windows probe uses
  ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess``
  because ``os.kill(pid, 0)`` terminates the target on nt.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

#: Repository root derived from this file (``engine/daemon_state.py``).
ROOT = Path(__file__).resolve().parents[1]

STOP_FILE = "STOP"
PID_FILE = "overnight.pid"
STATE_FILE = "state_machine.json"
ROADMAP_PATH = "docs/ROADMAP.md"
ALL_DONE_MARKER = "ALL_DONE"
COOLDOWN_FILE = "opencode_cooldown.json"

#: Deadline keys an opencode marker may use. ``endpoints/opencode.py`` writes
#: ``until``; hand-written / operator markers use the older ``cooldown_until``.
#: Either one must be honoured, or a real cooldown silently reads as "clear".
COOLDOWN_UNTIL_KEYS: tuple[str, ...] = ("until", "cooldown_until")

# ``- [ ]`` / ``- [x]`` anywhere at the start of a line (indented items too).
ROADMAP_PENDING_RE = re.compile(r"^[ \t]*-[ \t]+\[ \]", re.M)
ROADMAP_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+\[[ xX]\]", re.M)
#: First pending item *with* its body, used to name a round in prompts/commits.
ROADMAP_FIRST_PENDING_RE = re.compile(r"^[ \t]*-[ \t]+\[ \][ \t]*(.+)$", re.M)

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

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


# ---------------------------------------------------------------- pid probing
def _pid_alive_windows(pid: int) -> bool:
    """``PROCESS_QUERY_LIMITED_INFORMATION`` probe: read-only, never signals."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == _STILL_ACTIVE
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


def read_pid_file(path: str | Path) -> int | None:
    """Parse a pid file; None when absent, empty or unparsable."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


def scheduler_pid(runs_dir: str | Path) -> int | None:
    """PID recorded in ``<runs_dir>/overnight.pid`` (None when there is none)."""
    return read_pid_file(Path(runs_dir) / PID_FILE)


# ------------------------------------------------------- runs/STOP control file
def stop_path(runs_dir: str | Path) -> Path:
    return Path(runs_dir) / STOP_FILE


def stop_requested(runs_dir: str | Path) -> bool:
    """True when an operator dropped ``runs/STOP`` (never raises)."""
    try:
        return stop_path(runs_dir).is_file()
    except OSError:
        return False


def clear_stop_files(runs_dir: str | Path) -> None:
    """Remove ``STOP`` and ``overnight.pid`` from *runs_dir* (idempotent)."""
    for name in (STOP_FILE, PID_FILE):
        try:
            (Path(runs_dir) / name).unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------------------ atomic IO
def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``path`` via a sibling ``.tmp`` file + ``os.replace``.

    Same-directory temp keeps the replace atomic on NTFS; a crash mid-write
    leaves the previous complete file untouched instead of a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------ roadmap parsing
def count_roadmap_items(text: str) -> tuple[int, int]:
    """``(pending, total)`` checkbox counts for an in-memory roadmap body."""
    body = text or ""
    return (
        len(ROADMAP_PENDING_RE.findall(body)),
        len(ROADMAP_ITEM_RE.findall(body)),
    )


def roadmap_counts(path: str | Path) -> tuple[int, int]:
    """``(pending, total)`` for the roadmap at *path* (missing file ⇒ ``(0, 0)``)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    return count_roadmap_items(text)


def has_pending_roadmap_items(text: str) -> bool:
    """True when the roadmap still holds at least one ``- [ ]`` item."""
    return bool(ROADMAP_PENDING_RE.search(text or ""))


def has_all_done_marker(text: str) -> bool:
    """True only for a standalone ``ALL_DONE`` line (prose mentions ignored)."""
    for line in (text or "").splitlines():
        if line.strip().strip("`").strip() == ALL_DONE_MARKER:
            return True
    return False


def roadmap_all_done(path: str | Path) -> tuple[bool, str]:
    """``(done, reason)`` — done when the roadmap exists and owes nothing.

    ``ALL_DONE`` alone wins (the scheduler writes it to exit gracefully even if
    a checkbox was left behind by mistake), then "no pending checkbox".
    """
    target = Path(path)
    if not target.is_file():
        return False, "roadmap_missing"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, "roadmap_unreadable:%r" % exc
    if has_all_done_marker(text):
        return True, "ALL_DONE line present"
    if not has_pending_roadmap_items(text):
        return True, "no pending checkbox"
    return False, "pending items remain"


# ------------------------------------------------------- opencode cooldown read
def read_opencode_cooldown_until(
    path: str | Path, *, now: datetime | None = None
) -> str:
    """ISO stamp of an active opencode cooldown, or ``""`` when the lane is clear.

    Accepts both marker spellings (``until`` and ``cooldown_until``) because the
    driver writes one and operators/tooling write the other; honouring only one
    of them means a real cooldown silently reads as clear. Read-only: an
    unparsable, key-less or expired marker simply reads as clear.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    until_raw = ""
    for key in COOLDOWN_UNTIL_KEYS:
        value = data.get(key)
        if value:
            until_raw = str(value)
            break
    if not until_raw:
        return ""
    try:
        until = datetime.fromisoformat(until_raw)
    except ValueError:
        return ""
    return until_raw if until > (now or datetime.now()) else ""


# --------------------------------------------------------------- state machine
class StateMachine:
    """Explicit state machine that persists every transition to JSON.

    The file is rewritten on each transition so an operator (or the next round)
    can tell exactly where an unattended run stopped, even if the process was
    killed without unwinding.

    Two guarantees carried over from M4-1: writes go through a sibling ``.tmp``
    + ``os.replace`` so a crash can never leave truncated JSON, and a scheduler
    never overwrites the file of another scheduler that is still alive — it
    writes ``state_machine.<pid>.json`` instead, leaving the live file intact.

    The injection points exist so callers keep their historic contracts:
    ``runs_dir`` (a path or a zero-arg callable) honours a monkeypatched
    ``RUNS``, and ``pid_alive_fn`` / ``log_fn`` let a caller's patched
    ``pid_alive`` / ``log`` keep taking effect.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        runs_dir: str | Path | Callable[[], str | Path] | None = None,
        state_names: Iterable[str] = STATE_NAMES,
        pid_alive_fn: Callable[[int], bool] | None = None,
        log_fn: Callable[..., None] | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._runs_dir = runs_dir
        self._state_names = tuple(state_names)
        self._pid_alive = pid_alive_fn or pid_alive
        self._log = log_fn
        self._effective: Path | None = None
        self.state = "IDLE"
        self.reason = ""
        self.round = 0
        self.history: list[dict[str, Any]] = []

    def _runs(self) -> Path:
        base = self._runs_dir
        if callable(base):
            base = base()
        return Path(base) if base is not None else ROOT / "runs"

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log(message, "WARN")

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else self._runs() / STATE_FILE

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
        if foreign and foreign != os.getpid() and self._pid_alive(foreign):
            self._effective = base.with_name(
                "%s.%d%s" % (base.stem, os.getpid(), base.suffix)
            )
            self._warn(
                "state machine isolated: pid=%d is alive and holds %s; "
                "writing %s" % (foreign, base.name, self._effective.name)
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
        if state not in self._state_names:
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
            if self._log is not None:
                self._log("state machine write failed: %r" % exc, "ERROR")
