#!/usr/bin/env python3
"""deepseek 独立全功能脚本（完整包含 deepseek 的模型/档位选择、消息发送、回复读取全部逻辑）。

用法：
  1. 显式子命令（推荐，主流程 server 与总控统一调用方式）：
     python src/scripts/deepseek.py choose --list
     python src/scripts/deepseek.py choose --model <模型> [--effort <档位>] [--think on|off]
     python src/scripts/deepseek.py use "你的问题" [--purpose <用途>] [--timeout <秒>]
     python src/scripts/deepseek.py use --read

  2. 直接参数自动路由（100% 兼容原 deepseek_choose.py 与 deepseek_use.py 参数）：
     python src/scripts/deepseek.py --list
     python src/scripts/deepseek.py --read
     python src/scripts/deepseek.py "你的问题"
"""
from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_EXPECTED_CHOOSE_SYMBOLS = ['OFF_MIN', 'ON_MAX', 'REQUIRED', 'TEMPLATES', 'TITLE', '_SRC', '_parse', '_read', '_record_receipt', '_win', 'list_state', 'main', 'set_state', 'usage']
_EXPECTED_USE_SYMBOLS = ['REQUIRED', 'SITE', '_SRC', 'main']

# ---------------------------------------------------------------------------
# 内嵌完整 Choose（选择模型 / 强度 / 思考模式 / 状态回读）原版源码
# ---------------------------------------------------------------------------
_CHOOSE_SOURCE = '"""deepseek 网页版：只做选择，不发提问。\n\nDeepSeek 没有模型选择器，只有「深度思考」「智能搜索」两个开关，所以这里管这两个。\n\n用法：\n    python deepseek_choose.py --list\n    python deepseek_choose.py --think on\n    python deepseek_choose.py --think off --search on\n\n判断开关状态**不能靠模板分数**：归一化相关对亮度不敏感，\n开/关两态在同一位置都能拿到 0.96 以上。实测可靠的是颜色——\n关：浅蓝描边，B-R≈37；开：白底黑字，B-R≈0.6。\n所以模板只用来定位胶囊，开关状态看 B-R。\n\n读状态前必须先把鼠标移开：悬停会把胶囊变成灰底，读出来像"开"。\n"""\n\nfrom __future__ import annotations\n\nimport json\nimport os\nimport sys\nimport time\n\n_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\nif _SRC not in sys.path:\n    sys.path.insert(0, _SRC)\n\nfrom qi import ForegroundLost, Library, Screen, console, uilock  # noqa: E402\nfrom qi.browser import ensure_site  # noqa: E402\nfrom qi import failure, guard  # noqa: E402\nfrom qi import zoom as zoom_mod  # noqa: E402\n\n# 每个开关一张定位图（截"关"的样子即可，两态都能定位到）\nTEMPLATES = {"think": "ds_think", "search": "ds_search"}\nREQUIRED = list(TEMPLATES.values())\nTITLE = "Google Chrome"\n\n\ndef _win(s: Screen):\n    """目标窗口矩形。不限定就是在整个虚拟桌面里找，主屏上开着同款页面会先命中那边。"""\n    return s.window_rect(TITLE, on_secondary=True)\n\n\nOFF_MIN = 25.0   # B-R 大于这个值算"关"（浅蓝描边）\nON_MAX = 15.0    # 小于这个值算"开"（白底黑字）\n\n\ndef _read(s: Screen, kind: str) -> dict:\n    m = s.find(TEMPLATES[kind], region=_win(s))\n    if m is None:\n        return {"state": "missing", "blue": None, "hit": None}\n    # 把鼠标移开再量，否则悬停态会被读成"开"。\n    # 不能写 max(0, ...)：0 是虚拟桌面坐标里的一个"合法但随机"的点，\n    # 开关在屏幕上偏上时会正好落到别的元素上，反而触发新的悬停。改为夹在窗口内。\n    _w = _win(s)\n    ty = max(_w[1] + 20, m.center[1] - 400) if _w else max(0, m.center[1] - 400)\n    s.move_to(m.center[0], ty)\n    time.sleep(0.35)\n    b, _g, r = s.region_mean(m)\n    blue = round(b - r, 1)\n    state = "off" if blue > OFF_MIN else ("on" if blue < ON_MAX else "unknown")\n    return {"state": state, "blue": blue, "hit": m}\n\n\ndef list_state(s: Screen) -> dict:\n    out = {}\n    for kind in TEMPLATES:\n        d = _read(s, kind)\n        d.pop("hit", None)\n        out[kind] = d\n    return out\n\n\ndef set_state(s: Screen, kind: str, want: str) -> dict:\n    cur = _read(s, kind)\n    before = cur["state"]\n    if before in ("missing", "unknown"):\n        return {"ok": False, "kind": kind, "stage": before, "before": before, "blue": cur["blue"],\n                "hint": "定位图没找到，或 B-R 落在 15~25 之间；确认页面是浅色主题、开关没被悬停"}\n    if before == want:\n        return {"ok": True, "kind": kind, "stage": "already", "before": before, "after": before,\n                "blue": cur["blue"]}\n    s.click(cur["hit"])\n    time.sleep(0.9)\n    after = _read(s, kind)["state"]\n    return {"ok": after == want, "kind": kind, "stage": "done" if after == want else "unverified",\n            "before": before, "after": after}\n\n\ndef _parse(argv: list[str]) -> dict:\n    o = {"list": False, "think": None, "search": None, "tab": None, "bad": [], "help": False}\n    i = 0\n    while i < len(argv):\n        a = argv[i]\n        if a in ("-l", "--list"):\n            o["list"] = True\n        elif a in ("--think", "--search"):\n            key = a[2:]\n            if i + 1 >= len(argv):\n                o["bad"].append(f"{a} 缺值")\n            else:\n                v = argv[i + 1].strip().lower()\n                i += 1\n                if v in ("on", "1", "true", "开", "开启"):\n                    o[key] = "on"\n                elif v in ("off", "0", "false", "关", "关闭"):\n                    o[key] = "off"\n                else:\n                    o["bad"].append(f"{a} 的值只能是 on/off，收到 {v!r}")\n        elif a == "--tab":\n            if i + 1 >= len(argv):\n                o["bad"].append("--tab 缺值")\n            else:\n                i += 1\n                try:\n                    o["tab"] = int(argv[i])\n                except ValueError:\n                    o["bad"].append(f"--tab 要数字，收到 {argv[i]!r}")\n        elif a in ("-h", "--help"):\n            o["help"] = True\n        else:\n            o["bad"].append(a)\n        i += 1\n    return o\n\n\ndef usage(name: str) -> None:\n    print("用法：")\n    print(f"  python {name} --list                    # 读当前两个开关（只读）")\n    print(f"  python {name} --think on|off")\n    print(f"  python {name} --search on|off")\n    print(f"  python {name} --list --tab N            # 先按 Ctrl+N 切标签页")\n    print("  （发提问请用 deepseek_use.py）")\n\n\ndef _record_receipt(hwnd, dimension, target, readback, raw):\n    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**\n\n    本脚本刻意不 import 项目包（单文件契约），所以按需把 src 挂上 sys.path。\n    """\n    try:\n        from pathlib import Path\n        source = str(Path(__file__).resolve().parents[1])\n        if source not in sys.path:\n            sys.path.insert(0, source)\n        from qi import tier_guard\n        tier_guard.record_choice("deepseek", target=target, readback=readback,\n                                 dimension=dimension, hwnd=hwnd, raw=raw)\n    except Exception:                                  # noqa: BLE001 - 回执不是主流程\n        pass\n\n\ndef main() -> int:\n    name = os.path.basename(__file__)\n    need, wait, force, rest = guard.parse_guard_args(list(sys.argv[1:]))\n    o = _parse(rest)\n    if o.get("help"):\n        usage(name)\n        return 0\n    if o["list"] and (o["think"] or o["search"]):\n        o["bad"].append("--list 不能与 --think/--search 同用")\n    if o["bad"]:\n        print("参数有问题：", o["bad"], "（写错参数不会被当成提问）")\n        usage(name)\n        return 2\n\n    lib = Library()\n    print(lib.report(REQUIRED))\n    if lib.missing(REQUIRED):\n        return 3\n\n    if not guard.guard(need, wait, force):\n        return 4\n    s = Screen()\n    hwnd = ensure_site("deepseek", tab=o["tab"])\n    if hwnd is None:\n        print("开不出浏览器窗口（检查 Chrome 路径）")\n        return 1\n    s.bind(hwnd)\n    if not s.focus():\n        print("窗口提不到前台")\n        return 1\n    s.tab_site = "deepseek"\n    time.sleep(1.2)\n    try:\n        # 页面缩放预检（CARD-06b 第 6 条）：非 100% 先 Ctrl+0，仍不是就退出\n        zoom_mod.ensure(s, verbose=True)\n        if o["list"]:\n            print(json.dumps(list_state(s), ensure_ascii=False))\n            return 0\n\n        todo = {k: v for k, v in (("think", o["think"]), ("search", o["search"])) if v}\n        if not todo:\n            usage(name)\n            return 2\n        rc = 0\n        for kind, want in todo.items():\n            r = set_state(s, kind, want)\n            print(json.dumps(r, ensure_ascii=False))\n            if not r.get("ok"):\n                return 1\n            # 只有「深度思考」算档位（本端没有模型选择器）；搜索开关不进档位表。\n            # 回执失败不影响选择结果（CARD-06a (a)）。\n            if kind == "think":\n                try:\n                    rec = globals().get("_record_receipt")\n                    if callable(rec):\n                        rec(hwnd, "think", want, r.get("after"), r)\n                except Exception:                      # noqa: BLE001\n                    pass\n        return rc\n    except ForegroundLost as e:\n        print("失败:", json.dumps({"ok": False, "stage": "user_took_over",\n                                  "hint": str(e)}, ensure_ascii=False))\n        return 4\n    except zoom_mod.ZoomNot100 as e:\n        result = {**e.as_result(), "ok": False}\n        failure.record("deepseek", result, screen=s, hwnd=hwnd)\n        print("失败:", json.dumps(result, ensure_ascii=False))\n        return e.code\n\n\nif __name__ == "__main__":\n    console.setup()          # 入口编码：GBK 控制台不再被 \\u200b 打崩（CARD-08 第 1 条）\n    raise SystemExit(uilock.entry("deepseek_choose", main))\n'

