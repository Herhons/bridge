"""WorkBuddy：默认新任务；--continue 继续当前任务，提交一次并读取对应回复。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qi.workbuddy import main

if __name__ == "__main__":
    raise SystemExit(main(sending=True))
