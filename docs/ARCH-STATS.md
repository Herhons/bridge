# Bridge v0.0.9 — 代码关系统计

> 由 `tools/arch_stats.py` 自动生成，**只读**（不导入、不执行、不碰 UI）。生成时间：2026-11-11 15:26 +0800
> 重新生成：`env -u PYTHONPATH py -3.10 -X utf8 -B tools/arch_stats.py`

## 0. 口径

| 指标 | 口径 | 已知偏差 |
|---|---|---|
| 代码行 | `tokenize` 实际 token 行，剔除空行、纯注释行、独占行的字符串 | 多行坐标表/提示词被算作字符串 → **低估** |
| 依赖边 | AST `Import`/`ImportFrom`，只保留仓库内部名字 | 同名跨包按名字匹配 → 边数偏**高** |
| 相似度 | `difflib.quick_ratio()` | 上界近似，不是真实重复率 |
| 测试覆盖 | 测试源码里是否出现模块名 | **静态猜测**，不代表执行到 |

## 1. 规模

| 目录 | 文件数 | 总行 | 代码行 | 空行 |
|---|---:|---:|---:|---:|
| `src/qi` | 37 | 16928 | 11753 | 2376 |
| `src/scripts` | 20 | 13623 | 9634 | 1840 |
| `tools` | 32 | 2974 | 2247 | 484 |
| `tests` | 116 | 25328 | 19615 | 4501 |
| `src/chrome-extension` | 4 | 1600 | 207 | 101 |

- 测试：**116** 个 `test_*.py`，**1563** 个用例。
- 模块：`src/qi` **36** 个、`src/scripts` **20** 个。
- 依赖边：`qi` 内部 **127** 条；`scripts` → `qi` **96** 条；`scripts` → `scripts` **1** 条。

## 2. 分层

```text
Chrome 扩展 (src/chrome-extension/*.js)
        │ 注入 / 侧栏 → 由 qi.browser 准备可操作窗口
        ▼
qi.server  ── /ask 校验、预算、quorum、证据落盘、子进程调度
        │ 每个 endpoint 依次跑
        ▼
src/scripts/*_choose.py   → 只选档（写 tier_guard 回执）
src/scripts/*_use.py      → 选档 + 提问 + 收回答（发送前读回执）
        │ 共用
        ▼
qi/{uilock → 整轮前台锁} → qi/{screen,ui,window,browser} 定位与输入
qi/{conversation,flow,desk_flow} 会话与回复归属
qi/{tier_guard,sendgate,chip} 档位闸门    qi/{guard,human,zoom} 用户占用与人机验证
        │ 读写
        ▼
docs/tiers.json 档位表 · docs/bridge_server.json 服务配置 · runs/*.jsonl 运行日志
```

## 3. 模块清单（`src/qi`）

按**全仓入度**（被多少模块 import，含 `src/scripts` 的引用）降序；入度 0 = 叶子或入口。

