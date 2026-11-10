"""发送前的最后一刻门禁（CARD-06b 第 3/8 条）。

## 为什么门要挪到"写完之后、点击之前"

CARD-06a 把档位闸放在 `ask()` 开头：校验完再写文本、再点发送。中间隔着
"切新对话 / 打字 / 读回输入框"好几秒——页面可能在这几秒里跳转、被切走、
或者弹出人机验证。等点下去的时候，闸门校验的是几秒前的页面。

所以 06b 的顺序是：

    写文本 → 读回输入框（归一比对）→ 人机验证检测 → 档位只读复核（消费回执）
        → 记下页面指纹 → （读一次确认没变）→ 点发送

## 页面指纹比什么

    hwnd / pid / 进程启动时间   窗口有没有被换掉（客户端重启也算）
    url                          导航 / 新对话（网页端换路由）
    document runtime id          同 URL 的 SPA 重组（新对话常常只换树不换地址）
    foreground                   窗口有没有被切到后台

只有**读得到且不同**才判"变了"；读不到不冒充"没变"，记 `checked=false`。
真正需要 fail-closed 的地方是档位（`tier_unreadable`）——那条在 tier_guard 里。
"""

from __future__ import annotations

from . import chip, human, tier_guard, ui


def page_stamp(screen, site: str) -> dict:
    """当前页面指纹。`site` 是本端名（用于挑 DocumentControl）。"""
    hwnd = getattr(screen, "target_hwnd", None)
    fact = tier_guard.fingerprint(hwnd) if hwnd else {}
    stamp = {"hwnd": fact.get("hwnd"), "pid": fact.get("pid"),
             "process_start_time": fact.get("process_start_time"),
             "url": None, "doc_rid": None, "foreground": None, "title": ""}
    if not hwnd:
        return stamp
    try:
        stamp["foreground"] = int(ui.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:                       # noqa: BLE001 - 诊断字段，不拦流程
        stamp["foreground"] = None
    try:
        control, url = ui.document(hwnd, site)
        stamp["url"] = url or None
        try:
            stamp["doc_rid"] = tuple(control.GetRuntimeId()) or None
        except Exception:                   # noqa: BLE001
            stamp["doc_rid"] = None
    except Exception as exc:                # noqa: BLE001 - 读不到就留空，confirm 再判
        stamp["error"] = f"{type(exc).__name__}: {exc}"
    try:
        stamp["title"] = ui.title_of(hwnd)
    except Exception:                       # noqa: BLE001
        pass
    return stamp


def diff_stamp(before: dict, after: dict) -> list[str]:
    """两个页面指纹之间的差异字段列表。读不到（None）的不冒充"没变"。

    纯函数，不读屏幕——CARD-09f 第 3 条的 concurrent_ui 检测和
    `page_confirm` 共用这一份，避免两处各写各的比对。
    """
    changed: list[str] = []
    for key in ("hwnd", "pid", "process_start_time"):
        old, cur = before.get(key), after.get(key)
        if old is not None and cur is not None and old != cur:
            changed.append(key)
    if (before.get("foreground") is not None and after.get("foreground") is not None
            and before["foreground"] and not after["foreground"]):
        changed.append("foreground")
    for key in ("url", "doc_rid"):
        old, cur = before.get(key), after.get(key)
        if old is not None and cur is not None and old != cur:
            changed.append(key)
    return changed


def page_confirm(screen, site: str, stamp: dict | None) -> dict:
    """复核之后、点击之前再比一次。返回 {"ok","changed","now","checked"}。

    `ok=False` 时调用方必须**不点发送**（卡面第 3 条）。
    """
    if not stamp:
        return {"ok": True, "changed": [], "checked": False, "now": {}}
    now = page_stamp(screen, site)
    changed = diff_stamp(stamp, now)
    checked = any(stamp.get(key) is not None and now.get(key) is not None
                  for key in ("hwnd", "pid", "process_start_time"))
    if (stamp.get("foreground") is not None and now.get("foreground") is not None):
        checked = True
    if (stamp.get("url") is not None and now.get("url") is not None) or \
       (stamp.get("doc_rid") is not None and now.get("doc_rid") is not None):
        checked = True
    # 复核之后页面读不出来了：按"变了"处理（卡面要求拒发，不赌它只是抽风）
    if now.get("error") and not changed:
        changed.append("unreadable")
    return {"ok": not changed, "changed": changed, "checked": checked,
            "before": {k: stamp.get(k) for k in ("url", "doc_rid", "foreground")},
            "now": {k: now.get(k) for k in ("url", "doc_rid", "foreground", "error")}}


def pre_send(screen, site: str, purpose: str | None, *, root=None,
             require_readback: bool | None = None, reread=None) -> dict:
    """写完之后、点击之前的门：人机验证 → 档位复核 → 页面指纹。

    抛 `human.HumanCheck`（stage=human_check）或
    `tier_guard.TierUnconfirmed`（stage=tier_unconfirmed）。
    `purpose=None` 时跳过档位（只读/不发送的路径不会走到这里）。

    `reread(dimension) -> dict|None` 可以换掉默认的芯片读数。B17 起 chatgpt
    的芯片不再暴露档位文本，页面上也没有任何只读的档位来源；那一端改用
    「写入之前先做一次真读数快照」，把快照通过这个参数交给闸门复核——
    默认路径（`chip.read`）一个字节没变。
    """
    hwnd = getattr(screen, "target_hwnd", None)
    title = ui.title_of(hwnd) if hwnd else ""
    if root is None:
        try:
            control, _url = ui.document(hwnd, site)
            root = ui.tree(control)
        except Exception:                   # noqa: BLE001 - 读不到树就只按标题判
            root = None
    human.raise_if_hit(root, title=title)

    tier = None
    if purpose is not None:
        if require_readback is None:
            require_readback = chip.supports(site)
        reader = reread if callable(reread) else (
            lambda dimension: chip.read(screen, site, dimension, calm=False))
        tier = tier_guard.gate(
            site, purpose, hwnd=hwnd, require_readback=require_readback,
            # 发送前这次读**不按 Esc、不移鼠标**：那两个动作会被 guard 当成
            # 用户输入，把发送判成 user_took_over（见 chip.read 的 calm 参数）。
            reread=reader if chip.supports(site) else None)
    return {"ok": True, "stamp": page_stamp(screen, site), "tier": tier}
