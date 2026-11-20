"""Cross-process UI lock with automatic dead-PID stale recovery.

Unlike v0.1.0, an orphaned lock file left behind by a crashed process is
detected and reclaimed automatically instead of permanently blocking callers.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from engine.config import RUNS_DIR

_LOCAL = threading.local()


class LockBusyError(RuntimeError):
    """Raised when the UI lock cannot be acquired within the timeout."""


def pid_alive(pid: int | None) -> bool:
    """Return True if *pid* refers to a live OS process, False otherwise."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_stale_lock(lock_path: Path) -> bool:
    """Remove *lock_path* if its owning PID is no longer alive.

    Returns True if a stale lock was removed.
    """
    if not lock_path.exists():
        return False
    meta = _read_lock_meta(lock_path)
    if meta is None:
        # Corrupt or empty lock older than 5 seconds -> safe to reclaim.
        try:
            if time.time() - lock_path.stat().st_mtime > 5.0:
                lock_path.unlink(missing_ok=True)
                return True
        except OSError:
            pass
        return False
    owner_pid = meta.get("pid")
    if owner_pid == os.getpid():
        return False
    if not pid_alive(owner_pid):
        try:
            lock_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
    return False


def describe_lock(lock_path: Path | None = None) -> dict | None:
    path = lock_path or (RUNS_DIR / "ui.lock")
    if not path.exists():
        return None
    meta = _read_lock_meta(path) or {}
    meta["alive"] = pid_alive(meta.get("pid"))
    return meta


@contextmanager
def hold_ui_lock(
    owner: str,
    *,
    timeout: float = 120.0,
    poll: float = 0.1,
    lock_path: Path | None = None,
) -> Iterator[Path]:
    """Acquire the shared UI foreground token (re-entrant within the same thread)."""
    path = lock_path or (RUNS_DIR / "ui.lock")
    key = str(path.resolve())
    depth_map: dict[str, int] = getattr(_LOCAL, "depth", None)
    if depth_map is None:
        depth_map = {}
        _LOCAL.depth = depth_map

    if depth_map.get(key, 0) > 0:
        depth_map[key] += 1
        try:
            yield path
        finally:
            depth_map[key] -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout))
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "owner": owner,
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": sys.executable,
        },
        ensure_ascii=False,
    )

    acquired = False
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if clear_stale_lock(path):
                continue
            if time.monotonic() >= deadline:
                holder = _read_lock_meta(path) or {}
                raise LockBusyError(
                    f"UI lock busy after {timeout:.1f}s (held by pid={holder.get('pid')} owner={holder.get('owner')!r})"
                )
            time.sleep(poll)

    depth_map[key] = 1
    try:
        yield path
    finally:
        depth_map[key] -= 1
        if acquired and depth_map[key] == 0:
            try:
                meta = _read_lock_meta(path)
                if meta is None or meta.get("pid") == os.getpid():
                    path.unlink(missing_ok=True)
            except OSError:
                pass
