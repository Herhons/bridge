# DRAFT — 09e-workbuddy-restart-evidence

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 8 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:45`。


## 问题与文件行号证据

一次重启代码与mock覆盖存在，WorkBuddy本人的必需真机证据明确未完成。不能将另一app的同函数测试冒充完成。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09e.txt:22`：`evidence/card09e_live.txt:135-163,189-192` 实际只重启OpenCode，未重启WorkBuddy；端点AppID/关闭行为不能替代。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 先找已有WorkBuddy本人的close→消失→正确AppID/exe启动→窗口→UIA文档原始记录；不存在则标待补。
2. 新真机任务必须已允许关闭/重启且避开施工会话；保留操作对象、命令、时间、恢复次数和EXIT。发送若非必要不做，若需另有明确授权。
3. 区分mock、OpenCode替代测试、WorkBuddy真机三种证据；保留原始未验说明。

## 验收测试（离线，含入口与负例）

- 离线核对证据端点身份/AppID、一次重启上限、文档恢复与第二次失败6；不存在时只记pending。
- 负例：OpenCode记录、mock runlog、只有窗口出现但无文档均不能证明WorkBuddy完整重启。
- 本任务不关闭/启动任何app，不制造accepted或发送；不新增无意义代码测试。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不在本任务执行任何重启/发送，不把证据缺失定性为已知代码故障。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
