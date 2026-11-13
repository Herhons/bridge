#!/usr/bin/env python3
"""gemini 独立全功能脚本（完整包含 gemini 的模型/档位选择、消息发送、回复读取全部逻辑）。

用法：
  1. 显式子命令（推荐，主流程 server 与总控统一调用方式）：
     python src/scripts/gemini.py choose --list
     python src/scripts/gemini.py choose --model <模型> [--effort <档位>] [--think on|off]
     python src/scripts/gemini.py use "你的问题" [--purpose <用途>] [--timeout <秒>]
     python src/scripts/gemini.py use --read

  2. 直接参数自动路由（100% 兼容原 gemini_choose.py 与 gemini_use.py 参数）：
     python src/scripts/gemini.py --list
     python src/scripts/gemini.py --read
     python src/scripts/gemini.py "你的问题"
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

_EXPECTED_CHOOSE_SYMBOLS = ['CFG', 'CHECK_INK_BOX', 'CHIP_MARKS', 'DEBUG_DIR', 'MIC_TO_CARET', 'REQUIRED', '_SRC', '_check_ink', '_chip_missing_hint', '_chip_node', '_chip_probe', '_chip_tail', '_evidence', '_is_conversation_url', '_is_new_chat_url', '_items_visible', '_model_from_uia', '_open_model_menu', '_record_receipt', '_win', 'main', 'read_model', 'set_model']
_EXPECTED_USE_SYMBOLS = ['REQUIRED', 'SITE', 'WRITER', '_SRC', '_mk_prepare', 'main']

# ---------------------------------------------------------------------------
# 内嵌完整 Choose（选择模型 / 强度 / 思考模式 / 状态回读）原版源码
# ---------------------------------------------------------------------------
_CHOOSE_SOURCE = '"""gemini 网页版：只做选择，不发提问。\n\n用法：\n    python gemini_choose.py --list\n    python gemini_choose.py --model flash_lite|flash|pro|argon\n\n实测菜单（深色主题）：\n    3.5 Flash-Lite / 极速回答      ← 默认\n    3.8 Flash      / 全方位帮助\n    3.1 Pro        / 高级推理\n    4 Argon        / 复杂任务\n    思考等级 ›     （子菜单，本脚本没做）\n\n芯片是右对齐的，宽度随模型名变；所以开菜单靠"点当前芯片"，不按固定偏移点。\n"""\n\nfrom __future__ import annotations\n\nimport argparse\nimport json\nimport os\nimport sys\nimport time\nfrom urllib.parse import urlsplit\n\nimport cv2\n\n_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\nif _SRC not in sys.path:\n    sys.path.insert(0, _SRC)\n\nfrom qi import ForegroundLost, Library, Screen, console, uilock  # noqa: E402\nfrom qi import failure, guard  # noqa: E402\nfrom qi import zoom as zoom_mod  # noqa: E402\nfrom qi.browser import ensure_site  # noqa: E402\nfrom qi.choose import Chooser, choose_writer, read_writer  # noqa: E402\nfrom qi.flow import Site, _open_new_chat  # noqa: E402\n\nCFG = Chooser(\n    name="gemini",\n    anchor="gem_composer",\n    open_offset=(0, 0),\n    prefix="gem_",\n    options={\n        "flash_lite": "3.5 Flash-Lite（极速回答，默认）",\n        "flash": "3.8 Flash（全方位帮助）",\n        "pro": "3.1 Pro（高级推理）",\n        "argon": "4 Argon（复杂任务）",\n    },\n    has_chip=("flash_lite", "flash", "pro", "argon"),\n    writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),\n    writers={"dialog": 0, "task": 1},\n    open_by_chip=True,\n)\n\n# 思考等级（--think）**未实现**，相关代码和模板已删除。\n# 实测：子菜单只在鼠标真实悬停时展开，脚本点不开（点一下会把整个菜单关掉）。\n# 之前这里留着一整套 _open_think / read_think / set_think，但 --think 分支直接返回\n# not_supported，那些函数从来没被调用过，属于死代码，已清掉。\n\n\ndef _win(s):\n    """目标窗口矩形。不限定就是在整个虚拟桌面里找，主屏上开着同款页面会先命中那边。"""\n    return s.window_rect(CFG.title_hint, on_secondary=CFG.on_secondary)\n\n\ndef _items_visible(s) -> bool:\n    return any(s.find(f"gem_item_{k}", threshold=0.85, region=_win(s)) is not None for k in CFG.options)\n\n\nDEBUG_DIR = os.path.join(_SRC, "..", "evidence", "fail")\n\n# 芯片按钮的 UIA 名字（实测形态：`打开模式选择器，当前模式为“Argon”`）。\n# Gemini 把"模型"叫"模式"，两个词都收，换文案时至少中一个。\nCHIP_MARKS = ("当前模式为", "当前模型为")\n\n\ndef _chip_probe(s):\n    """只读地找芯片按钮，并带回"为什么没找到"：(node, 名字, why, url)。\n\n    why:\n      · ok                —— 找到了\n      · document_unbound  —— 连 Gemini 文档都没绑上（窗口里不是 Gemini 页 / 读不到）\n      · no_chip           —— 文档绑上了，但页面上没有模型芯片\n\n    2026-11-07 现场（05:17:27，hwnd=67552）：只回了 `menu_not_open/no_chip`，\n    分不清"不是 Gemini 页"和"这一页本来就没有芯片"。两种原因要分开报，\n    否则下一个人只能靠猜（本次只读复核：同一个 hwnd 在 `/app` 新对话页上\n    芯片是 `打开模式选择器，当前模式为“Flash”`，read_model=flash/source=uia）。\n    """\n    try:\n        from qi import ui\n        control, url = ui.document(s.target_hwnd, "gemini")\n        root = ui.tree(control)\n    except Exception as exc:                # noqa: BLE001 - UIA 不可用就退回模板路径\n        return None, "", "document_unbound", f"{type(exc).__name__}: {exc}"\n    for node in root.walk():\n        if node.kind != "ButtonControl":\n            continue\n        name = (node.name or "").strip()\n        if name and any(mark in name for mark in CHIP_MARKS):\n            return node, name, "ok", url\n    return None, "", "no_chip", url\n\n\ndef _chip_node(s):\n    """当前模型芯片的 UIA 按钮：(node, 名字) 或 None。\n\n    为什么要有它：旧实现靠"麦克风往左 68px"这个**像素偏移**点芯片，\n    芯片宽度/位置一变就点到空白，表现就是 menu_not_open。\n    按钮名是**控制身份**，不受布局影响；点它自己也不会点错。\n    """\n    node, name, _why, _url = _chip_probe(s)\n    return None if node is None else (node, name)\n\n\ndef _is_new_chat_url(url) -> bool:\n    """模型芯片只在新对话页（`/app`）出现；会话页是 `/app/<id>`。"""\n    try:\n        return urlsplit(str(url or "")).path.rstrip("/") == "/app"\n    except (TypeError, ValueError):\n        return False\n\n\ndef _is_conversation_url(url) -> bool:\n    """看起来就是一个 Gemini 会话页（`gemini.google.com/app/<id>`）。"""\n    try:\n        parts = urlsplit(str(url or ""))\n    except (TypeError, ValueError):\n        return False\n    host = (parts.hostname or "").lower()\n    path = parts.path.rstrip("/")\n    return host.endswith("gemini.google.com") and path.startswith("/app/") and len(path) > 5\n\n\ndef _chip_missing_hint(why: str, url) -> str:\n    """芯片找不到时的**准确**原因（不是"再试试"）。"""\n    if why == "document_unbound":\n        return f"当前窗口的活动文档不是 Gemini 页面（或读不到）：{url}"\n    if _is_conversation_url(url):\n        return (f"模型选择器只在新对话页（/app）出现，当前是 {url}："\n                "先 --new 开新对话再选模型")\n    return "找不到模型芯片（UIA 和麦克风锚点都没认出来）"\n\n\ndef _chip_tail(name: str) -> str:\n    """从按钮名里取档位词：`…当前模式为“Argon”` -> `Argon`。"""\n    for mark in CHIP_MARKS:\n        if mark in name:\n            tail = name.split(mark, 1)[1]\n            return tail.strip().strip("“”\\"\'’‘›>").strip()\n    return ""\n\n\ndef _model_from_uia(s, probe=None):\n    """UIA 读模型：读不到芯片返回 None（交给菜单路径）；读到但认不出返回 unknown。"""\n    node, name, _why, _url = probe or _chip_probe(s)\n    if node is None:\n        return None\n    tail = _chip_tail(name) or name\n    key = None\n    try:\n        from qi.chip import GEMINI_MODEL_NAMES\n        key = GEMINI_MODEL_NAMES.get(tail.lower())\n    except Exception:                       # noqa: BLE001\n        key = None\n    if key is None:\n        low = tail.lower()\n        key = next((k for k, label in CFG.options.items()\n                    if k.replace("_", " ") in low or label.split("（")[0].strip().lower() in low),\n                   None)\n    if key in CFG.options:\n        return {"model": key, "text": name, "chip": tail, "source": "uia"}\n    return {"model": "unknown", "text": name, "chip": tail, "source": "uia",\n            "hint": f"UIA 芯片读到 {tail!r}，不在已知模型清单里，不能确认"}\n\n\n# 芯片右端的下拉箭头相对**麦克风**的偏移。\n# 不能对输入框锚点用固定偏移：芯片左边界随模型名长短变（Flash-Lite 宽、Pro 短），\n# 固定偏移在名字短的时候会点到空白，表现为"菜单打不开"。\n# 麦克风是右对齐的，位置不变，用它当基准稳。\n# **2026-11-07 起这只是兜底**：优先点 UIA 认出来的芯片按钮本身（见 _chip_node）。\nMIC_TO_CARET = (-68, 0)\n\n\ndef _open_model_menu(s):\n    """打开模型菜单。返回 "open" / "clicked_unverified" / "no_chip"。\n\n    顺序：已经是开的 -> 点 UIA 芯片按钮 -> 老办法（麦克风往左 68px）。\n    点完一律用"菜单项能不能匹到"确认，**不确认就不往下点**。\n    """\n    if _items_visible(s):\n        return "open"\n    s.press("esc")\n    time.sleep(0.3)\n    got = _chip_node(s)\n    if got is not None:\n        node, _name = got\n        clicked = False\n        try:\n            from qi import ui\n            ui.click(s, node)\n            time.sleep(1.1)\n            clicked = True\n        except Exception:                   # noqa: BLE001 - 控件失效就退回偏移\n            clicked = False\n        if _items_visible(s):\n            return "open"\n        if clicked:\n            # 芯片点到了但菜单项模板一个都没匹上：模板过期的典型症状，别再乱点\n            return "clicked_unverified"\n    base = s.find("gem_mic", threshold=0.85, region=_win(s)) or s.find(\n        "gem_send_on", threshold=0.85, region=_win(s))\n    if base is None:\n        return "no_chip"\n    s.click(base, offset=MIC_TO_CARET)\n    time.sleep(1.1)\n    return "open" if _items_visible(s) else "clicked_unverified"\n\n\ndef _evidence(s, tag: str) -> str | None:\n    """菜单/选项读不出来时留一张现场图，方便重截模板。失败不影响主流程。"""\n    try:\n        win = _win(s)\n        if not win:\n            return None\n        img = s.grab(win)\n        if img is None or not getattr(img, "size", 0):\n            return None\n        os.makedirs(DEBUG_DIR, exist_ok=True)\n        path = os.path.join(DEBUG_DIR, f"gemini_{tag}_{time.strftime(\'%Y%m%d-%H%M%S\')}.png")\n        cv2.imwrite(path, img)\n        return path\n    except Exception:                       # noqa: BLE001\n        return None\n\n\nCHECK_INK_BOX = (-26, 3, 22, 22)      # 勾的取样窗：相对标题模板左上角 (dx, dy, w, h)\n\n\ndef _check_ink(band) -> int:\n    """采样窗里"勾"的亮像素数；**不是勾的形状就返回 0**。\n\n    为什么不能只数亮像素：菜单刚打开时**第一项可能带一个白色焦点框**（2026-11-02 实测），\n    框的左边线正好擦着采样窗的左边，能数出几十个亮像素，比真勾（30）还多——\n    只数像素会把"焦点停在第一项"误读成"选的就是第一项"，而它恰恰不是。\n    勾是 11 列 × 8 行的一小坨弯线；焦点框的边是 2~3 列、二十来行的直线。\n    所以要求列数 ≥5：直线过不了，勾过得去。\n    """\n    g = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)\n    mask = g > 100\n    cols = int(mask.any(axis=0).sum())\n    rows = int(mask.any(axis=1).sum())\n    if cols < 5 or rows < 3:\n        return 0\n    return int(mask.sum())\n\n\ndef read_model(s) -> dict:\n    """读当前模型：优先 UIA 芯片按钮（精确文本 + 控制身份），再退回"开菜单看勾"。\n\n    2026-11-07：旧实现只会开菜单看勾，而开菜单靠"麦克风往左 68px"这个像素偏移；\n    偏移一失效就同时得到 menu_not_open 和 model=unknown（执行方看到的就是这两个）。\n    UIA 按钮名（`…当前模式为“Argon”`）不依赖布局，读到就是读到，读不到才走老路。\n    """\n    probe = _chip_probe(s)\n    uia = _model_from_uia(s, probe)\n    if uia is not None:\n        return uia\n    node, _name, why, url = probe\n    # 文档都没绑上 / 是会话页：**先把原因报清楚，不去点**（老实现会去点麦克风\n    # 左边 68px 那块空白，既证明不了什么，也可能点到别的东西）。\n    if node is None and why in ("document_unbound", "no_chip") \\\n            and (why == "document_unbound" or _is_conversation_url(url)):\n        return {"model": "unknown", "source": "none", "detail": why,\n                "stage": "menu_not_open", "url": url, "clicks": 0,\n                "hint": _chip_missing_hint(why, url)}\n    st = _open_model_menu(s)\n    if st != "open":\n        no_chip = st == "no_chip"\n        return {"model": "unknown", "source": "none",\n                "detail": (why or "no_chip") if no_chip else st,\n                "stage": "menu_not_open" if no_chip else "menu_items_unreadable",\n                "url": url,\n                "hint": (_chip_missing_hint(why, url) if no_chip\n                         else "芯片点到了，但菜单项模板一个都没匹到：模板可能过期，"\n                              "按卡面不下调阈值、不猜着点")}\n    ink = {}\n    dx, dy, w, h = CHECK_INK_BOX\n    for k in CFG.options:\n        m = s.find(f"gem_item_{k}", threshold=0.88, region=_win(s))\n        if m is not None:\n            ink[k] = _check_ink(s.grab((m.x + dx, m.y + dy, w, h)))\n    s.press("esc")\n    time.sleep(0.3)\n    if not ink:\n        return {"model": "unknown", "source": "menu",\n                "hint": "模型项一个都没定位到"}\n    best = max(ink, key=ink.get)\n    return {"model": best if ink[best] > 5 else "unknown", "ink": ink, "source": "menu"}\n\n\ndef set_model(s, key: str) -> dict:\n    if key not in CFG.options:\n        return {"ok": False, "stage": "bad_arg", "want": key, "options": list(CFG.options)}\n    # 先做**只读**判断：页面不对就一个东西都不点（连 Esc 都不按）。\n    # 会话页（/app/<id>）没有模型选择器；老实现会去点"麦克风左边 68px"那块空白。\n    node, _name, why, url = _chip_probe(s)\n    if node is None and why == "document_unbound":\n        return {"ok": False, "stage": "document_unbound", "before": None, "want": key,\n                "detail": url, "clicks": 0,\n                "hint": "当前窗口的活动文档不是 Gemini 页面（或读不到）：先切到 Gemini 标签页；"\n                        "本次没有点任何东西"}\n    if node is None and why == "no_chip" and _is_conversation_url(url):\n        return {"ok": False, "stage": "page_not_new_chat", "before": None, "want": key,\n                "url": url, "clicks": 0,\n                "hint": "模型选择器只在新对话页（/app）出现：先 --new 开新对话再选；"\n                        "本次没有点任何东西"}\n    cur = read_model(s)["model"]\n    if cur == key:\n        return {"ok": True, "stage": "already", "before": cur, "after": cur}\n    st = _open_model_menu(s)\n    if st != "open":\n        shot = _evidence(s, "menu" if st == "no_chip" else "items")\n        return {"ok": False,\n                "stage": "menu_not_open" if st == "no_chip" else "menu_items_unreadable",\n                "before": cur, "want": key, "detail": st, "evidence_shot": shot,\n                "hint": (_chip_missing_hint(why, url) if st == "no_chip" else\n                         "芯片点到了但菜单项模板全部失配（模板可能过期）："\n                         "已留现场图，未点任何选项、未下调阈值")}\n    m = s.find(f"gem_item_{key}", threshold=0.88, region=_win(s))\n    if m is None:\n        s.press("esc")\n        return {"ok": False, "stage": "item_not_found", "before": cur, "want": key}\n    s.click(m)\n    time.sleep(1.2)\n    _w = _win(s)\n    if _w:\n        s.move_to(_w[0] + 60, _w[1] + 60)   # 移开鼠标，别压在菜单/芯片上\n    time.sleep(0.4)\n    after = read_model(s)["model"]\n    return {"ok": after == key, "stage": "done" if after == key else "unverified",\n            "before": cur, "after": after}\n\n\nREQUIRED = ([f"gem_item_{k}" for k in CFG.options]\n            + ["gem_mic"]\n            + list(CFG.writer_tabs))\n\n\ndef _record_receipt(hwnd, dimension, target, readback, raw):\n    """写档位回执（CARD-06a (a)）。**失败一律不影响选择结果。**\n\n    读回为空/读不回原名时传 None：use 会因此拒发——那是实话。\n    """\n    try:\n        from qi import tier_guard\n        tier_guard.record_choice("gemini", target=target, readback=readback,\n                                 dimension=dimension, hwnd=hwnd, raw=raw)\n    except Exception:                                  # noqa: BLE001 - 回执不是主流程\n        pass\n\n\ndef main() -> int:\n    need, wait, force, argv = guard.parse_guard_args(list(sys.argv[1:]))\n    parser = argparse.ArgumentParser(description="选择 Gemini 写入器和模型，不发送消息")\n    parser.add_argument("--list", "-l", action="store_true", help="读取当前模式和模型")\n    parser.add_argument("--new", action="store_true", help="先开新对话")\n    parser.add_argument("--writer", choices=CFG.writers, type=str.lower)\n    parser.add_argument("--model", choices=CFG.options, type=str.lower)\n    parser.add_argument("--think", help="思考等级（当前未实现）")\n    parser.add_argument("--tab", type=int, choices=range(1, 10))\n    args = parser.parse_args(argv)\n    if args.list and (args.writer is not None or args.model is not None or args.think is not None):\n        parser.error("--list 不能与 --writer、--model、--think 同用；--new --list 可以同用")\n    if not (args.list or args.new or args.writer or args.model or args.think):\n        parser.print_help()\n        return 2\n    if args.think is not None:\n        print(json.dumps({"ok": False, "stage": "not_supported",\n                          "hint": "Gemini 思考等级尚未实现"}, ensure_ascii=False))\n        return 1\n\n    lib = Library()\n    required = REQUIRED + (["gem_newchat"] if args.new else [])\n    print(lib.report(required))\n    if lib.missing(required):\n        return 3\n    if not guard.guard(need, wait, force):\n        return 4\n    sc = Screen()\n    hwnd = ensure_site("gemini", tab=args.tab)\n    if hwnd is None:\n        print("没有可操作的 Gemini 标签页")\n        return 1\n    sc.bind(hwnd)\n    if not sc.focus():\n        print("窗口提不到前台")\n        return 1\n    sc.tab_site = "gemini"\n    time.sleep(1.2)\n\n    try:\n        # 页面缩放预检（CARD-06b 第 6 条）：非 100% 先 Ctrl+0，仍不是就退出\n        zoom_mod.ensure(sc, verbose=True)\n        if args.new:\n            site = Site(name="gemini", title_hint=CFG.title_hint, composer="gem_composer",\n                        send="gem_send_on", actions="gem_actions", copy="gem_copy",\n                        composer_offset=(200, 0), newchat="gem_newchat")\n            result = _open_new_chat(sc, site)\n            print(json.dumps(result, ensure_ascii=False))\n            if not result["ok"]:\n                return 1\n        if args.list:\n            writer = read_writer(sc, CFG)["writer"]\n            out = {"writer": writer, **read_model(sc)}\n            if writer in (None, "unknown"):\n                out["note"] = "模式按钮只在新对话页出现；可用 --new --list 开新对话后读取。"\n            print(json.dumps(out, ensure_ascii=False))\n            # 模式读不出来也算失败：返回 0 会让验证器把"没读到"记成通过\n            bad = out["model"] == "unknown" or writer in (None, "unknown")\n            return 1 if bad else 0\n        if args.writer is not None:\n            result = choose_writer(sc, CFG, args.writer)\n            print(json.dumps(result, ensure_ascii=False))\n            if not result["ok"]:\n                return 1\n        if args.model is not None:\n            result = set_model(sc, args.model)\n            print(json.dumps(result, ensure_ascii=False))\n            if not result["ok"]:\n                return 1\n            rec = globals().get("_record_receipt")\n            if callable(rec):\n                rec(hwnd, "model", args.model,\n                    None if result.get("after") in (None, "unknown") else result.get("after"),\n                    result)\n    except ForegroundLost as exc:\n        print(json.dumps({"ok": False, "stage": "user_took_over", "hint": str(exc)},\n                         ensure_ascii=False))\n        return 4\n    except zoom_mod.ZoomNot100 as exc:\n        result = {**exc.as_result(), "ok": False}\n        failure.record("gemini", result, screen=sc, hwnd=hwnd)\n        print(json.dumps(result, ensure_ascii=False))\n        return exc.code\n    return 0\n\n\nif __name__ == "__main__":\n    console.setup()          # 入口编码：GBK 控制台不再被 \\u200b 打崩（CARD-08 第 1 条）\n    raise SystemExit(uilock.entry("gemini_choose", main))\n'

# ---------------------------------------------------------------------------
# 内嵌完整 Use（聚焦输入框 / 发送消息 / 等待完成 / 读取回复）原版源码
# ---------------------------------------------------------------------------
_USE_SOURCE = '"""gemini 网页版：提问 → 收回答。判据在 qi/gemini.py，这里只放本端配置。\n\n用法：\n    python gemini_use.py "只回复两个字：收到"\n    python gemini_use.py "帮我把这批文件按日期归类" --task      ← 任务（计划）模式\n    python gemini_use.py "..." --continue\n    python gemini_use.py --read                                 ← 只读最后一条回复，不发送\n\n本端要点（都是实测量出来的）：\n- 输入框锚点是左下角固定的「+」，按偏移 (200,0) 点进文字行。\n- 空输入时右侧只有麦克风；输入文字后才出现**紫色圆形发送键**，\n  所以"发送键出现了"就代表写进去了。\n- 回答动作条是 赞/踩/重新生成/**复制**/更多 —— 复制在**第 4 个**，不在第一个。\n- 页面是深色主题，模板也是深色下截的，换主题要重截。\n- **点「发起新对话」会把模式重置回对话**，所以要用任务模式必须"先新对话、再切任务"。\n  顺序反了等于白切（这是之前的 bug）。\n\n2026-11-01 修（CARD-GEM-fix-reply-detect）：判据从「整串精确相等 + 模板」改成\n「控件树切块 + 动作条 + 文本稳定 3 秒」，发送键模板降级为兜底。原因见 qi/gemini.py\n开头：11:4x 那次提问气泡是折叠的（尾部「…」+ 前缀「你说」），旧判据永远匹配不上，\n240s 白等到超时，而回复其实早就写完了。\n"""\n\nfrom __future__ import annotations\n\nimport os\nimport sys\n\n_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\nif _SRC not in sys.path:\n    sys.path.insert(0, _SRC)\n\nfrom qi import Library, console, gemini, uilock  # noqa: E402\nfrom qi.choose import Chooser, choose_writer  # noqa: E402\nfrom qi.flow import Site  # noqa: E402\n\nSITE = Site(\n    name="gemini",\n    title_hint="Google Chrome",\n    composer="gem_composer",\n    send="gem_send_on",\n    actions=("gem_actions", "gem_actions_task"),   # 对话 6 个图标 / 任务 4 个图标，两排模板\n    copy="gem_copy",\n    composer_offset=(200, 0),\n    newchat="gem_newchat",\n    sidebar=None,            # 侧栏是一直展开的，没有折叠态\n    send_mode="exists",\n    send_threshold=0.88,\n    # B19：gemini 的占位文字不在公共 KNOWN_PLACEHOLDERS 里，`flow.clear_composer`\n    # 会把它当成"还有 10 个字没清掉"（真机报 draft_clear_failed）。这里补上。\n    placeholders=("问问 Gemini", "Ask Gemini"),\n)\n\n# 模式切换用（对话 / 任务）。模板是"整组按钮"各一张，读法见 qi/choose.py。\nWRITER = Chooser(\n    name="gemini",\n    anchor="gem_composer",\n    open_offset=(0, 0),\n    prefix="gem_",\n    writer_tabs=("gem_writer_tabs_dialog", "gem_writer_tabs_task"),\n    writers={"dialog": 0, "task": 1},\n)\n\nREQUIRED = ["gem_composer", "gem_send_on", "gem_actions", "gem_actions_task", "gem_copy"]\n\n\ndef _mk_prepare(want: str):\n    def fn(s):\n        return choose_writer(s, WRITER, want)\n    return fn\n\n\ndef main() -> int:\n    argv = list(sys.argv[1:])\n    reading = "--read" in argv          # 只读模式：不发送，也不需要模板\n    want = None if (reading or "--continue" in argv) else "dialog"\n    if not reading:\n        if "--task" in argv and "--chat" in argv:\n            print("--task 和 --chat 不能同用")\n            return 2\n        if "--task" in argv:\n            argv.remove("--task")\n            want = "task"\n        elif "--chat" in argv:\n            argv.remove("--chat")\n            want = "dialog"\n\n        # 模式按钮只在新对话页出现，所以用 --task/--chat 时强制先回新对话页\n        if want is not None and "--continue" in argv:\n            print("当前会话不能切换写入器；使用 --new --task 或 --new --chat")\n            return 2\n\n        lib = Library()\n        need = list(REQUIRED) + list(WRITER.writer_tabs)\n        if "--new" in argv or not any(a.startswith("--") for a in argv) or want:\n            need.append("gem_newchat")\n        print(lib.report(need))\n        if lib.missing(need):\n            return 3\n\n    prepare = _mk_prepare(want) if want else None\n    return gemini.cli(\n        SITE, argv,\n        prepare=prepare,\n        extra_help="  --task      发到「任务（计划）」模式：先新对话，再切任务，然后提交\\n"\n                   "  --chat      发到「对话」模式（默认）\\n"\n                   "  --read      只读当前标签页最后一条回复，不发送",\n    )\n\n\nif __name__ == "__main__":\n    console.setup()          # 入口编码：GBK 控制台不再被 \\u200b 打崩（CARD-08 第 1 条）\n    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\\ui.lock\n    raise SystemExit(uilock.entry("gemini_use", main))\n'


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
        _choose_ns = types.ModuleType("scripts.gemini_choose")
        _populate_subspace(_choose_ns, "gemini_choose", _CHOOSE_SOURCE)
    elif force_reload:
        _populate_subspace(_choose_ns, "gemini_choose", _CHOOSE_SOURCE)
    return _choose_ns


def get_use_module(force_reload: bool = False) -> types.ModuleType:
    global _use_ns
    if _use_ns is None:
        _use_ns = types.ModuleType("scripts.gemini_use")
        _populate_subspace(_use_ns, "gemini_use", _USE_SOURCE)
    elif force_reload:
        _populate_subspace(_use_ns, "gemini_use", _USE_SOURCE)
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
            return int(uilock.entry("gemini_choose", mod.main))
        else:
            mod = get_use_module()
            return int(uilock.entry("gemini_use", mod.main))
    finally:
        sys.argv = saved_argv


if __name__ == "__main__":
    raise SystemExit(main())
