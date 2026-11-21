# -*- coding: utf-8 -*-
"""Bridge 桌面控制台（v0.2.5）——原生无黑框、极薄的仪表盘 + 按钮壳。

设计边界
--------
本文件**只做壳**：读路线图、读日志、读冷却标记、起停调度器、打一行状态。
调度、脑链、执行器回落、账本、测试门禁全部由 ``scripts/auto_heal_loop.py``
与 ``bridge.py`` 负责，这里**不重复实现任何调度引擎逻辑**。

红线
----
* 只用标准库 ``tkinter``，零外部依赖；``tkinter`` 是**惰性导入**的，
  因此本模块可以被离线单测 ``import`` 而不会创建任何真实窗口。
* 子进程一律带 ``CREATE_NO_WINDOW``：不留 python.exe 黑框。
* 守护进程用 ``CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``
  分离启动，关掉控制台窗口不会带走调度器。
* 默认停靠副屏左侧（``x < 0``），不碰主屏 ``0..2560``。
* 不读写系统剪贴板。

用法：``py -3.10 bridge_gui.pyw``（双击亦可，``.pyw`` 不挂控制台）。
"""
from __future__ import annotations

import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ROADMAP_PATH = "docs/ROADMAP.md"
LOG_PATH = "runs/overnight.log"
COOLDOWN_PATH = "runs/opencode_cooldown.json"
DAEMON_SCRIPT = "scripts/auto_heal_loop.py"
BRIDGE_SCRIPT = "bridge.py"

#: 与 ``scripts/auto_heal_loop.py`` 的 ``PROFILE_CHOICES`` 保持一致。
PROFILE_CHOICES: tuple[str, ...] = ("none", "test", "routine", "council")
PROFILE_DEFAULT = "none"
REFRESH_MS = 2500
#: 动作回执在日志面板里保留的秒数（之后恢复为日志尾随）。
NOTE_HOLD_S = 8.0
LOG_TAIL_LINES = 40
ALL_DONE_MARKER = "ALL_DONE"

#: 严格按行匹配 ``- [x]`` / ``- [ ]``（缩进的子项若无复选框不计入）。
ITEM_RE = re.compile(r"^[ \t]*-[ \t]+\[([ xX])\][ \t]*(.*)$")

#: 默认面板尺寸与副屏停靠点（x < 0 即不碰主屏 ``0..2560``）。
#: M4-5：宽度 480 → 560，长日志行（无空格 URL / JSON / 英文路径）不再右侧截断；
#: x 同步左移到 -580，保证 ``x + width = -20 <= 0``，右边缘仍不进主屏。
PANEL_W = 560
PANEL_H = 700
SECONDARY_X = -580
SECONDARY_Y = 40

BADGE_COLORS = {
    "running": "#3fb950",
    "bug": "#f85149",
    "all_done": "#58a6ff",
    "stopped": "#8b949e",
}
BADGE_LABELS = {
    "running": "运行中",
    "bug": "遇 Bug 停机",
    "all_done": "ALL_DONE",
    "stopped": "已停止",
}
OPENCODE_READY_LABEL = "可用"

#: 惰性导入的 tkinter；导入本模块时保持 ``None``（离线单测据此断言不建窗口）。
tk = None
ttk = None


def _load_tk():
    """Import tkinter on first real use and cache it in module globals."""
    global tk, ttk
    if tk is None:
        import tkinter as _tk  # noqa: F401  (imported for side effect below)
        from tkinter import ttk as _ttk

        tk = _tk
        ttk = _ttk
    return tk


