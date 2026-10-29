"""WorkBuddy：--list 读当前模型；--model 完整模型名 切换并复核。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qi.workbuddy import main

if __name__ == "__main__":
    raise SystemExit(main())
