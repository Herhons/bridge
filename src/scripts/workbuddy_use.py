"""WorkBuddy：默认新任务；--continue 继续当前任务，提交一次并读取对应回复。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qi import console, uilock  # noqa: E402 - 入口编码（CARD-08 第 1 条）
from qi.workbuddy import main  # noqa: E402

if __name__ == "__main__":
    console.setup()
    # UI 前台锁（CARD-09a 第 6 条）：和服务端抢同一把 runs\ui.lock
    raise SystemExit(uilock.entry("workbuddy_use", lambda: main(sending=True)))
