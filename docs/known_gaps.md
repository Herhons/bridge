# 已声明的未完成项（Known Gaps）

本文件只登记**已明确决定不做**的项，不是"待办清单"。
每一项都写清：谁决定的、为什么、影响是什么、验收时怎么算。

更新：2026-11-07

---

## T1-2 真人输入让位（事件级归属）

| 项 | 内容 |
|---|---|
| **状态** | `NOT_IMPLEMENTED`（**不是** `pending` —— 这是"按决策不做"，不是"还没轮到"） |
| **决策人 / 日期** | 用户，2026-11-07 |
| **施工单条款** | 阶段 1 · T1-2：「消除脚本动作结束 → 采样合成标记之间的时间窗盲区……需在动作前锁定自身事件关联标记并持续覆盖至采样后」 |
| **为什么不做** | 真正做到事件级归属，只剩一条路：装**全局低级键盘/鼠标钩子**（`WH_KEYBOARD_LL` / `WH_MOUSE_LL`），并用 `SendInput` 的 `dwExtraInfo` 给自身事件打标记、在钩子里读 `GetMessageExtraInfo` 区分来源。用户不愿在用户机上引入全局钩子（可能被安全软件拦、需要处理超时静默摘钩、失败模式复杂）。 |
| **当前代码的现状** | `src/qi/guard.py` 仍使用 `GetLastInputInfo` 的 tick 判定 + `synthetic_tick()` 的 5 秒 TTL（`SYNTHETIC_TTL=5.0`）。`conversation.py:256-257` / `311-312` 在打字/粘贴后 `sleep(0.15)` 再取 tick。`_submit` 在点击前查一次，之后最多 4 轮、每轮 `sleep(0.5)` 重读树，**检查到点击之间最长约 2 秒没有再次检查**。 |
| **影响（如实说）** | ① 打字/粘贴结束后约 **150ms 盲区**：用户若恰好在此窗口内按键，会被吸收成"自己的" tick；② 检查后到点击前约 **2 秒空窗**：这段时间的真人输入不会被发现；③ 现有机制属于施工单**明确禁止**的形态（"仅靠 tick 递增 / 延长等待"）。 |
| **缓解（已保留，未删）** | `guard.py` 的全部现有代码**原样保留**，没有任何一行被删除或注释掉。`GetLastInputInfo` 检查仍然在起作用——它挡得住"用户在脚本动作之前就动了"这一类，挡不住上面两个窗口期。 |
| **验收方判定指引** | 本项**不参与 F1–F6 判分**（它是阶段 1 的独立条款，不是 F1–F6 的一部分）。但**不得据此宣称"真人输入让位已实现"**；任何"已实现"的表述都是错的。若验收方认为此项必须做，需要先由用户重新决策是否接受全局钩子。 |
| **如果要补做** | 落点很明确：① `screen.py` 的 `SendInput` 调用把 `dwExtraInfo` 从恒 0 改成一次性 token；② 起一个钩子线程读 `GetMessageExtraInfo`，与当前 token 比对；③ `guard.user_moved_since` 改为"有没有非自身事件"；④ 钩子要有超时自动摘除与降级路径。 |

---

## F3 多端真并发（锁粒度）

