"""本地后端 server 的生命周期命令（CARD-09a）。

用法（在 F:\\Bridge\\v0.0.9 下）：

    py -3.10 src\\scripts\\bridge_server.py start              # 后台起，等 /health 通了才返回
    py -3.10 src\\scripts\\bridge_server.py start --foreground  # 前台跑，Ctrl+C 结束
    py -3.10 src\\scripts\\bridge_server.py stop [--force]
    py -3.10 src\\scripts\\bridge_server.py status
    py -3.10 src\\scripts\\bridge_server.py rotate             # 换 token（服务要重启才生效）

约定：
- 只绑 127.0.0.1，端口取 docs\\bridge_server.json（默认 18765）。
- 端口被占 / 已有实例在跑 → 打印 JSON 错误并退 1，不抢占、不改端口。
- token 只写进 `%LOCALAPPDATA%\\Bridge\\v009\\server.json`，**不打印**；
  start / status / rotate 只回 token 文件路径和打码后的样子。
- 子进程的 stdout/stderr 写到 `runs\\server.log`（只有启动/错误行，不含 token）。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qi import console, server, uilock  # noqa: E402

ROOT = server.ROOT
LOG_PATH = ROOT / "runs" / "server.log"
START_WAIT = 10.0
STOP_WAIT = 5.0


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def _env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = str(_SRC) + (os.pathsep + existing if existing else "")
    return env


#: 让子进程**不继承父控制台**：父 shell 退出时不会给它发 CTRL_CLOSE_EVENT。
#: 2026-11-08（B14）：这是"start 起的后端随父 shell 一起死"的根因修复。
DETACHED_PROCESS = 0x00000008
#: 允许子进程脱离父进程所在的 job 对象（否则 job 关闭会连带杀掉它）。
#: 只在父 job 允许 breakaway 时才成功，否则 CreateProcess 直接 ERROR_ACCESS_DENIED。
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ERROR_ACCESS_DENIED = 5


def _creationflags(*, breakaway: bool = True) -> int:
    """后台子进程的创建标志（纯函数，便于直接断言整数）。

    **不再带 `CREATE_NO_WINDOW` / `CREATE_NEW_PROCESS_GROUP`**：
    前者与 `DETACHED_PROCESS` 同时使用时会被系统忽略，留着只会让人误以为
    "已经做了隔离"；后者只影响 Ctrl+C 的分发组，对 `taskkill /T` 无效。
    """
    if os.name != "nt":
        return 0
    flags = DETACHED_PROCESS
    if breakaway:
        flags |= CREATE_BREAKAWAY_FROM_JOB
    return flags


def _spawn_detached(argv: list[str], handle) -> tuple[subprocess.Popen, str]:
    """起一个脱离当前 shell 的常驻子进程，返回 (child, how)。

    先试带 `CREATE_BREAKAWAY_FROM_JOB`；父 job 不允许脱离时系统会拒绝
    （`ERROR_ACCESS_DENIED`），此时**降级**为仅 `DETACHED_PROCESS` 重试一次。
    其它 `OSError` 原样抛出，仍走调用方的 `spawn_failed` 分支。
    """

    def _launch(breakaway: bool) -> subprocess.Popen:
        return subprocess.Popen(
            list(argv), cwd=str(ROOT), env=_env(), stdin=subprocess.DEVNULL,
            stdout=handle, stderr=handle,
            creationflags=_creationflags(breakaway=breakaway))

    try:
        return _launch(True), "breakaway"
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ACCESS_DENIED:
            raise
        return _launch(False), "plain"


def _health(port: int, timeout: float = 1.0) -> tuple[bool, dict | None, str]:
    conn = None
    try:
        conn = http.client.HTTPConnection(server.HOST, int(port), timeout=timeout)
        conn.request("GET", "/health")
        response = conn.getresponse()
        raw = response.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        return response.status == 200, data, ""
    except (OSError, http.client.HTTPException) as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


def _post(path: str, payload: dict, *, port: int, token: str,
          timeout: float = 3.0) -> tuple[int | None, dict | None, str]:
    conn = None
    try:
        conn = http.client.HTTPConnection(server.HOST, int(port), timeout=timeout)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        return response.status, data, ""
    except (OSError, http.client.HTTPException) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


def _port_free(port: int) -> tuple[bool, str]:
    """先探一下端口；Windows 上用 SO_EXCLUSIVEADDRUSE，和真绑定一样严格。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind((server.HOST, int(port)))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def _state() -> dict:
    return {
        "token_file": str(server.state_path()),
        "pid_file": str(server.pid_path()),
    }


