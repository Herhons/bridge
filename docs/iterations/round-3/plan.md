# Round 3 施工计划

依据 `verification.md`。全部离线可测。

| ID | 项 | 落点 | 验收 |
|---|---|---|---|
| P1 | accepted→unknown 一次性降级 | `src/qi/submissions.py::demote` + `tools/submission_demote.py` | `test_b30_submission_demote.py`（7 条：证据门槛、只降一次、仍拦截、留审计） |
| P2 | 剪贴板写后核对 + 按归属恢复 | `src/qi/conversation.py` | `test_b30_clipboard_verify.py`（6 条） |
| P3 | 孤儿锁挂到推进器入口 | `tools/sisyphus.py` | `test_b28_sisyphus.py` 增 1 条；清理失败不阻断 |
| P4 | 驱动边界 AST 检查 | `tests/test_b30_driver_boundary.py` | 禁止 import 发送栈/账本/锁；禁 ask/reserve/pending |
| P5 | 提问碎片认领（收卷存在性） | `src/qi/conversation.py::prompt_fragments` | `test_b30_prompt_fragments.py`（4 条） |
| P6 | 匿名包去来源 + 泄漏自检 | `src/qi/iteration.py` | `test_b30_anonymity.py`（4 条） |
| P7 | 上传前校验文件存在 | `src/qi/upload.py` | `test_b29_upload_path.py` 增 1 条 |

不做（留下一轮）：R3-2 写账所有权（reservation_id）、R3-5 收卷主路径统一、R3-6 驱动实现迁移。

**硬停检查**：本轮不花钱、不登录、不删数据、不碰 `F:/Bridge/v0.0.8`。
