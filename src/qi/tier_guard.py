"""档位回执：`*_choose` 读回确认后落一张凭证，`*_use` 发送前必须拿它对上（CARD-06a (a)）。

## 它解决什么

2026-11-01 巡检里反复出现同一类事故：**档位是"以为切了"，不是"读回来确认了"**。
choose 说 done、use 照发，实际档位还在最低档——会审用最低档跑、验收用测试档跑，
结论不可信。所以把"切档"和"发送"用一张回执绑起来：

    choose 读回确认 → 写 runs\\tier\\<端>.json
    use    --purpose → 校验回执（端/档/时间/窗口/用途）→ 原子消费 → 才发送

## 回执长什么样

    {
      "schema_version": 1,
      "endpoint": "grok",              # 端
      "dimension": "model",            # model / effort / think
      "target": "expert",              # 目标档
      "readback": "expert",            # 读回档（必须等于 target 才有意义）
      "writer": "chat",                # 在哪个模式下选的（CARD-06c；不分模式的端为 null）
      "hwnd": 526630, "pid": 1234,     # 窗口
      "process_start_time": 133... ,   # 进程启动时间（FILETIME 100ns）
      "tab": 2,                        # tab 标识（网页端标签序号）
      "created_at": "2026-11-02T05:40:12.345678+00:00",   # UTC 时间戳
      "created_monotonic": 12345.67,   # 同时刻的单调时钟读数
      "monotonic_origin": 12000.11,    # 本进程启动时的单调时钟基准
      "raw": "{...}"                   # 读回原文（原样存，不加工）
    }

## 用途（purpose）

    test        最低档：只验链路，别烧额度
    review      最高档：会审
    accept      最高档：验收
    accept_lite 独立的白名单，只给 opencode 的 GLM-5.3-Flash-Free（CARD-06c 第 6 条）：
                链路能验，但它不是旗舰档，结论必须写 PASS(GLM-lite)，不能混进 accept。

## 校验不过 → 退出码 5 / stage=tier_unconfirmed

缺失、schema 不符、读回档不符（target≠readback）、超 120 秒、时间戳在未来、
hwnd/pid/进程启动时间对不上、已被消费、用途要求的档位与读回不符 → 一律 5，
并提示先跑对应的 `*_choose`。**use 绝不自己切档**：切档只有 choose 能做。

## 消费（防并发重放）

发送动作**之前**在 `runs\\tier\\<端>.lock` 上加排它锁，重读回执、标 `consumed`、
原子写回，然后才返回。发送失败、只发出去一部分，也照样算作废——必须重新 choose。
两个 use 同时抢同一张回执时，只有一个能拿到锁并把 consumed 从 false 翻成 true。

## 安全边界（必须写清楚，别把它当成鉴权）

这张回执**只防误用，不防恶意伪造**：本机单用户，任何人能写 `runs\\tier\\*.json` 就能
自己造一张回执。所以这里**不引入密钥、不做 HMAC、不做签名**——加了也挡不住同机同用户的
伪造，只会制造"我们有鉴权"的错觉。它真正保证的是：

  · 档位是**读回来**的，不是"点了一下以为切了"；
  · 回执**只能用一次**，不能拿着上午的回执下午接着发；
  · 回执绑定了**当时的窗口和进程**，换窗口/重启客户端后自动失效；
  · 发送前还会**只读复核**当前 UI 档位（能读的端），状态漂了就拒发。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import exit_codes as ec

ROOT = Path(__file__).resolve().parents[2]
TIER_DIR = ROOT / "runs" / "tier"
TIERS_FILE = ROOT / "docs" / "tiers.json"

SCHEMA_VERSION = 1
TTL_SECONDS = 120.0        # 回执有效期：choose 与 use 必须紧邻执行
FUTURE_SKEW = 2.0          # 允许 2 秒时钟抖动；超过就算"时间戳在未来"
LOCK_TIMEOUT = 5.0

PURPOSES = ("test", "review", "accept", "accept_lite")

# CARD-09f 第 6 条：高档（review/accept）会审/验收等待回复的下限。
# 2026-11-08（B12）：900 → 1500。依据是实测——workbuddy 有 7 次在 ~902s 超时，
# 而产出物在 1070–1360s 才落盘（答案就在页面上，是预算不够）。
# 取实测上界 ×1.10 ≈ 1496，圆到 1500。**不是拍脑袋，也不是无限等。**
REVIEW_TIMEOUT = 1500.0


def floor_timeout(purpose: str, timeout: float) -> float:
    """高档（review/accept）等待下限 1500s；其余用途原样返回。"""
    if str(purpose) in ("review", "accept"):
        return max(float(timeout), REVIEW_TIMEOUT)
    return float(timeout)

# 用途 -> 验收结论上的写法（CARD-06c 第 6 条）。
# `accept_lite` 只给 opencode 的 GLM-5.3-Flash-Free 用：链路连通性可以验，
# 但它不是旗舰档，所以结论必须写成 PASS(GLM-lite)，不能和 accept 混成一个 PASS。
PURPOSE_LABELS = {"test": "TEST", "review": "REVIEW",
                  "accept": "PASS", "accept_lite": "PASS(GLM-lite)"}

_SAFE = re.compile(r"^[a-z0-9_]+$")

# 单调时钟基准：本进程启动时的读数。写进回执，便于跨进程判断"这张回执是这一轮的吗"。
_BOOT_MONOTONIC = time.monotonic()


# ---------------------------------------------------------------- 异常
class TierUnconfirmed(RuntimeError):
    """回执不成立。CLI 层转成 stage=tier_unconfirmed / 退出码 5。"""

    stage = "tier_unconfirmed"
    code = ec.TIER_UNCONFIRMED

    def __init__(self, reason: str, hint: str = "", detail: dict | None = None):
        self.reason = reason
        self.hint = hint
        self.detail = dict(detail or {})
        super().__init__(f"{reason}: {hint}" if hint else reason)

    def as_result(self) -> dict:
        out = {"ok": False, "stage": self.stage, "reason": self.reason,
               "code": self.code}
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
            # 失败留证（CARD-06b 第 9 条）要的字段提到顶层：模板得分、缩放。
            # 不复制一份新的真相，只是把 detail 里已有的东西露出来。
            if self.detail.get("template"):
                out["template"] = self.detail["template"]
            if self.detail.get("templates"):
                out["templates"] = self.detail["templates"]
            if "zoom_percent" in self.detail:
                out["zoom_percent"] = self.detail["zoom_percent"]
        return out


# ---------------------------------------------------------------- 路径
def _root(root=None) -> Path:
    return Path(root) if root else ROOT


def receipt_path(endpoint: str, *, root=None) -> Path:
    name = str(endpoint or "").strip().lower()
    if not _SAFE.match(name):
        raise ValueError(f"端名不合法：{endpoint!r}（只允许小写字母/数字/下划线）")
    return _root(root) / "runs" / "tier" / f"{name}.json"


def lock_path(endpoint: str, *, root=None) -> Path:
    return receipt_path(endpoint, root=root).with_suffix(".lock")


# ---------------------------------------------------------------- 时间
def purpose_label(purpose: str) -> str:
    """验收结论该写成什么（`accept_lite` -> `PASS(GLM-lite)`）。

    只影响**怎么写结论**，不参与判定：判定看 `allowed_tiers`。
    """
    return PURPOSE_LABELS.get(str(purpose), str(purpose).upper())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:                 # 不带时区的一律当 UTC，不按本地时区猜
        value = value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------- 进程/窗口指纹
def _kernel32():
    import ctypes
    return ctypes.windll.kernel32


def pid_of(hwnd) -> int | None:
    """窗口属于哪个进程。读不到返回 None（不猜）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:                        # noqa: BLE001 - 纯诊断信息，不许带崩主流程
        return None


