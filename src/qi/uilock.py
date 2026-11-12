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
import secrets
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
#: 一段桌面操作（聚焦/粘贴/点击/复制）最多等多久拿到锁。**不是**整轮预算。
#: 2026-11-08（B14）：20 → 60。并发时会审的 choose 子进程是**整轮持锁**的，
#: 它一跑十几秒，别的席的发送段如果只等 20s 就会白白丢掉一次发送（stage=ui_busy）。
#: 这里只放大"等锁上限"，互斥语义一个字没变。
SECTION_TIMEOUT = 60.0
#: 同端回合锁的整轮上限（要盖住 review 的 1500s + 收尾）。
TURN_TIMEOUT = 1800.0
#: `=1` 时入口走"分段持锁"（B13b）：只在桌面动作处短锁，等回答期间放锁。
SECTIONED_ENV = "BRIDGE_UI_SECTIONED"
#: `=1` 时说明"有别的席位在同时抢桌面"（前台是共享资源）。
CONCURRENT_ENV = "BRIDGE_UI_CONCURRENT"

# Parent delegation is an accidental-misuse boundary, not a sandbox against a
# malicious local user who can edit this code/metadata. Environment alone never
# bypasses a lock: exact parent/child identities, a per-launch capability, and a
# live kernel lock must all agree. The child retains a shared kernel lease even
# if the parent exits; unrelated CLIs cannot enter until that child exits too.
DELEGATION_ENV = "BRIDGE_UI_LOCK_DELEGATION"
_LOCAL = threading.local()


class Busy(RuntimeError):
    """等不到锁。命令行按退出码 8（独立于「让位」的 4）处理。"""

    stage = "ui_busy"
    code = ec.UI_LOCK_BUSY


