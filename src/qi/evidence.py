"""发送证据落盘（CARD-v010-P0P1）。

## 为什么单独成文件

`runs/` 整体被 `.gitignore` 忽略，证据只需要"能对账、能复现"，不需要进版本库。
但路径必须**可重定向**：`tests/offline_guard` 把可写目录限死在 `_selftest/<run>`
与 TEMP，任何往仓库 `runs/` 写的离线测试都会被判 `outside_write` 并以 86 退出。
所以优先级是「显式 root > 环境变量 > 仓库 runs」，测试用前两者。

## 内容契约

只记能对账的东西，**不记正文全文**：正文只留前 120 字（`prompt_head`），
其余靠 `prompt_sha256` 与账本对齐。证据文件名也不含正文。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

#: 与 `BRIDGE_JOB_ID` / `BRIDGE_GRANT_TOKEN` 同一命名族。
ENV_ROOT = "BRIDGE_EVIDENCE_ROOT"
PROMPT_HEAD = 120
_SAFE = re.compile(r"[^a-z0-9_]+")


def _guarded() -> bool:
    """当前进程是否在 `tests/offline_guard` 的护栏下。

    护栏把可写目录限死在 `_selftest/<run>` 与 TEMP，并**记录**任何往仓库 `runs/`
    的写操作（哪怕被调用方 catch 掉），整轮以 86 退出。所以离线测试里不落证据：
    证据是给真实运行对账用的，测试要验证据就显式传 `root=` 或设
    `BRIDGE_EVIDENCE_ROOT`（这两条路径都在可写区内）。
    """
    return bool(getattr(sys, "_bridge_offline_guard_active", None))


def evidence_root(root=None) -> Path | None:
    """证据根目录；`None` 表示"本轮不落证据"。

    显式 `root` 按"仓库根"语义解释（调用方传 `Engine.root` / 测试 tmpdir），
    环境变量按"证据目录本身"解释（部署侧可直接指到别处）。
    """
    if root is not None:
        return Path(root) / "runs" / "evidence"
    env = (os.environ.get(ENV_ROOT) or "").strip()
    if env:
        return Path(env)
    if _guarded():
        return None
    return Path(__file__).resolve().parents[2] / "runs" / "evidence"


def prompt_sha256(prompt) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    return _SAFE.sub("_", str(value or "").strip().lower()) or "unknown"


def evidence_path(endpoint: str, *, stage: str, prompt, root=None,
                  now: float | None = None) -> Path | None:
    """`runs/evidence/<endpoint>/<YYYYmmdd-HHMMSS>-<sha8>-<stage>.json`。"""
    base = evidence_root(root)
    if base is None:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    tail = prompt_sha256(prompt)[:8]
    name = f"{stamp}-{tail}-{_safe_name(stage)}.json"
    return base / _safe_name(endpoint) / name


def write_evidence(endpoint: str, payload: dict, *, stage: str, prompt,
                   root=None) -> str | None:
    """原子写证据。**永不抛**：证据写失败不许改变收发结论。"""
    target = evidence_path(endpoint, stage=stage, prompt=prompt, root=root)
    if target is None:
        return None
    body = {
        "v": 1,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint": str(endpoint),
        "stage": str(stage),
        "prompt_sha256": prompt_sha256(prompt),
        "prompt_chars": len(str(prompt or "")),
        "prompt_head": str(prompt or "")[:PROMPT_HEAD],
    }
    body.update(payload or {})
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return str(target)
    except OSError:
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
