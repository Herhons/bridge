# Round 0 改动记录

时间：2026-11-11（Asia/Shanghai）
施工：WorkBuddy（DeepSeek-V4-Pro）
规格：`docs/self_iteration_spec.md`

## 1. 网页端上传（本次新增能力）

| 项 | 内容 |
|---|---|
| 现状 | 仓库里没有任何上传实现；四家里只有 chatgpt 在 `lastreply.py:57` 记录了附件按钮名「添加文件等内容」 |
| 探测 | 只读探四家工具条：grok「附件」、gemini「上传和工具」（是菜单）、deepseek 首页仅 5 个按钮、无命中 |
| 关键发现 | chatgpt 的入口是二级：点「添加文件等内容」先开「+」菜单，菜单项是「添加照片和文件 从计算机上传」，点它才弹系统文件对话框（`#32770`，标题「打开」） |
| 难点 1 | 文件对话框抢前台，`Screen.press/type_text` 必然 `ForegroundLost` → 改用 UIA `ValuePattern.SetValue` + 原生鼠标点「打开」 |
| 难点 2 | 路径必须用**反斜杠**；写成正斜杠时对话框报「文件名无效」 |
| 难点 3 | 该对话框是 DirectUI，`EnumChildWindows` 只看到一个 Button；只能走 UIA（文件名框 `AutomationId=1148`，「打开」按钮 `AutomationId=1`） |
| 落地 | `src/qi/upload.py`：`upload(screen, site, path)`，含残留对话框清理、菜单项轮询、写路径、点打开、等附件落地（页面出现文件名） |
| 实测 | chatgpt 上传 `probe.md`：`{"ok": true, "waited": 3.7}`，页面出现「正在上传文件 probe.md / 移除 probe.md」；随后已清理附件 |

## 2. 编排器（规格 §13 最低交付）

| 文件 | 作用 |
|---|---|
| `src/qi/iteration.py` | 纯函数：状态机、材料打包（禁止全局 `**` 替换）、引用验真（路径 / 章节+逐字引句）、匿名化、归档 |
| `tools/iterate.py` | 驱动器：`snapshot / review / verify / anonymize / summarize / status`，支持 `upload` 与 `paste` 两种投喂 |
| `docs/iteration.json` | 本轮投喂配置：chatgpt=upload，其余=paste；分块 3600 字 |
| `tests/test_iteration_core.py` | 13 条：`**` 保真、分块与围栏闭合、验真阈值、匿名化去来源、状态续跑 |
| `tests/test_iterate_dryrun.py` | 干跑：夹具规格 + 四家夹具回复 → 验真 → 匿名包 → 归档结构 |

## 3. 本次踩到并修掉的问题

1. **`tools/` 进 `sys.path` 会污染 unittest 发现**：`tools/test_all.py`、`tools/test_writer.py` 会遮蔽 `tests/` 下的同名模块，导致全量 19 条红、单跑全绿。改为按文件路径 `importlib` 加载 `tools/iterate.py`。
2. **等发送键可用是错的**：空输入框本来就不启用发送键，第一枪因此空等 20 分钟被杀。改为等「正在上传」消失。
3. **上传后 composer 残留导致 `draft_present`**：上传前先 `_clear_composer`（聚焦 → Ctrl+A → Delete）。
4. **账本人为出口不够用**：被杀的进程留下 `submitting`，而 `submission_clear.py` 只认 `failed_terminal`/`unknown`。已把 `submitting` 纳入（同样要求 `--confirm` + `--reason` + 打印摘要）。这条是 Round 0 自己暴露的第一个缺口。

## 4. 测试证据

| 命令 | 结果 |
|---|---|
| `py -3.10 -B -m unittest discover -s tests` | `Ran 1577 tests` / `OK`（修复 `sys.path` 污染后） |
| `... -p test_iteration_core.py` | `Ran 13` / `OK` |
| `... -p test_iterate_dryrun.py` | `Ran 1` / `OK` |

## 5. 快照

- 被审快照：`2348121e488b97e1d13bbdfa73627b85e3162003`（含规格与材料）
- 公开 tip：`77430a5`（含 prompt）
- 公开仓：https://github.com/Herhons/bridge

## 6. 续跑（2026-11-11 20:00 起，Herhons 批准）

| 步骤 | 做法 | 结果 |
|---|---|---|
| STOP 归档 | `STOP.md` → `STOP_handled_20261111_2001.md` | 已改名 |
| 清理公开仓 | 删除误提交的 `docs/self_iteration_spec.md.20261111_bak` | 已删（随本轮提交） |
| 新开会话 | 新增 `start_new_chat()`：点「新聊天 / 发起新对话 / 聊天」，并以「地址里没有会话 id」为准 | chatgpt 实测 `/c/<id>` → `/`，通过 |
| 上传自检 | 新增 `upload-check` 命令：新会话 → 上传小文件 → 只问一句 | **通过**：`upload ok 3.2s`，回答正是 `BRIDGE-UPLOAD-CHECK-20261111`（token 命中） |
| 分段超时 | 中间分段 300s、最后一段用 review 档 900s，避免一段卡住拖 15 分钟 | 已配置 |

关键修正：`start_new_chat` 的判据不能是「URL 变了」——已经在 `/` 时点一下不会变地址。改成「地址里没有会话 id」，这才是"新会话页"的准确定义。

## 7. 第二次续跑（2026-11-11 21:45）

| 项 | 结果 |
|---|---|
| 删除 `.bak` | 已删，随本轮提交 |
| 新开会话 | `start_new_chat()` 增加「已在新会话页就直接通过」；chatgpt 实测切换成功 |
| 上传自检 | **通过**（token `BRIDGE-UPLOAD-CHECK-20261111` 命中，上传 3.2s） |
| 四席 | 未产出可验真回复：chatgpt 长回答 300s 未就绪、gemini 清框无效、deepseek/grok 粘贴折叠换行、grok 上传入口是二级菜单 |
| 快照 SHA | 修正为已推送的 `4f1a848`（原记本地 `2348121`，远端不存在） |

详细失败点见 `STOP_20261111_2145.md`。

## 8. 第三次续跑（2026-11-11 23:40）

代码级修复（6 处）：
1. `_clear_composer` 改用 `Conversation.clear()`；
2. 新增 `harvest_reply()`（停止键消失 + 复制内容稳定），upload/paste 两条路径都接；
3. 菜单项类型放宽到 `ButtonControl + MenuItemControl`；
4. 菜单 toggle：先查再点；
5. 附件判据同时认完整文件名与主干（gemini 去扩展名）；
6. 新增 `clear_attachments()`。

