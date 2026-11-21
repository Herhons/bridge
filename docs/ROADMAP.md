# Bridge 路线图（唯一权威主线）

> 本文件是 Bridge 的**唯一权威主线**。多主脑（`gemini` / `deepseek` / `grok`）与多执行器
> （`workbuddy` / `opencode` / `antigravity`）一律只依据本文件中未勾选的 `- [ ]` 项推进，
> 不得自行另立主线、不得跳过未完成项去做后面的活。
>
> 调度器 `scripts/auto_heal_loop.py` 把本文件列为 `BACKLOG_CANDIDATES` 的第一顺位；
> 一旦本文件不存在 `- [ ]` 待办（或正文出现 `ALL_DONE` 标记），状态机直接记 `ALL_DONE`
> 并优雅退出，不再空转。

---

## 1. 项目定位

Bridge 是运行在 **Windows 副屏（x 轴 `-1440..0`，竖屏 1440×2560）** 上的**多 AI 调度总线**：
把「规划」与「落地」拆成两层，用 UI 自动化把多家 AI 串成一条流水线，并在这条流水线之上
做**不依赖单一 AI 的自迭代系统**——自愈（红测试 → 报告 → 修复）、自排期（读本路线图取下一小步）、
自提交（全量单测为门禁，绿了才 commit/push）。

单一厂商限流、付费墙、改版、下线都不会让系统停摆：任一层失效即降级到下一层。

## 2. 红线（所有自动化必须遵守，不可协商）

- **双屏**：只在副屏 `-1440..0` 内操作窗口；**严禁抢占主屏（`0..2560` × `0..1440`）前台焦点**，
  严禁在本任务中打开或点击真实窗口。窗口移动一律 `SetWindowPos + SWP_NOACTIVATE`。
- **剪贴板**：**严禁污染系统剪贴板**。写入前必须备份、写入后必须无损还原（方案 A）。
- **单测**：**严禁删除、跳过或弱化任何既有单测断言**；任何改动后
  `py -3.10 -m unittest discover -s tests -v` 必须 `rc=0`（既有条数只增不减）。
- **Git**：严禁 `force push`、严禁删分支、严禁触碰 `v0.0.8` / `v0.0.9`。
- **运行态数据**：`runs/` 不入库；`ledger.json`、`state_machine*.json`、`dispatched/` 只作诊断用途。

## 3. 双层兜底架构

| 层 | 顺序 | 降级条件 |
| --- | --- | --- |
| **主脑**（规划 / 诊断） | `gemini -> deepseek -> grok` | 非零 rc、超时、空回执、缺少 `<<<BRIDGE_PATCH_*>>>` 或 `<<<WORKBUDDY_TASK_*>>>` 标记块 |
| **执行器**（落地） | `workbuddy -> opencode -> antigravity` | 空回执、非零 rc（含超时 124）、抛异常 |

同一张任务卡（`card_id = round + sha256(card)[:12]`）一旦处于 `dispatched` 或 `timed_out`，
**严禁盲目重派给下一个执行器**——先标记 `needs_review` 并上报，杜绝 WorkBuddy 与 Antigravity 并发双发。

## 4. 主线进度

- [x] **M1 / M2 / M3**：基础框架、11 端点驱动、模型策略与 `--profile`、doctor 自检、HTTP 只读服务
- [x] `engine/file_dialog.py` 拆分（从 `engine/base.py` 抽出文件对话框逻辑，行为不变）
- [x] OpenCode 调度修复与 5 轮基础闭环（截至 `585bdaa`，162/162 单测全绿）
- [x] **M4-1（P0）**：入库 `docs/ROADMAP.md`（唯一权威主线）
  - `bridge.py stop` / `bridge.py daemon-status` 一键急停与只读状态
  - `ALL_DONE` 立即优雅退出（本文件无 `- [ ]` 时不再空转 3 轮）
  - `--daemon` 正式命名（与 `--overnight` 等价，保留兼容）
  - `StateMachine` 原子落盘（`.tmp` + `os.replace`）与 PID 隔离（活着的调度器历史不被覆盖）
  - 任务卡幂等防双发（`runs/dispatched/<card_id>.json` 记录 `dispatched / completed / timed_out`）
