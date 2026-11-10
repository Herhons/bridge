"""用户占用保护：人在用电脑时，脚本不抢。

两条规则：
1. **开跑前**：如果最近 `--idle` 秒内有键鼠输入，就等（最多 `--wait` 秒）到你停手；
   等不到就退出，不硬抢焦点。`--force` 可以跳过这个检查。
2. **运行中**：在"发送/点击"这种会改变页面的动作前再查一次。
   如果你刚动过键鼠，就中止并报 `user_took_over`，把控制权让回去。

为什么必须放在 `bring_to_front` 之前：把窗口提到前台本身就是抢焦点，
被抢的那一下你正在打字就会丢字，所以检查要在一切动作之前。

## CARD-09e 第 7 条：别把自己的输入当成"用户在用电脑"

`GetLastInputInfo` 数的是**整台机器**的最后一次输入，脚本自己用 `SendInput`
打的字、点的鼠标同样算数。于是每次刚跑完 `*_choose` 再跑 `*_use`，
启动闸都会报一句「你正在用电脑（空闲 0.7–1.9s）」再白等两秒——那不是用户在用，
是我们上一秒打的字。

判据：`Screen` 每次合成输入之后把当时的 tick 记进 `runs\\synthetic_input.json`；
如果系统现在的"最后输入 tick"跟记下的完全一致，说明从那以后**没人动过机器**，
那次输入就是我们自己 —— 按"很久没动"处理。用户只要碰一下键鼠，tick 就变了，
判据自动失效，不会把真人操作吞掉。

记录只做提示用：读不到/写不进都不影响 `--idle` 的原语义（宁可多等，不会硬闯）。
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "runs"
MARK_FILE = RUNS / "synthetic_input.json"
OWN_IDLE = 999.0        # 「最后那次输入是我们自己打的」时报的空闲秒数

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _last_input_tick() -> int | None:
    """系统最后一次键鼠输入的 tick（毫秒）；拿不到返回 None。"""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    return int(lii.dwTime)


def idle_seconds() -> float:
    """距离最后一次键鼠输入过了多少秒。拿不到就当作"很久没动"。

    若最后那次输入是脚本自己合成的（见文件开头第 7 条），同样按"很久没动"
    处理：那一刻之前用户空了多久我们不知道，但"用户正在用"这个结论一定不成立。
    """
    tick = _last_input_tick()
    if tick is None:
        return 999.0
    if tick == synthetic_tick():
        return OWN_IDLE
    now = _kernel32.GetTickCount64() & 0xFFFFFFFF
    delta = (now - tick) & 0xFFFFFFFF
    return delta / 1000.0


# ---------------------------------------------------------------- 合成输入标记
SYNTHETIC_TTL = 5.0
_MARK_CACHE: dict = {"path": None, "mtime": None, "tick": None, "wall": None}


def mark_synthetic(kind: str = "input", *, path: str | Path | None = None) -> int | None:
    """记下"最后一次输入是我合成的"：写当时的 tick，返回它（读不到 tick 返回 None）。

    原子替换写入；写失败一律吞掉——这是**提示**，不许影响主流程。
    """
    tick = last_input_tick()
    if tick is None:
        return None
    target = Path(path) if path is not None else MARK_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps({"tick": tick, "wall": time.time(), "kind": kind},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass
    return tick


def synthetic_tick(*, path: str | Path | None = None) -> int | None:
    """Only a recent, well-formed exact tick can suppress idle protection."""
    target = Path(path) if path is not None else MARK_FILE
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    if _MARK_CACHE.get("path") != str(target) or _MARK_CACHE.get("mtime") != stamp:
        tick, wall = None, None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tick, wall = int(data["tick"]), float(data["wall"])
                if not 0 <= tick <= 0xFFFFFFFF:
                    tick = None
        except (OSError, ValueError, TypeError, KeyError):
            tick, wall = None, None
        _MARK_CACHE.update(path=str(target), mtime=stamp, tick=tick, wall=wall)
    wall = _MARK_CACHE.get("wall")
    if wall is None or not -2.0 <= time.time() - wall <= SYNTHETIC_TTL:
        return None
    return _MARK_CACHE.get("tick")


def wait_for_idle(need: float = 2.0, timeout: float = 120.0, verbose: bool = True) -> tuple[bool, float]:
    """等到连续 need 秒没有输入。返回 (是否等到, 当前空闲秒数)。"""
    cur = idle_seconds()
    if cur >= need:
        return True, cur
    if verbose:
        print(f"[等待] 你正在用电脑（空闲 {cur:.1f}s < {need:.0f}s），"
              f"最多等你 {timeout:.0f}s 停手…", flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        cur = idle_seconds()
        if cur >= need:
            if verbose:
                print(f"[继续] 已空闲 {cur:.1f}s，开始执行。", flush=True)
            return True, cur
    return False, idle_seconds()


def user_active(threshold: float = 0.8) -> bool:
    """最近 threshold 秒内有没有输入。运行中用它判断"要不要让位"。"""
    return idle_seconds() < threshold


def last_input_tick() -> int | None:
    """最后一次键鼠输入的系统 tick（毫秒）；拿不到返回 None。"""
    return _last_input_tick()


def user_moved_since(mark: int | None) -> bool:
    """自 mark 以后有没有新的键鼠输入。mark 为空（没记过）就不拦。"""
    if mark is None:
        return False
    now = last_input_tick()
    return now is not None and now != mark


def parse_guard_args(argv: list[str]) -> tuple[float, float, bool, list[str]]:
    """从参数里摘出 --idle / --wait / --force，其余原样返回。

    返回 (need, timeout, force, rest)。
    """
    need, timeout, force = 2.0, 120.0, False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--idle" and i + 1 < len(argv):
            try:
                need = max(0.0, float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        if a == "--wait" and i + 1 < len(argv):
            try:
                timeout = max(0.0, float(argv[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        if a == "--force":
            force = True
            i += 1
            continue
        rest.append(a)
        i += 1
    return need, timeout, force, rest


def guard(need: float, timeout: float, force: bool, verbose: bool = True) -> bool:
    """开跑前的闸。返回 False 表示"你还在用，别打扰"，调用方应直接退出。"""
    if force:
        if verbose:
            print("[跳过] --force：不检查你是否在用电脑。", flush=True)
        return True
    ok, cur = wait_for_idle(need, timeout, verbose)
    if not ok:
        print(f"[放弃] 等了 {timeout:.0f}s 你还在用电脑（空闲 {cur:.1f}s），这次不执行。"
              f" 想强制跑加 --force。", flush=True)
        return False
    return True