# --------------------------------------------------------------------- 纯逻辑层
def parse_roadmap(path: str | os.PathLike | None = None) -> dict:
    """Parse ``docs/ROADMAP.md`` into counts + a checkbox item list.

    ``total`` / ``done`` / ``pending`` count only real checkbox lines; the
    plain ``- `` sub-bullets used for sub-notes are ignored on purpose (the
    roadmap's maintenance rules require that). ``all_done`` is True only for a
    standalone ``ALL_DONE`` line — prose mentions do not count, matching
    ``scripts/auto_heal_loop.py``.
    """
    target = Path(path) if path is not None else ROOT / ROADMAP_PATH
    doc: dict = {
        "path": str(target),
        "total": 0,
        "done": 0,
        "pending": 0,
        "all_done": False,
        "items": [],
        "missing": False,
    }
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        doc["missing"] = True
        return doc

    items: list[dict] = []
    all_done = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = ITEM_RE.match(line)
        if match:
            items.append(
                {
                    "line": lineno,
                    "done": match.group(1) in ("x", "X"),
                    "text": (match.group(2) or "").strip(),
                }
            )
        elif line.strip().strip("`").strip() == ALL_DONE_MARKER:
            all_done = True

    doc["items"] = items
    doc["total"] = len(items)
    doc["done"] = sum(1 for item in items if item["done"])
    doc["pending"] = doc["total"] - doc["done"]
    doc["all_done"] = all_done
    return doc


def read_log_tail(
    path: str | os.PathLike | None = None, max_lines: int = LOG_TAIL_LINES
) -> list[str]:
    """Return the last ``max_lines`` lines of ``runs/overnight.log``.

    Missing/unreadable file yields ``[]`` so the UI shows an empty pane instead
    of crashing on a fresh checkout (``runs/`` is not tracked).
    """
    target = Path(path) if path is not None else ROOT / LOG_PATH
    try:
        limit = int(max_lines)
    except (TypeError, ValueError):
        limit = LOG_TAIL_LINES
    if limit <= 0:
        return []
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            tail = collections.deque(handle, maxlen=limit)
    except OSError:
        return []
    return [line.rstrip("\r\n") for line in tail]


def read_opencode_cooldown(
    path: str | os.PathLike | None = None, *, now: datetime | None = None
) -> dict:
    """Read ``runs/opencode_cooldown.json`` and report the remaining cooldown.

    Returns ``{"available", "status", "remaining_s", "until", "hint", "label"}``.
    Anything unreadable, malformed, or already expired reports *available* — a
    stale marker must never make the UI claim the endpoint is still blocked.
    """
    target = Path(path) if path is not None else ROOT / COOLDOWN_PATH
    current = now or datetime.now()
    doc: dict = {
        "available": True,
        "status": "available",
        "remaining_s": 0.0,
        "until": None,
        "hint": None,
        "label": OPENCODE_READY_LABEL,
    }
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return doc
    if not isinstance(raw, dict):
        return doc

    doc["hint"] = raw.get("hint")
    doc["until"] = raw.get("until")
    try:
        until = datetime.fromisoformat(str(raw.get("until")))
        remaining = (until - current).total_seconds()
    except (TypeError, ValueError):
        return doc
    if remaining <= 0:
        return doc

    minutes, seconds = divmod(int(remaining), 60)
    doc.update(
        available=False,
        status="cooldown",
        remaining_s=float(remaining),
        label="冷却 %02d:%02d" % (minutes, seconds),
    )
    return doc