环境级阻塞：前台长期停在 `WorkerW`，Chrome 抢不回前台；按红线不抢用户窗口，停机。
详见 `STOP_20261111_2340.md`。

## 9. 第四次续跑（2026-11-11 23:50）

- 清掉 chatgpt 残留草稿（点击点夹进窗口后才成功）。
- 修 8 处（详见 `STOP_20261111_2350.md`）。
- **拿到第 1 条真会审**：chatgpt 1714 字，9/10 命中，true_read。
- 仍缺 2 席：gemini 发出但无答案；deepseek/grok 粘贴保真未解。

## 10. 占位符判据（修正上一次的错误改法）

一开始把「问问 Gemini」塞进 `KNOWN_PLACEHOLDERS` 全局表，被 `test_conversation` 挡下：
占位符是**按位置**判的（值等于输入框框内的文本节点才算空），全局表会让"别处的同名字样"也当空。
正确做法：给 `Conversation` 注入该端的 `empty=` 判据（`composer_empty(view, placeholders=...)`），
核心文件恢复原样。gemini / chatgpt 现在都正确判空。

## 11. 第五次续跑（2026-11-12 00:30）

- 改用各端 `*_use.py` 发问（不自己拼 flow.ask）：deepseek 单发**成功**（review 档，6.9s）。
- 发现三个硬事实：档位回执一次性、失败必留草稿、`submission` 字段不可单独作证据。
- gemini 今天这次没到 reserve（旧账本 46.9h），页面回 `/app`，判定未落地。
- 详见 `STOP_20261111_0030.md`。

## 12. 第六次续跑（2026-11-12 03:00）

- `prompt.md` SHA 改为可达的 `6b049b32…`。
- deepseek：9 段全部落地（页面可见第 2–9 段），但第 9 段提交后无回复；`--read` 不支持。
- gemini：`newchat_not_landed` 是**误判**（把侧栏标题当消息），页面实际停在 `/app` 且输入框为空。
- grok：标签页 0 个按钮，疑似死页，附件菜单未采到。

- 补充：deepseek 对 9 段材料回 `可访问=否`（认为材料不完整、无法核对），不计有效席。
  结论：deepseek 必须走上传，否则拿不到可验真回复。

## 13. 第七次续跑（2026-11-12 02:31 起）

| 步骤 | 做法 | 结果 |
|---|---|---|
| STOP 归档 | `STOP_20261112_0300.md` → `STOP_handled_20261112_0231.md` | 已改名 |
| gemini 落点误判 | `_new_chat_landed` 只认**带标签或带动作条**的块；侧栏会话标题是无标签长文本，以前被当成"上一轮消息" | 加回归用例 `test_sidebar_titles_are_not_messages` |
| gemini 残草死锁 | 新增 `_drop_stale_draft()`：点「发起新对话」**之前**清掉上一轮失败留下的草稿（否则 `draft_present` 永远判"还在旧会话"） | 8 条用例全绿 |
| 档位回执统一 | `_refresh_tier()` 按端处理：gemini 用 `--model argon` 重设最高档；deepseek 用 `--think off→on`；上传/粘贴两条路径发送前都调用 | — |

**真机验证**：`gemini_use.py "只回复 OK" --purpose review --new` → `{"ok": true, "reply": "OK", "elapsed": 22.6, "stage": "answered"}`
（此前同一命令报 `newchat_not_landed`，根因是框里残留着上一轮的「只回复 OK」）。

### 13.1 gemini 走上上传（真机通过）

`conversation.ask` 在 gemini 上会把「写了字、没发出去」判成 accepted（两次实测：账本 accepted，
但地址仍 `/app`、输入框仍留 162 字提问、0 消息块），随后被 `submission_pending` 永久拦住。
因此新增投喂方式 **`upload-script`**：先上传附件，再用该端 `*_use.py --continue` 写短指令并发送。

| 修的东西 | 说明 |
|---|---|
| `gemini.draft_present` | 改成**优先读输入框真实文本**；挂附件时发送键本来就可点，旧判据永远说"有草稿" |
| `iterate._refresh_tier` | 按端配方：gemini `--model argon`（review 最高档）、deepseek `--think off→on`；上传/粘贴发送前都调用 |
| `submission_clear --state accepted` | 人工出口支持 accepted（仍强制 `--confirm` + `--reason`，写盘前打印记录摘要） |

真机结果：gemini `answered` 3896 字 / 74.7s；验真 25/45 = **0.56 partial**（引句 20/34、路径 5/11）。

### 13.2 grok 救活并跑通上传

- 页面 0 按钮 → `Ctrl+L` 重新导航后恢复 17 个按钮；
- 附件入口是 `MenuControl`「附件」→ 菜单项「上传文件」，补进 `ATTACH` 规格；
- 菜单偶发不弹出（页面重排），加一次重试点击；
- 附件关闭键叫「移除此附件」（**中间没有空格**），前缀改为裸「移除」。

真机结果：发送 `accepted`，但 `conversation.ask` 判 `timeout/no_reply`（0 字）。
页面上答案其实完整（34 段长文本），是**收卷判据**不认 grok。
`read_answer_text` 改为「锚在第一个提问回声，取到下一个回声/『已完成回复』为止」，
取回 2319 字；验真 15/33 = **0.45 partial**（引句 12/28、路径 3/5）。

### 13.3 安全事件（必须记）

`upload.clear_attachments` 原来会点名字以「删除 」开头的按钮。grok 页面上控件树混进了**桌面项**，
按坐标一点就落到桌面上，弹出了 Windows 的「确实要从桌面上删除这些图标吗?」。
**已用「否」取消，没有删除任何东西。** 两处立即收紧：

1. 前缀只认「移除 / close / remove」，**永不点「删除」**；
2. 只点**浏览器窗口矩形内**的控件。

### 13.4 严格引用重发（不改规格，只改提问措辞）

| 端 | 前 | 后 |
|---|---|---|
| grok | 0.45 (15/33) | **0.64 (7/11)** |
| gemini | 0.56 (25/45) | **0.57 (4/7)** |

两家仍未达 `true_read`（≥3 命中且 ≥0.7）。未命中的是"截断的引句"与"编造的路径"，
按合同不算命中。是否放宽口径属于改闸门 → 交 Herhons 决定，本轮不自行修改。

## 14. 结案（2026-11-12 09:37，Herhons 选 3）

| 步骤 | 动作 |
|---|---|
| STOP 归档 | `STOP_20261112_0330.md` → `STOP_handled_20261112_0937.md` |
| 结案文件 | 新增 `FINAL.md`：四席数字、三份意见索引、两条遗留问题、边界声明 |
| 清理 | 删除 0 字节的 `replies/deepseek.md`（它没有回复，空文件容易被误当成一席） |
| 未做 | 匿名包 / 汇总 / 核实表 / 计划——以 3 家真读为前提，前提不成立就不产出 |
| 边界 | 未改 `docs/self_iteration_spec.md`；未开下一轮；停在等 Herhons |

