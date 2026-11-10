"""真机驱动：提交一个 /ask job，轮询到终态，落证据。

**为什么单独成文件**：2026-11-08 之前两次真机尝试都是内联 `urllib` 片段，
失败时没留下任何可复现的东西（`evidence/batch13/live_*.txt` 全文只有
`ConnectionRefusedError`）。真机验证必须能一条命令重放。

平台约束：
- 只连 `127.0.0.1`，token 从 `%LOCALAPPDATA%\\Bridge\\v009\\server.json` 读，**不打印**。
- **必须绕过系统代理**：本机开着代理，`urllib` 默认走它，本机回环会拿到 502。
- 退出码：0=全部 done；1=partial；2=failed；3=等待超时。

用法：
  py -3.10 -X utf8 -B tools/live_job.py --endpoints codex,workbuddy \
      --prompt "只回复：XXX" --purpose test --out evidence/batch14/job_p2.json
"""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:18765"


def token_of() -> str:
    path = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Bridge" / "v009" / "server.json"
    return json.loads(path.read_text(encoding="utf-8"))["token"]


def make_caller(token: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            BASE + path, data=data,
            # 服务端只认 `Authorization: Bearer <token>`（`server.py:_authorized`），
            # 不是自定义头；写错会得到 401 unauthorized。
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST" if body is not None else "GET")
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    return call


def brief(endpoint_row: dict) -> str:
    name = endpoint_row.get("status")
    use = endpoint_row.get("use") or {}
    progress = endpoint_row.get("progress") or {}
    parts = [str(name)]
    if use.get("stage"):
        parts.append(f"stage={use['stage']}")
    if progress.get("stage"):
        secs = int(progress.get("elapsed") or 0)
        parts.append(f"{progress['stage']} {secs}s")
        if progress.get("reads") is not None:
            parts.append(f"读{progress['reads']}")
    return " ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="live_job.py")
    parser.add_argument("--endpoints", required=True, help="逗号分隔，如 codex,workbuddy")
    parser.add_argument("--prompt", help="提问正文（与 --prompt-file 二选一）")
    parser.add_argument("--prompt-file", help="从文件读提问（多行长材料用；优先于 --prompt）")
    parser.add_argument("--purpose", default="test")
    parser.add_argument("--wait", type=float, default=180.0, help="最多等多少秒")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--out", default="", help="把完整 job JSON 写到这里")
    parser.add_argument("--label", default="", help="打印用的标记")
    args = parser.parse_args(argv)

    # B20：长材料（3–5k 字、几十行）经 shell 传参在引号/换行上不可靠，改从文件读。
    if args.prompt_file:
        prompt = pathlib.Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.prompt:
        prompt = args.prompt
    else:
        parser.error("--prompt 与 --prompt-file 必须给一个")
    args.prompt = prompt

    names = [item.strip() for item in args.endpoints.split(",") if item.strip()]
    call = make_caller(token_of())
    label = f"[{args.label}] " if args.label else ""

    print(f"{label}提交 {time.strftime('%H:%M:%S')} → {names} purpose={args.purpose}",
          flush=True)
    submitted = call("/ask", {"endpoints": names, "prompt": args.prompt,
                              "purpose": args.purpose, "request_id": str(uuid.uuid4())})
    job_id = submitted["job_id"]
    print(f"{label}job {job_id} status={submitted.get('status')}", flush=True)

    started = time.monotonic()
    last_line = None
    samples = []
    job = {}
    while time.monotonic() - started < args.wait:
        job = call(f"/jobs/{job_id}")["job"]
        rows = job.get("endpoints") or {}
        line = f"{label}[{int(time.monotonic() - started):3d}s] {job['status']:<8} " + \
               " | ".join(f"{name}: {brief(rows.get(name) or {})}" for name in names)
        if line != last_line:
            print(line, flush=True)
            last_line = line
        # B16：只采 reads 看不出「为什么一直不交卷」。把心跳里的诊断帧也留下来，
        # 超时被 kill 时 stdout 会整个丢掉，只有这份采样能还原 text_len/busy 曲线。
        trace_keys = ("stage", "reads", "busy", "copies", "baseline", "accepted",
                      "composer_len", "text_len", "candidates", "sig", "why")
        progress = {n: (rows.get(n) or {}).get("progress", {}) for n in names}
        samples.append({
            "at": round(time.monotonic() - started, 1), "status": job["status"],
            "reads": {n: progress[n].get("reads") for n in names},
            "trace": {n: {k: progress[n][k] for k in trace_keys if k in progress[n]}
                      for n in names},
        })
        if job["status"] in ("done", "partial", "failed"):
            break
        time.sleep(args.interval)

    print(f"\n{label}=== 终态 ===")
    print(f"{label}status={job.get('status')} quorum={job.get('quorum')}")
    rows = job.get("endpoints") or {}
    for name in names:
        row = rows.get(name) or {}
        use = row.get("use") or {}
        print(f"{label}  {name}: status={row.get('status')} stage={use.get('stage')} "
              f"elapsed={row.get('elapsed')}")
        print(f"{label}    detail={use.get('detail')}")
        print(f"{label}    choose={{{(row.get('choose') or {}).get('dimension')}: "
              f"{(row.get('choose') or {}).get('elapsed')}s ok={(row.get('choose') or {}).get('ok')}}}")
        print(f"{label}    reply={(row.get('reply') or '')[:110]!r}")
        err = row.get("error")
        if err:
            print(f"{label}    error={err}")

    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"job": job, "samples": samples},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{label}证据写入 {path}")

    status = job.get("status")
    if status == "done":
        return 0
    if status == "partial":
        return 1
    if status == "failed":
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
