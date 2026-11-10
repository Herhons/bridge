# DRAFT rework INDEX — 2026-11-06

仅DRAFT；未发送、未合并、未经其他厂商独立验收。沿用已完成recheck，不重新评判16卡。

排序依据：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:37-46`（9组，组内拆成17张）。

| 顺序/原组 | 草稿 | 一行摘要 |
| --- | --- | --- |
| 1 / §三.1 | [09a-lock-integration](DRAFT-09a-lock-integration.md) | 父子CLI共享可核验的UI持锁关系，普通CLI保持互斥。 |
| 2 / §三.2 | [06a-workbuddy-purpose](DRAFT-06a-workbuddy-purpose.md) | WorkBuddy用途表未配置必须拒发，配置后按用途强制档位。 |
| 3 / §三.2 | [06b-workbuddy-presend](DRAFT-06b-workbuddy-presend.md) | WorkBuddy写后、提交前只读核验并消费回执，失败统一留证。 |
| 4 / §三.2 | [06c-composite-tier](DRAFT-06c-composite-tier.md) | ChatGPT保存同轮writer/model/effort并按用途联合校验。 |
| 5 / §三.3 | [07-reply-attribution](DRAFT-07-reply-attribution.md) | WorkBuddy主循环和最后一读共用本轮回复归属规则。 |
| 6 / §三.3 | [09f-fix3-entry-recovery](DRAFT-09f-fix3-entry-recovery.md) | 真实入口先只读查已答，accepted中断不得重发，统一外层预算。 |
| 7 / §三.4 | [09a-durable-idempotency](DRAFT-09a-durable-idempotency.md) | request_id持久去重超过200条，写盘失败拒绝接单。 |
| 8 / §三.4 | [09a-workflow-concurrency](DRAFT-09a-workflow-concurrency.md) | server与notify_done共用progress读改写锁，保留并发更新。 |
| 9 / §三.5 | [08-autopush-integration](DRAFT-08-autopush-integration.md) | 自动推送取新用途回执，并正确统计提交与已答。 |
| 10 / §三.6 | [WEB-spawn-cleanup](DRAFT-WEB-spawn-cleanup.md) | 新建窗口在place/focus等抛异常时也回收，复用窗口保留。 |
| 11 / §三.6 | [09b-health-contract-and-timeout](DRAFT-09b-health-contract-and-timeout.md) | health精确两字段，截止时间覆盖响应正文及恢复。 |
| 12 / §三.6 | [09e-self-input-all-entries](DRAFT-09e-self-input-all-entries.md) | 三单文件use与Desk统一排除自身合成输入。 |
| 13 / §三.7 | [04c-log-note](DRAFT-04c-log-note.md) | 保留note-only失败原因及明确hint/note/error优先级。 |
| 14 / §三.7 | [PLAN-sync](DRAFT-PLAN-sync.md) | 保留历史表，新增有原始证据关联的当前状态表。 |
| 15 / §三.8 | [WEB2-evidence](DRAFT-WEB2-evidence.md) | 只补Grok最低fast→new的原始证据链，先找既有记录。 |
| 16 / §三.8 | [09e-workbuddy-restart-evidence](DRAFT-09e-workbuddy-restart-evidence.md) | WorkBuddy本人完整重启证据待另行授权任务补齐。 |
| 17 / §三.9 | [TEST-ISOLATION](DRAFT-TEST-ISOLATION.md) | 全套单测隔离截图、日志、合成输入及所有外部副作用。 |

先完成本目录全部草稿，再在独立WIP按09a锁→06a→06b→06c实施。其余草稿只定义后续工作。

真实UI补证仅可在另行明确允许的任务实施；此任务全部离线。旧卡内通知/状态更新/真机条款不构成本次授权。
