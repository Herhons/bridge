# Bridge v0.0.9 — isolated WIP 当前状态

生成时间（Asia/Shanghai）：2026-11-07T13:05:00+08:00
上次更新：2026-11-07T12:10:00（阶段 0 完成后）／2026-11-06T05:40:25（原文见文末「历史快照」，未删除）

**本文件仅在独立 WIP 维护。正式目录 `F:\Bridge\v0.0.9` 保持只读。**
**正式目录 322 个核心文件在 B1–B5 全程零偏差（逐文件哈希核对）。**

---

## 0. 一句话状态

**代码改到位了，独立验收一项没做。**
离线 **1282 条全绿 / EXIT=0**（2026-11-08 B14）。`offline_audit_status` 全部 `pending`、
`e2e_accept_status` 全部 `not_run`。**不具备判完工条件，也不具备正式目录合并条件。**

### 三态分离（施工单红线 5）

| 字段 | 含义 | 谁能填 | 当前 |
|---|---|---|---|
| `dev_status` | 施工方自测 | 施工方 | B1–B5 已填；B6 `not_implemented` |
| `offline_audit_status` | 独立离线复核 | **非施工方** | 全部 pending |
| `e2e_accept_status` | 实机验收 | **非施工方** | 全部 not_run |

**施工 `done` 不等于验收通过。**

---

## 0.1 沿用既有结论（原文保留，事实未变）

以下三句是上一版状态表的结论，至今仍然成立，原样保留以免读者对照文末历史快照时误读：

1. **所有 WIP 改动均待不同厂商独立验收。**
2. CARD-05已存在并曾复验通过；插件已有09a/09b正式卡及成品；7b已由CARD-08实现，实际调度/真推不在本任务执行。旧表中的"无卡/未实现/05未发"仅留作历史。
3. 09e 的 WorkBuddy 本人完整重启原始证据待补（属证据型缺口，须真机补，不得用历史记录替代）。

---

## 1. 本次施工批次（2026-11-07，B1–B6）

> 施工单原定「逐阶段验收后才进下一阶段」。用户于 2026-11-07 明确要求
> **不再逐阶段等待，一次性推进到可用状态，由他统一安排验收**。本表的
> `offline_audit` / `e2e` 两列因此**全部为空**——这是事实，不是遗漏。

| 批次 | 施工单任务 | 做了什么 | 测试数 | dev | offline_audit | e2e |
|---|---|---|---|---|---|---|
| **B1** | T2-1 前半 / T2-2 / T4-1 | `submissions.py` 六态 v2 + L1/L2/L3 + 三条不变量；`use_timeout` 180→120；WorkBuddy test/accept 白名单补齐；通知 `NOT_CONFIGURED` 显式登记 | 1069 | selftested | pending | not_run |
| **B2** | T2-1 后半 | 五端（chatgpt/deepseek/codex/dsh/opencode）经 `conversation.ask` 统一接入账本；`flow` 透传 purpose；server 写 `evidence_l1` 并下发 env | 1094 | selftested | pending | not_run |
| **B3** | T3-1 | `replycodes.py` 四个显式错误码；采集链改 detail 版；`wait_reply` 连续结构原因早退（`stage` 仍叫 `timeout`） | 1117 | selftested | pending | not_run |
| **B5** | T4-2 | 插件 F4 从零接（只写 `progress-test.json`，三重防护 + mtime 乐观锁 + 3s 刷新 + 重载不丢）；F5 三维去重 + 补 `bad_request`/`server`；终态 job 输出回填；`request_id` 落盘；服务端双条件故障注入 | 1144 | selftested | pending | not_run |
| **B4** | T1-1 / T1-2 登记 | 删掉迟到窗口认领（不再误关用户窗口）；加 nonce 归属校验（灰度 chatgpt/gemini/grok）；`docs/known_gaps.md` 登记 T1-2 | 1282 | selftested | pending | not_run |
| **B6** | F3 并发 | **B13b/B14 已做**：锁下沉到分段临界区 + 线程池 + 回收线 + 错峰；真机 2/3 席全成、4 席回收 3 席（见 `evidence/batch14/`） | 1282 | **not_implemented** | n/a | not_run |

批次证据：`evidence/batch1..6/`（各含 `before_hashes.txt`、`after_hashes.txt`、
`unittest_batch*.txt`、`CHANGES_batch*.md`）。

---

## 2. 已声明的未完成项（`not_implemented`，不是 pending）

