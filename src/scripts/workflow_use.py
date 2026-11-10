"""工作流状态机命令行（任务卡 05，第 5 项后端部分）。

用法（--file 必填，没有默认值，免得手滑写到真 progress.json）：

    python src\\scripts\\workflow_use.py status        --file progress.json
    python src\\scripts\\workflow_use.py move 05 done  --file progress.json
    python src\\scripts\\workflow_use.py move 05 hold  --file progress.json --missing 截图 --missing 退出码
    python src\\scripts\\workflow_use.py advance 05    --file progress.json
    python src\\scripts\\workflow_use.py resolve-hold 05 --file progress.json

--workflow 可选，默认按 qi/workflow.py 往上推到项目根再拼 docs\\workflow\\workflow.json。

退出码：
    0  正常
    2  被规则拒绝（非法跳转、hold 没带清单、证据没补齐就出 hold、卡不在 hold）
    3  advance 后需要监工（同时多打一行 ``需要监工: <卡号>``）
    4  其他错误（文件不存在、JSON 坏、卡号不存在、状态名不存在、参数不对）

只读写 --file 指到的那一个文件；不开窗口、不发消息、不调用任何端的 CLI。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qi import console, workflow  # noqa: E402

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_NEED_SUPERVISOR = 3
EXIT_ERROR = 4


class UsageError(Exception):
    """参数写错：交给 main 统一打成退出码 4，别用 argparse 默认的 2。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):        # noqa: D401 - argparse 的钩子
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--file", default=argparse.SUPPRESS,
                        help="progress 文件路径（必填，测试用临时副本）")
    common.add_argument("--workflow", default=argparse.SUPPRESS,
                        help="workflow.json 路径（可选，默认 docs\\workflow\\workflow.json）")

    parser = _Parser(prog="workflow_use.py", parents=[common],
                     description="Bridge 工作流状态机（只读写 --file 指定的文件）")
    subs = parser.add_subparsers(dest="command", metavar="{status,move,advance,resolve-hold}")
    subs.required = True

    subs.add_parser("status", parents=[common], help="打印每张卡及状态")
    move_parser = subs.add_parser("move", parents=[common], help="按跳转表改一张卡的状态")
    move_parser.add_argument("card", help="卡号，如 05")
    move_parser.add_argument("state", help="目标状态，如 done")
    move_parser.add_argument("--missing", action="append", metavar="项",
                             help="进 hold 时缺的证据，可重复")
    advance_parser = subs.add_parser("advance", parents=[common], help="监工推一次")
    advance_parser.add_argument("card", help="卡号，如 05")
    hold_parser = subs.add_parser("resolve-hold", parents=[common], help="补齐证据后回 verifying")
    hold_parser.add_argument("card", help="卡号，如 05")
    return parser


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("(没有卡)")
        return
    for row in rows:
        line = f"{row['card']} {row['state']}"
        if row.get("parent"):
            line += f" parent={row['parent']}"
        if row.get("missing"):
            line += f" missing={len(row['missing'])}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return EXIT_ERROR

    file_arg = getattr(args, "file", None)
    if not file_arg:
        print("参数错误: 缺少 --file <progress 路径>（没有默认值）", file=sys.stderr)
        return EXIT_ERROR
    workflow_arg = getattr(args, "workflow", None)

    try:
        if args.command == "status":
            _print_rows(workflow.summary(progress_path=file_arg))
            return EXIT_OK

        if args.command == "move":
            result = workflow.move(args.card, args.state, missing=args.missing,
                                   progress_path=file_arg, workflow_path=workflow_arg)
            print(f"{result['card']} {result['from']} -> {result['to']}")
            if result.get("repair_card"):
                print(f"修复卡: {result['repair_card']} (parent={result['card']})")
            return EXIT_OK

        if args.command == "advance":
            result = workflow.advance(args.card, progress_path=file_arg,
                                      workflow_path=workflow_arg)
            if result["need_supervisor"]:
                print(f"需要监工: {str(args.card).strip()}")
                return EXIT_NEED_SUPERVISOR
            return EXIT_OK

        if args.command == "resolve-hold":
            result = workflow.resolve_hold(args.card, progress_path=file_arg,
                                           workflow_path=workflow_arg)
            print(f"{result['card']} {result['from']} -> {result['to']}")
            return EXIT_OK
    except workflow.MoveRejected as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REJECTED
    except workflow.WorkflowError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"错误: 读写失败 ({exc})", file=sys.stderr)
        return EXIT_ERROR

    print(f"参数错误: 未知子命令 {args.command}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    console.setup()          # 入口编码：GBK 控制台不再被 \u200b 打崩（CARD-08 第 1 条）
    raise SystemExit(main())
