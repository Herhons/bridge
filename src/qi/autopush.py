"""自动推：WorkBuddy 停下来时补一句话（PLAN 第 7 项子卡 CARD-07b，CARD-08 第 2 条落地）。

**这是个纯判定 + 记账模块，不发消息。** 真去发在 `src\\scripts\\bridge.py autopush`（内部走 `workbuddy.py use`），
这里只回答一个问题：现在该不该推？为什么？

判定（照 PLAN 大纲，一处不加）：

    progress.json 是 running
    且 runlog 超过 N 分钟没有新行（N 默认 20）
    且没有「生成中」的标志
    → 推一次

「生成中」的标志只有两种：`workbuddy.py use --list` 那边给的（命令行传 `--generating`），
或者 runlog 最后一行的 `stage=generating`。**超时里的 `still_generating` 不算**——
那是"上一轮等到超时时它还在生成"，不是"现在还在生成"，拿它当挡箭牌会让推永远不发。

防重复两条：

- 同一张卡 30 分钟内最多推 1 次（`cooldown_minutes`）；
- 连推 3 次 `progress.json` 的 `updated_at` 还没动过 → 停推，交给人（`action=stop`）。

**记账写在 `runs\\YYYYMMDD.jsonl`**，和 `qi.runlog` 是同一个文件、同一套 utf-8，
但行里多带 `endpoint="autopush"` + `card` + `progress_mark`。判定时这些行
**不算「有新行」**——否则自己推一次就把计时器清零，永远推不出第二次。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import console
from . import progress as progress_mod

ENDPOINT = "autopush"
PUSH = "autopush_push"
PUSH_FAILED = "autopush_push_failed"
STOP = "autopush_stop"
RUNS = Path(__file__).resolve().parents[2] / "runs"
PROMPT = "继续按卡施工"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OFFSET_TAIL = re.compile(r"([+-]\d{2})(\d{2})$")


# ---------------------------------------------------------------- 时间
def parse_time(value) -> datetime | None:
    """认 `2026-11-02T09:43:52+0800`（runlog）和 `2026-11-02T09:46:00+08:00`（progress）。

    3.10 的 `fromisoformat` 不吃没有冒号的时区，所以先把 `+0800` 补成 `+08:00`。
    认不出就返回 None —— 一行坏日志不该把整次判定拖垮。
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    match = _OFFSET_TAIL.search(text)
    if match:
        text = text[:match.start()] + match.group(1) + ":" + match.group(2)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=progress_mod.BEIJING)


def _aware(value, *, default=None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=progress_mod.BEIJING)
    return parse_time(value) or default


def now_beijing() -> datetime:
    """「现在」统一走北京时间，判定和日志里看到的时刻才是同一个。"""
    return datetime.now(progress_mod.BEIJING)


