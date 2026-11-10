"""UIA 文档丢失后的「重启一次」通道（CARD-09e 第 2 条）。

## 为什么要重启

`qi/ui.py` 的 `candidates()` 已经会：重建 UIA 客户端 → 提前台 → NotifyWinEvent →
FindAll → 挪一下鼠标，最多 3 轮。仍然读到 0 个文档时抛 `UiDocsLost`（退出码 6）。
2026-11-01 巡检的现场就是这一态：**WorkBuddy 空闲久了，只有重启进程才恢复**
（那时写的是"重启是用户的事"）。卡面现在要求：脚本自己试一次。

## 顺序（卡面原文：正常关闭 → 等待 → 启动 → 等 UIA 文档就绪）

1. 记下主窗口和它的 exe 全路径 —— 关掉以后就问不出来了；
2. `WM_CLOSE` 请求正常关闭（**不** TerminateProcess，应用自己收尾）；
3. 轮询到窗口消失；
4. 按第 1 步记下的路径重新拉起；
5. 等窗口出现，再等这个窗口里**至少有 1 个 DocumentControl**；
6. 全过程的每一步都留在返回值里，调用方写 runlog。

## 边界

· 只重启**一次**：调用方拿到结果就重试动作一次，再失败就退 6，不循环重启；
· 关不掉（比如弹了"确定要退出吗"）就**不硬杀**，如实返回 `close_timeout`；
· 找不到 exe 路径就不启动，返回 `no_exe`；
· 这里只负责"重启 + 等就绪"，不判断该不该重启——那是 workbuddy.py 的事。
"""
from __future__ import annotations

import time

from . import desk, ui


def restart(name: str, *, close_wait: float = 25.0, window_wait: float = 40.0,
            docs_wait: float = 45.0, settle: float = 1.0, verbose: bool = True) -> dict:
    """重启 `name` 这个桌面端并等 UIA 文档就绪。返回过程记录（`ok` 为结论）。"""
    started = time.monotonic()
    steps: list[str] = []
    info: dict = {"ok": False, "app": name, "steps": steps, "hwnd": None,
                  "docs": 0, "exe": "", "error": None}

    hwnd = desk.find_app(name)
    exe = desk.exe_path(hwnd) if hwnd else ""
    if verbose:
        print(f"  重启 {name}：hwnd={hwnd} exe={exe or '(未知)'}", flush=True)

    if hwnd:
        info["hwnd"] = int(hwnd)
        if not exe:
            info["error"] = "no_exe"
            steps.append("no_exe")
            info["elapsed"] = round(time.monotonic() - started, 1)
            return info
        info["exe"] = exe
        steps.append("close_requested")
        if not desk.close_window(hwnd):
            info["error"] = "close_failed"
            steps.append("close_failed")
            info["elapsed"] = round(time.monotonic() - started, 1)
            return info
        if not desk.wait_closed(hwnd, timeout=close_wait):
            # 没关掉就**不启动**：再拉一份会变成两个实例抢同一个 UIA 树
            info["error"] = "close_timeout"
            steps.append("close_timeout")
            info["elapsed"] = round(time.monotonic() - started, 1)
            return info
        steps.append("closed")
    else:
        # 本来就没窗口：能启动就启动（exe 路径只能从 APPS 推出，推不出就失败）
        spec = desk.APPS.get(name) or {}
        if spec.get("launch"):
            steps.append("was_closed")
        else:
            info["error"] = "not_running"
            steps.append("not_running")
            info["elapsed"] = round(time.monotonic() - started, 1)
            return info

    time.sleep(settle)
    via = _launch(name, exe, steps)
    if via is None:
        info["error"] = "launch_failed"
        info["elapsed"] = round(time.monotonic() - started, 1)
        return info
    info["launch_via"] = via

    fresh = desk.wait_window(name, timeout=window_wait)
    if fresh is None:
        # 换一种启动方式再试一次（AppID <-> exe 全路径）。
        # 2026-11-02 真机：热关闭后直接拉 exe 等了 40s 也没窗口，改用 AppID 2s 就起来了。
        steps.append("window_timeout")
        other = _launch(name, exe, steps, prefer_other=True)
        if other is not None:
            info["launch_via"] = other
            fresh = desk.wait_window(name, timeout=window_wait)
    if fresh is None:
        info["error"] = "no_window"
        steps.append("no_window")
        info["elapsed"] = round(time.monotonic() - started, 1)
        return info
    info["hwnd"] = int(fresh)
    steps.append("window")

    docs = _wait_docs(fresh, name, docs_wait)
    info["docs"] = docs
    if docs <= 0:
        info["error"] = "docs_lost"
        steps.append("docs_lost")
        info["elapsed"] = round(time.monotonic() - started, 1)
        return info
    steps.append("docs_ready")
    info["ok"] = True
    info["elapsed"] = round(time.monotonic() - started, 1)
    return info


def _launch(name: str, exe: str, steps: list, *, prefer_other: bool = False) -> str | None:
    """启动应用，返回用了哪种方式（`appid` / `exe`）；都失败返回 None。

    优先 **AppID**（`shell:AppsFolder`，和 ensure_app 同一条路），exe 全路径作兜底：
    2026-11-02 真机实测，同一个 OpenCode 关掉后直接拉 exe 等 40 秒没窗口，
    换 AppID 2 秒就起来了。
    """
    spec = desk.APPS.get(name) or {}
    order = ["exe", "appid"] if prefer_other else ["appid", "exe"]
    for kind in order:
        if kind == "appid" and not spec.get("launch"):
            continue
        if kind == "exe" and not exe:
            continue
        ok = desk.launch(name) if kind == "appid" else desk.launch_exe(exe)
        if ok:
            if "launched" not in steps:
                steps.append("launched")
            return kind
    steps.append("launch_failed")
    return None


def _wait_docs(hwnd: int, name: str, timeout: float, *, poll: float = 1.5) -> int:
    """等 UIA 文档就绪：每轮重新取一次文档列表（不复用元素）。"""
    end = time.time() + timeout
    found = 0
    while time.time() < end:
        try:
            found = len(ui.documents(hwnd))
        except Exception:                   # noqa: BLE001 - 刚起来时读失败是常态
            found = 0
        if found > 0:
            return found
        time.sleep(poll)
    return found