- [x] **M4-2（P0.5）**：模型与驱动层速败
  - 第 1 批：`ledger` 超时解锁（`unknown` + `timeout`/`submit_not_verified` 可重试，不再 `duplicate rc=6`）、
    `bridge.py models --json` 统一、`opencode.EXE` 加 `CREATE_NO_WINDOW` 与额度速败标记 +
    `runs/opencode_cooldown.json`
  - 第 2 批：`run_cmd` / `git` / `kill_process_tree` 全部 `CREATE_NO_WINDOW`（彻底消掉 python.exe 黑框）；
    回落前先比对工作区指纹，上一级已写完代码但没回执时**停止回落**交单测门禁，杜绝双发；
    `auto` 链遇到冷却中的 opencode 记 `executor_skip=opencode reason=cooldown_active` 直接跳过
  - 阻断弹窗速败：`engine/base.py` 新增 `blocking_popup_patterns`（默认覆盖 `解锁 4 Carbon` /
    `US$99.99` / `升级到` / `Waiting for user input` / `Yes, allow this time` / `Allow check `，
    可按端点在 `config/endpoints.json` 扩展），`ask()` 在发写前与等待循环内命中即返回
    `error="blocking_popup"`，账本记 `failed_retryable` 不锁死
    （M4-3 已把误报的裸词 `Request Review` 移出词表：它是常驻按钮，不是弹窗）
  - Gemini `council` 从付费墙档位 `carbon` 改为 `flash`（`carbon` 仍保留在 `models` 中供 `--model` 手动使用）
  - 遗留：`deepseek / grok` 网页版 `--profile council` 下拉最高可用模型的**真机**校准仍需真实窗口，
    顺延到 M4-4
- [x] **M4-3（P1）**：剪贴板与窗口层收编
  - 剪贴板「方案 A」：`engine/uia.py` 新增 `backup_clipboard` / `restore_clipboard` /
    `preserved_clipboard()`，`_headed_paste_text`、`click_ratio_and_paste`、
    `upload_via_file_dialog` 三条粘贴路径全部改为先备份、`finally` 无损还原；
    非文本剪贴板（图片等）标记 `restorable=False` 而不是用空串覆盖
  - 破冰键入：端点配置项 `icebreaker_before_paste: true`，粘贴前发一次 space+backspace
  - 主屏焦点运行时门禁：`engine/window.py` 新增 `FocusGuard` / `foreground_hwnd` /
    `is_on_primary_screen`；`focus_window`、ratio 粘贴、composer 写入三处均纳入门禁，
    事件写 `warnings.focus_guard:primary_lost:0x..->0x..`，`focus_guard_strict: true` 时
    在提交前返回 `error="focus_taken"`
  - DeepSeek 去思考：`endpoints/deepseek.py` 新增 `strip_thinking_sections`，base 过滤前后
    各剥一次（`已思考（用时 N 秒）` 会被 base 噪声正则先删掉，故必须先剥再交给 base）
  - 副屏平铺：`tile_windows_on_secondary_screen()` 用 `SetWindowPos + SWP_NOACTIVATE |
    SWP_NOZORDER` 纵向平铺，坐标全部来自副屏 bounds（自动读 `right <= 0` 的显示器）
  - 真机验证（2026-11-21 13:20，证据 `runs/m4_3_attach_verify.txt`）：
    **剪贴板已验无损还原**（2598 字符逐字节一致）；**Gemini `--attach` 未闭环**——
    当时快照里 composer 与“上传和工具”按钮都不在 AX 树（可见区仅剩正文 + 打开边栏/设置），
    `attach_menu_item` 必然找不到，#32770 从未被触发，故“空框卡死”既未复现也未证伪
  - 另发现（如实记录）：`--headed` 的 `focus_window` 把主屏前台（斗鱼客户端 529300）抢到
    副屏 Gemini(332038)；本轮已把该调用纳入 `FocusGuard`，但 `focus_guard_strict` 默认关闭，
    故本次仍实际抢了焦点