| ID | 内容 | 谁定的 | 影响 |
|---|---|---|---|
| **T1-2** | 真人输入让位（事件级归属） | 用户 | 打字/粘贴后约 150ms 盲区；检查到点击前约 2 秒空窗。`guard.py` 现有机制原样保留未删 |
| **F3-concurrency** | 多端真并发（锁粒度） | 施工方评估 | 多端会审功能可用，但总墙钟 ≈ 各端之和，**不满足"任务时间重叠"** |

详见 `docs/known_gaps.md` 与 `evidence/batch6/CONCURRENCY_ASSESSMENT.md`。

---

## 3. 原 16 卡（Codex 复验 2026-11-06，全量 731 OK / EXIT=0 / 0 跳过）

原复验结论与 WIP 自测分列，历史判定一字未改。详细缺口见
`evidence/stage0_baseline_20261107_1133/FAILURE_MATRIX.md`。

| 原卡 | 原复验 | 本卡 Ran | WIP 返工卡 | dev | offline_audit | e2e |
|---|---|---|---|---|---|---|
| 04c | FAIL | 10 | 04c-log-note | selftested | pending | not_run |
| PLAN-r1 | FAIL | 文档卡 | PLAN-sync | selftested | pending | not_run |
| GEM | PASS | 25 | 无（有后续保护改动） | unchanged | pass_prior | not_run |
| WEB | FAIL | 14 | WEB-spawn-cleanup | selftested | pending | not_run |
| 05 | PASS | 32 | 无（WIP 补并发锁） | unchanged | pass_prior | not_run |
| WEB2 | FAIL | 26 | WEB2-evidence | selftested | pending | **not_run（证据型待真机）** |
| 06a | FAIL | 79 | 06a-workbuddy-purpose | selftested | pending | not_run |
| 06b | FAIL | 72 | 06b-workbuddy-presend | selftested | pending | not_run |
| 06c | FAIL | 45 | 06c-composite-tier | selftested | pending | not_run |
| 06d | PASS | 35 | 无（后续入口调整） | unchanged | pass_prior | not_run |
| 07 | FAIL | 40 | 07-reply-attribution | selftested | pending | not_run |
| 08 | FAIL | 42 | 08-autopush-integration | selftested | pending | not_run |
| 09a | FAIL | 54 | 09a-lock / 09a-durable-idempotency / 09a-workflow-concurrency | selftested | pending | not_run |
| 09b | FAIL | 62 | 09b-health-contract-and-timeout | selftested | pending | not_run |
| 09e | FAIL | 64 | 09e-self-input-all-entries / 09e-workbuddy-restart-evidence | selftested | pending | **not_run（证据型待真机）** |
| 09f-fix2 | FAIL | 22 | 09f-fix3-entry-recovery | selftested | pending | not_run |

**合计**：PASS 3 / FAIL 13。13 项拆成 16 张返工卡 + TEST-ISOLATION = **17 张，全部 selftested**。

---

## 4. 16 卡复验之后的 9 轮（原文件生成时还没有）

| 轮次 | 内容 | 全量基线 | dev | offline_audit | e2e |
|---|---|---|---|---|---|
| UI-BOOTSTRAP-2/3 | 恢复/引导修复 + 失败定位 | 909 OK | done | none | not_run |
| R4 | opencode `read_effort` 下限 0.80→0.85 | 919 OK | done | glm_reviewed | not_run |
| R5 | CE1/CE2 恢复事务身份 + opencode 安全收集 | 949 OK | done | none | not_run |
| R6 | 同文档/同 URL 的 SPA 会话身份 | — | done | none | not_run |
| R7 | 标题不再作身份；Gemini `input_mismatch` | — | done | none | not_run |
| **R8** | `_existing_reply` 会话 id fail-closed | 992 OK | done | **Hy4 PASS** | not_run |
| **R9** | 采集只交本轮答案；Gemini 发送前需证明同会话 | 1005 OK | done | **Hy4: S8 FAIL, S9/S10 OPEN** | not_run |
| **R10** | 回答边界改为结构证据 | 1023 OK | done | **pending** | not_run |

**R9 不得记为通过。** Hy4 原文：*"the general claim 'reply always contains only the current answer' is NOT proven: S8 is a reproducible FAIL counterexample."*

---

## 5. 当前基线

