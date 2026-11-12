"""自迭代循环的纯函数层：状态、材料、引用验真、匿名包。

规格见 `docs/self_iteration_spec.md`。这一层**不碰 UI、不碰网络**，全部可离线测；
真机动作（发问、上传、读回复）在 `tools/iterate.py` 里，只调用这里。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 常量

SCHEMA = "round-state/2"          # 状态文件结构版本（崩溃续跑用）

SPEC_PATH = "docs/self_iteration_spec.md"
#: Round 0 的「核心命中」：意见里必须至少有一条落在规格文件上
ROUND0_CORE = SPEC_PATH
#: 代码轮的核心命中目录
CODE_CORE = ("src/qi/", "src/scripts/")

#: 真读 / partial 阈值（规格 §5.3、§6 同一套）
TRUE_READ = {"min_hits": 3, "min_rate": 0.7}
PARTIAL = {"min_hits": 1, "min_rate": 0.4}


# ---------------------------------------------------------------- 状态机
@dataclass
class State:
    """一轮的落盘状态。进程被杀后按已完成步骤续跑（规格 §13）。"""

    root: Path
    round_dir: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root, round_name: str) -> "State":
        root = Path(root)
        round_dir = root / "docs" / "iterations" / round_name
        path = round_dir / "state.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        # v1 状态没有 schema/materials：只在内存里补齐，不改历史记录。
        data.setdefault("schema", SCHEMA)
        data.setdefault("materials", {})
        return cls(root=root, round_dir=round_dir, data=data)

    def save(self) -> None:
        self.round_dir.mkdir(parents=True, exist_ok=True)
        path = self.round_dir / "state.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def done(self, step: str) -> dict | None:
        return (self.data.get("steps") or {}).get(step)

    def mark(self, step: str, payload: dict | None = None) -> dict:
        steps = self.data.setdefault("steps", {})
        steps[step] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **(payload or {})}
        self.save()
        return steps[step]

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    # ---- 崩溃续跑：每段材料一个固定身份 ----
    def chunk(self, ident: str) -> dict:
        return (self.data.get("materials") or {}).get(ident) or {}

    def mark_chunk(self, ident: str, status: str, **payload) -> dict:
        """记一段材料的发送状态：pending / sending / sent / unknown。

        `sending` 写在**发送之前**：进程若在发送途中被杀，续跑时这一段是
        "不确定"，必须先对着页面核对，不能直接重发。
        """
        materials = self.data.setdefault("materials", {})
        record = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **payload}
        materials[ident] = record
        self.save()
        return record


# ---------------------------------------------------------------- 材料
def strip_markdown(text: str) -> str:
    """只处理**行首**标记与围栏语言标签，绝不碰行内 `**`、反引号、缩进。

    2026-11-11 的教训：全局 `.replace("**", "")` 把 Python 字典解包也剥掉了，
    两家会审据此报了不存在的「必崩」。
    """
    out = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append("```")
            continue
        if in_fence:
            out.append(line)                      # 代码块原样保留
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)      # 行首标题
        line = re.sub(r"^\s{0,3}>\s?", "", line)           # 行首引用
        line = re.sub(r"^\s{0,3}[-*+]\s+", "  ", line)     # 行首列表
        out.append(line)
    return "\n".join(out)


def pack_chunks(text: str, limit: int, *, head: str = "") -> list[str]:
    """按行切块，每块不超过 `limit` 字符；不把一个代码块从中间切断。"""
    bodies: list[str] = []
    current: list[str] = []
    size = 0
    fence_open = False

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        body = "".join(current)
        if fence_open:
            body += "```\n"          # 先合上，下一块再重新打开
        bodies.append(body)
        current = ["```\n"] if fence_open else []
        size = 4 if fence_open else 0

    for line in text.splitlines():
        piece = line + "\n"
        if current and size + len(piece) > limit:
            flush()
        current.append(piece)
        size += len(piece)
        if line.strip().startswith("```"):
            fence_open = not fence_open
    if current:
        bodies.append("".join(current))

    total = len(bodies)
    out = []
    for index, body in enumerate(bodies, start=1):
        prefix = f"第 {index}/{total} 段{'（续）' if index > 1 else ''}\n\n"
        suffix = "\n\n材料已全部发完，请开始审查。" if index == total else ""
        out.append((head if index == 1 and head else "") + prefix + body + suffix)
    return out


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_identity(site: str, index: int, text: str) -> str:
    """一段材料的固定身份。**内容变了 id 就变**，续跑时靠它判断发没发过。"""
    return "chunk:" + sha256_text(f"{site}|{index}|{text}")


def prompt_identity(site: str, round_name: str, prompt: str) -> str:
    """最终提问的固定身份（与账本的 endpoint+prompt 哈希互相独立，便于交叉核对）。"""
    return "prompt:" + sha256_text(f"{site}|{round_name}|{prompt}")


def pack_chunks_with_ids(text: str, limit: int, *, head: str = "",
                         site: str = "") -> list[dict]:
    """`pack_chunks` 的带身份版本：返回 [{index, id, chars, text}]。"""
    return [{"index": index, "id": chunk_identity(site, index, body),
             "chars": len(body), "text": body}
            for index, body in enumerate(pack_chunks(text, limit, head=head), start=1)]


def resume_chunks(chunks, state) -> list[dict]:
    """续跑时还要发的段：已 `sent` 的跳过；`sending`/`unknown` 交给调用方核对。"""
    out = []
    for chunk in chunks:
        record = state.chunk(chunk["id"]) if state is not None else {}
        if record.get("status") == "sent":
            continue
        out.append(chunk)
    return out


# ---------------------------------------------------------------- 引用验真
PATH_RE = re.compile(r"(?:https?://github\.com/[\w.-]+/[\w.-]+/(?:blob|tree)/[\w.-]+/)?"
                     r"([A-Za-z0-9_./-]+\.(?:py|md|json|js|html|txt|yml|yaml|cfg|ini))")
LINE_RE = re.compile(r":(\d+)(?:-(\d+))?")
#: 同时抓「文件:行号」——判断一条是不是"代码式引用"要用到行号，
#: 但 PATH_RE 只抓文件名，行号会被丢掉（2026-11-12 单测暴露）。

QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{6,120})[」”\"']")
#: 两种写法都要认：「第 5.2 节」与「§5.2」。之前的正则只认前者，
#: 而真机回复里几乎全是后者（grok/chatgpt 的 `§8 / §5.2` 一条都抓不到）。
SECTION_RE = re.compile(
    r"(?:第\s*(\d+(?:\.\d+)*)\s*(?:节|章|条)|§\s*(\d+(?:\.\d+)*))")
PATH_LINE_RE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:py|md|json|js|html|txt|yml|yaml|cfg|ini)):(\d+)")

#: 一条意见的起点：编号列表项（`1.` / `2、`）。
NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*\d+[.、]\s+")
#: 行内分条：`第 5.2 节…原文依据：` 这种「章节号 + 原文依据」是 gemini 的写法。
SECTION_ANCHOR_RE = re.compile(
    r"(?:第\s*\d+(?:\.\d+)*\s*节|§\s*\d+(?:\.\d+)*)[^。\n]{0,24}?(?:原文依据|原文)[：:]")


def normalize_path(raw: str) -> str:
    path = str(raw or "").strip()
    path = re.sub(r"^https?://github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/", "", path)
    path = path.replace("\\", "/")
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    path = path.lstrip("./")
    return path


def _squash(text: str) -> str:
    """判「逐字」前的归一：去掉空白**与 markdown 强调标记**。

    为什么连 `` ` `` 和 `*` 一起去掉：会审方抄写时会丢掉行内代码的反引号
    （2026-11-12 Round 3 实测 6 条里 5 条只差这个；Round 2 的 chatgpt 也有 2 条），
    这属于**格式差**，不是改写。`_squash` 原来只去空白，于是逐字正确的引句判不中。
    下划线不去掉——它是文件名的一部分（`*_use.py`）。
    """
    return re.sub(r"[\s`*]+", "", text)