Herhons 的决定：§5.3 阈值与逐字口径**都不改**，按 §12 停机，三份意见原样人工采信。

## 15. 修复第 2 项：引用核对口径（2026-11-12 10:10）

按 Herhons 批准的 4 条规则改 `src/qi/iteration.py::verify_reply`：

| 规则 | 实现 |
|---|---|
| 分母 = 结构化意见条数 | `split_opinions()` 四种切法：编号列表 → 行内「章节号…原文依据」→ 逐行标题 → 空行 |
| 每条至少一处有效引用 | 一条里命中 ≥1 个引句或有效路径即算这条命中，一条只计 1 次 |
| 光写章节号不算命中 | `_has_citation` 要求章节号**或**引句**或** `文件:行号`；命中判定只认引句/路径 |
| 重复引句去重 | `seen_quotes` 全局去重，后来的复用标 `dup_of` 且不加分 |

顺带修掉三个真 bug：
1. `SECTION_RE` 只认「第 5.2 节」，**不认「§5.2」**——真机回复几乎全是后者，一条都抓不到；
2. 引句内部的文件名被当成独立路径引用（一句捏造引句只要提到真实文件名就算命中）；
3. 逐行写意见的回复（chatgpt）会被空行切法合并成 1 条 → 加「标题行」切分。

新增 `tests/test_quote_verify.py`（12 条夹具），并同步更新
`test_iteration_core.py` / `test_iterate_dryrun.py` 的夹具（旧夹具靠路径凑 hit，按新口径本来就该掉）。

真实三份重算（同一批回复、只换口径）：

| 端 | 旧 | 新 |
|---|---|---|
| chatgpt | 9/10 = 0.90 true_read | **7/9 = 0.78 true_read** |
| grok | 7/11 = 0.64 partial | 4/6 = 0.67 partial |
| gemini | 4/7 = 0.57 partial | 3/6 = 0.50 partial |

## 16. 修复第 3 项：UI 锁范围与残留清理（2026-11-12 10:20）

| 改动 | 文件 | 说明 |
|---|---|---|
| `clear_stale(root)` | `src/qi/uilock.py` | 只在**确认持有者已死**时把元数据改写成墓碑（`owner=stale` / `stale_pid` / `released_by=stale`）；活着的一律不动 |
| 等锁时识别死持有者 | `src/qi/uilock.py` | `hold()` 忙等里若 `_holder_dead(meta)`，写墓碑并**立刻重试**，不再空等到 timeout（最多 3 次，避免狂转） |
| 锁只在动网页时持有 | `tools/iterate.py` | `_ask_upload` 整段持锁；`_ask_upload_script` **只包上传段**（后面的 `*_use.py` 是子进程，它自己会 `uilock.run`，父进程再持锁会自锁死） |
| 验真等离线步骤不取锁 | `tools/iterate.py` | 已有行为，本次加用例**锁住**：`verify` 跑完不许出现 `runs/ui.lock` |
| 续跑前清残留 | `tools/iterate.py` | `cmd_review` 开头 `_clear_stale_lock(state)`，结果记进 `state.json` 的 `lock` 键 |

新增 `tests/test_b27_uilock_scope.py`（5 条）：
死持有者被清、活持有者不动、`hold` 遇死元数据 3 秒内拿到锁、无锁文件时返回 absent、**验真全程不取锁且不建 lock 文件**。
连带跑 `test_b13_lock` / `test_b14_choose_lock` / `test_r3_choose_use_lockstep` / `test_rework09a_lock` 全绿。

## 17. 修复第 4 项：崩溃续跑与防重发（2026-11-12 10:35）

| 改动 | 文件 | 说明 |
|---|---|---|
| 固定身份 | `src/qi/iteration.py` | `chunk_identity(site,index,text)` = `chunk:<sha16>`（内容变则身份变）；`prompt_identity(site,round,prompt)` |
| 带身份分块 | 同上 | `pack_chunks_with_ids()` 返回 `[{index,id,chars,text}]`；`resume_chunks()` 跳过 `sent` |
| 状态 schema | 同上 | `SCHEMA="round-state/2"`；`state.json` 增 `schema` + `materials`；v1 老状态只补字段不改历史 |
| 段状态机 | 同上 | `State.mark_chunk(id, status)`：`pending / sending / sent / unknown`；`sending` 写在**发送之前** |
| 账本优先 | `tools/iterate.py` | `_reconcile_gate()`：`accepted/submitting/unknown` 一律先只读查答；有答案就直接采用，没答案返回 `needs_manual_clear`（**不自动清账本、不重发**） |
| 续跑核对 | `tools/iterate.py` | `_ask_via_script` 遇到 `sending` 段先用 `_page_has("第 i/N 段")` 核对：在页面就记账跳过，不在才补发；全部 `sent` 时只读答案（`resumed_read`） |

新增 `tests/test_b27_resume.py`（12 条）：身份稳定/可区分、`pack_chunks_with_ids`、schema 落盘、
老状态兼容、跳过已发、账本三种状态的闸门、**杀进程模拟**（`sending` 段在页面上→不重发；不在→补发；
全发过→只读不发）。

## 18. 修复第 5 项：Gemini 汇总新对话 + 会话 ID（2026-11-12 10:45）

`cmd_summarize` 改成：
1. 先 `start_new_chat(screen, "gemini")`，**没开成就不发**（返回 `new_chat_failed`）；
2. 发送段纳入 UI 锁（`iterate:summarize:gemini`）；
3. 收卷后读页面 URL，`session_id_of()` 存进两处：
   - `gemini_summary.json`：`{session_id, url, chars, stage, at}`
   - `state.json` 的 `steps.summarize.session_id`
4. 页面没有会话 id → `summary_not_isolated`（退出码 1），归档仍写但标记为不可信。

新增 `tests/test_b27_summary_isolation.py`（3 条）：新对话失败则不发送、会话 ID 落到归档、
无会话 ID 时判 `summary_not_isolated` 且如实记 `None`。

## 19. 修复第 1 项：DeepSeek 真读（2026-11-12 11:00）

**根因**：deepseek 的附件入口在 UIA 里是**空名 ButtonControl**，按名字永远找不到；
它的位置固定在输入框右下 35×34 图标处。

