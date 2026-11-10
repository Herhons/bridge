"""Offline unittest fence, inherited by Python test subprocesses.

This prevents accidental suite side effects, not hostile Python/native code.
Setup errors terminate Python: sitecustomize ImportError is normally ignored.
"""
import atexit
import ctypes
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def inside(path):
    if isinstance(path, int):
        return True
    if str(path).lower() == "nul":
        return True
    try:
        candidate = Path(os.fsdecode(path)).resolve()
        return any(candidate == root or root in candidate.parents for root in WRITE_ROOTS)
    except (TypeError, ValueError, OSError):
        return False


def deny(kind, detail):
    expected = kind in getattr(_expected, "kinds", ())
    prefix = "expected_denials" if expected else "guard_events"
    with (EVENTS / f"{prefix}_{os.getpid()}.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps({"kind": kind, "detail": str(detail), "pid": os.getpid(),
                              "expected": expected}) + "\n")
    raise PermissionError("offline unittest guard: " + kind + " " + str(detail))


@contextmanager
def expected_denial(kind):
    """Assert an intentional negative case; never allow the operation."""
    previous = getattr(_expected, "kinds", ())
    _expected.kinds = previous + (kind,)
    try:
        yield
    finally:
        _expected.kinds = previous


def command_parts(executable, args):
    if isinstance(args, (tuple, list)):
        parts = list(args)
    elif os.name == "nt":
        # Windows quoting of embedded Python source is not POSIX shell syntax.
        # Use the OS parser, including backslashes before nested quotes.
        argc = ctypes.c_int()
        parser = ctypes.windll.shell32.CommandLineToArgvW
        parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
        values = parser(str(args), ctypes.byref(argc))
        if not values:
            raise OSError("CommandLineToArgvW failed")
        try:
            parts = [values[index] for index in range(argc.value)]
        finally:
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = ctypes.c_void_p
            free(values)
    else:
        parts = shlex.split(str(args))
    first = executable or (parts[0] if parts else "")
    return str(first).strip('"'), [str(part).strip('"') for part in parts]


