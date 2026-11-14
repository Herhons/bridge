"""工作流状态机（任务卡 05，第 5 项后端部分）。

纯标准库：不开窗口、不发消息、不调用任何端的 CLI。跳转表不写死在代码里，
一律从 ``docs\\workflow\\workflow.json`` 读（路径可用参数覆盖，测试用）。

对外四件事：
    move(card, state, missing=...)   按跳转表改一张卡的状态
    advance(card)                    「监工推了一次」：连推 3 次没变化就叫监工
    resolve_hold(card)               补齐证据后回 verifying
    summary(progress)                每张卡及当前状态（面板/CLI 用）

数据只写在 progress.json 顶层的 ``workflow`` 键下，原有顶层字段（card_id、status、
note、updated_at、running、verdicts、queue）一个不动：
    {"workflow": {"cards": {"05": {"state": "...", "no_progress": 0,
                                   "parent": null, "missing": [],
                                   "history": [{"from": "...", "to": "...", "at": "..."}]}}}}

几条钉死的规则（改之前先看任务卡）：
- 只有 done 能进 verifying；pass 是终态；block 不再跳转。
- 同状态 move 也算非法；非法一律抛 IllegalTransition，消息固定为
  ``非法跳转: <卡号> <旧状态> -> <新状态>``，并且不改任何文件。
- 进 block 会自动生成修复卡（05 -> 05b -> 05c，跳过已存在的），新卡状态 sent、
  parent 记原卡号；原卡停在 block。
- 进 hold 必须带非空的 missing 清单；清单没清空前，hold -> verifying 一律拒绝。
- 只要 move 成功就把 no_progress 清 0（含自动转 stuck、resolve_hold 那一次）。

写文件：先备份到同级 ``_backup_<yyyymmdd>\\<文件名>.bak-<HHMMSS>``，再「临时文件 +
os.replace」原子替换；UTF-8 无 BOM 写出，读入兼容 BOM。同一秒内连着写两次时，
后一次备份加 ``-2``、``-3`` 后缀，不覆盖前一次的备份。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFINITION_PATH = Path("docs") / "workflow" / "workflow.json"
PROGRESS_NAME = "progress.json"
BEIJING = timezone(timedelta(hours=8))

BACKUP_DIR_FORMAT = "_backup_%Y%m%d"
BACKUP_STAMP_FORMAT = "%H%M%S"
BOM = b"\xef\xbb\xbf"

DRAFT, SENT, RUNNING, DONE = "draft", "sent", "running", "done"
FAILED, STUCK, VERIFYING = "failed", "stuck", "verifying"
PASS, BLOCK, HOLD = "pass", "block", "hold"

SUFFIX_LETTERS = "abcdefghijklmnopqrstuvwxyz"      # 修复卡后缀：b、c、d……（跳过 a）

#: CARD-v010-P0P1：`docs/workflow/` 在 `.gitignore` 里，干净检出必然没有
#: workflow.json。以前这里直接抛 DefinitionError，`server._transitions` 再包成
#: 500 —— 于是"新克隆一份就跑不起来"。内置一份与仓库现行定义等价的默认表，
#: **只在文件不存在时**兜底；文件存在但坏掉仍然显式报错（不掩盖真实损坏）。
DEFAULT_DEFINITION: dict = {
    "version": 1,
    "states": ["draft", "sent", "running", "done", "failed",
               "stuck", "verifying", "pass", "block", "hold"],
    "terminal": ["pass"],
    "transitions": {
        "draft": ["sent"],
        "sent": ["running", "stuck"],
        "running": ["done", "failed", "stuck"],
        "done": ["verifying"],
        "failed": ["sent", "draft"],
        "stuck": ["sent", "draft"],
        "verifying": ["pass", "block", "hold"],
        "block": [],
        "hold": ["verifying"],
        "pass": [],
    },
    "max_no_progress": 3,
    "seats": {"plan": "红莉栖", "build": "workbuddy", "verify": "codex"},
}


class WorkflowError(Exception):
    """工作流相关的错误基类：命令行把它归到退出码 4。"""


class DefinitionError(WorkflowError):
    """workflow.json 读不到、坏掉或缺字段。"""


class ProgressError(WorkflowError):
    """progress 文件读不到、坏掉或结构不对。"""


class CardNotFound(WorkflowError):
    """卡号不在 workflow.cards 里，也不是顶层 card_id。"""


class UnknownState(WorkflowError):
    """状态名不在 workflow.json 的 states 里。"""


class MoveRejected(WorkflowError):
    """这一次 move 被规则拒绝：命令行归到退出码 2。"""


class IllegalTransition(MoveRejected):
    """跳转表里没有这条边（同状态 move 也算）。"""


def _now() -> datetime:
    return datetime.now(BEIJING)


def beijing_now() -> str:
    """北京时间 ISO 8601，带 +08:00，不随本机时区变。"""
    return _now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 定义

def definition_path(path: str | Path | None = None) -> Path:
    """workflow.json 的路径；默认按本文件往上推到项目根再拼 docs\\workflow。"""
    return Path(path) if path is not None else ROOT / DEFINITION_PATH


def load_definition(path: str | Path | None = None, *, fallback: bool = True) -> dict:
    """读并校验工作流定义；格式不对就抛 DefinitionError。

    `fallback=True`（默认）时，**只有文件不存在**才回退到 `DEFAULT_DEFINITION`；
    读不了、不是 JSON、结构不对仍然抛错 —— 兜底不能变成"把损坏当默认"。
    """
    target = definition_path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        if fallback:
            return json.loads(json.dumps(DEFAULT_DEFINITION))   # 深拷贝，防调用方改坏常量
        raise DefinitionError(f"找不到工作流定义: {target}") from exc
    except OSError as exc:
        raise DefinitionError(f"读不了工作流定义: {target} ({exc})") from exc
    if raw.startswith(BOM):
        raw = raw[len(BOM):]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DefinitionError(f"工作流定义不是合法 JSON: {target}") from exc

    if not isinstance(data, dict):
        raise DefinitionError("工作流定义顶层不是对象")
    states = data.get("states")
    if not isinstance(states, list) or not all(isinstance(x, str) and x for x in states):
        raise DefinitionError("工作流定义的 states 必须是非空字符串列表")
    transitions = data.get("transitions")
    if not isinstance(transitions, dict):
        raise DefinitionError("工作流定义的 transitions 必须是对象")
    for state, targets in transitions.items():
        if state not in states:
            raise DefinitionError(f"transitions 里的状态不在 states 中: {state}")
        if not isinstance(targets, list) or not all(isinstance(x, str) for x in targets):
            raise DefinitionError(f"transitions.{state} 必须是状态名列表")
        for target in targets:
            if target not in states:
                raise DefinitionError(f"transitions.{state} 指向未知状态: {target}")
    terminal = data.get("terminal")
    if not isinstance(terminal, list) or not all(x in states for x in terminal):
        raise DefinitionError("工作流定义的 terminal 必须是 states 的子集")
    limit = data.get("max_no_progress")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise DefinitionError("工作流定义的 max_no_progress 必须是正整数")
    if not isinstance(data.get("seats"), dict):
        raise DefinitionError("工作流定义的 seats 必须是对象")
    return data


def _check_state(state: object, definition: dict) -> str:
    name = str(state if state is not None else "").strip()
    if name not in definition["states"]:
        raise UnknownState(f"状态名不存在: {name}")
    return name


# --------------------------------------------------------------------------- 进度文件

def progress_path(path: str | Path | None = None) -> Path:
    """progress 文件的路径；不传就用项目根的 progress.json。"""
    return Path(path) if path is not None else ROOT / PROGRESS_NAME


def load_progress(path: str | Path | None = None) -> dict:
    """读进度文件；不存在/坏 JSON/顶层不是对象都抛 ProgressError。兼容 UTF-8 BOM。"""
    target = progress_path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise ProgressError(f"找不到进度文件: {target}") from exc
    except OSError as exc:
        raise ProgressError(f"读不了进度文件: {target} ({exc})") from exc
    if raw.startswith(BOM):
        raw = raw[len(BOM):]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProgressError(f"进度文件不是合法 JSON: {target}") from exc
    if not isinstance(data, dict):
        raise ProgressError(f"进度文件顶层不是对象: {target}")
    return data


def backup(target: Path, when: datetime | None = None) -> Path:
    """把 target 复制到同级 _backup_<yyyymmdd>\\<文件名>.bak-<HHMMSS>，返回备份路径。"""
    when = when or _now()
    folder = Path(target).parent / when.strftime(BACKUP_DIR_FORMAT)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{Path(target).name}.bak-{when.strftime(BACKUP_STAMP_FORMAT)}"
    dest = folder / name
    index = 2
    while dest.exists():                      # 同一秒连写两次：不覆盖上一次的备份
        dest = folder / f"{name}-{index}"
        index += 1
    shutil.copy2(target, dest)
    return dest


# ---------------------------------------------------------------- 状态版本（CARD-v010-P3）
#
# `revision` 是**写次数**（单调 +1），`state_sha256` 是**状态指纹**（同内容恒定）。
# 两者都只放在 `data["workflow"]` 里：`progress.json` 的顶层键集合被
# `tests/test_card05.py:308` 逐字钉死，顶层多一个键就红。

REVISION_KEY = "revision"
DIGEST_KEY = "state_sha256"


def workflow_digest(node: dict) -> str:
    """`data["workflow"]` 节点的指纹；**排除 revision/state_sha256 自身**。

    这样"同一份状态"在不同 revision 上得到同一个 digest —— 指纹回答"内容变了没"，
    revision 回答"写过几次"，两个语义不混。
    """
    body = {k: v for k, v in dict(node or {}).items()
            if k not in (REVISION_KEY, DIGEST_KEY)}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def stamp_workflow(data: dict, *, bump: bool = True) -> dict:
    """唯一 bump 点：写盘前给 `data["workflow"]` 盖版本。**不碰顶层任何键**。"""
    if not isinstance(data, dict):
        raise ProgressError("要盖版本的内容不是对象")
    node = data.get("workflow")
    if not isinstance(node, dict):
        node = {}
        data["workflow"] = node
    current = node.get(REVISION_KEY)
    try:
        revision = int(current) if current is not None else 0
    except (TypeError, ValueError):
        revision = 0
    if bump:
        revision += 1
    node[REVISION_KEY] = revision
    node[DIGEST_KEY] = workflow_digest(node)
    return {REVISION_KEY: revision, DIGEST_KEY: node[DIGEST_KEY]}


def save_progress(data: dict, path: str | Path | None = None,
                  when: datetime | None = None) -> Path:
    """先备份原文件，再临时文件 + os.replace 原子替换；UTF-8 无 BOM 写出。"""
    if not isinstance(data, dict):
        raise ProgressError("要写的内容不是对象")
    target = progress_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp_workflow(data)          # CLI 写路径：revision 在这里 +1
    if target.exists():
        backup(target, when)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def _cards(data: dict, create: bool = False) -> dict:
    """取 workflow.cards；结构不对就抛 ProgressError（不静默丢掉已有数据）。"""
    node = data.get("workflow")
    if node is None:
        if not create:
            return {}
        node = data["workflow"] = {}
    if not isinstance(node, dict):
        raise ProgressError("进度文件的 workflow 键不是对象")
    cards = node.get("cards")
    if cards is None:
        if not create:
            return {}
        cards = node["cards"] = {}
    if not isinstance(cards, dict):
        raise ProgressError("进度文件的 workflow.cards 不是对象")
    return cards


def _record(raw: object) -> dict:
    """把一张卡的记录补成固定形状；不认识的键原样留着，不丢。"""
    item = dict(raw) if isinstance(raw, dict) else {}
    item["state"] = str(item.get("state") or "")
    try:
        item["no_progress"] = int(item.get("no_progress") or 0)
    except (TypeError, ValueError):
        item["no_progress"] = 0
    item.setdefault("parent", None)
    missing = item.get("missing")
    item["missing"] = [str(x) for x in missing] if isinstance(missing, list) else []
    history = item.get("history")
    item["history"] = list(history) if isinstance(history, list) else []
    return item


def _card_id(card: object) -> str:
    value = str(card if card is not None else "").strip()
    if not value:
        raise CardNotFound("卡号不存在: (空)")
    return value


def _ensure_card(data: dict, card: str) -> dict:
    """拿一张卡的记录；不在 workflow.cards 时，用顶层 card_id/status 当初始状态。"""
    cards = _cards(data, create=True)
    if card in cards:
        record = _record(cards[card])
        cards[card] = record
        return record

    top = str(data.get("card_id") or "").strip()
    status = str(data.get("status") or "").strip()
    if card != top or not status:
        raise CardNotFound(f"卡号不存在: {card}")
    record = {"state": status, "no_progress": 0, "parent": None, "missing": [], "history": []}
    cards[card] = record
    return record


def _free_repair_id(cards: dict, card: str) -> str:
    """原卡号 + 下一个空闲小写字母后缀：05 -> 05b -> 05c（跳过已存在的）。"""
    base = card.rstrip(SUFFIX_LETTERS) or card
    for letter in SUFFIX_LETTERS[1:]:          # b..z，a 不用
        candidate = f"{base}{letter}"
        if candidate not in cards:
            return candidate
    raise WorkflowError(f"修复卡号已用尽: {base}b..z")


def sync_top(data: dict, card: str, state: str, now: str) -> None:
    """move 的卡号就是顶层 card_id 时，把顶层 status / updated_at 一起改掉。

    B26：公开出来给 HTTP 路径复用。此前只有 CLI 的 `move` 调它，
    `POST /workflow` 只改卡片，顶层 `status` 停在旧值 —— autopush 会继续
    给已经结束的卡推消息，`workbuddy_autopush` 的「进度变了就停」也失效。
    """
    if str(data.get("card_id") or "").strip() != card:
        return
    data["status"] = state
    data["updated_at"] = now


# 老名字保留：`_transition` 与既有调用方不用改。
_sync_top = sync_top


def _transition(data: dict, cards: dict, card: str, new: str,
                *, now: str, missing: list[str] | None = None) -> dict:
    """真正落一次状态变化：改 state、清计数、写 missing、追加 history、同步顶层。"""
    record = _record(cards[card])
    old = record["state"]
    record["state"] = new
    record["no_progress"] = 0
    record["missing"] = list(missing or [])
    record["history"].append({"from": old, "to": new, "at": now})
    cards[card] = record
    _sync_top(data, card, new, now)
    return record


# --------------------------------------------------------------------------- 对外动作

def _transaction(action):
    from functools import wraps
    @wraps(action)
    def wrapped(*args, **kwargs):
        from . import progress as progress_io
        target = progress_path(kwargs.get("progress_path"))
        with progress_io.locked(file_path=target):
            return action(*args, **kwargs)
    return wrapped


def next_repair_id(cards: dict, card: str) -> str:
    """修复卡号：原卡 + 下一个空闲小写字母后缀（05 -> 05b，跳过已存在）。"""
    return _free_repair_id(cards, card)


def check_move(record: dict, target: str, missing) -> list:
    """跳转的两条证据不变量。CLI（`move`）与 HTTP（`Engine.workflow_move`）共用。

    返回归一化后的 missing 列表；不合法抛 `MoveRejected`。
    —— 抽成纯函数的原因：HTTP 版曾把 missing 直接写成 `[]`，于是
    `hold ⇒ missing 非空` 与 `hold→verifying ⇒ 原 missing 已清` 两条
    在 HTTP 路径上被静默绕开，审计链被抹掉还看不出来。
    """
    items = [str(x) for x in (missing or []) if str(x).strip()]
    old = str(record.get("state") or "")
    if target == HOLD and not items:
        raise MoveRejected(f"缺少证据清单: {old} -> hold 必须带 missing")
    if old == HOLD and target == VERIFYING and record.get("missing"):
        raise MoveRejected(f"证据未补齐: 还缺 {len(record['missing'])} 项，不能从 hold 出去")
    return items


@_transaction
def move(card: object, new_state: object, *, missing: list[str] | None = None,
         progress_path: str | Path | None = None,
         workflow_path: str | Path | None = None) -> dict:
    """按跳转表改一张卡的状态。

    返回 ``{"card", "from", "to", "record", "repair_card"}``；repair_card 只有进 block
    生成修复卡时才有值，其余是 None。被拒时抛 MoveRejected 子类，且不写任何文件。
    """
    definition = load_definition(workflow_path)
    new = _check_state(new_state, definition)
    card = _card_id(card)
    data = load_progress(progress_path)
    cards = _cards(data, create=True)
    record = _ensure_card(data, card)
    old = record["state"]

    allowed = definition["transitions"].get(old, [])
    if new == old or new not in allowed:
        raise IllegalTransition(f"非法跳转: {card} {old} -> {new}")

    items = check_move(record, new, missing)

    now = beijing_now()
    record = _transition(data, cards, card, new, now=now,
                         missing=items if new == HOLD else [])
    repair = None
    if new == BLOCK:
        repair = _free_repair_id(cards, card)
        cards[repair] = {"state": SENT, "no_progress": 0, "parent": card,
                         "missing": [], "history": []}
    save_progress(data, progress_path)
    return {"card": card, "from": old, "to": new, "record": record, "repair_card": repair}


@_transaction
def advance(card: object, *, progress_path: str | Path | None = None,
            workflow_path: str | Path | None = None) -> dict:
    """「监工推了一次」：no_progress +1，够了就叫监工。

    计数到 max_no_progress（3）时，当前状态能去 stuck 就直接转 stuck（这次 move 成功，
    计数清 0）；去不了就只报 need_supervisor，状态不动。返回 ``{"need_supervisor": bool}``。
    """
    definition = load_definition(workflow_path)
    card = _card_id(card)
    data = load_progress(progress_path)
    cards = _cards(data, create=True)
    record = _ensure_card(data, card)

    limit = int(definition["max_no_progress"])
    record["no_progress"] = int(record["no_progress"]) + 1
    cards[card] = record
    need = record["no_progress"] >= limit

    now = beijing_now()
    if need and STUCK in definition["transitions"].get(record["state"], []):
        _transition(data, cards, card, STUCK, now=now, missing=[])
    save_progress(data, progress_path)
    return {"need_supervisor": need}


@_transaction
def resolve_hold(card: object, *, progress_path: str | Path | None = None,
                 workflow_path: str | Path | None = None) -> dict:
    """清空 missing 清单后自动回 verifying；卡不在 hold 就抛 MoveRejected。"""
    definition = load_definition(workflow_path)
    card = _card_id(card)
    data = load_progress(progress_path)
    cards = _cards(data, create=True)
    record = _ensure_card(data, card)
    if record["state"] != HOLD:
        raise MoveRejected(f"卡不在 hold: {card} 当前 {record['state']}")

    record["missing"] = []
    cards[card] = record
    now = beijing_now()
    old = record["state"]
    record = _transition(data, cards, card, VERIFYING, now=now, missing=[])
    save_progress(data, progress_path)
    return {"card": card, "from": old, "to": VERIFYING, "record": record}


def summary(progress_path: str | Path | None = None) -> list[dict]:
    """每张卡及状态，按卡号排序；workflow.cards 为空时回落到顶层 card_id/status。"""
    data = load_progress(progress_path)
    cards = _cards(data)
    rows = []
    for cid in sorted(cards):
        record = _record(cards[cid])
        rows.append({"card": cid, "state": record["state"], "parent": record["parent"],
                     "missing": record["missing"], "no_progress": record["no_progress"],
                     "source": "workflow"})
    top = str(data.get("card_id") or "").strip()
    if top and top not in cards:
        status = str(data.get("status") or "").strip()
        rows.append({"card": top, "state": status, "parent": None, "missing": [],
                     "no_progress": None, "source": "status"})
    return rows


# ---------------------------------------------------------------- 持久协作契约（CARD-v010-P1B-P2）
#
# 会审要求：任何等待状态都必须能回答四个问题——等谁、等到什么时候、什么条件唤醒、
# 超时去哪儿。没有这四样，协调进程一退出，"在等"就变成了"没发生"。
#
# 不新增状态：`on_timeout` 映射到既有状态（blocked→block、needs_human→hold、
# waiting_retry→stuck）。理由是 `states` 列表被 `tests/test_card05.py` 逐字钉死，
# 加状态等于改既有契约。

ON_TIMEOUT = {"blocked": BLOCK, "needs_human": HOLD, "waiting_retry": STUCK}

#: 等待四要素的字段名（顺序即落盘顺序，便于人读）。
WAIT_FIELDS = ("waiting_on", "deadline_at", "wake_condition", "on_timeout")

#: 算"真正的业务推进"的状态。`draft` 不算、`block/hold` 算显式停车。
ADVANCED_STATES = (SENT, RUNNING, DONE, VERIFYING, PASS)
PARKED_STATES = (HOLD, BLOCK)


def _parse_iso(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=BEIJING)
    return stamp


def waiting_of(record: dict) -> dict | None:
    """读出一张卡的等待四要素；缺任何一项都不算"有效等待"。"""
    raw = (record or {}).get("waiting")
    if not isinstance(raw, dict):
        return None
    values = {key: raw.get(key) for key in WAIT_FIELDS}
    if not values["waiting_on"] or not values["wake_condition"]:
        return None
    if values["on_timeout"] not in ON_TIMEOUT:
        return None
    if _parse_iso(values["deadline_at"]) is None:
        return None
    return values


@_transaction
def set_waiting(card, *, waiting_on: str, deadline_at: str, wake_condition: str,
                on_timeout: str, progress_path=None, workflow_path=None) -> dict:
    """把一张卡挂到等待态，并**强制**写全四要素。

    缺任何一项直接 `MoveRejected` —— 半个等待比没有等待更危险：协调进程退出后
    没人知道该等谁、等到几点。
    """
    for name, value in (("waiting_on", waiting_on), ("deadline_at", deadline_at),
                        ("wake_condition", wake_condition), ("on_timeout", on_timeout)):
        if not str(value or "").strip():
            raise MoveRejected(f"等待契约缺字段: {name}")
    if str(on_timeout) not in ON_TIMEOUT:
        raise MoveRejected(f"on_timeout 只能是 {'/'.join(ON_TIMEOUT)}")
    when = _parse_iso(deadline_at)
    if when is None:
        raise MoveRejected(f"deadline_at 不是合法时间: {deadline_at!r}")

    data = load_progress(progress_path)
    _ensure_card(data, card)
    cards = _cards(data, create=True)
    record = _record(cards[card])
    record["waiting"] = {"waiting_on": str(waiting_on), "deadline_at": when.isoformat(),
                         "wake_condition": str(wake_condition),
                         "on_timeout": str(on_timeout)}
    record["waiting"]["since"] = beijing_now()
    cards[card] = record
    save_progress(data, progress_path)
    return {"card": card, "state": record["state"], "waiting": record["waiting"]}


def check_timers_locked(data: dict, *, table: dict, now=None) -> dict:
    """无锁内核：**调用方必须已经持有 progress 锁**。

    原地改 `data`，**不写盘**（写盘与盖版本由调用方负责）。`/workflow/tick` 需要
    "读 → 计时器 → guard → 一次写"在同一把锁里完成，而 `progress.locked` 在
    Windows 上不可重入（msvcrt 同字节区间二次加锁会 5s 超时），所以必须把循环体
    从带锁的 `check_timers` 里拆出来。
    """
    stamp_now = _parse_iso(now) if now else datetime.now(BEIJING)
    if stamp_now is None:
        stamp_now = datetime.now(BEIJING)
    cards = _cards(data, create=False)
    moved, skipped = [], []
    for card, raw in list(cards.items()):
        record = _record(raw)
        waiting = waiting_of(record)
        if waiting is None:
            continue
        deadline = _parse_iso(waiting["deadline_at"])
        if deadline is None or deadline > stamp_now:
            continue
        target = ON_TIMEOUT[waiting["on_timeout"]]
        current = record["state"]
        fallback = None
        if target not in table.get(current, []):
            # 跳转表是既有契约（`running` 只有 done/failed/stuck 三条出边），
            # 硬迁会破表。退一步落到 `stuck`（它本来就是"需要监工"的既有状态），
            # 并把原定目标写进卡片，交给人决定下一步。
            if STUCK in table.get(current, []):
                fallback, target = target, STUCK
            else:
                skipped.append({"card": card, "from": current, "to": target,
                                "reason": f"not_allowed:{current}->{target}"})
                continue
        missing = list(record.get("missing") or [])
        if target == HOLD and not missing:
            missing = [f"timeout:{waiting['on_timeout']}"]
        _transition(data, cards, card, target, now=beijing_now(), missing=missing)
        cards[card]["waiting"] = None
        if fallback:
            cards[card]["timeout_target"] = fallback
        moved.append({"card": card, "from": current, "to": target,
                      "reason": (f"fallback_stuck:{fallback}" if fallback
                                 else f"timeout:{waiting['on_timeout']}")})
    return {"now": stamp_now.isoformat(), "checked": len(cards),
            "moved": moved, "skipped": skipped}


@_transaction
def check_timers(now=None, *, progress_path=None, workflow_path=None,
                 transitions: dict | None = None) -> dict:
    """把所有已过期的等待迁移到 `on_timeout` 指定的状态。

    一次只推进一次（不级联），写盘一次。目标边不存在时**不硬迁**，记 `skipped`：
    例如 `failed` 没有 `hold` 边，硬迁会让状态机自己破自己的表。
    """
    definition = ({"transitions": transitions}
                  if transitions is not None else load_definition(workflow_path))
    data = load_progress(progress_path)
    out = check_timers_locked(data, table=definition["transitions"], now=now)
    if out["moved"]:
        save_progress(data, progress_path)
    return {"ok": True, **out}


def _card_of(data: dict, card: str | None) -> tuple[str | None, dict | None]:
    cards = _cards(data, create=False)
    if card is not None:
        raw = cards.get(str(card))
        return str(card), (_record(raw) if isinstance(raw, dict) else None)
    if len(cards) == 1:
        name = next(iter(cards))
        return name, _record(cards[name])
    return None, None


def guard_coordinator_exit(before: dict, after: dict, *, card: str | None = None,
                           now=None) -> dict:
    """协调者退出前的守门：非终态不许"什么也没发生"就结束回合。

    三种情形算正常退出：
      · advanced —— 推进到了新的业务阶段；
      · waiting  —— 挂了一个**未过期**的有效等待对象；
      · parked   —— 显式停在 hold/block（交给人的状态）。
    否则返回 ok=False，调用方应把该卡迁到 `block` 并写 `blocker_reason`。
    """
    name, was = _card_of(before or {}, card)
    _, now_card = _card_of(after or {}, card)
    if now_card is None:
        return {"ok": False, "reason": "coordinator_exited_without_dispatch_or_wait",
                "detail": "card_missing", "card": name, "to": BLOCK}
    old_state = str((was or {}).get("state") or "")
    new_state = str(now_card.get("state") or "")
    advanced = new_state != old_state and new_state in ADVANCED_STATES
    if new_state == PASS:
        return {"ok": True, "reason": "terminal_pass", "card": name,
                "advanced": True, "waiting": None, "parked": False}
    waiting = waiting_of(now_card)
    if waiting is not None:
        deadline = _parse_iso(waiting["deadline_at"])
        stamp_now = _parse_iso(now) if now else datetime.now(BEIJING)
        alive = deadline is not None and deadline > (stamp_now or datetime.now(BEIJING))
    else:
        alive = False
    parked = new_state in PARKED_STATES
    ok = bool(advanced or alive or parked)
    reason = ("advanced" if advanced else
              "waiting" if alive else
              "parked" if parked else
              "coordinator_exited_without_dispatch_or_wait")
    return {"ok": ok, "reason": reason, "card": name, "from": old_state,
            "to": new_state, "advanced": advanced,
            "waiting": waiting if alive else None, "parked": parked,
            **({} if ok else {"blocker_reason": reason, "suggest": BLOCK})}


def require_verified(record: dict, *, level: str | None = None) -> None:
    """进 `verifying` 前必须有 L3。

    **默认不启用**：`require_verified` 只在调用方显式打开时生效（`verify_gate`）。
    一开就会让 `tests/test_card05.py` 的 11 条合法边用例全红——它们的历史卡片里
    没有任何证据字段，这是"判据齐备但默认关"的已知取舍。
    """
    value = level if level is not None else ((record or {}).get("evidence") or {}).get("level")
    if str(value or "") != "L3":
        raise MoveRejected(f"l3_required: 进 verifying 必须有 L3 证据（当前 {value!r}）")


def apply_guard_locked(data: dict, card: str, *, reason: str, table: dict,
                       now=None) -> dict:
    """把 guard 的建议落到一张卡上：**能迁才迁**到 `block`。

    调用方必须已持 progress 锁（与 `check_timers_locked` 同一约束）。
    跳转表里 `block` 只有 `verifying` 一条入边，所以 `sent/running/stuck/draft`
    上必然 `applied=False` + `not_allowed:<from>->block` —— 这不是保守，是唯一与
    现实一致的答案；`apply_guard=true` 只在 `verifying` 卡上真的有效。
    """
    cards = _cards(data, create=False)
    raw = cards.get(card)
    if not isinstance(raw, dict):
        return {"applied": False, "from": None, "to": None, "detail": "card_missing"}
    record = _record(raw)
    current = record["state"]
    if current == BLOCK:
        return {"applied": False, "from": current, "to": current,
                "detail": "already_blocked"}
    if BLOCK not in table.get(current, []):
        return {"applied": False, "from": current, "to": current,
                "detail": f"not_allowed:{current}->{BLOCK}"}
    stamp = now or beijing_now()
    _transition(data, cards, card, BLOCK, now=stamp, missing=[])
    cards[card]["blocker_reason"] = str(reason or "")
    return {"applied": True, "from": current, "to": BLOCK, "detail": None}