def process_start_time(pid) -> int | None:
    """进程启动时间（FILETIME，100ns 单位）。

    为什么要它：客户端崩溃重启后句柄可能被复用，光比 hwnd 会放行一张旧回执。
    pid + 启动时间一起比，重启就一定会对不上。非 Windows / 读不到返回 None。
    """
    if not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        k32 = _kernel32()
        handle = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return (int(created.high) << 32) | int(created.low)
        finally:
            k32.CloseHandle(handle)
    except Exception:                        # noqa: BLE001
        return None


def fingerprint(hwnd) -> dict:
    """{hwnd, pid, process_start_time}。拿不到的一律给 None，不编。"""
    if not hwnd:
        return {"hwnd": None, "pid": None, "process_start_time": None}
    pid = pid_of(hwnd)
    return {"hwnd": int(hwnd), "pid": pid, "process_start_time": process_start_time(pid)}


# ---------------------------------------------------------------- 原子写
def _atomic_write(path: Path, payload: dict) -> Path:
    """临时文件 + os.replace：写一半断电也不会留下半张回执。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)                    # 同卷原子替换；失败会抛，由调用方决定
    return path


# ---------------------------------------------------------------- 文件锁
_PROC_LOCK = threading.RLock()


def _lock_fd(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:                                    # pragma: no cover - 本机是 Windows
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:                                # pragma: no cover
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:                        # noqa: BLE001 - 解锁失败不该带崩
        pass


@contextmanager
def file_lock(path: Path, *, timeout: float = LOCK_TIMEOUT, poll: float = 0.02):
    """排它文件锁。同进程内再加一把线程锁（Windows 的字节锁是挂在句柄上的，
    同进程两个句柄会互相冲突，但语义上更容易理解的是"进程内也串行"）。

    **打开锁文件本身也要重试**：几个进程同时第一次创建同一个 `.lock` 时，
    Windows 会偶发 `PermissionError: [Errno 13]`（实测 4 进程并发时约 1/3 概率）。
    那不是业务错误，只是"别人正在创建这个文件"，等一下就过去了。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROC_LOCK:
        deadline = time.monotonic() + max(0.0, timeout)
        handle = None
        while True:
            try:
                handle = open(path, "a+b")
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"打不开锁文件（可能被别的进程占着）：{path}（{exc}）")
                time.sleep(poll)
        try:
            while True:
                try:
                    _lock_fd(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等锁超时：{path}")
                    time.sleep(poll)
            yield
        finally:
            _unlock_fd(handle)
            handle.close()


# ---------------------------------------------------------------- 写 / 读
def write_receipt(endpoint: str, *, target, readback, dimension: str = "model",
                  hwnd=None, tab=None, raw=None, pid=None,
                  start_time=None, path=None, root=None, now: str | None = None,
                  writer: str | None = None, dimensions: dict | None = None) -> dict:
    """原子写一张回执，返回写下去的内容。

    `readback` 为空表示"点了但没读回来"——这种回执**照样写**，
    但 use 会因为 `readback_missing` 拒发。写下来是为了让巡检看得见：
    "choose 说 done 但读不回档位"本身就是要查的问题。

    `writer` 记的是"这个档位是在哪个模式下选的"（CARD-06c 第 2 条）：
    ChatGPT 聊天/工作两个模式的模型表和强度表完全不同，use 必须按它去取白名单。
    """
    where = Path(path) if path else receipt_path(endpoint, root=root)
    fact = fingerprint(hwnd) if hwnd else {"hwnd": None, "pid": None,
                                          "process_start_time": None}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "endpoint": str(endpoint).lower(),
        "dimension": str(dimension or "model"),
        "target": target,
        "readback": readback,
        "writer": (str(writer).lower() if writer else None),
        "hwnd": fact["hwnd"],
        "pid": pid if pid is not None else fact["pid"],
        "process_start_time": start_time if start_time is not None
        else fact["process_start_time"],
        "tab": tab,
        "created_at": now or utc_now(),
        "created_monotonic": round(time.monotonic(), 6),
        "monotonic_origin": round(_BOOT_MONOTONIC, 6),
        "raw": raw if isinstance(raw, str) else (
            None if raw is None else json.dumps(raw, ensure_ascii=False, sort_keys=True)),
    }
    if dimensions is not None:
        # One write and one identity/timestamp, never merge separate old rounds.
        payload["composite_version"] = 1
        payload["dimensions"] = dimensions
    _atomic_write(where, payload)
    return payload