def _sections(text: str) -> list[str]:
    """抽出这条意见引用的章节号（去重、保序）。"""
    out: list[str] = []
    for match in SECTION_RE.finditer(text):
        ref = match.group(1) or match.group(2)
        if ref and ref not in out:
            out.append(ref)
    return out


def _cut_at(text: str, positions) -> list[str]:
    """按给定位置把文本切开；位置 0 不切（否则会切出一个空串）。"""
    cuts = sorted({p for p in positions if 0 < p < len(text)})
    if not cuts:
        return [text.strip()] if text.strip() else []
    blocks, prev = [], 0
    for cut in cuts:
        piece = text[prev:cut].strip()
        if piece:
            blocks.append(piece)
        prev = cut
    tail = text[prev:].strip()
    if tail:
        blocks.append(tail)
    return blocks


#: 以章节号开头的行（可能带 markdown 强调）。
HEAD_START_RE = re.compile(r"^[*_`\s]*(?:第\s*\d+(?:\.\d+)*\s*节|§\s*\d+(?:\.\d+)*)")
#: 章节号写在括号里的行（chatgpt 的小标题）。
HEAD_PAREN_RE = re.compile(r"[（(][^）)]{0,20}(?:§|第\s*\d+(?:\.\d+)*\s*节)")

#: 这些前缀说明该行是上一条的**说明**，不是新意见的标题。
DETAIL_PREFIXES = ("原句", "问题", "失败", "改", "验收", "为什么", "怎么", "注", "说明",
                   "证据", "建议", "风险")