def _num(value) -> str:
    """Render 300 / 300.0 as ``"300"`` so the command line stays readable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else str(number)


def build_daemon_cmd(
    root: str | os.PathLike | None = None,
    *,
    python_exe: str | None = None,
    brain: str = "auto",
    executor: str = "auto",
    executor_profile: str = PROFILE_DEFAULT,
    stop_on_bug: bool = True,
    timeout=300,
    sleep=60,
) -> list[str]:
    """Build the detached daemon argv for ``scripts/auto_heal_loop.py --daemon``.

    ``--executor-profile`` is **always** passed, including ``none``: the
    scheduler's own default is ``test``, and only an explicit ``none`` makes it
    keep each endpoint's current model (``--profile`` omitted downstream).
    """
    base = Path(root) if root is not None else ROOT
    exe = python_exe or sys.executable
    cmd = [
        str(exe),
        str(base / DAEMON_SCRIPT),
        "--daemon",
        "--brain",
        str(brain),
        "--executor",
        str(executor),
        "--executor-profile",
        str(executor_profile),
        "--timeout",
        _num(timeout),
        "--sleep",
        _num(sleep),
    ]
    if stop_on_bug:
        cmd.append("--stop-on-bug")
    return cmd


def _no_window_flag() -> int:
    """``CREATE_NO_WINDOW`` on Windows, ``0`` elsewhere."""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _detached_flags() -> int:
    """Flags that fully detach the daemon from this console.

    ``CREATE_NEW_PROCESS_GROUP`` also keeps Ctrl+C in the console away from the
    scheduler; ``DETACHED_PROCESS`` means closing the GUI does not kill it.
    """
    if os.name != "nt":
        return 0
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= int(getattr(subprocess, name, 0))
    return flags


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _extract_json(text: str):
    """Parse a JSON document that may be wrapped in unrelated stdout noise."""
    blob = (text or "").strip()
    if not blob:
        return None
    try:
        return json.loads(blob)
    except ValueError:
        pass
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(blob[start : end + 1])
    except ValueError:
        return None


def run_bridge_json(
    *args,
    root: str | os.PathLike | None = None,
    python_exe: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Call ``bridge.py <args>`` without a console window and parse its JSON.

    Used for ``daemon-status`` / ``stop`` / ``doctor``. Always returns a dict
    with ``ok``; transport problems come back as ``error`` values instead of
    raising, because this runs inside worker threads.
    """
    base = Path(root) if root is not None else ROOT
    exe = python_exe or sys.executable
    cmd = [str(exe), str(base / BRIDGE_SCRIPT)] + [str(arg) for arg in args]
    result: dict = {"ok": False, "command": cmd, "returncode": None, "error": ""}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(base),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_no_window_flag(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
        return result
    except OSError as exc:
        result["error"] = "oserror: %s" % exc
        return result

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    result["returncode"] = proc.returncode
    result["stderr"] = stderr.strip()[-2000:]
    parsed = _extract_json(stdout)
    if not isinstance(parsed, dict):
        result["error"] = "invalid_json"
        result["stdout"] = stdout.strip()[-2000:]
        return result

    parsed.setdefault("ok", proc.returncode == 0)
    parsed["returncode"] = proc.returncode
    parsed["command"] = cmd
    return parsed


def launch_daemon_detached(
    cmd: list[str],
    *,
    root: str | os.PathLike | None = None,
    log_path: str | os.PathLike | None = None,
) -> dict:
    """Spawn the daemon detached, appending its output to ``runs/overnight.log``."""
    base = Path(root) if root is not None else ROOT
    log = Path(log_path) if log_path is not None else base / LOG_PATH
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a", encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": "log_open: %s" % exc, "command": list(cmd)}
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(base),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_detached_flags(),
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "error": "spawn: %s" % exc, "command": list(cmd)}
    finally:
        handle.close()
    return {"ok": True, "pid": proc.pid, "command": list(cmd), "log": str(log)}


def compute_secondary_geometry(
    width: int = PANEL_W,
    height: int = PANEL_H,
    secondary_x: int = SECONDARY_X,
    secondary_y: int = SECONDARY_Y,
) -> str:
    """Return the dock geometry for the secondary screen, e.g. ``480x700-500+40``.

    ``x < 0`` is deliberate: the main screen is ``0..2560``, so any negative x
    keeps the window off it. Note that Tk reads a *leading* ``-`` as "offset
    from the right edge", so :func:`place_window_on_secondary` re-applies the
    parsed coordinates with ``SetWindowPos`` instead of trusting the string.
    """
    panel_w = int(width)
    panel_h = int(height)
    if panel_w <= 0 or panel_h <= 0:
        raise ValueError("width/height must be positive")
    return "%dx%d%+d%+d" % (panel_w, panel_h, int(secondary_x), int(secondary_y))


