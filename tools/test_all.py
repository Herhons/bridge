"""Final suite: offline regressions + 16 CLI entrypoints; --live adds one real turn per site."""
import argparse
import ast
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITES = ("deepseek", "grok", "gemini", "chatgpt", "workbuddy", "opencode", "dsh", "codex")


def run(script, args, timeout):
    command = [sys.executable, "-X", "utf8", "-B", str(script), *args]
    started = time.monotonic()
    try:
        process = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                 capture_output=True, timeout=timeout)
        return {"returncode": process.returncode, "stdout": process.stdout,
                "stderr": process.stderr, "seconds": round(time.monotonic() - started, 2)}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": 124, "stage": "process_timeout", "submission": "unknown",
                "seconds": round(time.monotonic() - started, 2)}


def last_result(output):
    for line in reversed(output.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--only", default=",".join(SITES))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    sites = args.only.split(",")
    if not all(site in SITES for site in sites):
        parser.error("--only 包含未知端")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    outdir = args.output_dir or ROOT / "deliverables" / "tests" / stamp
    outdir.mkdir(parents=True, exist_ok=False)
    rows = []

    def record(case, result, ok):
        row = {"case": case, "ok": bool(ok), **result}
        rows.append(row)
        (outdir / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{case}: {'PASS' if ok else 'FAIL'}", flush=True)

    sources = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tools").glob("*.py"))
    for source in sources:
        ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    record("syntax", {"files": len(sources)}, True)
    regression = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "unittest",
                                 "discover", "-s", "tests", "-v"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8")
    record("regressions", {"returncode": regression.returncode,
                            "output": regression.stdout + regression.stderr}, regression.returncode == 0)
    for site in sites:
        for kind in ("choose", "use"):
            script = ROOT / "src" / "scripts" / f"{site}_{kind}.py"
            result = run(script, ["--help"], 30)
            record(f"{site}_{kind}:help", result, result["returncode"] == 0)
    if not all(r["ok"] for r in rows):
        print(f"离线检查未通过，未进行真实发送。结果：{outdir}")
        return 1
    if args.live:
        for site in sites:
            result = run(ROOT / "src/scripts" / f"{site}_choose.py",
                         ["--list", "--idle", "2", "--wait", "15"], 90)
            record(f"{site}_choose:live", result, result["returncode"] == 0)
            token = f"BR009-{stamp}-{site}-OK"
            prompt = ("这是收发回归测试。不要调用工具、执行命令、访问文件或联网。\n"
                      "请只回复下面的标记，不加解释：\n" + token)
            result = run(ROOT / "src/scripts" / f"{site}_use.py",
                         [prompt, "--new", "--timeout", "120", "--idle", "2", "--wait", "15"], 160)
            data = last_result(result.get("stdout", ""))
            ok = result["returncode"] == 0 and data and data.get("ok") and token in data.get("reply", "")
            record(f"{site}_use:live", {**result, "expected_reply": token}, ok)
    print(f"结果：{outdir}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