def _is_detail(line: str) -> bool:
    """`**改法：**` / `问题：` / `原句：` 这类是上一条的说明行。"""
    probe = line.lstrip("*_` ").strip()
    return probe.startswith(DETAIL_PREFIXES)


def _line_heads(text: str) -> list[int]:
    """逐行写意见的回复：标题行本身就是一条意见的起点。"""
    heads, offset = [], 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not _is_detail(stripped):
            if (HEAD_START_RE.match(stripped) and len(stripped) <= 40) \
                    or HEAD_PAREN_RE.search(stripped):
                heads.append(offset + (len(line) - len(line.lstrip())))
        offset += len(line)
    return heads


def _candidates(text: str) -> list[list[int]]:
    """各种「新意见起点」的候选位置。真机回复的排版五花八门，靠单一正则切不准。"""
    out = []
    out.append([m.start() for m in re.finditer(r"(?m)^\s*\d+[）)]", text)])
    out.append([m.start() for m in re.finditer(r"(?m)^\s*\d+[.、]\s+", text)])
    # chatgpt 的紧凑写法：`1.① 二·S1：…`（点号后直接接 ①，没有空格）。
    # `(?!\d)` 避免把 `1.5 倍` 这类小数当条目。
    out.append([m.start() for m in re.finditer(r"(?m)^\s*\d+[.、](?!\d)", text)])
    # gemini：整段里 `1. [方向 A…] 2. [方向 B…]`
    out.append([m.start() for m in re.finditer(r"\d+\.\s*(?=[\[【]?\s*方向)", text)])
    # deepseek：`A 稳定：…` / `B 架构：…`
    # chatgpt 有时每条都以 `①` 起头（①…⑥ 当字段用）
    out.append([m.start() for m in re.finditer(r"(?m)^\s*①", text)])
    # gemini 有时用 `【A. 标题】` 当条目标题
    out.append([m.start() for m in re.finditer(r"【[A-F][.、]\s*", text)])
    # 整段里用「第 X 节…原文依据：」逐条展开（gemini 的一种写法）
    out.append([m.start() for m in SECTION_ANCHOR_RE.finditer(text)])
    # deepseek 实际写的是 `A 稳定：…` / `D 重复点击与…`（有的带冒号有的不带），
    # 所以按「字母 + 空格 + 短标题行」认，不强制冒号。
    out.append([m.start() for m in re.finditer(r"(?m)^[A-E][\s　]+\S[^\n]{0,40}$", text)])
    # grok：每条的引句行以 ② 开头，且上一条刚以 ⑥ 收尾
    grok = []
    lines = text.splitlines(keepends=True)
    offset, prev = 0, ""
    for line in lines:
        if line.strip().startswith("②") and prev.strip().startswith("⑥"):
            grok.append(offset)
        if line.strip():
            prev = line
        offset += len(line)
    out.append(grok)
    return [c for c in out if len(c) >= 2]