def parse_geometry(spec: str) -> tuple[int, int, int, int]:
    """Inverse of :func:`compute_secondary_geometry` → ``(w, h, x, y)``."""
    match = re.match(r"^\s*(\d+)x(\d+)([+-]\d+)([+-]\d+)\s*$", str(spec))
    if not match:
        raise ValueError("bad geometry spec: %r" % (spec,))
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def status_badge(status, roadmap: dict | None = None) -> dict:
    """Map scheduler state + roadmap to a colored badge.

    Precedence: a live daemon wins, then a bug stop (loudest problem), then
    ``ALL_DONE`` (from the state machine *or* the roadmap), else stopped.
    ``status`` accepts either a ``daemon-status`` dict or a plain state string.
    """
    state = ""
    running = False
    if isinstance(status, dict):
        running = bool(status.get("running"))
        state = str(status.get("state") or "")
    elif status is not None:
        state = str(status)
        running = state.strip().upper() in ("RUNNING", "RUN")

    upper = state.strip().upper()
    if running:
        key = "running"
    elif upper == "STOPPED_ON_BUG":
        key = "bug"
    elif upper == "ALL_DONE" or bool((roadmap or {}).get("all_done")):
        key = "all_done"
    else:
        key = "stopped"
    return {
        "key": key,
        "label": BADGE_LABELS[key],
        "color": BADGE_COLORS[key],
        "state": state or None,
    }


# ------------------------------------------------------------------- 界面层
BG = "#0d1117"
PANEL = "#161b22"
PANEL_ALT = "#21262d"
BORDER = "#30363d"
FG = "#c9d1d9"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"
GREEN = "#238636"
RED = "#b62324"
MONO = ("Consolas", 9)
MONO_SMALL = ("Consolas", 8)

# ---------------------------------------------------------------- M4-5 样式
#: 实时日志的换行模式：``char`` 让没有空格的长 URL / JSON / 路径也能按字符折行，
#: 而不是在右侧被硬截断（``none``/``word`` 都会留下看不见的尾巴）。
LOG_WRAP = "char"
#: 日志面板可见行数（高度固定，宽度交给窗口）。
LOG_HEIGHT = 12

#: ``clam`` 主题下 TCombobox 的高对比深色配色。
#: ``clam`` 的 readonly 状态会忽略 ``background``，只认显式的 state map，
#: 所以只 configure 不 map 时下拉框会渲染成「白底 + 浅灰字」（实机观感问题）。
COMBOBOX_STYLE_NAME = "Bridge.TCombobox"
COMBOBOX_FIELD_BG = "#1e293b"
COMBOBOX_ROW_BG = "#334155"
COMBOBOX_FG = "#f8fafc"
COMBOBOX_ARROW = "#e2e8f0"

COMBOBOX_STYLE_CONFIG: dict[str, dict] = {
    # 未展开时的底色/文字/箭头。
    "base": {
        "fieldbackground": COMBOBOX_FIELD_BG,
        "background": COMBOBOX_ROW_BG,
        "foreground": COMBOBOX_FG,
        "arrowcolor": COMBOBOX_ARROW,
        "bordercolor": BORDER,
        "lightcolor": BORDER,
        "darkcolor": BORDER,
    },
    # readonly 状态映射（ttk 的 map 语法：option -> [(state, value)]）。
    "readonly": {
        "fieldbackground": [("readonly", COMBOBOX_FIELD_BG)],
        "background": [("readonly", COMBOBOX_ROW_BG)],
        "foreground": [("readonly", COMBOBOX_FG)],
        "selectbackground": [("readonly", COMBOBOX_ROW_BG)],
        "selectforeground": [("readonly", COMBOBOX_FG)],
        "arrowcolor": [("readonly", COMBOBOX_ARROW)],
    },
    # 展开后的下拉列表（PopupListbox 走经典 option 数据库，不受 ttk 样式影响）。
    "popdown": {
        "*TCombobox*Listbox*Background": COMBOBOX_FIELD_BG,
        "*TCombobox*Listbox*Foreground": COMBOBOX_FG,
        "*TCombobox*Listbox*selectBackground": COMBOBOX_ROW_BG,
        "*TCombobox*Listbox*selectForeground": COMBOBOX_FG,
    },
}


