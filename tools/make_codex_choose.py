"""从 codex_use.py 生成 codex_choose.py（同一套定位代码，去掉发消息部分）。

两个脚本都是**单文件自包含**，所以 codex_choose.py 是复制而不是 import。
改定位逻辑时改 codex_use.py，然后跑这个脚本同步。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "src", "scripts")
USE = os.path.join(SCRIPTS, "codex_use.py")
CHOOSE = os.path.join(SCRIPTS, "codex_choose.py")

HEAD = '''"""codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 读/调强度。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**
（和 codex_use.py 是同一套定位代码，区别只是这个脚本不发消息、不取回答。）

用法：
    python codex_choose.py --list                       读当前模型和强度
    python codex_choose.py --model gpt6_sol             只切模型
    python codex_choose.py --effort high                只调强度
    python codex_choose.py --model gpt6_sol --effort ultra   两个都调
    python codex_choose.py --probe                      只报每个模板命中在哪，不点击

强度六档（2026-10-29 逐档实测，滑杆上那 5 个小灰点就是其余 5 档）：
    light / medium / high / xhigh(Extra High) / max / ultra
    low、mid 是 light、medium 的旧别名。

模型（列表一屏只能露 7 项，最后两项露不出来）：
    default / gpt61_sol / gpt6_astra / gpt61_terra / gpt6_sol / gpt6_luna

**注意 Default（自动）模型**：那种卡片里没有独立强度滑杆（只有一条
「模型+强度」预设条），所以 --effort 在 Default 上会明确拒绝，
要先用 --model 选一个具体模型。

定位方法、界面结构、已知限制都写在 codex_use.py 文件头里，这里不再重复。
"""'''


def main():
    src = open(USE, encoding="utf-8").read()

    end = src.index('"""', src.index('"""') + 3) + 3
    src = src[:src.index('"""')] + HEAD + src[end:]

    a = src.index("# ============================================================ 提问")
    b = src.index("# ============================================================ CLI")
    src = src[:a] + src[b:]

    src = src.replace('    ap.add_argument("prompt", nargs="?", default=None)\n', "")
    src = src.replace('    ap.add_argument("--timeout", type=float, default=180.0)\n', "")
    src = src.replace(
        "if not a.prompt and not a.list and not a.model and not a.effort and not a.probe:",
        "if not a.list and not a.model and not a.effort and not a.probe:")

    # 去掉调用 ask() 的三行（它上面的 if a.prompt: 一起删）
    lines = src.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() == "if a.prompt:":
            i += 4                      # if + 三行体
            continue
        out.append(lines[i])
        i += 1
    src = "".join(out)

    open(CHOOSE, "w", encoding="utf-8").write(src)
    print("wrote", CHOOSE, src.count("\n"), "lines")


if __name__ == "__main__":
    main()
