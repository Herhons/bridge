"""codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 调强度 + 提问收回答。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**

用法：
    python codex_use.py "只回复两个字：收到"
    python codex_use.py --list                          读当前模型和强度
    python codex_use.py --model gpt6_sol                只切模型
    python codex_use.py --effort high                   只调强度
    python codex_use.py --model gpt6_sol --effort max   两个都调
    python codex_use.py --model gpt6_sol --effort max "问题"
    python codex_use.py --probe                         只报每个模板命中在哪，不点击

## 控件怎么定位（这里的方法决定了它有多耐改版）

**每个控件都有自己的模板图，运行时在窗口里找出来，用命中的位置去点。**
没有任何「发送键左边 N 像素是芯片」这种推算。

    cdx_send    橙圆+白三角的发送键
    cdx_arrow   芯片右端的 ⌄ 箭头  ← 芯片文字随模型变，箭头不变，所以裁箭头
    cdx_corner  输入框左上角的圆角边框
    cdx_mic     麦克风（判断发送键是不是变成了麦克风）

窗口位置变了没事（每次摆到 --pos）；窗口尺寸变了也能用——模板匹配找的是图形，
不是坐标。唯一要求是 UI 的**缩放比例**没变（系统 DPI 缩放、浏览器缩放会改变像素大小，
那时模板要按新比例重裁）。换环境先跑 `--probe`，它会打印每个模板的实际分数，
哪个 < 阈值一眼就能看出来。

## 界面结构（2026-10-29 实测，窗口 1380x1340）

输入框在底部，右下角：`GPT-6 Astra Ultra ∨`  麦克风  ●（橙色发送键）
点芯片弹出强度层：

    ┌─────────────────────────┐
    │ ⚡        Ultra        ↻ │   ← 强度档位字
    │        GPT-6 Astra ›    │   ← 点这一行进模型列表
    │  ●━━━━━━━━━━━━━━━━━━    │   ← 紫色滑杆
    └─────────────────────────┘

## 已知限制

1. 模型列表有固定最大高度，一屏最多露 7 项。最后两项（GPT-5.6 Terra / GPT-5.6 Luna）
   既露不出来、滚轮也无效（实测滚 9 格纹丝不动，窗口 1340 拉到 1900 也一样），
   会明确报 unreachable。
2. 取回答还没接上：codex 的复制按钮要 hover 才出现，模板还没裁。
   现在 `"问题"` 会发出去、等生成结束，但取回会返回 need_copy_template。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

# ============================================================ 常量

TITLE_PROC = "ChatGPT.exe"                 # 进程名，不是窗口标题
LAUNCH = r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
WIN_SIZE = (1380, 1340)
WIN_POS = (-1440, -560)

# ---- 控件定位：全部靠**模板匹配**，一个偏移量都不用 ----
#
# 之前这里是 SEND_REL=(1180/1380,1298/1340) + CHIP_DX=-139 + COMPOSER=(-480,-58)，
# 也就是「按比例猜发送键在哪，然后用减法推出芯片和输入框」。那样写换分辨率、
# 改窗口大小、UI 微调都会整段失效，而且错的时候没有任何地方会报错——它只会点空。
#
# 现在每个控件都有自己的模板，运行时在窗口里找它：
SEND_TPL = "cdx_send"          # 橙圆+白三角，右下角发送键

# 强度弹层用**快捷键**打开，不点芯片。
# 芯片上的箭头不能当锚点：箭头紧跟在模型名后面，模型名一变长变短它就跟着移
# （实测 Light->High 移了 47px），而且芯片有时带胶囊底色、有时不带，箭头模板
# 换一次档就匹不上（1.000 -> 0.74）。芯片 tooltip 里写着这个快捷键。
CHIP_HOTKEY = ("ctrl", "shift", "m")
PLUS_TPL = "cdx_plus"          # 输入框左下角 + 按钮
# 输入框的落点模板：**加号 + 它上方 26px 空白**（30x56）。
# 为什么不单独用加号再往上挪 45px：那就是偏移。把"加号 + 上方空白"整块裁下来之后，
# 命中矩形的中心天然落在输入框内（y=1286，框是 1218..1327），点中心即可，零偏移。
# 为什么只留 26px、不是 48px：输入框**高度会变**（空的时候矮、多行的时候高），
# 上边框跟着上下跑。留 48px 时边框会落进模板顶部，分数从 1.0 掉到 0.77（实测）。
# 26px 距边框还有 22px 余量，而且区分度仍然够：1.0000 vs 次高分 0.5011。
COMPOSER_TPL = "cdx_composer"  # 加号+上方空白，命中中心 = 输入框内落点
# 弹层开着时，芯片位置一定显示固定占位文字「Select effort」。
# 这是判断"弹层开没开"唯一可靠的依据：
#   · 不能用滑杆：弹层关着时也会匹到页面上别的灰条，于是误判成"已经开着"，
#     连快捷键都不按，直接读错位置（实测 --list 读到 effort=low，实际是全关状态）；
#   · 不能用"橙色填充"：最高档是紫色渐变，压根没有橙色。
# 实测：弹层开着 1.000，关掉后直接 None。
MENU_TPL = "cdx_select_effort"
MIC_TPL = "cdx_mic"            # 麦克风（用来在发送键/麦克风互变时判断状态）

TPL_THR = 0.90             # 实测：正确位置 1.000，次高分 ≤0.745，0.90 留足余量

# 底部工具条带：只在「窗口全宽 × 距底 150px」这一条里找控件。
#
# 为什么纵向用距底像素、不用比例：
#   三个尺寸实测（1380x1340 / 1150x1050 / 980x900），发送键距底恒为 41px、
#   芯片箭头恒为 42px，一动不动；而距右是 199 / 84 / 41，比例从 0.856 漂到 0.958。
#   也就是说**纵向贴底是硬约束，横向会随窗口宽度重排**。
#   所以纵向按距底像素精确框（150 够放下工具行 41 + 弹层底部一点），
#   横向留全宽——横向位置由模板自己命中，不靠区域推算。
#
# 收益（用户提的三点，实测）：搜索面积从 1380x1340=185 万像素降到 1380x150=20.7 万，
# 少算 89% 的像素；而且不会和页面正文里的图形撞车。
BOTTOM_H = 150

# 窗口窄到这个宽度以下时，Codex 会挤压布局，芯片箭头模板掉到 0.834（实测 980x900），
# 提示一下，不硬跑。
MIN_W = 1050

SLIDER_ABOVE = 57          # 入口行（「GPT-6 Astra ›」）在**轨道中心行**上方 57px
ITEM_GAP = 31              # 模型列表行距
CLICK_MAX, CLICK_GAP = 14, 0.4

EFFORTS = ["low", "mid", "high", "xhigh", "max"]

# 各档位在轨道上的相对位置（0=最左 1=最右），**实测值，不是等分**。
# 键盘每按一次右键停下来的位置是 0 / 0.377 / 0.573 / 0.766 / 0.954，
# 第一个间距是后面几个的两倍（第一次按会跳两档），所以等分 0/0.25/0.5/0.75/1.0
# 会算错：mid 的目标落在两档之间，按一次就冲过去、再退又退回 low
# （实测 --effort mid 停在 low）。
EFFORT_FRAC = {"low": 0.000, "mid": 0.377, "high": 0.573,
               "xhigh": 0.766, "max": 0.954}
EFFORT_LABEL = {"low": "轻度", "mid": "中", "high": "高", "xhigh": "极高", "max": "Ultra"}

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "assets", "library")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "evidence")
TEMPLATE_DIR = os.path.abspath(TEMPLATE_DIR)
DEBUG_DIR = os.path.abspath(DEBUG_DIR)

MODELS = {
    "default":     "codex_m_default",
    "gpt61_sol":   "codex_m_gpt61_sol",
    "gpt6_astra":  "codex_m_gpt6_astra",
    "gpt61_terra": "codex_m_gpt61_terra",
    "gpt6_sol":    "codex_m_gpt6_sol",
    "gpt6_luna":   "codex_m_gpt6_luna",
}
UNREACHABLE = {
    "gpt56_sol": "第 8 项，只能露半截",
    "gpt56_terra": "第 9 项，被列表固定高度截断",
    "gpt56_luna": "第 10 项，被列表固定高度截断",
}
CHECK_TPL = "codex_check"
# 模型项模板的匹配阈值。**0.86 不够**：实测 "GPT-6.1 Sol" 模板在 "GPT-5.6 Sol"
# 上能匹到 0.864、"GPT-6.1 Terra" 在 "GPT-5.6 Terra" 上能匹到 0.883，
# 用 0.86 会取到上一行/下一行（勾读到 31px 外就是这么来的）。
# 正确行是 1.000，留到 0.92 两边都够。
MODEL_THR = 0.92

# ============================================================ Win32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def _init_dpi() -> str:
    """声明 DPI 感知。**必须在任何 SetCursorPos / 截图之前执行。**

    不声明的话进程是 DPI-unaware，Windows 会把所有坐标按系统 DPI 虚拟化：
    实测传 (x,738) 光标实际落到 (x,786)，差 48px，点击就全偏了。
    （网页端那套一直没事，因为 qi/screen.py 里有同样的初始化。）

    per-monitor-v2 让进程直接用**物理像素**，和 PrintWindow 截图的坐标系一致。
    """
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


DPI_LEVEL = _init_dpi()

SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "esc": 0x1B, "enter": 0x0D,
      "tab": 0x09, "delete": 0x2E, "left": 0x25, "up": 0x26, "right": 0x27,
      "down": 0x28, "space": 0x20, "backspace": 0x08}
# a-z 全部补齐。之前只写了 a/c/v/n 四个，按 Ctrl+Shift+M 时直接 KeyError('m')
# ——网页端也踩过同样的坑（Ctrl+Shift+O）。
for _i in range(26):
    VK[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(10):
    VK[str(_i)] = 0x30 + _i


class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    lii = _LII(ctypes.sizeof(_LII), 0)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


def _pid_of(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)


def _windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            out.append((int(hwnd), r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window():
    """按进程名找主窗口。不能按标题——codex 窗口也叫 ChatGPT，
    和 Chrome 的 `ChatGPT - Google Chrome` 会撞（v0.0.8 的基线就是这么废的）。"""
    want = TITLE_PROC.lower()
    best, area = None, 0
    for hwnd, _x, _y, w, h in _windows():
        if w < 400 or h < 300:
            continue
        if _proc_name(_pid_of(hwnd)).lower() != want:
            continue
        if w * h > area:
            area, best = w * h, hwnd
    return best


def rect_of(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    if r.right - r.left <= 0:
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def place(hwnd, pos, size):
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]),
                        int(size[0]), int(size[1]), SWP_NOACTIVATE | SWP_SHOWWINDOW)


def activate(hwnd, tries=3):
    """把窗口提到前台。返回是否真的提到了。

    SetForegroundWindow 会被**前台锁**拒绝：只有"当前前台进程"或"刚收到用户输入
    的进程"才能改前台。所以后台脚本直接调它经常无效。
    可靠做法是 AttachThreadInput 把自己的线程附到当前前台线程上，这样系统就当成
    同一个线程，SetForegroundWindow 才生效；调完立刻解除附加。

    2026-10-29：实测在斗鱼客户端占着前台时，只用 SetForegroundWindow 提不上来，
    附加线程后一次就成。
    """
    hwnd = int(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    for _ in range(tries):
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None)
        tid_me = kernel32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        try:
            user32.BringWindowToTop(wintypes.HWND(hwnd))
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            user32.SetFocus(wintypes.HWND(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.2)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


# ============================================================ 操作器

class Desk:
    def __init__(self, hwnd):
        self.hwnd = int(hwnd)
        self._clicks = []
        self._cache = (0.0, None)      # (时间戳, 整窗图)
        self._pw_ok = True             # PrintWindow 是否可用，坏了就退回流屏幕

    # ---- 截图 ----------------------------------------------------
    #
    # 两种抓法，优先 PrintWindow：
    #
    #   PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)
    #     让窗口自己绘制一份位图。**窗口被别的窗口盖住也照样是完整内容**。
    #     实测 Codex（Electron/Chromium）返回 1，四个模板全 1.0000。
    #     2026-10-29 之前的做法是 GetDC(0)+BitBlt 抓屏幕，微信盖住 Chrome 时
    #     截到的是微信，所有定位全建立在错误画面上，还点错了好几次。
    #
    #   BitBlt from screen（旧的）
    #     某些窗口不吃 PrintWindow（会返回 0 或全黑），这时退回来。
    #     代价是窗口被遮挡就抓不到真实内容，所以调用方仍应保证窗口在前台。

    def _printwindow(self):
        """整窗抓一张。失败返回 None。"""
        wr = rect_of(self.hwnd)
        if wr is None:
            return None
        _x, _y, w, h = wr
        if w <= 0 or h <= 0:
            return None
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        hdc = user32.GetWindowDC(self.hwnd)
        if not hdc:
            return None
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        ret = user32.PrintWindow(self.hwnd, mdc, 2)
        img = self._dib(mdc, bmp, w, h)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(self.hwnd, hdc)
        if not ret or img is None or img.size == 0:
            return None
        # 全黑/纯白 = 没画出来，当失败
        if int(img.max()) < 12 or int(img.min()) > 250:
            return None
        return img

    @staticmethod
    def _dib(mdc, bmp, w, h):
        class _BI(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]
        buf = ctypes.create_string_buffer(w * h * 4)
        bi = _BI(ctypes.sizeof(_BI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()

    def _screen_grab(self, region):
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            return np.zeros((0, 0, 3), np.uint8)
        hdc = user32.GetDC(0)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020)
        img = self._dib(mdc, bmp, w, h)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, hdc)
        return img

    def grab(self, region=None, *, fresh=False):
        """截窗口（或窗口内的一块）。region 用**屏幕坐标**，和原来一致。

        同一帧 0.25s 内复用：一次 locate() 要问四五个模板，逐个重新抓太浪费。
        点击/按键之后用 fresh=True 强制重抓。
        """
        wr = rect_of(self.hwnd)
        if region is None:
            region = wr
        if region is None:
            return np.zeros((0, 0, 3), np.uint8)
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            return np.zeros((0, 0, 3), np.uint8)

        if self._pw_ok:
            full = None
            ts, cached = self._cache
            if not fresh and cached is not None and time.time() - ts < 0.25:
                full = cached
            else:
                full = self._printwindow()
                if full is None:
                    self._pw_ok = False        # 这窗口不配合，后面都走屏幕抓
                else:
                    self._cache = (time.time(), full)
            if full is not None:
                dx, dy = x - wr[0], y - wr[1]
                sub = full[max(0, dy):dy + h, max(0, dx):dx + w]
                if sub.shape[0] == h and sub.shape[1] == w:
                    return sub
                # 请求区域超出窗口，退回屏幕抓
        return self._screen_grab(region)

    def _invalidate(self):
        """画面变了，下一帧必须重抓。点击/按键之后都要调。"""
        self._cache = (0.0, None)

    def _assert_fg(self):
        fg = user32.GetForegroundWindow()
        if fg == self.hwnd:
            return
        # 被别的程序抢回前台时，**先自己抢一次**再判断，不直接放弃。
        # 只重试一次：真抢不回来（用户正在操作那个窗口）就抛，绝不硬点。
        if activate(self.hwnd, tries=2) and user32.GetForegroundWindow() == self.hwnd:
            return
        raise RuntimeError(
            f"目标窗口不在前台（前台={user32.GetForegroundWindow()}，目标={self.hwnd}），"
            "已中止")

    def click(self, x, y):
        self._assert_fg()
        now = time.time()
        self._clicks = [t for t in self._clicks if now - t < 60]
        if len(self._clicks) >= CLICK_MAX:
            raise RuntimeError(f"60 秒内已点 {len(self._clicks)} 下，超过上限")
        if self._clicks:
            gap = CLICK_GAP - (now - self._clicks[-1])
            if gap > 0:
                time.sleep(gap)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        if abs(pt.x - int(x)) > 2 or abs(pt.y - int(y)) > 2:
            raise RuntimeError(f"光标没能移到 ({x},{y})，实际 ({pt.x},{pt.y})")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        self._clicks.append(time.time())
        self._invalidate()

    def press(self, key, times=1):
        self._assert_fg()
        vk = VK[key.lower()]
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.05)
        self._invalidate()

    def hotkey(self, *keys):
        self._assert_fg()
        codes = [VK[k.lower()] for k in keys]
        for c in codes:
            user32.keybd_event(c, 0, 0, 0)
            time.sleep(0.02)
        for c in reversed(codes):
            user32.keybd_event(c, 0, 2, 0)
            time.sleep(0.02)
        self._invalidate()

    def type_text(self, text, interval=0.004):
        """逐字符 SendInput UNICODE，中文也能打。"""
        self._assert_fg()

        class _M(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                        ("time", ctypes.c_uint), ("dwExtraInfo", ctypes.c_size_t)]

        class _K(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class _H(ctypes.Structure):
            _fields_ = [("uMsg", ctypes.c_uint), ("wParamL", ctypes.c_ushort),
                        ("wParamH", ctypes.c_ushort)]

        class _U(ctypes.Union):
            _fields_ = [("mi", _M), ("ki", _K), ("hi", _H)]

        class _IN(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", ctypes.c_uint), ("u", _U)]

        for ch in text:
            if ch == "\n":
                self.press("enter")
                continue
            code = ord(ch)
            for flags in (0x0004, 0x0004 | 0x0002):
                inp = _IN(type=1, u=_U(ki=_K(0, code, flags, 0, 0)))
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_IN))
                time.sleep(interval)
        self._invalidate()

    def set_clipboard(self, text):
        data = ctypes.create_unicode_buffer(text)
        user32.OpenClipboard(0)
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(0x0002, ctypes.sizeof(data))
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, ctypes.sizeof(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(13, h)
        finally:
            user32.CloseClipboard()

    def clipboard(self):
        user32.OpenClipboard(0)
        try:
            h = user32.GetClipboardData(13)
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def tpl(self, name):
        p = os.path.join(TEMPLATE_DIR, name + ".png")
        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"缺模板 {p}")
        return img

    def find(self, name, threshold=0.86, region=None):
        """返回 (屏幕x, 屏幕y, w, h, score) 或 None。"""
        img = self.grab(region)
        if img.size == 0:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        t = self.tpl(name)
        if t.shape[0] > gray.shape[0] or t.shape[1] > gray.shape[1]:
            return None
        res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, ml = cv2.minMaxLoc(res)
        if mx < threshold:
            return None
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return (ox + ml[0], oy + ml[1], t.shape[1], t.shape[0], float(mx))


# ============================================================ 定位

def _band(win):
    """底部工具条带（屏幕坐标）：窗口全宽 × 距底 BOTTOM_H 像素。

    纵向按**距底像素**而不是比例：实测三个尺寸下发送键距底恒为 41px，
    比例却在 0.954~0.969 之间漂。窗口高度变了比例就骗人，距底不会。
    """
    x, y, w, h = win
    top = y + max(0, h - BOTTOM_H)
    return (x, top, w, min(BOTTOM_H, h))


def _center(m, dx=0, dy=0):
    """Match 元组 (x,y,w,h,score) → 屏幕中心（可带小偏移）。"""
    return (m[0] + m[2] // 2 + dx, m[1] + m[3] // 2 + dy)


def locate(d, win, *, need_composer=True):
    """定位控件。**每个坐标都来自一次模板命中**，不做任何偏移推算。

    改之前是这样：SEND_REL 按比例猜发送键 → CHIP_DX=-139 减出芯片 →
    (COMPOSER_DX,-58) 减出输入框。窗口尺寸或 UI 一改，三个一起失效，
    而且失效时不会报错，只会安静地点空。

    现在每个控件都有自己的图，在窗口里找出来：
        cdx_send    橙圆+白三角的发送键          底部条带内找
        cdx_arrow   芯片右端的 ⌄ 箭头            底部条带内找（文字会变，箭头不变）
        cdx_plus    输入框左下角的 + 按钮        底部条带内找
        cdx_mic     麦克风                      只用于判断发送键是不是变成了麦克风

    **输入框本身没有模板**，因为它是纯白圆角矩形，一点纹理都没有，裁不出可匹配的图
    （试过裁左上角圆角：输入框一获得焦点，光标就出现在那里，min 从 199 掉到 26，
    模板当场失效）。所以只能借它内部唯一有特征的部件——加号——来落点：
    加号永远在输入框内部的左下角，它正上方 45px 必然还在框里。
    这是"在已定位的容器内部取一个安全落点"，不是"用偏移推另一个控件的位置"。

    返回 dict，缺任何一个都返回 None 并带上 missing 字段说明缺谁。
    """
    band = _band(win)
    out = {"missing": []}

    m_send = d.find(SEND_TPL, threshold=TPL_THR, region=band)
    if m_send is None:
        out["missing"].append(SEND_TPL)
    else:
        out["send"] = _center(m_send)

    m_mic = d.find(MIC_TPL, threshold=TPL_THR, region=band)
    out["mic"] = _center(m_mic) if m_mic else None

    if need_composer:
        m_box = d.find(COMPOSER_TPL, threshold=TPL_THR, region=band)
        if m_box is None:
            out["missing"].append(COMPOSER_TPL)
        else:
            # 模板就是「加号+上方空白」，命中矩形的中心已经在输入框里，不加任何偏移
            out["composer"] = _center(m_box)

    return out


def probe(d, win):
    """把每个模板的命中情况打出来（诊断用，不点击）。"""
    band = _band(win)
    rows = []
    for name, reg in ((SEND_TPL, band), (MIC_TPL, band),
                      (PLUS_TPL, band), (COMPOSER_TPL, band)):
        m = d.find(name, threshold=0.50, region=reg)
        rows.append((name, None if m is None else
                     (round(m[4], 4), (m[0] - win[0] + m[2] // 2,
                                       m[1] - win[1] + m[3] // 2))))
    return rows



def find_slider(d, win, send=None):
    """强度滑杆。返回 (left, right, row_y, knob_x)（窗口内坐标），None = 没找到。

    **完全不依赖填充色**——这点是最关键的教训：填充色随档位变。
    低档是橙色，最高档（Ultra）是**紫色渐变**（2026-10-29 实测截图），
    按橙色找会在最高档直接返回 None，表现是"弹层明明开着却说没找到滑杆"。

    只用两件与档位无关的事实：
      1. **轨道**：一条宽 140~360、高 16~34 的横条（灰度 <250，与白背景不同）；
      2. **滑块**：轨道行上最宽的那段 >250 的纯白，宽 16~44。
         滑块直径比轨道高、颜色永远是纯白，这两点在所有档位下都一样。
    排除芯片：芯片也是浅灰胶囊，但里面有深色文字（min<100），轨道行没有。
    """
    img = d.grab(win)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h_img, w_img = g.shape
    sx = (send[0] - win[0]) if send is not None else int(w_img * 0.85)
    y_lo, y_hi = max(0, h_img - 380), max(0, h_img - 30)
    # 右边界只留 5px 给发送键：滑块拖到最右时右边缘会超出轨道灰段
    # （实测轨道灰段到 1136，滑块到 1160），卡在 sx-30 会把滑块切掉一半。
    x_lo, x_hi = max(0, sx - 340), min(w_img, sx - 5)
    if x_hi - x_lo < 200 or y_hi - y_lo < 60:
        return None

    band = g[y_lo:y_hi, x_lo:x_hi] < 250
    counts = band.sum(axis=1)
    cand = np.where(counts >= 90)[0]
    rows = []
    for yy in cand:
        row = band[yy]
        idx = np.where(row)[0]
        if len(idx) < 90:
            continue
        span = int(idx[-1] - idx[0] + 1)
        if not (140 <= span <= 360):
            continue
        if len(idx) / span < 0.55:
            continue
        rows.append((y_lo + int(yy), x_lo + int(idx[0]), x_lo + int(idx[-1])))
    if len(rows) < 12:
        return None

    groups, cur = [], [rows[0]]
    for r in rows[1:]:
        if r[0] == cur[-1][0] + 1:
            cur.append(r)
        else:
            groups.append(cur)
            cur = [r]
    groups.append(cur)
    # 高度 16~28 先排掉「Select effort」芯片（实测轨道 24、芯片 31）。
    # 不能按"最暗值"排：紫色轨道最暗 65、芯片文字 56，挨得太近；
    # 也不能按均值排：最低档时轨道是浅灰，均值和芯片差不多。
    pick = [gp for gp in groups if 16 <= len(gp) <= 28]
    if not pick:
        return None

    def _knob_of(gp):
        """在这一组中间行上找滑块：最宽的 >250 白色段，宽 16~44。

        范围一直扫到 x_hi，不能只扫到轨道灰段右端——滑块拖到最右会伸出去。
        弹层底色白是连续几百像素，被上限 44 筛掉；
        紫色区域里的白色小装饰点宽 2~6px，被下限 16 筛掉。
        """
        ry, l, _r = gp[len(gp) // 2]
        line = g[ry]
        best, st = None, None
        for i in range(l, x_hi + 1):
            v = line[i] if i < w_img else 0
            if v > 250:
                if st is None:
                    st = i
            elif st is not None:
                w = i - st
                if 16 <= w <= 44 and (best is None or w > best[1] - best[0]):
                    best = (st, i - 1)
                st = None
        return best

    # **必须同时满足"形状像轨道"和"行上有滑块"**。
    # 只按形状选会选错：实测 y=1204 那组（高 27，白段宽 70）也像轨道，
    # 而且高度比真轨道(24)还大，max(key=len) 就挑中它，然后滑块找不到、返回 None。
    cands = []
    for gp in pick:
        b = _knob_of(gp)
        if b is not None:
            cands.append((gp, b))
    if not cands:
        return None
    gp, best = max(cands, key=lambda x: len(x[0]))
    row_y, left, right = gp[len(gp) // 2]
    knob = (best[0] + best[1]) // 2
    left = min(left, best[0])
    right = max(right, best[1])
    return (left, right, row_y, int(knob))


def items_visible(d, win):
    hit = 0
    for name in MODELS.values():
        if d.find(name, MODEL_THR, win):
            hit += 1
            if hit >= 2:
                return True
    return False


def list_band(d, win):
    """模型列表的纵向范围：所有能匹配到的模型项，上下各留 30px。

    **不要"按间距分组、取最长那组"**：实测列表会被断成
    [1007,1058,1089,1120] 和 [1215,1246] 两组（Default 是两行文字、个别模板偏几位），
    取最长那组后 band 只到 y=1148，而勾在 1141..1161 —— 差 13px 就落在范围外，
    于是明明列表开着、勾也在，却报"找不到勾"。
    改成直接取 min/max，不给它分组的机会。
    """
    ys = []
    for name in MODELS.values():
        m = d.find(name, MODEL_THR, win)
        if m:
            ys.append(m[1] + m[3] // 2)
    if len(ys) < 2:
        return None
    top = max(win[1], min(ys) - 30)
    bottom = min(win[1] + win[3], max(ys) + 34)
    if bottom - top < 40:
        return None
    return (win[0], top, win[2], bottom - top)


# ============================================================ 选模型

def enter_list(d, win):
    """进模型列表。三种状态分别处理：
       已经在列表层   -> 直接返回
       在强度层       -> 点「模型名 ›」那一行
       全关           -> 先开弹层，再点那一行
    """
    if items_visible(d, win):
        return True
    if not _menu_open(d, win):
        if not _open_effort(d, win):
            return False
    sl = find_slider(d, win, locate(d, win).get("send"))
    if sl is None:
        return False
    left, right, row_y, _knob = sl
    d.click(win[0] + (left + right) // 2, win[1] + row_y - SLIDER_ABOVE)
    time.sleep(1.3)
    return items_visible(d, win)


def read_model(d, win):
    band = list_band(d, win)
    if band is None:
        return {"model": "unknown", "hint": "列表里一项都没匹配到"}
    chk = d.find(CHECK_TPL, 0.80, band)
    if chk is None:
        return {"model": "unknown", "hint": "找不到勾"}
    cy = chk[1] + chk[3] // 2
    best, bd = None, 10 ** 9
    for key, name in MODELS.items():
        m = d.find(name, MODEL_THR, band)
        if m is None:
            continue
        dist = abs(m[1] + m[3] // 2 - cy)
        if dist < bd:
            bd, best = dist, key
    if best is None or bd > 16:
        return {"model": "unknown", "hint": f"勾不在任何一行上（最近 {bd}px）"}
    return {"model": best, "dist": bd}


def set_model(d, win, key):
    if key in UNREACHABLE:
        return {"ok": False, "stage": "unreachable",
                "hint": UNREACHABLE[key] + "；列表固定高度且不响应滚轮"}
    if key not in MODELS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(MODELS)}
    if not enter_list(d, win):
        return {"ok": False, "stage": "list_not_open"}
    before = read_model(d, win)
    if before.get("model") == key:
        return {"ok": True, "stage": "already", "before": key, "after": key}

    band = list_band(d, win)
    if band is None:
        return {"ok": False, "stage": "list_band_none"}
    chk = d.find(CHECK_TPL, 0.80, band)
    keys = list(MODELS)
    if chk is not None and before.get("model") in keys:
        # 用「勾的位置 + 固定行距 31px」推算目标行。
        # 不做整窗模板搜索：列表位置每次都变，模板分数会掉到 0.88，
        # 而 GPT-6.1 Sol 和 GPT-6 Sol 文字极像，整窗搜会串味
        # （实测点了 gpt6_sol 却切成了 gpt61_sol）。
        cur_idx = keys.index(before["model"])
        tgt_idx = keys.index(key)
        cy = chk[1] + chk[3] // 2 + (tgt_idx - cur_idx) * ITEM_GAP
        m = d.find(MODELS[key], MODEL_THR, band)
        cx = (m[0] + m[2] // 2) if m else (band[0] + 60)
        d.click(cx, cy)
    else:
        m = d.find(MODELS[key], MODEL_THR, band)
        if m is None:
            return {"ok": False, "stage": "item_not_visible", "want": key}
        d.click(m[0] + m[2] // 2, m[1] + m[3] // 2)
    # 点完模型项，弹层会收起。这时直接点芯片会被"关闭动画"吃掉
    # （实测 reverify_failed）。先点一下输入框空白：既把残余弹层关掉，
    # 又把键盘焦点还给页面，比按 Esc 稳（Esc 会吞掉下一次点击）。
    time.sleep(1.0)
    pm = locate(d, win)
    if "composer" in pm:
        d.click(*pm["composer"])
    time.sleep(1.2)

    if not enter_list(d, win):
        return {"ok": False, "stage": "reverify_failed", "before": before.get("model")}
    after = read_model(d, win)
    ok = after.get("model") == key
    return {"ok": ok, "before": before.get("model"), "after": after.get("model"),
            "want": key, "stage": "done" if ok else "unverified"}


# ============================================================ 调强度

def _menu_open(d, win) -> bool:
    """弹层（强度层）开着吗 = 芯片位置是不是显示「Select effort」。

    阈值 0.82 而不是 0.90：实测同一段文字在不同模型/不同次渲染下会偏移几个像素，
    分数落在 0.88（0.90 会判成"没开"，于是按快捷键又把弹层关掉，进列表必失败）。
    弹层真关掉时这个模板直接 None（<0.5），所以 0.82 有足够余量。
    """
    return d.find(MENU_TPL, 0.82, _band(win)) is not None


def _open_effort(d, win):
    """把强度弹层打开。**只开一次，不反复开关。**

    弹层是模态的：点它外面任何地方都会收起来。所以调用方拿到打开的弹层后，
    要一口气把要读的、要改的都做完，最后才 Esc。

    打开顺序：
      1. 先直接按 Ctrl+Shift+M —— 输入框通常已经聚焦，**不产生任何点击**；
      2. 没反应才点一次输入框再按。
    不做"Esc 清场 + 重试"：Esc 会关掉已开的弹层、再按又开，表现就是来回闪。
    """
    if _menu_open(d, win):
        return True
    d.hotkey(*CHIP_HOTKEY)
    time.sleep(1.2)
    if _menu_open(d, win):
        return True
    pm = locate(d, win)
    if "composer" not in pm:
        return False
    d.click(*pm["composer"])
    time.sleep(0.35)
    d.hotkey(*CHIP_HOTKEY)
    time.sleep(1.4)
    return _menu_open(d, win)


def _effort_now(d, win, sl):
    """从**已经打开**的弹层读档位（不开关弹层）。

    比例用**滑块圆心**在轨道上的位置，不用填充右端——填充右端是滑块左边缘，
    差一个半径（13px），在 200px 的轨道上就是 6%，足够把 High 读成 mid。
    """
    left, right, _row_y, knob = sl
    frac = (knob - left) / max(1, right - left)
    best, bd = EFFORTS[0], 9.9
    for k in EFFORTS:
        d_ = abs(frac - EFFORT_FRAC[k])
        if d_ < bd:
            bd, best = d_, k
    out = {"effort": best, "label": EFFORT_LABEL[best],
           "knob": knob, "track": [left, right], "frac": round(frac, 3)}
    if best == "low":
        out["at_min"] = True
    return out


def read_effort(d, win):
    """读强度：开一次弹层 -> 读 -> 关。"""
    if not _open_effort(d, win):
        return {"effort": "unknown", "hint": "打不开强度弹层"}
    sl = find_slider(d, win, locate(d, win).get("send"))
    out = _effort_now(d, win, sl) if sl else {"effort": "unknown",
                                              "hint": "没找到滑杆"}
    d.press("esc")
    time.sleep(0.35)
    return out


def set_effort(d, win, key):
    """调强度。**开一次弹层，一次做完，靠"读到位置就停"而不是数按键次数。**

    1. 打开弹层（快捷键，不点芯片）
    2. 读当前档位
    3. 点一下滑块（滑块在弹层内部，点它不会收起）
    4. 左键按到底归零
    5. 再按右键**逐步逼近目标位置**：每按一次重新读滑块圆心，到目标就停
    6. 弹层还开着，直接再读一次确认
    7. 最后才 Esc（Esc 才让强度真正生效）

    为什么不用"按 N 次 = 第 N 档"：实测第一次按右键会跳两档
    （0.005 -> 0.369），后面每次约一档，数次数必然偏
    （按 4 次想要最高档，实际停在 0.78）。

    不拖：拖 -52px 实际走 -82px，而且连续拖同一控件第二次就不动。
    """
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": EFFORTS}
    if not _open_effort(d, win):
        return {"ok": False, "stage": "menu_not_open"}

    send = locate(d, win).get("send")
    sl = find_slider(d, win, send)
    if sl is None:
        return {"ok": False, "stage": "no_slider"}
    before = _effort_now(d, win, sl)

    left, right, row_y, knob = sl
    d.click(win[0] + knob, win[1] + row_y)
    time.sleep(0.45)

    span = max(1, right - left)
    target = left + span * EFFORT_FRAC[key]
    if key == "max":
        target = right - 5                    # 最高档：顶到最右
    if key == "low":
        target = left + 5

    for _ in range(9):                        # 归零
        d.press("left")
        time.sleep(0.2)

    for _ in range(12):                       # 往右逼近
        cur = find_slider(d, win, send)
        if cur is None or cur[3] >= target - 5:
            break
        d.press("right")
        time.sleep(0.26)
    for _ in range(8):                        # 超了往回退
        cur = find_slider(d, win, send)
        if cur is None or cur[3] <= target + 5:
            break
        d.press("left")
        time.sleep(0.26)

    sl2 = find_slider(d, win, send)
    after = _effort_now(d, win, sl2) if sl2 else {"effort": "unknown"}
    d.press("esc")
    time.sleep(0.45)

    ok = after.get("effort") == key
    return {"ok": ok, "before": before.get("effort"), "want": key,
            "after": after.get("effort"), "knob": after.get("knob"),
            "target": int(target), "stage": "done" if ok else "unverified"}


# ============================================================ 提问

def ask(d, win, prompt, timeout=180.0):
    pos = locate(d, win)
    if pos["missing"]:
        return {"ok": False, "stage": "controls_not_found", "missing": pos["missing"]}
    d.click(*pos["composer"])
    time.sleep(0.4)
    d.hotkey("ctrl", "a")
    time.sleep(0.1)
    d.press("delete")
    time.sleep(0.15)
    d.type_text(prompt)
    time.sleep(0.5)
    pos = locate(d, win)
    if "send" not in pos:
        return {"ok": False, "stage": "no_send_button"}
    d.click(*pos["send"])

    # 等生成结束：发送键从「生成中的方块」恢复成橙色圆钮
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2.0)
        p = locate(d, win)
        if "send" not in p:
            continue
        img = d.grab(win)
        if img.size == 0:
            continue
        x, y = p["send"][0] - win[0], p["send"][1] - win[1]
        patch = img[max(0, y - 12):y + 12, max(0, x - 12):x + 12]
        if patch.size == 0:
            continue
        b = patch[:, :, 0].astype(int)
        r = patch[:, :, 2].astype(int)
        if int(((r - b) > 60).sum()) > 60:
            break
    else:
        return {"ok": False, "stage": "timeout", "hint": f"{timeout}s 内没生成完"}

    time.sleep(1.0)
    return {"ok": False, "stage": "need_copy_template",
            "hint": "回答已生成，但 codex 的复制按钮要 hover 才出现，模板还没裁"}


# ============================================================ CLI

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--probe", action="store_true",
                    help="只打印每个模板的命中分数和窗口内坐标，不点击")
    ap.add_argument("--size", default=None, help="WxH，默认 1380x1340")
    ap.add_argument("--pos", default=None, help="X,Y，默认 -1440,-560")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--idle", type=float, default=2.0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    size, pos = WIN_SIZE, WIN_POS
    if a.size:
        try:
            w, h = a.size.lower().split("x")
            size = (int(w), int(h))
        except ValueError:
            print("--size 要写成 1380x1340")
            return 2
    if a.pos:
        try:
            x, y = a.pos.split(",")
            pos = (int(x), int(y))
        except ValueError:
            print("--pos 要写成 -1440,-560")
            return 2

    if not a.prompt and not a.list and not a.model and not a.effort and not a.probe:
        print(__doc__)
        print("可选模型：" + ", ".join(MODELS))
        print("看不到的：" + ", ".join(UNREACHABLE))
        print("可选强度：" + ", ".join(EFFORTS))
        return 2

    if not a.force:
        end = time.time() + a.wait
        while time.time() < end:
            if idle_seconds() >= a.idle:
                break
            time.sleep(0.2)
        else:
            print(json.dumps({"ok": False, "stage": "user_busy",
                              "hint": f"{a.wait}s 内你一直在用电脑"}, ensure_ascii=False))
            return 4

    hwnd = find_window()
    if hwnd is None:
        print("启动 codex …")
        subprocess.Popen(["explorer.exe", LAUNCH])
        end = time.time() + 30
        while time.time() < end and hwnd is None:
            time.sleep(1.0)
            hwnd = find_window()
    if hwnd is None:
        print(json.dumps({"ok": False, "stage": "no_window"}, ensure_ascii=False))
        return 1

    # 默认**不动**窗口。定位靠模板匹配，所以窗口在哪、多大都不影响
    # （实测 1380x1340 / 1150x1050 / 980x900 三档下发送键都命中 1.000）。
    # 只有显式给了 --size / --pos 才去摆——那是"你想让它放在哪"，不是定位需要。
    if a.size or a.pos:
        place(hwnd, pos, size)
        time.sleep(0.8)

    win = rect_of(hwnd)
    if win is None:
        print(json.dumps({"ok": False, "stage": "no_rect"}, ensure_ascii=False))
        return 1
    if win[2] < MIN_W and not a.probe:
        print(json.dumps({"ok": False, "stage": "window_too_narrow",
                          "size": [win[2], win[3]], "min_w": MIN_W,
                          "hint": "窗口太窄时 Codex 会挤压布局，芯片箭头模板会失准"
                                  "（实测 980 宽时掉到 0.834）。拉宽到 1050 以上，"
                                  "或 --size 1380x1340"},
                         ensure_ascii=False))
        return 1

    d = Desk(hwnd)
    pm = locate(d, win, need_composer=not a.probe)

    if a.probe:
        # 只读诊断：不需要前台，也不需要控件齐全
        rows = probe(d, win)
        print(json.dumps({
            "window": list(win),
            "target_threshold": TPL_THR,
            "templates": [{"name": n,
                           "score": None if v is None else v[0],
                           "at_window": None if v is None else list(v[1]),
                           "hit": (v is not None and v[0] >= TPL_THR)}
                          for n, v in rows],
            "missing": pm["missing"],
        }, ensure_ascii=False, indent=2))
        return 0

    if pm["missing"]:
        print(json.dumps({"ok": False, "stage": "controls_not_found",
                          "missing": pm["missing"],
                          "hint": "模板没命中。多半是窗口太窄 / 换了主题 / UI 改版。"
                                  "先跑 --probe 看每个模板的实际分数"},
                         ensure_ascii=False))
        return 1

    # 下面要点击了，这时才必须前台
    if not activate(hwnd):
        # 提不到前台就**直接退出**，不要继续。系统前台锁（别的程序占着前台）时
        # SetForegroundWindow 会被拒，这时继续操作等于闭着眼睛点。
        print(json.dumps({"ok": False, "stage": "not_foreground",
                          "hint": "窗口提不到前台（你可能正在用别的窗口），已退出，不硬点"},
                         ensure_ascii=False))
        return 4
    time.sleep(0.8)
    d.grab(fresh=True)                 # 置前后重新抓一帧
    win = rect_of(hwnd)
    pm = locate(d, win, need_composer=False)
    if pm["missing"]:
        print(json.dumps({"ok": False, "stage": "controls_not_found",
                          "missing": pm["missing"]}, ensure_ascii=False))
        return 1
    rc = 0

    try:
        if a.list:
            # **只开一次弹层**：先读强度（此时是强度层），再点入口行进列表读模型，
            # 最后 Esc 收掉。原来读模型开一次、读强度又开一次，等于开关两遍。
            out = {}
            if not _open_effort(d, win):
                out = {"model": "unknown", "effort": "unknown",
                       "hint": "打不开强度弹层"}
            else:
                send = locate(d, win).get("send")
                sl = find_slider(d, win, send)
                if sl is not None:
                    out.update(_effort_now(d, win, sl))
                    left, right, row_y, _knob = sl
                    d.click(win[0] + (left + right) // 2,
                            win[1] + row_y - SLIDER_ABOVE)
                    time.sleep(1.3)
                if items_visible(d, win):
                    out.update(read_model(d, win))
                    if out.get("model") == "unknown":
                        # 失败时留一张现场图，省得再点一遍去复现
                        dbg = os.path.join(DEBUG_DIR, "codex_list_fail.png")
                        cv2.imwrite(dbg, d.grab(win, fresh=True))
                        out["debug_shot"] = dbg
                else:
                    dbg = os.path.join(DEBUG_DIR, "codex_list_fail.png")
                    cv2.imwrite(dbg, d.grab(win, fresh=True))
                    out["model"] = "unknown"
                    out["hint"] = "打不开模型列表"
                    out["debug_shot"] = dbg
                d.press("esc")
                time.sleep(0.4)
            print(json.dumps(out, ensure_ascii=False))
            return 0

        if a.model:
            r = set_model(d, win, a.model)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
        if a.prompt:
            r = ask(d, win, a.prompt, a.timeout)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
    except RuntimeError as e:
        print(json.dumps({"ok": False, "stage": "aborted", "hint": str(e)},
                         ensure_ascii=False))
        return 4
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