def configure_dark_combobox_style(
    style=None,
    *,
    option_add=None,
    style_name: str = COMBOBOX_STYLE_NAME,
) -> dict:
    """把高对比深色配色装到 ttk.Style 与 option 数据库上。

    ``style`` / ``option_add`` 都是可选协作者，返回值是真正生效的原始映射，
    因此可以在不创建任何 Tk 窗口的前提下离线断言「readonly 是否高对比」。
    """
    base = dict(COMBOBOX_STYLE_CONFIG["base"])
    readonly = {key: list(pairs) for key, pairs in COMBOBOX_STYLE_CONFIG["readonly"].items()}
    popdown = dict(COMBOBOX_STYLE_CONFIG["popdown"])
    if style is not None:
        style.configure(style_name, **base)
        style.map(style_name, **readonly)
    if option_add is not None:
        for option, value in popdown.items():
            option_add(option, value)
    return {"style": style_name, "base": base, "readonly": readonly, "popdown": popdown}


def log_text_kwargs() -> dict:
    """实时日志 ``Text`` 的构造参数（``wrap="char"`` + 固定行高）。"""
    return {"height": LOG_HEIGHT, "font": MONO, "wrap": LOG_WRAP}


def should_autoscroll(yview) -> bool:
    """视图已经在底部时才自动滚到底，避免把正在上翻的用户拽回末尾。"""
    try:
        _first, last = yview
        return float(last) >= 0.999
    except (TypeError, ValueError):
        return True


def place_window_on_secondary(root, spec: str) -> None:
    """Pin a Toplevel to the secondary screen without activating it.

    Tk's geometry string treats ``-x`` as "distance from the right edge", which
    would drop the panel back onto the main screen. So the parsed coordinates
    are applied with ``SetWindowPos(..., SWP_NOACTIVATE | SWP_NOZORDER)`` — the
    same convention ``engine/window.py`` uses.
    """
    width, height, x, y = parse_geometry(spec)
    root.geometry("%dx%d" % (width, height))
    try:
        root.update_idletasks()
    except Exception:  # noqa: BLE001 - cosmetic only
        return
    if os.name != "nt":
        root.geometry(spec)
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(root.winfo_id())
        parent = user32.GetParent(hwnd) or hwnd
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            parent, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE
        )
    except Exception:  # noqa: BLE001 - never let cosmetics break the console
        return