```
PYTHONPATH= py -3.10 -X utf8 -B -m unittest discover -s tests -v
Ran 1282 tests in 141.285s
OK
EXIT=0
```

演进：1023（本次开工前）→ 1069 → 1094 → 1117 → 1144 → 1159 → 1255 → **1282**。

> **跑测试必须清空 PYTHONPATH**：本机 `PYTHONPATH` 被预设为 WorkBuddy 的 shim 目录，
> 会顶掉项目的离线守卫，导致 `EXIT=86` 与大量假 error。详见 `TEST_BASELINE.md` §4。

---

## 6. 阶段进度对照（施工单口径）

| 阶段 | 代码改动 | 独立验收 | 能否进下一阶段 |
|---|---|---|---|
| 0 冻结基线 | 无 | — | ⏳ CALIBER / 审计例外仍待签 |
| 1 高危安全 | T1-1 已做；**T1-2 跳过** | **无** | ✘（T1-2 未实现） |
| 2 状态机 + CARD-08 | B1 + B2 已做 | **无** | ✘ |
| 3 文本保真 | B3 已做 | **无** | ✘ |
| 4 通道配置 + 插件 | B1 配置 + B5 已做 | **无** | ✘ |
| 5 F1–F6 实机 | **未开始** | — | ✘ |
| 6 合入 | **未开始** | — | ✘ |

---

## 7. 执行边界（沿用，未变）

本 WIP 不执行 F1–F6 真机流程、不更新正式 progress 或 verdicts、不通知、不发送、不重启应用。
GLM 额度例外仅代表链路到服务端，未冒称 token 回复。

## 8. 并发会审：2026-11-08 起第 1、2 条作废（B13b）

历史快照里「UI 并发与会审席位规则（Herhons 2026-11-02 定）」的前两条已被取代，
**原文保持逐字不动**（`test_rework_plan.py` 钉了它的哈希），以此处为准：

- ~~会审各席严格串行~~ → 桌面动作（焦点/剪贴板/合成输入/截图）仍**严格互斥**，
  但"等回答"并行；单次持锁只覆盖"准备+发送"一段（≤25s），等待期间**不持锁**。
  依据 `CALIBER §3.1`：不得持有全局锁串行等待完整回答。
- ~~脚本从启动到读完回复全程持 `runs\ui.lock`~~ → 入口改为 `uilock.entry`：
  `BRIDGE_UI_SECTIONED=1` 时锁下沉到 `uilock.section()` 短临界区；
  同一端另有 `runs\turn\<端>.lock` 整轮互斥。探针脚本仍走 `uilock.run`，不受影响。

并发度 / 回收线 / 错峰 / 读屏端名单：**以 `docs/bridge_server.json` 为准**
（`endpoint_concurrency`、`min_success`、`endpoint_stagger`、`exclusive_endpoints`；改完重启后端）。
真机结果：2 席与 3 席全成；4 席回收 3 席（`partial` + `quorum.met=true`），
**第 4 席 codex 在 4 席同开时仍不稳**（choose 抢前台 / use 超时），详见 `docs/known_gaps.md` 的 F3。

⑨ 后端常驻（B14）：`start` 起的子进程用 `DETACHED_PROCESS`（+ `CREATE_BREAKAWAY_FROM_JOB` 失败时回退）；
验收方式是"另起一个 shell 后 `status` 仍 `running:true` 且 `pid_alive:true`"。
`status` 新增 `pid_alive` / `stale_pid_file` / `mismatch`，`start` 现在会报真实 `pid` 与 `detached`。

## 历史快照（以下原文不是当前状态表）

以下保留原PLAN全文与带日期决定，不删除旧记录；本文件顶部当前表承接后续结果。

<!-- ORIGINAL_PLAN_SNAPSHOT_START -->
# Bridge v0.0.9 完工计划

## 目标
把 Bridge v0.0.9 做完。完工标准：插件移植到 v0.0.9 后，在 Herhons 电脑上实测 Chrome 插件能正常使用。

## 分工
- 计划/任务卡/监工：红莉栖
- 干活：WorkBuddy · DeepSeek 4.1 Flash（只按卡做，不自己加任务、不改方向）
- 验收：Codex · GPT-6.1 Sol 最高档（只看卡和成品，不看干活方的说明；结论只有 通过 / 打回 / 证据不足挂起）；同时审本计划