| 模块 | 入度 | 职责（docstring 首行） |
|---|---:|---|
| `uilock` | 26 | 跨进程 UI 前台锁（CARD-09a 第 6 条）。 |
| `tier_guard` | 19 | 档位回执：`*_choose` 读回确认后落一张凭证，`*_use` 发送前必须拿它对上（CARD-06a (a)）。 |
| `ui` | 17 | Named UI controls for binding, input and reply ownership. |
| `window` | 15 | 窗口最小化 → 恢复，并且**用实测结果**确认真的恢复了（CARD-06a (c)）。 |
| `console` | 14 | 控制台的入口设置：UTF-8 输出 + 打印前去掉零宽字符（CARD-08 第 1 条）。 |
| `guard` | 12 | 用户占用保护：人在用电脑时，脚本不抢。 |
| `desk` | 11 | 桌面端内核：按**进程名**找窗口，启动/复用，置前。 |
| `failure` | 11 | 失败自动留证（CARD-06b 第 9 条）。 |
| `runlog` | 10 | 每次运行追加一行到 runs\YYYYMMDD.jsonl（2026-10-30 第4项）。 |
| `exit_codes` | 9 | 统一退出码与 stage 映射（CARD-06a (d)）。 |
| `screen` | 9 | 找图内核：截屏 → 找图 → 点/输入/读剪贴板。 |
| `zoom` | 9 | 页面缩放（CARD-06b 第 6 条）：非 100% 就先按 Ctrl+0，仍不是 100% 就退出。 |
| `browser` | 6 | 准备一个可操作的 Chrome 窗口。 |
| `conversation` | 6 | One submission, bound to its newly observed user message and |
| `human` | 6 | 人机验证 / 未知错误检测（CARD-06b 第 8 条）。 |
| `textcmp` | 6 | 比对用的输入归一（CARD-06b 第 5 条 / CARD-09e 第 5 条）。 |
| `heartbeat` | 5 | 任务进度心跳：让面板在"等回答"的十几分钟里看得见东西。 |
| `progress` | 5 | progress.json 读写（任务卡 01 / 计划第 7 项：监工入口）。 |
| `sendgate` | 5 | 发送前的最后一刻门禁（CARD-06b 第 3/8 条）。 |
| `submissions` | 4 | Durable send-intent guard. Unknown/accepted attempts are nev |
| `chip` | 3 | 发送前的档位只读复核（CARD-06b 第 1/2 条）。 |
| `choose` | 2 | 选择类脚本的通用流程：写入器模式 → 打开菜单 → 点某一项 → 复核。 |
| `reject` | 2 | 端点"拒绝这一轮"的结构化判据。 |
| `server` | 2 | Bridge v0.0.9 本地后端（CARD-09a）。 |
| `workflow` | 2 | 工作流状态机（任务卡 05，第 5 项后端部分）。 |
| `audio` | 1 | 判断 Chrome 有没有在放视频/音频。 |
| `autopush` | 1 | 自动推：WorkBuddy 停下来时补一句话（PLAN 第 7 项子卡 CARD-07b，CARD-08 第 2 条落地 |
| `desk_flow` | 1 | 桌面端公用流程：提问 → 收回答。 |
| `gemini` | 1 | gemini 网页端专用：按控件树判「发出去了」和「答完了」。 |
| `lastreply` | 1 | 「按页面标记取最后一条助手回复」——ChatGPT 的 UIA 兜底（CARD-09e 第 3 条）。 |
| `replycodes` | 1 | 采集停摆的显式错误码（T3-1）。 |
| `restart` | 1 | UIA 文档丢失后的「重启一次」通道（CARD-09e 第 2 条）。 |
| `desk_sites` | 0 | 四个桌面端的配置。所有坐标都是**窗口内 1380x1340 下、相对发送键圆心**的偏移。 |
| `flow` | 0 | 通用「提问 → 收回答」流程。四个网页端共用这一份，差异全部压进 Site 配置。 |
| `library` | 0 | 资源库：只管"有哪些图、缺哪些图"。 |
| `workbuddy` | 0 | WorkBuddy 的选档与收发（按控件名 / UIA 树定位，不用模板）。 |

### 3.1 枢纽（全仓入度 ≥ 10）

| 模块 | 入度 | 说明 |
|---|---:|---|
| `uilock` | 26 | 跨进程 UI 前台锁（CARD-09a 第 6 条）。 |
| `tier_guard` | 19 | 档位回执：`*_choose` 读回确认后落一张凭证，`*_use` 发送前必须拿它对上（CARD-06a (a)）。 |
| `ui` | 17 | Named UI controls for binding, input and reply ownership. |
| `window` | 15 | 窗口最小化 → 恢复，并且**用实测结果**确认真的恢复了（CARD-06a (c)）。 |
| `console` | 14 | 控制台的入口设置：UTF-8 输出 + 打印前去掉零宽字符（CARD-08 第 1 条）。 |
| `guard` | 12 | 用户占用保护：人在用电脑时，脚本不抢。 |
| `failure` | 11 | 失败自动留证（CARD-06b 第 9 条）。 |
| `desk` | 11 | 桌面端内核：按**进程名**找窗口，启动/复用，置前。 |
| `runlog` | 10 | 每次运行追加一行到 runs\YYYYMMDD.jsonl（2026-10-30 第4项）。 |

## 4. 模块清单（`src/scripts`）

