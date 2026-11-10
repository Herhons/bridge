"""Bridge v0.0.9 本地后端（CARD-09a）。

只做后端：不开浏览器、不点 UI、不装插件。把「一次会审」拆成可轮询的 job，
真正执行时复用现成的 `*_choose.py` / `*_use.py`，一个端失败不影响别的端。

## 边界（卡面第 1、2、5 条）

- 只绑 `127.0.0.1`，默认端口 18765（`docs\\bridge_server.json` 可改）。
- 端口被占 / 已有实例在跑：直接报错退出，不抢占、不换端口。
- token 首次启动生成，落在 `%LOCALAPPDATA%\\Bridge\\v009\\server.json`（含 port），
  之后沿用；`bridge_server.py rotate` 换新的。token 不进 URL、不进日志、不回显。
- 除 `GET /health` 外都要 `Authorization: Bearer <token>`；Host 必须是
  `127.0.0.1:<端口>`；Origin 配了 `allowed_extension_id` 时**只认**
  `chrome-extension://<该 ID>`，没配时退回「只认 chrome-extension:// 前缀」；
  不带 Origin 的请求放行（扩展的 GET 不保证带 Origin），但照样要 Bearer token。
- endpoint 只能取 `docs\\tiers.json` 里登记过的；purpose 必填，只有 test/review/accept。
- 子进程一律「固定脚本路径 + 参数数组」，`shell=False`；每端有总超时，
  超时杀整棵进程树；输出截断到 `output_limit`。

## 一个端怎么跑（第 5、6 条）

    runs\\ui.lock 加锁（覆盖 choose → 读回 → use 全程）
      → *_choose.py <--model|--effort|--think> <target>
      → 读 runs\\tier\\<端>.json：退出码 0 + 回执新鲜 + readback == target 才算确认
      → *_use.py <prompt> --purpose <purpose> --timeout <n>
      → 解析 stdout 最后一行的 JSON

choose 没确认就**不跑 use**，该端记失败；锁拿不到也记失败，不排队硬等。
服务内只用线程锁是不够的：命令行直接跑 `*_use.py` 是另一个进程，
所以锁落在 `runs\\ui.lock`，见 `qi.uilock`。

## job（第 7 条）

- `POST /ask` 立刻 202 + job_id，真正执行在后台单线程里串行做（前台只有一个焦点）。
- 每端分开记 status / reply / error_code / exit_code；job 整体 done / partial / failed。
- 落盘 `runs\\jobs\\<job_id>.json`（临时文件 + os.replace），内存只留最近 50 个；
  重启后 `GET /jobs/<id>` 仍从磁盘读得到。
- 同 `request_id` 重复 POST 返回同一个 job，不重复派发；队列满 20 个返回 429。

## 工作流（第 8 条）

`GET /workflow?file=` / `POST /workflow` 只认 `progress.json`、`progress-test.json`
两个 basename（防路径穿越）；迁移表从 `docs\\workflow\\workflow.json` 读，
非法迁移 409；写盘用同目录临时文件 + `os.replace`，并与notify_done/工作流CLI共用完整读改写文件锁，
和 `notify_done.py` 抢着写时不会留下半个 JSON。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hmac
import inspect
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import heartbeat, progress, tier_guard, uilock

ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1"
DEFAULT_PORT = 18765

PURPOSES = ("test", "review", "accept")
DIMENSION_ORDER = ("model", "effort", "think")
DIMENSION_FLAGS = {"model": "--model", "effort": "--effort", "think": "--think"}
WORKFLOW_FILES = ("progress.json", "progress-test.json")
TERMINAL_JOB_STATES = ("done", "partial", "failed")

MAX_BODY = 1024 * 1024
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CARD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
EXTENSION_ORIGIN = "chrome-extension://"
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")     # Chrome 扩展 ID 只用 a-p 这 16 个字母

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "queue_limit": 20,
    "memory_jobs": 50,
    "choose_timeout": 120.0,
    # T2-2（施工单 2026-11-07）：POST_SUBMIT 上限 120s。
    # 只对 test / accept_lite 生效；review / accept 由 tier_guard.floor_timeout()
    # 抬到 REVIEW_TIMEOUT=900（高档会审本来就慢），属已批准的偏差，见 CALIBER §2。
    "use_timeout": 120.0,
    "endpoint_timeout": 420.0,
    "ui_lock_timeout": 600.0,
    # B12：并发会审。默认 1 / 1 = 与今天完全一致（既有 job 测试一条都不动）。
    # 真正开 4 席是在 docs/bridge_server.json 里配，不在这里改默认值。
    "endpoint_concurrency": 1,
    "endpoint_stagger": 0.0,   # 秒；>0 时各席错峰起跑（见 _run_job 注释）
    "min_success": 1,
    # 这些端要读屏幕像素（deepseek 的模板、grok 的动作条），被抢前台就读错。
    # 它们在"串行相"里一个接一个跑，不与其他端时间重叠。
    "exclusive_endpoints": [],
    # B15：一个 job 最多几席。默认 8 = 今天的行为上限；shipped 配 4，
    # 因为"4 网页会审"与"4 桌面任务"是两种口径（回收线语义不同），应分两轮。
    "max_endpoints_per_job": 8,
    # B15：网页端开窗形态。代码默认 per_site（= 今天的行为），
    # shipped 配 tabs（一个 Chrome 窗 + 每端一个标签，读之前切过去）。
    "web_window_mode": "per_site",
    # B15：这些端在派发前**在锁外**先拉起（现在只有 dsh 需要，它自己 Popen 会占着锁等 40s）。
    "prelaunch_endpoints": [],
    # B15：按端预算覆盖，**只许抬高**。{"codex": {"use": 300}, "grok:test": {"use": 180}}
    "budget_overrides": {},
    # B15：`busy` 停滞判据（秒）。{} = 关闭；{"codex": 45} = codex 的停止键常驻 45s 不动就按不忙处理。
    "stall_busy_ignore": {},
    # B15：按端追加给 `<端>_choose.py` 的参数。{"chatgpt": ["--new"]} 表示每次先开新会话
    # —— chatgpt 的"聊天/工作"开关只有在新聊天页才读得到（真机 2026-11-08）。
    "choose_args": {},
    "output_limit": 65536,
    "prompt_limit": 20000,
    "allowed_extension_id": "",     # 空 = 没配，Origin 退回前缀匹配（CARD-09b 兼容 09a）
    # F5 场景③（"找不到输入框"）需要的可控故障注入。
    # **默认 false**，且必须同时存在 runs/test-faults.json 才生效；
    # 生产配置下这两条永远不同时满足，所以不可能误触发。
    "allow_test_faults": False,
    # B26：启动时把上一次进程留下的 queued/running 标成 failed(interrupted_by_restart)。
    # 关掉它只是不做修复，僵尸 job 仍然永久停在原状态 —— 默认开。
    "recover_interrupted_jobs": True,
}


class RequestError(Exception):
    """HTTP 层能直接翻译成状态码的错误。"""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class QueueFull(RequestError):
    def __init__(self, limit: int):
        super().__init__(429, "queue_full", f"too many active jobs (limit={limit})")


class EndpointFailure(Exception):
    """某一个端失败；只影响这个端。"""

    def __init__(self, code: str, message: str, *, exit_code=None, step: str = "choose"):
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.step = step
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------- 小工具
def _retryable_after_restart(job: dict) -> bool:
    """只有"上一次被重启打断"的 job 才允许按同一个 request_id 重排。"""
    return (job.get("status") in TERMINAL_JOB_STATES
            and job.get("error_code") == "interrupted_by_restart")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: str | Path) -> dict | None:
    """读 JSON 对象；不存在/坏文件都返回 None。兼容 UTF-8 BOM。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path: str | Path, payload: dict) -> Path:
    """同目录临时文件 + os.replace：不会留下半个 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp, target)
    return target


def truncate(text: object, limit: int) -> str:
    """超长输出留头留尾，中间标出来；不静默丢内容。"""
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return f"{value[:head]}\n...[truncated {len(value) - limit} chars]...\n{value[-tail:]}"


def parse_last_json(text: str) -> dict | None:
    """取 stdout 里最后一行 JSON 对象（choose/use 都会先打印资源库报告）。"""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _decode(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return "" if data is None else str(data)


# ---------------------------------------------------------------- 配置
BUDGET_KINDS = ("use", "choose", "endpoint")
#: 覆盖只许抬高，且不允许把某一席抬成"永远不超时"。
BUDGET_CEILING = 3600.0
#: 错峰启动的单步上限。配置写错（999 甚至 Infinity）时，`index * stagger`
#: 会把整个 job 挂成小时级，而它只是一次 sleep，不消耗任何超时预算。
STAGGER_CEILING = 30.0


def normalize_name_list(value) -> list[str]:
    """端名列表：只收字符串、去重、小写。字符串会被拆成字符，所以显式拒绝。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        name = str(item or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def normalize_budgets(value, *, endpoints=None, purposes=None) -> dict:
    """把 budget_overrides 归一成 {键: {use/choose/endpoint: 正数}}。

    坏值一律**忽略**（打印一行提示），绝不抛错 —— 配置写错不该让后端起不来。
    键允许三种：`*`、端名、`端名:用途`。
    """
    if not isinstance(value, dict):
        return {}
    known_endpoints = set(endpoints or ())
    known_purposes = set(purposes or ())
    out: dict[str, dict] = {}
    for raw_key, block in value.items():
        key = str(raw_key or "").strip().lower()
        if not key:
            continue
        if key != "*":
            name, _, purpose = key.partition(":")
            if known_endpoints and name not in known_endpoints:
                print(f"[config] budget_overrides 忽略未知端: {raw_key}", file=sys.stderr)
                continue
            if purpose and known_purposes and purpose not in known_purposes:
                print(f"[config] budget_overrides 忽略未知用途: {raw_key}", file=sys.stderr)
                continue
        if not isinstance(block, dict):
            print(f"[config] budget_overrides[{raw_key}] 不是对象，已忽略", file=sys.stderr)
            continue
        clean: dict[str, float] = {}
        for kind, amount in block.items():
            name = str(kind or "").strip().lower()
            if name not in BUDGET_KINDS:
                continue
            try:
                number = float(amount)
            except (TypeError, ValueError):
                continue
            if not (number > 0) or number != number or number in (float("inf"),):
                continue
            clean[name] = min(number, BUDGET_CEILING)
        if clean:
            out[key] = clean
    return out


def normalize_number_map(value) -> dict[str, float]:
    """`{端名: 正数}`（stall_busy_ignore 用）。坏值忽略。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for raw_key, amount in value.items():
        name = str(raw_key or "").strip().lower()
        if not name:
            continue
        try:
            number = float(amount)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out[name] = number
    return out


def apply_budgets(config: dict, endpoint: str, purpose: str, kind: str, base: float) -> float:
    """按 `*` → 端 → 端:用途 三层叠加，**只许抬高**，最后夹到 ceiling。

    这条"只许抬高"是刻意的：`use_timeout=120` 与 review 的 1500 下限都被测试钉着，
    覆盖只能加时间，不能把它们压低。
    """
    effective = float(base)
    overrides = config.get("budget_overrides") or {}
    for key in ("*", str(endpoint).lower(), f"{str(endpoint).lower()}:{str(purpose).lower()}"):
        block = overrides.get(key)
        if isinstance(block, dict) and kind in block:
            try:
                effective = max(effective, float(block[kind]))
            except (TypeError, ValueError):
                continue
    return min(effective, BUDGET_CEILING)


def load_config(root: str | Path | None = None) -> dict:
    """读 docs\\bridge_server.json；坏文件按默认值走，不把服务拖死。"""
    base = Path(root) if root is not None else ROOT
    config = dict(DEFAULT_CONFIG)
    data = read_json(base / "docs" / "bridge_server.json") or {}
    for key, default in DEFAULT_CONFIG.items():
        value = data.get(key, default)
        try:
            config[key] = type(default)(value)
        except (TypeError, ValueError):
            config[key] = default
    if not 1 <= int(config["port"]) <= 65535:
        config["port"] = DEFAULT_PORT
    if int(config["queue_limit"]) < 1:
        config["queue_limit"] = DEFAULT_CONFIG["queue_limit"]
    if int(config["memory_jobs"]) < 1:
        config["memory_jobs"] = DEFAULT_CONFIG["memory_jobs"]

    # B15 的新键单独归一：`type(default)(value)` 对嵌套结构会把脏数据静默收进来。
    config["exclusive_endpoints"] = normalize_name_list(config.get("exclusive_endpoints"))
    # B26：错峰步长夹到 [0, STAGGER_CEILING]。`Infinity` 能通过 float()，
    # 不夹的话 `index * stagger` 会让 job 永久 sleep。
    try:
        config["endpoint_stagger"] = min(
            max(0.0, float(config.get("endpoint_stagger") or 0)), STAGGER_CEILING)
    except (TypeError, ValueError):
        config["endpoint_stagger"] = DEFAULT_CONFIG["endpoint_stagger"]
    config["prelaunch_endpoints"] = normalize_name_list(config.get("prelaunch_endpoints"))
    config["stall_busy_ignore"] = normalize_number_map(data.get("stall_busy_ignore"))
    try:
        known = list((tier_guard.load_tiers(base / "docs" / "tiers.json")
                      .get("endpoints") or {}).keys())
    except Exception:                       # noqa: BLE001 - 表坏了不该拖死配置
        known = []
    config["budget_overrides"] = normalize_budgets(
        data.get("budget_overrides"), endpoints=known, purposes=PURPOSES)
    raw_args = data.get("choose_args")
    args_map: dict[str, list[str]] = {}
    if isinstance(raw_args, dict):
        for name, extra in raw_args.items():
            key = str(name or "").strip().lower()
            if key and isinstance(extra, list):
                args_map[key] = [str(item) for item in extra if str(item).strip()]
    config["choose_args"] = args_map
    if str(config.get("web_window_mode") or "") not in ("tabs", "per_site"):
        config["web_window_mode"] = DEFAULT_CONFIG["web_window_mode"]
    try:
        cap = int(config.get("max_endpoints_per_job") or 8)
    except (TypeError, ValueError):
        cap = int(DEFAULT_CONFIG["max_endpoints_per_job"])
    config["max_endpoints_per_job"] = max(1, cap)
    return config


def config_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else ROOT
    return base / "docs" / "bridge_server.json"


def normalize_extension_id(value: object) -> str:
    """把 allowed_extension_id 归一成 32 位 a-p 的 ID；不合法返回空串。

    允许直接写 ID，也允许写成整条 `chrome-extension://<ID>`（多写一层协议不会
    静默变成「前缀匹配」——不合法就是空串，交给调用方决定是警告还是拒绝）。
    """
    text = str(value if value is not None else "").strip().lower()
    if text.startswith(EXTENSION_ORIGIN):
        text = text[len(EXTENSION_ORIGIN):]
    text = text.strip().strip("/")
    return text if EXTENSION_ID_RE.match(text) else ""


# ---------------------------------------------------------------- token 状态
def state_dir() -> Path:
    override = os.environ.get("BRIDGE_SERVER_STATE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Bridge" / "v009"


def state_path() -> Path:
    return state_dir() / "server.json"


def pid_path() -> Path:
    return state_dir() / "server.pid"


def load_state() -> dict | None:
    data = read_json(state_path())
    if not data:
        return None
    token = data.get("token")
    if not isinstance(token, str) or len(token) < 16:
        return None
    return data


def save_state(state: dict) -> Path:
    """原子写；权限收到 0600（Windows 上 chmod 只是尽力而为），再用 icacls 兜一层。"""
    path = atomic_write_json(state_path(), state)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    harden_state_acl(path)
    return path


def harden_state_acl(path: str | Path) -> bool:
    """把 server.json 的 ACL 收到当前用户：去掉继承、只给本人完全控制。

    失败只警告，不影响启动——权限收不紧是「差一点」，起不来是「全断」。
    运行时输出保持 ASCII，免得控制台代码页乱码。
    """
    target = Path(path)
    if os.name != "nt" or not target.exists():
        return False
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        sys.stderr.write("[bridge] warn: USERNAME empty; server.json ACL not tightened\n")
        return False
    try:
        proc = subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", f"{user}:F"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("[bridge] warn: icacls unavailable (%s); ACL unchanged\n"
                         % type(exc).__name__)
        return False
    # 中文 Windows 上 icacls 输出是 GBK：不能按 UTF-8 硬解，否则读线程直接抛
    # UnicodeDecodeError（实测会把启动线程卡住 15 秒并打印 traceback）。
    output = (proc.stdout or b"").decode("mbcs", "replace") if isinstance(proc.stdout, bytes) \
        else (proc.stdout or "")
    if proc.returncode != 0:
        detail = output.strip().splitlines()
        tail = f" ({detail[-1].strip()})" if detail else ""
        sys.stderr.write("[bridge] warn: icacls exit=%s; ACL unchanged%s\n"
                         % (proc.returncode, tail))
        return False
    return True


def load_or_create_state(*, port: int) -> dict:
    """有 token 就沿用（只更新端口），没有就生成一次并持久化。"""
    existing = load_state()
    if existing:
        if int(existing.get("port") or 0) != int(port):
            existing["port"] = int(port)
            existing["updated_at"] = now_iso()
            save_state(existing)
        existing["created"] = False
        return existing
    state = {
        "schema_version": 1,
        "port": int(port),
        "token": secrets.token_urlsafe(32),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    save_state(state)
    state["created"] = True
    return state


def rotate_token() -> dict:
    """换一把新 token 并落盘；服务在跑时要重启才生效。"""
    state = load_state() or {}
    state.update({
        "schema_version": 1,
        "token": secrets.token_urlsafe(32),
        "updated_at": now_iso(),
    })
    state.setdefault("port", DEFAULT_PORT)
    state.setdefault("created_at", state["updated_at"])
    save_state(state)
    return state


def mask_token(token: object) -> str:
    value = str(token or "")
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-2:]}"


# ---------------------------------------------------------------- pid 文件
def read_pid() -> dict | None:
    data = read_json(pid_path())
    if not data:
        return None
    try:
        int(data.get("pid"))
    except (TypeError, ValueError):
        return None
    return data


def write_pid(port: int) -> dict:
    info = {"pid": os.getpid(), "port": int(port), "started_at": now_iso()}
    atomic_write_json(pid_path(), info)
    return info


def clear_pid(pid: int | None = None) -> None:
    info = read_pid()
    if not info:
        return
    if pid is not None and int(info.get("pid") or 0) != int(pid):
        return
    try:
        pid_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 子进程
def terminate_tree(target) -> None:
    """杀整棵进程树：UI 脚本可能自己再起子进程，只杀父进程会留孤儿。"""
    pid = getattr(target, "pid", target)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    else:                                    # pragma: no cover - 本机是 Windows
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        if hasattr(target, "kill"):
            target.kill()
    except Exception:                        # noqa: BLE001
        pass


def _accepts_tick(runner) -> bool:
    """runner 认不认识 `tick` 这个 kwarg。

    测试里的假 runner 只声明 `(cmd, *, timeout, cwd, env)`；硬塞 `tick` 会 TypeError。
    不吞异常、不改签名，先问清楚再传。
    """
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return False
    return "tick" in params or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())


def run_process(cmd: list[str], *, timeout: float, cwd=None, env=None,
                tick=None, tick_interval: float = 1.5) -> dict:
    """跑一个固定脚本，参数数组、shell=False；超时就杀整棵树。

    `tick` 给了就在等子进程期间每隔 `tick_interval` 调一次（心跳用）。
    **不动 communicate**：另起一个看护线程，进程结束后置 Event 让它自己退。
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # Registration is per Popen; child CLI still executes real uilock.run.
    with uilock.child_launch(root=cwd, env=env) as launch:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(list(cmd), cwd=str(cwd) if cwd else None,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, shell=False,
                                    env=launch.env, creationflags=creationflags)
        except OSError as exc:
            return {"ok": False, "timeout": False, "exit_code": None, "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                    "elapsed": round(time.monotonic() - started, 3)}
        try:
            if launch.context is not None:
                launch.bind(proc.pid)
        except BaseException:
            terminate_tree(proc)
            proc.communicate(timeout=10)
            raise
        stop_tick = threading.Event()
        watcher = None
        if tick is not None:
            def _watch():
                # 只看文件、只 _patch，绝不碰 proc（communicate 正在同一个对象上等）
                while not stop_tick.wait(tick_interval):
                    try:
                        tick()
                    except Exception:          # noqa: BLE001 - 心跳永远不许带崩
                        pass
            watcher = threading.Thread(target=_watch, name="hb-watch", daemon=True)
            watcher.start()
        try:
            out, err = proc.communicate(timeout=max(0.1, float(timeout)))
            timed_out = False
        except subprocess.TimeoutExpired:
            terminate_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            timed_out = True
        finally:
            stop_tick.set()
            if watcher is not None:
                watcher.join(0.2)
        elapsed = round(time.monotonic() - started, 3)
        return {"ok": (not timed_out) and proc.returncode == 0, "timeout": timed_out,
                "exit_code": None if timed_out else proc.returncode,
                "stdout": _decode(out), "stderr": _decode(err), "elapsed": elapsed}

# ---------------------------------------------------------------- job 引擎
class Engine:
    """job 队列 + 每端 choose→use。HTTP 层只碰这里，不直接起子进程。"""

    def __init__(self, *, token: str, port: int, root: str | Path | None = None,
                 config: dict | None = None, runner=None, python: str | None = None):
        self.root = Path(root) if root is not None else ROOT
        self.token = str(token)
        self.port = int(port)
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(config or {})
        self.allowed_origin = self._resolve_allowed_origin()
        self.runner = runner or run_process
        self.python = python or sys.executable
        self.jobs: "dict[str, dict]" = {}
        self._order: "list[str]" = []
        self._lock = threading.RLock()
        self._workflow_lock = threading.RLock()
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self.started_at = time.time()

        folder = self.root / "runs" / "jobs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # B26：上一次进程被 kill 时留下的 running/queued 必须在这里收尾，
        # 否则它们会永久僵尸（`_patch` 对磁盘 job 直接 return）。
        self._recover_interrupted()

    # ------------------------------------------------------------ 白名单
    def _resolve_allowed_origin(self) -> str:
        """配了 allowed_extension_id 就锁死成 `chrome-extension://<ID>`；没配返回空串。

        空串表示「退回前缀匹配」：只在没写 docs\\bridge_server.json 的场景出现
        （比如 CARD-09a 的临时 root），真机上 docs 里一定写了 ID。
        """
        raw = self.config.get("allowed_extension_id")
        if not str(raw or "").strip():
            return ""
        ext_id = normalize_extension_id(raw)
        if not ext_id:
            sys.stderr.write("[bridge] warn: allowed_extension_id is not a valid id; "
                             "falling back to chrome-extension:// prefix\n")
            return ""
        return EXTENSION_ORIGIN + ext_id

    def tiers(self) -> dict:
        try:
            return tier_guard.load_tiers(self.root / "docs" / "tiers.json")
        except (OSError, ValueError) as exc:
            raise RequestError(500, "tiers_unreadable", str(exc)) from exc

    def allowed_endpoints(self) -> list[str]:
        data = self.tiers()
        endpoints = data.get("endpoints")
        if not isinstance(endpoints, dict):
            raise RequestError(500, "tiers_malformed", "endpoints is not an object")
        return sorted(str(name).lower() for name in endpoints)

    def endpoints_detail(self) -> dict:
        data = self.tiers()
        endpoints = data.get("endpoints") or {}
        rows = []
        for name in sorted(endpoints):
            entry = endpoints.get(name) or {}
            dims = entry.get("dimensions") or {}
            table = {}
            for dim, block in dims.items():
                if not isinstance(block, dict):
                    continue
                table[str(dim)] = {purpose: list(block.get(purpose) or [])
                                   for purpose in PURPOSES}
            rows.append({"name": str(name), "dimensions": table})
        return {"purposes": list(PURPOSES), "endpoints": rows,
                "note": "从 docs\\tiers.json 读；空列表表示本端没登记该用途的档位"}

    # ------------------------------------------------------------ 提交
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self.jobs.values()
                       if job.get("status") in ("queued", "running"))

    def request_ask(self, payload: dict) -> tuple[dict, bool]:
        """校验 /ask 的 body 并派发。返回 (job, duplicate)。"""
        allowed = self.allowed_endpoints()

        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, list) or not endpoints:
            raise RequestError(400, "bad_endpoints", "endpoints must be a non-empty list")
        cleaned: list[str] = []
        for item in endpoints:
            name = str(item or "").strip().lower()
            if not name:
                raise RequestError(400, "bad_endpoints", "endpoint name is empty")
            if name not in allowed:
                raise RequestError(400, "endpoint_not_allowed",
                                   f"unknown endpoint: {name}; allowed: {', '.join(allowed)}")
            if name not in cleaned:
                cleaned.append(name)

        cap = int(self.config.get("max_endpoints_per_job") or 8)
        if len(cleaned) > cap:
            raise RequestError(
                400, "too_many_endpoints",
                f"一个 job 最多 {cap} 席（本次选了 {len(cleaned)}）；"
                "请分两轮：先 4 个网页端会审，再 4 个桌面端安排任务")

        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RequestError(400, "bad_prompt", "prompt must be a non-empty string")
        if len(prompt) > int(self.config["prompt_limit"]):
            raise RequestError(400, "prompt_too_long",
                               f"prompt exceeds {int(self.config['prompt_limit'])} chars")

        purpose = payload.get("purpose")
        if purpose not in PURPOSES:
            raise RequestError(400, "bad_purpose", "purpose must be one of test/review/accept")

        request_id = payload.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
                raise RequestError(400, "bad_request_id", "request_id has invalid characters")

        # B26：显式重试。默认 False，幂等语义一个字节不变；
        # 只有在调用方明确要求、且旧 job 是被重启打断的，才允许再排一次。
        return self.submit(cleaned, prompt, purpose, request_id=request_id,
                           retry=bool(payload.get("retry")))

    def submit(self, endpoints: list[str], prompt: str, purpose: str, *,
               request_id: str | None = None,
               retry: bool = False) -> tuple[dict, bool]:
        with self._lock, tier_guard.file_lock(self.root / "runs/jobs/requests.lock"):
            superseded = None
            if request_id:
                found = self._find_request(request_id)
                if found:
                    if not (retry and _retryable_after_restart(found)):
                        return found, True
                    # 显式重试：旧 job 不删，打上 superseded_by，`_find_request`
                    # 之后只认新的那条（否则同一个 request_id 会指回旧记录）。
                    superseded = found
            limit = int(self.config["queue_limit"])
            active = sum(1 for job in self.jobs.values()
                         if job.get("status") in ("queued", "running"))
            if active >= limit:
                raise QueueFull(limit)

            job_id = self._new_job_id()
            accepted_at = now_iso()
            job = {
                "job_id": job_id,
                "request_id": request_id,
                "status": "queued",
                "purpose": purpose,
                "prompt": prompt,
                "created_at": now_iso(),
                "started_at": None,
                "finished_at": None,
                "order": list(endpoints),
                "endpoints": {
                    name: {"status": "queued", "reply": None, "error": None,
                           "error_code": None, "exit_code": None,
                           "choose": None, "use": None,
                           "started_at": None, "finished_at": None, "elapsed": None,
                           # T2-1 的 L1 证据：只代表**本地后端已接收任务并落盘**。
                           # 不等于目标端已提交（L2），更不等于拿到回答（L3）。
                           # 子进程会按 env 写一份同 job_id 的链接，两边必须一致。
                           "evidence_l1": {"job_id": job_id, "request_id": request_id,
                                           "at": accepted_at, "source": "server"}}
                    for name in endpoints
                },
            }
            # Durable acceptance precedes memory visibility and any dispatch.
            self._persist(job, required=True)
            if superseded is not None:
                superseded["superseded_by"] = job_id
                try:
                    atomic_write_json(self.root / "runs" / "jobs"
                                      / f"{superseded['job_id']}.json", superseded)
                except OSError as exc:
                    sys.stderr.write(f"[bridge] supersede 写入失败: {exc}\n")
            self.jobs[job_id] = job
            self._order.append(job_id)
            self._trim()
        self._ensure_worker()
        self._queue.put(job_id)
        return copy.deepcopy(job), False

    def _new_job_id(self) -> str:
        while True:
            candidate = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
            if candidate not in self.jobs and not (self.root / "runs" / "jobs" /
                                                   f"{candidate}.json").exists():
                return candidate

    def _trim(self) -> None:
        """只清已经结束的 job；正在跑的那几个不能被挤掉（否则状态就丢了）。"""
        limit = int(self.config["memory_jobs"])
        index = 0
        while len(self._order) > limit and index < len(self._order):
            job_id = self._order[index]
            job = self.jobs.get(job_id)
            if job is None or job.get("status") in TERMINAL_JOB_STATES:
                self._order.pop(index)
                self.jobs.pop(job_id, None)
            else:
                index += 1

    def _persist(self, job: dict, *, required=False) -> None:
        try:
            atomic_write_json(self.root / "runs" / "jobs" / f"{job['job_id']}.json", job)
        except OSError as exc:
            if required:
                raise RequestError(503, "job_persistence_failed", "cannot durably accept job") from exc
            sys.stderr.write(f"[bridge] job persistence failed: {job.get('job_id')}\n")

    def _find_request(self, request_id: str) -> dict | None:
        for job in self.jobs.values():
            if job.get("request_id") == request_id and not job.get("superseded_by"):
                return copy.deepcopy(job)
        folder = self.root / "runs" / "jobs"
        try:
            # No expiry/window: every readable historical job retains its ID.
            paths = sorted(folder.glob("*.json"))
        except OSError as exc:
            raise RequestError(503, "request_history_unreadable", "cannot verify request history") from exc
        for path in paths:
            data = read_json(path)
            if not data or data.get("job_id") != path.stem:
                raise RequestError(503, "request_history_unreadable", "job history needs repair before new dispatch")
            if data.get("request_id") == request_id and not data.get("superseded_by"):
                return data
        return None

    # ------------------------------------------------------------ 重启恢复
    def _recover_interrupted(self) -> list[str]:
        """把上一次进程留下的 `queued`/`running` 标成 `failed(interrupted_by_restart)`。

        B26：`_patch` 对不在内存的 job 直接 return，`get_job` 又原样返回磁盘内容，
        所以被 kill / 断电留下的 running 会**永久僵尸**：既不推进也不结束。

        三条护栏：
          · 只碰 queued/running，终态文件一个字节不动（`test_rework09a` 会数文件数）；
          · 坏文件 / `job_id != 文件名` 只跳过并计数，不 raise（那是派发路径的规矩）；
          · 写失败只告警，不让引擎起不来。

        幂等不放宽：恢复后同 `request_id` 仍是 `duplicate=True`（见 `submit`），
        只多一个只读字段 `retryable`，客户端据此**换新的 request_id** 重提。
        """
        if not self.config.get("recover_interrupted_jobs", True):
            return []
        folder = self.root / "runs" / "jobs"
        stamp = now_iso()
        fixed: list[str] = []
        skipped = 0
        with self._lock:
            try:
                lock = tier_guard.file_lock(folder / "requests.lock")
            except OSError:
                return []
            with lock:
                try:
                    paths = sorted(folder.glob("*.json"))
                except OSError:
                    return []
                for path in paths:
                    data = read_json(path)
                    if not data or data.get("job_id") != path.stem:
                        skipped += 1
                        continue
                    if data.get("status") not in ("queued", "running"):
                        continue
                    data["status"] = "failed"
                    data["finished_at"] = stamp
                    data["error_code"] = "interrupted_by_restart"
                    data["error"] = {"code": "interrupted_by_restart",
                                     "message": "服务重启时这个 job 还没跑完，已终止",
                                     "step": "recovery"}
                    data["retryable"] = True
                    for endpoint in (data.get("endpoints") or {}).values():
                        if not isinstance(endpoint, dict):
                            continue
                        if endpoint.get("status") in TERMINAL_JOB_STATES:
                            continue
                        endpoint["status"] = "failed"
                        endpoint["finished_at"] = stamp
                        endpoint["error_code"] = "interrupted_by_restart"
                    try:
                        data["quorum"] = self._quorum(data)
                    except Exception:          # noqa: BLE001 - 证据不全就别算
                        pass
                    try:
                        atomic_write_json(path, data)
                        fixed.append(path.stem)
                    except OSError as exc:
                        sys.stderr.write(f"[bridge] recovery: {path.stem} 写入失败: {exc}\n")
        if fixed or skipped:
            sys.stderr.write(f"[bridge] recovery: {len(fixed)} job(s) marked failed"
                             f"{f', {skipped} skipped' if skipped else ''}\n")
        return fixed

    # ------------------------------------------------------------ 读 job
    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is not None:
                return copy.deepcopy(job)
        data = read_json(self.root / "runs" / "jobs" / f"{job_id}.json")
        return data if data and data.get("job_id") == job_id else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        rows: "dict[str, dict]" = {}
        with self._lock:
            for job_id in reversed(self._order):
                job = self.jobs.get(job_id)
                if job:
                    rows[job_id] = self._summary(job)
        folder = self.root / "runs" / "jobs"
        try:
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True)[:200]
        except OSError:
            paths = []
        for path in paths:
            if len(rows) >= limit:
                break
            data = read_json(path)
            if data and data.get("job_id") and data["job_id"] not in rows:
                rows[data["job_id"]] = self._summary(data)
        return list(rows.values())[:limit]

    @staticmethod
    def _summary(job: dict) -> dict:
        return {"job_id": job.get("job_id"), "status": job.get("status"),
                "purpose": job.get("purpose"), "created_at": job.get("created_at"),
                "finished_at": job.get("finished_at"),
                "endpoints": list(job.get("order") or [])}

    def wait_job(self, job_id: str, timeout: float = 10.0) -> dict | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            job = self.get_job(job_id)
            if job and job.get("status") in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(0.02)

    # ------------------------------------------------------------ 执行
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop, name="bridge-jobs",
                                            daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self._run_job(job_id)
            except Exception:                # noqa: BLE001 - 一个 job 崩了不许停worker
                self._patch(job_id, status="failed", finished_at=now_iso())

    def stop(self) -> None:
        self._stopping.set()
        self._queue.put(None)

    def _patch(self, job_id: str, endpoint: str | None = None, **fields):
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            target = job["endpoints"].get(endpoint) if endpoint else job
            if target is None:
                return None
            target.update(fields)
            self._persist(job)
            return copy.deepcopy(target)

    def _quorum(self, job: dict) -> dict:
        """回收线：`min_success` 席成功就算这一轮成立。

        缺省 `min_success=1` 时，判定逐条等价于原来的 all/any/else 三档，
        所以既有 job 测试一条都不用改。
        """
        order = list(job.get("order") or [])
        need = int(self.config.get("min_success") or 1)
        clamped = False
        if need > len(order):
            need, clamped = max(1, len(order)), True
        need = max(1, need)
        endpoints = job.get("endpoints") or {}
        done = sum(1 for name in order
                   if (endpoints.get(name) or {}).get("status") == "done")
        return {"required": need, "recovered": done,
                "met": done >= need, "clamped": clamped}

    def _refresh_quorum(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job:
            self._patch(job_id, quorum=self._quorum(job))

    @staticmethod
    def _start_delayed(func, name: str, delay: float) -> None:
        """错峰：等 `delay` 秒再跑这一席（`delay<=0` 时直接跑，与改动前一致）。"""
        if delay > 0:
            time.sleep(delay)
        func(name)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                return
            order = list(job["order"])
        self._patch(job_id, status="running", started_at=now_iso())
        self._refresh_quorum(job_id)

        def one(name: str) -> None:
            self._run_endpoint(job_id, name)
            self._refresh_quorum(job_id)

        concurrency = max(1, int(self.config.get("endpoint_concurrency") or 1))
        # 错峰启动（秒）。**默认 0**，行为与改动前一致。
        # 为什么需要：几个席同时开跑时，各自的"新建会话/抢前台"会撞在一起，
        # 后到的 choose 抢不回来（真机 2026-11-08：codex 的 choose 连续被
        # opencode 的窗口抢走前台，exit 4）。错开几秒，让"抢前台"这件事排队，
        # 之后各席的等待期仍然重叠 —— 仍然是并发，只是起跑线错开。
        stagger = min(max(0.0, float(self.config.get("endpoint_stagger") or 0)),
                      STAGGER_CEILING)
        exclusive = {str(item) for item in (self.config.get("exclusive_endpoints") or [])}
        pool = [name for name in order if name not in exclusive]
        serial = [name for name in order if name in exclusive]
        if concurrency > 1 and len(pool) > 1:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(pool)),
                                    thread_name_prefix="ep") as pool_exec:
                futures = []
                for index, name in enumerate(pool):
                    futures.append(pool_exec.submit(self._start_delayed, one, name,
                                                    index * stagger))
                for future in futures:
                    future.result()
        else:
            for name in pool:
                one(name)
        for name in serial:
            one(name)

        job = self.get_job(job_id) or {}
        statuses = [((job.get("endpoints") or {}).get(name) or {}).get("status")
                    for name in job.get("order") or []]
        quorum = self._quorum(job)
        if statuses and all(status == "done" for status in statuses):
            final = "done"
        elif quorum["met"]:
            final = "partial"
        else:
            final = "failed"
        self._patch(job_id, status=final, finished_at=now_iso(), quorum=quorum)

    def _run_endpoint(self, job_id: str, endpoint: str) -> None:
        started = time.monotonic()
        self._patch(job_id, endpoint, status="running", started_at=now_iso())
        job = self.get_job(job_id) or {}
        purpose = str(job.get("purpose") or "")
        prompt = str(job.get("prompt") or "")

        try:
            # B15：要自动拉起的端（现在只有 dsh）**在锁外**先起。
            # 它自己的 Popen 会占着整轮锁等 40s，那 40s 会把别的席全部堵住。
            if endpoint in {str(n) for n in (self.config.get("prelaunch_endpoints") or [])}:
                from . import desk
                if desk.find_app(endpoint) is None:
                    if desk.ensure_app(endpoint, wait=45.0, verbose=False) is None:
                        raise EndpointFailure("desk_launch_failed",
                                              f"{endpoint} 未运行，且 45s 内没起来",
                                              step="prelaunch")
            # B13b：并发模式下**不再整段持锁**。锁下沉到子进程的桌面动作（section），
            # 等回答期间不持锁 —— 这正是 CALIBER §3.1 要求的形态。
            # 单端（concurrency=1）仍整段持锁，行为与今天逐字节一致。
            # B26：exclusive 端即使并发也要整段持锁 —— 它要读屏幕像素，被抢前台就读错。
            exclusive_now = {str(item) for item in
                             (self.config.get("exclusive_endpoints") or [])}
            serial_ui = (int(self.config.get("endpoint_concurrency") or 1) <= 1
                         or endpoint in exclusive_now)
            lock_cm = (uilock.hold(f"server:{job_id}:{endpoint}",
                                   timeout=float(self.config["ui_lock_timeout"]),
                                   root=self.root)
                       if serial_ui else contextlib.nullcontext())
            with lock_cm:
                endpoint_budget = float(self.config["endpoint_timeout"])
                if purpose in ("review", "accept"):
                    endpoint_budget = max(endpoint_budget, float(self.config["choose_timeout"]) +
                                          tier_guard.floor_timeout(purpose, self.config["use_timeout"]) + 60)
                # B15：按端覆盖只许抬高（codex 的长思考就是靠这条拿到 300s+）
                endpoint_budget = apply_budgets(self.config, endpoint, purpose,
                                                "endpoint", endpoint_budget)
                deadline = time.monotonic() + endpoint_budget
                # F5 ③：故障注入在任何 UI 动作之前生效，命中就直接失败。
                fault = self._test_fault(endpoint)
                if fault:
                    raise EndpointFailure(fault, f"test fault injected: {fault}",
                                          step="pre_submit")
                # 声明了 require_writer 的端（chatgpt）：先只读地问一次"现在哪个模式"，
                # 再按那套表取目标值。读不到就用默认表（与改动前一致）。
                writer = (self._detect_writer(endpoint, deadline)
                          if self._requires_writer(endpoint) else None)
                pairs = self._pick_targets(endpoint, purpose, writer=writer)
                dimension, target = pairs[0]
                choose = self._run_choose(endpoint, dimension, target, deadline,
                                          targets=pairs, purpose=purpose)
                if writer:
                    choose["writer"] = writer
                self._patch(job_id, endpoint, choose=choose)
                if not choose.get("ok"):
                    raise EndpointFailure(str(choose.get("error_code") or "choose_failed"),
                                          str(choose.get("error_message") or "choose failed"),
                                          exit_code=choose.get("exit_code"), step="choose")

                use = self._run_use(endpoint, prompt, purpose, deadline, job_id=job_id)
                self._patch(job_id, endpoint, use=use, exit_code=use.get("exit_code"),
                            reply=use.get("reply"))
                if not use.get("ok"):
                    raise EndpointFailure(str(use.get("error_code") or "use_failed"),
                                          str(use.get("error_message") or "use failed"),
                                          exit_code=use.get("exit_code"), step="use")
        except uilock.Busy as exc:
            self._fail(job_id, endpoint, "ui_busy", str(exc), started=started)
        except EndpointFailure as exc:
            self._fail(job_id, endpoint, exc.code, exc.message, exit_code=exc.exit_code,
                       started=started, step=exc.step)
        except Exception as exc:             # noqa: BLE001 - 单端异常必须被隔离
            self._fail(job_id, endpoint, "internal_error",
                       f"{type(exc).__name__}: {exc}", started=started)
        else:
            self._patch(job_id, endpoint, status="done", error=None, error_code=None,
                        finished_at=now_iso(),
                        elapsed=round(time.monotonic() - started, 2))

    def _fail(self, job_id: str, endpoint: str, code: str, message: str, *,
              exit_code=None, started: float | None = None, step: str | None = None) -> None:
        self._patch(job_id, endpoint, status="failed", error_code=code,
                    error={"code": code, "message": truncate(message, 2000),
                           "step": step, "exit_code": exit_code},
                    exit_code=exit_code, finished_at=now_iso(),
                    elapsed=None if started is None else round(time.monotonic() - started, 2))

    # ------------------------------------------------------------ 一个端
    def _test_fault(self, endpoint: str) -> str | None:
        """F5 场景③的故障注入。**双条件**才生效，缺一不可：

        1. `docs/bridge_server.json` 里 `allow_test_faults: true`（默认 false）
        2. `runs/test-faults.json` 存在且 `{"endpoint": ..., "fault": ...}` 命中本端

        生效时在**任何 UI 动作之前**直接失败，所以既能复现"找不到输入框"这类故障，
        又不会真的去点桌面。生产配置下第 1 条永远不成立。

        做法是"在提交前抛 EndpointFailure"，不是去改 UI 检测逻辑——
        后者等于为了测试给生产代码开后门。
        """
        if not self.config.get("allow_test_faults"):
            return None
        path = self.root / "runs" / "test-faults.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if str(data.get("endpoint") or "") != endpoint:
            return None
        fault = str(data.get("fault") or "").strip()
        return fault or None

    def _requires_writer(self, endpoint: str) -> bool:
        try:
            tiers = tier_guard.load_tiers(self.root / "docs" / "tiers.json")
        except (OSError, ValueError):
            return False
        entry = (tiers.get("endpoints") or {}).get(endpoint) or {}
        return bool(entry.get("require_writer"))

    def _detect_writer(self, endpoint: str, deadline: float) -> str | None:
        """读一次"这个端当前在哪个写作模式"（chat / work）。读不到返回 None。

        为什么必须先读：chatgpt 的两套档位表**完全不同**（work 模式 review 是
        `gpt61_sol + max`，chat 模式只有 `gpt6 + high`）。拿错表去设，
        闸门会判 `purpose_mismatch`。`--list` 是只读的，只读芯片不点任何东西。
        """
        script = self.root / "src" / "scripts" / f"{endpoint}_choose.py"
        if not script.exists():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        timeout = min(20.0, remaining)
        env = {**os.environ,
               "BRIDGE_UI_LOCK_TIMEOUT": str(max(1, int(timeout) - 2))}
        if int(self.config.get("endpoint_concurrency") or 1) > 1:
            env["BRIDGE_UI_CONCURRENT"] = "1"
            env["BRIDGE_WEB_WINDOW_MODE"] = str(
                self.config.get("web_window_mode") or "per_site")
            # B15：按端的 busy 停滞判据（codex 的停止键常驻，busy 恒真）。
            stall = (self.config.get("stall_busy_ignore") or {}).get(str(endpoint).lower())
            if stall:
                env["BRIDGE_STALL_BUSY"] = str(float(stall))
        result = self.runner([self.python, str(script), "--list"],
                             timeout=timeout, cwd=self.root, env=env)
        parsed = parse_last_json(result.get("stdout") or "") or {}
        writer = str(parsed.get("writer") or "").lower()
        return writer if writer in ("chat", "work") else None

    def _pick_targets(self, endpoint: str, purpose: str,
                      *, writer: str | None = None) -> list[tuple[str, str]]:
        """该端这次要设的**全部**维度：[(维度, 目标值)]，第一个是主维度。

        为什么不是一个：`tiers.json` 里 chatgpt 声明了
        `required_dimensions: ["model","effort"]` + `require_writer`，
        闸门要求两张维度都在同一张联合回执里（`composite_version=1`）且都读回一致。
        只设 model 时 `chatgpt_choose.py` 会拿"页面上当时那个 effort"去补，
        于是能不能过完全取决于页面当时的档 —— 这不是稳定行为（2026-11-08 核实：
        正式目录 `required_dimensions` 是 None，所以那时只设 model 也能过）。
        """
        try:
            tiers = tier_guard.load_tiers(self.root / "docs" / "tiers.json")
        except (OSError, ValueError) as exc:
            raise EndpointFailure("tiers_unreadable", str(exc)) from exc
        endpoints = tiers.get("endpoints") or {}
        entry = endpoints.get(endpoint) or {}
        dims = entry.get("dimensions") or {}
        if not isinstance(dims, dict):
            raise EndpointFailure("tier_table_malformed", f"{endpoint}: dimensions is not an object")

        required = [str(item) for item in (entry.get("required_dimensions") or [])]
        wanted = required or list(DIMENSION_ORDER)
        picked: list[tuple[str, str]] = []
        for dimension in wanted:
            if required and dimension not in DIMENSION_ORDER:
                continue
            block = dims.get(dimension)
            if not isinstance(block, dict):
                if required:
                    raise EndpointFailure("tier_table_unconfigured",
                                          f"{endpoint}/{dimension} 是 required 维度但表里没有")
                continue
            # 有 writer 分表（chatgpt 的 chat / work 两套）且知道当前模式 → 用那一套。
            # 不知道模式就用默认表（work），行为与改动前一致。
            table = block
            writers = block.get("writers")
            if writer and isinstance(writers, dict) and isinstance(writers.get(writer), dict):
                table = writers[writer]
            values = table.get(purpose)
            if not (isinstance(values, list) and values):
                # 声明了 required 就 **fail-closed**：少一维等于联合回执永远凑不齐，
                # 与其让 use 在闸门那里报 composite_missing，不如这里就说清楚。
                if required:
                    raise EndpointFailure(
                        "tier_table_empty",
                        f"{endpoint}/{dimension} has no {purpose} tier")
                if isinstance(values, list):
                    raise EndpointFailure(
                        "tier_table_empty", f"{endpoint}/{dimension} has no {purpose} tier")
                continue
            picked.append((dimension, str(values[0])))
            if not required:
                return picked          # 未声明 required：维持旧行为，只设第一个
        if not picked:
            raise EndpointFailure("tier_table_unconfigured",
                                  f"{endpoint} has no usable dimension for purpose={purpose}")
        return picked

    def _pick_target(self, endpoint: str, purpose: str) -> tuple[str, str]:
        """主维度（向后兼容的老接口）。"""
        return self._pick_targets(endpoint, purpose)[0]

    def _run_choose(self, endpoint: str, dimension: str, target: str,
                    deadline: float, *, targets: list[tuple[str, str]] | None = None,
                    purpose: str = "") -> dict:
        script = self.root / "src" / "scripts" / f"{endpoint}_choose.py"
        pairs = list(targets or [(dimension, target)])
        out = {"ok": False, "dimension": dimension, "target": target, "readback": None,
               # 联合维度（chatgpt 这种 required_dimensions 有两维的，这里是全部）
               "targets": {dim: value for dim, value in pairs},
               "confirmed": False, "exit_code": None, "timeout": False,
               "elapsed": 0.0, "error_code": None, "error_message": None, "stderr": ""}
        if not script.exists():
            out.update(error_code="script_missing",
                       error_message=f"choose script not found: {script}")
            return out

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            out.update(error_code="endpoint_timeout", error_message="endpoint timeout before choose")
            return out
        choose_budget = apply_budgets(self.config, endpoint, purpose,
                                      "choose", float(self.config["choose_timeout"]))
        timeout = min(choose_budget, remaining)
        # 一维端拼一个 flag；`required_dimensions` 有两维的端拼两个
        # （`chatgpt_choose.py` 支持 `--model X --effort Y` 一次设完，
        #  也只有一次设完才会写 composite 回执 —— 这是闸门放行的前提）。
        cmd = [self.python, str(script)]
        for dim, value in pairs:
            cmd.extend([DIMENSION_FLAGS[dim], value])
        for extra in (self.config.get("choose_args") or {}).get(str(endpoint).lower(), []):
            cmd.append(str(extra))
        started_utc = datetime.now(timezone.utc)
        # B14：choose 子进程**必须整轮持锁**（它要点菜单，不能和别人的写入交错），
        # 所以这里**不下发** SECTIONED/WAKE/CONCURRENT。但 `uilock.entry` 的默认
        # 等锁上限只有 60s（`DEFAULT_TIMEOUT`）：4 席并发时后到的 choose 要等前几个
        # 跑完，60s 不够就变成退出码 8（ui_busy）。所以只把"等锁上限"对齐到它自己的
        # 预算（留 2s 余量，让它能先报 Busy 而不是被父进程直接砍掉）。
        env = {**os.environ,
               "BRIDGE_UI_LOCK_TIMEOUT": str(max(1, int(timeout) - 2))}
        if int(self.config.get("endpoint_concurrency") or 1) > 1:
            # choose **只**拿这个"前台是共享的"标记：它仍然整轮持锁（要点菜单），
            # 但点之前要能把自己的窗口抢回来，否则网页端一开窗就把它打断。
            env["BRIDGE_UI_CONCURRENT"] = "1"
            env["BRIDGE_WEB_WINDOW_MODE"] = str(
                self.config.get("web_window_mode") or "per_site")
        result = self.runner(cmd, timeout=timeout, cwd=self.root, env=env)
        out.update({"exit_code": result.get("exit_code"),
                    "timeout": bool(result.get("timeout")),
                    "elapsed": result.get("elapsed"),
                    "stderr": truncate(result.get("stderr"), 2000)})
        if result.get("timeout"):
            out.update(error_code="choose_timeout",
                       error_message=f"choose timed out after {timeout:.0f}s")
            return out
        if result.get("exit_code") != 0:
            # B16：hint 取不到时把 stdout 末几行带出来。dsh_choose 以前在
            # `menu_not_open` 分支只 return 1、什么都不打印，面板就只剩
            # `choose exit code 1`，连"窗口没在前台"都看不出来。
            stdout = result.get("stdout") or ""
            hint = (parse_last_json(stdout) or {}).get("hint")
            tail = " | ".join(line.strip() for line in stdout.splitlines()
                              if line.strip())[-400:]
            bare = f"choose exit code {result.get('exit_code')}"
            out.update(error_code="choose_failed",
                       error_message=hint or truncate(result.get("stderr"), 500)
                       or (f"{bare}; stdout: {tail}" if tail else bare))
            return out

        receipt = tier_guard.read_receipt(endpoint, root=self.root)
        if not receipt:
            out.update(error_code="receipt_missing",
                       error_message=f"runs/tier/{endpoint}.json not found after choose")
            return out
        if receipt.get("schema_version") != tier_guard.SCHEMA_VERSION:
            out.update(error_code="receipt_invalid", error_message="receipt schema mismatch")
            return out
        if str(receipt.get("endpoint") or "").lower() != endpoint:
            out.update(error_code="receipt_invalid", error_message="receipt endpoint mismatch")
            return out
        out["readback"] = receipt.get("readback")
        if receipt.get("consumed"):
            out.update(error_code="receipt_consumed", error_message="receipt already consumed")
            return out
        if str(receipt.get("target")) != target or str(receipt.get("readback")) != target:
            out.update(error_code="readback_mismatch",
                       error_message=(f"wanted {target}, receipt target={receipt.get('target')} "
                                      f"readback={receipt.get('readback')}"))
            return out
        created = tier_guard.parse_utc(receipt.get("created_at"))
        if created is None:
            out.update(error_code="receipt_invalid", error_message="receipt timestamp unreadable")
            return out
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if created < started_utc.replace(microsecond=0) - timedelta(seconds=5):
            out.update(error_code="receipt_stale", error_message="receipt predates this choose run")
            return out
        if age > tier_guard.TTL_SECONDS:
            out.update(error_code="receipt_stale",
                       error_message=f"receipt is {age:.0f}s old (ttl={tier_guard.TTL_SECONDS:.0f}s)")
            return out

        out.update(ok=True, confirmed=True)
        return out

    def _run_use(self, endpoint: str, prompt: str, purpose: str, deadline: float,
                 job_id: str | None = None) -> dict:
        script = self.root / "src" / "scripts" / f"{endpoint}_use.py"
        out = {"ok": False, "stage": None, "reply": None, "exit_code": None,
               "timeout": False, "elapsed": 0.0, "error_code": None,
               "error_message": None, "stderr": ""}
        if not script.exists():
            out.update(error_code="script_missing",
                       error_message=f"use script not found: {script}")
            return out

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            out.update(error_code="endpoint_timeout", error_message="endpoint timeout before use")
            return out
        reply_budget = apply_budgets(
            self.config, endpoint, purpose, "use",
            tier_guard.floor_timeout(purpose, float(self.config["use_timeout"])))
        overhead = 30.0 if purpose in ("review", "accept") else 0.0
        if purpose in ("review", "accept") and remaining < reply_budget + overhead:
            out.update(error_code="endpoint_timeout", error_message="insufficient outer reply budget")
            return out
        timeout = min(reply_budget + overhead, remaining)
        cmd = [self.python, str(script), prompt, "--purpose", purpose,
               "--timeout", str(max(1, int(min(reply_budget, timeout))))]
        # T2-1：把 job 身份下发给子进程，子进程据此写 L1 证据（`source="env"`）。
        # child_launch 的 env 是**整份替换**，所以必须带上 os.environ。
        record = self.get_job(job_id) if job_id else None
        env = {**os.environ,
               "BRIDGE_JOB_ID": str(job_id or ""),
               "BRIDGE_REQUEST_ID": str((record or {}).get("request_id") or ""),
               "BRIDGE_PURPOSE": purpose,
               "BRIDGE_ENDPOINT": endpoint}
        if int(self.config.get("endpoint_concurrency") or 1) > 1:
            # 三个开关一起下发：分段持锁 / 不抢前台 / 一端一窗
            # B26：exclusive 端由父进程整段持锁，子进程**不能**再按 section 抢锁，
            # 否则两套锁模型叠着，只靠委派链的巧合才不冲突。
            if endpoint not in {str(item) for item in
                                (self.config.get("exclusive_endpoints") or [])}:
                env["BRIDGE_UI_SECTIONED"] = "1"
            env["BRIDGE_UI_WAKE"] = "0"
            env["BRIDGE_UI_CONCURRENT"] = "1"
            # B15：网页端开窗形态（tabs = 一窗多标签，per_site = 一端一窗）。
            env["BRIDGE_WEB_WINDOW_MODE"] = str(
                self.config.get("web_window_mode") or "per_site")
            # B16：按端 busy 停滞判据。B15 只在 `_detect_writer`（choose --list）
            # 下发过它，`_run_use` 漏了 —— 结果 `stall_busy_ignore` 配了等于没配，
            # codex 的 use 从来没拿到 BRIDGE_STALL_BUSY。
            try:
                stall = float((self.config.get("stall_busy_ignore") or {})
                              .get(str(endpoint).lower()) or 0)
            except (TypeError, ValueError):
                stall = 0.0
            if stall > 0:
                env["BRIDGE_STALL_BUSY"] = str(stall)

        def _tick():
            # B11：子进程写心跳，这里把它搬进 job 记录（面板只读 /jobs/<id>）
            data = heartbeat.read(job_id, endpoint, root=self.root) if job_id else None
            if data:
                self._patch(job_id, endpoint, progress=data)

        kwargs = {"timeout": timeout, "cwd": self.root, "env": env}
        if job_id and _accepts_tick(self.runner):
            self._patch(job_id, endpoint, progress={"stage": "dispatching"})
            kwargs["tick"] = _tick
        result = self.runner(cmd, **kwargs)
        if job_id:
            # 留最后一帧当排障证据，然后删文件（下次同 job 不会读到旧帧）
            last = heartbeat.read(job_id, endpoint, root=self.root)
            if last:
                self._patch(job_id, endpoint,
                            progress={**last, "stage": "finished"})
            try:
                heartbeat.path_for(job_id, endpoint, self.root).unlink()
            except OSError:
                pass
        parsed = parse_last_json(result.get("stdout") or "")
        out.update({"exit_code": result.get("exit_code"),
                    "timeout": bool(result.get("timeout")),
                    "elapsed": result.get("elapsed"),
                    "stderr": truncate(result.get("stderr"), 2000),
                    "stage": (parsed or {}).get("stage")})
        if result.get("timeout"):
            out.update(error_code="use_timeout",
                       error_message=f"use timed out after {timeout:.0f}s; process tree killed")
            return out
        if parsed is None:
            out.update(error_code="bad_output",
                       error_message=truncate(result.get("stderr"), 500)
                       or "no JSON object on stdout")
            return out
        reply = parsed.get("reply")
        out["reply"] = truncate(reply, int(self.config["output_limit"])) if reply else None
        # B11：把子进程的细因带上来。原来只取 stage，`timeout_kind=stalled` 与
        # `error_code=REPLY_NEVER_APPEARED` 全被丢掉，面板只能显示"等回复超时"。
        detail = {key: parsed.get(key) for key in
                  ("timeout_kind", "error_code", "stall_reason", "stall_reads",
                   "reason", "warnings", "salvaged", "settle_basis", "prepare",
                   # B17：dsh 的 UIA 文本兜底必须把来源与截图带进 job 证据
                   "source", "basis", "screenshot",
                   # B26：prompt_gone 的现场（当时有几条回复动作条 / 提问 / 读到第几次）
                   "misses", "copies", "prompts",
                   # B26：判据未开的端也留下"看到了什么"，攒够证据才能加 PROFILES
                   "reject_facts")
                  if parsed.get(key) is not None}
        if detail:
            out["detail"] = detail
        if result.get("exit_code") != 0 or not parsed.get("ok"):
            out.update(error_code=str(parsed.get("stage") or "use_failed"),
                       error_message=truncate(parsed.get("hint") or parsed.get("error")
                                              or f"use exit code {result.get('exit_code')}", 1000))
            return out
        out["ok"] = True
        return out

    # ------------------------------------------------------------ 工作流
    def workflow_path(self, name: str) -> Path:
        if name not in WORKFLOW_FILES:
            raise RequestError(400, "bad_file",
                               f"file must be one of {', '.join(WORKFLOW_FILES)}")
        return self.root / name

    def workflow_read(self, name: str) -> dict:
        path = self.workflow_path(name)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"file": name, "exists": False, "data": {}, "mtime": None}
        except OSError as exc:
            raise RequestError(500, "workflow_unreadable", str(exc)) from exc
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RequestError(500, "workflow_bad_json", str(exc)) from exc
        if not isinstance(data, dict):
            raise RequestError(500, "workflow_bad_json", "top level is not an object")
        return {"file": name, "exists": True, "data": data,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                .isoformat(timespec="seconds")}

    def _transitions(self) -> dict:
        path = self.root / "docs" / "workflow" / "workflow.json"
        data = read_json(path)
        transitions = (data or {}).get("transitions")
        if not isinstance(transitions, dict):
            raise RequestError(500, "workflow_config_error",
                               f"cannot read transitions from {path}")
        return transitions

    def workflow_move(self, payload: dict) -> dict:
        # 局部导入：避免模块级循环依赖（workflow 也依赖本模块的常量）。
        from . import workflow as wf

        name = str(payload.get("file") or "progress.json")
        path = self.workflow_path(name)

        card = str(payload.get("card") or "").strip()
        if not card or not CARD_RE.match(card):
            raise RequestError(400, "bad_card", "card has invalid characters")
        target = str(payload.get("to") or "").strip()
        if not target:
            raise RequestError(400, "bad_state", "to is required")
        create = bool(payload.get("create"))

        transitions = self._transitions()
        known = set(transitions)
        for targets in transitions.values():
            known.update(str(item) for item in (targets or []))
        if target not in known:
            raise RequestError(400, "bad_state", f"unknown state: {target}")

        with self._workflow_lock, progress.locked(file_path=path):
            last_error: RequestError | None = None
            for _ in range(3):               # 写后复核；被外部写入盖掉就重试
                try:
                    data = self.workflow_read(name)["data"]
                except RequestError:
                    raise
                node = data.get("workflow")
                if not isinstance(node, dict):
                    node = {}
                cards = node.get("cards")
                if not isinstance(cards, dict):
                    cards = {}
                record = cards.get(card)
                now = now_iso()

                if record is not None and not isinstance(record, dict):
                    raise RequestError(500, "workflow_bad_json",
                                       f"workflow.cards.{card} is not an object")
                if record is None:
                    if not (create and target == "draft"):
                        raise RequestError(404, "card_not_found", f"card not in {name}: {card}")
                    record = {"state": "draft", "no_progress": 0, "parent": None,
                              "missing": [], "history": [],
                              "created_at": now}
                    cards[card] = record
                    node["cards"] = cards
                    data["workflow"] = node
                    wf.sync_top(data, card, "draft", now)
                    atomic_write_json(path, data)
                    return {"file": name, "card": card, "from": None, "to": "draft",
                            "created": True, "state": "draft"}

                current = str(record.get("state") or "")
                if current == target:
                    raise RequestError(409, "illegal_transition",
                                       f"{card} is already {target}")
                allowed = [str(x) for x in (transitions.get(current) or [])]
                if target not in allowed:
                    raise RequestError(409, "illegal_transition",
                                       f"{card}: {current} -> {target} not allowed "
                                       f"(allowed: {allowed or 'none'})")

                # B25：证据不变量与 CLI 共用同一份判据。以前这里无条件
                # `missing = []`，等于把 hold 的证据链和 block 的修复卡一起抹掉，
                # 而且事后分不清"本来没缺"与"被 HTTP 清空"。
                try:
                    items = wf.check_move(record, target, payload.get("missing"))
                except wf.MoveRejected as exc:
                    raise RequestError(422, "missing_required", str(exc)) from exc

                updated = dict(record)
                updated["state"] = target
                updated["no_progress"] = 0
                updated["missing"] = items if target == wf.HOLD else []
                repair = None
                if target == wf.BLOCK:
                    repair = wf.next_repair_id(cards, card)
                    cards[repair] = {"state": wf.SENT, "no_progress": 0, "parent": card,
                                     "missing": [], "history": []}
                history = record.get("history")
                updated["history"] = (list(history) if isinstance(history, list) else []) + [
                    {"from": current, "to": target, "at": now}]
                updated["updated_at"] = now
                cards[card] = updated
                node["cards"] = cards
                data["workflow"] = node
                # B26：顶层 status 与卡片一起改，否则 autopush 还在推已结束的卡。
                wf.sync_top(data, card, target, now)
                atomic_write_json(path, data)

                check = self.workflow_read(name)["data"]
                got = (((check.get("workflow") or {}).get("cards") or {})
                       .get(card) or {}).get("state")
                if got == target:
                    return {"file": name, "card": card, "from": current, "to": target,
                            "created": False, "state": target, "repair_card": repair}
                last_error = RequestError(500, "write_conflict",
                                          "concurrent writer overwrote the transition")
            raise last_error or RequestError(500, "write_conflict", "workflow write failed")