# ---------------------------------------------------------------- 路径 / 时间
def lock_path(root: str | Path | None = None, name: str | None = None) -> Path:
    """UI 锁的路径。

    `name=None` → 桌面操作那把全局锁（`runs/ui.lock`）。
    `name="gemini"` → 该端的**回合锁**（`runs/turn/gemini.lock`），
    只在"同一个端被两个自动化同时动"时起作用，与桌面锁互不干扰（不同 path，可嵌套）。
    """
    base = Path(root) if root is not None else ROOT
    if name:
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(name))[:40]
        return base / "runs" / "turn" / f"{safe}.lock"
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
    try:                                      # POSIX identity, no guessed PID
        return int(Path(f"/proc/{value}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
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
        try:
            handle = open(path, "w+b", buffering=0)
        except PermissionError as exc:
            raise Busy(f"ui lock not creatable: {path} ({exc})") from exc
    except PermissionError as exc:
        # 2026-11-07 实测：另一个自动化进程正持锁时，锁文件可能连打开都被拒绝
        # （独占共享模式 / ACL）。这不是"程序出错"，是"UI 被占着"——
        # 必须按 ui_busy（退出码 8）干净退出，绝不把 traceback 甩给调用方，
        # 也绝不绕过锁去点桌面。
        raise Busy(f"ui lock not openable: {path} ({exc})") from exc
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


def _stale_marker(meta: dict) -> dict:
    """把「持有者已死」的元数据改写成一个可读的墓碑，供事后追查。"""
    return {"pid": None, "owner": "stale", "stale_pid": meta.get("pid"),
            "stale_owner": meta.get("owner"), "released_at": _now(),
            "updated_at": _now(), "released_by": "stale"}


def clear_stale(*, root: str | Path | None = None) -> dict:
    """清掉「持有者已死」的残留锁。续跑/开跑前调用。

    为什么需要：字节锁由内核在进程退出时释放，所以死进程**不会真的挡住**下一次
    加锁；但元数据里还留着它的 pid/owner，现场看上去像"前台被占"，排查时会误导。
    这里只在**确认持有者已死**时改写元数据；活着的一律不动。

    返回 `{"cleared": bool, "reason": absent|held|alive|no_meta|stale", ...}`。
    """
    path = lock_path(root).resolve()
    if not path.exists():
        return {"cleared": False, "reason": "absent", "path": str(path)}
    handle = _open(path)
    locked = False
    try:
        try:
            _try_lock(handle)
            locked = True
        except OSError:
            meta = _read_meta(handle) or {}
            return {"cleared": False, "reason": "held", "path": str(path),
                    "pid": meta.get("pid"), "owner": meta.get("owner")}
        meta = _read_meta(handle) or {}
        if not meta:
            return {"cleared": False, "reason": "no_meta", "path": str(path)}
        if not _holder_dead(meta):
            return {"cleared": False, "reason": "alive", "path": str(path),
                    "pid": meta.get("pid"), "owner": meta.get("owner")}
        _write_meta(handle, _stale_marker(meta))
        return {"cleared": True, "reason": "stale", "path": str(path),
                "pid": meta.get("pid"), "owner": meta.get("owner")}
    finally:
        if locked:
            _unlock(handle)
        handle.close()


# ---------------------------------------------------------------- 父子代持
def _aux_lock(handle, *, shared=False):
    """Shared lifetime lease or exclusive child-entry lock (separate files)."""
    if os.name != "nt":                        # pragma: no cover - Windows host
        import fcntl
        fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    | fcntl.LOCK_NB)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel.LockFileEx
    fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                   wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    fn.restype = wintypes.BOOL
    overlap = OVERLAPPED(Offset=LOCK_OFFSET)
    if not fn(msvcrt.get_osfhandle(handle.fileno()), 1 | (0 if shared else 2),
              0, 1, 0, ctypes.byref(overlap)):
        raise ctypes.WinError(ctypes.get_last_error())


def _aux_unlock(handle):
    if os.name != "nt":                        # pragma: no cover
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel.UnlockFileEx
    fn.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                   wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
    fn.restype = wintypes.BOOL
    overlap = OVERLAPPED(Offset=LOCK_OFFSET)
    if not fn(msvcrt.get_osfhandle(handle.fileno()), 0, 1, 0, ctypes.byref(overlap)):
        raise ctypes.WinError(ctypes.get_last_error())


def _active(path):
    # Key reentry by canonical lock path AND process, never just thread depth.
    return next((item for item in reversed(getattr(_LOCAL, "stack", []))
                 if item["path"] == path and item["pid"] == os.getpid()), None)


class _ChildLaunch:
    def __init__(self, context, token, env):
        self.context, self.token, self.env = context, token, env

    def bind(self, pid):
        start = process_start_time(pid)
        if not start:
            raise Busy("cannot verify delegated child process identity")
        self.context["meta"]["delegate"].update(pid=int(pid), process_start_time=start)
        _write_meta(self.context["handle"], self.context["meta"])


@contextmanager
def child_launch(*, root=None, env=None):
    """Per-Popen authorization; no global os.environ mutation.

    The child waits for bind(pid) before entering. Only an owning hold on this
    exact root can delegate. A delegated child cannot mint grandchildren grants.
    """
    context = _active(lock_path(root).resolve())
    child_env = dict(os.environ if env is None else env)
    child_env.pop(DELEGATION_ENV, None)
    if context is None:
        yield _ChildLaunch(None, None, child_env)
        return
    if context.get("delegated"):
        raise Busy("delegated UI child cannot delegate again")
    if context["meta"].get("delegate"):
        raise Busy("a UI child launch is already active")
    if not context.get("shared"):
        _aux_unlock(context["lease"])
        # Main byte lock stays owned throughout this conversion. Ordinary hold
        # always takes main before trying the lease and never waits with main.
        _aux_lock(context["lease"], shared=True)
        context["shared"] = True
    token = secrets.token_urlsafe(32)
    meta = context["meta"]
    if not meta.get("process_start_time"):
        raise Busy("cannot verify parent process identity")
    meta["delegate"] = {"token": token, "pid": None, "process_start_time": None}
    _write_meta(context["handle"], meta)
    child_env[DELEGATION_ENV] = json.dumps({"path": str(context["path"]),
        "pid": os.getpid(), "process_start_time": meta["process_start_time"],
        "session": meta["session"], "token": token})
    try:
        yield _ChildLaunch(context, token, child_env)
    finally:
        meta.pop("delegate", None)
        _write_meta(context["handle"], meta)


def _verify_child(handle, grant):
    meta = _read_meta(handle) or {}
    parent = grant.get("pid")
    if (parent != os.getppid() or not grant.get("process_start_time")
            or process_start_time(parent) != grant["process_start_time"]
            or meta.get("pid") != parent
            or meta.get("process_start_time") != grant["process_start_time"]
            or not grant.get("session") or meta.get("session") != grant["session"]
            or meta.get("released_at")):
        raise Busy("invalid or stale UI parent identity/session")
    delegate = meta.get("delegate") or {}
    if not grant.get("token") or delegate.get("token") != grant["token"]:
        raise Busy("invalid or expired UI child capability")
    if delegate.get("pid") is None:
        return None                            # Popen registration handshake
    if (delegate.get("pid") != os.getpid()
            or not delegate.get("process_start_time")
            or process_start_time(os.getpid()) != delegate["process_start_time"]):
        raise Busy("UI capability belongs to a different child identity")
    try:
        _try_lock(handle)
    except OSError:
        return meta                            # kernel still owns parent lock
    else:
        _unlock(handle)
        raise Busy("UI delegation has no active kernel parent lock")


@contextmanager
def _delegated_hold(path, grant, deadline, poll):
    if not isinstance(grant, dict) or grant.get("path") != str(path):
        raise Busy("UI delegation root/path mismatch")
    if not path.is_file():
        raise Busy("UI delegation lock is missing")
    handle = _open(path)
    lease = _open(path.with_name(path.name + ".lease"))
    entry = _open(path.with_name(path.name + ".entry"))
    leased = entered = False
    try:
        while True:
            # Validate even when another child is busy. Stale grants cannot
            # fall back to ordinary acquisition after the parent releases.
            meta = _verify_child(handle, grant)
            if meta is not None:
                try:
                    _aux_lock(lease, shared=True)
                    leased = True
                    _aux_lock(entry)
                    entered = True
                    meta = _verify_child(handle, grant)
                    if meta is not None:
                        break
                except OSError:
                    pass
                if entered:
                    _aux_unlock(entry)
                    entered = False
                if leased:
                    _aux_unlock(lease)
                    leased = False
            if time.monotonic() >= deadline:
                raise Busy("UI child authorization/entry timed out")
            time.sleep(poll)
        context = {"path": path, "pid": os.getpid(), "delegated": True,
                   "meta": meta, "handle": handle, "lease": lease}
        _LOCAL.stack.append(context)
        try:
            yield dict(meta, delegated_pid=os.getpid())
        finally:
            _LOCAL.stack.remove(context)
    finally:
        if entered:
            _aux_unlock(entry)
        if leased:
            _aux_unlock(lease)
        entry.close()
        lease.close()
        handle.close()


# ---------------------------------------------------------------- 取锁 / 释放
@contextmanager
def hold(owner: str, *, timeout: float = DEFAULT_TIMEOUT, root: str | Path | None = None,
         poll: float = POLL, announce: bool = False, name: str | None = None,
         delegation: bool = True):
    """Own one UI transaction, or join its verified per-child delegation.

    Same-thread reentry is restricted to the same canonical lock path. Separate
    shared leases preserve exclusion if the owning parent exits before a child.

    `name` 给了就锁那一把"回合锁"（`runs/turn/<name>.lock`），用于同端互斥。

    `delegation=False` 表示**不认委派凭证**：委派只对一个 path 有效（主
    `runs/ui.lock`），拿它去比另一把锁必然"root/path mismatch"。
    """
    path = lock_path(root, name).resolve()
    if not hasattr(_LOCAL, "stack"):
        _LOCAL.stack = []
    if _active(path):
        yield None
        return
    deadline = time.monotonic() + max(0.0, timeout)
    raw = os.environ.get(DELEGATION_ENV) if delegation else None
    if raw is not None:
        try:
            grant = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise Busy("malformed UI delegation") from exc
        with _delegated_hold(path, grant, deadline, poll) as meta:
            yield meta
        return

    handle = _open(path)
    lease = _open(path.with_name(path.name + ".lease"))
    locked = leased = False
    stale_retries = 0
    announced = False
    try:
        while True:
            try:
                _try_lock(handle)
                locked = True
                _aux_lock(lease)
                leased = True
                break
            except OSError:
                if locked:
                    _unlock(handle)
                    locked = False
                meta = _read_meta(handle) or {}
                if announce and not announced:
                    announced = True
                    print(f"[lock] UI busy: owner={meta.get('owner')} pid={meta.get('pid')}",
                          flush=True)
                if _holder_dead(meta) and stale_retries < 3:
                    # 持有者已死：内核早该释放字节锁，这里把墓碑写好并**立刻重试**，
                    # 不再空等到 timeout（续跑时最常见：上一条命令被 kill）。
                    _write_meta(handle, _stale_marker(meta))
                    stale_retries += 1
                    continue
                if time.monotonic() >= deadline:
                    raise Busy(f"ui lock busy: owner={meta.get('owner')} pid={meta.get('pid')}")
                time.sleep(poll)

        meta = {"pid": os.getpid(), "owner": str(owner), "acquired_at": _now(),
                "process_start_time": process_start_time(os.getpid()),
                "updated_at": _now(), "session": secrets.token_urlsafe(32)}
        _write_meta(handle, meta)
        context = {"path": path, "pid": os.getpid(), "handle": handle,
                   "lease": lease, "meta": meta, "shared": False, "delegated": False}
        _LOCAL.stack.append(context)
        try:
            yield meta
        finally:
            _LOCAL.stack.remove(context)
            released = dict(meta)
            released.pop("delegate", None)
            released["released_at"] = released["updated_at"] = _now()
            _write_meta(handle, released)
    finally:
        if locked:
            _unlock(handle)
        if leased:
            _aux_unlock(lease)
        lease.close()
        handle.close()


def run(name: str, func, *, timeout: float | None = None,
        root: str | Path | None = None) -> int:
    """命令行入口用：持锁调用 func；拿不到锁打印一行 JSON 并返回 8。"""
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


def entry(name: str, func, *, root: str | Path | None = None) -> int:
    """命令行入口（B13）：同一端互斥 + （默认）整轮持锁。

    锁的两种模式由 `BRIDGE_UI_SECTIONED` 决定：

    * 未设置（**默认，今天的行为**）：整轮持 `ui.lock`。等回答期间也持着，
      所以多端仍然串行 —— 但绝不比改动前更不安全。
    * `=1`（B13b 落地后由服务端下发）：不持整轮锁，只在调用点用 `section()`
      包住"会动桌面的那几段"，等回答期间放锁。这样多端才能真并发
      （CALIBER §3.1：不得持全局锁串行等待完整回答）。

    两种模式都额外持一把**同端回合锁**（`runs/turn/<端>.lock`）：
    它只挡"同一个端被两份自动化同时动"，不影响不同端之间的并发。
    """
    timeout = None
    try:
        timeout = float(os.environ.get("BRIDGE_UI_LOCK_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    sectioned = os.environ.get(SECTIONED_ENV) == "1"
    try:
        with turn(name, root=root):
            if sectioned:
                return int(func())
            with hold(name, timeout=timeout, root=root, announce=True):
                return int(func())
    except Busy as exc:
        print(json.dumps({"ok": False, "stage": exc.stage, "error": str(exc)},
                         ensure_ascii=False))
        return exc.code


def sectioned() -> bool:
    """当前是不是"分段持锁"模式（并发）。"""
    return os.environ.get(SECTIONED_ENV) == "1"


def concurrent_mode() -> bool:
    """当前是不是"多席并发"模式（不管持锁方式）。

    与 `sectioned()` 的区别：`choose` 必须**整轮持锁**（它要连着开菜单、点条目、
    读回），所以它不设 `SECTIONED`；但它同样处在多席并发里，前台会被别人抢走
    （网页端新建窗口就会抢）。所以"前台是否共享"单看这个开关。
    """
    return os.environ.get(CONCURRENT_ENV) == "1"


def section(owner: str = "ui", *, timeout: float = SECTION_TIMEOUT,
            root: str | Path | None = None):
    """一段桌面操作的短临界区。

    **只有会动桌面的动作需要它**：聚焦、粘贴、点击发送、点复制、清空输入框。
    纯 UIA 读（`ui.tree`）不抢前台、不碰剪贴板，不需要持锁。
    同线程重入是安全的（`hold` 按 canonical path 判重，直接放行）。
    """
    return hold(owner, timeout=timeout, root=root)


def turn(endpoint: str, *, timeout: float = TURN_TIMEOUT,
         root: str | Path | None = None):
    """同端回合锁：整轮持有，只挡"同一个端被两份自动化同时动"。

    与桌面锁是**两个不同的 path**，可以嵌套，不会互锁。

    **必须 `delegation=False`**：父进程给子进程的委派凭证绑的是
    `runs/ui.lock`，拿它来比对 `runs/turn/<端>.lock` 会立刻
    `Busy("UI delegation root/path mismatch")` —— 2026-11-08 真机实测，
    服务端派发的每个 choose/use 子进程都在这一步 exit 8。
    """
    return hold(f"turn:{endpoint}", timeout=timeout, root=root, name=endpoint,
                delegation=False)


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
                lease_path = path.with_name(path.name + ".lease")
                if lease_path.exists():
                    with open(lease_path, "r+b", buffering=0) as lease:
                        try:
                            _aux_lock(lease)
                        except OSError:
                            meta["locked"] = True
                            meta["child_lease"] = True
                        else:
                            _aux_unlock(lease)
            return meta
    except OSError:
        return None