| 项 | 内容 |
|---|---|
| **状态** | `IMPLEMENTED`（2026-11-08 B13b/B14，离线 1282 条全绿；**真机**：2 席全成 / 3 席全成 / 4 席回收 3 席（`partial` 且 `quorum.met=true`），见 `evidence/batch14/`） |
| **改了什么** | ① `_run_job` 用 `ThreadPoolExecutor` 并发，`exclusive_endpoints` 分串行相；<br>② 入口 `uilock.run` → `uilock.entry`，锁下沉到 `uilock.section()`（发送段/轮询读段/复制段）；<br>③ 服务端在 `endpoint_concurrency>1` 时**不再整段持锁**，并下发 `BRIDGE_UI_SECTIONED/WAKE/CONCURRENT`；<br>④ 网页端**一端一窗**（`browser.STATE` 加 `by_site`），不再复用同窗切标签。 |
| **并发度** | **以 `docs/bridge_server.json` 为准**（文档不写死数字）：`endpoint_concurrency` / `min_success` / `endpoint_stagger` / `exclusive_endpoints`，改完必须重启后端。 |
| **已知取舍 ①** | 并发模式下 `BRIDGE_UI_WAKE=0`：读到 0 个文档**不再抢前台自愈**（那会 4 席互相把对方挤到后台形成乒乓）。代价是客户端休眠/最小化后需要人工点一下窗口。 |
| **已知取舍 ②** | 让位判定仍在 `_submit` 里（持锁期间别的席位打不了字，判据成立）；但**段与段之间**的抢鼠标不会立刻让位 —— 单段最长约 25s。 |
| **已知取舍 ③** | deepseek / grok 读屏幕像素，被抢前台会读错，所以它们在**串行相**单独跑，不与其他端时间重叠。 |
| **真机判据（可机读）** | pool 内各完成席 `[started_at, finished_at]` 的**公共交集 ≥ 2s**，且 `quorum.recovered ≥ min_success`；串行相（deepseek/grok）不计入。 |
| **真机实测** | 2 席交集 17.0s、墙钟 18s（各席之和 35s）；3 席交集 19–21s、墙钟 27–28s、并发收益 **2.5–2.6×**。两波 token（T4→T5）互不残留 ⇒ 无陈旧/串答。 |
| **仍未过的一席** | **codex**：4 席同开时它的 `choose` 会被别的席抢前台（`exit 4`）；加 `endpoint_stagger=6` 后 choose 能过，但 `use` 120s 内不出答复（`use_timeout`）。同一 prompt 单独跑 16–18s 是正常的，属它自己的慢/被遮挡，不是并发判据坏。 |
| **已知取舍 ④** | 并发时"前台"是共享资源：`Screen.focus` / `desk.activate` / `ui.click` / `press` / `hotkey` 在 `BRIDGE_UI_CONCURRENT=1` 下会先抢回前台再动作；**单跑不受影响**（用户抢走仍然中止）。`ShowWindow(SW_RESTORE)` 是必须的前置——前台是桌面时只调 `SetForegroundWindow` 一律失败。 |
| **已知取舍 ⑤** | 并发分支**忽略** `continue_session` / `tab`（`ensure_site` 只认自己那个端的窗口）；改文件后需重启后端。 |

| **B15 单窗多标签（2026-11-08 晚）** | `web_window_mode="tabs"`：四端共用一个 Chrome 窗、每端一个标签；**每次读文档前切回自己**（`ui.document` 里"切标签+读文档"是同一段锁）。`ensure_site` 选**标签最多**的窗（不是记忆里的 hwnd）。桌面四席补了统一的 `desk.ensure_foreground`（含 `SW_RESTORE`）、dsh 锁外预启动、codex 的 busy 停滞判据、按端预算覆盖。**实测**：chatgpt+gemini 双席全成；4 网页最好 2/4（grok 的 choose 模板失配、chatgpt 4 席竞争下失败）；4 桌面最好 3/4（dsh 的 choose 仍失败）。**未达到 4/4。** |
| **B15 仍未解决** | ① grok 的模型菜单模板失配（需重截模板）；② chatgpt 在 4 席同开时 `--new` 后被判"读不出模式"，需加"等新页可读"的闸；③ codex/opencode 在有 dsh 的那轮双双 `use_timeout`（D4 无 dsh 时三席全成），根因要 `text_len` 时间序列；④ dsh choose 仍 exit 1。 |


---

## 桌面端长任务：回答回传会断（`conversation_changed`）