## 档位规则
- 所有测试发送：最低档（如 GPT-6 Luna）
- 所有问答：最高档
- ChatGPT 网页端切档暂时关闭，先用当前档（第 3 项修好后再纳入）
- 额度不够只挡「真实发送」的验证；离线单测、读文件、只读查询的验收照常进行。

## 顺序与状态表
| # | 任务 | 状态 | 卡 / 证据 |
|---|------|------|-----------|
| 7 | 监工入口：progress.json + 完工通知 + WorkBuddy 停了自动推 | 通过（CARD-01）／待做（CARD-07b 自动推） | CARD-01 已验收通过；证据 `evidence\reaccept_1150\card01_notify.txt`、`evidence\reaccept_1150\card01_progress_after.json`。CARD-07b 只有大纲，卡文件未建 |
| 7b | WorkBuddy 停了自动推（从第 7 项拆出） | 待做 | CARD-07b（未建卡）；无证据 |
| 1 | 今天改的 3–7 项：实机跑一次 + Codex 验收 | 待做 | 无卡；等第 2 项收尾后回头做，暂无证据 |
| 2 | Codex 端、WorkBuddy 端收发验证 | 通过（CARD-02、CARD-03）／待做（CARD-GEM-fix） | CARD-02 证据 `evidence\reaccept_1150\card02_codex_choose_list.txt`；CARD-03 证据 `evidence\reaccept_1150\card02_03_workbuddy_list.txt`；CARD-GEM-fix 现场证据 `evidence\gemini_bug_1215.png`、`evidence\gemini_bug_1215_uia.txt`、`evidence\gemini_bug_1215_script_log.txt` |
| 3 | 修 ChatGPT 切模型/切档 | 待做 | 无卡；现状证据 `evidence\chatgpt_tier_1142.png`（`chatgpt_choose --list` 读不出模式和档位） |
| 4 | codex/dsh/opencode 接上新超时和运行日志 | 打回（CARD-04、CARD-04b）／施工中（CARD-04c） | CARD-04 打回；CARD-04b 复验再打回；CARD-04c 已发出。证据 `evidence\card04b_list.txt`、`evidence\reaccept_1150\card04b_codex_list.txt`、`evidence\reaccept_1150\card04b_dsh_list.txt`、`evidence\reaccept_1150\card04b_opencode_list.txt`；`evidence\card04c_list.txt` 待出 |
| 5 | 工作流模式放进 v0.0.9（草稿已有） | 待做（草稿骨架已建） | CARD-05 未发；草稿 `docs\workflow\WORKFLOW-DRAFT.md`（原稿没找到，按 PLAN 写的骨架） |
| 6 | 插件移植 + 工作流面板 | 待做 | 无卡；无证据 |
| 8 | 实机测 Chrome 插件能正常用 = 完工 | 待做（最后） | 判据见「第 8 项」小节；证据收在 `evidence\item8\`（还没有） |

状态只有六档：待做 / 施工中 / 已交付待验收 / 通过 / 打回 / 挂起。
`progress.json` 侧只有 running / done / stuck / failed 四档，对应：施工中=running、已交付待验收=done、挂起=stuck、失败=failed；打回是验收结论，跟不上四档，下一步由新卡（如 04b、04c）的 running 体现。
本表按 2026-10-31 12:12 巡检的实际情况填：11:40 卡面写的「CARD-01/02/03 挂起、CARD-04b 已交付待验收」已被 12:12 的 Codex 复验结果取代（01/02/03 通过，第 4 项含 04b 再次打回）。

## 第 8 项：Chrome 插件实测（必测流程 / 判据 / 证据）

测试账号的登录由 Herhons 自己完成，测试时不填密码。证据统一放到 `evidence\item8\`。

**公共约定**
- token 由测试脚本生成，格式 `BR9-<端>-<时间戳>-<随机4位>`（例 `BR9-grok-20261031T1430-7f2a`）。
- 同一个 token 在运行日志里要能对上「发送」「收到」两行。
- F2/F3 统一话术：「请只原样回复这一行：<token>」；回复里的 token 必须和发出去的逐字一致（大小写、连字符都不许变）。
- F2/F3 超时 120 秒，最多重试 1 次；重试要在证据里写明是第几次。
- 测试发送一律最低档。

**F1 加载**
- 在 `chrome://extensions` 用开发者模式加载 v0.0.9 插件目录，不能有报错标记，能打开面板。
- service worker 和面板控制台都没有报错；权限提示正常。
- 记下 Chrome 版本号和插件目录的绝对路径。
- 证据：截图 + 版本号/路径写进 `evidence\item8\f1_load.txt`。

