# Round 0 修复计划（已过计划闸门）

批准：Herhons 2026-11-12 09:42
计划落盘：2026-11-12 10:05（先落盘，后动手）
依据：`FINAL.md` 的三份意见 + Herhons 的五条指定修复

> 边界：本计划**不改** `docs/self_iteration_spec.md`；只改实现与测试。
> 若某项发现必须改合同条款（阶段、闸门、停机条件、四审集合、匿名规则、授权边界），
> 立即停止并交 Herhons。

---

## 0. 总览

| # | 项 | 目标文件 | 验收 |
|---|---|---|---|
| 1 | DeepSeek 必须真读一次 | `src/qi/upload.py`、`tools/iterate.py`、`docs/iteration.json` | `iterate.py review --site deepseek` 产出一条回复，`verify` 判 `true_read` |
| 2 | 引用核对口径 | `src/qi/iteration.py`、`tools/iterate.py`、`tests/test_quote_verify.py`(新) | 单测锁死：按条计分母、章节号不算命中、重复去重 |
| 3 | UI 锁范围与残留清理 | `src/qi/uilock.py`、`tools/iterate.py`、`tests/test_b27_uilock_scope.py`(新) | 死进程残留锁被清；离线命令不持锁 |
| 4 | 崩溃续跑防重发 | `src/qi/iteration.py`(schema/身份)、`tools/iterate.py`、`tests/test_b27_resume.py`(新) | 每段有固定 id；`submitting/unknown` 只对账；杀进程后续跑不重发 |
| 5 | Gemini 汇总新对话 + 会话 ID | `tools/iterate.py` | 归档里有 `session_id`；单测断言先新开会话再发 |

顺序：2 → 3 → 4 → 5 → 1（前四项可离线验完；第 1 项要真机，放最后集中跑）。
每项独立提交 + 推送 + 写 `changes.md`。全部完成后写 `STOP_20261112_*.md` 停下。

---

## 1. DeepSeek 真读

**现状**：附件按钮是纯图标（UIA 里无名），`ATTACH["deepseek"]` 匹配不到 → `attach_not_found`；
分段粘贴 9 段后回 `可访问=否`（它不认"手上有部分文本"）。

**做法**（按优先级，成功即止）：
1. 探针枚举 deepseek 页面**全部** `ButtonControl`（含无名）与 `DocumentControl`，打印矩形；
   找出输入框左侧第一个 40×40 左右的图标位，按坐标点一次，观察是否弹 `#32770` 文件框。
2. 若弹框：把该坐标写进 `upload.py` 的一个新入口 `click_by_point`，规格改为
   `{"button": (), "point": (dx, dy), "anchor": "composer"}`——**锚点用输入框矩形算**，
   不写死屏幕坐标（副屏坐标会变）。
3. 若弹出的是菜单：采菜单项的 UIA 名字，按现有 `menu` 机制接。
4. 若两条都不行：改粘贴路径——先测"把规格压成单条 ≤N 字（去掉代码块围栏、保留章节号）"
   能否被它接受；能接受则只用于 deepseek，并在材料里注明"这是节选+章节号"。
   **不允许**用"截断材料 + 假装完整"的方式骗真读。

**验收**：`review --site deepseek` 的 `stage=answered`；`replies/deepseek.md` 非空；
`verify` 里 deepseek 的 `verdict` 至少 `partial`（真读更好，取决于它是否逐字引）。

**风险**：deepseek 会重发同一提问 → 先用 `submission_audit` 确认账本状态，再决定是否清。

---

## 2. 引用核对口径（规格 §9.1 未定义分母，属补空白）

**改动**（`src/qi/iteration.py::verify_reply`）：
1. **先切条**：把回复按「结构化意见」切分——序号开头（`1.` / `一、` / `- ` 且带章节号）、
   或空行分隔的段落，且段内至少含一个章节号或路径。
2. **命中按条计**：`total = 意见条数`；某条内**至少一处**有效命中即该条命中。
3. **章节号不算命中**：只写「§5.3」但没有逐字引句 → 该条不计命中；
   章节号只作为"这条在说什么"的线索。
4. **重复去重**：同一引句（空白归一后）在多条里重复出现，只算一次；
   若某条只有重复引句，该条不计命中。
5. **保留按条明细**：`rows` 改成 `{"index": n, "section": "5.3", "hit": bool, "why": ...}`，
   同时保留 `quotes` 子明细便于人工复核。
6. 阈值不变：`true_read = 命中条数 ≥ 3 且 命中条数/总条数 ≥ 0.7 且至少一条落在规格`。

**测试**（`tests/test_quote_verify.py`，全离线）：
- 6 条意见、5 条带有效引句 → `5/6=0.83` → true_read；
- 3 条意见全中 → 命中 3 但分母 3 → 1.0，仍判 true_read（**除非**总条数 < 5 时按 §7.2 记 `too_few`，只降级为 partial）；
- 一条只有「§5.3」无引句 → 不计命中；
- 同一引句出现 3 次 → 只算 1 条命中；
- 引句尾部截断（`「…docs/self`）→ 不计命中（维持现状口径，不改）；
- 全空 / 只有路径无引句 → `unread`。

