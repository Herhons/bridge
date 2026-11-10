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


# ---------------------------------------------------------------- 引用验真
PATH_RE = re.compile(r"(?:https?://github\.com/[\w.-]+/[\w.-]+/(?:blob|tree)/[\w.-]+/)?"
                     r"([A-Za-z0-9_./-]+\.(?:py|md|json|js|html|txt|yml|yaml|cfg|ini))")
LINE_RE = re.compile(r":(\d+)(?:-(\d+))?")
QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{6,120})[」”\"']")
SECTION_RE = re.compile(r"(?:第\s*)?(\d+(?:\.\d+)*)\s*(?:节|章|条|§)")


def normalize_path(raw: str) -> str:
    path = str(raw or "").strip()
    path = re.sub(r"^https?://github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/", "", path)
    path = path.replace("\\", "/")
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    path = path.lstrip("./")
    return path


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def verify_reply(reply: str, *, tree_paths, spec_text: str | None = None,
                 core_paths=CODE_CORE) -> dict:
    """核对一条回复里的引用。返回命中数、命中率、判定与逐条明细。

    `tree_paths` 是本轮快照的文件清单（真值表）；`spec_text` 给了就额外支持
    「章节号 + 逐字引句」（Round 0 变体，规格 §5.3）。
    """
    tree = {normalize_path(p) for p in tree_paths}
    squashed_spec = _squash(spec_text) if spec_text else None
    rows = []
    hits = 0

    for raw in PATH_RE.findall(reply or ""):
        path = normalize_path(raw)
        ok = path in tree
        rows.append({"kind": "path", "ref": path, "hit": ok})
        hits += 1 if ok else 0

    if squashed_spec:
        for quote in QUOTE_RE.findall(reply or ""):
            ok = _squash(quote) in squashed_spec
            rows.append({"kind": "quote", "ref": quote[:40], "hit": ok})
            hits += 1 if ok else 0

    total = len(rows)
    rate = (hits / total) if total else 0.0
    core_hit = False
    for row in rows:
        if not row["hit"]:
            continue
        ref = row["ref"]
        if row["kind"] == "quote" and squashed_spec:
            core_hit = True
        elif any(ref.startswith(prefix) for prefix in core_paths):
            core_hit = True
        elif ref == SPEC_PATH:
            core_hit = True

    if hits >= TRUE_READ["min_hits"] and rate >= TRUE_READ["min_rate"] and core_hit:
        verdict = "true_read"
    elif hits >= PARTIAL["min_hits"] and rate >= PARTIAL["min_rate"]:
        verdict = "partial"
    else:
        verdict = "unread"
    return {"hits": hits, "total": total, "rate": round(rate, 2),
            "core": core_hit, "verdict": verdict, "rows": rows}


# ---------------------------------------------------------------- 匿名化
#: 任何一家都不能出现在匿名包里（大小写不敏感）
VENDORS = ("chatgpt", "grok", "gemini", "deepseek")
MODEL_WORDS = ("gpt-4", "gpt-5", "claude", "llama", "qwen", "glm", "sonnet", "opus")


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
        body = text
        for vendor in VENDORS:
            body = re.sub(rf"(?i)\b{vendor}\b", "某端", body)
        for word in MODEL_WORDS:
            body = re.sub(rf"(?i)\b{re.escape(word)}\b", "某模型", body)
        body = re.sub(r"(?i)(我作为|作为)(某模型|某端|AI|助手)", "作为审查者", body)
        blocks.append(f"## F{index}（验真：{verdict}）\n\n{body.strip()}\n")
    return "\n".join(blocks)


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
