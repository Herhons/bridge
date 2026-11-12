"""Durable send-intent guard. Unknown/accepted attempts are never blindly resent.

Keys conservatively cover endpoint+exact prompt across conversations/restarts.
This may require human review for an intentional repetition; a new tier receipt
does not erase an ambiguous previous submission. No prompt body is stored.

2026-11-07（R5 / CE1-CE2）：记录里还要存**发送时的文档/标签身份**（scope）。
只靠"仓库里有记录 + 页面上出现同一条提问"不够——那是文本证据，不是归属证据：
另一个文档/标签里碰巧有同样的提问和旧回答时，会被当成本轮结果交回去。
恢复时用 `reconcile()` 严格比对；缺字段、读不到、对不上，一律 fail-closed。

2026-11-07（T2-1 / 施工单 B1）：状态机升 v2，六态 + 三级证据。

**双字段兼容设计（这是本模块唯一需要先理解的事）**

磁盘上同时写两个状态名：

* `state`    —— **v1 兼容名**（`not_sent` / `unknown` / `accepted` / `answered`）。
  所有尚未改造的调用方（`conversation.py` / `gemini.py` / `grok_use.py`）继续读
  这个字段，行为一个字节都不变。这是"改状态机不破坏 3 个调用点"的落点。
* `v2_state` —— **新六态**（`draft` / `submitting` / `accepted` / `unknown` /
  `failed_retryable` / `failed_terminal`）。新代码只读这个。

两者由 `_v1_name()` 从同一处推导，不可能写歪。老记录没有 `v2_state` 时，
`normalize()` 从 `state` 反推，并打上 `legacy=True`。

**为什么老记录不许推断证据级别**

老记录没有 `basis`，也没有 `scope`。从 `answered` 反推"它一定是 L3"看起来合理，
但那是在**补造证据**——我们并不知道当时到底拿到的是完整回答还是被污染的文本。
所以归一后一律 `level=None` + `legacy=True`，交给 `reconcile()` 去 fail-closed。
"""
import hashlib
import json
import os
import time
from pathlib import Path

from . import tier_guard

#: 本模块抛出的唯一异常类型。显式导出，避免调用方去猜 `tier_guard.TierUnconfirmed`。
TierUnconfirmed = tier_guard.TierUnconfirmed

# ---------------------------------------------------------------- 状态（v2）
STATE_DRAFT = "draft"
STATE_SUBMITTING = "submitting"
STATE_ACCEPTED = "accepted"
STATE_UNKNOWN = "unknown"
STATE_FAILED_RETRYABLE = "failed_retryable"
STATE_FAILED_TERMINAL = "failed_terminal"

STATES_V2 = (STATE_DRAFT, STATE_SUBMITTING, STATE_ACCEPTED,
             STATE_UNKNOWN, STATE_FAILED_RETRYABLE, STATE_FAILED_TERMINAL)

#: 拦下发车（`pending()` 返回记录）的集合；其余状态放行。
#: 与 v1 行为的等价性见 `pending()` 的 docstring。
BLOCKING_STATES = frozenset({STATE_SUBMITTING, STATE_ACCEPTED,
                             STATE_UNKNOWN, STATE_FAILED_TERMINAL})

#: v2 -> v1 的名字映射。老代码只看得到这些名字。
_V1_NAMES = {
    STATE_DRAFT: "not_sent",
    STATE_SUBMITTING: "unknown",          # 老版本把"提交中"直接写成 unknown
    STATE_ACCEPTED: "accepted",
    STATE_UNKNOWN: "unknown",
    STATE_FAILED_RETRYABLE: "not_sent",   # 没发出去 -> 允许重试
    STATE_FAILED_TERMINAL: "unknown",     # 终态也必须拦住，不能再发
}

# ---------------------------------------------------------------- 证据级别
LEVEL_L1 = "L1"      # 本地后端接收任务（POST /jobs 202）—— 只代表入队
LEVEL_L2 = "L2"      # 目标端确认提交（页面上出现我方提问气泡）
LEVEL_L3 = "L3"      # 取得完整回答（容器结构可证 + 稳定）
LEVELS = (LEVEL_L1, LEVEL_L2, LEVEL_L3)
_LEVEL_ORDER = {LEVEL_L1: 1, LEVEL_L2: 2, LEVEL_L3: 3}


