"""WorkBuddy：--list 读当前模型；--model 完整模型名 切换并复核。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qi import console, uilock  # noqa: E402 - 入口编码（CARD-08 第 1 条）
from qi.workbuddy import main  # noqa: E402

if __name__ == "__main__":
    console.setup()
    raise SystemExit(uilock.entry("workbuddy_choose", main))