| 改动 | 文件 | 说明 |
|---|---|---|
| 按点位点附件 | `src/qi/upload.py` | `ATTACH["deepseek"]` 增 `point=(0.906, 1.48)`，按**输入框矩形**算（不写死屏幕坐标）；新增 `_composer_rect()` |
| 每端提问覆盖 | `tools/iterate.py` | `upload_prompt_by_site`：deepseek 看到 `docs/...` 会去仓库找再回 `NO_ACCESS`，给它一条「只读附件」的提问 |
| **安全闸门** | `tools/iterate.py` | `_clear_composer` 点完必须确认 `GetForegroundWindow()==target_hwnd`，否则**不发** Ctrl+A/Delete |

**安全事件**：定位点位时焦点落到桌面，`Ctrl+A/Delete` 选中了桌面 155 个图标并弹出「移到回收站」确认框。
**已按「否」取消；回收站近 40 分钟新增 0 条，没有删除任何东西。** 随即加上上面的焦点闸门。

**真机结果**：
- 上传：`ok 2.7s`；§8 自检 `token_found=true`（答案正是 `BRIDGE-UPLOAD-CHECK-20261111`）；
- 正式一席：`answered` → 文本兜底取回 2183 字；
- 验真：**3/4 = 0.75，core=true → true_read**（第 2 家真读）。

## 20. 规则变更：计划闸门关闭（2026-11-12 10:42，Herhons 批准）

改 `docs/self_iteration_spec.md`：
- **§10**：核实通过即施工，不再等签字；架构重构在「四家共识 + Gemini 汇总列高优先级」时放开；
  新增「四种必须停下等 Herhons 的情形」（花钱 / 账号登录注册 / 删除用户数据 / 动 v0.0.8）。
- **§12**：停机条件重写为 A 类（四种硬停）+ B 类（技术性停机，记录后结束循环，不等签字）；
  保留 5 轮上限。
- 顺带把 7 处指向旧闸门的表述同步成「见 §10」，并在 §10 写明它优先于 §5.4 的「大改须批准」，
  但**改规格文本本身**仍按 §5.4 处理。

边界：只改这两节及其指针，未动其他条款；`CALIBER.md` / `known_gaps.md` 未改。

## 21. Round 1 会审与施工（2026-11-12 12:00–14:00）

**验真口径再修**（会审本身暴露的）：
1. 语料从「只有规格」扩成「规格 + 本轮材料」，但 `core` 仍只认规格正文（新增 `core_text`）；
2. 切分支持五种真实排版：`1）` / `1.` / `整段 1. [方向]` / `A 稳定：` / grok 的 `②` 续条；
3. **分母漏洞**：原来把"没有引句的意见"整条排除，等于少写少错；改为只丢页眉，其余计未命中；
4. `SECTION_RE` 现在同时认 `§5.2` 与 `第 5.2 节`。

**会审**：四席齐（2087/1875/6392/3147 字），2 家真读（deepseek 5/5、gemini 5/5），
chatgpt 3/7 partial、grok 3/5 partial（引句取自材料 §3.1，core=false）。

**汇总**：Gemini 新开对话（`session_id=daba2074c268d3ef`），上传匿名包 + 短指令，2743 字。

**施工（S1–S4）**：
- `src/qi/safe_ui.py`：点位归属/黑名单/客户区判定 + 危险键前台与焦点双检；接进 `upload` 点位点击与 `_clear_composer`。
- `tools/iterate.py::_drop_prompt_echo`：剔除尾部提问回声（chatgpt 尾部混了 3 段）。
- `src/qi/session_anchor.py`：`stay/navigate/new_allowed/session_lost`，已 accepted 时**绝不新建**。
- `src/qi/drivers/__init__.py` + `tools/sisyphus.py`：总控骨架，按 plan.md 条目断点续跑、命中四种硬停即拒。

新增用例 20 条（safe_ui 9 / echo 3 / anchor 5 / sisyphus 3）；全量 **1631 OK**。

## 22. Round 2（代码轮）：会审 + 汇总 + P1–P4（2026-11-12 12:45–13:10）

**会审**（3 席：gemini 6114 / chatgpt 2050 / deepseek 3934 字）：
- deepseek 4/5 = **true_read**、gemini 5/6 = **true_read**、chatgpt 1/4 unread（引句取自实现片段，未落规格）。
- 切分又遇到两种新排版，已补进 `_candidates`：`^①`（chatgpt 用 ①–⑥ 当字段）、`【A. 标题】`（gemini）；
  并把选择规则从"第一个匹配"改成"块数最多且 ≤10"，解决跨轮排版漂移。

**汇总**：Gemini 新开对话（`session_id=2d744528ebfb94d6`），2329 字。
过程中又出现一次「假 accepted」（页面 /app、0 消息、附件未发却记 accepted），
按人工出口清账本后重跑成功——这条已记进 Round 2 待办（四阶段账本）。

**施工（P1–P4）**：
- P1 `safe_ui.decide_point` 增 `hit_top_hwnd`（同进程弹窗）与 `expected_rect`（布局漂移）校验；
  `point_verdict` 用 `GetAncestor(GA_ROOT)` 取顶层窗口。
- P2 `tools/iterate.py` 新增 `_session_plan` / `_remember_anchor` / `_navigate`：
  发送前按锚点决定 stay/navigate/new_allowed/session_lost；`session_lost` 直接不发；锚点写 `state.anchors`。
- P3 `_drop_prompt_echo` 对含「」/§/`文件:行号` 的块豁免，避免误删合规引文。
- P4 `sisyphus.py` 增 `verification_ok` 前置闸门（未核实拒跑，退出码 4）与 `MAX_ATTEMPTS=3`（三振，退出码 5）。

用例 +4（safe_ui 2、echo 1、sisyphus 1），全量 **1635 OK**。

## 23. STOP_20261112_1115 的三项下一步（2026-11-12 13:31 起）

STOP 已改名 `STOP_handled_20261112_1331.md`。

### 23.1 第 3 项（快照 SHA）——本来就已经修好

`prompt.md` 里的快照是 `6b049b32f20cc11010d3bf92e467fdeeff98d659`，
与 `round-0/snapshot.json` 一致，且 `git cat-file -e` 可达。STOP 写的是旧值 `2348121`。
**无需改动**，只在此记录核对结果。

### 23.2 第 2 项（DeepSeek 取答案靠兜底）——根因不是"完成检测弱"，是**锚点选错**

三轮会审都是 `stage=text_extracted`，即 `conversation.ask` 空转到 timeout。真机诊断（read-only）：

| 观察 | 值 |
|---|---|
| `busy`（`ds_stop` 模板） | 前 17 次读为真，之后为假——**不是**「停止图标常驻」 |
| `copies`（`ds_actions`/`ds_copy` 模板） | 全程 0（动作条要 hover，模板抓不到） |
| `prompt_nodes` | **2 个**（下标 1000 与 1217），编辑器在 **1177** |
| `uia_text_candidates` | **0** |

