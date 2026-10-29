"""从 opencode_choose.py 生成 opencode_use.py（多一个发消息入口）。

两个脚本都要求单文件自包含，所以是复制而不是 import。
改定位逻辑时改 opencode_choose.py，然后跑这个脚本同步。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "src", "scripts")
CHOOSE = os.path.join(SCRIPTS, "opencode_choose.py")
USE = os.path.join(SCRIPTS, "opencode_use.py")

HEAD = '''"""opencode（OpenCode 桌面端 / OpenCode.exe）：选模型 + 调强度 + 发消息。

**单文件，只依赖 cv2 / numpy / ctypes，不 import 本项目的任何模块。**
（定位代码和 opencode_choose.py 同一套，这个脚本多一个发消息入口。）

用法：
    python opencode_use.py "只回复两个字：收到"
    python opencode_use.py --list                       读当前模型和强度
    python opencode_use.py --model "Claude Opus 5.5"    只切模型
    python opencode_use.py --effort max                 只调强度
    python opencode_use.py --model "Gemini 4 Argon" --effort low "问题"
    python opencode_use.py --probe                      只报模板命中，不点击

**发消息只到"确认发出去"为止**：取回答还缺复制按钮模板（要 hover 才出现），
所以带文案调用会返回 stage=no_copy_template / sent=true，不会假装读到了回答。
发送前会校验发送键是不是真的变成深色启用态（输入框空时它是浅灰禁用态）。

界面结构、定位方式、已知限制都写在 opencode_choose.py 文件头里。
"""'''


def main():
    src = open(CHOOSE, encoding="utf-8").read()

    end = src.index('"""', src.index('"""') + 3) + 3
    src = src[:src.index('"""')] + HEAD + src[end:]

    # CLI：加位置参数 prompt / --timeout
    src = src.replace(
        '    ap.add_argument("--list", "-l", action="store_true")\n',
        '    ap.add_argument("prompt", nargs="?", default=None)\n'
        '    ap.add_argument("--list", "-l", action="store_true")\n')
    src = src.replace(
        '    ap.add_argument("--probe", action="store_true")\n',
        '    ap.add_argument("--probe", action="store_true")\n'
        '    ap.add_argument("--timeout", type=float, default=180.0)\n')
    src = src.replace(
        "    if not (a.list or a.model or a.effort or a.probe):",
        "    if not (a.list or a.model or a.effort or a.probe or a.prompt):")

    # 主流程：在 effort 之后加 prompt 分支
    src = src.replace('''        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1''','''        if a.effort:
            r = set_effort(d, win, a.effort)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1
        if a.prompt:
            r = ask(d, win, a.prompt, a.timeout)
            print(json.dumps(r, ensure_ascii=False))
            rc = 0 if r.get("ok") else 1''')

    open(USE, "w", encoding="utf-8").write(src)
    print("wrote", USE, src.count("\n"), "lines")


if __name__ == "__main__":
    main()