def path_for(endpoint, prompt, *, root=None):
    key = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
    return Path(root or tier_guard.ROOT) / "runs/submissions" / (str(endpoint) + "-" + key + ".json")


def read(endpoint, prompt, *, root=None):
    """读原始记录（**不归一**）。调用方看到的仍是他熟悉的字段。"""
    path = path_for(endpoint, prompt, root=root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "unknown", "v2_state": STATE_UNKNOWN,
                "error": "submission journal unreadable"}
    if not isinstance(value, dict):
        return {"state": "unknown", "v2_state": STATE_UNKNOWN}
    if not value.get("state") and not value.get("v2_state"):
        return {"state": "unknown", "v2_state": STATE_UNKNOWN}
    return value


# ---------------------------------------------------------------- 归一化
def normalize(record):
    """把任意版本的记录归一成 v2 只读视图（纯函数，无 IO，不写盘）。

    返回 dict，至少含 `v2_state` / `level` / `legacy` / `submission_count`。
    传入 None 或读不出来时返回 None。

    **老记录不推断证据级别**：`level` 一律 None，`legacy` 一律 True。
    """
    if not isinstance(record, dict):
        return None
    v2 = record.get("v2_state")
    if v2 in STATES_V2:
        out = dict(record)
        out.setdefault("level", None)
        out.setdefault("legacy", False)
        out.setdefault("submission_count", 1 if v2 not in (STATE_DRAFT, STATE_FAILED_RETRYABLE) else 0)
        return out

    v1 = str(record.get("state") or "").strip().lower()
    if not v1:
        return None
    # v1 -> v2。这一张表就是"老账本怎么被读懂"的全部依据。
    if v1 == "not_sent":
        new_state, subtype = STATE_FAILED_RETRYABLE, "not_sent"
        count = 0
    elif v1 == "accepted":
        new_state, subtype = STATE_ACCEPTED, None
        count = 1
    elif v1 == "answered":
        new_state, subtype = STATE_ACCEPTED, None
        count = 1
    else:                                  # unknown / 任何未知名
        new_state, subtype = STATE_UNKNOWN, None
        count = 1                          # 状态不明按"可能已发"占位，绝不重发
    out = dict(record)
    out["v2_state"] = new_state
    out["level"] = None                    # 关键：不推断 L2/L3
    out["legacy"] = True
    out["state_v1"] = v1
    out["submission_count"] = count
    if subtype:
        out["subtype"] = subtype
    return out


def read_state(endpoint, prompt, *, root=None):
    """归一化只读视图。**不做任何写**。"""
    return normalize(read(endpoint, prompt, root=root))


def pending(endpoint, prompt, *, root=None):
    """拦发车则返回原始记录，放行返回 None。

    与 v1 行为的等价性（老记录归一后）：
      * 老 `not_sent`          -> failed_retryable -> **放行**（v1 也是放行）
      * 老 `unknown`           -> unknown          -> 拦（v1 也拦）
      * 老 `accepted`/`answered` -> accepted       -> 拦（v1 也拦）
      * 无记录                 -> None             -> 放行
    """
    value = read(endpoint, prompt, root=root)
    if not value:
        return None
    view = normalize(value)
    if view and view.get("v2_state") in BLOCKING_STATES:
        return value
    return None


# ---------------------------------------------------------------- 文档身份（R5）
def clean_scope(scope):
    """把身份清洗成可比较的字典；不可用返回 None（**不做任何默认填充**）。

    只保留能证明"同一个窗口里的同一个文档"的字段：
      site / hwnd / pid / process_start_time / doc_rid / url
    一个都没有 → None。缺字段不补、不猜，交给 `reconcile` 判 fail-closed。
    """
    if not isinstance(scope, dict):
        return None
    out = {"v": 1}
    site = scope.get("site")
    if isinstance(site, str) and site.strip():
        out["site"] = site.strip().lower()
    url = scope.get("url")
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
    for key in ("hwnd", "pid"):
        value = scope.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    start = scope.get("process_start_time")
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        out["process_start_time"] = float(start)
    rid = scope.get("doc_rid")
    if isinstance(rid, (list, tuple)) and rid:
        try:
            out["doc_rid"] = [int(x) for x in rid]
        except (TypeError, ValueError):
            pass
    # R7：与标题无关的**会话 id**（地址里的会话路径）。这是唯一能证明"同一个会话"的
    # 字段；标题（session）只留作诊断，不参与匹配。
    session_id = scope.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        out["session_id"] = session_id.strip()
    # R6：应用自己画出来的会话名。**只作诊断**（标题可变、可重复，R7 不再拿它比）。
    session = scope.get("session")
    if isinstance(session, dict) and session.get("proven"):
        title = session.get("title")
        if isinstance(title, str) and title.strip():
            out["session"] = {"title": title.strip(), "proven": True}
    # 只有 site/url 不算身份（URL 可能被多个会话共用）：必须还带着窗口或文档句柄。
    if "hwnd" not in out and "doc_rid" not in out:
        return None
    return out if len(out) > 1 else None