class BridgeConsole:
    """Thin dashboard shell: three buttons, one checklist, one log pane."""

    def __init__(self, root, *, repo_root: Path | None = None, refresh_ms: int = REFRESH_MS):
        self.root = root
        self.repo_root = Path(repo_root) if repo_root is not None else ROOT
        self.refresh_ms = int(refresh_ms)
        self.queue: queue.Queue = queue.Queue()
        self._ticks = 0
        self._last_status: dict = {}
        self._note_until = 0.0
        self._build()
        self.root.after(120, self._pump)
        self.root.after(self.refresh_ms, self._tick)

    # -- layout ---------------------------------------------------------
    def _build(self) -> None:
        _load_tk()
        root = self.root
        root.title("Bridge v0.2.5 — 桌面控制台")
        root.configure(bg=BG)
        root.minsize(420, 560)
        place_window_on_secondary(root, compute_secondary_geometry())

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=10, pady=(8, 4))
        self.light = tk.Canvas(
            head, width=12, height=12, bg=BG, highlightthickness=0, bd=0
        )
        self.light.pack(side="left", pady=2)
        self.dot = self.light.create_oval(
            1, 1, 11, 11, fill=BADGE_COLORS["stopped"], outline=""
        )
        self.status_label = tk.Label(
            head, text="已停止", bg=BG, fg=FG, font=("Microsoft YaHei UI", 11, "bold")
        )
        self.status_label.pack(side="left", padx=(6, 0))
        self.version_label = tk.Label(
            head, text="v0.2.5", bg=BG, fg=FG_DIM, font=("Consolas", 8)
        )
        self.version_label.pack(side="right")

        self.meta_label = tk.Label(
            root,
            text="Round - | 主线 -/- | 待复核 - | OpenCode 可用",
            bg=BG,
            fg=FG_DIM,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            justify="left",
        )
        self.meta_label.pack(fill="x", padx=12, pady=(0, 6))

        ctl = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        ctl.pack(fill="x", padx=10, pady=(0, 6))
        row = tk.Frame(ctl, bg=PANEL)
        row.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(row, text="执行器 Profile", bg=PANEL, fg=FG_DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001 - theme is cosmetic
            pass
        # 高对比深色：clam 的 readonly 必须显式 map，否则白底浅灰字看不清。
        configure_dark_combobox_style(style, option_add=root.option_add)
        self.profile_var = tk.StringVar(value=PROFILE_DEFAULT)
        self.profile_box = ttk.Combobox(
            row,
            textvariable=self.profile_var,
            values=list(PROFILE_CHOICES),
            state="readonly",
            width=10,
            style="Bridge.TCombobox",
        )
        self.profile_box.pack(side="left", padx=(6, 10))

        self.bug_var = tk.BooleanVar(value=True)
        self.bug_check = tk.Checkbutton(
            row,
            text="遇 Bug 立即上报并停机",
            variable=self.bug_var,
            bg=PANEL,
            fg=FG,
            selectcolor=PANEL_ALT,
            activebackground=PANEL,
            activeforeground=FG,
            font=("Microsoft YaHei UI", 8),
            bd=0,
            highlightthickness=0,
        )
        self.bug_check.pack(side="left")

        buttons = tk.Frame(ctl, bg=PANEL)
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        self.start_btn = self._button(
            buttons, "▶ 启动自迭代", self._on_start, bg=GREEN
        )
        self.stop_btn = self._button(buttons, "■ 一键急停", self._on_stop, bg=RED)
        self.doctor_btn = self._button(buttons, "↻ Doctor 自检", self._on_doctor)
        for btn in (self.start_btn, self.stop_btn, self.doctor_btn):
            btn.pack(side="left", expand=True, fill="x", padx=2)

        self.roadmap_text = self._readonly_text(
            root, height=7, font=MONO_SMALL, wrap="word"
        )
        self.roadmap_text.pack(fill="x", padx=10, pady=(0, 6))

        log_frame = tk.Frame(root, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        # wrap="char"：长 URL / JSON / 路径也能按字符折行，右侧不再截断。
        self.log_text = self._readonly_text(log_frame, **log_text_kwargs())
        scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("err", foreground="#f85149")
        self.log_text.tag_configure("warn", foreground="#d29922")
        self.log_text.tag_configure("note", foreground=ACCENT)

    def _button(self, parent, text, command, *, bg=PANEL_ALT):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg="#ffffff" if bg != PANEL_ALT else FG,
            activebackground=BORDER,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=6,
            pady=5,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )

    def _readonly_text(self, parent, *, height, font, wrap="none"):
        widget = tk.Text(
            parent,
            height=height,
            bg=PANEL,
            fg=FG,
            font=font,
            relief="flat",
            bd=0,
            padx=6,
            pady=4,
            wrap=wrap,
            highlightthickness=0,
            insertbackground=FG,
        )
        widget.configure(state="disabled")
        return widget

    # -- async plumbing -------------------------------------------------
    def _spawn(self, worker) -> None:
        threading.Thread(target=self._run_worker, args=(worker,), daemon=True).start()

    def _run_worker(self, worker) -> None:
        try:
            message = worker()
        except Exception as exc:  # noqa: BLE001 - the shell must never die
            self.queue.put(("note", "后台任务异常：%s" % exc))
            return
        if message is not None:
            self.queue.put(message)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "snapshot":
                    self._render_snapshot(payload)
                elif kind == "status":
                    self._render_status(payload)
                elif kind == "note":
                    self._append_log(str(payload), tag="note")
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001 - keep the Tk loop alive
            pass
        self.root.after(120, self._pump)

    def _tick(self) -> None:
        self._ticks += 1
        self._spawn(self._collect_snapshot)
        if self._ticks % 4 == 1:
            self._spawn(self._collect_status)
        self.root.after(self.refresh_ms, self._tick)

    # -- collectors (run in worker threads) -----------------------------
    def _collect_snapshot(self):
        return (
            "snapshot",
            {
                "roadmap": parse_roadmap(self.repo_root / ROADMAP_PATH),
                "log": read_log_tail(self.repo_root / LOG_PATH, LOG_TAIL_LINES),
                "cooldown": read_opencode_cooldown(self.repo_root / COOLDOWN_PATH),
            },
        )

    def _collect_status(self):
        status = run_bridge_json("daemon-status", root=self.repo_root)
        return ("status", status)

    # -- rendering (main thread only) -----------------------------------
    def _render_snapshot(self, payload) -> None:
        roadmap = payload.get("roadmap") or {}
        cooldown = payload.get("cooldown") or {}
        self._render_roadmap(roadmap)
        self._render_log(payload.get("log") or [])
        self._refresh_meta(self._last_status, roadmap, cooldown)
        badge = status_badge(self._last_status or {}, roadmap)
        self._paint_badge(badge)

    def _render_status(self, status) -> None:
        self._last_status = status if isinstance(status, dict) else {}
        if self._last_status.get("ok") is False:
            self._append_log(
                "daemon-status 失败：%s"
                % (self._last_status.get("error") or "unknown"),
                tag="err",
            )
            return
        self._refresh_meta(self._last_status, None, None)
        self._paint_badge(status_badge(self._last_status))

    def _refresh_meta(self, status, roadmap, cooldown) -> None:
        status = status or {}
        if roadmap is None:
            roadmap = getattr(self, "_roadmap_cache", {})
        else:
            self._roadmap_cache = roadmap
        if cooldown is None:
            cooldown = getattr(self, "_cooldown_cache", {})
        else:
            self._cooldown_cache = cooldown

        round_no = status.get("state_round")
        total = roadmap.get("total")
        done = roadmap.get("done")
        review = status.get("dispatch_needs_review")
        self.meta_label.configure(
            text="Round %s | 主线 %s/%s | 待复核 %s | OpenCode %s"
            % (
                "-" if round_no is None else round_no,
                "-" if done is None else done,
                "-" if total is None else total,
                "-" if review is None else review,
                cooldown.get("label") or OPENCODE_READY_LABEL,
            )
        )

    def _paint_badge(self, badge: dict) -> None:
        self.status_label.configure(text=badge["label"], fg=badge["color"])
        self.light.itemconfigure(self.dot, fill=badge["color"])

    def _render_roadmap(self, roadmap: dict) -> None:
        lines = []
        for item in roadmap.get("items") or []:
            lines.append("%s %s" % ("[x]" if item["done"] else "[ ]", item["text"]))
        if roadmap.get("missing"):
            lines = ["docs/ROADMAP.md 不存在"]
        elif roadmap.get("all_done"):
            lines.append("")
            lines.append("ALL_DONE")
        if getattr(self, "_roadmap_snapshot", None) == lines:
            return
        self._roadmap_snapshot = list(lines)
        self._set_text(self.roadmap_text, lines)

    def _render_log(self, lines: list[str]) -> None:
        # Give a just-clicked action result a few seconds of visibility before
        # the tail refresh replaces the pane again.
        if time.monotonic() < self._note_until:
            return
        widget = self.log_text
        if getattr(self, "_log_snapshot", None) == lines:
            return
        self._log_snapshot = list(lines)
        # 只有原本就贴在底部才自动滚底：用户上翻看历史时不要被拽回末尾。
        stick_to_bottom = should_autoscroll(widget.yview())
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for line in lines:
            tag = ()
            if "[ERROR]" in line:
                tag = ("err",)
            elif "[WARN]" in line:
                tag = ("warn",)
            widget.insert("end", line + "\n", tag)
        if stick_to_bottom:
            widget.see("end")
        widget.configure(state="disabled")

    def _append_log(self, text: str, *, tag: str | None = None) -> None:
        widget = self.log_text
        stick_to_bottom = should_autoscroll(widget.yview())
        widget.configure(state="normal")
        widget.insert("end", text + "\n", (tag,) if tag else ())
        if stick_to_bottom:
            widget.see("end")
        widget.configure(state="disabled")
        self._note_until = time.monotonic() + NOTE_HOLD_S

    @staticmethod
    def _set_text(widget, lines: list[str]) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "\n".join(lines))
        widget.configure(state="disabled")

    # -- actions --------------------------------------------------------
    def _on_start(self) -> None:
        profile = self.profile_var.get() or PROFILE_DEFAULT
        stop_on_bug = bool(self.bug_var.get())
        self._append_log(
            "启动自迭代：profile=%s stop_on_bug=%s" % (profile, stop_on_bug), tag="note"
        )
        self._spawn(lambda: self._do_start(profile, stop_on_bug))

    def _do_start(self, profile: str, stop_on_bug: bool):
        status = run_bridge_json("daemon-status", root=self.repo_root)
        if status.get("ok") is False:
            return ("note", "启动中止：daemon-status %s" % (status.get("error") or "失败"))
        if status.get("running"):
            return ("note", "已在运行（pid=%s），未重复启动" % status.get("pid"))

        cmd = build_daemon_cmd(
            self.repo_root,
            python_exe=sys.executable,
            brain="auto",
            executor="auto",
            executor_profile=profile,
            stop_on_bug=stop_on_bug,
            timeout=300,
            sleep=60,
        )
        result = launch_daemon_detached(cmd, root=self.repo_root)
        if not result.get("ok"):
            return ("note", "启动失败：%s" % result.get("error"))
        return ("note", "已分离启动调度器 pid=%s（日志 %s）" % (result.get("pid"), result.get("log")))

    def _on_stop(self) -> None:
        self._append_log("一键急停：写入 runs/STOP 并等待退出…", tag="warn")
        self._spawn(self._do_stop)

    def _do_stop(self):
        result = run_bridge_json("stop", root=self.repo_root)
        if result.get("ok") is False:
            return ("note", "急停失败：%s" % (result.get("error") or "unknown"))
        return ("note", "急停结果：%s" % (result.get("detail") or result.get("action")))

    def _on_doctor(self) -> None:
        self._append_log("Doctor 自检中…", tag="note")
        self._spawn(self._do_doctor)

    def _do_doctor(self):
        result = run_bridge_json("doctor", root=self.repo_root)
        if result.get("ok") is False and not result.get("version"):
            return ("note", "Doctor 失败：%s" % (result.get("error") or "unknown"))
        issues = result.get("issues") or []
        if not issues:
            return ("note", "Doctor ok=%s endpoints=%s issues=0"
                    % (result.get("ok"), result.get("endpoint_count")))
        return ("note", "Doctor 发现 %d 个问题：%s" % (len(issues), "; ".join(map(str, issues))[:400]))


def launch_gui(*, repo_root: Path | None = None) -> int:
    """Create the Tk root and block on the UI loop. Offline tests never call this."""
    _load_tk()
    root = tk.Tk()
    BridgeConsole(root, repo_root=repo_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())
