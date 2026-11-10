"""写完 progress.json，再发一条完工通知（任务卡 01）。

用法：
    python src\\scripts\\notify_done.py --card 01 --status done --note "一句话说明"

环境变量：
    BRIDGE_NOTIFY_URL  收通知的地址。没设就只写文件：不联网、不报错、退出码 0。
    BRIDGE_NOTIFY_KEY  有就放进请求头 X-Bridge-Key；不落盘、不进日志、不写进 progress.json。

顺序固定：先落文件，再发通知——通知失败也不能让监工看不到状态。
POST 的 body 和 progress.json 内容一致，Content-Type: application/json; charset=utf-8。
地址是 127.0.0.1 / localhost 时直连、不走 HTTP_PROXY（代理接不了回环）。
发送失败重试 2 次（共 3 次尝试），仍失败就在 progress.json 记 notify_failed=true，
退出码仍为 0；只有「写文件」本身失败才返回 2。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qi import progress  # noqa: E402

TIMEOUT = 5.0       # 单次 POST 超时（秒）
RETRIES = 2         # 失败后重试次数：共 3 次尝试
RETRY_WAIT = 0.5    # 重试间隔基数，第 n 次重试等 n * RETRY_WAIT
USER_AGENT = "bridge-notify/0.0.9"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _open(request: urllib.request.Request, timeout: float):
    """回环地址直连：本机若设了 HTTP_PROXY，代理也接不了 127.0.0.1。"""
    host = (urllib.parse.urlsplit(request.full_url).hostname or "").lower()
    if host in LOOPBACK:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def post(url: str, key: str, payload: dict, *, timeout: float = TIMEOUT,
         retries: int = RETRIES, wait: float | None = None) -> tuple[bool, str]:
    """发一次通知，失败重试。返回 (成功与否, 最后一次的错误描述)。"""
    wait = RETRY_WAIT if wait is None else wait
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}
    if key:
        headers["X-Bridge-Key"] = key
    last = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with _open(request, timeout) as response:
                code = int(getattr(response, "status", None) or response.getcode())
            if 200 <= code < 300:
                return True, ""
            last = f"HTTP {code}"
        except Exception as exc:                      # noqa: BLE001 - 网络异常种类太多
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries and wait:
            time.sleep(wait * (attempt + 1))
    return False, last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notify_done.py", description="先写 progress.json，再发完工通知")
    parser.add_argument("--card", required=True, help="任务卡号，如 01")
    parser.add_argument("--status", required=True, choices=progress.STATUSES)
    parser.add_argument("--note", required=True, help="一句话说明，不要粘提示词或回复正文")
    args = parser.parse_args(argv)

    try:
        record = progress.write(args.card, args.status, args.note)
    except (OSError, ValueError) as exc:
        print(f"progress write failed: {exc}", file=sys.stderr)
        return 2

    url = os.environ.get("BRIDGE_NOTIFY_URL", "").strip()
    if not url:
        # T4-1 第 3 条：通知模块"未配置"必须明确登记，不能只是个静默 skip。
        # 保留原有的人类可读行（有脚本/习惯依赖），再追加一行结构化状态供检索。
        print(f"progress.json written ({record['status']}); "
              f"notify NOT_CONFIGURED (BRIDGE_NOTIFY_URL unset, notify skipped)")
        print(json.dumps({"notify": "NOT_CONFIGURED",
                          "reason": "BRIDGE_NOTIFY_URL_unset",
                          "card": record.get("card_id"),
                          "status": record.get("status")}, ensure_ascii=False))
        return 0

    key = os.environ.get("BRIDGE_NOTIFY_KEY", "").strip()
    ok, error = post(url, key, record)
    if ok:
        print("progress.json written; notified")
        return 0

    try:                                    # 记不上失败标记也别改退出码
        progress.mark_notify_failed(record)
    except OSError:
        pass
    print(f"progress.json written; notify failed after {RETRIES + 1} attempts ({error})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
