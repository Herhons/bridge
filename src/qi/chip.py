"""发送前的档位只读复核（CARD-06b 第 1/2 条）。

## 为什么要有这一层

CARD-06a 的 `tier_guard.gate()` 已经能校验回执，但"复核"那一步是**软**的：
`reread` 返回 None 就记 `drift.unchecked` 放行。06b r2 三家会审一致选 A——
**读不到就拒发**，因为"以为切了"和"读回来确认了"是两件事。

所以这里把"读"做成一个明确的返回值：

    {"value": "expert", "source": "template", "template": {...}, "zoom_percent": 100}
    {"value": None,     "source": "none",     "reason": "template_miss", ...}

`value=None` 一律由 `tier_guard` 转成 `reason=tier_unreadable`（退出码 5），
`value != 回执.readback` 转成 `reason=tier_drift`。**不在 use 里自动重选**。

## 两条读法，模板优先、UIA 兜底一次

1. **模板**：`<prefix>chip_<key>.png`，多尺度（0.8–1.25）唯一匹配。
   同一张图多处命中 = 失败（可能是菜单项和芯片同时匹上），绝不"取第一个"。
2. **UIA 文本**（卡面允许的兜底，只读一次）：
      grok    `型号: Expert`                     ← 2026-11-02 实测
      gemini  `打开模式选择器，当前模式为“Argon”`
      chatgpt `选择 ChatGPT 模型` 的子文本 `高` / `GPT-6 Astra 高`
      deepseek `ds_think` 模板 + B-R 颜色（开关两态分数一样，只能看颜色）
   UIA 读到且与回执一致才算确认；读不到/不一致都算没确认。

## 桌面端暂时不在这一层

codex / dsh / opencode 的档位要打开卡片才读得到，开卡片要点两下，已经不算"只读"。
那是 CARD-06c 的适配项；本轮它们在 `gate()` 里显式 `require_readback=False`，
结果里带 `tier_readback="unsupported"`，**不是静默放行**（见 docs/exit-codes.md）。
"""

from __future__ import annotations

from . import zoom

# 有只读档位通道的端（web）。不在表里的端，gate 必须显式关掉 require_readback。
TEMPLATE_CHIPS = {
    "grok": ("fast", "expert"),
    "gemini": ("flash_lite", "flash", "pro", "argon"),
}

CHIP_SCORE = 0.85          # 芯片模板阈值：低于它算"没读到"，不是"差不多"

# 有芯片模板的端，模板读不到时要不要再用 UIA 兜底？
# **默认 False**，理由是可验收性：卡面完成标准要求"故意把 grok 芯片模板换成错图，
# grok_use 必须退出 5（tier_unreadable）"。如果模板失配还能被 UIA 文本洗成确认，
# 那条验收永远过不了，而且等于把"模板该重截了"这件事藏起来。
# UIA 兜底仍然用于**本来就没有芯片模板**的端/档位（chatgpt 的芯片文本、
# deepseek 的开关）——那里没有模板可失配，UIA 是唯一的只读来源。
# 将来若要放宽，把这里改成 True；改之前请先确认验收标准也跟着改。
UIA_FALLBACK_WITH_TEMPLATES = False

# grok 的 UIA 芯片名（2026-11-02 实测）：`型号: Expert`
GROK_PREFIX = "型号:"

# gemini 的 UIA 芯片名：`打开模式选择器，当前模式为“Argon”`
GEMINI_MARK = "当前模式为"

# chatgpt 的 UIA 芯片名（2026-11-02 实测）：按钮 `选择 ChatGPT 模型`，子文本是档位词
CHATGPT_CHIP = "选择 ChatGPT 模型"

# 档位词 -> 内部 key。**长词在前**：先匹配「极高」再匹配「高」，否则永远命中「高」。
# CARD-06c 第 1 条：聊天模式三档是 即时/中/高，档名 instant/mid/high；
# 工作模式的「轻度」才是 low。两个词不会同时出现在同一模式下，所以分开映射。
EFFORT_WORDS = (
    # B17：「最高」是工作模式第 5 档（max）的 UIA 播报词（实测
    # `最高，第 5 项，共 5 项。`）。必须排在「高」前面，否则会先命中「高」判成 high。
    ("最高", "max"),
    ("极高", "xhigh"), ("Extra High", "xhigh"), ("extra high", "xhigh"),
    ("即时", "instant"), ("Instant", "instant"), ("instant", "instant"),
    ("轻度", "low"), ("Light", "low"), ("light", "low"),
    ("中", "mid"), ("Medium", "mid"), ("medium", "mid"),
    ("高", "high"), ("High", "high"), ("high", "high"),
    ("Max", "max"), ("max", "max"),
)

