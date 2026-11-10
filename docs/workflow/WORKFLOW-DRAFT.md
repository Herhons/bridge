# Bridge v0.0.9 工作流模式草稿（骨架）

> 2026-10-31 红莉栖起草。PLAN.md 第 5 项写“草稿已有”，但在 F:\Bridge 各版本、桌面、文档里都没找到原稿（只有 v0.0.2 的《02-架构与工作流程规范.md》和 v0.0.5 一个空的 bridge-workflow.json），所以先按 PLAN 写这份骨架。<host> 如果找到原稿，以原稿为准。

## 1. 目标
把“计划 → 施工 → 验收 → 通知”的流程变成 v0.0.9 里的一个工作流模式，第 6 项的插件面板直接用它。

## 2. 角色（席位）
- 计划/监工：红莉栖
- 施工：WorkBuddy（DeepSeek-V4.1-Flash）
- 验收：Codex（GPT-6.1 Sol 最高档），只看卡和成品

## 3. 步骤（状态机）
draft → sent → running → done/failed/stuck → verifying → pass / block / hold
- block：自动生成修复卡，回到 sent
- hold：列出缺的证据，补齐后再 verifying
- 同一张卡连续 3 次没进展：停下来叫监工

## 4. 数据
- 任务卡：cards\CARD-xx-*.md（做什么 / 怎样算完成 / 允许改 / 禁止碰）
- 进度：progress.json（card_id, status, note, updated_at）
- 运行日志：runlog
- 证据：evidence\

## 5. 已定（2026-11-01，CARD-05 落地）

- **格式 = JSON**：`docs\workflow\workflow.json`，纯 JSON，不引新依赖。**跳转表以这份文件为准**，
  代码里不写死；状态列表、终态、`max_no_progress`、三个席位也都在里面。
  ```json
  {"version": 1,
   "states": ["draft","sent","running","done","failed","stuck","verifying","pass","block","hold"],
   "terminal": ["pass"],
   "transitions": {"draft": ["sent"], "sent": ["running","stuck"],
                   "running": ["done","failed","stuck"], "done": ["verifying"],
                   "failed": ["sent","draft"], "stuck": ["sent","draft"],
                   "verifying": ["pass","block","hold"], "block": [], "hold": ["verifying"],
                   "pass": []},
   "max_no_progress": 3,
   "seats": {"plan": "红莉栖", "build": "workbuddy", "verify": "codex"}}
  ```
- **状态机实现在 `src\qi\workflow.py`**（纯标准库，离线可测），命令行是
  `src\scripts\workflow_use.py`（`status` / `move` / `advance` / `resolve-hold`，
  `--file` 必填、没有默认值）。进度数据只写在 `progress.json` 顶层的 `workflow` 键下，
  原有 7 个顶层字段不动。
- **面板要展示哪些字段、提供哪些按钮**：第 6 项定（本卡只做后端，不涉及界面）。
- **通知渠道**：沿用 `notify_done.py`（`BRIDGE_NOTIFY_URL`），不另开通道。