# ---------------------------------------------------------------- HTTP
class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "BridgeServer/0.0.9"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础设施
    @property
    def engine(self) -> Engine:
        return self.server.engine                  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):             # noqa: D401 - 只记方法+路径，不记头
        path = urlsplit(getattr(self, "path", "") or "").path
        sys.stderr.write(f"[bridge] {self.command} {path} {args[1] if len(args) > 1 else ''}\n")

    def _origin(self) -> str:
        return (self.headers.get("Origin") or "").strip()

    @staticmethod
    def _origin_allowed(origin: str, allowed_origin: str) -> bool:
        """Origin 判定（CARD-09b 第 5 条，合并三家会审意见）。

        - 有 Origin：必须**精确等于** `chrome-extension://<固定ID>`，前缀相同也不行；
        - 没 Origin：放行（扩展的 GET 不保证带 Origin），但后面照样要 Bearer token；
        - 没配 allowed_extension_id：退回 09a 的前缀规则，只认 chrome-extension://。
        """
        if not origin:
            return True
        current = origin.strip().lower()                 # 精确比较，连结尾的 / 都不通融
        if allowed_origin:
            return current == allowed_origin
        return current.startswith(EXTENSION_ORIGIN)

    def _send_json(self, status: int, payload: dict,
                   extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._origin()
        if origin and self._origin_allowed(origin, self.engine.allowed_origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_empty(self, status: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        origin = self._origin()
        if origin and self._origin_allowed(origin, self.engine.allowed_origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _error(self, status: int, code: str, message: str) -> None:
        extra = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        self._send_json(status, {"ok": False, "error": code, "message": message}, extra)

    def _guard(self) -> bool:
        """Host / Origin / token 三件事都在这里判。返回 False 表示已经回过错。"""
        host = (self.headers.get("Host") or "").strip().lower()
        expected = f"{HOST}:{self.engine.port}"
        if host != expected:
            self._error(403, "bad_host", f"Host must be {expected}")
            return False
        origin = self._origin()
        if not self._origin_allowed(origin, self.engine.allowed_origin):
            expected_origin = self.engine.allowed_origin or f"{EXTENSION_ORIGIN}<extension-id>"
            self._error(403, "bad_origin", f"Origin must be {expected_origin} or absent")
            return False
        query = parse_qs(urlsplit(self.path).query)
        if "token" in query:
            self._error(400, "token_in_url", "token must not be passed in the URL")
            return False
        return True

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            self._error(401, "unauthorized", "Authorization: Bearer <token> is required")
            return False
        given = header[7:].strip()
        # 恒定时间比较；先编码成 bytes，免得非 ASCII 的头让 compare_digest 抛 TypeError（→500）。
        if not hmac.compare_digest(given.encode("utf-8", "replace"),
                                   self.engine.token.encode("utf-8", "replace")):
            self._error(401, "unauthorized", "invalid token")
            return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise RequestError(400, "bad_length", "invalid Content-Length")
        if length <= 0:
            raise RequestError(400, "empty_body", "JSON body is required")
        if length > MAX_BODY:
            raise RequestError(413, "body_too_large", f"body exceeds {MAX_BODY} bytes")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "bad_json", "body is not valid JSON")
        if not isinstance(data, dict):
            raise RequestError(400, "bad_json", "body must be a JSON object")
        return data

    # ------------------------------------------------------------ OPTIONS
    def do_OPTIONS(self) -> None:                  # noqa: N802 - BaseHTTPRequestHandler
        if not self._guard():
            return
        self._send_empty(204, {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
        })

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:                      # noqa: N802
        if not self._guard():
            return
        parts = urlsplit(self.path)
        route = parts.path

        if route == "/health":
            # Public health contract: exactly ok + version.
            self._send_json(200, {
                "ok": True, "version": "0.0.9",
            })
            return

        if not self._authorized():
            return

        try:
            self._route_get(route, parts)
        except RequestError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception as exc:                   # noqa: BLE001 - 兜底，不让线程裸崩
            self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")

    def _route_get(self, route: str, parts) -> None:
        if route == "/endpoints":
            self._send_json(200, {"ok": True, **self.engine.endpoints_detail()})
            return

        if route == "/jobs":
            jobs = self.engine.list_jobs()
            self._send_json(200, {"ok": True, "count": len(jobs), "jobs": jobs})
            return

        if route.startswith("/jobs/"):
            job_id = unquote(route[len("/jobs/"):]).strip("/")
            if not JOB_ID_RE.match(job_id):
                self._error(400, "bad_job_id", "invalid job id")
                return
            job = self.engine.get_job(job_id)
            if job is None:
                self._error(404, "job_not_found", f"no such job: {job_id}")
                return
            self._send_json(200, {"ok": True, "job": job})
            return

        if route == "/workflow":
            name = (parse_qs(parts.query).get("file") or ["progress.json"])[0]
            self._send_json(200, {"ok": True, **self.engine.workflow_read(name)})
            return

        self._error(404, "not_found", f"no route: {route}")

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:                     # noqa: N802
        if not self._guard():
            return
        if not self._authorized():
            return
        route = urlsplit(self.path).path
        try:
            payload = self._body()
            if route == "/ask":
                job, duplicate = self.engine.request_ask(payload)
                self._send_json(202, {"ok": True, "job_id": job["job_id"],
                                      "status": job["status"], "duplicate": duplicate,
                                      "created_at": job["created_at"]})
                return
            if route == "/workflow":
                result = self.engine.workflow_move(payload)
                self._send_json(200, {"ok": True, **result})
                return
            if route == "/shutdown":
                if self.engine.active_count():
                    self._error(409, "job_running",
                                "a job is running; retry later or stop with --force")
                    return
                self._send_json(200, {"ok": True, "action": "shutdown"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._error(404, "not_found", f"no route: {route}")
        except RequestError as exc:
            self._error(exc.status, exc.code, exc.message)
        except Exception as exc:                   # noqa: BLE001 - 兜底，不让线程裸崩
            self._error(500, "internal_error", f"{type(exc).__name__}: {exc}")


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False        # Windows 上不要 SO_REUSEADDR，占用的端口必须抢不到

    def __init__(self, address, handler, engine: Engine):
        self.engine = engine
        super().__init__(address, handler)


def create_server(port: int, engine: Engine) -> BridgeHTTPServer:
    """建服务；端口被占直接把 OSError 抛出去（调用方按退出码 1 处理）。"""
    httpd = BridgeHTTPServer((HOST, int(port)), BridgeHandler, engine)
    engine.port = httpd.server_address[1]
    return httpd


def serve(port: int | None = None, *, root: str | Path | None = None) -> int:
    """前台跑服务：给 bridge_server.py start --foreground 和子进程用。"""
    config = load_config(root)
    listen = int(port or config["port"])
    state = load_or_create_state(port=listen)
    engine = Engine(root=root, token=state["token"], port=listen, config=config)
    httpd = create_server(listen, engine)

    state["port"] = engine.port
    save_state(state)
    write_pid(engine.port)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass                                  # 前台 Ctrl+C 是正常收工，不打 traceback
    finally:
        engine.stop()
        httpd.server_close()
        clear_pid(os.getpid())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qi.server", description="Bridge local server")
    parser.add_argument("--serve", action="store_true", help="run in foreground")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.serve:
        parser.print_help()
        return 2
    try:
        return serve(args.port)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "bind_failed", "detail": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