# 模型显示名 -> key。和 chatgpt_choose.MODELS 的显示名一一对应（测试会核对）。
CHATGPT_MODEL_NAMES = {
    "GPT-6（默认）": "gpt6", "GPT-6": "gpt6",
    "GPT-5.6 Sol": "gpt56", "GPT-5.6 Terra": "gpt56_terra", "GPT-5.6 Luna": "gpt56_luna",
    "GPT-6.1 Sol": "gpt61_sol", "GPT-6.1 Terra": "gpt61_terra",
    "GPT-6 Astra": "gpt6_astra", "GPT-6 Sol": "gpt6_sol", "GPT-6 Luna": "gpt6_luna",
    "默认（推荐模型集）": "default", "默认": "default",
}

GEMINI_MODEL_NAMES = {
    "3.5 flash-lite": "flash_lite", "flash-lite": "flash_lite",
    "3.8 flash": "flash", "flash": "flash",
    "3.1 pro": "pro", "pro": "pro",
    "4 argon": "argon", "argon": "argon",
}

# deepseek 开关的读法与 deepseek_choose 完全一致（模板定位 + B-R 判色）
DS_OFF_MIN, DS_ON_MAX = 25.0, 15.0


def supports(endpoint: str) -> bool:
    return str(endpoint).lower() in ("grok", "gemini", "chatgpt", "deepseek")


def _info(name=None, score=None, threshold=CHIP_SCORE, scale=None, hit=False) -> dict:
    return {"name": name, "score": None if score is None else round(float(score), 3),
            "threshold": threshold, "scale": scale, "hit": bool(hit)}


def _empty(endpoint, reason, *, template=None, raw="") -> dict:
    return {"value": None, "source": "none", "reason": reason,
            "template": template or _info(name=f"{endpoint}_chip"), "raw": raw,
            "zoom_percent": None}


def _cfgs():
    """芯片配置。延迟构造，避免 import 期就依赖 Screen/Chooser。"""
    from .choose import Chooser

    return {
        # B16：与 grok_choose.CFG 保持一致（新聊天页芯片右移 54px）。
        # 这里只用来 read_current，不点菜单，但两处参数不同迟早要出事。
        "grok": Chooser(name="grok", anchor="grok_composer", open_offset=(584, 8),
                        prefix="grok_", options={}, has_chip=TEMPLATE_CHIPS["grok"]),
        "gemini": Chooser(name="gemini", anchor="gem_composer", open_offset=(0, 0),
                          prefix="gem_", options={}, has_chip=TEMPLATE_CHIPS["gemini"],
                          open_by_chip=True),
    }


def _read_templates(screen, endpoint: str, *, calm: bool = True) -> dict:
    """模板读芯片：多尺度 + 唯一性。返回 {"value","source","template","reason"}。

    `calm=False` 给**发送前**用：`_calm` 会按 Esc、移鼠标，这两个动作会被
    `guard.user_moved_since` 当成"用户动了键鼠"，把一次正常发送判成
    user_took_over（2026-11-02 真实踩到）。发送前只读一次，不碰页面。
    """
    from .choose import _calm

    cfg = _cfgs().get(endpoint)
    if cfg is None:
        return {"value": None, "source": "none", "reason": "no_template_reader",
                "template": _info(name=f"{endpoint}_chip")}
    if calm:
        _calm(screen, cfg)
    region = screen.window_rect(cfg.title_hint, on_secondary=cfg.on_secondary)
    hits, best, scores = [], None, []
    for key in cfg.has_chip:
        name = f"{cfg.prefix}chip_{key}"
        got = screen.find_unique(name, threshold=CHIP_SCORE, region=region,
                                 scales=zoom.SCALES)
        match = got.get("match")
        if match is not None and not got.get("ambiguous"):
            hits.append((key, match))
        score = got.get("best")
        info = _info(name=name, score=score, threshold=CHIP_SCORE,
                     scale=got.get("scale"), hit=match is not None)
        scores.append(info)
        if best is None or (score or 0) > (best["score"] or 0):
            best = info
        if got.get("ambiguous"):
            return {"value": None, "source": "none", "reason": "ambiguous",
                    "template": info, "templates": scores}
    if len(hits) == 1:
        key, match = hits[0]
        return {"value": key, "source": "template", "reason": "",
                "template": _info(name=f"{cfg.prefix}chip_{key}", score=match.score,
                                  scale=getattr(match, "scale", 1.0), hit=True),
                "templates": scores}
    if len(hits) > 1:
        return {"value": None, "source": "none", "reason": "ambiguous",
                "template": best, "templates": scores}
    return {"value": None, "source": "none", "reason": "template_miss",
            "template": best or _info(name=f"{endpoint}_chip"), "templates": scores}