根因：`uia_text_candidates` 用「最后一个提问」当锚点、用编辑器当下界。
deepseek 把**历史提问预览**排在 composer 之后，于是 `start=1218 > end=1177`，切片恒空。
`answer_action_after` 同病，所以 `evidence_seen=false`。

修了四处（全部可离线测）：

| 改动 | 位置 | 说明 |
|---|---|---|
| 锚点只认编辑器**之前**的提问 | `conversation._prompt_indices` | 新增；`uia_text_candidates` 与 `answer_action_after` 共用 |
| 完成证据 | `conversation.answer_action_after` | 回答动作条按钮（朗读/复制/重新生成…）出现即视为生成完毕；`positional_ready(evidence=)` 允许在 `busy` 误报时放行 |
| 多段拼接 | `conversation.uia_text_join` | 原 `nodes[-1]` 只交最后一段；改为按视觉行回拼（同一行接起来，跨行换行） |
| 超时诊断 | `conversation._ask_impl` | timeout 结果带 `diagnostics{reads,copies_max,busy_reads,accepted,evidence_seen}` |

真机结果（同一标签页）：
- 长回答（8 段）静态取回：候选 67 → 54 段、2079 字，起点是答案而非提问回声，`evidence=1`；
- 新发一问走 `conversation.ask`：**`stage=answered`、7.9 秒、641 字**（此前必 timeout）。
- 兜底不再是唯一出路；`text_extracted` 只在真超时时才出现。

顺带修掉两个取回失真：

1. `read_answer_text` 原来丢 `len(text) < 8` 的节点——chatgpt 把「」渲染成**独立单字符节点**，
   于是每条引句都缺右括号、验真全判不中；改为只过滤噪声词，去重按「文本+位置」。
2. 切分补 `1.①`（点号后无空格）这种紧凑编号。

复算（同一批回复，只换取回/切分）：

| 轮 | 端 | 旧 | 新 |
|---|---|---|---|
| Round 2 | chatgpt | 1/4 unread（文本被拆断） | **1/7 unread**（7 块 = 1 页眉 + 6 条；引句本身是转述，覆盖率 0.36–0.69，判不中是诚实的） |
| Round 2 | deepseek | 4/5 | 4/5（不变） |
| Round 2 | gemini | 5/6 | 5/6（不变） |
| Round 1 | chatgpt | 3/7 | 3/7（不变） |

**结论：第 2 项完成。** 两家真读的结论没变；chatgpt 的 partial 是它自己在转述，不是测量错。

### 23.3 第 1 项（仍差 1 家真读）——补测拿到第三家，阈值没动

按 STOP 里唯一允许的那条路（"让某家再答一版更严格的引用"），对 **grok** 做了一次补测：
同一份 `round-2/materials/material.md`，提问要求「只依据第一章（规格）」「每条只用一处「」逐字抄
第一章原文，不得改写或删节」「禁止引用第二、三章」。

过程中修掉三个真 bug（都不是"教它怎么写"，而是取回端的问题）：

| 现象 | 根因 | 修法 |
|---|---|---|
| 上传 `dialog_stuck` | 传了**相对路径**，文件对话框按自己的当前目录解析 | `upload._absolute()`，入口统一转绝对路径 |
| `prompt_gone` | grok 把一条提问渲染成 **9~10 个节点**（含 `**` 独立节点），单块匹配判据整条断掉 | `_prompt_span()` 用**拼接后**的整段定位气泡起止；`**` 在合并时去掉 |
| 取回从半句提问开始 / 混杂过程行 | 60 字前缀只覆盖到气泡中段；grok 的 `Analyzing…`、`工作了 57s`、`畅所欲问` 也在树里 | 取「能匹配的最长前缀」+ 跳过残余提问碎片 + `is_progress_noise()` + 占位符过滤 |

结果：grok 回复 **2238 字 / 22 处引句**，验真 **5/6 = 0.83，core=true → true_read**。

**Round 2 因此凑齐 3 家真读**（deepseek 4/5、gemini 5/6、grok 5/6）。

必须写明的边界：
- 这一席是**补测**，不是原始 Round 2 批次的一部分（原始批次只有 gemini/chatgpt/deepseek）；
- 它的提问比原始批次更严（只许抄第一章），所以它拿到的"优势"来自提问措辞，不是口径放宽；
- **§5.3 的阈值与逐字口径一个字都没改**（`TRUE_READ = ≥3 命中且 ≥0.7`）。
- 仍未解决：`conversation.ask` 在 grok 上仍会判 `prompt_gone`（`prompt_nodes` 还是按单块匹配），
  这次是靠 `read_answer_text` 收的卷；下一轮应把 `_prompt_span` 接进收卷主路径。

## 24. STOP_20261111_2145 的未决清单（2026-11-12 14:31 起）

该 STOP 已改名 `STOP_handled_20261112_1431.md`。清单五项，逐条对现状核过：

| # | 清单项 | 现状 | 处置 |
|---|---|---|---|
| 1 | 完成检测（停止键消失 + copy 长度稳定） | **未解决到本次才修**：chatgpt 的复制键被长回答顶到 y=2061，窗口只到 1340，`copy()` 直接抛「光标没能移到」→ 整轮重试到 timeout | 已修，见 24.1 |
| 2 | gemini 清框（按位置找 contenteditable） | **已不是问题**：gemini 的输入框现在是带名字的 `EditControl`（"为 Gemini 输入提示"），`_clear_composer` 取第一个 EditControl 并夹进窗口矩形即可 | 真机复验，见 24.2 |
| 3 | deepseek/grok 换行 | **已绕开**：四端都不再粘长材料，`feed` = chatgpt/grok/deepseek `upload`、gemini `upload-script` | 无需改代码 |
| 4 | grok 上传（点附件后还要点菜单项） | **已修**：`ATTACH["grok"] = {"button": ("附件",), "menu": ("上传文件",)}`，附件是 `MenuControl` | 今天补测里 upload ok |
| 5 | prompt.md 快照 SHA | **已修**：现为 `6b049b32…`，与 `snapshot.json` 一致且可达（`4f1a848` 也仍可达，是被后续快照取代） | 无需改 |

### 24.1 第 1 项：动作条顶出视口 → 走 UIA 文本兜底

真机量到的因果链（chatgpt，Round 2 长回答）：

| 观察 | 值 |
|---|---|
| 复制按钮矩形 | `(-954,2061)-(-921,2094)` |
| 窗口矩形 | `(-1440,-560)-(1380,1340)` |
| `conversation.copy(copy键)` | `RuntimeError: 光标没能移到 (-938,2077)，实际停在 (-938,1906)` |
| `copies` | 1（不是 0，所以"没有复制键"的兜底分支根本进不去） |

