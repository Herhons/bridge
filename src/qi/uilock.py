"""跨进程 UI 前台锁（CARD-09a 第 6 条）。

## 为什么是文件锁，不是 threading.Lock

前台只有一个焦点：choose 点菜单、use 打字发送，两件事不能交错。服务里用
`threading.Lock` 只能挡住同一进程的线程；命令行直接跑 `grok_use.py` 是另一个进程，
照样会在服务正在点菜单时抢焦点。所以锁必须落到磁盘上，所有进程抢同一个文件：

    runs\\ui.lock

锁文件同时记 pid 与时间（`pid` / `owner` / `acquired_at` / `released_at`），
出问题时能直接看出「谁持着、持了多久」，不用猜。

## 怎么锁

- 互斥用操作系统的字节锁：Windows 是 `msvcrt.locking`，POSIX 是 `flock`。
  **进程退出（含被强杀）时内核自动释放**，所以「持锁进程已死 → 自动释放」不需要
  额外清理逻辑。
- 锁的字节固定在偏移 4096，前 4096 字节留给元数据。这样写元数据不会碰到被锁的字节，
  不会出现「锁着自己的元数据」这种自锁。
- 文件不存在时创建；先补足到 4097 字节再锁，避免锁到 EOF 之外的字节。

## 超时与让位

`hold(..., timeout=)` 等不到锁就抛 `Busy`（退出码 8，独立于「让位」的 4）。
不静默排队：服务端卡住时，命令行应该立刻告诉调用方「前台被占了」，
而不是假装在工作。命令行等锁期间会打印当前持有者的 owner / pid / 时间。

## 命令行入口

    from qi import uilock
    raise SystemExit(uilock.run("grok_use", main))

`run()` 负责取锁、调用、释放；拿不到锁时打印一行 JSON 并返回 8。
包 `*_use.py` 与 `*_choose.py`（CARD-09f：choose 也要全程持锁）；
`evidence\` 与 `tools\` 下的临时探针同样必须经 `hold()`。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import exit_codes as ec

ROOT = Path(__file__).resolve().parents[2]
LOCK_NAME = "ui.lock"

META_SIZE = 4096          # 前 4KB 放元数据
LOCK_OFFSET = 4096        # 被锁的字节；写在元数据之后，互不干扰
DEFAULT_TIMEOUT = 60.0
POLL = 0.05

_LOCAL = threading.local()


class Busy(RuntimeError):
    """等不到锁。命令行按退出码 8（独立于「让位」的 4）处理。"""

    stage = "ui_busy"
    code = ec.UI_LOCK_BUSY


# ---------------------------------------------------------------- 路径 / 时间
def lock_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else ROOT
    return base / "runs" / LOCK_NAME


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------- 进程存活
def pid_alive(pid) -> bool | None:
    """进程还在不在。读不到（权限/参数不对）返回 None，不猜。"""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, value)
            if not handle:
                # ERROR_ACCESS_DENIED(5)：进程在，只是问不了；其它按不存在算
                return kernel32.GetLastError() == 5
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                    # noqa: BLE001 - 诊断信息，不许带崩主流程
            return None
    try:
        os.kill(value, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_time(pid) -> int | None:
    """进程启动时间（FILETIME，100ns 单位）。读不到返回 None，不猜。

    为什么要它：pid 会被系统回收复用。只凭 pid 判断「持有者还活着」，
    遇到 pid 复用就会把「旧持有者已死、新进程恰好用了同一个 pid」误判成
    「持有者还在」，锁就卡死到超时。pid + 启动时间一起比才判得准。
    """
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, value)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel, user = (FILETIME(), FILETIME(),
                                                 FILETIME(), FILETIME())
                if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                                ctypes.byref(exited), ctypes.byref(kernel),
                                                ctypes.byref(user)):
                    return None
                return (int(created.high) << 32) | int(created.low)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:                    # noqa: BLE001 - 诊断信息，不许带崩主流程
            return None
    return None


# ---------------------------------------------------------------- 文件与字节锁
def _open(path: Path):
    """打开锁文件（不截断）。文件短于锁偏移时先补足，否则锁到 EOF 之外不可靠。

    必须是**无缓冲**的 `buffering=0`：Windows 的字节锁是强制锁，带缓冲的读会
    预读一整块（8KB）并把被锁的第 4096 字节一起读进来，直接 `PermissionError`。
    换成 FileIO 后 read(4096) 只读 4096 字节，不会碰到锁区。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(path, "r+b", buffering=0)
    except FileNotFoundError:
        handle = open(path, "w+b", buffering=0)
    handle.seek(0, os.SEEK_END)
    if handle.tell() < LOCK_OFFSET + 1:
        handle.write(b"\0" * (LOCK_OFFSET + 1 - handle.tell()))
    return handle