# ---------------------------------------------------------------- UIA 兜底
def _tree(screen, endpoint):
    from . import ui

    control, _url = ui.document(screen.target_hwnd, endpoint)
    return ui.tree(control)


def _button_text(node) -> str:
    """按钮自己 + 后代的文本拼起来（chatgpt 的档位词是按钮的子文本）。"""
    parts = [node.name or ""]
    for child in node.walk():
        if child is node:
            continue
        if child.kind in ("TextControl", "ButtonControl") and child.name:
            parts.append(child.name)
    return " ".join(part.strip() for part in parts if part.strip())


def _read_grok_uia(screen, dimension: str) -> dict | None:
    if dimension != "model":
        return None
    for node in _tree(screen, "grok").walk():
        if node.kind != "ButtonControl":
            continue
        text = (node.name or "").strip()
        if text.startswith(GROK_PREFIX):
            value = text[len(GROK_PREFIX):].strip().lower()
            if value:
                return {"value": value, "source": "uia", "raw": text}
    return None


def _read_gemini_uia(screen, dimension: str) -> dict | None:
    if dimension != "model":
        return None
    for node in _tree(screen, "gemini").walk():
        if node.kind != "ButtonControl":
            continue
        text = node.name or ""
        if GEMINI_MARK not in text:
            continue
        tail = text.split(GEMINI_MARK, 1)[1]
        raw = tail.strip(" “”\"'’‘›>").strip()
        key = GEMINI_MODEL_NAMES.get(raw.lower())
        if key:
            return {"value": key, "source": "uia", "raw": text}
    return None


def _parse_effort(text: str) -> str | None:
    for word, key in EFFORT_WORDS:
        if word in text:
            return key
    return None


#: **聊天模式**的芯片只显示短名（实测 raw = "选择 ChatGPT 模型 5.6 即时"）。
#: 长名表按"最长优先"匹配不到时，才用这张兜底表 —— 不去污染长名表，
#: 避免 "6" 这种单字符误命中 "GPT-6.1 Sol" 之类。
CHATGPT_MODEL_SHORT = {"5.6": "gpt56", "6": "gpt6"}


def _parse_model(text: str) -> str | None:
    # 长名优先：GPT-6.1 Sol 不能先命中 GPT-6
    for name in sorted(CHATGPT_MODEL_NAMES, key=len, reverse=True):
        if name in text:
            return CHATGPT_MODEL_NAMES[name]
    for name in sorted(CHATGPT_MODEL_SHORT, key=len, reverse=True):
        if name in text:
            return CHATGPT_MODEL_SHORT[name]
    return None


def chatgpt_snapshot(screen) -> dict:
    """All axes from ONE fresh tree; no menus, clicks, Esc, or inferred model."""
    tree = _tree(screen, "chatgpt")
    states, duplicate = {}, False
    chips = []
    labels = {"聊天": "chat", "Chat": "chat", "工作": "work", "Work": "work"}
    for node in tree.walk():
        if node.kind != "ButtonControl":
            continue
        try:
            if node.control.IsOffscreen:
                continue
        except Exception:
            continue
        name = (node.name or "").strip()
        if name == CHATGPT_CHIP:
            chips.append(node)
        if name in labels:
            key = labels[name]
            try:
                value = int(node.control.GetTogglePattern().ToggleState)
            except Exception:
                continue
            if key in states:
                duplicate = True
            states[key] = value
    on = [key for key, value in states.items() if value == 1]
    off = [key for key, value in states.items() if value == 0]
    writer = on[0] if len(on) == len(off) == 1 and not duplicate else None
    raw = _button_text(chips[0]) if len(chips) == 1 else ""
    values = {"model": _parse_model(raw), "effort": _parse_effort(raw)}
    return {"writer": writer, "dimensions": values, "source": "uia_snapshot", "raw": raw,
            "value": values if writer and all(values.values()) else None}


def _read_chatgpt_uia(screen, dimension: str) -> dict | None:
    for node in _tree(screen, "chatgpt").walk():
        if node.kind != "ButtonControl":
            continue
        if (node.name or "").strip() != CHATGPT_CHIP:
            continue
        text = _button_text(node)
        if dimension == "effort":
            value = _parse_effort(text)
        else:
            value = _parse_model(text)
        return {"value": value, "source": "uia", "raw": text,
                "effort": _parse_effort(text), "model": _parse_model(text)}
    return None