**F2 单端问答**
- 面板里选 deepseek，最低档发一条带唯一 token 的消息，120 秒内回复里要带这个 token（逐字一致）。
- 证据：面板截图 + 运行日志里同一个 token 的「发送」「收到」两行。
- 这一端因额度不够发不出去 → 本项判挂起，不判打回。

**F3 多端会审**
- 一次发给 ≥2 个端（例如 grok、gemini），多端并发发送，超时 120 秒。
- 每个端都要回来带 token 的回复，并分端显示。
- 判据：选中的端全部成功才算过。任何一端失败，面板要单独标红并写明是哪一端、什么原因 → 判打回，不能算通过。
- 证据：截图 + 运行日志（每端「发送」「收到」各一行）。

**F3b 部分失败隔离**
- 会审中途关掉一端，另一端要照常返回并显示，失败的那一端单独标红（写明端名和原因）。

**F4 工作流面板**
- 用专门的测试卡 CARD-T8 和单独的 `progress-test.json`，不碰真实的 `progress.json`。
- 在面板里建这张测试卡，状态从面板上操作切换：草稿 → 已发 → 完成；`progress-test.json` 和面板显示要一致。
- 反向同步：外部用 `notify_done.py` 改了 `progress-test.json` 之后，面板 10 秒内要自动刷新。
- 插件重新加载后，卡片状态不能丢。
- 测完自动清理测试卡和 `progress-test.json`。
- 证据：`progress-test.json` 原文 + 面板截图。

**F5 异常**
- 四个场景：①某一端不可用（关掉它的标签页）；②登录过期（由 Herhons 手动登出某一端）；③找不到输入框（用测试开关模拟）；④本地 Bridge 后端没启动。
- 判据：每种场景 10 秒内出现明确的错误提示，指明是哪一端 / 哪个场景；面板不能卡死，其他端照常能用；运行日志里该端恰好记 1 行 error。
- 证据：截图 + 运行日志。

**F6 回归**
- `python -m unittest` 全过。
- 用 Chrome 的「重新加载」刷新插件后，把 F2~F5 全部重跑一遍（不只是 F2）。

**判据**
- F1~F6 全部通过才算完工；任意一条不过就判打回；证据缺了就判挂起。
- 某端因额度不够发不出去，那一项判挂起，不判打回；所有项都过了才算完工。

## 第 7 项子卡 CARD-07b：WorkBuddy 停了自动推（大纲，本次不实现）
- 停了的判定：`progress.json` 里是 running，同时运行日志超过 N 分钟（默认 20）没有新行，也没有「生成中」的标志。
- 「生成中」的标志 = `workbuddy_use --list` 的输出，或 runlog 最后一行的 `stage=generating`。
- 动作：用 `workbuddy_use --continue` 发一句固定的「继续按卡施工」。
- 防重复：同一张卡 30 分钟内最多推 1 次；连着推 3 次还没进展就停下来，通知红莉栖。
- 本卡不出卡文件、不写代码；将来出卡时按任务卡规则补齐「做什么 / 怎样算完成 / 允许改 / 禁止碰」。

## 任务卡规则
每张卡写清：做什么、怎样算完成、允许改的文件、禁止碰的东西。
最后一步固定：把结果（完成/卡住/失败 + 一句原因）写进 progress.json，并调用通知脚本。不管成没成都要通知。
同一张卡连推 3 次没进展 → 停，叫红莉栖。