def read_receipt(endpoint: str, *, path=None, root=None) -> dict | None:
    """读回执。文件不在、JSON 坏了都返回 None（坏文件也算"缺失"，原因在 check 里说）。"""
    where = Path(path) if path else receipt_path(endpoint, root=root)
    try:
        with open(where, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_choice(endpoint: str, *, target, readback, dimension: str = "model",
                  hwnd=None, tab=None, raw=None, path=None, root=None,
                  writer: str | None = None, dimensions: dict | None = None) -> dict:
    """给 `*_choose` 用：读回确认后写回执。**任何异常都不许带崩 choose。**

    返回 {"written": bool, "path": str|None, "reason": str}。
    读回为空或与目标不符时也写（use 会拒绝），只是把原因记在返回里。
    """
    out = {"written": False, "path": None, "reason": ""}
    try:
        written = write_receipt(endpoint, target=target, readback=readback,
                               dimension=dimension, hwnd=hwnd, tab=tab, raw=raw,
                               path=path, root=root, writer=writer, dimensions=dimensions)
        out.update({"written": True, "path": str(Path(path) if path
                                                 else receipt_path(endpoint, root=root)),
                    "reason": "ok" if readback == target else "readback_mismatch"})
        out["receipt"] = written
    except Exception as exc:                 # noqa: BLE001 - 写不了回执不算 choose 失败
        out["reason"] = f"error: {type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------- 档位表
def load_tiers(path=None) -> dict:
    """读档位表本体。**格式错误抛 ValueError / 读不到抛 OSError**。

    这是"纯读取"层：给 server、autopush 这类自己会把异常翻译成业务错误的调用方用。
    发送门禁那一路要走 `_load_tiers_for_gate`——它把同样的错误统一成
    `TierUnconfirmed`，否则 `endpoints` 写成 list/string 时会在 `.get` 上抛
    AttributeError，从入口漏出去（CARD-06a 返工：格式错误必须一律 tier_unconfirmed/5）。
    """
    where = Path(path) if path else TIERS_FILE
    try:
        with open(where, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:                 # json.JSONDecodeError 也是 ValueError
        raise ValueError(f"档位表不是合法 JSON：{where}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"档位表不是对象：{where}（{type(data).__name__}）")
    return data


def _load_tiers_for_gate(path=None) -> dict:
    """门禁路径专用：任何"读不出/读不懂"都换成 TierUnconfirmed（退出码 5）。"""
    try:
        return load_tiers(path)
    except OSError as exc:
        raise TierUnconfirmed("table_unreadable",
                              f"读不到档位表 {path or TIERS_FILE}：{exc}") from exc
    except ValueError as exc:
        raise TierUnconfirmed("table_malformed", str(exc)) from exc


def endpoint_entry(endpoint: str, *, tiers: dict | None = None, path=None) -> dict:
    """一个端的整段登记（`dimensions` 和可选的 `writers` 都在里面）。"""
    data = tiers if tiers is not None else _load_tiers_for_gate(path)
    if not isinstance(data, dict):
        raise TierUnconfirmed("table_malformed",
                              f"档位表根节点必须是对象，实际是 {type(data).__name__}")
    entries = data.get("endpoints")
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        # 2026-11-06 返工反例：`{"endpoints": ["workbuddy"]}` / `"workbuddy"`
        # 以前直接 .get → AttributeError 从入口漏出，退出码既不是 5 也没留证。
        raise TierUnconfirmed(
            "table_malformed",
            f"档位表的 endpoints 必须是对象（端名 → 登记），实际是 {type(entries).__name__}")
    entry = entries.get(str(endpoint).lower())
    if not isinstance(entry, dict):
        raise TierUnconfirmed("endpoint_not_in_table",
                              f"docs/tiers.json 里没有 {endpoint} 的档位表")
    return entry


def endpoint_table(endpoint: str, *, tiers: dict | None = None, path=None) -> dict:
    dims = endpoint_entry(endpoint, tiers=tiers, path=path).get("dimensions") or {}
    if not isinstance(dims, dict):
        raise TierUnconfirmed("table_malformed", f"{endpoint} 的 dimensions 不是对象")
    return dims


def _dimension_entry(endpoint: str, dimension: str, *, writer: str | None,
                     tiers: dict | None = None, path=None) -> dict:
    """取「这个端、这个模式、这个维度」的白名单条目。

    `docs/tiers.json` 的结构（CARD-06c 第 2 条）：

        endpoints.<端>.writer_modes                ← 这个端有哪几个模式（可选，声明了才校验）
        endpoints.<端>.dimensions.<维度>            ← 默认表（默认模式/不分模式的端）
        endpoints.<端>.dimensions.<维度>.writers.<模式>   ← 该模式的**覆盖**表（可选）

    例如 ChatGPT 的 `model` 维度默认是工作模式的 8 个档；`writers.chat` 另外登记
    聊天模式的两个档。规则：

      · 没声明 `writer_modes`，或回执里没有 writer → 用默认表（老回执向后兼容）；
      · 声明了 `writer_modes` 但回执里的模式不在里面 → `writer_not_in_table`（拒发，
        防的是"回执写了个不存在的模式"这种配置/拼写问题）；
      · 声明了、但该模式没有覆盖表 → 默认表就是它的表（work 就是这么走的，
        不用把 8 个档抄两遍）。
    """
    entry = endpoint_entry(endpoint, tiers=tiers, path=path)
    modes = entry.get("writer_modes")
    scoped_writer = str(writer).lower() if writer else None
    if scoped_writer and isinstance(modes, list) and modes:
        known = [str(m).lower() for m in modes]
        if scoped_writer not in known:
            raise TierUnconfirmed(
                "writer_not_in_table",
                f"{endpoint} 只登记了模式 {known}，回执里是 {scoped_writer!r}")
    dims = entry.get("dimensions") or {}
    if not isinstance(dims, dict):
        raise TierUnconfirmed("table_malformed", f"{endpoint} 的 dimensions 不是对象")
    block = dims.get(str(dimension))
    if not isinstance(block, dict):
        raise TierUnconfirmed("dimension_not_in_table",
                              f"{endpoint} 没有 {dimension} 维度的档位表（先补 docs/tiers.json）")
    if not scoped_writer:
        return block
    blocks = block.get("writers")
    if isinstance(blocks, dict):
        scoped = blocks.get(scoped_writer)
        if isinstance(scoped, dict):
            return scoped
    return block


def allowed_tiers(endpoint: str, purpose: str, dimension: str, *,
                  tiers: dict | None = None, path=None,
                  writer: str | None = None) -> list[str]:
    """这个端、这个维度、这个用途允许的读回值。空列表 = 未配置；check/gate必须拒发。"""
    if purpose not in PURPOSES:
        raise TierUnconfirmed("bad_purpose", f"purpose 只能是 {list(PURPOSES)}")
    entry = _dimension_entry(endpoint, dimension, writer=writer,
                             tiers=tiers, path=path)
    values = entry.get(purpose)
    if values is None:
        where = f"{endpoint}/{writer}/{dimension}" if writer else f"{endpoint}/{dimension}"
        raise TierUnconfirmed("purpose_not_in_table", f"{where} 没登记 {purpose} 档")
    if not isinstance(values, list) or any(
            not isinstance(v, str) or not v.strip() for v in values):
        raise TierUnconfirmed("table_malformed", f"{endpoint}/{dimension}/{purpose} 必须是非空档名列表")
    return list(values)


# ---------------------------------------------------------------- 校验
def check(endpoint: str, purpose: str, *, path=None, root=None, now=None,
          tiers: dict | None = None, live: dict | None = None) -> dict:
    """只校验、不消费。返回 {"ok", "reasons", "receipt", "expected", "tier_check"}。

    `live` 给当前窗口指纹（默认按回执里的 hwnd 现取）；测试可注入。
    """
    out = {"ok": False, "reasons": [], "receipt": None, "expected": [],
           "tier_check": "checked", "endpoint": endpoint, "purpose": purpose}
    where = Path(path) if path else receipt_path(endpoint, root=root)
    receipt = read_receipt(endpoint, path=where)
    if receipt is None:
        out["reasons"].append("receipt_missing")
        return out
    out["receipt"] = receipt

    if receipt.get("schema_version") != SCHEMA_VERSION:
        out["reasons"].append("schema_mismatch")
        return out
    if str(receipt.get("endpoint") or "").lower() != str(endpoint).lower():
        out["reasons"].append("endpoint_mismatch")
        return out
    if receipt.get("consumed"):
        out["reasons"].append("consumed")
        return out

    # 时间：既要没过期，也不能"来自未来"
    created = parse_utc(receipt.get("created_at"))
    moment = parse_utc(now) if now else datetime.now(timezone.utc)
    if created is None or moment is None:
        out["reasons"].append("timestamp_unreadable")
        return out
    age = (moment - created).total_seconds()
    out["age"] = round(age, 3)
    if age < -FUTURE_SKEW:
        out["reasons"].append("future_timestamp")
        return out
    if age > TTL_SECONDS:
        out["reasons"].append("expired")
        return out

    # 档位：目标=读回，且读回=用途要求的那一档
    target, readback = receipt.get("target"), receipt.get("readback")
    if not readback:
        out["reasons"].append("readback_missing")
        return out
    if target != readback:
        out["reasons"].append("readback_mismatch")
        return out
    dimension = str(receipt.get("dimension") or "model")
    writer = receipt.get("writer")
    if writer:
        out["writer"] = writer
    try:
        # CARD-06c 第 2 条：带上回执里的 writer 去取白名单。
        # ChatGPT 聊天/工作是两张不同的表，不按 writer 取就会拿工作表去比聊天档。
        expected = allowed_tiers(endpoint, purpose, dimension, tiers=tiers,
                                 writer=writer)
    except TierUnconfirmed as exc:
        out["reasons"].append(exc.reason)
        out["expected"] = []
        out["hint"] = exc.hint
        return out
    out["expected"] = expected
    if not expected:
        out["tier_check"] = "table_unconfigured"
        out["reasons"].append("table_unconfigured")
        out["hint"] = (f"{endpoint}/{dimension}/{purpose} 未配置用途档位，拒绝发送；"
                       "先核实最低/最高档并配置 docs/tiers.json，再重新 choose")
        return out
    elif str(readback) not in expected:
        out["reasons"].append("purpose_mismatch")
        out["hint"] = (f"--purpose {purpose} 要求 {dimension} 是 {expected}，"
                       f"回执里读回的是 {readback}；先按用途重新跑一次 choose")
        return out

    # Endpoints declaring required dimensions cannot use a legacy single-axis
    # receipt. Check every dimension against the same purpose and writer table.
    try:
        entry = endpoint_entry(endpoint, tiers=tiers)
    except TierUnconfirmed as exc:
        out["reasons"].append(exc.reason)
        out["hint"] = exc.hint
        return out
    required = entry.get("required_dimensions") or []
    if required:
        blocks = receipt.get("dimensions")
        if receipt.get("composite_version") != 1 or not isinstance(blocks, dict):
            out["reasons"].append("composite_missing")
            return out
        if entry.get("require_writer") and not writer:
            out["reasons"].append("writer_missing")
            return out
        out["expected_dimensions"] = {}
        for dim in required:
            block = blocks.get(dim)
            if not isinstance(block, dict) or not block.get("readback"):
                out["reasons"].append("composite_missing")
                out["dimension"] = dim
                return out
            if block.get("target") != block["readback"]:
                out["reasons"].append("readback_mismatch")
                out["dimension"] = dim
                return out
            try:
                allowed = allowed_tiers(endpoint, purpose, dim, tiers=tiers, writer=writer)
            except TierUnconfirmed as exc:
                out["reasons"].append(exc.reason)
                out["hint"] = exc.hint
                return out
            out["expected_dimensions"][dim] = allowed
            if not allowed or block["readback"] not in allowed:
                out["reasons"].append("purpose_mismatch" if allowed else "table_unconfigured")
                out["dimension"] = dim
                out["hint"] = f"{dim} 必须是 {allowed}，联合回执读回 {block['readback']!r}"
                return out
        primary = blocks.get(dimension) or {}
        if primary.get("target") != target or primary.get("readback") != readback:
            out["reasons"].append("readback_mismatch")
            return out

    # 窗口：hwnd + pid + 进程启动时间，三者都要对得上
    live = live if live is not None else fingerprint(receipt.get("hwnd"))
    out["live"] = live
    for key in ("hwnd", "pid", "process_start_time"):
        old, cur = receipt.get(key), (live or {}).get(key)
        if old is None and cur is None:
            continue
        if old != cur:
            # CARD-06b 第 4 条：这是"回执身份不符"，单列一个 reason，
            # 和"档位漂了"分开——排查时一个查窗口、一个查档位。
            out["reasons"].append("receipt_identity")
            out["identity"] = {"field": key, "receipt": old, "live": cur}
            out["hint"] = (f"回执绑定的 {key}={old}，现在是 {cur}；"
                           "窗口换了或客户端重启过，回执作废（reason=receipt_identity）")
            return out

    out["ok"] = True
    return out


# ---------------------------------------------------------------- 消费
def consume(endpoint: str, *, path=None, root=None, timeout: float = LOCK_TIMEOUT) -> dict:
    """在文件锁里把 consumed 从 false 翻成 true。返回 {"ok", "reason"}。

    发送动作**之前**调用。两个 use 同时抢同一张回执时，只有一个 ok。
    """
    where = Path(path) if path else receipt_path(endpoint, root=root)
    lock = where.with_suffix(".lock")
    with file_lock(lock, timeout=timeout):
        receipt = read_receipt(endpoint, path=where)
        if receipt is None:
            return {"ok": False, "reason": "receipt_missing"}
        if receipt.get("consumed"):
            return {"ok": False, "reason": "consumed"}
        updated = dict(receipt)
        updated["consumed"] = True
        updated["consumed_at"] = utc_now()
        _atomic_write(where, updated)
    return {"ok": True, "reason": "consumed"}


# ---------------------------------------------------------------- 一支到底
def _readback_of(got) -> tuple:
    """把 `reread()` 的返回值拆成 (值, 诊断)。兼容旧的"只返回字符串"写法。

    CARD-06b 起 web 端返回 dict：{"value","source","template","zoom_percent",...}，
    这样"读不到"能带上模板得分和缩放，失败留证才有东西可写。
    """
    if got is None:
        return None, {"source": "none", "reason": "none"}
    if isinstance(got, dict):
        detail = {key: got.get(key) for key in
                  ("source", "reason", "template", "templates", "zoom_percent", "raw")}
        return got.get("value"), {k: v for k, v in detail.items() if v is not None}
    return str(got), {"source": "caller"}


def _composite_reread(receipt, reread):
    """One read-only snapshot must confirm writer AND every saved dimension."""
    try:
        got = reread("composite") if callable(reread) else None
    except Exception as exc:
        got = {"reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(got, dict):
        got = {}
    values = got.get("dimensions") or {}
    if not isinstance(values, dict):
        values = {}
    detail = {"source": got.get("source"), "raw": got.get("raw"),
              "values": values, "writer": got.get("writer")}
    wanted = {key: value.get("readback") for key, value in receipt["dimensions"].items()
              if isinstance(value, dict)}
    failure = None
    if not got.get("writer") or any(not values.get(key) for key in wanted):
        failure = TierUnconfirmed("tier_unreadable", "联合档位有维度读不到，拒绝发送", detail)
    elif got["writer"] != receipt.get("writer") or any(values[key] != value for key, value in wanted.items()):
        failure = TierUnconfirmed("tier_drift", "writer/model/effort任一漂移，拒绝发送", detail)
    return {"checked": failure is None, "value": values, "source": got.get("source"),
            "detail": detail}, failure


def gate(endpoint: str, purpose: str, *, reread=None, require_readback: bool = True,
         path=None, root=None, now=None, tiers: dict | None = None, live: dict | None = None,
         hwnd=None, do_consume: bool = True) -> dict:
    """use 的发送前闸门：校验 → 只读复核（防漂移）→ 原子消费。

    `reread(dimension) -> str|dict|None`：只读地再读一遍当前 UI 档位。
    **CARD-06b r2 起改成 fail-closed**（三家会审一致选 A）：

      · 返回 None / dict 里 value=None（模板低于阈值、读回为空、多处命中）
        → `reason=tier_unreadable`，退出码 5，**不放行、也不"警告放行"**；
      · 读到但与回执不符 → `reason=tier_drift`，退出码 5，提示重跑 choose，
        use 自己不点菜单、不自动重选；
      · 只有读到且与回执一致才算确认。

    `require_readback=False` 只给"本端确实没有只读通道"的端用（桌面端，
    适配在 CARD-06c）；结果里会带 `tier_readback="unsupported"`，
    不是静默放行——调用方和日志都看得见。

    `hwnd` 给了就用**当前正在操作的这个窗口**的指纹去比回执（这才是真的防漂移：
    换窗口了就该拒）；不给才回落到回执里自己那个 hwnd。

    返回 {"ok": True, ...} 或 `TierUnconfirmed.as_result()` 的形状。
    """
    if live is None and hwnd:
        live = fingerprint(hwnd)
    verdict = check(endpoint, purpose, path=path, root=root, now=now,
                    tiers=tiers, live=live)
    if not verdict["ok"]:
        reason = verdict["reasons"][0]
        # A failed sending attempt invalidates its receipt too. A read-only
        # preflight explicitly uses do_consume=False and leaves it untouched.
        if do_consume and verdict.get("receipt"):
            consume(endpoint, path=path, root=root)
        raise TierUnconfirmed(reason, _hint_for(endpoint, purpose, reason, verdict),
                              {k: v for k, v in verdict.items()
                               if k in ("age", "expected", "live", "tier_check",
                                        "identity")})

    receipt = verdict["receipt"]
    dimension = str(receipt.get("dimension") or "model")
    drift: dict = {"checked": False, "value": None, "source": None}
    failure: TierUnconfirmed | None = None
    if receipt.get("composite_version") == 1:
        drift, failure = _composite_reread(receipt, reread)
    elif callable(reread):
        try:
            got = reread(dimension)
        except Exception as exc:             # noqa: BLE001 - 读失败一律按"没读到"处理
            got = {"value": None, "source": "none",
                   "reason": f"{type(exc).__name__}: {exc}"}
        value, detail = _readback_of(got)
        drift.update({"checked": value is not None, "value": value,
                      "source": detail.get("source")})
        if detail:
            drift["detail"] = detail
        if value is None:
            if require_readback:
                template = detail.get("template")
                hint = (f"发送前读不回档位（{detail.get('reason') or 'readback_missing'}）："
                        f"模板 {((template or {}).get('name')) or '—'} "
                        f"最高 {((template or {}).get('score')) if template else '—'}"
                        f"（阈值 {((template or {}).get('threshold')) or '—'}），"
                        f"页面缩放 {detail.get('zoom_percent') or '未知'}%。"
                        "按 CARD-06b：读不到一律拒发，请人工确认页面/重截模板后重新 choose")
                failure = TierUnconfirmed(
                    "tier_unreadable", hint,
                    {"dimension": dimension, "drift": drift, **detail})
        elif str(value) != str(receipt.get("readback")):
            failure = TierUnconfirmed(
                "tier_drift",
                f"UI 档位已经变成 {value!r}，回执里是 {receipt.get('readback')!r}；"
                f"use 不切档、不自动重选，请重新跑 {endpoint}_choose",
                {"dimension": dimension, "drift": drift, **detail})
    elif require_readback:
        failure = TierUnconfirmed(
            "tier_unreadable",
            f"{endpoint} 没有可用的只读档位通道，但本卡要求发送前必须读回；"
            "先补只读复核（CARD-06c），不要放行",
            {"dimension": dimension, "drift": drift})

    # 失败也消费（06a 的既有语义：失败/部分失败都作废，必须重新 choose）。
    # 先消费再抛，避免同一张回执被反复拿来试。
    ok = False
    if do_consume:
        used = consume(endpoint, path=path, root=root)
        ok = bool(used["ok"])
        if not ok and failure is None:       # 并发：别人先消费了
            raise TierUnconfirmed("consumed", f"回执已被另一个进程用掉（{used['reason']}）")
    if failure is not None:
        raise failure
    return {"ok": True, "endpoint": endpoint, "purpose": purpose,
            "purpose_label": purpose_label(purpose),
            "tier_check": verdict["tier_check"], "expected": verdict["expected"],
            "receipt": receipt, "drift": drift, "consumed": bool(do_consume),
            "tier_readback": "confirmed" if drift["checked"] else "unsupported"}


def _hint_for(endpoint: str, purpose: str, reason: str, verdict: dict) -> str:
    choose = f"{endpoint}_choose"
    base = {
        "receipt_missing": f"没有档位回执：先跑一次 {choose} 选档，再紧跟着发（120 秒内）",
        "schema_mismatch": f"回执版本不对（不是 v{SCHEMA_VERSION}）：重跑 {choose}",
        "endpoint_mismatch": f"回执不是这个端的：重跑 {choose}",
        "consumed": f"回执已经用过：每张回执只能发一次，重跑 {choose}",
        "timestamp_unreadable": f"回执里没有可读的 UTC 时间戳：重跑 {choose}",
        "future_timestamp": f"回执时间戳在未来（时钟被改过？）：重跑 {choose}",
        "expired": f"回执超过 {TTL_SECONDS:.0f} 秒：choose 与 use 要连着头跑，重跑 {choose}",
        "readback_missing": f"choose 没能读回档位（点完没确认）：换一个可读回的档位重跑 {choose}",
        "readback_mismatch": f"回执自相矛盾（目标≠读回）：重跑 {choose}",
        "purpose_mismatch": verdict.get("hint") or f"档位与 --purpose {purpose} 不符：重跑 {choose}",
        # CARD-06b 第 4 条：改名并单列，排查时"查窗口"和"查档位"分开
        "receipt_identity": verdict.get("hint") or f"回执绑的窗口/进程已经变了：重跑 {choose}",
        "tier_unreadable": "发送前读不回档位：不放行；人工确认页面/重截模板后重跑 choose",
        "tier_drift": f"档位已经漂了：use 不切档，重跑 {choose}",
        "endpoint_not_in_table": "docs/tiers.json 缺这个端",
        "dimension_not_in_table": "docs/tiers.json 缺这个维度",
        "purpose_not_in_table": "docs/tiers.json 缺这个用途",
        "table_unreadable": "读不到 docs/tiers.json，拒绝发送；先修好档位表再 choose",
        "writer_not_in_table": verdict.get("hint") or "docs/tiers.json 缺这个模式的档位表",
        "table_unconfigured": verdict.get("hint") or "用途档位未配置，拒绝发送",
        "table_malformed": verdict.get("hint") or "用途档位表格式错误，拒绝发送",
        "composite_missing": "需要同轮writer/model/effort联合回执，旧单维回执不可发送；重新choose",
        "writer_missing": "联合回执缺少writer，拒绝发送；重新choose",
        "bad_purpose": f"--purpose 只能是 {'/'.join(PURPOSES)}",
    }
    return base.get(reason, f"档位没确认：先跑 {choose}")


def gate_or_exit(endpoint: str, purpose: str, *, reread=None, out=print,
                 **kwargs) -> int | None:
    """给各 use 的入口用：过了返回 None，没过打印 JSON 并返回退出码 5。"""
    try:
        result = gate(endpoint, purpose, reread=reread, **kwargs)
    except TierUnconfirmed as exc:
        out(json.dumps(exc.as_result(), ensure_ascii=False))
        return exc.code
    out(json.dumps({"ok": True, "stage": "tier_confirmed",
                    "tier_check": result["tier_check"],
                    "tier_readback": result.get("tier_readback"),
                    "purpose": result.get("purpose"),
                    "purpose_label": result.get("purpose_label"),
                    "dimension": result["receipt"].get("dimension"),
                    "writer": result["receipt"].get("writer"),
                    "readback": result["receipt"].get("readback"),
                    "drift": result["drift"]}, ensure_ascii=False))
    return None
