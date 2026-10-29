"""dsh（DeepSeek Harness 桌面端）：提问 → 收回答。

用法：
    python dsh_use.py "只回复两个字：收到"

流程：找窗口（按进程名）→ 开新对话（有的话）→ 点输入框 → 全选清除 →
输入 → 点右下角发送键 → 等复制按钮出现 → 点最后一个复制 → 读剪贴板。

窗口会被强制摆到副屏 1380x1340：所有偏移都是这个尺寸下量的（和网页端同一个道理）。
找不到发送键就明确报 no_send_button 并退出，不猜位置、不硬点。

注：Electron 应用，窗口标题会变成会话名（`… — DeepSeek Harness`），同样按进程名找
"""

import os
import sys

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qi.desk_flow import cli            # noqa: E402
from qi.desk_sites import SITES         # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cli(SITES["dsh"]))
