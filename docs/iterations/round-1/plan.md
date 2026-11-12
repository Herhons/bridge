# Round 1 施工计划（核实通过即施工，§10）

依据 `verification.md`。只做 S1–S4，全部可离线测；架构大改只落骨架。

| ID | 项 | 落点 | 验收（离线） |
|---|---|---|---|
| S1 | 安全点击/按键原语 | 新增 `src/qi/safe_ui.py`：黑名单类名（Progman/WorkerW/Shell_TrayWnd）、`WindowFromPoint` 归属校验、前台+焦点角色双检；`upload._raw_click` 走它 | `tests/test_b28_safe_ui.py`：桌面点被拒、他窗点被拒、目标窗内放行、危险键在前台不符时拒绝 |
| S2 | 收卷剔除尾部 prompt 回声 | `tools/iterate.py::read_answer_text` 末尾过滤与提问重合的块 | `tests/test_b28_echo.py`：尾部 3 段回声被剔除，正文不动 |
| S3 | 会话锚点 | 新增 `src/qi/session_anchor.py`：记 `round/site/url/prompt_hash`，`ensure(...)` 漂移时回跳，失败且已 accepted → `SESSION_LOST`（不新建） | `tests/test_b28_anchor.py`：URL 匹配放行、漂移回跳、已 accepted 时不新建 |
| S4 | 总控骨架 + 驱动协议 | 新增 `src/qi/drivers/__init__.py`（`SiteDriver` 协议）与 `tools/sisyphus.py`（读 plan.md 条目、按 ID 断点续跑，只调度不决策） | `tests/test_b28_sisyphus.py`：计划解析、断点续跑、硬停拦截 |

不做（留下一轮）：四阶段账本（staged/dispatched/accepted/completed）、剪贴板事务锁、四端实现迁移。
理由：都会动到发送栈核心，需独立一轮 + 全量回归。

**硬停检查**：S1–S4 不花钱、不登录、不删数据、不碰 v0.0.8。