| 项 | 内容 |
|---|---|
| **状态** | `KNOWN_LIMITATION`（有实测复现，**未修**） |
| **发现** | 2026-11-07，两次实测：① 04:39:11 `elapsed=167.4s`；② 13:12:52 `elapsed=594.6s`（本轮独立复核那次） |
| **现象** | `{"stage": "conversation_changed", "reason": "session_changed", "submission": "accepted", "sent": true}` —— **消息确实发出去了**，但收回答时判定"换了会话"，主动放弃收集。 |
| **根因** | 长任务进行中，opencode 会把**提问气泡整个从控件树里丢掉**（`conversation.py:921-922` 已有注释记录）。此时 `prompt_here=False`，代码走到 `conversation.py:931` 的 `elif accepted_stamp is not None and not _session_same(...)`，而新读到的页头 `proven=false`（`why=header_mismatch`，因为标签页枚举把消息正文也当成了标签）。**"读不出来"被当成了"变了"。** |
| **为什么不顺手修** | R6/R7/R9 三轮把"会话身份"收得很紧（没有与标题无关的会话 id 就永不交卷）。放松这一条会打开"认领到别人回答"的口子——那比收不到更糟。要修得先证明桌面端有稳定的会话标识，属于设计变更，不是改个条件。 |
| **影响（如实说）** | ① **短回答不受影响**（05:11 实测 5.0s、05:12 实测 25.9s 均正常）；② 长回答（数百秒级）在 opencode 桌面端**拿不回来**，只能从它自己写出的文件里取；③ F2/F3 用的是短 token 回显，不受此限。 |
| **验收方判定指引** | 若在 F2/F3 里遇到 `conversation_changed`，先看 `submission` 是否为 `accepted`：是则**消息已发出**，不是"没发出去"。 |

---

## 非 nonce 端：失败时不再关闭窗口（可能留孤儿窗口）

| 项 | 内容 |
|---|---|
| **状态** | `ACCEPTED_TRADEOFF` |
| **起因** | 独立复核 FINDINGS F2：旧代码在"等待期恰好多 1 个新窗口"（弱证据）被认领后，`place`/`focus` 失败会把它关掉。施工单 T1-1 只允许关**持有本次启动归属证据**的窗口。 |
| **改法** | `browser._spawn` 只有 `nonce` 取证通过（`proven=True`）才允许关；弱证据一律**保留**并打日志。取证也提到 `place`/`focus` 之前——失败时归属还没证。 |
| **代价** | `SPAWN_NONCE_SITES = {chatgpt, gemini, grok}` 之外的端（deepseek 等）没有 nonce，绑定失败时会**留一个孤儿窗口**在桌面上，需要人手动关。 |
| **为什么接受** | 宁可留一个我们自己的孤儿窗口，也绝不误关用户的窗口。误关是不可逆的用户损失；孤儿窗口是可见、可恢复的。 |

---

## 桌面端（`desk_flow`）未接提交账本

| 项 | 内容 |
|---|---|
| **状态** | `NOT_IMPLEMENTED` |
| **原因** | `src/qi/desk_flow.py:168` 的 `ask()` 本身**没有 purpose 概念**，因此拿不到用途，也就无法接入 T2-1 的提交账本。 |
| **覆盖情况** | 已接账本：gemini / grok（各自既有钩子）、chatgpt / deepseek（经 `flow.ask` 透传 `purpose`）、codex / dsh / opencode（2026-11-07 补齐透传）、**workbuddy（B26 起：`_last_gate` 里 reserve，并向 `conversation.ask` 回报 `reserved=True`，避免二次 reserve）**。**未接**：走 `desk_flow` 的桌面端——而 `desk_flow.ask/cli` 目前在 `src/` 里没有任何调用方（只有 `desk_sites.py` import 了它的 `DeskSite` 数据类）。 |
| **影响** | `desk_flow` 那条路没有"同一 (端,prompt) 只计一次"的护栏；但它当前是死路径。真要启用前必须先接账本。 |

---

## 端点拒绝这一轮：只有 2 个端有结构判据，其余 6 个没有