def split_opinions(reply: str) -> list[str]:
    """把回复切成「结构化意见」的候选块。

    真机上见过五种写法：`1）`、`1.`、`A 稳定：`、整段 `1. [方向…]`、grok 的
    `②`（上一条以 ⑥ 收尾时为下一条起点）。这里生成多组候选切点，取**块数最接近 6**
    （但至少 3 块）的那组——规格 §7.2 要求 5–8 条，6 是常见值。

    切分只负责分段；过滤与计数在 `verify_reply` 里做。
    """
    text = (reply or "").strip()
    if not text:
        return []
    # 取「块数最多且不超过 10」的候选：真机上同一种排版会在不同轮次漂移
    # （Round 1 用 ②，Round 2 用 ①），按优先级取第一个会被切碎。
    # 上限 10 防止把代码块里的编号也当意见。
    best = None
    for positions in _candidates(text):
        blocks = _cut_at(text, positions)
        if not (2 <= len(blocks) <= 10):
            continue
        if best is None or len(blocks) > len(best):
            best = blocks
    if best:
        return best
    heads = _line_heads(text)
    if len(heads) >= 2:
        return _cut_at(text, heads)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return blocks or [text]


def _has_citation(block: str) -> bool:
    """这条到底算不算「一条意见」。

    算：写了章节号、或写了逐字引句、或写了 `文件:行号`。
    不算：只有一句 `可访问=是`、或只提到一个不带行号的文件名
    （「（全文已读：docs/self_iteration_spec.md）」这类前言不能凑成一条意见）。
    """
    bare = QUOTE_RE.sub(" ", block)          # 引句里的文件名不算独立引用
    if SECTION_RE.search(bare) or QUOTE_RE.search(block):
        return True
    return bool(PATH_LINE_RE.search(bare))