# ---------------------------------------------------------------------------
# 内嵌完整 Use（聚焦输入框 / 发送消息 / 等待完成 / 读取回复）原版源码
# ---------------------------------------------------------------------------
_USE_SOURCE = '"""deepseek 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。\n\n用法：\n    python deepseek_use.py "只回复两个字：收到"\n    python deepseek_use.py "..." --continue\n    python deepseek_use.py "..." --timeout 300\n\n本端要点：\n- 输入框锚点是**麦克风图标**，点它本身会触发语音，所以按偏移 (-300,-65) 点到文字行。\n- 发送键深蓝=可点、浅蓝=不可点，但模板匹配分不出来（都是 1.000），\n  所以靠颜色判断：B-R > 100 才算可点。\n- 回答动作条是整排六连排，全屏唯一，复制键在排内左侧。\n"""\n\nfrom __future__ import annotations\n\nimport os\nimport sys\n\n_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\nif _SRC not in sys.path:\n    sys.path.insert(0, _SRC)\n\nfrom qi import Library, console, uilock  # noqa: E402\nfrom qi.flow import Site, cli  # noqa: E402\n\nSITE = Site(\n    name="deepseek",\n    title_hint="Google Chrome",\n    composer="ds_composer",\n    send="ds_send_on",\n    actions="ds_actions",\n    copy="ds_copy",\n    composer_offset=(-300, -65),\n    newchat="ds_newchat",\n    stop="ds_stop",\n    send_mode="color",\n    send_blue_min=100.0,\n    # B19：三处只在 deepseek 打开的加固。\n    # 1) 粘贴必须确认落地（2450 字时只写进前 3 个字的根因）\n    write_verify=True,\n    # 2) 输入框里若是本提问的前缀，认成自己上一轮的残草，清掉继续\n    adopt_self_draft=True,\n    # 3) 判空以输入框文本为准：有草稿时发送键本来就深蓝，旧判据会把\n    #    自己的残草当用户草稿，之后每次 draft_present\n    empty_by_text=True,\n    # 4) 单次粘贴超过 2000 字会被整段吞掉（实测 2000 进、2001 不进，不截断\n    #    也不报错）；分 1500 字一块粘，总量可以远超 2000。\n    paste_chunk=1500,\n)\n\nREQUIRED = ["ds_composer", "ds_send_on", "ds_actions", "ds_copy"]\n\n\ndef main() -> int:\n    lib = Library()\n    need = list(REQUIRED)\n    if "--new" in sys.argv[1:] or not any(a.startswith("--") for a in sys.argv[1:]):\n        need.append("ds_newchat")\n    print(lib.report(need))\n    if lib.missing(need):\n        return 3\n    return cli(SITE, sys.argv[1:])\n\n\nif __name__ == "__main__":\n    console.setup()          # 入口编码：GBK 控制台不再被 \\u200b 打崩（CARD-08 第 1 条）\n    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\\ui.lock\n    raise SystemExit(uilock.entry("deepseek_use", main))\n'


