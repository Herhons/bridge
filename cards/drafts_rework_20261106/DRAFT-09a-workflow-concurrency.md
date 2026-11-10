# DRAFT — 09a-workflow-concurrency

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 4 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:41`。


## 问题与文件行号证据

原子replace只能防坏JSON，不能防读改写丢更新。server必须遵守progress.write/notify_done使用的同一跨进程事务锁。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:35`：`src/qi/server.py:981,985,1034,1036` 只有Engine RLock，覆盖旧整份data。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:36`：真实progress.write的ANOTHER-CARD/NEW_NOTIFY_NOTE被服务回写旧card/note；原WorkflowTests只测同一Engine。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. server workflow_move在progress.lock内重读、验证迁移并合并更新，所有相关写者共享同一锁与同目录原子replace；不跨锁使用旧整份data。
2. 不覆盖其他写者新card/note/verdicts/queue等无关字段；明确同字段冲突顺序或409，不以仅核对state当完整成功。
3. 保持basename白名单、非法迁移不写、备份和异常释放。

## 验收测试（离线，含入口与负例）

- 真实server.workflow_move与progress.write/notify_done（仅通知发送mock）跨线程及真实子进程交错，保留ANOTHER-CARD/NEW_NOTIFY_NOTE与verdicts/queue；不是两个Engine RLock内调用。
- 读取后另一写者抢锁、非法迁移409、replace失败、锁超时及进程退出；JSON有效、更新不丢、锁可回收。
- 仅临时progress-test.json，绝不调用原目录notify_done或外部通知。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不调整状态机边、不写原progress，不修request_id。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
