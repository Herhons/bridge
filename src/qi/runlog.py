"""每次运行追加一行到 runs\\YYYYMMDD.jsonl（2026-10-30 第4项）。

只记 端、结果、耗时、错误，不存提问和回复正文。写失败绝不影响主流程。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "runs"

TIMEOUT_KINDS = ("still_generating", "no_reply")


def with_timeout_kind(result: dict) -> dict:
    """给 stage=timeout 的结果补上 timeout_kind（2026-10-31 任务卡04，只加不改）。

    判据不在这里：`qi.conversation.ask` 已经按"发出去了、且最后还在生成或有未稳定的
    回复"算好 still_generating / no_reply 放进结果。这个函数只给三个单文件脚本
    （codex / dsh / opencode）兜个底——结果里没有这个字段时按 no_reply 记，
    宁可说"没收到回复"，也不冒充"还在生成"。非超时结果原样返回，不改已有字段。
    """
    if result.get("stage") == "timeout" and result.get("timeout_kind") not in TIMEOUT_KINDS:
        return {**result, "timeout_kind": "no_reply"}
    return result


def failure_note(result: dict) -> str | None:
    """Failure cause priority: hint > error > note > stage. Success stays null."""
    if result.get("ok"):
        return None
    for key in ("hint", "error", "note"):
        value = result.get(key)
        if isinstance(value, str):
            if value.strip():
                return value                  # preserve the original wording
        elif value:
            return str(value)
    return f"stage={result.get('stage') or 'unknown'} (no hint/error/note)"


def record(endpoint: str, result: dict, *, root: Path | None = None) -> None:
    try:
        folder = Path(root) if root else RUNS
        folder.mkdir(parents=True, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": endpoint,
                "ok": bool(result.get("ok")), "result": result.get("stage"),
                "timeout_kind": result.get("timeout_kind"),
                "submission": result.get("submission"), "elapsed": result.get("elapsed"),
                "error": failure_note(result)}
        with open(folder / time.strftime("%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                 # noqa: BLE001
        pass


def record_evidence(endpoint: str, result: dict, *, png, meta,
                    root: Path | None = None) -> None:
    """失败留证的一行（CARD-06b 第 9 条）：与 `record()` 同一套形状 + `evidence`。

    `result` 固定写 `fail_evidence`，和时间轴上的收发结果分开；
    截图/JSON 路径放在 `evidence` 里，看日志的人不用去猜文件名。
    写失败一律吞掉——证据写不进去也不许影响主流程。
    """
    try:
        folder = Path(root) if root else RUNS
        folder.mkdir(parents=True, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": endpoint,
                "ok": False, "result": "fail_evidence", "timeout_kind": None,
                "submission": result.get("submission"), "elapsed": result.get("elapsed"),
                "error": failure_note(result),
                "evidence": {"png": str(png) if png else None,
                             "json": str(meta) if meta else None,
                             "stage": result.get("stage"),
                             "reason": result.get("reason")}}
        with open(folder / time.strftime("%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                 # noqa: BLE001
        pass


def record_probe(endpoint: str, *, round: int, rounds: int, docs: int, urls: int,
                 elapsed: float, note: str = "", root: Path | None = None) -> None:
    """UIA 唤醒的一轮（CARD-06a (b)）：每轮都要留痕，失败时才查得出"试了几轮"。

    `result` 固定写 `uia_probe`，与真正的收发结果分开；
    其余字段和 `record()` 保持同一套形状，读日志的脚本不用分叉。
    写失败一律吞掉——诊断日志不许影响主流程。
    """
    try:
        folder = Path(root) if root else RUNS
        folder.mkdir(parents=True, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": endpoint,
                "ok": docs > 0, "result": "uia_probe", "timeout_kind": None,
                "submission": None, "elapsed": round(elapsed, 2),
                "error": None if docs > 0 else (note or "no_document_control"),
                "probe": {"round": round, "rounds": rounds, "docs": docs, "urls": urls}}
        with open(folder / time.strftime("%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                 # noqa: BLE001
        pass


def record_restart(endpoint: str, info: dict, *, root: Path | None = None) -> None:
    """UIA 文档丢失后重启一次的那一行（CARD-09e 第 2 条）。

    `result` 固定写 `uia_restart`，和收发结果分开：一次运行里可能既有
    "重启成功"又有"重启后这一轮失败"，两条都要能查到。
    每一步（close_requested / closed / launched / window / docs_ready）都进 `steps`。
    写失败一律吞掉。
    """
    try:
        folder = Path(root) if root else RUNS
        folder.mkdir(parents=True, exist_ok=True)
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": endpoint,
                "ok": bool(info.get("ok")), "result": "uia_restart", "timeout_kind": None,
                "submission": None, "elapsed": info.get("elapsed"),
                "error": None if info.get("ok") else (info.get("error") or "restart_failed"),
                "restart": {"steps": info.get("steps"), "hwnd": info.get("hwnd"),
                            "docs": info.get("docs"), "exe": info.get("exe")}}
        with open(folder / time.strftime("%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:                                 # noqa: BLE001
        pass