| 模块 | 入口 | 入度 | 职责 |
|---|---|---:|---|
| `bridge_server` | 是 | 0 | 本地后端 server 的生命周期命令（CARD-09a）。 |
| `chatgpt_choose` | 是 | 1 | chatgpt 网页版：选模式、选模型、调推理强度。不发提问。 |
| `chatgpt_use` | 是 | 0 | chatgpt 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。 |
| `codex_choose` | 是 | 0 | codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 读/调强度。 |
| `codex_use` | 是 | 0 | codex（Codex 桌面端 / ChatGPT.exe）：选模型 + 调强度 + 提问收回答。 |
| `deepseek_choose` | 是 | 0 | deepseek 网页版：只做选择，不发提问。 |
| `deepseek_use` | 是 | 0 | deepseek 网页版：提问 → 收回答。流程在 qi/flow.py，这里只放本端配置。 |
| `dsh_choose` | 是 | 0 | dsh（DeepSeek Harness 桌面端 / DeepSeek Harness.exe）：选模型 + 调强度。 |
| `dsh_use` | 是 | 0 | dsh（DeepSeek Harness 桌面端 / DeepSeek Harness.exe）：选模型 + 调强度 + |
| `gemini_choose` | 是 | 0 | gemini 网页版：只做选择，不发提问。 |
| `gemini_use` | 是 | 0 | gemini 网页版：提问 → 收回答。判据在 qi/gemini.py，这里只放本端配置。 |
| `grok_choose` | 是 | 0 | grok 网页版：只做选择，不发提问。 |
| `grok_use` | 是 | 0 | grok 网页版：提问 → 收回答；--read 只读最新回复。 |
| `notify_done` | 是 | 0 | 写完 progress.json，再发一条完工通知（任务卡 01）。 |
| `opencode_choose` | 是 | 0 | opencode（OpenCode 桌面端 / OpenCode.exe）：选模型 + 调强度 + 读当前状态。 |
| `opencode_use` | 是 | 0 | opencode（OpenCode 桌面端 / OpenCode.exe）：选模型 + 调强度 + 发消息。 |
| `workbuddy_autopush` | 是 | 0 | 自动推：WorkBuddy 停了就补一句「继续按卡施工」（CARD-08 第 2 条 / PLAN 第 7 项子卡 CA |
| `workbuddy_choose` | 是 | 0 | WorkBuddy：--list 读当前模型；--model 完整模型名 切换并复核。 |
| `workbuddy_use` | 是 | 0 | WorkBuddy：默认新任务；--continue 继续当前任务，提交一次并读取对应回复。 |
| `workflow_use` | 是 | 0 | 工作流状态机命令行（任务卡 05，第 5 项后端部分）。 |

## 5. 依赖边

### 5.1 `src/qi` 内部（按被依赖方分组）

- **`ui`** ← `browser`, `chip`, `choose`, `conversation`, `flow`, `gemini`, `lastreply`, `restart`, `sendgate`, `workbuddy`, `zoom`
- **`screen`** ← `browser`, `choose`, `conversation`, `desk_flow`, `failure`, `flow`, `gemini`, `library`, `workbuddy`
- **`window`** ← `browser`, `choose`, `desk`, `failure`, `flow`, `gemini`, `screen`, `ui`, `workbuddy`
- **`exit_codes`** ← `flow`, `gemini`, `human`, `tier_guard`, `ui`, `uilock`, `workbuddy`, `zoom`
- **`tier_guard`** ← `choose`, `conversation`, `flow`, `gemini`, `sendgate`, `server`, `submissions`, `workbuddy`
- **`uilock`** ← `browser`, `conversation`, `desk`, `gemini`, `screen`, `server`, `ui`, `workbuddy`
- **`guard`** ← `choose`, `conversation`, `desk_flow`, `flow`, `gemini`, `screen`, `workbuddy`
- **`browser`** ← `choose`, `conversation`, `flow`, `gemini`, `screen`, `ui`
- **`conversation`** ← `desk_flow`, `flow`, `gemini`, `lastreply`, `submissions`, `workbuddy`
- **`runlog`** ← `desk_flow`, `failure`, `flow`, `gemini`, `ui`, `workbuddy`
- **`desk`** ← `desk_flow`, `restart`, `screen`, `server`, `workbuddy`
- **`human`** ← `conversation`, `flow`, `gemini`, `sendgate`, `workbuddy`
- **`textcmp`** ← `conversation`, `gemini`, `lastreply`, `reject`, `workbuddy`
- **`failure`** ← `choose`, `flow`, `gemini`, `workbuddy`
- **`heartbeat`** ← `conversation`, `gemini`, `server`, `workbuddy`
- **`sendgate`** ← `conversation`, `flow`, `gemini`, `workbuddy`
- **`zoom`** ← `chip`, `choose`, `flow`, `gemini`
- **`progress`** ← `autopush`, `server`, `workflow`
- **`submissions`** ← `conversation`, `gemini`, `workbuddy`
- **`chip`** ← `gemini`, `sendgate`
- **`reject`** ← `conversation`, `workbuddy`
- **`audio`** ← `browser`
- **`choose`** ← `chip`
- **`console`** ← `autopush`
- **`desk_flow`** ← `desk_sites`
- **`lastreply`** ← `flow`
- **`replycodes`** ← `workbuddy`
- **`restart`** ← `workbuddy`
- **`workflow`** ← `server`