def scope_from(*, site, url=None, doc_rid=None, hwnd=None, session=None, session_id=None):
    """按"端 + 页面 + 文档句柄 + 窗口指纹 + 会话 id"组一份身份；读不到的字段留空。

    指纹读不出来（假句柄、驱动异常）**不抛**：只少写几个字段，由 `reconcile`
    判"缺字段"——恢复照样 fail-closed，不会因为这里抛异常把恢复流程带崩。
    `session` 是应用画出来的会话名（**仅诊断**）；`session_id` 是地址里与标题无关的
    会话 id（R7：恢复唯一认的身份）。
    """
    try:
        fact = tier_guard.fingerprint(hwnd) if hwnd else {}
    except Exception:                       # noqa: BLE001 - 读不到就留空
        fact = {}
    if session_id is None and url:
        try:
            from .conversation import session_id_of
            session_id = session_id_of(url)
        except Exception:                   # noqa: BLE001 - 取不到就当没有
            session_id = None
    return clean_scope({"site": site, "url": url, "doc_rid": doc_rid,
                        "hwnd": fact.get("hwnd"), "pid": fact.get("pid"),
                        "process_start_time": fact.get("process_start_time"),
                        "session": session, "session_id": session_id})


def scope_from_view(view, *, site, hwnd=None):
    """从一个已经读到的 View 里取身份（url + 文档 id + 窗口指纹 + 会话名/会话 id）。"""
    try:
        rid = tuple(view.root.control.GetRuntimeId())
    except Exception:                       # noqa: BLE001 - 读不到就不写这一项
        rid = ()
    try:
        # 局部导入：submissions 不依赖 conversation，避免把整个 UI 栈拖进记录模块。
        from .conversation import session_id_of, session_stamp
        session = session_stamp(view)
        session_id = session_id_of(getattr(view, "url", None))
    except Exception:                       # noqa: BLE001 - 读不出来就当没有
        session, session_id = None, None
    return scope_from(site=site, url=getattr(view, "url", None),
                      doc_rid=rid, hwnd=hwnd, session=session, session_id=session_id)


def _scope_digest(scope):
    """身份摘要：只用于证据里"这两次是不是同一个身份"的等价判断，不可逆。"""
    clean = clean_scope(scope)
    if not clean:
        return None
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def reconcile(recorded, current):
    """恢复时比对"发送时的身份"和"现在看到的身份"。返回 (ok, reason)。

    **任何读不到、缺字段、对不上都算不成立**（fail-closed）：
      · recorded 缺失/空     -> scope_missing（老记录没有身份，不能证明归属）
      · current 读不出来      -> scope_unreadable
      · hwnd/pid/启动时间    -> 必须两边都有且相等（少一个都拒）
      · doc_rid              -> 必须两边都有且相等（同 URL 的 SPA 重组靠它）
      · url                   -> 两边都读得到时必须相等；一边读不到不冒充"相同"，
                                但只要 doc_rid 对得上就仍算同一文档
    """
    old = clean_scope(recorded)
    now = clean_scope(current)
    if old is None:
        return False, "scope_missing"
    if now is None:
        return False, "scope_unreadable"
    if old.get("site") and now.get("site") and old["site"] != now["site"]:
        return False, "site_mismatch"
    if old.get("hwnd") is None or now.get("hwnd") is None:
        return False, "hwnd_missing"
    if old["hwnd"] != now["hwnd"]:
        return False, "hwnd_mismatch"
    # pid / 进程启动时间能认出"同一个 hwnd 号被重用/客户端重启"。有些环境读不到
    # （驱动、权限、假句柄）——**两边都读不到**时不算不成立（hwnd+doc_rid 仍在绑）；
    # 只要有一边读得到，就必须两边都有且相等。
    for key in ("pid", "process_start_time"):
        before, after = old.get(key), now.get(key)
        if before is not None or after is not None:
            if before is None or after is None:
                return False, f"{key}_missing"
            if before != after:
                return False, f"{key}_mismatch"
    if old.get("doc_rid") is None or now.get("doc_rid") is None:
        return False, "doc_rid_missing"
    if list(old["doc_rid"]) != list(now["doc_rid"]):
        return False, "doc_rid_mismatch"
    if old.get("url") and now.get("url") and old["url"] != now["url"]:
        return False, "url_mismatch"
    # R7：文档和 URL 相同也可能换了 SPA 会话，而**标题不能当证明**（可变、可重复）。
    # 唯一认的是与标题无关的会话 id：记录里没有 -> 不再"兼容成功"；现在读不到 -> 拒；
    # 对不上 -> 拒。没有 id 的端（oc:// / app://）等于永远恢复不了，这是有意的代价。
    old_sid = old.get("session_id")
    now_sid = now.get("session_id")
    if not old_sid:
        return False, "session_id_missing"
    if not now_sid:
        return False, "session_id_unreadable"
    if old_sid != now_sid:
        return False, "session_id_mismatch"
    return True, "matched"