def audit(event, args):
    if event == "open":
        path, mode, flags = args
        write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND |
                                                    os.O_CREAT | os.O_TRUNC)))
        if write and not inside(path):
            deny("outside_write", path)
    elif event in ("os.rename", "os.link", "os.symlink"):
        if not inside(args[0]) or not inside(args[1]):
            deny(event, args[:2])
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"):
        if not inside(args[0]):
            deny(event, args[0])
    elif event in ("os.startfile", "os.system"):
        deny(event, args)
    elif event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in LOOPBACK:
            deny("external_network", address)
        listener = _test_ports.get(address[1])
        if (listener is None or listener.fileno() < 0 or
                not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
            deny("unregistered_loopback", address)
    elif event == "socket.bind":
        if not isinstance(args[1], tuple) or args[1][0] not in LOOPBACK:
            deny("external_bind", args[1])
    elif event == "subprocess.Popen":
        executable, parts = command_parts(args[0], args[1])
        name = Path(executable).name.lower()
        if Path(executable).resolve() == Path(sys.executable).resolve():
            child_env = args[3] if args[3] is not None else os.environ
            search = str(child_env.get("PYTHONPATH", "")).split(os.pathsep)
            if (str(GUARD_DIR) not in search or
                    child_env.get("BRIDGE_WIP_ROOT") != str(OUT) or
                    not child_env.get("BRIDGE_WIP_RUN") or
                    any(part in ("-S", "-I", "-E") for part in parts[1:])):
                deny("child_guard_missing", executable)
        elif NODE and Path(executable).resolve() == NODE:
            # Only the reviewed deterministic JS test harnesses use Node.
            pass
        elif name in ("icacls", "icacls.exe"):
            # Real ACL checks may harden only temporary test files, using the
            # one fixed argument shape emitted by server.harden_state_acl.
            if (len(parts) != 5 or not inside(parts[1]) or
                    parts[2:4] != ["/inheritance:r", "/grant:r"] or
                    parts[4] != os.environ.get("USERNAME", "") + ":F"):
                deny("outside_acl", parts)
        elif name in ("taskkill", "taskkill.exe"):
            try:
                target = int(parts[parts.index("/PID") + 1])
            except (ValueError, IndexError):
                target = -1
            process = _children.get(target)
            if process is None or process.poll() is not None:
                deny("non_test_process_kill", target)
        else:
            deny("non_test_process", executable)


def blocked(name):
    def call(*args, **kwargs):
        return deny("real_ui_api", name)
    return call


def check_exit():
    """A swallowed fence exception must still make plain unittest fail."""
    events = [path for path in EVENTS.glob("guard_events_*.jsonl") if path.stat().st_size]
    if events:
        sys.stderr.write("OFFLINE_GUARD_UNEXPECTED_EVENTS=" + str(len(events)) + "\n")
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(86)


def install():
    global OUT, EVENTS, RUNTIME, WRITE_ROOTS, GUARD_DIR, NODE, LOOPBACK
    global _expected, _test_ports, _children
    GUARD_DIR = Path(__file__).resolve().parent
    OUT = GUARD_DIR.parents[1]
    if Path(os.environ.get("BRIDGE_WIP_ROOT", OUT)).resolve() != OUT:
        raise ValueError("BRIDGE_WIP_ROOT must match this test checkout")
    run = os.environ.get("BRIDGE_WIP_RUN") or (
        "direct_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + str(os.getpid()))
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", run) or ".." in run:
        raise ValueError("invalid offline run name")
    EVENTS = OUT / "_selftest" / run
    RUNTIME = EVENTS / "runtime"
    temp = OUT / "_tmp"
    for directory in (EVENTS, RUNTIME, temp):
        directory.mkdir(parents=True, exist_ok=True)
    WRITE_ROOTS = (EVENTS, temp)
    os.environ.update(BRIDGE_WIP_ROOT=str(OUT), BRIDGE_WIP_RUN=run,
                      PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", TEMP=str(temp), TMP=str(temp),
                      LOCALAPPDATA=str(RUNTIME / "LocalAppData"), APPDATA=str(RUNTIME / "AppData"))
    os.environ.pop("BRIDGE_NOTIFY_URL", None)
    os.environ.pop("BRIDGE_NOTIFY_KEY", None)
    os.environ.pop("BRIDGE_SERVER_STATE", None)
    paths = [str(GUARD_DIR), str(OUT / "src")]
    paths += [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p not in paths]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    if str(OUT / "src") not in sys.path:
        sys.path.insert(0, str(OUT / "src"))
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(temp)
    NODE = Path(shutil.which("node")).resolve() if shutil.which("node") else None
    LOOPBACK = ("127.0.0.1", "localhost", "::1")
    _expected = threading.local()
    _test_ports = weakref.WeakValueDictionary()
    _children = weakref.WeakValueDictionary()
    sys.addaudithook(audit)
    atexit.register(check_exit)
    original_bind = socket.socket.bind

    def test_bind(sock, address):
        result = original_bind(sock, address)
        _test_ports[sock.getsockname()[1]] = sock
        return result

    socket.socket.bind = test_bind
    original_popen_init = subprocess.Popen.__init__

    def test_popen_init(process, *args, **kwargs):
        original_popen_init(process, *args, **kwargs)
        _children[process.pid] = process

    subprocess.Popen.__init__ = test_popen_init
    for library, names in {
        "user32": ("SendInput", "SetCursorPos", "SetForegroundWindow", "ShowWindow", "PostMessageW",
                   "PostMessageA", "SendMessageW", "keybd_event", "mouse_event", "OpenClipboard",
                   "GetClipboardData", "SetClipboardData", "EmptyClipboard", "BringWindowToTop",
                   "PrintWindow", "SetWindowPos", "MoveWindow", "CloseWindow", "DestroyWindow",
                   "SetFocus", "SetActiveWindow", "AttachThreadInput", "NotifyWinEvent"),
        "shell32": ("ShellExecuteW", "ShellExecuteA"),
        "gdi32": ("BitBlt", "GetDIBits"),
    }.items():
        dll = getattr(ctypes.windll, library)
        for name in names:
            setattr(dll, name, blocked(library + "." + name))

    import uiautomation as auto
    for name in ("GetRootControl", "ControlFromHandle", "GetFocusedControl"):
        setattr(auto, name, blocked("uiautomation." + name))
    from qi import runlog, failure, guard, progress, tier_guard, uilock, browser, workflow, screen
    from qi.screen import Screen
    import numpy as np

    def synthetic_grab(self, region=None):
        return np.zeros((2, 2, 3), dtype=np.uint8)

    Screen.grab = synthetic_grab
    runlog.RUNS = EVENTS / "runs"
    failure.FAIL_DIR = EVENTS / "fail"
    guard.RUNS = EVENTS / "runs"
    guard.MARK_FILE = EVENTS / "synthetic_input.json"
    tier_guard.ROOT = progress.ROOT = uilock.ROOT = RUNTIME
    tier_guard.TIER_DIR = RUNTIME / "runs/tier"
    browser.STATE = RUNTIME / ".chrome-window.json"
    screen.EVIDENCE = EVENTS / "screen"
    original_progress_path = workflow.progress_path

    def isolated_progress_path(path=None):
        # Keep workflow definition/config reads and explicit test roots intact.
        if path is None and workflow.ROOT == OUT:
            return RUNTIME / "progress.json"
        return original_progress_path(path)

    workflow.progress_path = isolated_progress_path
    manifest = {"pid": os.getpid(), "root": str(OUT), "run": run, "events": str(EVENTS),
                "write_roots": [str(path) for path in WRITE_ROOTS], "runtime": str(RUNTIME),
                "capture": "synthetic_2x2_black", "real_ui": "blocked",
                "network": "only loopback ports bound by this test process",
                "subprocess": "inherited Python, Node harness, tracked-child taskkill, temp-file icacls",
                "unexpected_events_exit": 86}
    (EVENTS / f"guard_manifest_{os.getpid()}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    sys._bridge_offline_guard_active = str(OUT)
    sys._bridge_offline_guard_module = sys.modules[__name__]


try:
    install()
except BaseException as error:
    sys.stderr.write("OFFLINE_GUARD_SETUP_FAILED: " + type(error).__name__ + ": " + str(error) + "\n")
    sys.stderr.flush()
    os._exit(87)