def verify_reply(reply: str, *, tree_paths, spec_text: str | None = None,
                 core_text: str | None = None, core_paths=CODE_CORE) -> dict:
    """核对一条回复里的引用。返回命中**条数**、命中率、判定与逐条明细。

    计数口径（2026-11-12 Herhons 批准，规格 §9.1 未定义分母，这里补空白）：

    * **分母 = 结构化意见条数**，不是全文引用总数。一段里写 8 条意见，分母就是 8。
    * **一条命中要求至少一处有效引用**：引句在规格里逐字找到，或路径在快照树里存在。
    * **光写章节号不算命中**：章节号只说明这条在讲哪儿，必须有引句或有效路径。
    * **重复引句只算一次**：同一句（空白归一后）在多条里复用，后来的那条不算命中。
    * 阈值不变：真读 = 命中 ≥ 3 且 命中率 ≥ 0.7 且至少一条落在核心位置。
    """
    tree = {normalize_path(p) for p in tree_paths}
    # `spec_text` 是**本轮材料全文**（Round 1 起含架构与已知问题，引句可以来自任何一节）；
    # `core_text` 只含规格正文——"核心命中"必须落在规格上，避免拿附录里的引句充数。
    squashed_spec = _squash(spec_text) if spec_text else None
    squashed_core = _squash(core_text) if core_text else squashed_spec
    seen_quotes: dict[str, int] = {}
    rows, hits, core_hit = [], 0, False

    raw_blocks = split_opinions(reply)
    if len(raw_blocks) == 1 and not _has_citation(raw_blocks[0]):
        return {"hits": 0, "total": 0, "rate": 0.0, "core": False,
                "verdict": "unread", "rows": [], "few_opinions": True}

    for index, block in enumerate(raw_blocks, start=1):
        # 只丢页眉（`可访问=是` 之类）；**没有引句的意见要留在分母里算未命中**，
        # 否则"少写几条就命中率 100%"，口径立刻失真（2026-11-12 自测发现）。
        probe = block.strip()
        if len(probe) < 12 and not _has_citation(probe):
            continue
        if probe.startswith("可访问=") and len(probe) < 40:
            continue
        # 引句里出现的文件名/章节号属于「被引用的原文」，不能当成作者自己的定位，
        # 否则一句捏造的引句只要提到真实文件名就会被判命中。
        bare = QUOTE_RE.sub(" ", block)
        sections = _sections(bare)
        has_line = bool(PATH_LINE_RE.search(bare))

        quote_rows = []
        for quote in QUOTE_RE.findall(block):
            squashed = _squash(quote)
            if not squashed:
                continue
            if squashed in seen_quotes:
                quote_rows.append({"ref": quote[:40], "hit": False,
                                   "dup_of": seen_quotes[squashed]})
                continue
            ok = bool(squashed_spec) and squashed in squashed_spec
            if ok:
                seen_quotes[squashed] = index
            in_core = bool(squashed_core) and squashed in squashed_core
            quote_rows.append({"ref": quote[:40], "hit": ok, "core": in_core})

        path_rows = []
        for raw in PATH_RE.findall(bare):
            path = normalize_path(raw)
            path_rows.append({"ref": path, "hit": path in tree,
                              "with_line": has_line})

        hit = any(r["hit"] for r in quote_rows) or any(r["hit"] for r in path_rows)
        if hit:
            hits += 1
            if any(r["hit"] and r.get("core") for r in quote_rows):
                core_hit = True
            if any(r["hit"] and (r["ref"] == SPEC_PATH
                                 or any(r["ref"].startswith(p) for p in core_paths))
                   for r in path_rows):
                core_hit = True
        rows.append({"index": index, "sections": sections, "hit": hit,
                     "quotes": quote_rows, "paths": path_rows})

    total = len(rows)
    rate = (hits / total) if total else 0.0
    if hits >= TRUE_READ["min_hits"] and rate >= TRUE_READ["min_rate"] and core_hit:
        verdict = "true_read"
    elif hits >= PARTIAL["min_hits"] and rate >= PARTIAL["min_rate"]:
        verdict = "partial"
    else:
        verdict = "unread"
    return {"hits": hits, "total": total, "rate": round(rate, 2),
            "core": core_hit, "verdict": verdict, "rows": rows,
            # 只做提示，不参与判定：意见条数少于 §7.2 要求的 5 条时标记出来。
            "few_opinions": total < 5}


# ---------------------------------------------------------------- 匿名化
#: 任何一家都不能出现在匿名包里（大小写不敏感）
VENDORS = ("chatgpt", "grok", "gemini", "deepseek")
MODEL_WORDS = ("gpt-4", "gpt-5", "claude", "llama", "qwen", "glm", "sonnet", "opus")


#: 提取时留在正文里的界面残渣：说话人标签、耗时、附件名标题、占位符。
RESIDUE_LINE = re.compile(
    r"^\s*(?:[^\n]{0,12}说[：:]\s*$|用时[^\n]*$|可访问\s*=\s*[是否]\s*$"
    r"|附件[^\n]{0,40}(?:全文|如下)\s*$|畅所欲问\s*$|问问\s*Gemini\s*$)")


def strip_ui_residue(text: str) -> str:
    """去掉回复开头/结尾的界面残渣（不是意见内容）。

    会审方指出匿名包里出现「ChatGPT说：」「用时 4分钟 41秒」这类东西：
    它们既暴露来源，又把汇总带偏（2026-11-12 Round 3）。
    """
    # 说话人标签可能和正文同一行（`ChatGPT说：可访问=是`），先剥前缀再按行过滤。
    body = re.sub(r"(?m)^\s*[^\n]{0,12}说[：:]\s*", "", text or "")
    lines = [line for line in body.splitlines() if not RESIDUE_LINE.match(line)]
    return "\n".join(line for line in lines if line.strip()).strip()