# ---------------------------------------------------------------- start
def start(port: int | None = None, *, foreground: bool = False) -> int:
    config = server.load_config()
    listen = int(port or config["port"])

    info = server.read_pid()
    if info and uilock.pid_alive(info.get("pid")):
        ok, health, _ = _health(int(info.get("port") or listen))
        return _emit({"ok": False, "action": "start", "error": "already_running",
                      "pid": info.get("pid"), "port": info.get("port"),
                      "health_ok": ok, "health": health, **_state()}, 1)
    if info:
        server.clear_pid()                       # 上次留下的死 pid，清掉

    free, detail = _port_free(listen)
    if not free:
        return _emit({"ok": False, "action": "start", "error": "port_in_use",
                      "port": listen, "detail": detail, **_state()}, 1)

    if foreground:
        try:
            return server.serve(listen)
        except OSError as exc:
            return _emit({"ok": False, "action": "start", "error": "bind_failed",
                          "port": listen, "detail": str(exc), **_state()}, 1)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOG_PATH, "a", encoding="utf-8")
    detached = "plain"
    try:
        child, detached = _spawn_detached(
            [sys.executable, "-m", "qi.server", "--serve", "--port", str(listen)],
            handle)
    except OSError as exc:
        handle.close()
        return _emit({"ok": False, "action": "start", "error": "spawn_failed",
                      "port": listen, "detail": str(exc), **_state()}, 1)
    handle.close()

    deadline = time.monotonic() + START_WAIT
    health = None
    while time.monotonic() < deadline:
        ok, data, error = _health(listen, 0.5)
        if ok:
            health = data
            break
        if child.poll() is not None:
            break
        time.sleep(0.1)

    if health is None:
        server.terminate_tree(child)
        return _emit({"ok": False, "action": "start", "error": "start_failed",
                      "port": listen, "pid": child.pid, "log": str(LOG_PATH),
                      "detail": "server did not answer /health in %.0fs" % START_WAIT,
                      **_state()}, 1)

    return _emit({"ok": True, "action": "start", "port": health.get("port", listen),
                  # `/health` 只回 {ok, version} 是公开契约，**不许**加 pid；
                  # 所以 pid 只能取自 child（原来写 health.get("pid") 恒为 null）。
                  "pid": child.pid, "detached": detached,
                  "health": health, "token": "***", "log": str(LOG_PATH), **_state()})


# ---------------------------------------------------------------- stop
def stop(*, force: bool = False) -> int:
    info = server.read_pid()
    if not info:
        ok, _, _ = _health(server.load_config()["port"])
        if not ok:
            return _emit({"ok": True, "action": "stop", "running": False,
                          "note": "not running"})
        return _emit({"ok": False, "action": "stop", "error": "pid_file_missing",
                      "detail": "server answers /health but no server.pid; pass --force "
                                "only after locating the process yourself"}, 1)

    listen = int(info.get("port") or server.load_config()["port"])
    pid = int(info["pid"])
    ok, health, error = _health(listen)
    if not ok and uilock.pid_alive(pid) is False:
        server.clear_pid(pid)
        return _emit({"ok": True, "action": "stop", "running": False,
                      "note": "process already exited; stale pid file removed", "pid": pid})

    state = server.load_state()
    if state:
        status, body, error = _post("/shutdown", {}, port=listen, token=state["token"])
        if status == 200:
            deadline = time.monotonic() + STOP_WAIT
            while time.monotonic() < deadline:
                if uilock.pid_alive(pid) is False:
                    break
                time.sleep(0.1)
            server.clear_pid(pid)
            return _emit({"ok": True, "action": "stop", "running": False,
                          "pid": pid, "graceful": True})
        if status == 409 and not force:
            return _emit({"ok": False, "action": "stop", "error": "job_running",
                          "pid": pid, "detail": (body or {}).get("message"),
                          "hint": "wait for the job, or stop --force (kills the process tree)"}, 1)
        detail = error or (body or {}).get("message") or f"HTTP {status}"
    else:
        detail = f"token state missing: {server.state_path()}"

    if not force:
        return _emit({"ok": False, "action": "stop", "error": "shutdown_failed",
                      "pid": pid, "detail": detail, "hint": "retry with --force"}, 1)

    server.terminate_tree(pid)
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        if uilock.pid_alive(pid) is False:
            break
        time.sleep(0.1)
    server.clear_pid(pid)
    return _emit({"ok": True, "action": "stop", "running": False, "pid": pid,
                  "graceful": False, "note": "terminated process tree"})


# ---------------------------------------------------------------- status
def status() -> int:
    info = server.read_pid()
    listen = int((info or {}).get("port") or server.load_config()["port"])
    ok, health, error = _health(listen)
    running = bool(ok and health)
    pid = (info or {}).get("pid")
    # pid_alive 返回 True/False/None。**None 不等于活着**（问不动就是没证据）。
    alive = uilock.pid_alive(pid) if pid else None
    payload = {"ok": running, "action": "status", "running": running,
               "port": listen, "pid": pid, "pid_alive": alive,
               # pid 文件在、但里面那个进程已经没了：下次 start 会自己清掉
               "stale_pid_file": bool(pid) and alive is not True,
               # 端口上有人应答，但应答的不是 pid 文件里那个进程
               # （最常见：另一份 checkout 的后端占着同一个端口）
               "mismatch": bool(running and alive is not True),
               "health": health, "token": "***", **_state()}
    if not running and error:
        payload["detail"] = error
    return _emit(payload, 0 if running else 1)


# ---------------------------------------------------------------- rotate
def rotate() -> int:
    config = server.load_config()
    state = server.load_state()
    if state is None:
        state = server.load_or_create_state(port=int(config["port"]))
    else:
        state = server.rotate_token()
    info = server.read_pid()
    running = bool(info and uilock.pid_alive(info.get("pid")))
    return _emit({"ok": True, "action": "rotate", "rotated": True,
                  "token": server.mask_token(state.get("token")),
                  "restart_required": running,
                  "note": "read the new token from the token file; restart the server"
                          if running else "read the new token from the token file",
                  **_state()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge_server.py",
                                     description="Bridge local server lifecycle")
    sub = parser.add_subparsers(dest="action", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--port", type=int, default=None)
    p_start.add_argument("--foreground", action="store_true")

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--force", action="store_true")

    sub.add_parser("status")
    sub.add_parser("rotate")

    args = parser.parse_args(argv)
    if args.action == "start":
        return start(args.port, foreground=args.foreground)
    if args.action == "stop":
        return stop(force=args.force)
    if args.action == "status":
        return status()
    if args.action == "rotate":
        return rotate()
    parser.print_help()
    return 2


if __name__ == "__main__":
    console.setup()
    raise SystemExit(main())
