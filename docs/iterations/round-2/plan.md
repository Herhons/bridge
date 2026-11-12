# Round 2 施工计划

| ID | 项 | 落点 | 验收 |
|---|---|---|---|
| P1 | S1 补顶层窗口与布局漂移校验 | `src/qi/safe_ui.py` | 用例：同进程弹窗被拒（other_top_window）、布局变化被拒（layout_moved） |
| P2 | 会话锚点接进发送路径 | `tools/iterate.py`：`_session_plan` / `_remember_anchor` | `session_lost` 时不发；锚点写入 `state.anchors` |
| P3 | S2 引文豁免 | `tools/iterate.py::_drop_prompt_echo` | 引文块（含「」/§/文件行）不被删 |
| P4 | 推进器核实闸门 + 三振 | `tools/sisyphus.py` | 未核实拒跑（4）；3 次尝试拒跑（5） |

不做（Round 3）：四阶段账本、legacy_adapter、剪贴板事务锁、孤儿锁挂到 sisyphus 入口。
