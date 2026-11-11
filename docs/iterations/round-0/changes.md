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
