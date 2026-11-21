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
- [ ] **M4-2（P0.5）**：模型与驱动层速败
  - 校准 `deepseek / grok / gemini` 的 `--profile council` 真实可用最高模型
    （修复 Gemini `carbon` 撞 US$99.99 付费墙、DeepSeek/Grok `available_options=[]`）
  - 驱动层加阻断弹窗（付费墙 / 升级 / 权限确认）速败与短时无回执 fail-fast（不再死等 300s）
  - 修复 `ledger.json` 超时 `unknown` 误锁重试（`duplicate rc=6`）
  - 统一 `bridge.py models --json` 参数
  - `opencode.EXE` 加 `CREATE_NO_WINDOW` 与额度冷却跳过
  - Antigravity `Request Review` 弹窗检测速败
- [ ] **M4-3（P1）**：剪贴板与窗口层收编
  - 剪贴板「方案 A」：粘贴前备份、粘贴后无损还原，破冰键入后再粘贴（防输入框未激活 bug）
  - 主屏 `GetForegroundWindow()` 防抢焦点运行时门禁
  - 真机验证 `#32770` 修复后的 Gemini `--attach` 闭环
  - DeepSeek 过滤思考过程（CoT），只取最终正文
  - 副屏 `SetWindowPos + SWP_NOACTIVATE` 无感平铺收编进 `engine/window.py`

## 5. 维护约定

1. 只有**全量单测绿 + 已提交**的项才可以改成 `- [x]`；半成品一律保持 `- [ ]`。
2. 新增工作项必须写进本文件再动手，禁止只写在 `runs/` 的临时 todo 里。
3. 子说明用普通 `- ` 列表（**不要**写成 `- [ ]`），否则会被计为未完成主线项。
4. 全部主项收口后，在本文件末尾另起一行写 `ALL_DONE`，调度器会立即优雅退出。
