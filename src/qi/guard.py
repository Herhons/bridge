"""用户占用保护：人在用电脑时，脚本不抢。

两条规则：
1. **开跑前**：如果最近 `--idle` 秒内有键鼠输入，就等（最多 `--wait` 秒）到你停手；
   等不到就退出，不硬抢焦点。`--force` 可以跳过这个检查。
2. **运行中**：在"发送/点击"这种会改变页面的动作前再查一次。
   如果你刚动过键鼠，就中止并报 `user_took_over`，把控制权让回去。

为什么必须放在 `bring_to_front` 之前：把窗口提到前台本身就是抢焦点，
被抢的那一下你正在打字就会丢字，所以检查要在一切动作之前。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.GetTickCount64.restype = ctypes.c_ulonglong


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """距离最后一次键鼠输入过了多少秒。拿不到就当作"很久没动"。"""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        return 999.0
    now = _kernel32.GetTickCount64() & 0xFFFFFFFF
    delta = (now - lii.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


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