### 5.2 `src/scripts` → `src/qi`（每个 CLI 依赖了哪些内核模块）

- **`bridge_server`** → `console`, `server`, `uilock`
- **`chatgpt_choose`** → `chip`, `console`, `failure`, `guard`, `tier_guard`, `ui`, `uilock`, `zoom`
- **`chatgpt_use`** → `console`, `uilock`
- **`codex_choose`** → `desk`, `tier_guard`, `uilock`, `window`
- **`codex_use`** → `desk`, `failure`, `runlog`, `tier_guard`, `ui`, `uilock`, `window`
- **`deepseek_choose`** → `console`, `failure`, `guard`, `tier_guard`, `uilock`, `zoom`
- **`deepseek_use`** → `console`, `uilock`
- **`dsh_choose`** → `desk`, `tier_guard`, `uilock`, `window`
- **`dsh_use`** → `desk`, `failure`, `runlog`, `tier_guard`, `uilock`, `window`
- **`gemini_choose`** → `console`, `failure`, `guard`, `tier_guard`, `ui`, `uilock`, `zoom`
- **`gemini_use`** → `console`, `gemini`, `uilock`
- **`grok_choose`** → `choose`, `console`, `guard`, `uilock`, `zoom`
- **`grok_use`** → `console`, `exit_codes`, `failure`, `guard`, `heartbeat`, `human`, `runlog`, `sendgate`, `submissions`, `textcmp`, `tier_guard`, `ui`, `uilock`, `zoom`
- **`notify_done`** → `progress`
- **`opencode_choose`** → `desk`, `tier_guard`, `ui`, `uilock`, `window`
- **`opencode_use`** → `desk`, `failure`, `runlog`, `tier_guard`, `ui`, `uilock`, `window`
- **`workbuddy_autopush`** → `autopush`, `console`, `progress`, `server`, `tier_guard`, `uilock`
- **`workbuddy_choose`** → `console`, `uilock`
- **`workbuddy_use`** → `console`, `uilock`
- **`workflow_use`** → `console`, `workflow`

### 5.3 `src/scripts` 内部

- `chatgpt_use` → `chatgpt_choose`


### 5.3 双向依赖（互相 import）

- `browser` ⇄ `screen`
- `browser` ⇄ `ui`
- `conversation` ⇄ `submissions`

> 双向 import 在 Python 里通常靠函数内 import 或延迟求值规避，不一定致命；
> 但它意味着两个模块的职责边界有重叠，是后续拆分的候选点。

## 6. 重复代码（choose / use 成对）

| 对 | quick_ratio | 同名函数 | 仅前者 | 仅后者 |
|---|---:|---:|---:|---:|
| `dsh_choose` / `dsh_use` | 0.94 | 35 | 1 | 1 |
| `codex_choose` / `codex_use` | 0.93 | 35 | 4 | 7 |
| `opencode_choose` / `opencode_use` | 0.91 | 42 | 1 | 2 |
| `grok_choose` / `grok_use` | 0.23 | 1 | 1 | 23 |
| `gemini_choose` / `gemini_use` | 0.35 | 1 | 15 | 1 |
| `deepseek_choose` / `deepseek_use` | 0.38 | 1 | 7 | 0 |

> `quick_ratio` 是上界近似；0.9 以上基本可以确认是**整份复制后改**。

## 7. 测试覆盖（静态猜测）

`src/qi` 中被测试源码**完全没提到**的模块（1 个）：

```text
reject
```

> 判据只是「测试文件里出现过这个名字」，**不代表真的覆盖**；反过来，名字没出现
> 也不等于没被间接测到（可能通过上层模块进入）。

## 8. 入口点

`src/scripts` 里带 `__main__` 的入口（20 个）：

```text
bridge_server chatgpt_choose chatgpt_use codex_choose codex_use deepseek_choose deepseek_use dsh_choose dsh_use gemini_choose gemini_use grok_choose grok_use notify_done opencode_choose opencode_use workbuddy_autopush workbuddy_choose workbuddy_use workflow_use
```