改法（不靠滚动，避免打断页面状态）：

1. `rect_in_window(node, window)`：只要求「有一部分在窗口内」，完全在窗口外才算不可达；
2. 候选过滤：若**全部候选都不可达**，当作没有候选，`positional=True` 让 UIA 文本兜底接手，
   并记 `warnings=["actions_offscreen"]`；
3. `chatgpt` 加进 `UIA_TEXT_SITES`（它的树内文本完整、且不需要 hover）；
4. 噪声词补 `最新一条回复`、前缀补 `用时`。

真机验证（同标签页，新发一问要 8 段长回答）：

```
stage=answered, reply=2590字, basis=uia_text_after_prompt, source=uia_text,
warnings=["actions_offscreen"], elapsed=300.7s
```

此前同一形状必定 `stage=timeout` → `text_extracted`。**第 1 项完成。**

### 24.2 第 2 项：gemini 清框真机复验

```
before len: 10   (值是占位符「问问 Gemini」)
type 后:    17   'BRIDGE-CLEAR-TEST'
_clear_composer → True
after  clear: 10 (回到占位符)
```

清框有效；`composer_empty` 也把占位符认成空。无需改代码。

### 24.3 五项之外的顺带修复

- `conversation.rect_in_window` + 新增 `tests/test_b29_offscreen_actions.py`（7 条）；
- 未改任何阈值、未改规格；`docs/self_iteration_spec.md` 零改动。

## 25. Round 3（2026-11-12 15:05–16:15）

### 25.1 会审：gemini 全程拒答，三席成轮

| 端 | 字数 | 结果 |
|---|---:|---|
| chatgpt | 2097 | `answered`（出屏兜底在流程内生效） |
| deepseek | 4528 | `text_extracted` |
| grok | 2308 | `text_extracted`（`prompt_gone` 老问题） |
| gemini | 20 | **拒答**：「我是一个语言模型，这超出了我的设计用途」 |

gemini 的排查记录（都做了，不是没试）：
- 换措辞（中性「文档评审」）→ 同一句拒答；
- 换小文件（5 行、无害内容）→ 仍拒答；
- 换档位 `pro` → 被 `tier_guard` 挡回（review 只认 argon）；
- 新会话问「1+1 等于几」→ 发送挂住，30s 内无回复，页面停在 `/app`（0 消息）。
结论：该账号/模型进入拒答状态，**不是附件或措辞问题**。按 §7.3 用三席成轮。

### 25.2 验真口径又一处失真（这次影响三轮）

`_squash` 只去空白，而模型抄写时会**丢掉行内代码的反引号**：
`「现在的后果：`accepted`属于拦截集合…只能人工`submission_clear`。」` → 抄成
`…只能人工submission_clear。」`（其余逐字一致）。于是逐字正确的引句判不中。