def _read_deepseek(screen, dimension: str) -> dict | None:
    """deepseek 的「深度思考」：模板定位 + B-R 判色（和 deepseek_choose 同一套阈值）。"""
    if dimension != "think":
        return None
    region = screen.window_rect("Google Chrome", on_secondary=True)
    got = screen.find_unique("ds_think", threshold=0.85, region=region, scales=zoom.SCALES)
    match = got.get("match")
    if match is None or got.get("ambiguous"):
        return {"value": None, "source": "none",
                "reason": "ambiguous" if got.get("ambiguous") else "template_miss",
                "template": _info(name="ds_think", score=got.get("best"),
                                  scale=got.get("scale"), hit=match is not None)}
    # 悬停会把胶囊变灰底，读出来像"开"：先把鼠标移开（只移动，不点击）
    try:
        win = screen.window_rect("Google Chrome", on_secondary=True)
        ty = max((win or [0, 0])[1] + 20, match.center[1] - 400)
        screen.move_to(match.center[0], ty)
        import time

        time.sleep(0.35)
    except Exception:                       # noqa: BLE001 - 移不动就按当前颜色读
        pass
    b, _g, r = screen.region_mean(match)
    blue = round(b - r, 1)
    value = "off" if blue > DS_OFF_MIN else ("on" if blue < DS_ON_MAX else None)
    return {"value": value, "source": "template", "raw": f"B-R={blue}",
            "template": _info(name="ds_think", score=match.score,
                              scale=getattr(match, "scale", 1.0), hit=True),
            "reason": "" if value else "color_ambiguous"}


UIA_READERS = {
    "grok": _read_grok_uia,
    "gemini": _read_gemini_uia,
    "chatgpt": _read_chatgpt_uia,
    "deepseek": _read_deepseek,
}


def read(screen, endpoint: str, dimension: str = "model", *, calm: bool = True) -> dict:
    """只读复核当前档位。**不点任何东西**（deepseek 只移动鼠标避开悬停）。

    返回的 dict 形状固定：value / source / reason / template / zoom_percent / raw。
    `value=None` 表示"没读到"，调用方必须拒发（卡面第 1 条），绝不当成"没变化"。
    """
    endpoint = str(endpoint).lower()
    if endpoint == "chatgpt" and dimension == "composite":
        return chatgpt_snapshot(screen)
    hwnd = getattr(screen, "target_hwnd", None)
    zoom_pct = zoom.percent(hwnd)
    template = None
    reason = ""

    if endpoint in TEMPLATE_CHIPS:
        got = _read_templates(screen, endpoint, calm=calm)
        template, reason = got.get("template"), got.get("reason", "")
        if got.get("value"):
            return {"value": got["value"], "source": got["source"], "reason": "",
                    "template": template, "templates": got.get("templates"),
                    "zoom_percent": zoom_pct, "raw": ""}
        if not UIA_FALLBACK_WITH_TEMPLATES:
            # 模板失配就是"读不到"，不再用 UIA 洗成确认（见常量说明）
            return {"value": None, "source": "none", "reason": reason or "template_miss",
                    "template": template or {"name": f"{endpoint}_chip"},
                    "templates": got.get("templates"),
                    "zoom_percent": zoom_pct, "raw": ""}
    elif endpoint == "deepseek":
        got = _read_deepseek(screen, dimension) or {}
        template = got.get("template")
        reason = got.get("reason", "")
        if got.get("value"):
            return {"value": got["value"], "source": got.get("source", "template"),
                    "reason": "", "template": template, "zoom_percent": zoom_pct,
                    "raw": got.get("raw", "")}

    # 模板读不到 → UIA 兜底**只读一次**（卡面明确允许这一步）
    try:
        uia = (UIA_READERS.get(endpoint) or (lambda *_a: None))(screen, dimension)
    except Exception as exc:                # noqa: BLE001 - 读失败不等于档位变了
        uia = {"value": None, "source": "uia", "reason": f"uia_error: {type(exc).__name__}"}
    if uia and uia.get("value"):
        return {"value": uia["value"], "source": "uia", "reason": "",
                "template": template, "zoom_percent": zoom_pct,
                "raw": uia.get("raw", "")}
    if uia and uia.get("template") and template is None:
        template = uia["template"]
        reason = uia.get("reason") or reason
    return {"value": None, "source": "none", "reason": reason or "unreadable",
            "template": template or _info(name=f"{endpoint}_chip"),
            "zoom_percent": zoom_pct, "raw": (uia or {}).get("raw", "")}


def reread(screen, endpoint: str, dimension: str = "model") -> dict:
    """给 `tier_guard.gate(reread=...)` 用的闭包形状：`reread(dimension) -> dict`。"""
    return read(screen, endpoint, dimension)
