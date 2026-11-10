"""每端随机抽 N 个组合跑一遍，只做选择与复核，**不发送任何消息**。

用法：
    python tools/spotcheck.py                 # 每端随机 3 个
    python tools/spotcheck.py --n 5
    python tools/spotcheck.py --only grok,gemini --seed 123

和 verify_all.py 的区别：那个是"先跑全量、再从通过项里抽"，很慢；
这里直接抽组合、只跑抽中的，适合快速确认现在还能不能用。
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


def combos_deepseek():
    out = []
    for th in ("on", "off"):
        for se in ("on", "off"):
            out.append({
                "label": f"think={th} search={se}",
                "set": ["--think", th, "--search", se],
                "verify": (th, se),
            })
    return out


def combos_grok():
    return [{"label": m, "set": ["--model", m], "verify": m}
            for m in ("fast", "expert", "auto", "build")]


GEMINI = {"dialog": ["flash_lite", "flash", "pro", "argon"], "task": ["flash", "argon"]}
CHATGPT = {
    "chat": ["gpt6", "gpt56"],
    "work": ["default", "gpt61_sol", "gpt61_terra", "gpt6_astra",
             "gpt6_sol", "gpt6_luna", "gpt56_sol", "gpt56_terra", "gpt56_luna"],
}


def combos_writer(table):
    out = []
    for w, models in table.items():
        for m in models:
            out.append({"label": f"{w}+{m}", "writer": w, "model": m})
    return out


SPEC = {
    "deepseek": ("deepseek_choose.py", combos_deepseek),
    "grok": ("grok_choose.py", combos_grok),
    "gemini": ("gemini_choose.py", lambda: combos_writer(GEMINI)),
    "chatgpt": ("chatgpt_choose.py", lambda: combos_writer(CHATGPT)),
}


def run(script: str, args: list[str], timeout: int = 150):
    cmd = [PY, os.path.join(SCRIPTS, script), *args]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        lines = (p.stdout or "").strip().splitlines()
        last = lines[-1] if lines else ""
        try:
            data = json.loads(last)
        except Exception:
            data = {"_raw": last[-200:]}
        return p.returncode, data, round(time.time() - t0, 1), lines
    except subprocess.TimeoutExpired:
        return 124, {"_raw": "timeout"}, round(time.time() - t0, 1), []


def check(name: str, item: dict) -> tuple[bool, str]:
    script = SPEC[name][0]
    if name == "deepseek":
        th, se = item["verify"]
        run(script, ["--list"])                       # 先读一次，确保页面就绪
        rc, d, _, _ = run(script, item["set"])
        rc2, v, _, _ = run(script, ["--list"])
        got_t = (v.get("think") or {}).get("state")
        got_s = (v.get("search") or {}).get("state")
        ok = rc2 == 0 and got_t == th and got_s == se
        return ok, f"读回 think={got_t} search={got_s}"

    if item.get("writer"):
        w, m = item["writer"], item["model"]
        rc1, d1, _, _ = run(script, ["--writer", w])
        rc2, d2, _, _ = run(script, ["--model", m])
        ok = (rc1 == 0 and d1.get("ok") and rc2 == 0 and d2.get("ok")
              and d2.get("after") == m)
        return ok, f"writer={d1.get('after')} after={d2.get('after')}"

    rc, d, _, _ = run(script, item["set"])
    return (rc == 0 and d.get("ok") and d.get("after") == item["verify"],
            f"after={d.get('after')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--only", default="deepseek,grok,gemini,chatgpt")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    print(f"随机种子：{seed}（复现用 --seed {seed}）\n")

    names = [n.strip() for n in args.only.split(",") if n.strip()]
    total = passed = 0
    failed: list[str] = []

    for name in names:
        pool = SPEC[name][1]()
        picks = rng.sample(pool, min(args.n, len(pool)))
        print(f"===== {name}（{len(pool)} 个组合，抽 {len(picks)}）=====", flush=True)
        for item in picks:
            ok, detail = check(name, item)
            total += 1
            passed += 1 if ok else 0
            if not ok:
                failed.append(f"{name}:{item['label']}")
            print(f"  [{'OK  ' if ok else 'FAIL'}] {item['label']:<22} {detail}", flush=True)
        print("", flush=True)

    print(f"合计 {passed}/{total} 通过")
    if failed:
        print("失败：", ", ".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
