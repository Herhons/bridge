"""自迭代循环的驱动器（规格 §4 的十步）。

真机动作只在这里：发问、上传、读回复。判定、材料、匿名、验真全在
`src/qi/iteration.py`（可离线测）。

用法：
    py -3.10 -B tools/iterate.py status --round round-0
    py -3.10 -B tools/iterate.py snapshot --round round-0
    py -3.10 -B tools/iterate.py review --round round-0 --site chatgpt
    py -3.10 -B tools/iterate.py verify --round round-0
    py -3.10 -B tools/iterate.py anonymize --round round-0
    py -3.10 -B tools/iterate.py summarize --round round-0
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qi import iteration as it                      # noqa: E402

CONFIG_PATH = ROOT / "docs" / "iteration.json"
DEFAULT_CONFIG = {
    # 投喂方式：upload=网页端上传文件；paste=分段粘贴。按厂商分别设定。
    "feed": {"chatgpt": "upload", "grok": "paste", "gemini": "paste", "deepseek": "paste"},
    "chunk_chars": 3600,
    "seats": ["chatgpt", "grok", "gemini", "deepseek"],
    "review_timeout": 900,
    "upload_timeout": 120,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {})
    return cfg


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def round_dir(name: str) -> Path:
    return ROOT / "docs" / "iterations" / name


# ---------------------------------------------------------------- 1. 快照
def cmd_snapshot(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    sha = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    remote = git("remote", "get-url", "origin")
    snapshot = {"repo": remote, "branch": branch, "sha": sha,
                "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "orchestrator": "tools/iterate.py@1"}
    it.write_archive(round_dir(args.round), {"snapshot.json": snapshot})
    state.set("snapshot", snapshot)
    state.mark("snapshot", {"sha": sha})
    print(json.dumps(snapshot, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 2. 发问
def _screen_for(site: str, *, new: bool = False):
    from qi.flow import ensure_site
    from qi.screen import Screen

    hwnd = ensure_site(site, tab=None, continue_session=not new, verbose=False)
    screen = Screen(verbose=False)
    screen.bind(hwnd)
    if not screen.focus():
        raise RuntimeError(f"{site}: 拿不到前台")
    screen.tab_site = site
    return screen


def _upload_settled(site: str, screen, timeout: float = 90.0) -> bool:
    """等附件传完。

    注意：**不能**等"发送键可用"——空输入框本来就不启用发送键，
    那样会一直等到超时（Round 0 第一枪就是这么空等 20 分钟的）。
    判据改成页面上不再出现「正在上传」。
    """
    from qi import ui

    end = time.time() + timeout
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                     # noqa: BLE001
            time.sleep(1.0)
            continue
        if not any("正在上传" in (node.name or "") for node in root.walk()):
            return True
        time.sleep(1.0)
    return False


#: 各端「新开会话」的入口名。取不到就退回站点根。
NEW_CHAT = {
    "chatgpt": ("新聊天", "新对话"),
    "gemini": ("发起新对话",),
    "grok": ("聊天",),
    "deepseek": ("开启新对话", "新建对话"),
}
ROOT_URL = {
    "chatgpt": "https://chatgpt.com/",
    "grok": "https://grok.com/",
    "gemini": "https://gemini.google.com/app",
    "deepseek": "https://chat.deepseek.com/",
}


def conversation_session_id(url: str):
    """从地址里取与标题无关的会话 id；没有就是新会话页。"""
    from qi.conversation import session_id_of
    return session_id_of(url)


def start_new_chat(screen, site: str, timeout: float = 20.0) -> bool:
    """真的新开一个会话，并**验证 URL 已经变**。

    上一轮的教训：`ensure_site(continue_session=False)` 并不会替 chatgpt 新开，
    材料被贴进了旧会话，取回的"回复"是别的话题。发错地方的代价比不发大得多，
    所以这里必须验证；验证不过就返回 False，调用方**不许发送**。
    """
    from qi import ui

    def url_now() -> str:
        try:
            control, url = ui.document(screen.target_hwnd, site)
            return url or ""
        except Exception:                     # noqa: BLE001
            return ""

    before = url_now()
    if before and conversation_session_id(before) is None:
        return True                      # 已经在新会话页（例如站点根）
    end = time.time() + timeout
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                     # noqa: BLE001
            time.sleep(0.5)
            continue
        target = None
        for node in root.walk():
            if node.kind not in ("ButtonControl", "HyperlinkControl") or node.offscreen:
                continue
            name = (node.name or "").strip()
            if name and any(name == want for want in NEW_CHAT.get(site, ())):
                target = node
                break
        if target is not None:
            rect = target.control.BoundingRectangle
            screen.click_xy((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            time.sleep(2.0)
            after = url_now()
            if after and conversation_session_id(after) is None:
                # 「新会话页」= 地址里没有会话 id。已经在 `/` 时点一下不会变地址，
                # 那也算成功（再发一条自然就是新会话）。
                return True
        time.sleep(1.0)
    return False


def read_answer_text(screen, site: str, prompt: str) -> str:
    """从控件树里按顺序读答案文本（复制键点不到时的兜底）。

    为什么需要：动作条常在可见区外，`Click` 打不中；而答案本身就在
    TextControl 里。这里过滤掉提问原文、侧栏标题与动作条字样。
    """
    from qi import ui

    sp = "".join(prompt.split())
    try:
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
    except Exception:                     # noqa: BLE001
        return ""
    noise = {"回答已完成", "最新一条回复", "复制", "编辑", "朗读", "分享", "重新生成回复"}
    seen, out, started = set(), [], False
    for node in root.walk():
        if node.kind != "TextControl":
            continue
        text = (node.name or "").strip()
        if len(text) < 12 or text in seen or text in noise:
            continue
        if "".join(text.split()) in sp:
            continue
        # 答案的第一行按合同就是「可访问=是/否」。只认**以它开头**的块，
        # 否则侧栏标题（“Bridge 仓库材料接收…”）会把整列标题带进来。
        if not started:
            if text.startswith("可访问") or text.startswith("NO_ACCESS"):
                started = True
            elif len(text) > 60 and any(k in text[:20] for k in ("附件", "材料")):
                started = True
            else:
                continue
        seen.add(text)
        out.append(text)
    return "\n".join(out).strip()


#: 各端的输入框占位文字。`composer_empty` 的结构判据要求把它显式传进去，
#: 否则空输入框会被当成草稿（gemini 实测：值就是「问问 Gemini」）。
SITE_PLACEHOLDERS = {"gemini": ("问问 Gemini",), "grok": ("畅所欲问",)}


def _conversation(screen, site: str):
    from qi import conversation

    return conversation.Conversation(
        screen, site,
        empty=lambda view: conversation.composer_empty(
            view, placeholders=SITE_PLACEHOLDERS.get(site, ())))


def _clear_composer(screen, site: str) -> bool:
    """点输入框 → Ctrl+A → Delete 清空。

    **必须把点击点夹进窗口矩形**：chatgpt 的编辑区 UIA 矩形会超出窗口下边界
    （实测 402..1286，而窗口只到 780），点它的中心等于点在桌面上，
    焦点立刻被桌面抢走，后面的按键全部 ForegroundLost。
    """
    from qi import ui
    from qi.upload import _raw_click

    try:
        import uiautomation as auto

        conv_ctl = auto.ControlFromHandle(screen.target_hwnd)
        win = conv_ctl.BoundingRectangle
        control, _url = ui.document(screen.target_hwnd, site)
        root = ui.tree(control)
        editors = [n for n in root.walk() if n.kind == "EditControl" and not n.offscreen]
        if not editors:
            return True
        rect = editors[0].control.BoundingRectangle
        # 取「控件矩形 ∩ 窗口矩形」的中心：
        # · gemini 的输入框很小且在窗口内 —— 中心就是框里；
        # · chatgpt 的编辑区会超出窗口下沿 —— 交集中心仍在窗口内。
        left, right = max(rect.left, win.left), min(rect.right, win.right)
        top, bottom = max(rect.top, win.top), min(rect.bottom, win.bottom)
        if right - left < 8 or bottom - top < 8:
            left, right, top, bottom = win.left, win.right, win.top, win.bottom
        x = (left + right) // 2
        y = (top + bottom) // 2
        _raw_click(x, y)
        time.sleep(0.3)
        screen.hotkey("ctrl", "a")
        time.sleep(0.1)
        screen.press("delete")
        time.sleep(0.3)
        return True
    except Exception:                     # noqa: BLE001
        return False


def harvest_reply(screen, site: str, wait: float = 420.0) -> dict:
    """等「停止」消失、最后一条消息可复制且内容稳定，再复制回来。

    为什么需要它：`conversation.ask` 只看动作条上的复制键数量，
    chatgpt 在长回答**仍在生成**时复制键是 0，于是判超时；等它真生成完，
    这里按「停止键消失 + 复制内容两次相同」收卷。
    """
    from qi import ui

    def scroll_bottom(times: int = 6) -> None:
        """把页面滚到底。动作条常在可见区之外（仓库文档记过 y≈2014 的坑），
        不滚到底根本点不到复制键。"""
        import ctypes

        u32 = ctypes.windll.user32
        u32.mouse_event(0x0800, 0, 0, -120 * times, 0)
        time.sleep(0.6)

    end = time.time() + wait
    last_text, stable_since = "", None
    while time.time() < end:
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
        except Exception:                 # noqa: BLE001
            time.sleep(2.0)
            continue
        busy = any((n.name or "").strip() in ("停止", "Stop") for n in root.walk()
                   if n.kind == "ButtonControl")
        copies = [n for n in root.walk()
                  if n.kind == "ButtonControl" and (n.name or "").strip() in ("复制", "Copy")]
        if busy or not copies:
            stable_since = None
            time.sleep(2.0)
            continue
        scroll_bottom()
        try:
            control, _url = ui.document(screen.target_hwnd, site)
            root = ui.tree(control)
            copies = [n for n in root.walk()
                      if n.kind == "ButtonControl"
                      and (n.name or "").strip() in ("复制", "Copy")]
        except Exception:                 # noqa: BLE001
            time.sleep(2.0)
            continue
        if not copies:
            time.sleep(2.0)
            continue
        rect = copies[-1].control.BoundingRectangle
        seq_before = screen.clipboard_seq()
        try:
            screen.focus()            # 长等待里焦点可能掉到桌面，先抢回来
            screen.click_xy((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        except Exception:             # noqa: BLE001 - 焦点抢不回来就用裸点击
            __import__("qi.upload", fromlist=["x"])._raw_click(
                (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        # **必须**等剪贴板序号变化：点空时读到的是用户自己的剪贴板内容
        # （实测抓到过一段地址+电话），那不是回答。
        for _ in range(20):
            if screen.clipboard_seq() != seq_before:
                break
            time.sleep(0.15)
        if screen.clipboard_seq() == seq_before:
            time.sleep(2.0)
            continue
        text = screen.clipboard() or ""
        if text and text == last_text:
            return {"ok": True, "stage": "harvested", "reply": text}
        last_text, stable_since = text, time.time()
        time.sleep(3.0)
    return {"ok": False, "stage": "harvest_timeout", "reply": last_text}


def _ask_upload(site: str, prompt: str, material: Path, cfg) -> dict:
    from qi import conversation, upload

    screen = _screen_for(site)
    _clear_composer(screen, site)
    if not start_new_chat(screen, site):
        # 上传模式同样必须新开会话：否则材料会发进上一次的旧会话。
        return {"ok": False, "stage": "new_chat_failed", "feed": "upload"}
    up = upload.upload(screen, site, str(material), timeout=cfg["upload_timeout"])
    if not up.ok:
        return {"ok": False, "stage": "upload_failed", **up.as_dict()}
    if not _upload_settled(site, screen):
        return {"ok": False, "stage": "upload_not_settled", "upload": up.as_dict()}
    # 上传模式材料在附件里，提问用一句短指令即可：长多行提问在部分端会写不进去。
    ask_text = str(cfg.get("upload_prompt") or prompt)
    conv = _conversation(screen, site)
    result = conversation.ask(conv, ask_text, timeout=cfg["review_timeout"],
                              purpose="review")
    if not result.get("ok") and result.get("stage") in ("timeout", "interrupted", "harvest_timeout"):
        harvested = harvest_reply(screen, site, wait=float(cfg.get("harvest_wait", 420)))
        if harvested["ok"]:
            result = harvested
        else:
            # 复制键点不到时，直接从控件树读（答案确实在，只是动作条不可点）。
            text = read_answer_text(screen, site, ask_text)
            if len(text) >= 300:
                result = {"ok": True, "stage": "text_extracted", "reply": text}
    result["feed"] = "upload"
    result["upload"] = up.as_dict()
    return result


def _site_object(name: str):
    """复用各端 `*_use.py` 里打磨过的 Site 配置（新会话、输入框、发送键）。"""
    import importlib.util

    path = ROOT / "src" / "scripts" / f"{name}_use.py"
    spec = importlib.util.spec_from_file_location(f"{name}_use_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SITE


def _ask_paste(site: str, prompt: str, material_text: str, cfg) -> dict:
    """分段粘贴：第一段 `mode=new`（各端自己的新开会话），其余 `mode=continue`。"""
    from qi import flow

    site_obj = _site_object(site)
    limit = int((cfg.get("chunk_chars_by_site") or {}).get(site, cfg["chunk_chars"]))
    chunks = it.pack_chunks(material_text, limit, head=prompt)
    last = {"ok": False, "stage": "no_chunks"}
    for index, chunk in enumerate(chunks, start=1):
        budget = cfg["review_timeout"] if index == len(chunks) else int(
            cfg.get("chunk_timeout", 300))
        last = flow.ask(site_obj, chunk, mode=("new" if index == 1 else "continue"),
                        timeout=budget, purpose="review", verbose=False)
        if not last.get("ok"):
            return {**last, "feed": "paste", "chunks": len(chunks), "at": index}
    return {**last, "feed": "paste", "chunks": len(chunks)}


def cmd_review(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    done = state.done(f"review:{args.site}")
    if done and not args.force:
        print(json.dumps({"skipped": "already_done", **done}, ensure_ascii=False))
        return 0

    prompt_path = round_dir(args.round) / "prompt.md"
    if not prompt_path.is_file():
        print(json.dumps({"ok": False, "error": "prompt_missing",
                          "hint": f"先写 {prompt_path}"}, ensure_ascii=False))
        return 2
    prompt = prompt_path.read_text(encoding="utf-8")
    material = round_dir(args.round) / "materials" / "material.md"
    mode = str(cfg["feed"].get(args.site, "paste"))

    started = time.time()
    if mode == "upload":
        if not material.is_file():
            print(json.dumps({"ok": False, "error": "material_missing"}, ensure_ascii=False))
            return 2
        result = _ask_upload(args.site, prompt, material, cfg)
    else:
        text = material.read_text(encoding="utf-8") if material.is_file() else ""
        result = _ask_paste(args.site, prompt, text, cfg)

    reply = result.get("reply") or ""
    replies_dir = round_dir(args.round) / "replies"
    replies_dir.mkdir(parents=True, exist_ok=True)
    (replies_dir / f"{args.site}.md").write_text(reply, encoding="utf-8")
    payload = {"ok": bool(result.get("ok")), "stage": result.get("stage"),
               "feed": result.get("feed", mode), "chars": len(reply),
               "elapsed": round(time.time() - started, 1)}
    state.mark(f"review:{args.site}", payload)
    print(json.dumps(payload, ensure_ascii=False))

    # 材料上传/粘贴的原始证据留档（不入库）
    evidence = ROOT / "evidence" / "iterations" / args.round
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{args.site}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------- 3. 验真
def _tree_paths(sha: str, rdir: Path | None = None) -> list:
    """真值表：优先读本轮的 `tree.txt`（离线可测），否则问 git。"""
    if rdir is not None:
        cached = rdir / "tree.txt"
        if cached.is_file():
            return [line.strip() for line in cached.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    try:
        out = git("ls-tree", "-r", "--name-only", sha)
    except Exception:                    # noqa: BLE001 - 离线测试守卫会拦 subprocess
        return []
    return [line for line in out.splitlines() if line.strip()]


def cmd_verify(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    snapshot = state.data.get("snapshot") or {}
    sha = str(snapshot.get("sha") or "")
    if not sha:
        cached = round_dir(args.round) / "tree.txt"
        if cached.is_file():
            sha = "unknown"              # 有真值表就够，不必惊动 git（离线干跑）
        else:
            try:
                sha = git("rev-parse", "HEAD")
            except Exception:            # noqa: BLE001 - 没有 git 也能降级
                sha = "unknown"
    tree = _tree_paths(sha, round_dir(args.round))
    spec_text = (ROOT / it.SPEC_PATH).read_text(encoding="utf-8")

    replies_dir = round_dir(args.round) / "replies"
    verdicts, report = {}, {}
    for path in sorted(replies_dir.glob("*.md")):
        site = path.stem
        text = path.read_text(encoding="utf-8")
        if "NO_ACCESS" in text[:200]:
            verdict = {"hits": 0, "total": 0, "rate": 0.0, "core": False,
                       "verdict": "no_access", "rows": []}
        else:
            verdict = it.verify_reply(text, tree_paths=tree, spec_text=spec_text,
                                      core_paths=(it.SPEC_PATH,))
        verdicts[site] = verdict
        report[site] = {"chars": len(text), **{k: verdict[k] for k in
                                              ("hits", "total", "rate", "core", "verdict")}}

    valid = [s for s, v in verdicts.items() if v["verdict"] == "true_read"]
    summary = {"sha": sha, "valid_seats": valid, "valid_count": len(valid),
               "meets_three_vendors": len(valid) >= 3, "seats": report}

    it.write_archive(round_dir(args.round), {
        "quote_check.json": {"summary": summary, "detail": verdicts},
        "quote_check.md": _quote_md(summary, report),
    })
    state.mark("verify", {"valid": valid, "count": len(valid)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["meets_three_vendors"] else 1


def _quote_md(summary, report) -> str:
    lines = ["# 引用验真（Round 0 变体）", "",
             f"快照 SHA：`{summary['sha']}`", "",
             "| 端 | 字数 | 命中 | 总数 | 命中率 | 核心命中 | 判定 |",
             "|---|---:|---:|---:|---:|---|---|"]
    for site, row in sorted(report.items()):
        lines.append(f"| {site} | {row['chars']} | {row['hits']} | {row['total']} | "
                     f"{row['rate']} | {'是' if row['core'] else '否'} | {row['verdict']} |")
    lines += ["", f"有效（真读）：{', '.join(summary['valid_seats']) or '无'}；"
                  f"门槛 3 家：{'满足' if summary['meets_three_vendors'] else '未满足'}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 4. 匿名 + 5. 汇总
def cmd_anonymize(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    qc = json.loads((round_dir(args.round) / "quote_check.json").read_text(encoding="utf-8"))
    detail = qc["detail"]
    replies = {p.stem: p.read_text(encoding="utf-8")
               for p in sorted((round_dir(args.round) / "replies").glob("*.md"))}
    pack = it.anonymize(replies, verdicts=detail)
    (round_dir(args.round) / "anonymous_pack.md").write_text(pack, encoding="utf-8")
    state.mark("anonymize", {"entries": pack.count("## F")})
    print(json.dumps({"ok": True, "entries": pack.count("## F"),
                      "chars": len(pack)}, ensure_ascii=False))
    return 0


def cmd_summarize(args, cfg) -> int:
    from qi import conversation

    pack = (round_dir(args.round) / "anonymous_pack.md").read_text(encoding="utf-8")
    prompt = it.anonymize_prompt(pack)
    screen = _screen_for("gemini")
    conv = conversation.Conversation(screen, "gemini")
    result = conversation.ask(conv, prompt, timeout=cfg["review_timeout"],
                              purpose="review")
    if not result.get("ok"):
        print(json.dumps({"ok": False, "stage": result.get("stage")}, ensure_ascii=False))
        return 1
    (round_dir(args.round) / "gemini_summary.md").write_text(
        result.get("reply") or "", encoding="utf-8")
    it.State.load(ROOT, args.round).mark("summarize", {"chars": len(result.get("reply") or "")})
    print(json.dumps({"ok": True, "chars": len(result.get("reply") or "")}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 上传自检
def cmd_upload_check(args, cfg) -> int:
    """新会话 → 上传一个小文件 → 只问一句 → 答案必须来自文件内容。

    规格 §8 要求「文件已落到该端、对方按文件内容作答」，这一步就是那个自检。
    """
    from qi import conversation, upload

    site = args.site or "chatgpt"
    sample = ROOT / "docs" / "iterations" / args.round / "materials" / "upload_check.md"
    sample.parent.mkdir(parents=True, exist_ok=True)
    token = "BRIDGE-UPLOAD-CHECK-20261111"
    sample.write_text(f"# 上传自检\n\n本文件只有一行关键内容：{token}\n", encoding="utf-8")

    screen = _screen_for(site, new=True)
    if not start_new_chat(screen, site):
        print(json.dumps({"ok": False, "stage": "new_chat_failed"}, ensure_ascii=False))
        return 1
    _clear_composer(screen, site)
    up = upload.upload(screen, site, str(sample), timeout=cfg["upload_timeout"])
    if not up.ok:
        print(json.dumps({"ok": False, "stage": "upload_failed", **up.as_dict()},
                         ensure_ascii=False))
        return 1
    _upload_settled(site, screen)
    conv = _conversation(screen, site)
    result = conversation.ask(conv, "附件里那行关键内容是什么？只回答那一行，不要解释。",
                              timeout=cfg["review_timeout"], purpose="review")
    reply = (result.get("reply") or "").strip()
    ok = token.lower() in reply.lower()
    payload = {"ok": bool(result.get("ok")) and ok, "upload": up.as_dict(),
               "stage": result.get("stage"), "answer": reply[:200],
               "token_found": ok}
    (ROOT / "docs" / "iterations" / args.round / "upload_check.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


# ---------------------------------------------------------------- status
def cmd_status(args, cfg) -> int:
    state = it.State.load(ROOT, args.round)
    print(json.dumps(state.data, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="自迭代循环驱动器")
    parser.add_argument("command", choices=("status", "snapshot", "review", "verify",
                                            "anonymize", "summarize", "upload-check"))
    parser.add_argument("--round", default="round-0")
    parser.add_argument("--site", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "review" and not args.site:
        parser.error("review 需要 --site")
    return {"status": cmd_status, "snapshot": cmd_snapshot, "review": cmd_review,
            "verify": cmd_verify, "anonymize": cmd_anonymize,
            "summarize": cmd_summarize, "upload-check": cmd_upload_check}[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
