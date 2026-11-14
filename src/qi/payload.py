"""端到端文件载荷通道（CARD-v010-P0P1 第 2 条）。

## 它解决什么

2026-11-14 的截断事故（#7）：正文走 `argv` 传给 8 个入口脚本时，长文 / 中文 /
引号 / 换行在 `.cmd` 拼接、PowerShell 转义、控制台代码页三处都可能被改写。
改成"正文只走 UTF-8 文件 + SHA-256"，命令行只留路径，字节就不再经过任何转义层。

三件事：

  · `--payload-file <path>`  —— 从文件读正文（强制 UTF-8，不做 BOM 猜测）；
  · `--payload-sha256 <hex>` —— 进任何 UI 操作**之前**校验字节哈希；
  · `--result-file <path>`   —— 把结构化结果写到文件，父进程不用再猜 stdout。

## 为什么用 `run(main)` 包装而不是每个 main 里加一行

8 个入口的 argparse 各不相同，而且 codex/dsh/opencode 的 `main` 有 1600+ 行。
包装在 `__main__` 层只需要改一行，还能统一兜住 argparse 的 `SystemExit`
和 `uilock.Busy` 的 JSON 输出 —— 否则这两类结果写不进 result-file。

## 注入方式

`--payload-file` 意味着"这次是发送"，所以：

  1. 若 argv 第一个词不是 `use`/`read`/`choose`，补一个 `use`（避免正文被
     `_detect_mode` 当成 flag 扫描的输入）；
  2. 正文以 `["--", text]` 追加到**末尾**。`--` 让 argparse 把以 `-` 开头的正文
     仍当位置参数，前面的 flag 照常解析。

## 令牌

`--grant-token` 从 argv 摘出后写进 `BRIDGE_GRANT_TOKEN`（与 `BRIDGE_JOB_ID`
同一命名族），不落盘、不进日志、不进 result-file。父进程已设且不一致时直接
`bad_args` 退出，防止子进程被投毒换令牌。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import exit_codes as ec

ENV_GRANT_TOKEN = "BRIDGE_GRANT_TOKEN"
ENV_IDLE_GATE = "BRIDGE_IDLE_GATE"
ENV_PAYLOAD_FILE = "BRIDGE_PAYLOAD_FILE"
ENV_PAYLOAD_SHA = "BRIDGE_PAYLOAD_SHA256"
ENV_RESULT_FILE = "BRIDGE_RESULT_FILE"

#: 只属于"外壳"的参数：摘走之后各端 parser 不会看到它们。
VALUE_FLAGS = {
    "--payload-file": "prompt_file",
    "--payload-sha256": "payload_sha256",
    "--result-file": "result_file",
    "--grant-token": "grant_token",
}
BOOL_FLAGS = {"--idle-gate": "idle_gate"}

#: 本进程最后一次 `record()` 的结果（result-file 的权威来源）。
_LAST: dict | None = None


class PayloadRefused(RuntimeError):
    """载荷通道拒绝。CLI 层转成 `stage=precheck_refused` / 退出码 11。"""

    stage = "precheck_refused"
    delivery_state = "PRECHECK_REFUSED"
    code = ec.PRECHECK_REFUSED

    def __init__(self, reason: str, hint: str = ""):
        self.reason = str(reason)
        self.hint = str(hint or reason)
        super().__init__(self.hint)

    def as_result(self) -> dict:
        return {
            "ok": False,
            "stage": self.stage,
            "delivery_state": self.delivery_state,
            "attempt_consumed": False,
            "sent": False,
            "submission": "not_sent",
            "reason": self.reason,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class Options:
    prompt_file: Path | None = None
    payload_sha256: str | None = None
    result_file: Path | None = None
    grant_token: str | None = None
    idle_gate: bool = False


def _take(argv: list[str], flags: dict[str, str]) -> tuple[dict, list[str]]:
    """把 `--x v` / `--x=v` 两种写法都摘出来，其余原样返回。"""
    found: dict = {}
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        key, sep, inline = token.partition("=")
        if key in flags and sep:
            found[flags[key]] = inline
            index += 1
            continue
        if token in flags:
            if index + 1 >= len(argv):
                raise PayloadRefused("bad_args", f"{token} 缺少取值")
            found[flags[token]] = argv[index + 1]
            index += 2
            continue
        rest.append(token)
        index += 1
    return found, rest


def _current_grant() -> str | None:
    value = (os.environ.get(ENV_GRANT_TOKEN) or "").strip()
    return value or None


def preflight(argv: list[str], *, consume_payload: bool = True,
              consume_grant: bool = True) -> tuple[Options, list[str]]:
    """摘出外壳参数并写环境变量。返回 (Options, 交回给各端 parser 的 argv)。

    `consume_payload=False` 给 `bridge.py` 用：`bridge run <ai> use ...` 只是
    subprocess 转发，`--payload-file` 必须留给子进程消费，否则会出现两个写入方。
    """
    values, rest = _take(argv, VALUE_FLAGS if consume_payload else
                         {k: v for k, v in VALUE_FLAGS.items()
                          if k in ("--result-file", "--grant-token")})
    # 布尔开关（`--idle-gate`）不吃下一个 token，单独摘。
    bools = {BOOL_FLAGS[token] for token in rest if token in BOOL_FLAGS}
    rest = [token for token in rest if token not in BOOL_FLAGS]

    options = Options(
        prompt_file=Path(values["prompt_file"]) if values.get("prompt_file") else None,
        payload_sha256=(values.get("payload_sha256") or "").strip().lower() or None,
        result_file=Path(values["result_file"]) if values.get("result_file") else None,
        grant_token=(values.get("grant_token") or "").strip() or None,
        idle_gate="idle_gate" in bools,
    )

    if options.grant_token and consume_grant:
        existing = _current_grant()
        if existing and existing != options.grant_token:
            raise PayloadRefused(
                "grant_token_conflict",
                "父进程已经下发了一张票据，与命令行上的不是同一张；拒绝覆盖")
        os.environ[ENV_GRANT_TOKEN] = options.grant_token
    if options.idle_gate:
        os.environ[ENV_IDLE_GATE] = "1"
    if options.prompt_file is not None:
        os.environ[ENV_PAYLOAD_FILE] = str(options.prompt_file)
    if options.payload_sha256:
        os.environ[ENV_PAYLOAD_SHA] = options.payload_sha256
    if options.result_file is not None:
        os.environ[ENV_RESULT_FILE] = str(options.result_file)
    return options, rest


#: server 写出的信封形状。`v=1` 时字段名固定；这里只做"必须出现"的校验，
#: 不要求键集完全相等 —— 以后加键不会让信封静默退化成纯文本。
ENVELOPE_PURPOSES = ("test", "review", "accept")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _unwrap(text: str) -> tuple[str, str] | None:
    """识别 server 的 JSON 信封，返回 (正文, 信封声明的正文哈希)。

    判据必须够严：这个仓库里的提问经常直接贴 JSON 片段，只靠"以 `{` 开头 +
    能解析 + 有 prompt 字段"会把用户的普通提问误认成信封，然后把用户精心写的
    JSON 当成外壳剥掉。这里要求 v/endpoint/purpose/prompt_sha256 同时成立。
    """
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("{"):
        return None
    try:
        doc = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    if not isinstance(doc.get("prompt"), str):
        return None
    if not isinstance(doc.get("endpoint"), str) or not doc["endpoint"].strip():
        return None
    if doc.get("purpose") not in ENVELOPE_PURPOSES:
        return None
    declared = str(doc.get("prompt_sha256") or "").strip().lower()
    if not _SHA_RE.match(declared):
        return None
    return doc["prompt"], declared


def load(options: Options) -> tuple[str | None, dict | None]:
    """读正文并校验哈希。返回 (text, None) 或 (None, 拒绝结果)。

    CARD-v010-FIX-AUDIT：支持两种载荷格式。

    · **信封**（`server._run_use` 写的 `runs/payload/<stem>.json`）：
      `{"v":1,"endpoint":...,"prompt":<正文>,"prompt_sha256":<正文哈希>,...}`。
      外层 `--payload-sha256` 传的是**正文**的哈希，所以必须解开信封、
      对 `doc["prompt"]` 校验，再把**纯正文**交给下游。直接拿整个文件字节去比
      （旧行为）恒不相等 → 每次派发都在这里被拒；即便放宽哈希，也会把
      `{"v":1,...}` 这层壳当正文发给 AI。
    · **纯文本**（CLI 手工传的 `.txt/.md`）：文件字节即正文，按整段字节校验。
    """
    if options.prompt_file is None:
        return None, None
    path = options.prompt_file
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, PayloadRefused(
            "payload_file_missing", f"找不到载荷文件：{path}").as_result()
    except OSError as exc:
        return None, PayloadRefused(
            "payload_file_unreadable", f"读不了载荷文件：{path} ({exc})").as_result()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, PayloadRefused(
            "payload_not_utf8",
            f"载荷不是合法 UTF-8（第 {exc.start} 字节起）：{path}").as_result()

    envelope = _unwrap(text)
    if envelope is not None:
        prompt, declared = envelope
        inner = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if declared != inner:
            # 信封自身对不上：这是文件损坏或被改过，**不能**退化成"把整段
            # JSON 当正文发出去"，那等于把损坏静默变成一次真实发送。
            return None, PayloadRefused(
                "payload_envelope_sha_mismatch",
                f"信封内的 prompt_sha256 与正文不符：声明 {declared} / 实际 {inner}"
            ).as_result()
        if options.payload_sha256 and options.payload_sha256.lower() != inner:
            return None, PayloadRefused(
                "payload_sha256_mismatch",
                f"载荷哈希不符：期望 {options.payload_sha256} / 实际 {inner}"
                "（信封格式下请传正文的 sha256，不是文件字节的）").as_result()
        return prompt, None

    if options.payload_sha256:
        actual = hashlib.sha256(raw).hexdigest()
        if actual.lower() != options.payload_sha256.lower():
            return None, PayloadRefused(
                "payload_sha256_mismatch",
                f"载荷哈希不符：期望 {options.payload_sha256} / 实际 {actual}").as_result()
    return text, None


def inject(argv: list[str], text: str) -> list[str]:
    """把正文安全地塞进 argv：补 `use` + 末尾 `-- <正文>`。"""
    args = list(argv)
    if not args or args[0] not in ("use", "read", "choose"):
        args = ["use"] + args
    elif args[0] == "choose":
        args = ["use"] + args[1:]
    return args + ["--", text]


# ---------------------------------------------------------------- 结果登记
def record(result: dict) -> None:
    """登记本进程的结构化结果。`run()` 收尾时优先写它。"""
    global _LAST
    if isinstance(result, dict):
        _LAST = dict(result)


def last_result() -> dict | None:
    return dict(_LAST) if _LAST else None


def reset() -> None:
    global _LAST
    _LAST = None


def _parse_last_json(text: str) -> dict | None:
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def write_result(path, payload: dict | None) -> bool:
    """原子写 result-file。失败**不抛**：结果写不成不该改变退出码。"""
    if path is None:
        return False
    body = payload if isinstance(payload, dict) else {}
    body = dict(body)
    body.pop("grant_token", None)          # 令牌绝不落盘
    target = Path(path)
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class _Tee:
    """`sys.stdout` 的写透代理：原样转发，同时留一份给"最后一个 JSON 行"兜底。"""

    def __init__(self, real):
        self._real = real
        self._chunks: list[str] = []

    def write(self, text):
        self._chunks.append(str(text))
        return self._real.write(text)

    def flush(self):
        return self._real.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def buffer(self):
        return self._real.buffer

    def reconfigure(self, **kwargs):
        return self._real.reconfigure(**kwargs)

    def captured(self) -> str:
        return "".join(self._chunks)


def _peek_result_file(argv: list[str]) -> Path | None:
    for index, token in enumerate(argv):
        if token == "--result-file" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if token.startswith("--result-file="):
            return Path(token.split("=", 1)[1])
    return None


def run(main, argv=None, *, endpoint: str = "",
        consume_payload: bool = True) -> int:
    """包装入口 `main`：载荷注入 + result-file 落盘 + 退出码兜底。"""
    reset()
    raw = list(sys.argv[1:] if argv is None else argv)
    fallback_result = _peek_result_file(raw)

    try:
        options, clean = preflight(raw, consume_payload=consume_payload)
        text, refusal = load(options) if consume_payload else (None, None)
    except PayloadRefused as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False))
        write_result(fallback_result, exc.as_result())
        return exc.code

    if refusal is not None:
        print(json.dumps(refusal, ensure_ascii=False))
        write_result(options.result_file or fallback_result, refusal)
        return ec.PRECHECK_REFUSED

    if text is not None:
        clean = inject(clean, text)

    tee = None
    if options.result_file is not None:
        tee = _Tee(sys.stdout)
        sys.stdout = tee
    code = 1
    try:
        try:
            code = int(main(clean))
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            raise
        except BaseException as exc:           # noqa: BLE001 - 结果文件先落盘，再原样抛
            payload = {"ok": False, "stage": "crashed",
                       "error": f"{type(exc).__name__}: {exc}"}
            record(payload)
            raise
    finally:
        if tee is not None:
            sys.stdout = tee._real
            body = last_result() or _parse_last_json(tee.captured()) or {
                "ok": code == 0, "stage": "no_result", "exit_code": code}
            body.setdefault("exit_code", code)
            write_result(options.result_file, body)
    return code
