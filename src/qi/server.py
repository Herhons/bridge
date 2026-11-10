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
非法迁移 409；写盘用同目录临时文件 + `os.replace`，并在进程内串行 + 写后复核重试，
和 `notify_done.py` 抢着写时不会留下半个 JSON。
"""
from __future__ import annotations

import argparse
import copy
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import tier_guard, uilock

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
    "use_timeout": 180.0,
    "endpoint_timeout": 420.0,
    "ui_lock_timeout": 600.0,
    "output_limit": 65536,
    "prompt_limit": 20000,
    "allowed_extension_id": "",     # 空 = 没配，Origin 退回前缀匹配（CARD-09b 兼容 09a）
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
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("[bridge] warn: icacls unavailable (%s); ACL unchanged\n"
                         % type(exc).__name__)
        return False
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip().splitlines()
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


def run_process(cmd: list[str], *, timeout: float, cwd=None, env=None) -> dict:
    """跑一个固定脚本，参数数组、shell=False；超时就杀整棵树。"""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(list(cmd), cwd=str(cwd) if cwd else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, shell=False,
                                env=env, creationflags=creationflags)
    except OSError as exc:
        return {"ok": False, "timeout": False, "exit_code": None, "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.monotonic() - started, 3)}
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

        return self.submit(cleaned, prompt, purpose, request_id=request_id)

    def submit(self, endpoints: list[str], prompt: str, purpose: str, *,
               request_id: str | None = None) -> tuple[dict, bool]:
        with self._lock:
            if request_id:
                found = self._find_request(request_id)
                if found:
                    return found, True
            limit = int(self.config["queue_limit"])
            active = sum(1 for job in self.jobs.values()
                         if job.get("status") in ("queued", "running"))
            if active >= limit:
                raise QueueFull(limit)

            job_id = self._new_job_id()
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
                           "started_at": None, "finished_at": None, "elapsed": None}
                    for name in endpoints
                },
            }
            self.jobs[job_id] = job
            self._order.append(job_id)
            self._trim()
            self._persist(job)
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

    def _persist(self, job: dict) -> None:
        try:
            atomic_write_json(self.root / "runs" / "jobs" / f"{job['job_id']}.json", job)
        except OSError:
            pass                              # 落盘失败不改内存里的结果

    def _find_request(self, request_id: str) -> dict | None:
        for job in self.jobs.values():
            if job.get("request_id") == request_id:
                return copy.deepcopy(job)
        folder = self.root / "runs" / "jobs"
        try:
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True)[:200]
        except OSError:
            return None
        for path in paths:
            data = read_json(path)
            if data and data.get("request_id") == request_id:
                return data
        return None

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

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                return
            order = list(job["order"])
        self._patch(job_id, status="running", started_at=now_iso())
        for endpoint in order:
            self._run_endpoint(job_id, endpoint)

        job = self.get_job(job_id) or {}
        statuses = [((job.get("endpoints") or {}).get(name) or {}).get("status")
                    for name in job.get("order") or []]
        if statuses and all(status == "done" for status in statuses):
            final = "done"
        elif any(status == "done" for status in statuses):
            final = "partial"
        else:
            final = "failed"
        self._patch(job_id, status=final, finished_at=now_iso())

    def _run_endpoint(self, job_id: str, endpoint: str) -> None:
        started = time.monotonic()
        self._patch(job_id, endpoint, status="running", started_at=now_iso())
        job = self.get_job(job_id) or {}
        purpose = str(job.get("purpose") or "")
        prompt = str(job.get("prompt") or "")

        try:
            with uilock.hold(f"server:{job_id}:{endpoint}",
                             timeout=float(self.config["ui_lock_timeout"]),
                             root=self.root):
                deadline = time.monotonic() + float(self.config["endpoint_timeout"])
                dimension, target = self._pick_target(endpoint, purpose)
                choose = self._run_choose(endpoint, dimension, target, deadline)
                self._patch(job_id, endpoint, choose=choose)
                if not choose.get("ok"):
                    raise EndpointFailure(str(choose.get("error_code") or "choose_failed"),
                                          str(choose.get("error_message") or "choose failed"),
                                          exit_code=choose.get("exit_code"), step="choose")

                use = self._run_use(endpoint, prompt, purpose, deadline)
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
    def _pick_target(self, endpoint: str, purpose: str) -> tuple[str, str]:
        try:
            tiers = tier_guard.load_tiers(self.root / "docs" / "tiers.json")
        except (OSError, ValueError) as exc:
            raise EndpointFailure("tiers_unreadable", str(exc)) from exc
        endpoints = tiers.get("endpoints") or {}
        entry = endpoints.get(endpoint) or {}
        dims = entry.get("dimensions") or {}
        if not isinstance(dims, dict):
            raise EndpointFailure("tier_table_malformed", f"{endpoint}: dimensions is not an object")
        for dimension in DIMENSION_ORDER:
            block = dims.get(dimension)
            if not isinstance(block, dict):
                continue
            values = block.get(purpose)
            if isinstance(values, list) and values:
                return dimension, str(values[0])
            if isinstance(values, list):
                raise EndpointFailure("tier_table_empty",
                                      f"{endpoint}/{dimension} has no {purpose} tier")
        raise EndpointFailure("tier_table_unconfigured",
                              f"{endpoint} has no usable dimension for purpose={purpose}")

    def _run_choose(self, endpoint: str, dimension: str, target: str,
                    deadline: float) -> dict:
        script = self.root / "src" / "scripts" / f"{endpoint}_choose.py"
        out = {"ok": False, "dimension": dimension, "target": target, "readback": None,
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
        timeout = min(float(self.config["choose_timeout"]), remaining)
        cmd = [self.python, str(script), DIMENSION_FLAGS[dimension], target]
        started_utc = datetime.now(timezone.utc)
        result = self.runner(cmd, timeout=timeout, cwd=self.root)
        out.update({"exit_code": result.get("exit_code"),
                    "timeout": bool(result.get("timeout")),
                    "elapsed": result.get("elapsed"),
                    "stderr": truncate(result.get("stderr"), 2000)})
        if result.get("timeout"):
            out.update(error_code="choose_timeout",
                       error_message=f"choose timed out after {timeout:.0f}s")
            return out
        if result.get("exit_code") != 0:
            out.update(error_code="choose_failed",
                       error_message=(parse_last_json(result.get("stdout") or "") or {}).get("hint")
                       or truncate(result.get("stderr"), 500)
                       or f"choose exit code {result.get('exit_code')}")
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

    def _run_use(self, endpoint: str, prompt: str, purpose: str, deadline: float) -> dict:
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
        timeout = min(float(self.config["use_timeout"]), remaining)
        cmd = [self.python, str(script), prompt, "--purpose", purpose,
               "--timeout", str(max(1, int(timeout)))]
        result = self.runner(cmd, timeout=timeout, cwd=self.root)
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

        with self._workflow_lock:
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

                updated = dict(record)
                updated["state"] = target
                updated["no_progress"] = 0
                updated["missing"] = []
                history = record.get("history")
                updated["history"] = (list(history) if isinstance(history, list) else []) + [
                    {"from": current, "to": target, "at": now}]
                updated["updated_at"] = now
                cards[card] = updated
                node["cards"] = cards
                data["workflow"] = node
                atomic_write_json(path, data)

                check = self.workflow_read(name)["data"]
                got = (((check.get("workflow") or {}).get("cards") or {})
                       .get(card) or {}).get("state")
                if got == target:
                    return {"file": name, "card": card, "from": current, "to": target,
                            "created": False, "state": target}
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
            # 唯一免 token 的接口，只回最小信息：ok + version（外加 port）。
            # 保留 port 是 CARD-09a 的用例按它确认「监听在哪个端口」；pid / uptime /
            # active_jobs / name 这些内部状态不再对外说。
            self._send_json(200, {
                "ok": True, "version": "0.0.9", "port": self.engine.port,
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