- [x] **M4-GUI**：原生无黑框桌面控制台 bridge_gui.pyw（stdlib tkinter，副屏停靠、一键启停、主线进度、OpenCode 冷却与实时日志）
  - 纯逻辑层（`parse_roadmap` / `read_log_tail` / `read_opencode_cooldown` /
    `build_daemon_cmd` / `run_bridge_json` / `compute_secondary_geometry` / `status_badge`）
    全部可离线单测；`tkinter` 惰性导入，导入模块不建窗口
  - 子进程一律 `CREATE_NO_WINDOW`，守护进程 `CREATE_NO_WINDOW | DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP` 分离启动；关闭控制台不会带走调度器
  - 默认停在副屏左侧（`x < 0`，`480x700-500+40`），`SetWindowPos + SWP_NOACTIVATE`
    落地，不抢主屏焦点、不碰系统剪贴板
- [x] **M4-bugfix**：任务卡占位符自匹配根治（过夜调度 13:36 把 `...任务卡内容...` 当卡片派发）
  - `extract_task_card` / `extract_patch` 改为遍历 `finditer()` 取**最后一个非占位块**，
    `_is_placeholder_block()` 过滤空串 / `...任务卡内容...` / `...unified diff...` / 短省略号示例；
    全为占位块时返回 `""`
  - `build_task_prompt` / `build_gemini_prompt` 不再内嵌任何可被 `TASK_RE` / `PATCH_RE` 命中的
    示例块（标记名改写成文字说明），提示词自身永远不会被抽取
  - `reply_has_contract` 同步收紧：纯回声不再算合法回执，脑链继续回落到下一个端点
- [ ] **M4-4（P0）**：Gemini 附件真机闭环 + 焦点严格门禁默认化
  - composer 可见性前置检查（滚到底/等待输入区就绪）后再触发 `--attach`，把 `#32770`
    空框问题一次验穿，证据补进 `runs/m4_3_attach_verify.txt`
  - 真机回归统一开启 `focus_guard_strict: true`，让抢主屏焦点直接失败而非静默发生
  - 真机校准 `deepseek / grok` 的 `--profile council` 最高可用模型（`available_options=[]`）
  - 本轮已完成（离线，已验）：`focus_guard_strict` 默认 `True`（`config/bridge.json` 显式写死，
    调用方可用端点配置覆盖）；`--headed` 进入交互前先探主屏前台，有主屏前台时**不再调用**
    `focus_window`，只在确实夺焦时按严格门禁返回 `error="focus_taken"`；新增
    `_ensure_composer_ready()`（滚底 + 重采，仍缺则结构化失败）；attach 按钮/菜单点击纳入
    `guarded_write`，夺焦即在提交前中止。新增 12 条离线单测
    `tests/test_m4_4_focus_and_attach_ready.py`，全量 295 条 rc=0
  - 本轮被阻塞（未验，原因已实测）：① Gemini「上传和工具」按钮**没有 InvokePattern**，
    headless（`headless_default=true`，命令未带 `--headed`）点击即 `POLICY_DENIED`，
    `#32770` 因此从未被触发——这是 M4-3 “空框未复现”的上游真因；`--headed` 的物理点击会
    激活副屏窗口并抢走主屏前台，严格门禁下只会返回 `focus_taken`，需用户让出主屏或显式
    关闭门禁方可重跑；② `deepseek / grok` 在非前台状态下 AX 树只暴露 1 个 `DocumentControl`，
    无 `model_selector` 节点，展开下拉必须激活窗口，故 council 校准未做、配置未改（不编造）
  - 本轮事故（如实记录）：13:44 只读探针用 `uia:invoke` 让 DeepSeek 窗口成为前台
    （前台 529300→2362972），0.4s 内已还原为 529300；此后改为一律只读快照
  - 副发现（未修）：`bridge.py status` 对 `deepseek / grok` 报 `ready=true`，实际是整页
    `DocumentControl` 被 `_find_composer` 当成 composer 的假阳性，建议下一轮收窄角色/名字

## 5. 维护约定

1. 只有**全量单测绿 + 已提交**的项才可以改成 `- [x]`；半成品一律保持 `- [ ]`。
2. 新增工作项必须写进本文件再动手，禁止只写在 `runs/` 的临时 todo 里。
3. 子说明用普通 `- ` 列表（**不要**写成 `- [ ]`），否则会被计为未完成主线项。
4. 全部主项收口后，在本文件末尾另起一行写 `ALL_DONE`，调度器会立即优雅退出。