修法：`_squash` 同时忽略 `` ` `` 与 `*`（**不动下划线**，那是文件名的一部分）。
三轮复算：

| 轮 | 端 | 旧 | 新 |
|---|---|---|---|
| Round 1 | chatgpt / deepseek / gemini / grok | 3/7 / 5/5 / 5/5 / 3/5 | **6/7 / 5/5 / 5/5 / 4/5** |
| Round 2 | chatgpt / deepseek / gemini / grok | 1/7 / 4/5 / 5/6 / 5/6 | **6/7 / 4/5 / 6/6 / 6/6** |
| Round 3 | chatgpt / deepseek / grok | 2/7 / 4/6 / 3/7 | **6/7 / 6/6 / 6/7** |

**必须更正我之前的判断**：Round 2 我说「chatgpt 在转述、判不中是诚实的」——**那是错的**。
当时用的是被截断的取回文本；重取后再叠加反引号问题，才得出错误结论。
两轮都只有 2 家真读的"事实"，其实一部分是测量造成的。

### 25.3 汇总：gemini 不可用，改由 chatgpt 代汇总（偏离已记录）

`cmd_summarize` 新增 `--site`：非 gemini 时走「附件 + 短指令」，同样**强制新开对话**、
同样把 `session_id` 与 `summarizer` 写进 `gemini_summary.json`。

- 第一次：`session_id=6af56c00-ef48-83ea-93c9-4f1b29366dd4`，1093 字；
- 第二次（清洗后重跑）：**复用了同一个会话**（session_id 未变）→ 修 `force_new`：
  汇总不允许走会话锚点；
- 第三/四次（强制新会话）：`user_took_over` / `interrupted`（用户开始使用机器，前台被抢），
  0 字。因此**保留第一次输出**，并在 `gemini_summary.json` 标 `pack_dirty=true`
  说明"该次附件含残渣，已在之后修掉"。

### 25.4 施工（P1–P7）

| ID | 内容 | 位置 |
|---|---|---|
| P1 | `accepted→unknown` 一次性降级：`page_empty` 必填 + `scope_mismatch`/`no_l3` 之一；保留原证据；仍拦截 | `src/qi/submissions.py::demote`、`tools/submission_demote.py` |
| P2 | 剪贴板写后**内容**核对 + **按归属**恢复（用户中途复制的新内容不被冲掉） | `src/qi/conversation.py` |
| P3 | 推进器入口清孤儿锁（失败只告警，不阻断——它不动 UI） | `tools/sisyphus.py` |
| P4 | 驱动边界 AST 检查：禁 import 发送栈/账本/锁，禁 `ask/reserve/pending` | `tests/test_b30_driver_boundary.py` |
| P5 | 提问碎片认领（grok 9~10 节点）：仅用于"提问还在不在" | `src/qi/conversation.py::prompt_fragments` |
| P6 | 匿名包去来源：`\b` 对中文边界失效 → 去掉边界；去说话人/残渣；`assert_anonymous` 泄漏即抛 | `src/qi/iteration.py` |
| P7 | 上传前校验文件存在（不存在时不要弹对话框） | `src/qi/upload.py` |

新增用例 24 条（demote 7 / clipboard 6 / driver 3 / fragments 4 / anonymity 4）；
同时把 `test_card09f`、`test_b19_ds_write` 里的假剪贴板改成"能写能读"（旧假实现无法表达真实剪贴板）。

### 25.5 遗留（Round 4）

1. R3-2 写账所有权（reservation_id）——会审三方都点了，但要动 `_Ledger` 与门禁接口；
2. R3-5 收卷主路径统一到 `_prompt_span`（现在只有取回侧用）；
3. R3-6 驱动实现迁移（第一步只抽 gemini upload）；
4. gemini 账号的拒答状态需要人工确认（这是唯一需要人看一眼的点，但不属于四种停机）。

## 26. Round 4 施工：承接 Round 3 的已核实遗留（2026-11-12 16:31 起）

**没有新开会审**，理由：R3-2 / R3-5 已在 `round-3/verification.md` 判为**成立**，
且三席有明确共识，按 §10「核实通过即施工」直接做。下一轮会审的对象就是这批改动。

### 26.1 R3-2 写账所有权

`_Ledger.adopt()` 原来只把 `reserved=True` 置真（"门禁说预约过我就信"），
过期回执会被当成本次预约，随后终态写账就会改掉旧记录。

| 改动 | 说明 |
|---|---|
| `submissions.adopt_owned()` | 记录必须存在；站点不同 → `site_mismatch`；两边都有 hwnd 且不同 → `hwnd_mismatch` |
| `_Ledger.adopt(written)` | 用当前视图算 scope 再核对归属；不通过就返回拒发（`tier_unconfirmed`），**不点击、不记账** |
| 调用点 | `_ask_impl` 收到拒发就原样返回，不再继续提交 |

**为什么只比到窗口级**：发送时新会话还没生成 `session_id`，`doc_rid` 也会因新开对话而变；
照 `reconcile()` 那套逐字段比会把**每一次正常首发**都拒掉（`test_rework09f_recovery`
当场就抓到了：期望 `code=1/submission_pending`，实际被我的严格版本判成 `scope_mismatch`）。
adopt 只需要挡住"上一轮/别的窗口的过期回执"，真正的严格比对留给恢复阶段。

### 26.2 R3-5 提问匹配规则收成一处

`conversation.merged_blocks()` / `conversation.prompt_span()` 成为唯一实现，
`tools/iterate.py` 的两个同名函数改为**委托**。此前两边各写一套，改一边忘另一边就会出现
"能定位不能取回 / 能取回不能定位"。

### 26.3 兜底不再自毁证据

`_ask_upload` 走 harvest/text_extracted 时会把 `ask` 的结果整个覆盖掉，
Round 3 的 grok 因此只剩 `stage=text_extracted`，看不出是 timeout 还是 prompt_gone。
现在覆盖前先留一份 `ask_failure`（`stage / timeout_kind / reason / diagnostics / warnings`）。

### 26.4 用例

新增 10 条：`test_b31_adopt_owned.py`(4)、`test_b31_ask_failure.py`(2)、
`test_b31_prompt_rule_shared.py`(3)，另在 `test_b31_adopt_owned` 里覆盖"只有 session 不同不阻断"。
全量 **1716 用例 OK**。

### 26.5 仍未做

- R3-6 驱动实现迁移（第一步只抽 gemini upload）——需要真机跑通再落；
- `status` 是否清锁的语义（本轮维持"只报告"）。

## 27. Round 4 会审：被 chatgpt 额度墙挡住（A 类停点）

材料与快照已就绪（`round-4/materials/material.md` 21k、`snapshot.json` = `820609ac`），
chatgpt 一席 582s 后 `stage=timeout`、0 字。真机页面上是：

```
升级订阅或添加额度，即可立即继续使用 Work；也可以等到用量在 明天 20:36 重置
```

**这不是收卷问题**：页面根本没有生成任何回答，是 `Work` 用量耗尽。
唯一的前进方式是升级/加额度 —— 属于 §10 的**花钱**，按 §12 A1 停轮，等 Herhons。

处置：
- 不升级、不购买、不换账号；
- 本轮会审暂停（round-4 目录保留材料/快照/提问，随时可续）；
- 未改动任何"绕过额度"的逻辑。

## 28. Round 4 会审归档失败 + B32（首轮落库误判）已修（17:00 起）

### 28.1 会审：3 家凑不齐，按 §7.3 归档

| 端 | 状态 | 证据 |
|---|---|---|
| chatgpt | **额度墙** | 页面「你暂时已用完工作用量 / 升级订阅或添加额度…明天 20:36 重置」；模型选择器只有 `GPT-6.1 Sol` 一档，模式只有 ChatGPT/Codex，没有不耗 Work 额度的入口 |
| gemini | **账号级拒答** | 新开会话问「用一句话描述春天」「今天适合散步吗」均回「因此没法帮到你」「我是一个文本 AI，这超出了我的能力范围」。侧栏历史里已有一串同类标题（拒审自迭代流程规格 / 拒绝简短回复 / 无法获取附件标题） |
| grok / deepseek | 可用 | — |

按 §7.3「至少 3 家真读才算齐」，本轮只有 2 家 → **Round 4 会审不成立**，不进汇总，
材料与快照保留在 `round-4/`（快照 `820609ac`）。没有降级标准、没有换端凑数、没有碰额度。

### 28.2 B32：首轮新会话「落库」被判成换会话

探活时抓到：gemini 新会话问一句，发送后地址一直是 `/app`，**第 16 秒**答案出来时
才变成 `/app/1bd58577…`。而 `_identity_broken` 一旦命中就判 `conversation_changed`，
可用的例外只有两条：`since_accept <= ACCEPT_NAV_GRACE_READS(=2)`（约 0.8 秒）
和 chatgpt 的 `/c/local-` 窄路径。读到第 40 次早超出窗口 → 明明发成功、答案在页面上，
却报 `document_changed`。

| 改动 | 说明 |
|---|---|
| `conversation._first_turn_landed()` | 按**路径形状**判落库，不看第几次读：`/app` → `/app/<id>`、`/c/local-…` → `/c/<uuid>`；同一容器下换到别的会话仍拒 |
| 收集主循环 | 改用新判据；**落库时答案已上屏就不重设基线**，否则重新锚定等于把刚到的答案划成历史，白等到超时 |

真机复验（gemini，新提问）：`stage=answered`（修复前是 `conversation_changed/document_changed`）。
离线：`tests/test_b32_first_turn_landed.py` 7 条。**旧代码红**（原样复现
`reason=document_changed, before=/app, now=/app/1bd58577…`）、新代码绿。
全量 **1723 用例 OK**。

### 28.3 本轮没做

- Round 4 会审（缺第三家，§7.3 不允许）；
- 会审有共识才能做的架构项（本轮没有新会审，无从产生新共识）。

## 29. Round 5 续：会审预算与额度墙（17:30 起）

两条都是"让下一次会审能跑通"的修复，不是新功能。规格依据分别是 §7.1 与 §12 精神
（不误判、也不白等）。

### 29.1 B33：`iterate.py` 绕过了会审超时下限

`tier_guard.floor_timeout()` 只在 `*_use.py` 的 CLI 入口生效，而 `tools/iterate.py`
是**直接**调 `conversation.ask` / `subprocess.run` 的。后果有两个，都是实测过的：

| 现象 | 后果 |
|---|---|
| `docs/iteration.json` 写 `review_timeout: 480`，`conversation.ask` 就在 480s 收工 | Round 4 的 chatgpt 一席跑 582s 报 `stage=timeout`、0 字 |
| 同一份配置交给 `gemini_use.py` 时子进程自己 floor 到 1500s | 父进程 `subprocess.run(timeout=budget+180)` 只有 660s，**先杀掉子进程** |

改法：新增 `_review_budget(cfg)`（过一次 `floor_timeout`），7 个调用点全部改走它；
`docs/iteration.json` 的 `review_timeout` 同步改成 900（仍会被 floor 到 1500）。
新增 `tests/test_b33_review_budget.py` 6 条，其中一条**扫源码**：谁再把裸
`cfg["review_timeout"]` 递给 ask/subprocess 就红。

### 29.2 B34：额度墙要快速失败，且不能留下"永久拦住"的假账

chatgpt 的 Work 额度用完后，页面写着「你暂时已用完工作用量 / 升级订阅或添加额度…」。
收卷侧看不出差别（没有回复键、也不 busy），于是白等到整档预算。更糟的是**账本**：
`_submit` 找不到可点的发送键 → `RuntimeError: 可用发送按钮: 找到 0 个控件` →
外层成 `interrupted` → 账本记 `unknown`（`BLOCKING_STATES`）→ **这条提问永久发不出去**，
只能人工清账，而实际上一个字节都没发。

| 改动 | 说明 |
|---|---|
| `conversation.quota_wall()` | 只认整句（`你暂时已用完工作用量` 等 4 条），且**有回复键就不判**；只对 chatgpt 生效 |
| 收集循环 | 命中即返回 `quota_exhausted`：没接受过就记 `not_sent`（可重试），接受过则**保留 accepted 不改口**（改口会造成重复发送） |
| `_submit` | 有额度墙证据且一个能点的发送键都没有 → `SendUnavailable`（`send_unavailable`，可重试态）。判据是 `not buttons` 而不是 `not named` —— 真机上"发送"按钮还在，只是 disabled |

**真机复验**（chatgpt，同一份 material）：
`stage=send_unavailable / submission=not_sent / sent=false`，
hint = `发送键不可用（1 个存在 / 0 个可用），且页面提示用量已用完：你暂时已用完工作用量`，
**12.1 秒**返回（修复前 103 秒，且留下一条 unknown 假账）。

离线：`tests/test_b34_quota_wall.py` 10 条。全量 **1739 用例 OK**。

### 29.3 已知残留

- 探活期间留下 3 条 chatgpt 账本记录（prompt 以「探活」开头），是**假账**
  （消息没发出去，但记成了 accepted/unknown）。它们不影响真实提问（账本按 prompt 哈希分键），
  按"删数据要停"的约定**没有删除**；额度恢复后如要重跑同一提问，先确认不撞这三条。
- `tools/iterate.py` 之外的 `flow.ask` 调用是否都带了 purpose，未逐一复核。

## 30. B35：子进程超时不再抛 traceback（17:50 起）

`tools/iterate.py` 有 4 处 `subprocess.run(..., timeout=…)` 没接 `TimeoutExpired`：
`_ask_upload_script`（会审）、`_choose_tier`（选档）、`_ask_paste`（分段粘贴）、
`cmd_summarize` 的 gemini 分支。子进程卡住时（UI 锁死、网页端不返回），整个
`review` / `summarize` 以 traceback 收场，**归档里一行结果都没有** —— 事后连
"是超时还是崩了"都分不清。和 `ask_failure` 同一个理由：失败也要留下可读的原因。

改法：新增 `_run_or_timeout(cmd, timeout, what)`，超时返回
`(None, {stage: "timeout", timeout_kind: "hard", reason: "subprocess_timeout", hint})`；
4 处全部改走它。第 5 处（`_relaunch_site` 的试点）原本就有 `try/except`，不动。

顺带修掉一个错配：`_ask_upload_script` 的 `budget` 以前取原始 `review_timeout`
（480），父进程 `timeout=budget+180=660` **比子进程自己 floor 后的 1500 还短**，
必然先杀掉子进程。B33 之后 `budget` 已是 floor 后的值，这条自然对齐。

新增 3 条用例（`test_b33_review_budget.py` 的 `SubprocessTimeout`）：
超时变结构化结果、正常路径原样透传、**扫源码**禁止再出现裸的
`subprocess.run(... timeout=...)`。全量 **1742 用例 OK**。

## 31. B36：锁用例的全局文件没隔离，会随机挡住推送（18:00 起）

推送 `efaa83f` 时 pre-push 报 `FAILED (failures=1)`：

```
FAIL: test_same_endpoint_excludes_but_does_not_block_the_ui_lock (test_b13_lock.TurnTests)
  self.assertTrue(started.wait(2))