def recovery_scope(value):
    """恢复时该拿来比的身份：优先发送后的（含新会话 id），退回发送前的。

    `scope_after` 是"接受那一页"的文档身份；会话 id 如果它没有（发送前还没带上 id），
    就用发送前那份补上——否则一个已经证明过的会话会被悄悄降级成"没有会话证据"。
    """
    if not isinstance(value, dict):
        return None
    chosen = value.get("scope_after") or value.get("scope")
    if not isinstance(chosen, dict):
        return None
    before = value.get("scope")
    if isinstance(before, dict):
        for key in ("session_id", "session"):
            if not chosen.get(key) and before.get(key):
                chosen = {**chosen, key: before[key]}
    return chosen


# ---------------------------------------------------------------- 写盘
def _validate(record):
    """写入前的不变量校验。违反即抛，**不静默降级**。

    1. `accepted` 必须带 L2/L3 证据
       —— 这条直接封死"后端 202 就当发送成功"的历史事故。
    2. v2 记录的 `submission_count` 必须是 1 或 0，且与状态自洽。
    3. `attempts` 里 `kind=="submit"` 至多一条（重试只能记 `kind="retry"`）。
    """
    v2 = record.get("v2_state")
    level = record.get("level")
    if v2 == STATE_ACCEPTED and level not in (LEVEL_L2, LEVEL_L3):
        raise tier_guard.TierUnconfirmed(
            "accepted_without_evidence",
            f"accepted 必须带 L2/L3 证据，当前 level={level!r}（202 只算 L1，不等于已提交）")
    if record.get("v") == 2:
        count = record.get("submission_count")
        expected = 0 if v2 in (STATE_DRAFT, STATE_FAILED_RETRYABLE) else 1
        if count != expected:
            raise tier_guard.TierUnconfirmed(
                "submission_count_invariant",
                f"submission_count={count!r} 与状态 {v2!r} 不符（应为 {expected}）")
        submits = [a for a in (record.get("attempts") or [])
                   if isinstance(a, dict) and a.get("kind") == "submit"]
        if len(submits) > 1:
            raise tier_guard.TierUnconfirmed(
                "duplicate_submit_attempt",
                f"attempts 里有 {len(submits)} 条 submit，同一事务至多一条")


