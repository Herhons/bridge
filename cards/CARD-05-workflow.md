# CARD-05 工作流模式放进 v0.0.9（第 5 项，后端部分；面板是第 6 项，不在本卡）— r2
依据：docs\workflow\WORKFLOW-DRAFT.md（骨架；原稿没找到，以骨架为准）。
r2 说明：2026-11-01 会审（Gemini、Grok 两家独立厂商）都给 REVISE，意见原文在 evidence\review_card05_gemini_read_1041.txt、evidence\review_card05_grok_read_1047.txt。本版把跳转表、推进规则、数据形状、退出码和文件范围全部写死，施工照本卡做，不要自己发挥。

## 做什么
1. 新建 docs\workflow\workflow.json（纯 JSON，不引新依赖），结构固定为：
   {"version": 1,
    "states": ["draft","sent","running","done","failed","stuck","verifying","pass","block","hold"],
    "terminal": ["pass"],
    "transitions": {
      "draft": ["sent"],
      "sent": ["running","stuck"],
      "running": ["done","failed","stuck"],
      "done": ["verifying"],
      "failed": ["sent","draft"],
      "stuck": ["sent","draft"],
      "verifying": ["pass","block","hold"],
      "block": [],
      "hold": ["verifying"],
      "pass": []},
    "max_no_progress": 3,
    "seats": {"plan": "红莉栖", "build": "workbuddy", "verify": "codex"}}
   说明：只有 done 能进 verifying；failed/stuck 由监工决定重派（sent）或改卡（draft）；pass 是终态；block 本身不再跳转，见第 3 条。seats 只是记录，本卡代码不用它做任何动作。
2. 新建 src\qi\workflow.py：纯标准库，离线可测，不操作任何窗口、不发消息、不调用任何 CLI。
   - workflow.json 的路径默认按 Path(__file__) 向上推到项目根再拼 docs\workflow\workflow.json，函数参数可覆盖（测试用）。
   - move(card, new_state)：按跳转表检查，非法就抛 WorkflowError（消息格式固定为 `非法跳转: <卡号> <旧状态> -> <新状态>`），不改任何文件。同状态 move 也算非法。
   - advance(card)：表示“监工推了一次”。卡的 no_progress 计数 +1；任何一次成功的 move 把计数清 0。计数达到 max_no_progress（3）时，若当前状态允许去 stuck 就转 stuck，返回 {"need_supervisor": true}；否则只返回 need_supervisor=true、不改状态。
   - block：move 到 block 时自动生成修复卡：新卡号 = 原卡号 + 下一个空闲小写字母后缀（05→05b→05c…，跳过已存在的），新卡状态 sent、记 parent=原卡号；原卡停在 block 不再动。
   - hold：move 到 hold 必须带 missing 证据清单（字符串列表，非空）；resolve_hold(card) 清空清单后自动 move 回 verifying；清单非空时 move hold→verifying 拒绝。
3. progress.json 数据形状：现有顶层字段（card_id、status、note、updated_at、running、verdicts、queue）一个都不能丢、类型不变；新数据只写在顶层 "workflow" 键：
   {"workflow": {"cards": {"05": {"state": "...", "no_progress": 0, "parent": null, "missing": [], "history": [{"from": "...", "to": "...", "at": "ISO8601+08:00"}]}}}}
   当 move 的卡号等于顶层 card_id 时，同步把顶层 status 改成新状态、updated_at 改成当前时间；其他卡只改 workflow 键。status 子命令以 workflow.cards 为准，没有该卡时回落到顶层 status。
   写之前先把原文件复制到 _backup_<当天yyyymmdd>\<文件名>.bak-<HHMMSS>（目录不存在就建），写用“临时文件 + os.replace”原子替换。编码 UTF-8 无 BOM，读取兼容 BOM。
4. 新建 src\scripts\workflow_use.py，参数 --file <progress 路径>（必填，没有默认值，防止误写真实文件）和 --workflow <json 路径>（可选）：
   - status：打印每张卡及状态，退出 0。
   - move <卡号> <新状态> [--missing 项1 --missing 项2 …]：合法退出 0，非法跳转退出 2。
   - advance <卡号>：退出 0；需要监工时额外打印一行 `需要监工: <卡号>` 且退出 3。
   - resolve-hold <卡号>：退出 0；卡不在 hold 退出 2。
   - 其他错误（文件不存在、JSON 损坏、卡号不存在、状态名不存在）退出 4，并打印一行中文原因。
5. 补 docs\workflow\WORKFLOW-DRAFT.md 的「5. 待定」：格式=JSON、位置=docs\workflow\workflow.json、跳转表以 workflow.json 为准；面板字段和按钮写明“第 6 项定”；通知渠道写明“沿用 notify_done.py（BRIDGE_NOTIFY_URL）”。

## 怎样算完成
- python -m unittest discover -s tests 全过，测试数 ≥ 157 + 新增；新建 tests\test_card05.py（全部用临时目录里的 progress-test.json 和 workflow.json 副本），至少覆盖：transitions 里每一条合法边、至少 3 条非法跳转（含 draft→pass、同状态 move、block→sent）被拒且文件不变、block 生成 05b 且再 block 一次生成 05c、hold 无 missing 被拒 / 有 missing 成功 / resolve_hold 回 verifying、连续 3 次 advance 无变化判 stuck 且中途 move 成功会清零、move 顶层 card_id 时同步顶层 status、写后原有 7 个顶层字段全保留、写前备份文件存在、BOM 文件可读、五种退出码（0/2/3/4）各一条。
- 真机只读：workflow_use.py status --file progress.json 退出 0；复制一份到 evidence\card05_tmp\progress-test.json，对它跑 move 合法一次、非法一次、advance 三次，原始输出和每条退出码存到 evidence\card05_live.txt，跑完删掉 evidence\card05_tmp\。
- 真实 progress.json 除最后一步写结果外不被改动（施工前后各记一次 SHA256 写进 card05_live.txt）。

## 允许改
新建 src\qi\workflow.py、src\scripts\workflow_use.py、tests\test_card05.py、docs\workflow\workflow.json、evidence\card05_live.txt、evidence\card05_tmp\（用完删）；改 docs\workflow\WORKFLOW-DRAFT.md、CHANGES-20261030.md、progress.json。改已有文件之前先备份到 _backup_20261101。

## 禁止碰
F:\Bridge\v0.0.8、其他已有 .py 的内容（含 notify_done.py、runlog.py、各端 *_use.py）、插件目录、注册表/策略、登录、audit\；不调用任何端的 CLI（codex/opencode/dsh 等），不给任何端发消息。
注意：“禁止碰 notify_done.py”指不改它的代码；最后一步按下面要求运行它是允许的，也是必须的。

## 最后一步
写 progress.json（card_id=05，status=done/stuck/failed，note 一句原因），再运行 python src\scripts\notify_done.py --card 05 --status <done|stuck|failed> --note "<一句原因>"。不管成没成都要通知。