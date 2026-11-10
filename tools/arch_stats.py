"""生成 `docs/ARCH-STATS.md`：全仓代码关系的可复现统计。

**只读**：只解析源码，不导入、不执行、不碰 UI。

    env -u PYTHONPATH py -3.10 -X utf8 -B tools/arch_stats.py
    env -u PYTHONPATH py -3.10 -X utf8 -B tools/arch_stats.py --out docs/ARCH-STATS.md

口径（写进输出，避免数字被误读）：

- **代码行**：`tokenize` 取实际 token 行，剔除空行、纯 `#` 注释行、以及独占整行的
  字符串/docstring 行。多行字符串里的坐标表会被当成字符串扣掉，属**低估**。
- **依赖边**：AST 的 `Import` / `ImportFrom`，只保留 `src/qi` 与 `src/scripts` 的内部名字，
  忽略标准库与第三方。同名跨包（例如标准库 `queue` 与内部 `queue.py`）按名字匹配，
  所以边数偏**高**，看方向比看数字更有意义。
- **相似度**：`difflib.SequenceMatcher.quick_ratio()`，是**上界近似**。
- **测试覆盖**：以 `tests/*.py` 里是否出现 `qi.<模块>` / `from qi import <模块>` 为准，
  属**静态猜测**，不代表真的跑到了。
"""
from __future__ import annotations

import argparse
import ast
import collections
import difflib
import io
import pathlib
import tokenize
import datetime as dt

QI = pathlib.Path("src/qi")
SCRIPTS = pathlib.Path("src/scripts")
AREAS = [("src/qi", "*.py"), ("src/scripts", "*.py"),
         ("tools", "*.py"), ("tests", "*.py"), ("src/chrome-extension", "*.js")]


def py_files(d: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in d.glob("*.py") if p.name != "__init__.py")


def first_doc(p: pathlib.Path) -> str:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return "(语法错误，未能解析)"
    doc = ast.get_docstring(tree) or ""
    line = next((x.strip() for x in doc.strip().splitlines() if x.strip()), "")
    return line[:60]


def imports_of(p: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module.split(".")[0])
            out.update(a.name for a in node.names)
    return out


def loc(p: pathlib.Path) -> tuple[int, int, int]:
    """(总行, 代码行, 空行)。"""
    src = p.read_text(encoding="utf-8", errors="replace")
    total = len(src.splitlines())
    blank = sum(1 for line in src.splitlines() if not line.strip())
    code: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER, tokenize.COMMENT):
                continue
            code.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        return total, 0, blank
    return total, len(code), blank


def func_names(p: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]


def entry_points(p: pathlib.Path) -> bool:
    src = p.read_text(encoding="utf-8", errors="replace")
    return '__name__ == "__main__"' in src or "__name__ == '__main__'" in src