AssertionError: False is not true
```

同一份代码在本地跑全量是绿的，日志开头是 `[lock] UI busy: owner=holder pid=…`。
根因不是并发逻辑，是**测试没隔离全局锁文件**：`uilock.lock_path()` 落在
`tier_guard.ROOT/runs`，上一条用例（或别的进程）留下的 `runs/turn/*.lock`
会让 holder 线程卡在 `turn(timeout=5)` 里，2 秒的 `started.wait(2)` 自然超时。
机器负载高时必现 —— 这条会**随机拦截每一次推送**。

改法：`TurnTests.setUp` 把 `uilock.ROOT` patch 到临时目录（`addCleanup` 清理）。
10 条用例全绿。

顺带说明：pre-push 里 `Ran 1666 tests` 比本地少 76 条，是因为钩子在 `public`
分支上跑，而 `git checkout <wip> -- tests` **只覆盖同名文件、不删除** public 独有
的测试。这个差异不影响判据（失败的是 public 也有的那条），但读数字时会困惑。

### 31.1 另一条环境性抖动（观察，未改）

同一次排查里还撞到一次：

```
FAIL: test_workflow_cli_mutation_shares_notify_lock_too
AssertionError: [TimeoutError('等 progress.json 的文件锁超过 5.0s')] is not false
```

单独连跑 3 次全绿，重跑全量也绿。`progress.LOCK_TIMEOUT = 5.0` 对真实场景是够的
（`workflow.move` 里的持锁是毫秒级），这次是**机器被别的任务压住**导致的调度饥饿。
**没有**改这个常量，也**没有**给这条用例加容差 —— 那是拿产品语义去迁就测试环境。
记在这里是为了下次再看到它时不用重新排查。