#: 泄漏自检用的名字表：比 `VENDORS + MODEL_WORDS` 宽——替换只处理已知的四端，
#: 自检要能抓住"我们没列进替换表"的名字（Copilot/Mistral 之类）。
LEAK_WORDS = (*VENDORS, *MODEL_WORDS,
              "copilot", "mistral", "kimi", "doubao", "文心", "通义", "豆包")


def assert_anonymous(pack: str) -> None:
    """匿名包发出前自检：仍能看出是哪一端就**拒绝发送**，不靠人眼。"""
    lowered = (pack or "").lower()
    leaked = [name for name in LEAK_WORDS
              if name and re.search(rf"(?i){re.escape(name)}", lowered)]
    if leaked:
        raise AnonymityLeak(f"匿名包仍含来源标记：{sorted(set(leaked))}")


class AnonymityLeak(RuntimeError):
    """匿名包没洗干净。宁可整轮失败，也不能把来源带进汇总。"""


def anonymize(replies: dict, *, verdicts: dict | None = None) -> str:
    """把四家原文压成匿名包：中性编号 + 去来源 + 只保留真读/部分成立。

    路径里的厂商词（例如 `src/qi/gemini.py`）允许保留——那是代码事实，
    不构成「来自某端」。这里只清洗**来源标记**与自称。
    """
    verdicts = verdicts or {}
    blocks = []
    index = 0
    for name, text in replies.items():
        verdict = (verdicts.get(name) or {}).get("verdict")
        if verdict not in ("true_read", "partial"):
            continue
        index += 1
        body = strip_ui_residue(text)
        for vendor in VENDORS:
            # **不能加 \b**：`ChatGPT说：` 里 T 与「说」都是 \w，`\b` 不成立，
            # 于是来源署名原样留在匿名包里（2026-11-12 汇总者指出、实测确认）。
            body = re.sub(rf"(?i){re.escape(vendor)}", "某端", body)
        for word in MODEL_WORDS:
            body = re.sub(rf"(?i){re.escape(word)}", "某模型", body)
        body = re.sub(r"(?i)(我作为|作为)(某模型|某端|AI|助手)", "作为审查者", body)
        blocks.append(f"## F{index}（验真：{verdict}）\n\n{body.strip()}\n")
    pack = "\n".join(blocks)
    assert_anonymous(pack)
    return pack


def anonymize_prompt(pack: str) -> str:
    return (
        "下面是一组**匿名**的审查意见（编号 F1、F2…，来源已去掉）。\n"
        "请只根据这些意见做四件事：\n"
        "1. 按主题聚类；同一件事合并成一条，注明合并了哪几条编号；\n"
        "2. 标出彼此冲突的条目，并说明冲突点；\n"
        "3. 按「对本项目的好处」排序；\n"
        "4. 你若想补充新缺陷，必须单独列在「汇总者新增」小节，不得混进原意见。\n"
        "不要猜来源，不要复述原文。控制在 1500 字内。\n\n"
        "===== 意见开始 =====\n\n" + pack
    )


# ---------------------------------------------------------------- 归档
ARCHIVE_FILES = ("snapshot.json", "prompt.md", "quote_check.json", "quote_check.md",
                 "anonymous_pack.md", "gemini_summary.md", "verification.md",
                 "plan.md", "decisions.md", "changes.md", "STOP.md", "CONTINUE.md")


def write_archive(round_dir, files: dict) -> list:
    """把产物写进 `docs/iterations/round-N/`，返回实际写下的文件名。"""
    round_dir = Path(round_dir)
    round_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in files.items():
        path = round_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.write_text(str(content), encoding="utf-8")
        written.append(name)
    return written