def _write(path, value):
    _validate(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _raise_level(current, wanted):
    """取更高的一级。级别只升不降——重写不能把证据变弱。"""
    if wanted not in LEVELS:
        return current
    if current not in LEVELS:
        return wanted
    return wanted if _LEVEL_ORDER[wanted] >= _LEVEL_ORDER[current] else current


def _v1_name(v2_state):
    return _V1_NAMES.get(v2_state, "unknown")


def _skeleton(state, *, scope=None, job_id=None, request_id=None, now=None):
    """造一条 v2 记录骨架。`state`/`v2_state` 由同一处推导，写不歪。"""
    stamp = now if now is not None else time.time()
    record = {
        "v": 2,
        "state": _v1_name(state),
        "v2_state": state,
        "level": None,
        "submission_count": 0 if state in (STATE_DRAFT, STATE_FAILED_RETRYABLE) else 1,
        "attempts": [],
        "evidence": {},
        "pid": os.getpid(),
        "at": stamp,
        "updated_at": stamp,
    }
    clean = clean_scope(scope)
    if clean:
        record["scope"] = clean
    if job_id:
        record["evidence"]["l1"] = {"job_id": str(job_id), "request_id": request_id,
                                    "at": stamp, "source": "caller"}
        record["level"] = LEVEL_L1
    return record


def _upgrade(existing, state, *, now=None):
    """把一条（可能还是 v1 的）记录升级成 v2 骨架，保留原有的 scope/timestamps。"""
    base = normalize(existing) or {}
    stamp = now if now is not None else time.time()
    record = {
        "v": 2,
        "state": _v1_name(state),
        "v2_state": state,
        "level": base.get("level"),
        "submission_count": base.get("submission_count"),
        "attempts": list(base.get("attempts") or []),
        "evidence": dict(base.get("evidence") or {}),
        "pid": os.getpid(),
        "at": base.get("at", stamp),
        "updated_at": stamp,
    }
    for key in ("scope", "scope_after"):
        if base.get(key):
            record[key] = base[key]
    if base.get("legacy"):
        record["legacy"] = True
    return record


# ---------------------------------------------------------------- 对外动作
def reserve(endpoint, prompt, *, root=None, scope=None, job_id=None,
            request_id=None, now=None):
    """提交动作**之前**落账（draft -> submitting）。落账失败就不许提交。

    `scope` 是发送时看到的文档/标签身份（见 `scope_from_view`）。老调用方不传
    也能工作，但那样的记录在恢复时会被 `reconcile` 判 `scope_missing` → 拒答。

    `job_id`/`request_id` 给定时同时写入 L1 证据（本地后端已接收任务）。
    """
    path = path_for(endpoint, prompt, root=root)
    stamp = now if now is not None else time.time()
    try:
        with tier_guard.file_lock(path.with_suffix(".lock")):
            if pending(endpoint, prompt, root=root):
                raise tier_guard.TierUnconfirmed("submission_pending", "上次提交状态不明/已接受，禁止重发")
            existing = read(endpoint, prompt, root=root)
            if existing:
                # 上一次是 failed_retryable（否则 pending 已经拦住）：**保留历史**，
                # 只追加一条 retry，绝不把原来的 submit 证据覆盖掉。
                record = _upgrade(existing, STATE_SUBMITTING, now=stamp)
                record["attempts"] = list(record.get("attempts") or [])
                record["attempts"].append({"kind": "retry", "at": stamp})
                # 注意：`submission_count` 不是重试次数，是"这条记录有没有过提交"的
                # 布尔式不变量（0/1，见 `_validate`）；重试历史在 `attempts` 里。
                record["submission_count"] = 1
            else:
                record = _skeleton(STATE_SUBMITTING, scope=scope, job_id=job_id,
                                   request_id=request_id, now=stamp)
                record["attempts"] = [{"kind": "submit", "at": stamp}]
            if job_id:
                merged = dict(record.get("evidence") or {})
                merged["l1"] = {"job_id": str(job_id), "request_id": request_id,
                                "at": stamp, "source": "caller"}
                record["evidence"] = merged
                record["level"] = _raise_level(record.get("level"), LEVEL_L1)
            if scope and "scope" not in record:
                clean = clean_scope(scope)
                if clean:
                    record["scope"] = clean
            _write(path, record)
    except OSError as exc:
        raise tier_guard.TierUnconfirmed("submission_state_unwritable", "无法持久化提交状态，拒绝发送") from exc


def begin(endpoint, prompt, *, root=None, scope=None, job_id=None,
          request_id=None, now=None):
    """draft -> submitting 的显式入口。

    当前草稿**不入账**（只有真正要发的那一刻才落盘），因此语义上等价于
    `reserve()`。单独命名只是为了让调用方表达"我现在要提交了"这个意图。
    """
    return reserve(endpoint, prompt, root=root, scope=scope,
                   job_id=job_id, request_id=request_id, now=now)


def mark_level(endpoint, prompt, level, evidence=None, *, root=None, now=None):
    """把证据级别抬到 `level`（只升不降）。记录不存在则**什么也不做**（不建记录）。

    用于调用方在"已经在页面上看到我方提问气泡"（L2）或"拿到完整回答"（L3）之后
    补记证据。不建记录是刻意的：没有提交过就不该因为读到一个回答而凭空记账。
    """
    path = path_for(endpoint, prompt, root=root)
    if not path.is_file():
        return None
    stamp = now if now is not None else time.time()
    with tier_guard.file_lock(path.with_suffix(".lock")):
        existing = read(endpoint, prompt, root=root)
        if not existing:
            return None
        view = normalize(existing) or {}
        record = _upgrade(existing, view.get("v2_state") or STATE_UNKNOWN, now=stamp)
        wanted = _raise_level(record.get("level"), level)
        if wanted != record.get("level"):
            record["level"] = wanted
            # 级别抬到 L2 意味着提交已被目标端确认
            if wanted in (LEVEL_L2, LEVEL_L3) and record["v2_state"] != STATE_ACCEPTED:
                record["v2_state"] = STATE_ACCEPTED
                record["state"] = _v1_name(STATE_ACCEPTED)
                record["submission_count"] = 1
        if isinstance(evidence, dict) and evidence:
            key = {"L1": "l1", "L2": "l2", "L3": "l3"}[level]
            merged = dict(record.get("evidence") or {})
            merged[key] = {**evidence, "at": stamp}
            record["evidence"] = merged
        record["updated_at"] = stamp
        _write(path, record)
        return record


def fail(endpoint, prompt, *, state, subtype=None, evidence=None,
         root=None, now=None):
    """写明确失败终态/可重试态。

    `state` 只能是 `failed_retryable` 或 `failed_terminal`。前者表示"有可信证据
    说明还没提交成功"，放行重试；后者表示"重试没有意义"（登录失效、额度耗尽、
    目标端不可用、结构不合法被拒），永久拦住同一 (endpoint, prompt)。
    """
    if state not in (STATE_FAILED_RETRYABLE, STATE_FAILED_TERMINAL):
        raise tier_guard.TierUnconfirmed(
            "bad_fail_state", f"fail() 只接受 failed_retryable/failed_terminal，收到 {state!r}")
    path = path_for(endpoint, prompt, root=root)
    stamp = now if now is not None else time.time()
    with tier_guard.file_lock(path.with_suffix(".lock")):
        existing = read(endpoint, prompt, root=root)
        if existing:
            view = normalize(existing) or {}
            record = _upgrade(existing, state, now=stamp)
            record["level"] = view.get("level")
        else:
            record = _skeleton(state, now=stamp)
        # 与状态自洽：可重试 = 没占掉提交名额；终态 = 已占（绝不重发）
        record["submission_count"] = 0 if state == STATE_FAILED_RETRYABLE else 1
        if subtype:
            record["subtype"] = str(subtype)
        if isinstance(evidence, dict) and evidence:
            merged = dict(record.get("evidence") or {})
            merged["failure"] = {**evidence, "at": stamp}
            record["evidence"] = merged
        record["updated_at"] = stamp
        _write(path, record)
        return record


def finish(endpoint, prompt, result, *, root=None, scope=None, now=None):
    """收尾写状态。`scope` 给了就记成 `scope_after`（**只写第一次**）。

    为什么要 scope_after：Gemini/Grok 新会话发送后地址才带上会话 id，发送前那份
    scope 已经和"回答所在的那个会话"对不上了。第一次判定 accepted 的那次读取，
    页面上正显示着我们的提问（或它的回答），此刻的身份才是恢复该比对的。
    只写第一次，避免后面某次误读（比如用户切走）把好身份覆盖掉。

    v2 映射（`stage` 名字一个都不改，只在记录内部换状态）：
      * `answered` / `already_answered` -> accepted + L3
      * `submission == "accepted"`       -> accepted + **L2**（不是 L3）
      * `not_sent & sent is False`       -> failed_retryable(not_sent)
      * 其余                             -> unknown
    """
    path = path_for(endpoint, prompt, root=root)
    stamp = now if now is not None else time.time()
    with tier_guard.file_lock(path.with_suffix(".lock")):
        value = read(endpoint, prompt, root=root)
        if value is None:
            return
        view = normalize(value) or {}
        stage = result.get("stage")
        if stage in ("answered", "already_answered"):
            new_state, wanted_level = STATE_ACCEPTED, LEVEL_L3
        elif result.get("submission") == "accepted":
            new_state, wanted_level = STATE_ACCEPTED, LEVEL_L2
        elif result.get("submission") == "not_sent" and result.get("sent") is False:
            new_state, wanted_level = STATE_FAILED_RETRYABLE, None
        else:
            new_state, wanted_level = STATE_UNKNOWN, None

        # B24：不降级。已经 accepted / 状态不明的记录，不许被一次"没发出去"的收尾
        # 改写成 failed_retryable —— `pending()` 不拦可重试态，那等于把上一轮的成功
        # 解锁成可重发。判据是**旧状态**，所以本轮刚 reserve 的 submitting
        # 照旧允许改写成 failed_retryable（失败要能重试）。
        if (new_state == STATE_FAILED_RETRYABLE
                and view.get("v2_state") in (STATE_ACCEPTED, STATE_UNKNOWN)):
            return

        record = _upgrade(value, new_state, now=stamp)
        # v1 兼容：老代码（gemini.py / conversation.py）会把 `state` 原样塞进
        # `result["submission"]` 并据此判断，所以 "answered" 必须继续写成
        # "answered"，不能因为 v2 里它和 accepted 同属一态就被抹平。
        if stage in ("answered", "already_answered"):
            record["state"] = "answered"
        else:
            record["state"] = _v1_name(new_state)
        if wanted_level:
            record["level"] = _raise_level(view.get("level"), wanted_level)
            key = "l3" if wanted_level == LEVEL_L3 else "l2"
            merged = dict(record.get("evidence") or {})
            ev = {"basis": stage or "unknown"}
            if wanted_level == LEVEL_L3:
                ev.update({"chars": result.get("chars"),
                           "stable_reads": result.get("stable_reads")})
            merged.setdefault(key, {**{k: v for k, v in ev.items() if v is not None},
                                    "at": stamp})
            record["evidence"] = merged
        else:
            record["level"] = view.get("level")

        # 终态/可重试态会改变 submission_count 的期望值，显式对齐
        record["submission_count"] = 0 if new_state in (
            STATE_DRAFT, STATE_FAILED_RETRYABLE) else 1

        clean = clean_scope(scope)
        if clean and "scope_after" not in record:
            record["scope_after"] = clean
        record["updated_at"] = stamp
        _write(path, record)


def adopt_owned(endpoint, prompt, *, scope=None, root=None) -> dict:
    """这次调用能不能"认领"门禁事先写好的那条预约。

    为什么需要：`_Ledger.adopt()` 原来只把 `reserved` 置 True，等于"门禁说预约过，
    我就信"。过期回执（上一轮/别的会话留下的）于是会被当成本次预约，后续终态写账
    就会改掉旧记录（2026-11-12 Round 3 会审三方都点了这条）。

    判据只到「同一个窗口、同一个站」这一层：
      · 记录不存在            -> reservation_missing
      · 站点不同              -> site_mismatch
      · 两边都有 hwnd 且不同  -> hwnd_mismatch
    为什么不逐字段比 `reconcile` 那一套：发送时新会话还没生成 session_id，
    doc_rid 也会因为新开对话而变；按那里比会把**每一次正常首发**都拒掉。
    adopt 只需要挡住"上一轮/别的窗口留下的过期回执"，窗口级身份足够，
    真正的严格比对在恢复阶段由 `reconcile()` 做。
    """
    record = read(endpoint, prompt, root=root)
    if not record:
        return {"ok": False, "reason": "reservation_missing"}
    stored = clean_scope(record.get("scope"))
    now = clean_scope(scope)
    if stored and now:
        if stored.get("site") and now.get("site") and stored["site"] != now["site"]:
            return {"ok": False, "reason": "site_mismatch"}
        if stored.get("hwnd") is not None and now.get("hwnd") is not None \
                and stored["hwnd"] != now["hwnd"]:
            return {"ok": False, "reason": "hwnd_mismatch"}
    return {"ok": True, "v2_state": (normalize(record) or {}).get("v2_state")}


#: 允许降级的证据键。三项里 `page_empty` 必填，另两项至少一项——
#: 单凭"页面空白"不足以降级（可能只是还没渲染），必须再有"身份/证据不支持"的旁证。
DEMOTE_REQUIRED = "page_empty"
DEMOTE_SUPPORT = ("scope_mismatch", "no_l3")


def demote(endpoint, prompt, *, reason: str, evidence: dict, root=None, now=None) -> dict:
    """`accepted` → `unknown`：**只降一级、只降一次、留审计**。

    为什么需要：accepted 在 `BLOCKING_STATES` 里，一旦记录与页面事实不符
    （2026-11-12 gemini 汇总：消息上屏后页面被重置，账本仍是 accepted），
    该提问就永久发不出去，只能人工清账。会审三方一致要求：允许降级，但**不得**
    降成 draft、不得自动放行重发——`unknown` 仍然拦截，把决定权留给人。

    门槛（缺一不可）：
      · 记录存在且当前就是 accepted；
      · 从未降级过（`demotions` 为空）；
      · `page_empty=True`（页面确实找不到这条消息）；
      · `scope_mismatch` 或 `no_l3` 至少一条（身份或证据不支持"已提交"）；
      · `reason` 非空。
    原 `level` / `scope` / `accepted_at` 全部保留——降级是加一条事实，不是抹掉历史。
    """
    path = path_for(endpoint, prompt, root=root)
    record = read(endpoint, prompt, root=root)
    view = normalize(record)
    if not view:
        return {"ok": False, "reason": "no_record", "path": str(path)}
    # 先判「降过没有」：降级之后状态已经变成 unknown，先判状态就永远报 not_accepted，
    # 分不清"本来就不是 accepted"和"已经降过一次"。
    if record.get("demotions"):
        return {"ok": False, "reason": "already_demoted",
                "demotions": len(record.get("demotions") or []), "path": str(path)}
    current = view.get("v2_state")
    if current != STATE_ACCEPTED:
        return {"ok": False, "reason": "not_accepted", "state": current,
                "path": str(path)}
    evidence = dict(evidence or {})
    if not evidence.get(DEMOTE_REQUIRED):
        return {"ok": False, "reason": "evidence_missing:page_empty", "path": str(path)}
    if not any(evidence.get(key) for key in DEMOTE_SUPPORT):
        return {"ok": False, "reason": "evidence_missing:scope_mismatch|no_l3",
                "path": str(path)}
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "reason": "reason_required", "path": str(path)}

    stamp = now if now is not None else time.time()
    before = {"state": record.get("state"), "v2_state": current,
              "level": record.get("level")}
    record.update({"state": _v1_name(STATE_UNKNOWN), "v2_state": STATE_UNKNOWN,
                   "submission_count": 1,          # unknown 仍按"可能已发"占位
                   "demoted_at": stamp, "demote_reason": reason})
    record.setdefault("demotions", []).append(
        {"at": stamp, "reason": reason, "evidence": evidence,
         "from": current, "to": STATE_UNKNOWN})
    _write(path, record)
    after = {"state": record.get("state"), "v2_state": record.get("v2_state")}
    return {"ok": True, "path": str(path), "before": before, "after": after,
            "blocking": after["v2_state"] in BLOCKING_STATES,
            "hint": "仍被拦截；确需重发请人工核对后走 submission_clear --state unknown"}


def refusal(value):
    """对"不允许发车"的记录给一份统一的拒发结果。"""
    view = normalize(value) or {}
    # 只有**已确认接受**才报 accepted；submitting / unknown 仍报 unknown
    # （与 v1 一致：v1 里"提交中"本来就写作 unknown）。
    blocking = view.get("v2_state") == STATE_ACCEPTED
    return {"ok": False, "stage": "interrupted", "sent": None,
            "submission": "accepted" if blocking else "unknown",
            "reason": "submission_pending", "hint": "上次提交已接受或状态不明；只读查答，禁止盲目重发"}


def summary(record):
    """给诊断/审计用的一行摘要（不含正文）。"""
    view = normalize(record)
    if not view:
        return {"v2_state": None, "level": None, "legacy": None}
    return {"v2_state": view.get("v2_state"), "level": view.get("level"),
            "legacy": view.get("legacy"), "submission_count": view.get("submission_count"),
            "attempts": len(view.get("attempts") or []),
            "has_scope": bool(view.get("scope")),
            "has_scope_after": bool(view.get("scope_after")),
            "session_id": bool((clean_scope(view.get("scope")) or {}).get("session_id"))}
