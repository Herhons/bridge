"""失败自动留证（CARD-06b 第 9 条）。

## 哪些失败要留

`tier_*`（含 tier_unconfirmed / tier_unreadable / tier_drift）、
`zoom_*`、`human_check`、`input_mismatch` —— 这四类失败光看一行 JSON 判不出原因：
当时屏幕上是哪个芯片、模板多少分、页面缩放多少、有没有弹层。

## 留什么、放哪

    evidence\\fail\\<端>_<YYYYmmdd-HHMMSS>.png    当时的目标窗口（拿不到就整屏）
    evidence\\fail\\<端>_<YYYYmmdd-HHMMSS>.json   统一结构（见下）

```json
{
  "schema_version": 1,
  "endpoint": "grok",
  "stage": "tier_unconfirmed",
  "reason": "tier_unreadable",
  "code": 5,
  "at": "2026-11-02T06:12:33+08:00",
  "hwnd": 526630,
  "window": {"rect": [512,161,1380,1024], "zoom_percent": 100},
  "templates": [{"name":"grok_chip_expert","score":0.41,"threshold":0.85,"scale":1.0,"hit":false}],
  "result": {"ok": false, "stage": "...", "hint": "..."}   // 原样（hint 截 500 字）
}
```

## 绝不带崩主流程

截图失败就写 JSON（`"screenshot": null`），JSON 也写不了就只写 runlog 一行。
证据是给人看的，不是业务结果。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import runlog

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence"
FAIL_DIR = EVIDENCE / "fail"

SCHEMA_VERSION = 1
# 同一 (端, stage, reason) 在这么短时间内重复失败，不再重复截大图——
# 只写 JSON 并指向上一次的证据。2026-11-02 实测：外部巡检以 10 秒间隔重试
# chatgpt_use，每次都留一张 4.6MB 的整屏图，半小时就能把 evidence 撑爆。
DEDUPE_SECONDS = 30.0
_LAST: dict[tuple, float] = {}
_LAST_PATH: dict[tuple, str | None] = {}
MAX_FULL_WIDTH = 1600      # 整屏截图缩到这个宽度再存（窗口截图不缩）

# 精确 stage
# CARD-06b 返工：`--new` 之后按回执重选模型失败（model_reset_after_new）也是
# "档位不可信、没发送"的失败，必须和 tier_* 一样留 PNG+JSON，否则统一失败出口
# 只是把 print 换了个地方。登记在这里，别让证据链断在这一条分支上。
# R11：端点拒绝这一轮的三条结构判据。这三条**只能靠看现场判断**（发送键灰着、
# 输入框里还有字），光看一行 JSON 是判不出来的，所以必须留截图。
EXACT = {"human_check", "input_mismatch", "model_reset_after_new",
         "submit_refused", "send_unavailable", "accepted_silent"}
# 前缀 stage
PREFIXES = ("tier_", "zoom_")


def wanted(stage) -> bool:
    text = str(stage or "")
    return text in EXACT or any(text.startswith(pre) for pre in PREFIXES)


def _hint(result: dict) -> str:
    text = str(result.get("hint") or result.get("error") or "")
    return text if len(text) <= 500 else text[:500] + "…"


class _Deduped(Exception):
    """内部控制流：同一种失败刚留过证，跳过截图。"""


def _rect(screen, hwnd):
    """目标窗口矩形。最小化时不要去"恢复"——取证不该改现场。"""
    try:
        from . import window as window_mod

        rect = window_mod.raw_rect(hwnd) if hwnd else None
        if rect and window_mod.usable(rect):
            return list(rect)
    except Exception:                       # noqa: BLE001
        pass
    return None


def _templates(result: dict) -> list:
    """从结果里收集模板得分。允许 `template` 是 dict 或 list[dict]。"""
    raw = result.get("templates") or result.get("template")
    if not raw:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({key: item.get(key) for key in
                    ("name", "score", "threshold", "scale", "hit")})
    return out


def capture(endpoint: str, result: dict, *, screen=None, hwnd=None,
            extra: dict | None = None) -> dict | None:
    """要留证就截一张图 + 写一份 JSON，返回 {"png","json"}；不该留证返回 None。

    绝不抛异常：任何一步失败都降级，最多只留下 JSON。
    """
    if not wanted(result.get("stage")):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{endpoint}_{stamp}"
    png_path = meta_path = None
    rect = None
    key = (endpoint, result.get("stage"), result.get("reason"))
    now = time.monotonic()
    deduped = (_LAST_PATH.get(key)
               if now - _LAST.get(key, 0.0) < DEDUPE_SECONDS else None)
    try:
        FAIL_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:                       # noqa: BLE001
        return None

    # 截图：优先目标窗口（要找的东西都在里面），拿不到就整屏（缩到 1600 宽）
    try:
        if deduped:
            raise _Deduped()          # 同一种失败刚留过证：不重复截大图
        if screen is None:
            from .screen import Screen

            screen = Screen(verbose=False)
        target = hwnd or getattr(screen, "target_hwnd", None)
        rect = _rect(screen, target)
        try:
            img = screen.grab(tuple(rect)) if rect else screen.grab()
        except Exception:                   # noqa: BLE001 - 窗口区域抓不到就整屏
            img = screen.grab()
            rect = None
        if img is not None and getattr(img, "size", 0):
            import cv2

            if rect is None and img.shape[1] > MAX_FULL_WIDTH:
                scale = MAX_FULL_WIDTH / img.shape[1]
                img = cv2.resize(img, (MAX_FULL_WIDTH, max(1, int(img.shape[0] * scale))),
                                 interpolation=cv2.INTER_AREA)
            png_path = FAIL_DIR / f"{stem}.png"
            if not cv2.imwrite(str(png_path), img):
                png_path = None
    except _Deduped:
        png_path = None
    except Exception:                       # noqa: BLE001 - 截图失败只影响证据
        png_path = None

    payload = {
        "schema_version": SCHEMA_VERSION,
        "endpoint": endpoint,
        "stage": result.get("stage"),
        "reason": result.get("reason"),
        "code": result.get("code"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hwnd": int(hwnd) if hwnd else (getattr(screen, "target_hwnd", None) if screen else None),
        "window": {"rect": rect, "zoom_percent": result.get("zoom_percent")},
        "templates": _templates(result),
        "hint": _hint(result),
        "result": {key: value for key, value in result.items() if key != "reply"},
        "extra": dict(extra or {}),
        "screenshot": str(png_path) if png_path else None,
        "deduped": deduped,
    }
    try:
        meta_path = FAIL_DIR / f"{stem}.json"
        meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except Exception:                       # noqa: BLE001
        meta_path = None

    _LAST[key] = now
    _LAST_PATH[key] = str(png_path) if png_path else deduped
    runlog.record_evidence(endpoint, result, png=png_path or deduped, meta=meta_path)
    return {"png": str(png_path) if png_path else deduped,
            "json": str(meta_path) if meta_path else None}





def record(endpoint: str, result: dict, *, screen=None, hwnd=None,
           extra: dict | None = None) -> dict:
    """capture 的"绝不抛"包装：返回 {} 或 {"png","json"}，并写进 result["evidence"]。"""
    try:
        got = capture(endpoint, result, screen=screen, hwnd=hwnd, extra=extra) or {}
    except Exception as exc:                # noqa: BLE001 - 证据不许带崩主流程
        got = {"error": f"{type(exc).__name__}: {exc}"}
    if got:
        result["evidence"] = got
    return got