def _try_lock(handle) -> None:
    handle.seek(LOCK_OFFSET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:                                    # pragma: no cover - 本机是 Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    try:
        handle.seek(LOCK_OFFSET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:                                # pragma: no cover
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:                        # noqa: BLE001 - 解锁失败不该带崩
        pass


def _read_meta(handle) -> dict | None:
    try:
        handle.seek(0)
        raw = handle.read(META_SIZE).rstrip(b"\x00 ").strip()
        if not raw:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_meta(handle, meta: dict) -> None:
    raw = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
    raw = raw[:META_SIZE].ljust(META_SIZE, b" ")
    handle.seek(0)
    handle.write(raw)
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _holder_dead(meta: dict) -> bool:
    """元数据里的持有者是不是已经死了（或 pid 复用导致身份对不上）。

    只有明确判死才返回 True；读不到进程状态（None）一律按「还活着」保守处理，
    宁可多等也不误杀——字节锁由内核在持有者进程退出时自动释放，这里只是
    加速诊断与重试，不是替代内核。
    """
    pid = meta.get("pid")
    if not pid:
        return False
    alive = pid_alive(pid)
    if alive is False:
        return True
    if alive is True and meta.get("process_start_time") is not None:
        start = process_start_time(pid)
        if start is not None and start != meta.get("process_start_time"):
            return True                       # pid 复用：活着，但不是当初那个持有者
    return False


# ---------------------------------------------------------------- 取锁 / 释放
@contextmanager
def hold(owner: str, *, timeout: float = DEFAULT_TIMEOUT, root: str | Path | None = None,
         poll: float = POLL, announce: bool = False):
    """持锁执行。拿不到锁抛 `Busy`，带上当前持有者的 pid / owner / 时间。

    同一线程可重入（内层不再抢文件锁）：避免服务里「已经持锁又调一层」时自锁。

    `announce=True`（命令行入口用）时，等锁期间打印一次持有者信息；服务端
    静默等待，不往流里写字。
    """
    depth = getattr(_LOCAL, "depth", 0)
    if depth:
        _LOCAL.depth = depth + 1
        try:
            yield None
        finally:
            _LOCAL.depth -= 1
        return

    path = lock_path(root)
    deadline = time.monotonic() + max(0.0, timeout)
    handle = _open(path)
    announced = False
    try:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                meta = _read_meta(handle) or {}
                if announce and not announced:
                    announced = True
                    if _holder_dead(meta):
                        print(f"[锁] runs\\ui.lock 的持有者 {meta.get('owner') or '?'}"
                              f"（pid={meta.get('pid')}）已退出，内核已释放字节锁，重试…",
                              flush=True)
                    else:
                        print(f"[锁] runs\\ui.lock 被 {meta.get('owner') or '?'}"
                              f"（pid={meta.get('pid')}，since={meta.get('acquired_at')}）"
                              f"占用，最多等 {max(0.0, timeout):.0f}s…", flush=True)
                if time.monotonic() >= deadline:
                    holder = meta.get("owner") or "?"
                    raise Busy(
                        f"ui lock busy: owner={holder} pid={meta.get('pid')} "
                        f"since={meta.get('acquired_at')}")
                time.sleep(poll)
    except BaseException:
        handle.close()
        raise

    meta = {"pid": os.getpid(), "owner": str(owner), "acquired_at": _now(),
            "process_start_time": process_start_time(os.getpid()),
            "updated_at": _now()}
    _write_meta(handle, meta)
    _LOCAL.depth = 1
    try:
        yield meta
    finally:
        _LOCAL.depth = 0
        released = dict(meta)
        released["released_at"] = _now()
        released["updated_at"] = released["released_at"]
        try:
            _write_meta(handle, released)
        finally:
            _unlock(handle)
            handle.close()


def run(name: str, func, *, timeout: float | None = None,
        root: str | Path | None = None) -> int:
    """命令行入口用：持锁调用 func；拿不到锁打印一行 JSON 并返回 4。"""
    if timeout is None:
        try:
            timeout = float(os.environ.get("BRIDGE_UI_LOCK_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
    try:
        with hold(name, timeout=timeout, root=root, announce=True):
            return int(func())
    except Busy as exc:
        print(json.dumps({"ok": False, "stage": exc.stage, "error": str(exc)},
                         ensure_ascii=False))
        return exc.code


def describe(root: str | Path | None = None) -> dict | None:
    """只读诊断：谁持着锁、何时拿的。文件不存在返回 None。"""
    path = lock_path(root)
    if not path.exists():
        return None
    try:
        with open(path, "r+b", buffering=0) as handle:
            meta = _read_meta(handle) or {}
            try:
                _try_lock(handle)
            except OSError:
                meta["locked"] = True
            else:
                _unlock(handle)
                meta["locked"] = False
            return meta
    except OSError:
        return None
