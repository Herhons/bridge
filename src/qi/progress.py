"""progress.json 读写（任务卡 01 / 计划第 7 项：监工入口）。

## 本文件负责的字段

只有四个是「本卡的」：`card_id, status, note, updated_at`。
别的字段（`verdicts`、`queue`、`running`、将来的扩展字段）**不归本模块管**，
写的时候原样保留——CARD-09e 第 1 条的原始事故是：`notify_done.py` 写完工状态时
拿四条字段整份覆盖，把整张 `verdicts` 表和 `queue` 一起抹掉了。

所以写入是**读-改-写合并**（CARD-09e 第 1 条）：

    加锁 → 读现有 → 只替换本卡四个字段 → 同目录临时文件 → os.replace → 解锁

`notify_failed` 是「上一次通知没发出去」的标记，属于**这一次**写入的附注：
新写一条时清掉，通知失败时再补上（`mark_notify_failed`）。

## 并发

服务端（`qi/server.py` 的后台任务）和命令行可能同时写，所以读+写必须在同一把锁里。
锁是**同目录的 `progress.json.lock`**，Windows 走 `msvcrt.locking`（进程退出即释放，
不会留下僵尸锁），其他平台走 `fcntl.flock`。等锁最多 `LOCK_TIMEOUT` 秒，
等不到抛 `TimeoutError`（是 `OSError` 的子类，调用方按"写文件失败"处理即可）。

读不到或读坏了都返回空 dict，不抛异常。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:                                    # Windows
    import msvcrt
except ImportError:                     # pragma: no cover - 非 Windows
    msvcrt = None
try:                                    # POSIX
    import fcntl
except ImportError:
    fcntl = None

ROOT = Path(__file__).resolve().parents[2]
FILENAME = "progress.json"
STATUSES = ("running", "done", "stuck", "failed")
NOTE_LIMIT = 200
BEIJING = timezone(timedelta(hours=8))
LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.05

# 本卡自己的四个字段：合并时只替换它们，其余键一个都不动
OWN_FIELDS = ("card_id", "status", "note", "updated_at")


def path(root: str | Path | None = None) -> Path:
    """progress.json 的完整路径；root 只给测试换目录用。"""
    base = Path(root) if root is not None else ROOT
    return base / FILENAME


def lock_path(root: str | Path | None = None) -> Path:
    """锁文件紧跟数据文件，避免跨机器/跨盘锁不上。"""
    target = path(root)
    return target.with_name(target.name + ".lock")


def beijing_now() -> str:
    """北京时间 ISO 8601，带 +08:00，不随本机时区变。"""
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def clean_note(note: object) -> str:
    """压成一行并截断：note 只留一句话。"""
    one_line = " ".join(str(note or "").split())
    if len(one_line) > NOTE_LIMIT:
        one_line = one_line[: NOTE_LIMIT - 3] + "..."
    return one_line


def read(root: str | Path | None = None) -> dict:
    """读当前进度；文件不存在或不是合法 JSON 对象时返回 {}。"""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _acquire(handle, timeout: float) -> None:
    """拿排他锁；等不到抛 TimeoutError（OSError 子类）。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:                       # pragma: no cover - POSIX
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等 progress.json 的文件锁超过 {timeout}s") from None
            time.sleep(LOCK_POLL)


def _release(handle) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:                           # pragma: no cover - POSIX
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:                       # 解锁失败不该盖住真正的异常
        pass


@contextmanager
def locked(root: str | Path | None = None, *, timeout: float = LOCK_TIMEOUT,
           file_path: str | Path | None = None):
    """同目录文件锁。锁文件本身不删——删一把别人可能正持有的锁是竞态。

    **打开也要重试**：Windows 的字节范围锁是强制锁，别人持锁时连
    `open(..., "a+b")` 都可能直接被拒（PermissionError 13）。
    所以"打开 + 加锁"整段放进同一个超时循环里。
    """
    target = Path(file_path) if file_path is not None else path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = None
    while True:
        try:
            handle = open(target.with_name(target.name + ".lock"), "a+b")
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等 progress.json 的文件锁超过 {timeout}s") from None
            time.sleep(LOCK_POLL)
    try:
        _acquire(handle, max(0.01, deadline - time.monotonic()))
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


def save(record: dict, root: str | Path | None = None) -> Path:
    """整份原子写：同目录临时文件 -> os.replace。

    临时文件名带 pid + 线程号：同一个进程里两个线程同时写时不会踩到对方的半成品
    （只带 pid 会撞名，实测报 PermissionError）。
    """
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def merge(existing: dict, card_id: object, status: str, note: object = "") -> dict:
    """把本卡四个字段合进现有内容；`verdicts` / `queue` 等原样保留。

    纯函数，方便单测直接盯「哪些键被动了」。
    """
    card = str(card_id if card_id is not None else "").strip()
    if not card:
        raise ValueError("card_id is required")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    fresh = {"card_id": card, "status": status, "note": clean_note(note),
             "updated_at": beijing_now()}
    rest = {key: value for key, value in dict(existing or {}).items()
            if key not in OWN_FIELDS and key != "notify_failed"}
    return {**fresh, **rest}


def write(card_id: object, status: str, note: object = "", *, root: str | Path | None = None) -> dict:
    """读-改-写一条进度并返回它；状态非法或卡号为空时抛 ValueError。

    整个「读 + 写」在文件锁里完成：别的进程在这中间写的字段不会丢。
    """
    with locked(root):
        record = merge(read(root), card_id, status, note)
        save(record, root=root)
    return record


def mark_notify_failed(record: dict, *, root: str | Path | None = None) -> dict:
    """通知发不出去时补一个 notify_failed=true。

    只加这一个键，其余字段和 updated_at 保持不动；文件被别人改过就按**文件现状**加，
    不拿调用方手里那份旧记录去覆盖别人的新写入。
    """
    with locked(root):
        current = read(root)
        updated = dict(current or record)
        updated["notify_failed"] = True
        save(updated, root=root)
    return updated