def _strip_entry(src: str) -> str:
    for marker in ('if __name__ == "__main__":', "if __name__ == '__main__':"):
        idx = src.rfind(marker)
        if idx != -1:
            return src[:idx]
    return src


def _populate_subspace(mod: types.ModuleType, label: str, source: str) -> types.ModuleType:
    mod.__file__ = __file__
    mod.__package__ = __package__ or "scripts"
    if getattr(mod, "__spec__", None) is None:
        mod.__spec__ = importlib.machinery.ModuleSpec(mod.__name__, None, origin=__file__)
    # Python 3.10: from __future__ import annotations + @dataclass 会经
    # sys.modules 查 cls.__module__；exec 前不注册会抛 AttributeError:/n    # 'NoneType' object has no attribute '__dict__'。
    sys.modules[mod.__name__] = mod
    code_no_entry = _strip_entry(source)
    exec(compile(code_no_entry, __file__, "exec"), mod.__dict__)
    return mod


_choose_ns: types.ModuleType | None = None
_use_ns: types.ModuleType | None = None


def get_choose_module(force_reload: bool = False) -> types.ModuleType:
    global _choose_ns
    if _choose_ns is None:
        _choose_ns = types.ModuleType("scripts.deepseek_choose")
        _populate_subspace(_choose_ns, "deepseek_choose", _CHOOSE_SOURCE)
    elif force_reload:
        _populate_subspace(_choose_ns, "deepseek_choose", _CHOOSE_SOURCE)
    return _choose_ns


