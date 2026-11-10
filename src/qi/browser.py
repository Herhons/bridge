"""准备一个可操作的 Chrome 窗口。

规则（2026-10-29 用户定的）：
  1. 上次脚本开的窗口还在 → 直接用（重开会在你桌面堆窗口）
  2. 否则看现有 Chrome **有没有在播音频**（放视频几乎一定出声）：
       · 没在放 → 就在那个 Chrome 里开目标网页，再把它摆到副屏
       · 在放   → 新开一个窗口（不打扰你正在看的东西）
  3. 一个 Chrome 都没有 → 新开窗口

**一次只开"当前要操作的那个站"**，不是四个都开。因为复用已有窗口时，
用户原有的标签还在前面，`Ctrl+N` 的编号就不可靠了；只开一个的话，
新标签必然落在最后并且是当前标签，脚本直接就能操作——不依赖任何编号。

判断"当前标签是不是目标页"靠窗口标题和 URL（Chrome 的标题 = 当前标签标题）。

2026-11-01 CARD-WEB 修的两条：
  · **新窗口必须提到前台再判页。** Chrome 在后台时页面文档的 URL 读不到
    （见 qi/ui.py 开头），旧代码用 SW_SHOWMINNOACTIVE 开窗后直接轮询 URL，
    永远判"不是目标页"，25 秒后报 no_window 还留一个窗口在桌面。
  · **绑不上就把自己开的窗口关掉**，不留垃圾窗口。
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32


def _declare_win32():
    """声明句柄参数类型。

    2026-11-01 CARD-WEB：64 位下句柄可以大于 2^31，ctypes 不声明就按 32 位 int 传，
    抛 `OverflowError: int too long to convert`。枚举窗口时正好会碰到这种句柄
    （实测同一天 opencode_use --list 就是这么崩的）。
    """
    from ctypes import POINTER, c_int, c_uint

    user32.IsWindowVisible.argtypes, user32.IsWindowVisible.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, c_int]
    user32.GetWindowTextW.restype = c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.BringWindowToTop.argtypes, user32.BringWindowToTop.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, c_int, c_int,
                                    c_int, c_int, c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, c_uint, ctypes.c_size_t, ctypes.c_size_t]
    user32.PostMessageW.restype = wintypes.BOOL
    shell32.ShellExecuteW.restype = ctypes.c_void_p


_declare_win32()

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_CLOSE = 0x0010

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".chrome-window.json"

# 站点名 -> (URL, 窗口标题里该有的关键词)
SITES = {
    "chatgpt": ("https://chatgpt.com/", "chatgpt"),
    "gemini": ("https://gemini.google.com/app", "gemini"),
    "grok": ("https://grok.com/", "grok"),
    "deepseek": ("https://chat.deepseek.com/", "deepseek"),
}

DEFAULT_POS = (-1440, -560)
DEFAULT_SIZE = (1380, 1340)


# ---------------------------------------------------------------- 窗口枚举
def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(int(hwnd))
    if n <= 0:
        return ""
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(int(hwnd), b, n + 1)
    return b.value


def _chrome_hwnds() -> set[int]:
    out: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        t = _title(int(hwnd))
        if "google chrome" not in t.lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < 300 or r.bottom - r.top < 300:
            return True
        out.add(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return out


def alive(hwnd: int) -> bool:
    if not hwnd or not user32.IsWindow(int(hwnd)):
        return False
    return "google chrome" in _title(int(hwnd)).lower()


def _frontmost_chrome() -> int | None:
    h = int(user32.GetForegroundWindow())
    return h if alive(h) else None


def _on_secondary(hwnd: int) -> bool:
    """窗口中心是否在副屏（主屏原点固定 (0,0)）。"""
    pw = user32.GetSystemMetrics(0)
    ph = user32.GetSystemMetrics(1)
    r = wintypes.RECT()
    user32.GetWindowRect(int(hwnd), ctypes.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    return not (0 <= cx < pw and 0 <= cy < ph)


def _best_tab_window() -> int | None:
    """tabs 模式选窗口：**标签最多的那个**（同数量再比面积）。

    理由：四端共用一窗的前提是那窗里已经有这几家的标签。记忆里的 hwnd 可能是
    上一次 per_site 模式留下的单标签窗（2026-11-08 就踩到：chatgpt 记着一个只有
    一个标签的窗，四个端全绑过去，另外三家的标签根本不在里面）。
    """
    from . import ui                    # 模块级没导入：漏了它 NameError 会被下面吞成 0

    best, best_key = None, (0, 0)
    for h in _chrome_hwnds():
        try:
            count = len(ui.tabs(h))
        except (RuntimeError, OSError, AttributeError):   # 读不到标签条就当 0
            count = 0
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        area = (r.right - r.left) * (r.bottom - r.top)
        if (count, area) > best_key:
            best, best_key = h, (count, area)
    return best


def _largest_chrome() -> int | None:
    best, area = None, 0
    for h in _chrome_hwnds():
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        a = (r.right - r.left) * (r.bottom - r.top)
        if a > area:
            best, area = h, a
    return best


# ---------------------------------------------------------------- 摆放与前台
def place(hwnd: int, position=DEFAULT_POS, size=DEFAULT_SIZE) -> None:
    """摆到指定位置和大小。不激活。

    2026-11-02 CARD-06a (c)：`ShowWindow(SW_RESTORE)` 的返回值不可信
    （它返回"调用前是否可见"），所以恢复后要轮询确认窗口真的脱离了最小化，
    再摆位置。恢复不了就不摆——摆一个还在最小化的窗口没有意义。
    """
    from . import window as window_mod

    window_mod.restore(int(hwnd), timeout=4.0)
    user32.SetWindowPos(int(hwnd), 0, position[0], position[1], size[0], size[1],
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


def _focus(hwnd: int) -> bool:
    hwnd = int(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    tid_cur = kernel32.GetCurrentThreadId()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if tid_fg and tid_fg != tid_cur:
        attached = bool(user32.AttachThreadInput(tid_cur, tid_fg, True))
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_cur, tid_fg, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def close(hwnd: int, *, timeout: float = 6.0) -> bool:
    """关掉**脚本自己开的**窗口（用户的窗口不许走这里）。

    新窗口绑定失败后要收尾：巡检 06:0x 那轮留下 2 个 Gemini + 1 个 Grok
    窗口没人关，就是这么堆起来的。返回是否真的关掉了。
    """
    try:
        hwnd = int(hwnd)
        if not user32.IsWindow(hwnd):
            return True
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:                       # noqa: BLE001 - 关不掉只是留个窗口，不该抛
        return False
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.2)
        if not user32.IsWindow(hwnd):
            return True
    return not user32.IsWindow(hwnd)


# ---------------------------------------------------------------- 状态
def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(hwnd: int, site: str = "") -> None:
    """写窗口记忆。

    2026-11-08（B13b）：并发会审要**一端一窗**。原来只有一份 `{hwnd, site}`，
    `ensure_site` 复用同一个窗口切标签 —— 于是 chatgpt/gemini/grok/deepseek
    任何时刻只有一个标签在前台，`ui.document` 只能拿到它，并发无从谈起。

    现在多存一层 `by_site`，**顶层 `hwnd/site` 照旧**（老代码/老文件都能读）。
    """
    data = _load()
    data["hwnd"] = int(hwnd)
    data["site"] = site or ""
    data["v"] = 2
    if site:
        bucket = data.get("by_site")
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[str(site)] = int(hwnd)
        data["by_site"] = bucket
    STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


TAB_TIMEOUT = 2.5
#: 切标签后用它确认"切对了"。只认 URL host —— 标签名会被 Chrome 加上
#: "内存用量 / 闲置标签页" 之类的后缀，名字只能当提示（真机探针 2026-11-08）。
HOSTS = None          # 延迟到 ui 导入后取，避免模块级循环导入


def window_mode() -> str:
    """网页端开窗形态：`tabs`（一窗多标签）或 `per_site`（一端一窗）。

    代码默认 `per_site` —— 与 B13b 之后的行为逐字节一致；
    真正切到 `tabs` 是 `docs/bridge_server.json` 里配的，由服务端下发环境变量。
    """
    return os.environ.get("BRIDGE_WEB_WINDOW_MODE", "per_site") == "tabs" and "tabs" or "per_site"


def _hosts() -> dict:
    from . import ui
    return ui.HOSTS


def _tab_record(site: str) -> dict:
    bucket = _load().get("tabs")
    if not isinstance(bucket, dict):
        return {}
    item = bucket.get(str(site))
    return item if isinstance(item, dict) else {}


def _save_tab(hwnd: int, site: str, item=None) -> None:
    """记住"这个 site 用哪个窗口的哪个标签"。`by_site` 仍然写（四端同值）。"""
    data = _load()
    tabs = data.get("tabs")
    if not isinstance(tabs, dict):
        tabs = {}
    record = {"hwnd": int(hwnd), "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if isinstance(item, dict):
        record.update({k: v for k, v in item.items() if k in ("rid", "name")})
    tabs[str(site)] = record
    data["tabs"] = tabs
    data["v"] = 3
    if site:
        bucket = data.get("by_site")
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[str(site)] = int(hwnd)
        data["by_site"] = bucket
    data["hwnd"] = int(hwnd)
    data["site"] = site or data.get("site") or ""
    STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _active_url(hwnd: int) -> tuple[str, int]:
    """当前活动标签的 (url, 文档数)。后台窗口 URL 会是空串（ui.documents 的既定行为）。"""
    from . import ui
    try:
        docs = ui.documents(hwnd)
    except Exception:                    # noqa: BLE001 - 读不到就算没读到
        return "", 0
    if len(docs) != 1:
        return "", len(docs)
    return (docs[0][1] or ""), 1


def _await_site(hwnd: int, site: str, seconds: float = TAB_TIMEOUT) -> bool:
    from urllib.parse import urlsplit

    host = _hosts().get(site)
    if not host:
        return True
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        url, count = _active_url(hwnd)
        if count == 1 and url and urlsplit(url).hostname == host:
            return True
        time.sleep(0.2)
    return False


class TabUnbound(RuntimeError):
    """认不出"哪个标签是我的"。**fail-closed**：绝不猜一个标签就往上写字。"""

    stage = "tab_unbound"
    code = 10

    def __init__(self, message: str, *, stage: str = "tab_unbound", facts=None):
        super().__init__(message)
        self.stage = stage
        self.facts = facts or {}


def acquire_tab(site: str, hwnd: int, *, tab: int | None = None,
                verbose: bool = False) -> None:
    """把 hwnd 的活动标签确保成 `site` 那一个。**幂等**：已经对就 0 次切换。

    每次读/点/截图之前都要调一次：轮询段每轮放锁，别的席位会把标签切走。
    判据只认 URL host（`ui.HOSTS`）；标签名只用来缩小候选（会被 Chrome 加后缀）。
    """
    from urllib.parse import urlsplit

    from . import ui

    host = _hosts().get(site)
    if not host:
        return                            # 桌面端没有标签概念
    url, count = _active_url(hwnd)
    if count == 1 and url and urlsplit(url).hostname == host:
        return                            # 已经是我这一页
    items = ui.tabs(hwnd)
    if not items:
        raise TabUnbound(f"{site}: 读不到标签条（窗口={hwnd}）", stage="tab_strip_missing",
                         facts={"hwnd": hwnd})
    keyword = (SITES.get(site) or ("", site))[1].lower()
    others = [k for name, (_, k) in SITES.items() if name != site]
    if tab is not None and 1 <= int(tab) <= len(items):
        candidates = [items[int(tab) - 1]]
    else:
        named = [it for it in items if keyword in (it.name or "").lower()]
        if len(named) == 1:
            candidates = named
        else:
            # 名字不唯一（Chrome 会加"内存用量"后缀、也可能有两个同站标签）：
            # 先试名字命中的，再试其余"明显不是别家"的，逐个 Select + URL 复核。
            candidates = named + [it for it in items
                                  if it not in named
                                  and not any(k in (it.name or "").lower() for k in others)]
    last_error = None
    for item in candidates:
        try:
            item.control.GetSelectionItemPattern().Select()
        except Exception as exc:          # noqa: BLE001 - 元素失效：换下一个
            last_error = exc
            continue
        if _await_site(hwnd, site):
            try:
                rid = list(item.control.GetRuntimeId() or [])
            except (TypeError, AttributeError):
                rid = []                   # UIA 偶发不返回数组；rid 只是提示，不当证据
            _save_tab(hwnd, site, {"rid": rid, "name": item.name})
            if verbose:
                print(f"  切到标签 {item.name!r}（{site}）")
            return
    raise TabUnbound(
        f"{site}: 切不到自己的标签（窗口={hwnd}；读到 {len(items)} 个标签："
        f"{[it.name for it in items][:6]}）" + (f"；最后一次错误 {last_error}" if last_error else ""),
        stage="tab_ambiguous" if len(candidates) > 1 else "tab_unbound",
        facts={"hwnd": hwnd, "tabs": [it.name for it in items][:8]})


def acquire_document(site: str, hwnd: int, *, wake=None):
    """取文档。tabs 模式的"先切标签"已经在 `ui.document` 里统一做了。"""
    from . import ui
    return ui.document(hwnd, site, wake=wake)


def concurrent() -> bool:
    """并发会审模式：每个端用自己的窗口，不切别人的标签。"""
    return os.environ.get("BRIDGE_UI_CONCURRENT") == "1"


def current(site: str | None = None) -> int | None:
    data = _load()
    if site:
        bucket = data.get("by_site")
        h = bucket.get(site) if isinstance(bucket, dict) else None
        return int(h) if h and alive(int(h)) else None
    h = data.get("hwnd")
    return int(h) if h and alive(int(h)) else None


def forget() -> None:
    if STATE.exists():
        STATE.unlink()


# ---------------------------------------------------------------- 主流程
def _wait_ready(hwnd: int, timeout: float = 15.0) -> bool:
    """等页面加载完：窗口标题连续两次相同就算稳。

    Chrome 加载中标题会变（先域名、再页面标题、再带未读数），稳定下来基本就是好了。
    不这么等的话，`ensure_site` 返回时 DOM 还没渲染，脚本上去找不到控件，报一堆
    "unreadable / 打不开菜单"——实测新标签打开后只等 1.2s 就是这个结果。
    """
    prev = None
    end = time.time() + timeout
    while time.time() < end:
        t = _title(hwnd)
        if prev is not None and t == prev and "google chrome" in t.lower():
            return True
        prev = t
        time.sleep(0.8)
    return False


def _open_url(hwnd: int, url: str, wait: float = 1.2) -> None:
    """在已有窗口里开一个标签。Chrome 会把它开在最后并切过去。"""
    if not _focus(hwnd):
        raise RuntimeError("浏览器窗口无法获得焦点")
    shell32.ShellExecuteW(None, "open", CHROME, url, None, SW_SHOWMINNOACTIVE)
    time.sleep(wait)
    _wait_ready(hwnd)


def _cleanup_spawn(hwnd, verbose=False):
    try:
        close(hwnd)
    except Exception as exc:                   # cleanup must preserve the cause
        if verbose:
            print(f"  新窗口清理失败 hwnd={hwnd}: {type(exc).__name__}: {exc}")


#: 新窗口归属 nonce 的适用范围（T1-1 P1）。
#: 只对"已确认会保留查询串"的站点启用；deepseek 未验证，先走弱证据路径
#: （恰好一个新增窗口），避免站点吞掉 query 后永远认领不到窗口。
#: 真要一键关掉整条 P1 路径，把这里改成空集合即可。
SPAWN_NONCE_SITES = frozenset({"chatgpt", "gemini", "grok"})


def _spawn_nonce(name: str) -> str | None:
    """给本次启动生成一个一次性归属 nonce；不在白名单返回 None（不启用 P1）。"""
    if name not in SPAWN_NONCE_SITES:
        return None
    import secrets

    return secrets.token_hex(8)


def _with_nonce(url: str, nonce: str) -> str:
    """把归属 nonce 拼进查询串（保留原有 query 与 fragment）。"""
    from urllib.parse import urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    extra = urlencode({"bridge": nonce})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _spawn_proves_nonce(hwnd: int, nonce: str, seconds: float = 4.0) -> bool:
    """这个窗口里有没有一条文档的 URL 带本次 nonce。

    读不到文档、或读到但与 nonce 对不上，都返回 False（fail-closed）。
    页面刚打开时 URL 可能还没出来，所以轮询几秒；仍然读不到就算不成立。
    """
    from . import ui

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            docs = ui.documents(hwnd)
        except Exception:                  # noqa: BLE001 - 读不到就继续等
            docs = []
        if any(nonce in (value or "") for _, value in docs):
            return True
        time.sleep(0.4)
    return False


def _spawn(url: str, position, size, timeout: float, verbose: bool,
           nonce: str | None = None) -> int | None:
    """启动一个新窗口并绑定它。**只有证明得了归属才认领，认领了才允许关闭。**

    归属证据（T1-1）：
      · 给了 `nonce`  -> URL 里带它，且窗口里某条文档的 URL 也能读回它（强证据）
      · 没给 `nonce`  -> 只能靠"启动前后恰好多了 1 个窗口"（弱证据，但仍**不关**
        任何未认领的窗口）

    **绝不做的事**：超时后再"看一眼有没有迟到窗口"就把它当成自己的。
    迟到 1 秒出现的 Chrome 窗口没有任何归属证据，它极可能是用户自己新开的；
    旧代码就是这么把一个无证据的窗口赋给 `hwnd`、然后被 `finally` 关掉的。
    """
    before = _chrome_hwnds()
    target = _with_nonce(url, nonce) if nonce else url
    arg = " ".join(["--new-window", "--no-first-run", "--no-default-browser-check",
                    f"--window-position={position[0]},{position[1]}",
                    f"--window-size={size[0]},{size[1]}", target])
    rc = shell32.ShellExecuteW(None, "open", CHROME, arg, None, SW_SHOWMINNOACTIVE) or 0
    if rc <= 32:
        return None
    hwnd, keep, proven = None, False, False
    try:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.4)
            new = _chrome_hwnds() - before
            if len(new) > 1:
                # Ownership is ambiguous; never pick/close an arbitrary window.
                if verbose:
                    print("  出现多个新窗口，无法确认归属，停止绑定")
                return None
            if new:
                hwnd = next(iter(new))
                break
        if hwnd is None:
            # 超时没有任何新窗口：就是没有。**不再回头认领迟到窗口**——
            # 那一步既证明不了归属，又会把用户自己新开的窗口关掉。
            if verbose:
                print("  超时没有等到新窗口；不认领任何迟到窗口")
            return None
        # 归属**先证、后动窗口**（F2，2026-11-07 独立复核实测 A4/A5）：
        # 旧顺序是先 place/focus 再验 nonce，一旦 place/focus 失败就进 finally 关窗，
        # 而那时归属还没证。现在把取证提到最前，验证不过就原地保留。
        if nonce:
            if not _spawn_proves_nonce(hwnd, nonce):
                # 有窗口但读不到本次 nonce：归属不成立。**保留窗口不关**，
                # 交给人和日志去判断（可能是站点丢了 query，也可能是别人开的）。
                if verbose:
                    print(f"  窗口 hwnd={hwnd} 里读不到本次 nonce，不认领（保留，不关闭）")
                return None
            proven = True
        place(hwnd, position, size)
        if not _focus(hwnd) or not _wait_ready(hwnd):
            return None
        if verbose:
            print(f"  新窗口 hwnd={hwnd}" + ("（nonce 已证）" if proven else "（弱证据）"))
        keep = True
        return hwnd
    finally:
        # 只关**证明得了归属**的窗口。弱证据（启动前后恰好多 1 个）不足以杀掉
        # 用户自己的窗口：宁可留一个孤儿窗口，也不误关（施工单 T1-1）。
        if hwnd is not None and not keep and proven:
            _cleanup_spawn(hwnd, verbose)


def _site_ready(hwnd: int, name: str, *, remembered: bool = False) -> bool:
    """这个窗口里有没有本站的页面。

    判据与 `ui._pick` 同构，**顺序不可换**：

    ① 有非空 URL 且 host 对得上 → 就绪（强证据）；
    ② URL **全读不到**、窗口里只有一个文档、且这个窗口是**我们记住的**那个
       → 就绪。`ui.documents` 的 docstring 明写：URL 为空是"窗口不在前台"，
       不是"没有这个文档"；`ui._pick` 也是靠这一条兜底绑定的。
    ③ 其余一律不就绪（fail-closed）。

    为什么 ② 只对记住的窗口放行：假阴（说没就绪）只会多开一个窗，可逆；
    假阳（说就绪但那是别人的窗口）会往别人的对话里写字，不可逆。
    """
    from urllib.parse import urlsplit

    from . import ui
    host = ui.HOSTS.get(name)
    try:
        docs = ui.documents(hwnd)
    except Exception:                      # noqa: BLE001 - 读不到就是没准备好
        return False
    if any(url and host and urlsplit(url).hostname == host for _c, url in docs):
        return True
    if remembered and len(docs) == 1 and not (docs[0][1] or "").strip():
        return True
    return False


def ensure_site(name: str, *, tab: int | None = None, continue_session: bool = False,
                position=DEFAULT_POS, size=DEFAULT_SIZE,
                timeout: float = 25.0, verbose: bool = True) -> int | None:
    """绑定实际站点 URL。续聊只选择现有标签，绝不打开首页。"""
    from .audio import probe as audio_probe
    from . import ui
    from .screen import Screen
    from urllib.parse import urlsplit

    if name not in SITES:
        raise KeyError(f"未知站点 {name}，可选 {list(SITES)}")
    url, keyword = SITES[name]

    if window_mode() == "tabs":
        # 单窗多标签：四端共用一个 Chrome 窗口，靠"读之前切过去"实现等待并行。
        # 这里只负责**找/开那个窗口**；切标签在 acquire_tab（每次读前都做）。
        own = _best_tab_window() or current(name)
        if own and alive(own):
            # **必须现在就切到本站标签**：choose 脚本拿到窗口后立刻去找芯片/模板，
            # 不切就等于在别人那一页上找（2026-11-08 真机：三个端全在 Gemini 页上失败）。
            # 整段进锁：切标签 + 抢前台是共享资源。
            from . import uilock
            with uilock.section(f"tab:{name}", timeout=uilock.SECTION_TIMEOUT):
                _focus(own)
                acquire_tab(name, own, tab=tab, verbose=verbose)
            _save_tab(own, name)
            if verbose:
                print(f"  复用单窗 hwnd={own}（{name}，标签模式）")
            return own
        spawned = _spawn(url, position, size, timeout, verbose)
        if spawned:
            _save_tab(spawned, name)
        return spawned

    if concurrent():
        # 并发会审：**只认自己那个端的窗口**，没有就开一个新的。
        # 绝不在别人的窗口里切标签 —— 那会让别人的页面从树里消失。
        own = current(name)
        if own and _site_ready(own, name, remembered=True):
            if verbose:
                print(f"  复用本端窗口 hwnd={own}（{name}）")
            return own
        if own and verbose:
            # 记住的窗口还在、但里面读不到本站页面：宁可贵一次开窗，也不误用别人的
            print(f"  本端窗口 hwnd={own} 读不到 {name} 的页面，另开一个（弱证据）")
        spawned = _spawn(url, position, size, timeout, verbose)
        if spawned:
            _save(spawned, name)
        return spawned

    def docs_of(hwnd):
        try:
            return ui.documents(hwnd)
        except Exception as exc:            # noqa: BLE001 - 只用于判断，下面会走"没就绪"
            return exc

    def correct_page(hwnd):
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            return False
        return any(value and urlsplit(value).hostname == ui.HOSTS[name]
                   for _, value in found)

    def title_matches(hwnd):
        """URL 读不到时的退路：Chrome 的标题就是当前标签标题。"""
        return keyword in _title(hwnd).lower()

    def page_ready(hwnd):
        """目标页是否已经在眼前。

        先看 URL；URL 读不到（Chrome 窗口不在前台时的常态）时看标题。
        **不能把"读不到 URL"直接当成"没打开"**——旧代码就是这么误判的。
        """
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            return False
        if any(value and urlsplit(value).hostname == ui.HOSTS[name]
               for _, value in found):
            return True
        if len(found) == 1 and not found[0][1]:
            return title_matches(hwnd)
        return False

    def why(hwnd):
        """失败时一行说清现场，别只留个 no_window。"""
        found = docs_of(hwnd)
        if isinstance(found, Exception):
            docs = f"读取失败 {type(found).__name__}: {found}"
        else:
            docs = "；".join(value or "(URL 未知)" for _, value in found) or "没有文档"
        return f"{ui.window_facts(hwnd)}；文档：{docs}"

    def await_page(hwnd, seconds=12.0):
        """等页面真的变成目标站。

        标签页标题会被会话名替换（ChatGPT 的标题曾经是「回归测试标记」），
        所以「按标题找不到」不能推出「这个站没开着」；新开的标签也不是
        立刻就能读到 URL。这里统一按 URL 轮询，URL 读不到才退回标题。
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if page_ready(hwnd):
                return True
            time.sleep(0.5)
        return False

    def select_existing(hwnd):
        from . import window as window_mod
        # 最小化时 UIA 枚举标签页会读空，先恢复（轮询确认，不看返回值）
        window_mod.restore(int(hwnd), timeout=4.0)
        s = Screen(verbose=verbose)
        s.bind(hwnd)
        if not s.focus():
            return False
        if tab is None and correct_page(hwnd):
            return True
        items = ui.tabs(hwnd)
        if tab is not None:
            if not 1 <= tab <= len(items):
                return False
            target = items[tab - 1]
        else:
            matches = [item for item in items if keyword in item.name.lower()]
            if len(matches) != 1:
                # 标题常常已经变成会话名：挨个试，命中 URL 就算（别去开重复标签）
                others = [k for s2, (_, k) in SITES.items() if s2 != name]
                for item in items:
                    if any(k in item.name.lower() for k in others):
                        continue
                    item.control.GetSelectionItemPattern().Select()
                    if await_page(hwnd, 3.0):
                        return True
                return False
            target = matches[0]
        target.control.GetSelectionItemPattern().Select()
        return await_page(hwnd, timeout)

    old = current()
    if old:
        if select_existing(old):
            if verbose:
                print(f"  复用窗口 hwnd={old}（当前已是 {name}）")
            _save(old, name)
            return old
        if continue_session or tab is not None:
            return None
        if verbose:
            print(f"  复用窗口 hwnd={old}，在里面开 {name}")
        _open_url(old, url)
        if not await_page(old):
            # 这是用户的窗口，即使没打开也不能关；只把现场说清楚。
            if verbose:
                print(f"  在复用窗口里没打开 {name}（{why(old)}），窗口是用户的，不动它")
            return None
        _save(old, name)
        return old

    if continue_session or tab is not None:
        return None

    info = audio_probe()
    playing = info["playing"] if info["available"] else True
    if verbose:
        if info["available"]:
            print(f"  Chrome 在播音频: {playing}")
        else:
            print(f"  音频检测不可用，按「正在播放」处理 → 新开窗口（{info['hint']}）")

    if not playing:
        target = _frontmost_chrome() or _largest_chrome()
        # 只复用**已经在副屏**的窗口。主屏那个是用户自己的，把它挪到副屏等于
        # 改了他的布局——那已经越界了，宁可多开一个窗口。
        if target and _on_secondary(target):
            if verbose:
                print(f"  没在播放 → 复用副屏上已有的 Chrome hwnd={target} 开 {name}")
            place(target, position, size)
            _open_url(target, url)
            if not await_page(target):
                if verbose:
                    print(f"  复用窗口里没打开 {name}（{why(target)}），窗口是用户的，不动它")
                return None
            _save(target, name)
            return target
        if verbose and target:
            print("  现有 Chrome 在主屏，不动它 → 新开窗口")

    hwnd = _spawn(url, position, size, timeout, verbose, nonce=_spawn_nonce(name))
    keep = False
    try:
        if hwnd and await_page(hwnd):
            _save(hwnd, name)
            keep = True
            return hwnd
        return None
    finally:
        if hwnd and not keep:
            _cleanup_spawn(hwnd, verbose)