| 项 | 内容 |
|---|---|
| **状态** | `PARTIAL`（接线对 8 端统一，**开关按证据开**） |
| **做了什么** | 新增 `src/qi/reject.py`：三条**结构**判据（不猜原因）。<br>`submit_refused` 点了发送但文字被退回输入框、且没出现提问气泡；<br>`send_unavailable` 发送键存在但全部不可用（与"找不到发送键"分开）；<br>`accepted_silent` 端收下了但一直没有生成迹象与回复。 |
| **哪些端开了** | 只有 `opencode`（三条全开，实测证据 `evidence/opencode_audit2_20261107_1331/RUN.txt`、`F:\Bridge\v0.0.9\evidence\card06c_glm_quota_0840.txt`）与 `codex`（只开"发送键不可用"，证据 `docs/完成矩阵.md:15`）。 |
| **哪些端没开** | `deepseek` / `gemini` / `grok` / `chatgpt` / `dsh` / `workbuddy` —— **零实测结构证据，不猜**。它们的"端拒绝了这一轮"依然表现为 `timeout` 或 `answered`。 |
| **怎么补** | 拿到某端的实测现场（截图 + 当时的控件树）后，在 `reject.PROFILES` 里把对应布尔值打开即可，**不用改判据逻辑**。判据是纯函数，已按端隔离。 |
| **为什么不做错误文案表** | 8 个端里只有 codex / opencode 有实测原文；而"聊天正文里出现'额度'二字"是常态，做短语匹配必然误判。`human.py:13-20` 那段"为什么不做全文包含"的理由同样成立。 |

---

## `opencode_audit2` 那条「answered + 半句话（148 秒）」**没有修好**

| 项 | 内容 |
|---|---|
| **状态** | `KNOWN_LIMITATION` |
| **事实** | 如果复制键从第一秒就在，`settle=1.5s` 会在 1.5 秒交卷，跑不到 148 秒。所以真相是**生成被中断、文本已冻结、动作条此时才挂上**——结构上与"回答很短就结束"不可区分。 |
| **唯一旁证不成立** | "输入框被回填"是 opencode **每轮结束都会发生**的正常行为（13:44 追问轮直接 `draft_present`）。拿它否掉已到手的回答，会让 opencode 系统性丢失所有答案。 |
| **本次只做到** | ① `sig` 加文本长度，修掉**另一类**真实缺陷（动作条已在、正文还在流 → 1.5 秒复制半句）；② `answered` 时若输入框是回填状态，加 `warnings: ["composer_echo_after_answer"]` 供人工判断。<br>**不得宣称此条已修。** |

---

## 已知的口径偏差（不是未实现，但验收时要认）

| # | 项 | 说明 |
|---|---|---|
| 1 | `use_timeout = 120s` 只管 `test` / `accept_lite` | `review` / `accept` 经 `tier_guard.floor_timeout()` 抬到 `REVIEW_TIMEOUT=900s`（高档会审本来就慢）。用户 2026-11-07 批准。 |
| 2 | `endpoint_timeout` 名不副实 | review/accept 场景下 `server.py` 把它抬到 `max(420, choose+900+60)=1080s`。字段名说的是"单端总预算"，实际是高优先级下限。 |
| 3 | 面板对正式 `progress.json` **永久只读** | 设计决定，不是缺陷。F4 只对 `progress-test.json` 判分。 |
| 4 | `progress.json` 是扁平单卡记录 | 没有 `workflow.cards` 节点，面板即使只读展示也只能显示当前这一张卡。schema 事实。 |
| 5 | `.chrome-window.json` 未升 v2 | T1-1 的 P1 只做了"启动归属 nonce"，没有改复用缓存的 schema。理由：该缓存是**复用**用途（复用用户窗口时明确不关闭），不是**归属**证据；升版收益低、回归面大。 |
| 6 | `SESSION_ID_MISSING` 只在归属分支产生 | `wait_reply` 主循环不主动判它；真正的恢复路径在 `submissions.reconcile()`，那边已经 fail-closed。 |
| 7 | PRE_SUBMIT 探测期计入总预算 | T2-2 的 5s 探测期从 `_ask_impl` 的 `deadline` 里扣（`deadline` 在函数入口就算好）。所以 `test` 用途实际留给回答的是 `120 - 探测时长`。最坏 10s，可接受；如果要严格切分预算，得把 `deadline` 改成提交后再算。 |
| 8 | T2-2 的"重试"只在检测到"框里还有文字"时发生 | 这是**故意**的 fail-closed：发送成功会清空输入框，还留着才说明点没落下去。框已空、读不到、或错过 5–10s 窗口，一律不重发。 |

---

## 怎么读这张表

- `NOT_IMPLEMENTED` = 按决策不做，**不要等**
- `pending` = 还没开始，将来会做
- 两个词不能混用。混用会让验收方误以为"再等等就好了"。