def build() -> str:
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    out: list[str] = []
    w = out.append

    qi = py_files(QI)
    sc = py_files(SCRIPTS)
    qi_names = {p.stem for p in qi}
    sc_names = {p.stem for p in sc}

    # ---------- 依赖 ----------
    edges: list[tuple[str, str]] = []
    indeg: collections.Counter = collections.Counter()
    for p in qi:
        for target in sorted(imports_of(p) & qi_names):
            if target != p.stem:
                edges.append((p.stem, target))
                indeg[target] += 1
    sc_edges = []          # scripts → qi
    sc_sc_edges = []       # scripts → scripts（真正的 scripts 内部依赖）
    for p in sc:
        for target in sorted(imports_of(p) & qi_names):
            sc_edges.append((p.stem, target))
            indeg[target] += 1
        for target in sorted(imports_of(p) & sc_names):
            if target != p.stem:
                sc_sc_edges.append((p.stem, target))
                indeg[target] += 1

    # ---------- 双向依赖 ----------
    g: dict[str, set[str]] = collections.defaultdict(set)
    for a, b in edges + sc_edges:
        g[a].add(b)

    def cycle(a: str, b: str) -> bool:
        return a in g.get(b, ()) and b in g.get(a, ())

    mutual = sorted({tuple(sorted(e)) for e in edges + sc_edges if cycle(*e)})

    # ---------- 规模 ----------
    sizes = []
    for area, pat in AREAS:
        files = sorted(pathlib.Path(area).glob(pat))
        total = code = blank = 0
        for p in files:
            a, b, c = loc(p)
            total += a
            code += b
            blank += c
        sizes.append((area, len(files), total, code, blank))

    tests = sorted(pathlib.Path("tests").glob("test_*.py"))
    cases = sum(p.read_text(encoding="utf-8", errors="replace").count("def test_")
                for p in tests)

    # ---------- 测试覆盖（静态猜测） ----------
    test_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests)
    uncovered = [p.stem for p in qi
                 if f"qi.{p.stem}" not in test_text
                 and f"import {p.stem}" not in test_text
                 and f"from qi import {p.stem}" not in test_text
                 and f'"{p.stem}"' not in test_text]

    # ---------- 重复度 ----------
    dup_pairs = [("dsh_choose", "dsh_use"), ("codex_choose", "codex_use"),
                 ("opencode_choose", "opencode_use"), ("grok_choose", "grok_use"),
                 ("gemini_choose", "gemini_use"), ("deepseek_choose", "deepseek_use")]
    dups = []
    for a, b in dup_pairs:
        pa, pb = SCRIPTS / f"{a}.py", SCRIPTS / f"{b}.py"
        if not (pa.exists() and pb.exists()):
            continue
        sa = pa.read_text(encoding="utf-8", errors="replace")
        sb = pb.read_text(encoding="utf-8", errors="replace")
        ratio = difflib.SequenceMatcher(None, sa, sb).quick_ratio()
        fa, fb = set(func_names(pa)), set(func_names(pb))
        dups.append((a, b, ratio, len(fa & fb), len(fa - fb), len(fb - fa)))

    # ---------- 输出 ----------
    w("# Bridge v0.0.9 — 代码关系统计")
    w("")
    w(f"> 由 `tools/arch_stats.py` 自动生成，**只读**（不导入、不执行、不碰 UI）。生成时间：{now}")
    w("> 重新生成：`env -u PYTHONPATH py -3.10 -X utf8 -B tools/arch_stats.py`")
    w("")
    w("## 0. 口径")
    w("")
    w("| 指标 | 口径 | 已知偏差 |")
    w("|---|---|---|")
    w("| 代码行 | `tokenize` 实际 token 行，剔除空行、纯注释行、独占行的字符串 | 多行坐标表/提示词被算作字符串 → **低估** |")
    w("| 依赖边 | AST `Import`/`ImportFrom`，只保留仓库内部名字 | 同名跨包按名字匹配 → 边数偏**高** |")
    w("| 相似度 | `difflib.quick_ratio()` | 上界近似，不是真实重复率 |")
    w("| 测试覆盖 | 测试源码里是否出现模块名 | **静态猜测**，不代表执行到 |")
    w("")
    w("## 1. 规模")
    w("")
    w("| 目录 | 文件数 | 总行 | 代码行 | 空行 |")
    w("|---|---:|---:|---:|---:|")
    for area, n, total, code, blank in sizes:
        w(f"| `{area}` | {n} | {total} | {code} | {blank} |")
    w("")
    w(f"- 测试：**{len(tests)}** 个 `test_*.py`，**{cases}** 个用例。")
    w(f"- 模块：`src/qi` **{len(qi)}** 个、`src/scripts` **{len(sc)}** 个。")
    w(f"- 依赖边：`qi` 内部 **{len(edges)}** 条；`scripts` → `qi` **{len(sc_edges)}** 条；"
      f"`scripts` → `scripts` **{len(sc_sc_edges)}** 条。")
    w("")
    w("## 2. 分层")
    w("")
    w("```text")
    w("Chrome 扩展 (src/chrome-extension/*.js)")
    w("        │ 注入 / 侧栏 → 由 qi.browser 准备可操作窗口")
    w("        ▼")
    w("qi.server  ── /ask 校验、预算、quorum、证据落盘、子进程调度")
    w("        │ 每个 endpoint 依次跑")
    w("        ▼")
    w("src/scripts/*_choose.py   → 只选档（写 tier_guard 回执）")
    w("src/scripts/*_use.py      → 选档 + 提问 + 收回答（发送前读回执）")
    w("        │ 共用")
    w("        ▼")
    w("qi/{uilock → 整轮前台锁} → qi/{screen,ui,window,browser} 定位与输入")
    w("qi/{conversation,flow,desk_flow} 会话与回复归属")
    w("qi/{tier_guard,sendgate,chip} 档位闸门    qi/{guard,human,zoom} 用户占用与人机验证")
    w("        │ 读写")
    w("        ▼")
    w("docs/tiers.json 档位表 · docs/bridge_server.json 服务配置 · runs/*.jsonl 运行日志")
    w("```")
    w("")
    w("## 3. 模块清单（`src/qi`）")
    w("")
    w("按**全仓入度**（被多少模块 import，含 `src/scripts` 的引用）降序；入度 0 = 叶子或入口。")
    w("")
    w("| 模块 | 入度 | 职责（docstring 首行） |")
    w("|---|---:|---|")
    for p in sorted(qi, key=lambda x: (-indeg[x.stem], x.stem)):
        w(f"| `{p.stem}` | {indeg[p.stem]} | {first_doc(p)} |")
    w("")
    w("### 3.1 枢纽（全仓入度 ≥ 10）")
    w("")
    hubs = [(m, c) for m, c in indeg.most_common() if c >= 10 and m in qi_names]
    w("| 模块 | 入度 | 说明 |")
    w("|---|---:|---|")
    for m, c in hubs:
        w(f"| `{m}` | {c} | {first_doc(QI / (m + '.py'))} |")
    w("")
    w("## 4. 模块清单（`src/scripts`）")
    w("")
    w("| 模块 | 入口 | 入度 | 职责 |")
    w("|---|---|---:|---|")
    for p in sorted(sc, key=lambda x: x.stem):
        ep = "是" if entry_points(p) else "否"
        w(f"| `{p.stem}` | {ep} | {indeg[p.stem]} | {first_doc(p)} |")
    w("")
    w("## 5. 依赖边")
    w("")
    w("### 5.1 `src/qi` 内部（按被依赖方分组）")
    w("")
    by_target: dict[str, list[str]] = collections.defaultdict(list)
    for a, b in edges:
        by_target[b].append(a)
    for target in sorted(by_target, key=lambda t: (-len(by_target[t]), t)):
        users = ", ".join(f"`{u}`" for u in sorted(by_target[target]))
        w(f"- **`{target}`** ← {users}")
    w("")
    if sc_edges:
        w("### 5.2 `src/scripts` → `src/qi`（每个 CLI 依赖了哪些内核模块）")
        w("")
        by_script: dict[str, list[str]] = collections.defaultdict(list)
        for a, b in sc_edges:
            by_script[a].append(b)
        for a in sorted(by_script):
            w(f"- **`{a}`** → " + ", ".join(f"`{x}`" for x in sorted(by_script[a])))
        w("")
    if sc_sc_edges:
        w("### 5.3 `src/scripts` 内部")
        w("")
        for a, b in sc_sc_edges:
            w(f"- `{a}` → `{b}`")
        w("")
    w("")
    w("### 5.3 双向依赖（互相 import）")
    w("")
    if mutual:
        for a, b in mutual:
            w(f"- `{a}` ⇄ `{b}`")
    else:
        w("- 无")
    w("")
    w("> 双向 import 在 Python 里通常靠函数内 import 或延迟求值规避，不一定致命；")
    w("> 但它意味着两个模块的职责边界有重叠，是后续拆分的候选点。")
    w("")
    w("## 6. 重复代码（choose / use 成对）")
    w("")
    w("| 对 | quick_ratio | 同名函数 | 仅前者 | 仅后者 |")
    w("|---|---:|---:|---:|---:|")
    for a, b, ratio, same, oa, ob in dups:
        w(f"| `{a}` / `{b}` | {ratio:.2f} | {same} | {oa} | {ob} |")
    w("")
    w("> `quick_ratio` 是上界近似；0.9 以上基本可以确认是**整份复制后改**。")
    w("")
    w("## 7. 测试覆盖（静态猜测）")
    w("")
    w(f"`src/qi` 中被测试源码**完全没提到**的模块（{len(uncovered)} 个）：")
    w("")
    if uncovered:
        w("```text")
        for m in uncovered:
            w(m)
        w("```")
    else:
        w("（无）")
    w("")
    w("> 判据只是「测试文件里出现过这个名字」，**不代表真的覆盖**；反过来，名字没出现")
    w("> 也不等于没被间接测到（可能通过上层模块进入）。")
    w("")
    w("## 8. 入口点")
    w("")
    eps = [p.stem for p in sc if entry_points(p)]
    w(f"`src/scripts` 里带 `__main__` 的入口（{len(eps)} 个）：")
    w("")
    w("```text")
    w(" ".join(eps))
    w("```")
    w("")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--out", default="docs/ARCH-STATS.md")
    args = ap.parse_args(argv)
    text = build()
    target = pathlib.Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"written {target} ({len(text)} chars, {len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
