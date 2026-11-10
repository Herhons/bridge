"""任务进度心跳：让面板在"等回答"的十几分钟里看得见东西。

**为什么需要**：`elapsed` 只在结束时写一次，`_run_use` 又是阻塞的
`proc.communicate()` —— 子进程的 stdout 要等它退出才被读。于是从点"提交"到结束，
面板只有 `queued → running` 一次变化，中间 20 分钟是一片静默，看起来像卡死。

**做法**：子进程每轮把当前状态原子写进 `runs/progress/<job>.<端>.json`，
服务端用一个看护线程轮询这个文件，`_patch` 进 job 记录，面板照旧读 `/jobs/<id>`。

**纪律**：
- 没有 `BRIDGE_JOB_ID` / `BRIDGE_ENDPOINT` 时**完全 no-op**（命令行直跑、离线测试都不受影响）；
- 任何异常一律吞掉 —— 诊断信息不许把主流程带崩；
- 只写自己那一个文件，只删自己那一个文件。
"""
import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1

#: 面板 3 秒轮一次，这里 1.5 秒写一次就够；stage 变化时立即写。
MIN_INTERVAL = 1.5

JOB_ENV = "BRIDGE_JOB_ID"
ENDPOINT_ENV = "BRIDGE_ENDPOINT"

ROOT = Path(__file__).resolve().parents[2]
_DIR = "progress"

_last_at = 0.0
_last_stage = None


def _safe(name) -> str:
    """文件名只留安全字符：环境变量来自外部，不许拼出路径穿越。"""
    text = str(name or "").strip()
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text)[:80]
    while ".." in safe:                # 不让它拼出 ".." 片段
        safe = safe.replace("..", "__")
    return safe


def dir_for(root=None) -> Path:
    return Path(root or ROOT) / "runs" / _DIR


def path_for(job_id, endpoint, root=None) -> Path:
    return dir_for(root) / (f"{_safe(job_id)}.{_safe(endpoint)}.json")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def beat(stage: str, *, force: bool = False, **fields) -> None:
    """写一帧心跳。拿不到 job/端信息时什么都不做。"""
    global _last_at, _last_stage
    job = os.environ.get(JOB_ENV)
    endpoint = os.environ.get(ENDPOINT_ENV)
    if not job or not endpoint:
        return
    now = time.monotonic()
    if (not force and stage == _last_stage
            and now - _last_at < MIN_INTERVAL):
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": str(job),
        "endpoint": str(endpoint),
        "pid": os.getpid(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stage": str(stage),
    }
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    try:
        _write(path_for(job, endpoint), payload)
        _last_at, _last_stage = now, stage
    except Exception:                      # noqa: BLE001 - 心跳永远不许带崩主流程
        pass


def beat_view(stage: str, view, *, force: bool = False, **fields) -> None:
    """从 View 里取几个字段写一帧。**一律 getattr 容错** —— 假件可能没有这些属性。

    心跳是诊断信息，任何时候都不许让"视图缺个字段"把主流程带崩。
    """
    if view is None:
        return beat(stage, force=force, **fields)
    copies = getattr(view, "copies", None)
    fields.setdefault("busy", bool(getattr(view, "busy", False)))
    if copies is not None:
        # 没有这个字段就别写 0 —— 面板上"候选 0"和"不知道"是两件事
        try:
            fields.setdefault("copies", len(copies))
        except TypeError:
            pass
    beat(stage, force=force, **fields)


def clear() -> None:
    """删掉自己的那一帧（任务结束时）。"""
    job = os.environ.get(JOB_ENV)
    endpoint = os.environ.get(ENDPOINT_ENV)
    if not job or not endpoint:
        return
    try:
        path_for(job, endpoint).unlink()
    except Exception:                      # noqa: BLE001
        pass


def read(job_id, endpoint, root=None):
    """读一帧（服务端用）。读不到 / 坏 JSON 一律 None，不猜。"""
    try:
        raw = path_for(job_id, endpoint, root).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:                      # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def sweep(root=None, *, max_age: float = 86400.0) -> int:
    """清掉上次崩溃留下的孤儿（超过 max_age 的）。返回删除个数。"""
    folder = dir_for(root)
    if not folder.is_dir():
        return 0
    removed = 0
    now = time.time()
    for path in folder.glob("*.json"):
        try:
            if now - path.stat().st_mtime > max_age:
                path.unlink()
                removed += 1
        except Exception:                  # noqa: BLE001
            continue
    return removed