---

## 3. UI 锁

**改动**：
1. `uilock.clear_stale(root=None) -> dict`：读 `runs/ui.lock` 元数据，`_holder_dead(meta)` 为真则
   把元数据改写成 `{"released_at":..., "released_by":"stale", "stale_pid":<pid>}` 并返回 `cleared=True`；
   pid 活着返回 `cleared=False, reason="alive"`；无文件返回 `cleared=False, reason="absent"`。
2. `hold()` 的等锁循环里：忙时读 meta，若 `_holder_dead` 为真 → 先 `clear_stale`（清元数据）再立即重试，
   不再空等到 timeout。
3. `tools/iterate.py`：
   - 新增 `_ui_guard(owner)` 上下文管理器 = `uilock.hold(owner)`；
   - **只在** `_ask_upload` / `_ask_upload_script` / `_ask_via_script` / `cmd_summarize` 的
     发送与收回复段使用；
   - `verify` / `anonymize` / `snapshot` 全程不取锁；
   - 每个 UI 命令开始前调用 `uilock.clear_stale()` 并把结果记进 `state.json` 的 `lock` 键
     （`{"stale_cleared": bool, "at": ...}`）。
4. 等审批：停机路径不持锁（本来就无锁），补一条单测断言 `cmd_verify` 不创建/不持有 `ui.lock`。

**测试**（`tests/test_b27_uilock_scope.py`）：
- 伪造 meta（pid 明显不存在，如 999999）→ `clear_stale` 返回 cleared=True，文件元数据变 stale；
- 伪造存活 pid（当前进程）→ cleared=False, reason=alive；
- `hold` 遇到死进程 meta → 在 <1s 内拿到锁（不死等 timeout）；
- `cmd_verify` 跑完后 `runs/ui.lock` 的 `updated_at` 不变（没被取用）。

---

## 4. 崩溃续跑防重发

**改动**：
1. **固定身份**：`iteration.py` 新增
   `chunk_identity(site, index, text) -> "chunk:<sha16>"` 与
   `prompt_identity(site, round, prompt) -> "prompt:<sha16>"`；
   `pack_chunks` 返回 `(text, id)` 列表（保留旧签名兼容：新增 `pack_chunks_with_ids`）。
2. **状态文件 schema**：`state.json` 增加 `schema: "round-state/2"`，`materials` 记录
   `{"index": n, "id": "chunk:...", "chars": n, "status": "sent|pending"}`；
   加载时若 `schema` 缺失 → 视为 v1，只补字段不改历史。
3. **账本优先**：发送前查 `submissions.read_state(endpoint, prompt)`：
   - `accepted` → 不重发，走只读查答（已有 `already_answered` 逻辑；没有答案则返回 `needs_reconcile`，不自动清账本）；
   - `submitting` / `unknown` → **只对账不重发**：先只读查答，答不出就返回
     `needs_manual_clear`（带文件名与 `submission_clear` 命令提示），**不自动清、不重发**；
   - `failed_retryable` / 无记录 → 正常发送。
4. 每段发送成功后立刻 `state.mark(f"chunk:{id}", {...})`；崩溃后重跑时跳过已 `sent` 的段。

**测试**（`tests/test_b27_resume.py`）：
- 同一材料两次 `pack_chunks_with_ids` → id 稳定；
- 改一个字 → id 变；
- 伪造账本 `submitting` + 页面无答案 → `review` 返回 `needs_manual_clear`，且**没有**调用发送（用桩断言）；
- 中断后续跑：state 里 chunk 1/2 已 sent → 只发 3/4/5；
- `state.json` 写入后含 `schema` 与 `materials`。

---

## 5. Gemini 汇总新对话 + 会话 ID

**改动**（`tools/iterate.py::cmd_summarize`）：
1. 发送前 `start_new_chat(screen, "gemini")`，失败则返回 `new_chat_failed`（**不发**）；
2. 发送并收卷后，从 `ui.document(...)` 取 URL，`session_id_of(url)` 存进
   `gemini_summary.json`（`{"session_id":..., "url":..., "chars":..., "at":...}`）；
3. `state.json` 的 `summarize` 也记 `session_id`；
4. 无 `session_id`（页面没换会话）→ 判 `summary_not_isolated`，归档仍写但标红。

**测试**：
- 桩：`start_new_chat` 返回 False → 不调用 ask；
- 桩：返回 URL 带 id → 归档 JSON 里有该 id；
- 桩：URL 无 id → `summary_not_isolated`。

---

## 6. 收尾

- 全部完成后跑 `unittest discover -s tests`（全量）+ 逐项定向用例；
- 写 `STOP_20261112_*.md`：做了什么、真机证据、还剩什么；
- 推送公开分支；**不开下一轮、不改规格**；停下等 Herhons。
