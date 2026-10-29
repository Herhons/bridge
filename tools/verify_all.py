"""把四端的「所有模式 × 所有模型」跑一遍，只做选择与复核，**不发送任何消息**。

用法：
    python tools/verify_all.py --only deepseek,grok
    python tools/verify_all.py --only gemini
    python tools/verify_all.py --only chatgpt
    python tools/verify_all.py --random 3 --only grok,gemini   # 随机抽组合复验

每条记录：目标组合、返回码、脚本自报的 before/after、读回值、是否通过。
结果同时写到 deliverables/verify-<端>.json，方便回看。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "src", "scripts")
PY = sys.executable

TABS = {"grok": 1, "chatgpt": 1, "gemini": 3, "deepseek": 8}

# gemini 任务模式的模型列表与对话模式不同（实测：任务只有 Flash / Argon）
GEMINI_MODELS = {
    "dialog": ["flash_lite", "flash", "pro", "argon"],
    "task": ["flash", "argon"],
}
CHATGPT_MODELS = {
    "chat": ["gpt6", "gpt56"],
    "work": ["default", "gpt61_sol", "gpt61_terra", "gpt6_astra", "gpt6_sol",
             "gpt6_luna", "gpt56_sol", "gpt56_terra", "gpt56_luna"],
}


def run(script: str, args: list[str], tab: int, timeout: int = 120) -> tuple[int, dict, str]:
    cmd = [PY, os.path.join(SCRIPTS, script), *args, "--tab", str(tab)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or "").strip().splitlines()
        last = out[-1] if out else ""
        try:
            data = json.loads(last)
        except Exception:
            data = {"_raw": last[-300:]}
        data["_sec"] = round(time.time() - t0, 1)
        return p.returncode, data, last
    except subprocess.TimeoutExpired:
        return 124, {"_raw": "timeout", "_sec": round(time.time() - t0, 1)}, "timeout"


def rec(kind: str, target: str, rc: int, data: dict, ok: bool, detail: str = "") -> dict:
    return {"kind": kind, "target": target, "rc": rc, "ok": bool(ok),
            "detail": detail, "sec": data.get("_sec"), "raw": data}


# ------------------------------------------------------------------ 各端
def verify_deepseek() -> list[dict]:
    rows = []
    for th in ("on", "off"):
        for se in ("on", "off"):
            # 每个开关打印一行 JSON，所以分两次设，最后用 --list 统一复核
            run("deepseek_choose.py", ["--think", th], TABS["deepseek"])
            run("deepseek_choose.py", ["--search", se], TABS["deepseek"])
            rc, d, _ = run("deepseek_choose.py", ["--list"], TABS["deepseek"])
            got_t = (d.get("think") or {}).get("state")
            got_s = (d.get("search") or {}).get("state")
            ok = rc == 0 and got_t == th and got_s == se
            rows.append(rec("state", f"think={th}, search={se}", rc, d, ok,
                            f"读回 think={got_t}, search={got_s}"))
    return rows


def verify_grok() -> list[dict]:
    rows = []
    for m in ("fast", "expert", "auto", "build"):
        rc, d, _ = run("grok_choose.py", ["--model", m], TABS["grok"])
        ok = rc == 0 and d.get("ok") and d.get("after") == m
        rows.append(rec("model", m, rc, d, ok, f"after={d.get('after')}"))
    return rows


def verify_gemini() -> list[dict]:
    rows = []
    for w, models in GEMINI_MODELS.items():
        rc, d, _ = run("gemini_choose.py", ["--writer", w], TABS["gemini"])
        ok = rc == 0 and d.get("ok") and d.get("after") == w
        rows.append(rec("writer", w, rc, d, ok, f"after={d.get('after')}"))
        for m in models:
            rc, d, _ = run("gemini_choose.py", ["--model", m], TABS["gemini"])
            ok = rc == 0 and d.get("ok") and d.get("after") == m
            rows.append(rec("model", f"{w}+{m}", rc, d, ok, f"after={d.get('after')}"))
    return rows


def verify_chatgpt() -> list[dict]:
    rows = []
    for w, models in CHATGPT_MODELS.items():
        rc, d, _ = run("chatgpt_choose.py", ["--writer", w], TABS["chatgpt"])
        ok = rc == 0 and d.get("ok") and d.get("after") == w
        rows.append(rec("writer", w, rc, d, ok, f"after={d.get('after')}"))
        for m in models:
            rc, d, _ = run("chatgpt_choose.py", ["--model", m], TABS["chatgpt"])
            ok = rc == 0 and d.get("ok") and d.get("after") == m
            rows.append(rec("model", f"{w}+{m}", rc, d, ok, f"after={d.get('after')}"))
    for e in ("instant", "mid"):
        rc, d, _ = run("chatgpt_choose.py", ["--effort", e], TABS["chatgpt"])
        ok = rc == 0 and d.get("ok")
        rows.append(rec("effort", e, rc, d, ok, f"after={d.get('after')}"))
    return rows


VERIFIERS = {"deepseek": verify_deepseek, "grok": verify_grok,
             "gemini": verify_gemini, "chatgpt": verify_chatgpt}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="deepseek,grok,gemini,chatgpt")
    ap.add_argument("--random", type=int, default=0, help="每个端随机抽 N 个组合复验")
    ap.add_argument("--seed", type=int, default=20261029)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    outdir = os.path.join(ROOT, "deliverables")
    os.makedirs(outdir, exist_ok=True)

    summary = {}
    for name in names:
        print(f"\n===== {name} =====", flush=True)
        rows = VERIFIERS[name]()
        passed = sum(1 for r in rows if r["ok"])
        for r in rows:
            flag = "OK  " if r["ok"] else "FAIL"
            print(f"  [{flag}] {r['kind']:6} {r['target']:<28} {r['detail']}  {r['sec']}s", flush=True)

        if args.random:
            good = [r for r in rows if r["ok"]]
            picks = rng.sample(good, min(args.random, len(good)))
            print(f"  --- 随机复验 {len(picks)} 个 ---", flush=True)
            for r in picks:
                tgt = r["target"]
                if name == "deepseek":
                    parts = dict(p.split("=") for p in tgt.replace(" ", "").split(","))
                    run("deepseek_choose.py", ["--think", parts["think"]], TABS[name])
                    run("deepseek_choose.py", ["--search", parts["search"]], TABS[name])
                    rc, d, _ = run("deepseek_choose.py", ["--list"], TABS[name])
                    got_t = (d.get("think") or {}).get("state")
                    got_s = (d.get("search") or {}).get("state")
                    ok = rc == 0 and got_t == parts["think"] and got_s == parts["search"]
                    detail = f"复验读回 think={got_t}, search={got_s}"
                elif name == "grok":
                    rc, d, _ = run("grok_choose.py", ["--model", tgt], TABS[name])
                    ok = rc == 0 and d.get("ok") and d.get("after") == tgt
                    detail = f"after={d.get('after')}"
                else:
                    script = f"{name}_choose.py"
                    if r["kind"] == "writer":
                        rc, d, _ = run(script, ["--writer", tgt], TABS[name])
                        ok = rc == 0 and d.get("ok") and d.get("after") == tgt
                        detail = f"after={d.get('after')}"
                    elif "+" in tgt:
                        w, m = tgt.split("+", 1)
                        rc1, d1, _ = run(script, ["--writer", w], TABS[name])
                        rc, d, _ = run(script, ["--model", m], TABS[name])
                        ok = rc1 == 0 and d1.get("ok") and rc == 0 and d.get("ok") and d.get("after") == m
                        detail = f"writer={d1.get('after')}, after={d.get('after')}"
                    else:
                        rc, d, _ = run(script, ["--model", tgt], TABS[name])
                        ok = rc == 0 and d.get("ok") and d.get("after") == tgt
                        detail = f"after={d.get('after')}"
                rows.append(rec("random", tgt, rc, d, ok, detail))
                print(f"  [{'OK  ' if ok else 'FAIL'}] random {tgt:<28} {detail}  {d.get('_sec')}s",
                      flush=True)

        passed = sum(1 for r in rows if r["ok"])
        summary[name] = {"total": len(rows), "passed": passed,
                         "failed": [r["target"] for r in rows if not r["ok"]]}
        with open(os.path.join(outdir, f"verify-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"  小结：{passed}/{len(rows)} 通过", flush=True)

    print("\n===== 汇总 =====")
    for k, v in summary.items():
        print(f"  {k:9} {v['passed']}/{v['total']}  失败: {v['failed'] or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