def _stamp(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


# ---------------------------------------------------------------- 读日志
def read_progress(file_path) -> dict:
    """读 progress.json（**文件路径**，不是目录）；读不到或读坏了返回 {}。

    `qi.progress.read()` 收的是目录（它自己拼 progress.json），自动推要能被测试指到
    临时文件上，所以这里直接收文件路径，不跟它抢参数含义。
    """
    try:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def modified_at(file_path) -> datetime | None:
    """文件的最后修改时间：progress.json 连 updated_at 都没有时的最后一根稻草。"""
    try:
        return datetime.fromtimestamp(Path(file_path).stat().st_mtime, tz=progress_mod.BEIJING)
    except (OSError, TypeError, ValueError):
        return None


def read_events(folder=None) -> list[dict]:
    """读 `runs\\*.jsonl`。坏行、空行直接跳过，读不了的文件也算没读到。"""
    events: list[dict] = []
    for path in sorted(Path(folder or RUNS).glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
    events.sort(key=lambda item: parse_time(item.get("time")) or _EPOCH)
    return events


def activity(events) -> list[dict]:
    """真有干活的行：autopush 自己写的那些不算「有新行」。"""
    return [item for item in events if item.get("endpoint") != ENDPOINT]


def last_activity(events) -> datetime | None:
    rows = activity(events)
    return parse_time(rows[-1].get("time")) if rows else None


def is_generating(events) -> bool:
    """runlog 最后一行的 stage/result 是 `generating` 就算「生成中」（PLAN 原话）。

    只认这一个字眼：超时里的 `still_generating` 不算——那是"上一轮等到超时它还在生成"，
    拿它挡着会让推永远不发（本机 09:49 那条 workbuddy 超时就长这样）。
    """
    rows = activity(events)
    if not rows:
        return False
    return str(rows[-1].get("stage") or rows[-1].get("result") or "") == "generating"


def _idle(now: datetime, last: datetime | None, updated: datetime | None,
          fallback_time) -> float | None:
    """多久没动静（分钟）：runlog 最后一行 > progress.updated_at > 文件修改时间。"""
    reference = last or updated or _aware(fallback_time)
    if reference is None:
        return None
    return (now - reference).total_seconds() / 60


def pushes(events, card) -> list[dict]:
    """这张卡真推出去过几次（时间正序）。推失败的不算——没发出去就不是一次推。"""
    wanted = str(card or "")
    return [item for item in events
            if item.get("endpoint") == ENDPOINT
            and item.get("result") == PUSH
            and str(item.get("card") or "") == wanted]


def stop_notified(events, card, mark) -> bool:
    """同一张卡、同一次 progress 已经叫过人没有。"""
    wanted, wanted_mark = str(card or ""), str(mark or "")
    return any(item.get("endpoint") == ENDPOINT
               and item.get("result") == STOP
               and str(item.get("card") or "") == wanted
               and str(item.get("progress_mark") or "") == wanted_mark
               for item in events)


# ---------------------------------------------------------------- 判定
@dataclass(frozen=True)
class Config:
    idle_minutes: float = 20.0
    cooldown_minutes: float = 30.0
    max_pushes: int = 3
    prompt: str = PROMPT


@dataclass(frozen=True)
class Decision:
    action: str                      # push / wait / stop / skip
    reason: str                      # 机器可读的一句，见下面 decide() 里的取值
    card: str = ""
    idle_minutes: float | None = None
    pushes: int = 0                  # 这次 progress 之后已经推过几次
    last_activity: str | None = None
    next_allowed_at: str | None = None

    @property
    def should_push(self) -> bool:
        return self.action == "push"

    @property
    def needs_human(self) -> bool:
        return self.action == "stop"

    def as_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "card": self.card,
                "idle_minutes": None if self.idle_minutes is None else round(self.idle_minutes, 1),
                "pushes_since_progress": self.pushes,
                "last_activity": self.last_activity,
                "next_allowed_at": self.next_allowed_at}


def decide(record, events, *, now=None, config: Config | None = None,
           generating: bool = False, fallback_time=None) -> Decision:
    """该不该推。`record` 就是 progress.json 的内容，`events` 是 read_events() 的结果。

    reason 的取值（测试和日志都按这些字眼对）：

        not_running         progress.json 不是 running
        no_card             是 running，但没写卡号
        generating          有「生成中」标志，别插嘴
        recent_activity     runlog 还没到 N 分钟
        cooldown            这张卡 30 分钟内已经推过一次
        no_progress         连推 3 次，progress 一动不动 → 停推，叫人
        stalled             该推了
        stalled_no_signal   连一条 runlog 都没有（只有 progress 的 updated_at 兜底）
    """
    config = config or Config()
    now = _aware(now, default=datetime.now(progress_mod.BEIJING))
    entry = record if isinstance(record, dict) else {}
    card = str(entry.get("card_id") or "").strip()
    status = str(entry.get("status") or "").strip()
    updated = parse_time(entry.get("updated_at"))

    if status != "running":
        return Decision("skip", "not_running", card,
                        _idle(now, last_activity(events), updated, fallback_time), 0,
                        _stamp(last_activity(events)))
    if not card:
        return Decision("skip", "no_card", card,
                        _idle(now, last_activity(events), updated, fallback_time), 0,
                        _stamp(last_activity(events)))

    last = last_activity(events)
    idle = _idle(now, last, updated, fallback_time)

    rows = pushes(events, card)
    if updated is not None:
        rows = [row for row in rows if (parse_time(row.get("time")) or _EPOCH) > updated]

    if generating or is_generating(events):
        return Decision("wait", "generating", card, idle, len(rows), _stamp(last))
    if idle is not None and idle < config.idle_minutes:
        return Decision("wait", "recent_activity", card, idle, len(rows), _stamp(last))

    if len(rows) >= config.max_pushes:
        return Decision("stop", "no_progress", card, idle, len(rows), _stamp(last))

    if rows:
        last_push = parse_time(rows[-1].get("time"))
        if last_push is not None:
            next_allowed = last_push + timedelta(minutes=config.cooldown_minutes)
            if now < next_allowed:
                return Decision("wait", "cooldown", card, idle, len(rows), _stamp(last),
                                _stamp(next_allowed))

    reason = "stalled" if idle is not None else "stalled_no_signal"
    return Decision("push", reason, card, idle, len(rows), _stamp(last))


# ---------------------------------------------------------------- 记账
def progress_mark(record) -> str:
    """progress.json 的 updated_at：这几次推之后有没有进展，就靠它当锚点。"""
    return str((record or {}).get("updated_at") or "")


def event_for(decision: Decision, record, *, result: str, ok: bool = True,
              error: str | None = None, prompt: str = PROMPT) -> dict:
    """一行 runlog。字段形状跟 `qi.runlog.record()` 对齐，多几个只有 autopush 用的。"""
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "endpoint": ENDPOINT,
            "ok": bool(ok), "result": result, "timeout_kind": None, "submission": None,
            "elapsed": 0.0, "error": error, "card": decision.card,
            "action": decision.action, "reason": decision.reason,
            "pushes_since_progress": decision.pushes,
            "idle_minutes": (None if decision.idle_minutes is None
                             else round(decision.idle_minutes, 1)),
            "progress_mark": progress_mark(record), "prompt": prompt}


def append(event: dict, *, root=None) -> Path:
    """追一行到 `runs\\YYYYMMDD.jsonl`（同一套 utf-8，和 qi.runlog 共用一个文件）。"""
    folder = Path(root or RUNS)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / time.strftime("%Y%m%d.jsonl")
    with console.utf8_open(target, "a") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target
