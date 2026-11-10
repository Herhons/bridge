"""codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 调强度 + 提问收回答。

**定位主体使用 cv2/numpy/ctypes；运行入口共用 qi 的锁和自身输入标记。**

用法：
    python codex_use.py "只回复两个字：收到"
    python codex_use.py --list                          读当前模型和强度
    python codex_use.py --model gpt6_sol                只切模型
    python codex_use.py --effort high                   只调强度
    python codex_use.py --model gpt6_sol --effort ultra 两个都调
    python codex_use.py --model gpt6_sol --effort ultra "问题"
    python codex_use.py --probe                         只报每个模板命中在哪，不点击

强度六档（2026-10-29 逐档实测，滑杆上那 5 个小灰点就是其余 5 档）：
    light / medium / high / xhigh(Extra High) / max / ultra
    low、mid 是 light、medium 的旧别名。

模型（列表一屏只能露 7 项，最后两项露不出来）：
    default / gpt61_sol / gpt6_astra / gpt61_terra / gpt6_sol / gpt6_luna

## 控件怎么定位（这里的方法决定了它有多耐改版）

**能裁模板的控件一律裁模板，运行时在窗口里找出来、点命中的位置。**
没有任何「发送键左边 N 像素是芯片」这种推算。

    cdx_send          橙圆+白三角的发送键
    cdx_mic           麦克风（同时当芯片的右锚点）
    cdx_plus          输入框左下角的 + 按钮
    cdx_composer      「+ 及上方空白」，命中中心就是输入框内的落点
    cdx_select_effort 卡片开着时芯片上的占位文字「Select effort」
    cdx_select_model  卡片开着时芯片上的「Select model」（Default 自动模式）
    cdx_r_light ...   卡片顶部那六个档位词，用来**读**当前档位

芯片（`GPT-6 Astra Light ⌄`）关着时是**一串纯文字、没有胶囊底色**，文字随模型
和档位变，裁不出固定模板。它的定位办法见 chip_point()：以麦克风为右锚点，
取它左边条带里最靠右的一小撮深色像素——那就是 ⌄，也就是芯片按钮本身。

窗口位置变了没事（每次摆到 --pos）；窗口尺寸变了也能用——模板匹配找的是图形，
不是坐标。唯一要求是 UI 的**缩放比例**没变（系统 DPI 缩放、浏览器缩放会改变像素大小，
那时模板要按新比例重裁）。换环境先跑 `--probe`，它会打印每个模板的实际分数，
哪个 < 阈值一眼就能看出来。

## 界面结构（2026-10-29 实测，窗口 1380x1340）

输入框在底部，右下角：`GPT-6 Astra Ultra ⌄`  麦克风  ●（橙色发送键）
**点一下芯片**弹出卡片（不用快捷键：Ctrl+Shift+M 只在输入框有焦点时才生效，
焦点不在那儿就毫无反应，而且失败时看不出是没按对还是没打开）。

卡片有两种形态，**用芯片上的字区分**：

  形态 A：选的是**具体模型**，芯片写「Select effort」
    ┌─────────────────────────┐
    │ ⚡        Ultra        ↻ │   ← 大号档位词（橙色），就是读数依据
    │        GPT-6 Astra ›    │   ← 点这一行进模型列表
    │  ●━━━━━━━━━━━━━━━━━━    │   ← 独立强度滑杆（颜色随档位变，橙/紫都有）
    └─────────────────────────┘

  形态 B：选的是 **Default（自动）**，芯片写「Select model」
    ┌─────────────────────────┐
    │ ⚡   GPT-6 Astra Medium ›│   ← 模型和档位合成一行
    │  ●━━━━━━━━━━━━━━━━━━    │   ← **不是**强度滑杆，是「模型+强度」预设条
    └─────────────────────────┘
    这条预设条左右键是在组合之间换（Luna High → Sol Light → …），
    所以 Default 模式下**没有独立强度可调**，--effort 会明确拒绝。

卡片是**模态**的：点外面任何地方就收起。所以每次操作只开一次，
读完/改完再 Esc，绝不反复开关。

## 已知限制

1. 模型列表有固定最大高度，一屏最多露 7 项。最后两项（GPT-5.6 Terra / GPT-5.6 Luna）
   既露不出来、滚轮也无效（实测滚 9 格纹丝不动，窗口 1340 拉到 1900 也一样），
   会明确报 unreachable。
2. Default（自动）模型下调不了强度：那种卡片里没有独立滑杆（见上）。先用 --model
   选一个具体模型。
3. 取回答还没接上：codex 的复制按钮要 hover 才出现，模板还没裁。
   现在 `"问题"` 会发出去、等生成结束，但取回会返回 need_copy_template。

## 2026-11-02 CARD-07 第 3 条：新版 Codex 找不到 `cdx_composer`

现象：新版把输入框和「+」的间距、圆角改了，`cdx_composer`（加号 + 上方 26px 空白）
匹不上，于是 `controls_not_found` 直接退出——而且是在**置前之前**就退出，
越找不到越不获焦。现在按卡面把顺序倒过来：

    1  先恢复（最小化就 restore，恢复不了退 7）+ 置前；
    2  模板找不到 composer 时，用 UIA 在**下半窗**找编辑框、**点它一下让它获焦**，
       再找一次模板（输入框失焦/聚焦的画法不一样，先把这个变量消掉）；
    3  模板仍然不认，就用 UIA 那个落点（`composer_basis=uia`，写进结果留证）。

发送键还是只认模板：点错发送键比找不到更糟。
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
# **同一个橙圆按钮有两种图标**：输入框有内容时是白色三角（发送），
# 空的时候是白色声波竖条（语音输入）。实测声波那一版在 cdx_send 模板上
# 能匹到 0.947 —— 用 TPL_THR=0.90 会把它当成发送键，点下去就**开始录音**。
# 所以"真的要按发送"这一步必须用 0.97 这个更严的阈值（真三角实测 1.000）。
SEND_THR = 0.97

# 强度弹层**直接点芯片**打开，不用快捷键。
# （Ctrl+Shift+M 只在输入框有焦点时才生效，焦点在别处就毫无反应，而且失败时
#  看不出来到底是没按对还是弹层没开。点击没有这个歧义。）
#
# 芯片关着的时候是「模型名 档位 ⌄」一串**纯文字、没有胶囊底色**，文字随模型和
# 档位变，裁不出固定模板；而 ⌄ 只有 6x4 像素，单独当模板又太弱。
# 所以用**几何 + 右锚点**定位（见 chip_point）：芯片永远紧贴在麦克风左边，
# ⌄ 是这串文字里最后一个字形，跟麦克风之间有一小段留白。
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
#     连芯片都不点，直接读错位置（实测 --list 读到 effort=light，实际是全关状态）；
#   · 不能用"橙色填充"：最高档是紫色渐变，压根没有橙色。
# 实测：弹层开着 1.000，关掉后直接 None。
MENU_TPL = "cdx_select_effort"
# 卡片还有第二种画法：芯片变成「Select model」。
# **什么时候出现**：模型选的是 Default（自动）时。这时卡片里没有独立强度滑杆，
# 只有一条「模型名 强度 ›」+ 一条**预设滑条**（左右键是在「模型+强度」的组合之间
# 换，实测一路按右键会看到 Luna High -> Sol Light -> Sol Medium -> Astra Light ...）。
# 选了具体模型之后卡片才变成形态 A（顶行档位词 + 模型行 + 独立强度滑杆），
# 芯片也随之变回「Select effort」。
# 所以：**判形态 = 判有没有独立强度滑杆**，只认 "Select effort" 会把 Default 下
# 开着的卡片当成关着，再点一下芯片反而把它点关 —— 表现就是"点来点去弹层一闪一闪"。
MODEL_MENU_TPL = "cdx_select_model"
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

SLIDER_ABOVE = 57          # 形态 A：模型行在**滑杆中心行**上方 57px（实测）
ROW_ABOVE_PRESET = 40      # 形态 B：那一行在预设滑条中心行上方 40px（实测）
# 两种形态卡片高度不同，偏移也跟着不同。这个差值是**卡片内部**的排版常量
# （卡片贴着芯片长上去，尺寸固定），和"用窗口比例猜控件位置"是两回事。
ROW_ABOVE = {"effort": SLIDER_ABOVE, "preset": ROW_ABOVE_PRESET}
ITEM_GAP = 31              # 模型列表行距
CLICK_MAX, CLICK_GAP = 14, 0.4

EFFORTS = ["light", "medium", "high", "xhigh", "max", "ultra"]

# 各档位在轨道上的相对位置（0=最左 1=最右），**实测值，不是等分猜测**。
# 2026-10-29 定位了六个停靠点（滑杆上那 5 个小灰点 = 除当前档外的其余 5 档）：
#   knob 950 / 992 / 1032 / 1073 / 1114 / 1155  ->  Light / Medium / High /
#   Extra High / Max / Ultra（档位字是橙色，Ultra 时轨道变成紫渐变）
# 用 (knob-left)/(right-left) 换算，left=923 right=1167。
#
# **原来这里只有 5 档是错的**：把 Extra High 和 Max 挤成了一档，
# 于是 --effort max 永远到不了真正的 Max（停在 Extra High），Ultra 反而被叫成 max。
EFFORT_FRAC = {"light": 0.111, "medium": 0.283, "high": 0.447,
               "xhigh": 0.615, "max": 0.783, "ultra": 0.951}
EFFORT_LABEL = {"light": "Light", "medium": "Medium", "high": "High",
                "xhigh": "Extra High", "max": "Max", "ultra": "Ultra"}

# 老名字（low/mid）保留成别名，省得已经写好的调用方全改。
EFFORT_ALIAS = {"low": "light", "mid": "medium", "med": "medium",
                "extrahigh": "xhigh", "extra_high": "xhigh", "xhigh": "xhigh"}

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
    from qi.guard import idle_seconds as shared_idle
    return shared_idle()


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


def _win32_size_api():
    """最小化相关入口：句柄必须声明成 HWND，否则 64 位句柄会 OverflowError。"""
    if getattr(_win32_size_api, "ready", False):
        return
    user32.IsIconic.argtypes, user32.IsIconic.restype = [wintypes.HWND], wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    _win32_size_api.ready = True


_CURRENT_HWND = {"v": None}   # 失败留证用：当前操作的窗口
SENTINEL = -32000           # 最小化时 GetWindowRect 的哨兵坐标
MIN_WIN_W, MIN_WIN_H = 200, 120


def _raw_rect(hwnd):
    r = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def _usable_rect(rect):
    """不是哨兵、宽高过阈值。只判 w>0 不够：最小化时是 160x28 这种"小但正"的尺寸。"""
    if not rect:
        return False
    x, y, w, h = rect
    return x > SENTINEL + 1 and y > SENTINEL + 1 and w >= MIN_WIN_W and h >= MIN_WIN_H


def rect_of(hwnd):
    """窗口矩形。最小化先恢复并**轮询确认**（CARD-06a (c) / 06b 第 7 条）。

    **统一走 `qi.window.restore`**：网页端和四个桌面端用的是同一份恢复实现，
    判据只有一条——IsIconic 变假、矩形脱离 -32000 哨兵、宽高过阈值。
    恢复不了就把原矩形（可能是哨兵）还回去，由调用方按退出码 7 处理，
    不在"取矩形"这一步抛异常。
    """
    if not hwnd:
        return None
    r = _raw_rect(hwnd)
    if r is not None and _usable_rect(r):
        return r
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import window as window_mod
        facts = window_mod.restore(int(hwnd), timeout=5.0)
        if facts["ok"]:
            return tuple(facts["rect"])
    except Exception:                       # noqa: BLE001 - 退回原判据，别让取矩形带崩
        pass
    return r

def place(hwnd, pos, size):
    user32.SetWindowPos(int(hwnd), 0, int(pos[0]), int(pos[1]),
                        int(size[0]), int(size[1]), SWP_NOACTIVATE | SWP_SHOWWINDOW)


def activate(hwnd, tries=3):
    # B15：并发下抢前台是共享资源。统一走 desk.ensure_foreground
    # （进 section → SW_RESTORE → SetFg → Alt 解前台锁 → topmost 闪 → 重试）。
    # 只改函数体：名字、签名、调用点都不动（`test_desktop_controls` 按名字打桩）。
    from qi import desk as _desk
    return _desk.ensure_foreground(hwnd, tries=max(3, int(tries)))



# ============================================================ 操作器

class Desk:
    def _mark_input(self):
        from qi.guard import mark_synthetic
        mark_synthetic("desk")

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
        if fg != self.hwnd:
            raise RuntimeError(f"目标窗口不在前台（前台={fg}，目标={self.hwnd}），已中止")

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
        self._mark_input()
        self._invalidate()

    def press(self, key, times=1):
        self._assert_fg()
        vk = VK[key.lower()]
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.05)
        self._mark_input()
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
        self._mark_input()
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

        for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
            self._assert_fg()
            if ch == "\n":
                self.hotkey("shift", "enter")
                continue
            encoded = ch.encode("utf-16-le")
            for i in range(0, len(encoded), 2):
                code = int.from_bytes(encoded[i:i + 2], "little")
                for flags in (0x0004, 0x0004 | 0x0002):
                    inp = _IN(type=1, u=_U(ki=_K(0, code, flags, 0, 0)))
                    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_IN))
                    time.sleep(interval)
        self._mark_input()
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
        cdx_plus    输入框左下角的 + 按钮        底部条带内找
        cdx_mic     麦克风                      判断发送键是否变成麦克风，兼作芯片右锚点
        芯片       没有固定模板，见 chip_point()：麦克风左边最靠右那一小撮深色字形

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


def _is_minimized(hwnd) -> bool:
    """窗口是不是最小化。读不到就当没最小化（交给后面的 rect_of 判哨兵）。"""
    try:
        _win32_size_api()
        return bool(user32.IsIconic(int(hwnd)))
    except Exception:                       # noqa: BLE001
        return False


def _restore_window(hwnd) -> dict:
    """恢复窗口。**统一走 `qi.window.restore`**（和四端同一份实现、同一批判据）。

    取不到模块就返回 ok=False —— 调用方按退出码 7 收场，绝不带着哨兵坐标继续点。
    """
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import window as window_mod
        return window_mod.restore(int(hwnd), timeout=5.0)
    except Exception as exc:                # noqa: BLE001 - 恢复不了就是恢复不了
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _prepare_window(hwnd):
    """最小化就恢复；恢复不了返回"该退出"的结果（调用方按 7 退出）。

    没最小化、或者恢复成功都返回 None——"什么都不用做"和"做不了"必须分得开。
    """
    if not _is_minimized(hwnd):
        return None
    facts = _restore_window(hwnd)
    if facts.get("ok"):
        return None
    return {"ok": False, "stage": "window_minimized", "reason": facts.get("reason"),
            "hint": "窗口最小化且没能恢复，先手动还原窗口再跑"}


def uia_edit_box(hwnd, win):
    """UIA 兜底：窗口里的消息输入框，返回屏幕坐标 (x, y)；找不到返回 None。

    **为什么需要它**：`cdx_composer` 是「加号 + 上方 26px 空白」裁出来的图，
    新版 Codex 把输入框和加号的间距、圆角都改了，这张图匹不上（卡面第 3 条）。
    这里换个不依赖像素的找法：控件树里**窗口下半部**可点、可见、够大的编辑框
    （EditControl / DocumentControl），取面积最大的那个，命中矩形的中心就是落点。

    只**读**，不点。找不到就返回 None——绝不拿"窗口正中间"这种坐标去猜。
    """
    try:
        from pathlib import Path
        source = str(Path(__file__).resolve().parents[1])
        if source not in sys.path:
            sys.path.insert(0, source)
        from qi import ui
    except Exception:                       # noqa: BLE001 - 没有 UIA 就只用模板
        return None
    try:
        docs = ui.documents(int(hwnd))
    except Exception:                       # noqa: BLE001
        return None
    best = None
    for control, _url in docs:
        try:
            root = ui.tree(control)
        except Exception:                   # noqa: BLE001
            continue
        for node in root.walk():
            if node is root or node.kind not in ("EditControl", "DocumentControl",
                                                 "ComboBoxControl"):
                continue
            try:
                if node.control is None or not node.control.IsEnabled:
                    continue
                if node.control.IsOffscreen:
                    continue
                rect = node.control.BoundingRectangle
            except Exception:               # noqa: BLE001 - 元素失效就跳过
                continue
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < 120 or h < 24:           # 行内小框、图标框
                continue
            if not (win[0] <= rect.left and rect.right <= win[0] + win[2]):
                continue
            cy = (rect.top + rect.bottom) // 2
            if cy < win[1] + win[3] * 0.5:  # 只找下半窗：消息列表在上面
                continue
            area = w * h
            if best is None or area > best[0]:
                best = (area, (rect.left + rect.right) // 2, cy)
    return None if best is None else (best[1], best[2])


def focus_composer(d, hwnd, win, pm):
    """模板找不到输入框时：先点它一下让它获焦，再找一次；仍不认就用 UIA 的落点。

    顺序照着卡面来：**点击输入区域使其获焦 → 再找 "+"/composer → 模板失败回退 UIA**。
    Codex 的输入框失焦时没有插入光标、占位行也淡，和裁模板那张图的差别正好在
    这一点上——所以"先获焦再匹配"不是玄学，是把变量消掉。
    返回的 dict 里多一个 `composer_basis`，说明这个落点是怎么来的（写进结果留证）。
    """
    box = uia_edit_box(hwnd, win)
    if box is None:
        return pm
    d.click(*box)                       # 获焦（也会把残余弹层关掉）
    time.sleep(0.5)
    after = locate(d, win)
    after.setdefault("composer_basis", "uia_focus+template")
    if "composer" in after:
        return after
    # 模板还是不认：落点用 UIA 的，send 仍然要模板找到（点错发送键比找不到更糟）
    after["composer"] = box
    after["composer_basis"] = "uia"
    after["missing"] = [m for m in after["missing"] if m != COMPOSER_TPL]
    return after


def locate_with_composer_fallback(d, hwnd, win, pm):
    """只在 composer 模板失配时才走 UIA 回退；其它情况原样返回（不多点一下）。"""
    if COMPOSER_TPL not in (pm.get("missing") or []):
        return pm
    return focus_composer(d, hwnd, win, pm)


def chip_point(d, win):
    """芯片按钮的点击点（窗口内坐标），None = 没找到。

    芯片关着时是 `GPT-6.1 Terra Ultra ⌄` 一串**纯文字、无胶囊底色**，
    文字随模型/档位变，裁不出固定模板。只用两个不变的事实定位：
      1. 右锚点：芯片永远紧贴麦克风左边（生成中麦克风变停止键，才退用发送键）；
      2. ⌄ 是这串文字里**最后一个字形**，与麦克风之间有一段留白。

    于是在「麦克风左侧 60px × 上下 12px」这条小条带里找深色列、按空白切组，
    取**最靠右的一组**，再要求它宽度 4~14px、右边缘离麦克风 4~30px。
    实测 ⌄ 宽 6px、离麦克风 15px；它左边最近的字形离它 10px，不会并成一组。

    不做 `mic_left - 139` 这类减法：模型名换长换短，芯片整体会平移（实测 47px）。
    """
    img = d.grab(win)
    if img.size == 0:
        return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    band = _band(win)
    anchor = d.find(MIC_TPL, TPL_THR, band)
    near = 30                                   # ⌄ 离锚点左边缘的上限
    if anchor is None:
        anchor = d.find(SEND_TPL, TPL_THR, band)
        near = 60                               # 退用发送键时中间还隔着麦克风
    if anchor is None:
        return None
    ax = anchor[0] - win[0]                     # 锚点左边缘（窗口内）
    ay = anchor[1] - win[1] + anchor[3] // 2
    x0, x1 = max(0, ax - 60), max(0, ax - 4)
    y0, y1 = max(0, ay - 12), min(g.shape[0], ay + 12)
    if x1 - x0 < 10 or y1 - y0 < 6:
        return None

    strip = g[y0:y1, x0:x1]
    cols = (strip < 205).sum(axis=0)
    groups, st = [], None
    for i, c in enumerate(cols):
        if c > 0 and st is None:
            st = i
        elif c == 0 and st is not None:
            groups.append((x0 + st, x0 + i - 1))
            st = None
    if st is not None:
        groups.append((x0 + st, x0 + len(cols) - 1))
    if not groups:
        return None

    gl, gr = groups[-1]                          # 最靠右的一组 = ⌄
    if not (4 <= gr - gl + 1 <= 14):
        return None
    if not (4 <= ax - gr <= near):
        return None

    # 纵向用这一组的深色像素质心，横向用中点
    sub = strip[:, gl - x0:gr - x0 + 1]
    ys, xs = np.nonzero(sub < 205)
    if len(ys) == 0:
        return None
    cy = y0 + int(round(ys.mean()))
    return ((gl + gr) // 2, cy)


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

    # **必须同时满足"形状像轨道"和"行上有滑块"**，然后**取最靠下的那条**。
    #
    # 为什么是"最靠下"而不是"最高"/"最长"：
    #   · 只按形状选会选错——实测 y=1204 那组（高 27、白段宽 70）也像轨道，
    #     比真轨道(24)还高，max(key=len) 就挑中它，然后返回 None；
    #   · 卡片里**上面的元素也会像轨道**：模型行是浅灰胶囊（宽 213、高 26），
    #     里面的字间空白能被当成"白色滑块"，于是被误认成轨道（实测 y=1217）；
    #   · 而滑杆永远是卡片里**最下面**的那个元素（卡片从芯片往上长）。
    # 同时用芯片行当上界：卡片里的东西一定在芯片上方 25px 以上，
    # 芯片自己那行（浅灰胶囊 + 文字）就不会被当成轨道。
    chip = chip_point(d, win)
    ceil_y = (chip[1] - 25) if chip else (h_img - 55)
    cands = [(gp, _knob_of(gp)) for gp in pick]
    cands = [(gp, b) for gp, b in cands if b is not None and gp[len(gp) // 2][0] <= ceil_y]
    if not cands:
        return None
    gp, best = max(cands, key=lambda x: x[0][len(x[0]) // 2][0])
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
       在卡片层       -> 点「模型名 ›」那一行（两种形态行高不同，按形态取偏移）
       全关           -> 先点芯片开卡片，再点那一行
    """
    if items_visible(d, win):
        return True
    form = _open_card(d, win)
    if form is None:
        return False
    for attempt in (0, 1):
        sl = find_slider(d, win, locate(d, win).get("send"))
        if sl is None:
            return False
        left, right, row_y, _knob = sl
        d.click(win[0] + (left + right) // 2,
                win[1] + row_y - ROW_ABOVE.get(form, SLIDER_ABOVE))
        time.sleep(1.3)
        if items_visible(d, win):
            return True
        if attempt == 0 and _popup_state(d, win) == "closed":
            # 点空了/卡片被关掉，重新开一次再点，最多两轮
            form = _open_card(d, win)
            if form is None:
                return False
    return False


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
    # **优先点目标模板自己的命中中心**：模板阈值 0.92 已经把
    # "GPT-6.1 Sol / GPT-5.6 Sol" 这类近似的行筛掉了（错行的最高分只有 0.88）。
    # 只有模板没匹上时才退回「勾的位置 + 固定行距」推算——
    # 那条路对 **Default** 这种两行高的项是错的（行距不是 31），
    # 实测点 Default 会落到隔壁 GPT-6.1 Sol 上。
    m = d.find(MODELS[key], MODEL_THR, band)
    if m is not None:
        d.click(m[0] + m[2] // 2, m[1] + m[3] // 2)
    elif d.find(CHECK_TPL, 0.80, band) is not None and before.get("model") in MODELS:
        chk = d.find(CHECK_TPL, 0.80, band)
        keys = list(MODELS)
        cy = (chk[1] + chk[3] // 2
              + (keys.index(key) - keys.index(before["model"])) * ITEM_GAP)
        d.click(band[0] + 60, cy)
    else:
        return {"ok": False, "stage": "item_not_visible", "want": key}
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
    # 复核完把列表收掉：留着它会挡住下一次调用（下一次进来先当"已在列表层"，
    # 跳过开弹层，接着 set_effort 就会在这一层找不到滑杆）。
    d.press("esc")
    time.sleep(0.45)
    ok = after.get("model") == key
    return {"ok": ok, "before": before.get("model"), "after": after.get("model"),
            "want": key, "stage": "done" if ok else "unverified"}


# ============================================================ 调强度

def _bands(g, x0, x1, y0, y1, min_ink=3, min_h=5, min_gap=3):
    """在 y0..y1 里找"有墨迹"的横带（= 文字行），返回 [(top, bottom)]（窗口内坐标）。

    卡片是白底，文字和图标都是深色，所以一行文字就是一条连续带；
    带与带之间至少 min_gap 行白底才算两行（实测档位词 2371..2384、
    模型行 2394..2403，中间空 9 行）。

    2026-10-31 CARD-04b：从 codex_choose.py 搬过来（CARD-02 修好的那一套）。
    """
    marks = []
    height = g.shape[0]
    for y in range(max(0, y0), min(height, y1)):
        if int((g[y, x0:x1] < 200).sum()) >= min_ink:
            marks.append(y)
    out, start, prev = [], None, None
    for y in marks:
        if start is None:
            start = prev = y
        elif y - prev <= min_gap:
            prev = y
        else:
            out.append((start, prev))
            start = prev = y
    if start is not None:
        out.append((start, prev))
    return [(a, b) for a, b in out if b - a + 1 >= min_h]


def card_bands(d, win, sl):
    """滑杆上方的文字带。强度卡从上到下是 [档位词][模型行][滑杆]。

    2026-10-31 CARD-04b：从 codex_choose.py 搬过来（CARD-02 修好的那一套）。
    """
    left, right, row_y, _knob = sl
    img = d.grab(win)
    if img.size == 0:
        return []
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    x0, x1 = max(0, left + 8), min(g.shape[1], right - 8)
    y0, y1 = max(0, row_y - 100), max(0, row_y - 18)
    if x1 - x0 < 40 or y1 - y0 < 20:
        return []
    return _bands(g, x0, x1, y0, y1)


def model_row(d, win, sl):
    """模型行（`GPT-6 Luna ›`）的中心 y，窗口内坐标；找不到返回 None。

    取**最下面那条带**：强度卡里模型行永远紧挨着滑杆上方。
    旧代码用的是固定偏移（轨道中心行上方 57px），26.1027 版卡片矮了 21px，
    按老偏移点下去落在档位词上——点不开列表，还不会报错。
    （--list 走的就是这条：CARD-04b 之前 codex_use 一直用 ROW_ABOVE 的固定偏移。）

    2026-10-31 CARD-04b：从 codex_choose.py 搬过来（CARD-02 修好的那一套）。
    """
    bands = card_bands(d, win, sl)
    if not bands:
        return None
    top, bottom = bands[-1]
    return (top + bottom) // 2


def _popup_form(d, win):
    """卡片形态：'effort'（独立强度滑杆）/ 'preset'（预设滑条）/ 'unknown' / None（没开）。

    2026-10-31 CARD-04b：判据换成 codex_choose.py 里 CARD-02 修好的那一套。
    旧判据只看芯片上的**英文**模板（实测开着 0.936 / 1.000，关着 None / 0.489）：
        Select effort -> 具体模型，卡片里是档位词 + 模型行 + 强度滑杆
        Select model  -> Default（自动），卡片里是「模型 强度 ›」+ 预设滑条
    但界面换皮/换成中文时，两个模板的相似度会掉到 0.33 / 0.44，**永远匹配不上**：
    弹层明明开着，这里一直返回 None，--list 打一行「打不开卡片」就退出
    （还会因为反复点芯片把弹层点开又点关）。

    现在文字模板只当**快速路径**（英文界面仍然精确、也不用多抓图），
    中文/换皮界面走结构判据：
      1. 轨道在不在（find_slider）：关着时实测 None，开着时能定位到轨道+滑块；
      2. 轨道上方有几条文字带：强度卡是 [档位词][模型行] 两条。
         只有一条就不认成 effort，报 unknown —— Default（自动）模型的预设卡
         里那条滑条是"模型+强度"的组合条，按强度去调它等于**换模型**，
         宁可停在这里也不猜。
    """
    band = _band(win)
    if d.find(MENU_TPL, 0.82, band) is not None:
        return "effort"
    if d.find(MODEL_MENU_TPL, 0.85, band) is not None:
        return "preset"
    sl = find_slider(d, win, locate(d, win).get("send"))
    if sl is None:
        return None
    return "effort" if len(card_bands(d, win, sl)) >= 2 else "unknown"


def _popup_state(d, win):
    """卡片现在停在哪一层：closed / effort（强度滑杆层）/ preset（预设滑条层）/ unknown / models。

    先判 models：「列表里能同时看到 ≥2 个模型名」。
    列表层里芯片也显示「Select ...」，只看芯片会把列表层误当成卡片层，
    接着去 find_slider 就必然返回 None，报一句莫名其妙的 no_slider。
    """
    if items_visible(d, win):
        return "models"
    return _popup_form(d, win) or "closed"


def _open_card(d, win):
    """把卡片打开，返回形态 'effort' / 'preset' / 'unknown'，打不开返回 None。

    点芯片，一次只点一下，不反复开关。弹层是模态的：点它外面任何地方都会收起，
    所以调用方拿到打开后的形态，要一口气把要读的、要改的都做完，最后才收。

    点完芯片要**确认真的开了**再返回：关闭动画没走完时点击会被吃掉
    （刚点完模型项、卡片正在收起，这时点芯片实测无效）。
    失败只重试一次，且第二次前先点一下输入框把残余动画和焦点都处理干净。
    """
    form = _popup_form(d, win)
    if form:
        return form
    for attempt in (0, 1):
        if attempt:
            pm = locate(d, win)
            if "composer" not in pm:
                return None
            d.click(*pm["composer"])
            time.sleep(0.4)
        p = chip_point(d, win)
        if p is None:
            return None
        d.click(win[0] + p[0], win[1] + p[1])
        time.sleep(1.3)
        form = _popup_form(d, win)
        if form:
            return form
    return None


def _ensure_effort(d, win):
    """保证现在停在**强度滑杆层**，返回 True/False。

    三种入口状态都收敛到这里，且**不来回开关**：
        effort -> 已经在这层，什么都不做
        models -> 列表层，先按一次 Esc 退回去（退不回去就重新点开）
        preset -> Default(自动) 模式，这里根本没有独立滑杆，返回 False
        closed -> 点芯片打开
    """
    st = _popup_state(d, win)
    if st == "effort":
        return True
    if st == "models":
        d.press("esc")
        time.sleep(0.5)
        if _popup_state(d, win) == "effort":
            return True
    return _open_card(d, win) == "effort"


def _effort_text(d, win, sl):
    """在卡片顶部读档位词（Light / Medium / ...），读不到返回 None。

    形态 A 的卡片顶部有一个**大号橙色档位词**，这是最可靠的读数来源：
    实测正确那个 1.000，其余 ≤0.94（"High" 会在 "Extra High" 里匹到 0.94，
    所以要求赢家 ≥0.95 且领先第二名 ≥0.03，避免 High / Extra High 串味）。

    为什么还需要它：滑杆位置换算成比例会被卡片宽度带偏
    （实测轨道 244~267px 都出现过，档距 41~52px），换算误差能到半档。
    文字是直接读数，不受宽度影响。
    """
    left, right, row_y, _knob = sl
    w = max(80, (right - left) - 40)
    region = (win[0] + left + 20, win[1] + row_y - 95, w, 70)
    best, bs, second = None, 0.0, 0.0
    for k in EFFORTS:
        m = d.find("cdx_r_" + k, 0.0, region)
        s = m[4] if m else 0.0
        if s > bs:
            second, bs, best = bs, s, k
        elif s > second:
            second = s
    if best is not None and bs >= 0.95 and bs - second >= 0.03:
        return best
    return None


def _effort_now(d, win, sl):
    """从**已经打开**的卡片读档位（不开关卡片）。

    先读卡片顶部的档位词；读不到（比如卡片高度变了、词被裁掉）才退回
    按滑块圆心算比例的老办法。比例用**滑块圆心**，不用填充右端——
    填充右端是滑块左边缘，差一个半径（13px），200px 轨道上就是 6%。
    """
    left, right, row_y, knob = sl
    frac = (knob - left) / max(1, right - left)
    out = {"knob": knob, "track": [left, right], "frac": round(frac, 3)}
    guess = _effort_text(d, win, sl)
    if guess is not None:
        out.update({"effort": guess, "label": EFFORT_LABEL[guess], "from": "text"})
    else:
        best, bd = EFFORTS[0], 9.9
        for k in EFFORTS:
            d_ = abs(frac - EFFORT_FRAC[k])
            if d_ < bd:
                bd, best = d_, k
        out.update({"effort": best, "label": EFFORT_LABEL[best], "from": "slider"})
    if out["effort"] == "light":
        out["at_min"] = True
    if out["effort"] == "ultra":
        out["at_max"] = True
    return out


def read_effort(d, win):
    """读强度：开一次卡片 -> 读 -> 关。Default 模式下没有档位，如实报 auto。"""
    st = _popup_state(d, win)
    if st == "preset":
        return {"effort": "auto", "hint": "当前是 Default（自动）模型，"
                                          "卡片里只有「模型+强度」预设条，没有独立档位"}
    if not _ensure_effort(d, win):
        if _popup_state(d, win) == "preset":
            return {"effort": "auto", "form": "preset",
                    "hint": "Default（自动）模型：卡片里只有「模型+强度」预设条，"
                            "没有独立档位"}
        return {"effort": "unknown", "hint": "打不开强度卡片"}
    sl = find_slider(d, win, locate(d, win).get("send"))
    out = _effort_now(d, win, sl) if sl else {"effort": "unknown",
                                              "hint": "没找到滑杆"}
    d.press("esc")
    time.sleep(0.35)
    return out


def set_effort(d, win, key):
    """调强度。**开一次弹层，一次做完，靠"读到位置就停"而不是数按键次数。**

    1. 点芯片打开弹层（只点一下，点完确认真的开了）
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
    key = EFFORT_ALIAS.get(key, key)
    if key not in EFFORTS:
        return {"ok": False, "stage": "bad_arg", "want": key, "options": EFFORTS}
    st = _popup_state(d, win)
    if st == "preset":
        # Default（自动）模式下卡片里没有独立强度滑杆，那条滑条是
        # 「模型+强度」的预设组合，调它等于换模型 —— 不能当成调强度用。
        return {"ok": False, "stage": "default_model",
                "hint": "当前模型是 Default（自动），卡片里只有「模型+强度」预设条，"
                        "没有独立强度滑杆。先用 --model 选一个具体模型再调强度"}
    if not _ensure_effort(d, win):
        # 打不开时再看一眼形态：Default 下 _ensure_effort 会先把卡片点开、
        # 再因为形态不对返回 False，所以这里能拿到准确原因。
        if _popup_state(d, win) == "preset":
            return {"ok": False, "stage": "default_model",
                    "hint": "当前模型是 Default（自动），卡片里只有"
                            "「模型+强度」预设条，没有独立强度滑杆。"
                            "先用 --model 选一个具体模型再调强度"}
        return {"ok": False, "stage": "menu_not_open"}

    send = locate(d, win).get("send")
    sl = find_slider(d, win, send)
    if sl is None:
        return {"ok": False, "stage": "no_slider"}
    before = _effort_now(d, win, sl)

    left, right, row_y, knob = sl
    d.click(win[0] + knob, win[1] + row_y)     # 点圆点，键盘才接管滑杆
    time.sleep(0.45)

    # **现场自标定**，不用写死的比例：
    #   ① 左键按到底 -> p0（硬停，真实最左）
    #   ② 再按 3 次右键读 p3，步长 = (p3 - p0) / 3
    #   ③ 回到底部，从 p0 数着往右走
    # 为什么除 3 不除 1：单次读数有 ±10px 抖动（动画没停稳就抓），
    # 实测一次就量出 step=52（真值 41），把 max 目标推到轨道外。
    # 卡片宽度本身也会变（实测轨道 244~267px），写死比例同样会差半档。
    for _ in range(9):
        d.press("left")
        time.sleep(0.2)
    p0 = (find_slider(d, win, send) or (0, 0, 0, knob))[3]
    for _ in range(3):
        d.press("right")
        time.sleep(0.35)
    p3 = (find_slider(d, win, send) or (0, 0, 0, p0))[3]
    step = (p3 - p0) / 3.0
    if step < 8:                                # 读抖动/没动：退回按轨道宽度估
        step = max(8.0, (right - left) / 5.0)
    for _ in range(12):
        d.press("left")
        time.sleep(0.16)

    idx = EFFORTS.index(key)
    target = p0 + idx * step

    for _ in range(10):                        # 往右逼近
        cur = find_slider(d, win, send)
        if cur is None or cur[3] >= target - 4:
            break
        d.press("right")
        time.sleep(0.26)
    for _ in range(10):                        # 超了往回退
        cur = find_slider(d, win, send)
        if cur is None or cur[3] <= target + 4:
            break
        d.press("left")
        time.sleep(0.26)

    # **按文字读数闭环纠偏**：位置往前走完，再读卡片上那个档位词，
    # 差几档就补几下。步长估算有误差（卡片宽度会变），但文字是直接读数，
    # 用它当反馈就不怕步长不准。最多纠 3 轮，每轮最多挪 6 下。
    after = {"effort": "unknown"}
    for _ in range(3):
        sl2 = find_slider(d, win, send)
        if sl2 is None:
            break
        after = _effort_now(d, win, sl2)
        if after.get("effort") == key or after.get("from") != "text":
            break
        diff = EFFORTS.index(after["effort"]) - idx
        for _ in range(min(6, abs(diff))):
            d.press("right" if diff < 0 else "left")
            time.sleep(0.26)

    sl2 = find_slider(d, win, send)
    if sl2 is not None:
        after = _effort_now(d, win, sl2)
    d.press("esc")
    time.sleep(0.45)

    ok = after.get("effort") == key
    if not ok and after.get("from") != "text" and after.get("knob") is not None:
        # 没有文字读数时（卡片形态变了）才退回位置判断：落在目标半档内算到位。
        ok = abs(after["knob"] - target) <= step * 0.5
    return {"ok": ok, "before": before.get("effort"), "want": key,
            "after": after.get("effort"), "read": after.get("from"),
            "knob": after.get("knob"),
            "p0": int(p0), "step": int(step), "target": int(target),
            "stage": "done" if ok else "unverified"}


# ============================================================ 提问

def ask(d, win, prompt, timeout=180.0, purpose=None):
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[1])
    if source not in sys.path:
        sys.path.insert(0, source)
    from qi import runlog, tier_guard
    from qi.screen import Screen
    from qi.conversation import Conversation, ask as send_and_receive
    started = time.monotonic()
    # 第 6 条：高档（review/accept）等待上限下限 900s，不得 300s 就判超时
    timeout = tier_guard.floor_timeout(purpose, timeout)

    def door():
        """CARD-06b 第 3 条：门放在**写入文本之后、点发送之前**。

        Codex 没有只读档位通道（读档要开卡片点两下，不算只读，适配属 CARD-06c），
        所以这里显式 `require_readback=False`：结果里带
        `tier_readback="unsupported"`，不是静默放行。
        """
        tier_guard.gate("codex", purpose, reread=None, hwnd=d.hwnd,
                        require_readback=False)
        return {}

    screen = Screen(verbose=False)
    screen.bind(d.hwnd)
    result = send_and_receive(Conversation(screen, "codex"), prompt, timeout,
                              pre_send=door if purpose is not None else None,
                              # T2-1 接线：不传 purpose 就不记账，失去重复发送护栏。
                              purpose=purpose)
    result.setdefault("elapsed", round(time.monotonic() - started, 1))
    # 超时分型由 conversation.ask 算，这里只保证字段一定在（见 runlog.with_timeout_kind）
    return runlog.with_timeout_kind(result)


# ============================================================ CLI

# 运行日志（2026-10-31 任务卡04 / 04c）记在 main 里的 done()：
#   * 正常结束都从 `return done(...)` 出去，漏不掉；
#   * 异常分三段兜底——入口段（解析参数、等你空闲）、初始化段（04b）、动作段，
#     三段合起来盖住从入口到出口的每一行；`logged` 是闸门，一次运行只落一行；
#   * done() 自己吞掉所有异常，写日志失败不影响打印和退出码。
def main(argv=None):
    """入口：跑一次，结束时**正好**记一行运行日志，返回它的退出码。

    2026-10-31 任务卡04：每次运行结束（成功 / 超时 / 报错 / 参数错后的正常退出）
    都走 `done()` 记一行；`--help` 由 argparse 直接 SystemExit(0)，不记。
    任务卡04c：`idle_seconds()` 和等待循环原来在保护区外，一炸就一行日志都没有。
    现在入口段也包进 try：任何一步抛异常都先记一行 stage=exception，再照原样抛出
    （异常实例、类型、堆栈都不动）。
    """
    started = time.time()
    logged = []

    def done(result, code):
        """记一行运行日志，再把退出码原样返回——写在 return 里就不会漏。

        失败时把 hint / error 一起交出去（`runlog.failure_note` 兜底），
        **不写提问和回复正文**。`logged` 是闸门：内层 except 已经记过的，出口
        兜底不会再记第二行。写日志失败一律吞掉：连 import 失败都不能影响原来的
        输出和退出码。
        """
        if logged:
            return code
        logged.append(True)
        try:
            import sys
            from pathlib import Path
            source = str(Path(__file__).resolve().parents[1])
            if source not in sys.path:
                sys.path.insert(0, source)
            from qi import failure, runlog
            result = dict(result)                      # 不就地改调用方的 dict
            result.setdefault("elapsed", round(time.time() - started, 1))
            # 失败留证（CARD-06b 第 9 条）：tier_*/zoom_*/human_check/input_mismatch
            failure.record("codex", result, hwnd=_CURRENT_HWND.get("v"))
            runlog.record("codex", runlog.with_timeout_kind(result))
        except Exception:                              # noqa: BLE001
            pass
        return code

    # 2026-10-31 任务卡04c：从解析参数到「等你空闲」这一整段原来都在异常保护区外，
    # idle_seconds() 一抛异常就一行日志都不留。现在整段包进 try——异常先记一行，
    # 再照原样抛出去；等待循环里 `return done(busy, 4)` 那条正常出口不受影响。
    try:
        ap = argparse.ArgumentParser(add_help=True)
        ap.add_argument("prompt", nargs="?", default=None)
        ap.add_argument("--new", action="store_true", help="先打开一个新对话")
        ap.add_argument("--list", "-l", action="store_true")
        ap.add_argument("--model", default=None)
        ap.add_argument("--effort", default=None)
        ap.add_argument("--probe", action="store_true",
                        help="只打印每个模板的命中分数和窗口内坐标，不点击")
        ap.add_argument("--size", default=None, help="WxH，默认 1380x1340")
        ap.add_argument("--pos", default=None, help="X,Y，默认 -1440,-560")
        ap.add_argument("--timeout", type=float, default=180.0)
        ap.add_argument("--purpose", choices=["test", "review", "accept"], default=None,
                        help="test=最低档，review/accept=最高档（发送必填，无默认）")
        ap.add_argument("--wait", type=float, default=120.0)
        ap.add_argument("--idle", type=float, default=2.0)
        ap.add_argument("--force", action="store_true")
        a = ap.parse_args(argv)                # SystemExit 交给入口段的 except SystemExit

        size, pos = WIN_SIZE, WIN_POS
        if a.size:
            try:
                w, h = a.size.lower().split("x")
                size = (int(w), int(h))
            except ValueError:
                print("--size 要写成 1380x1340")
                return done({"ok": False, "stage": "bad_args",
                             "hint": "--size 要写成 1380x1340"}, 2)
        if a.pos:
            try:
                x, y = a.pos.split(",")
                pos = (int(x), int(y))
            except ValueError:
                print("--pos 要写成 -1440,-560")
                return done({"ok": False, "stage": "bad_args",
                             "hint": "--pos 要写成 -1440,-560"}, 2)

        if not a.prompt and not a.list and not a.model and not a.effort and not a.probe:
            print(__doc__)
            print("可选模型：" + ", ".join(MODELS))
            print("看不到的：" + ", ".join(UNREACHABLE))
            print("可选强度：" + ", ".join(EFFORTS))
            return done({"ok": False, "stage": "bad_args", "hint": "没有给任何动作"}, 2)

        # 「等你空闲」也在保护区里（04c）：idle_seconds 一炸就先记一行再抛出。
        if not a.force:
            end = time.time() + a.wait
            while time.time() < end:
                if idle_seconds() >= a.idle:
                    break
                time.sleep(0.2)
            else:
                busy = {"ok": False, "stage": "user_busy",
                        "hint": f"{a.wait}s 内你一直在用电脑"}
                print(json.dumps(busy, ensure_ascii=False))
                return done(busy, 4)

    except SystemExit as exc:                          # argparse：--help / 参数错
        if not exc.code:
            raise                                      # --help：不记
        done({"ok": False, "stage": "bad_args", "hint": "参数解析失败"}, exc.code)
        raise
    except Exception as e:                             # noqa: BLE001 - 入口异常也要留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise

    # 初始化（找/开窗口、摆位、抓首帧、置前）原来在异常保护区外面：这一段抛异常时
    # 一行日志都不会留（CARD-04b 打回的第 2 条）。现在整段包进 try，异常照原样往上
    # 抛——退出方式和堆栈都不变，只是先记一次，带异常类型。
    try:
        hwnd = find_window()
        if hwnd is None:
            print("启动 codex …")
            subprocess.Popen(["explorer.exe", LAUNCH])
            end = time.time() + 30
            while time.time() < end and hwnd is None:
                time.sleep(1.0)
                hwnd = find_window()
        _CURRENT_HWND["v"] = hwnd            # 失败留证（第 9 条）要截这个窗口
        if hwnd is None:
            gone = {"ok": False, "stage": "no_window"}
            print(json.dumps(gone, ensure_ascii=False))
            return done(gone, 1)

        # 默认**不动**窗口。定位靠模板匹配，所以窗口在哪、多大都不影响
        # （实测 1380x1340 / 1150x1050 / 980x900 三档下发送键都命中 1.000）。
        # 只有显式给了 --size / --pos 才去摆——那是"你想让它放在哪"，不是定位需要。
        if a.size or a.pos:
            place(hwnd, pos, size)
            time.sleep(0.8)

        # CARD-07 第 3 条：**先恢复、再置前，然后才去找控件**。
        # 最小化时 GetWindowRect 给的是 -32000 哨兵，拿它算出来的点击位置会飞到屏幕外；
        # 恢复不了就按退出码 7 收场，绝不带着哨兵坐标继续（CARD-06a (c) 的老约定）。
        #
        # 用 globals().get 取：`test_desktop_controls` 会把 main 这一个函数从源码里抠出来
        # exec（没有包上下文），取不到就跳过——和 qi/choose.py 的 tier_guard 同一招。
        prepare = globals().get("_prepare_window")
        if not a.probe and prepare is not None:
            problem = prepare(hwnd)
            if problem is not None:
                print(json.dumps(problem, ensure_ascii=False))
                return done(problem, 7)

        win = rect_of(hwnd)
        # 最小化且轮询 5s 也没恢复：rect_of 会把哨兵矩形(-32000,...)原样还回来。
        # 这时绝不能拿它算点击位置 —— 退出码 7（CARD-06a (c)）。
        if win and (win[0] <= -31999 or win[1] <= -31999):
            mini = {"ok": False, "stage": "window_minimized",
                    "hint": "窗口最小化且没能恢复，先手动还原窗口再跑"}
            print(json.dumps(mini, ensure_ascii=False))
            return done(mini, 7)
        if win is None:
            flat = {"ok": False, "stage": "no_rect"}
            print(json.dumps(flat, ensure_ascii=False))
            return done(flat, 1)
        if win[2] < MIN_W and not a.probe:
            narrow = {"ok": False, "stage": "window_too_narrow",
                      "size": [win[2], win[3]], "min_w": MIN_W,
                      "hint": "窗口太窄时 Codex 会挤压布局，芯片箭头模板会失准"
                              "（实测 980 宽时掉到 0.834）。拉宽到 1050 以上，"
                              "或 --size 1380x1340"}
            print(json.dumps(narrow, ensure_ascii=False))
            return done(narrow, 1)

        d = Desk(hwnd)
        # CARD-07 第 3 条：先置前（拿真焦点），再找控件。
        # 原来置前放在第一次 locate 之后——找不到 composer 就直接报 controls_not_found，
        # 根本没机会让窗口获得焦点；而 Codex 的输入框在失焦时和聚焦时画法不一样，
        # 越找不到越不获焦，越不获焦越找不到。
        if not a.probe and not activate(hwnd):
            # 提不到前台就**直接退出**，不要继续。系统前台锁（别的程序占着前台）时
            # SetForegroundWindow 会被拒，这时继续操作等于闭着眼睛点。
            cold = {"ok": False, "stage": "not_foreground",
                    "hint": "窗口提不到前台（你可能正在用别的窗口），已退出，不硬点"}
            print(json.dumps(cold, ensure_ascii=False))
            return done(cold, 4)
        if not a.probe:
            time.sleep(0.6)
            d.grab(fresh=True)              # 置前后重新抓一帧
            win = rect_of(hwnd) or win

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
            return done({"ok": True, "stage": "probe"}, 0)

        # 第 3 条：composer 模板匹不上（新版 Codex 改了输入框的画法）时，
        # 先用 UIA 找到输入框、点它一下让它获焦，再找一次模板；
        # 模板仍然不认，就直接用 UIA 的落点，并把依据写进结果里。
        # 同样走 globals()：抠出来 exec 的测试里取不到，就保持原样。
        fallback = globals().get("locate_with_composer_fallback")
        if fallback is not None and not a.probe:
            pm = fallback(d, hwnd, win, pm)

        if pm["missing"]:
            lacked = {"ok": False, "stage": "controls_not_found",
                      "missing": pm["missing"],
                      "hint": "模板没命中（UIA 也没找到输入框）。多半是窗口太窄 / 换了主题 / "
                              "UI 改版。先跑 --probe 看每个模板的实际分数"}
            print(json.dumps(lacked, ensure_ascii=False))
            return done(lacked, 1)

        # 下面要点击了，这时才必须前台 —— 已经在上面置前过了，这里只复核一次：
        # 万一被别人抢回前台，仍然按"不硬点"退出（CARD-06a(c) 的老约定）。
        if not activate(hwnd):
            cold = {"ok": False, "stage": "not_foreground",
                    "hint": "窗口提不到前台（你可能正在用别的窗口），已退出，不硬点"}
            print(json.dumps(cold, ensure_ascii=False))
            return done(cold, 4)
        time.sleep(0.4)
        d.grab(fresh=True)                 # 置前后重新抓一帧
        win = rect_of(hwnd) or win
        basis = pm.get("composer_basis")   # 落点是怎么来的，重找一次也要带着
        pm = locate(d, win, need_composer=False)
        if basis:
            pm["composer_basis"] = basis
        if pm["missing"]:
            lacked = {"ok": False, "stage": "controls_not_found", "missing": pm["missing"]}
            print(json.dumps(lacked, ensure_ascii=False))
            return done(lacked, 1)
    except Exception as e:                             # noqa: BLE001 - 初始化异常也要留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise

    r = {"ok": True, "stage": "noop"}
    try:
        if a.new:
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from qi.screen import Screen
            from qi.workbuddy import new_chat
            screen = Screen(verbose=False)
            screen.bind(hwnd)
            new_chat(screen, "codex")
            d._invalidate()
        if a.list:
            # **只开一次卡片**：先读强度（只有形态 A 有），再点入口行进列表读模型，
            # 最后 Esc 收掉。原来读模型开一次、读强度又开一次，等于开关两遍。
            out = {}
            form = _open_card(d, win)
            if form is None:
                out = {"model": "unknown", "effort": "unknown",
                       "hint": "打不开卡片"}
            else:
                sl = find_slider(d, win, locate(d, win).get("send"))
                if form == "preset":
                    out.update({"effort": "auto", "form": "preset",
                                "hint": "Default（自动）模型：卡片里只有"
                                        "「模型+强度」预设条，没有独立档位"})
                elif form == "unknown":
                    out.update({"form": "unknown", "effort": "unknown",
                                "hint": "卡片开着，但轨道上方只有一条文字带，"
                                        "不是核对过的强度卡形态，档位不敢猜"})
                elif sl is not None:
                    out.update(_effort_now(d, win, sl))
                if sl is not None:
                    # 模型那一行按**文字带**找（见 model_row），不再用固定偏移：
                    # 26.1027 版卡片比 10-29 那版矮 21px，57px 的偏移点下去会落在
                    # 档位词上，模型列表根本点不开。
                    left, right, _row_y, _knob = sl
                    y = model_row(d, win, sl)
                    if y is None:
                        out["model"] = "unknown"
                        out["hint"] = "卡片里找不到模型行"
                    else:
                        d.click(win[0] + (left + right) // 2, win[1] + y)
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
            code = 0 if out.get("model") not in (None, "unknown") else 1
            # 2026-10-31 任务卡04c：失败时把**已经打印出去**的那句 hint 也交给日志。
            # 原来只传 ok/stage，落盘 error 是 null，等于把「打不开卡片」丢了。
            # 连 hint 都没有的那种（卡片和列表都开了，就是读不出模型）才按现场字段
            # 补一句，免得 error 又变回 null。
            row = {"ok": code == 0, "stage": "list"}
            if code:
                row["hint"] = (out.get("hint") or out.get("error")
                               or f"列表读不出模型（model={out.get('model')}）")
            return done(row, code)

        if a.model:
            r = set_model(d, win, a.model)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return done(r, 1)
        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                return done(r, 1)
        if a.prompt:
            # --purpose 必填（无默认）：发送前要拿 *_choose 写下的档位回执过闸
            if not a.purpose:
                # 不用 ap.error：那会直接 SystemExit(2)，绕过 done() 的运行日志
                # （CARD-04 的契约是"每次运行正好一行"）。
                bad = {"ok": False, "stage": "bad_args",
                       "hint": "发送必须带 --purpose test|review|accept（无默认）"}
                print(json.dumps(bad, ensure_ascii=False))
                return done(bad, 2)
            r = ask(d, win, a.prompt, a.timeout, a.purpose)
            if pm.get("composer_basis"):
                # 输入框落点怎么来的（template / uia_focus+template / uia），留证用
                r = {**r, "composer_basis": pm["composer_basis"]}
            print(json.dumps(r, ensure_ascii=False))
            if not r.get("ok"):
                # 档位没确认 = 5（与 docs/exit-codes.md 同一张表）
                return done(r, 5 if r.get("stage") == "tier_unconfirmed" else 1)
    except RuntimeError as e:
        # 自带 stage/code 的异常（ui.UiDocsLost=6、window.WindowMinimized=7）按它自己的码走；
        # 本脚本不 import 项目包，用鸭子类型认。
        if hasattr(e, "as_result"):
            print(json.dumps(e.as_result(), ensure_ascii=False))
            return done(e.as_result(), getattr(e, "code", 6))
        r = {"ok": False, "stage": "aborted", "hint": str(e)}
        print(json.dumps(r, ensure_ascii=False))
        return done(r, 4)
    except Exception as e:                             # noqa: BLE001 - 意外异常也留痕
        done({"ok": False, "stage": "exception",
              "error": f"{type(e).__name__}: {e}"}, 0)   # 返回值用不上，记完原样抛
        raise
    return done(r, 0)


if __name__ == "__main__":
    # 入口编码（CARD-08 第 1 条）：借 qi.console；装不上就自己 reconfigure，绝不挡住主流程
    _lock = None
    try:
        _source = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _source not in sys.path:
            sys.path.insert(0, _source)
        from qi.console import setup as _setup_console
        from qi import uilock as _uilock

        _setup_console()
        _lock = _uilock
    except Exception:                                  # noqa: BLE001
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                          # noqa: BLE001
                pass
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    if _lock is None:
        print(json.dumps({"ok": False, "stage": "ui_busy", "reason": "lock_runtime_unavailable"}))
        raise SystemExit(8)
    raise SystemExit(_lock.entry("codex_use", main))