def get_use_module(force_reload: bool = False) -> types.ModuleType:
    global _use_ns
    if _use_ns is None:
        _use_ns = types.ModuleType("scripts.deepseek_use")
        _populate_subspace(_use_ns, "deepseek_use", _USE_SOURCE)
    elif force_reload:
        _populate_subspace(_use_ns, "deepseek_use", _USE_SOURCE)
    return _use_ns


def __getattr__(attr: str):
    """动态暴露原 use / choose 模块顶层符号，确保离线单元测试零改动通过。"""
    u = get_use_module()
    if hasattr(u, attr):
        return getattr(u, attr)
    c = get_choose_module()
    if hasattr(c, attr):
        return getattr(c, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")


def _detect_mode(argv: list[str]) -> tuple[str, list[str]]:
    """判断当前调用是 choose（选模/列表/探针）还是 use（发消息/读回复）。"""
    if argv and argv[0] in ("choose", "use"):
        return argv[0], argv[1:]
    if argv and argv[0] == "read":
        return "use", ["--read"] + argv[1:]

    use_only_flags = {"--read", "--continue", "--task", "--chat", "--purpose", "--timeout"}
    for a in argv:
        if a in use_only_flags or any(a.startswith(f + "=") for f in use_only_flags):
            return "use", argv

    choose_only_flags = {"--list", "-l", "--probe", "--ready-wait"}
    for a in argv:
        if a in choose_only_flags or any(a.startswith(f + "=") for f in choose_only_flags):
            return "choose", argv

    choose_value_flags = {"--model", "-m", "--effort", "-e", "--writer", "-w", "--think", "--search", "--tab", "--idle-need", "--idle-wait"}
    i = 0
    positional = []
    while i < len(argv):
        tok = argv[i]
        if tok in choose_value_flags:
            i += 2
            continue
        if any(tok.startswith(f + "=") for f in choose_value_flags):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positional.append(tok)
        i += 1

    if positional:
        return "use", argv
    return "choose", argv


def main(argv: list[str] | None = None) -> int:
    from qi import uilock

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    mode, rest = _detect_mode(raw_argv)
    saved_argv = sys.argv[:]
    try:
        sys.argv = [sys.argv[0]] + rest
        if mode == "choose":
            mod = get_choose_module()
            return int(uilock.entry("deepseek_choose", mod.main))
        else:
            mod = get_use_module()
            return int(uilock.entry("deepseek_use", mod.main))
    finally:
        sys.argv = saved_argv


if __name__ == "__main__":
    raise SystemExit(main())