## 会审规则
遇到关键节点，先找 ≥2 家不同厂商会审，再动手。关键节点包括：发新阶段的卡、改 PLAN、判定完工。
- 同厂不算数：施工方是 DeepSeek 4.1 Flash，DeepSeek 那一家只能算一家，必须另有一家独立厂商（如 Gemini、Grok、GPT）才能凑够 ≥2 家。
- 会审有 REVISE 的，先把意见并进卡里再发；会审原文留证据（放 `evidence\`）。
- 只凑不到 ≥2 家就先排队，不硬上；排队的卡在进度表里标「待做」。

## 巡检
- 完工即时通知 + 每小时巡检兜底
- 每次巡检记一笔：有没有推动进度 / 白跑。跑一晚后看记录，没意义就拉长间隔
- 只在三种情况叫 Herhons：要合并、碰红线、连续打回

## 底线（不变）
先备份、写改动说明、留回滚；合并要 Herhons 点头；尽量不调 CLI；不改注册表/策略/其他扩展/登录/书签/audit\；不填密码不绕验证码；`F:\Bridge\v0.0.8` 不碰。

## 前提
电脑晚上不睡眠；监工脚本在自动审核里放行。

## 决定记录
- 2026-10-30 目标定为完成 v0.0.9，不做 0.1.0
- 2026-10-30 计划由红莉栖写；通知脚本作为第一张卡交 DeepSeek 4.1 Flash 做
- 2026-10-30 验收用 GPT-6.1 Sol 最高档；GPT-6 Luna 只用于测试发送
- 2026-10-30 即时通知 + 1 小时巡检，先试一晚
- 2026-10-30 第 8 项（Chrome 插件实测）是最终完工标准，排最后
- 2026-10-31 05:10 红莉栖：Codex 额度用完、codex_choose 认不出弹层，CARD-01 验收挂起；先发 CARD-02 修第 2 项里的这两个收发 bug（Codex 选档弹层、WorkBuddy 多输入框），修好后再补 CARD-01 验收、回到第 1 项
- 2026-10-31 06:10 红莉栖：CARD-02 已交付（78 OK，codex_choose --list 真机正常）；Codex 窗口仍显示使用上限，CARD-01/02 验收继续挂起；发 CARD-03 修 workbuddy_use.py --list，仍属第 2 项
- 2026-10-31 07:10 红莉栖：CARD-03 已交付（91 OK，workbuddy_use --list 真机退出 0、读出 DeepSeek-V4.1-Flash），第 2 项代码收尾；Codex 仍未确认额度恢复，CARD-01/02/03 验收继续挂起。第 3 项（ChatGPT 切档）要在真网页反复点击、之前误点过，留到白天能验收时再做；先发 CARD-04 做第 4 项（codex/dsh/opencode 接运行日志和超时分型，离线可测）
- 2026-10-31 12:12 红莉栖（巡检）：Codex 复验 CARD-01/02/03 通过；CARD-04（含 04b）再次打回（空闲等待在异常保护区外、Codex --list 失败的 hint 没进 runlog error）。发 CARD-04c 修这两条；CARD-PLAN-r1、CARD-05 继续排队
- 2026-10-31 12:2x 红莉栖：会审等待改为最长 5 分钟、每 30 秒轮询，超时先回页面核对再判失败（gemini_use 检测 bug 见 CARD-GEM-fix）

- 2026-11-01 10:50 巡检：CARD-WEB 经 GLM 只读验收 PASS（待 Codex 复验）。CARD-05 会审 Gemini、Grok 两家均 REVISE（跳转表不闭合、推进/修复卡/hold 数据形状未定、notify_done 与禁止项矛盾、证据文件不在白名单、退出码不全），已按意见改成 r2 并发 WorkBuddy。备份 _backup_20261101\*.bak-1050。

- 2026-11-01 12:20 巡检：CARD-05 经 GLM 只读验收 PASS（189 OK，证据 evidence\card05_accept_glm_1210.txt，待 Codex 复验）。为让会审能自动跑，先发 CARD-WEB2（grok 发送键失配、gemini 新对话/完成检测、grok_use --read），属第 2 项 bug 修复，不改计划范围。- 2026-11-02 05:15 巡检（手动恢复施工）：CARD-06 会审 Gemini(argon)、Grok(expert)、ChatGPT(GPT-6 高) 三家均 REVISE（回执防重放/原子消费、发送前复核档位、UIA 唤醒不可靠需多重兜底、退出码统一、最小化恢复轮询校验），合并为 r2 并拆卡：先发 CARD-06a（档位回执+UIA 唤醒+最小化），CARD-06b（ChatGPT 聊天选档、Ctrl+Shift+M 冲突、列表符、缩放模板、gemini 新对话）排队。备份 _backup_20261102\*.bak-0515。

- 2026-11-02 05:56 决定：06b 三家会审(Gemini/Grok/ChatGPT)均 REVISE；档位读不到选 A 拒发；GLM 不进 accept，改独立 accept_lite；拆为 06b(门禁机制) + 06c(适配/配置)。此后简单明确的修复卡不再会审。

- 2026-11-02 08:12 决定（Herhons）：opencode_use 已到达服务端并收到「Free usage limit reached」即视为发送链路打通，可作 06c 证据，不必等额度重置。验收改用 WorkBuddy Hy4 preview（0.29x，已批准）：每次验收先开新对话 → workbuddy_choose Hy4 读回确认 → use --continue --purpose accept，只读，必须附命令/退出码/测试数，无证据即 FAIL。简单卡不走会审（CARD-06d 起）。

- 2026-11-02 12:22 巡检：CARD-09 会审三家齐（Gemini REVISE / Grok APPROVE 附缺口 / ChatGPT REVISE）。合并：ThreadingHTTPServer+OPTIONS 预检、固定端口 18765、token 持久化不每次重生成、跨进程 UI 文件锁覆盖 choose→读回→use、202+job 落盘+request_id 去重、workflow 文件名白名单+合法迁移+原子写；progress.json 仍为唯一状态源（ChatGPT 的 SQLite 意见属 v0.0.4，不采用）。拆为 09a 后端 / 09b 插件移植+配对（manifest 固定 key、popup 生命周期改 sidePanel）/ 09c 工作流面板 / 09d 第 8 项实机。09a 已发 WorkBuddy。

## 测试与验收范围规则（Herhons 2026-11-02 15:57 定）
- 单元测试每张卡都跑全量（py -3.10 -m unittest，便宜）。
- 真机实测和验收只覆盖本卡改动及其触及的模块；已通过的端点不重复真机复测。
- Hy4 验收只核对本卡改动 + 全量单测数（Ran/OK），不重验旧卡。


- 2026-11-02 18:05 决定：CARD-10 会审三家齐（Gemini argon / Grok expert / ChatGPT GPT-6 高，均 REVISE，原文 evidence\card10_review\*_reply_1751.txt）。Herhons（t1223u）批准直接合并施工，不用他过目。拆成 10a（ruff F/E9/I + 冻结基线、每个 bug 一条回归测试、行数棘轮白名单无通配符并带 owner/目标、测试名基线对比 09e）/ 10b 草稿（_ui 改名 + 薄转发、@ui_entry、锁→活动→窗口→输入、回执绑定窗口/任务/过期且只用一次、唯一输入出口 ui_input.py、不设通用 --i-know）/ 10c 草稿（replay L1/L2/L3、哈希加脱敏摘要、原文默认关、按端按阶段 watchdog）。冲突一律取更严一侧，并在卡内注明提议方；10b、10c 方法有变，施工前各自单独会审。施工模型改为 WorkBuddy DeepSeek-V4-Pro（Herhons 指定，不用 V4.1 Flash），没有 V4 Pro 或读回失败就停，不回退。

## UI 并发与会审席位规则（Herhons 2026-11-02 定）
- 会审各席严格串行：上一席发出并读完回复后，才开下一席。
- 每个 *_choose / *_use / 探针脚本从启动到读完回复（或失败退出）全程持有 runs\ui.lock；施工方写真机探针前也必须先拿锁。施工卡运行期间，主线不跑真机席位，或者双方都经锁。
- 输入：先键入几个字符激活编辑器，其余内容走剪贴板粘贴（保存并恢复原剪贴板）。
- 选模型：目标模型项在可视区外（IsOffscreen）要先 ScrollIntoView 再点；下拉没打开要重试打开。
- 不得关闭、静音或切走 Herhons 的直播窗口；优先用副屏，用完把直播切回前台。

## 防重发规则（09f，Herhons 17:48 截图确认；09f-fix 补全）
- 任何 *_use 发送前，先查当前对话里本提问之后是否已有新回复；有就只读，返回 already_answered，不发送；此时若输入框被回填了提问文本，也要清掉并读回为空。
- 高档（review/accept）等回复上限 ≥900s，不得 300s 就判超时。
- 一次 submission=accepted 之后绝不自动重发；回填到输入框的提问要清掉并读回为空。workbuddy 因 UiDocsLost 自动重启后，重跑发送前先只读确认没答过，答过就只读返回，不重发。
- 19:24 Herhons 新规：验收必须在 WorkBuddy 新对话里做，不得用构建者对话。步骤：新对话 → choose Hy4 preview → 读回档位 → use --purpose accept（实现：workbuddy_use --new --model "Hy4 preview" --purpose accept，脚本内顺序为新建→选模型→读回写回执→档位闸→发送）。验收提示只给卡、证据路径和仓库路径，由验收方自己跑检查。构建留在构建对话用 --continue。
<!-- ORIGINAL_PLAN_SNAPSHOT_END -->
