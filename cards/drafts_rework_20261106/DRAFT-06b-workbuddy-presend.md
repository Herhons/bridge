# DRAFT — 06b-workbuddy-presend

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 2 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:39`。
依赖：06a-workbuddy-purpose。

## 问题与文件行号证据

WorkBuddy在写入之前消费回执，未接写后复核；写入中模型/页面/窗口漂移仍可能提交。gate失败也缺统一截图+JSON诊断。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06b.txt:22`：`src/qi/workbuddy.py:828-840` 提前gate；`:667` send_and_wait未传pre_send。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06b.txt:32`：WorkBuddy gate_or_exit直接return未调用failure.record；现有FailureEvidenceTests只测公共函数。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 接入真实WorkBuddy发送链的pre_send：写入及正文比对完成→新取正确窗口/文档→只读核验模型、身份、用途和human状态→原子消费→提交。可做不消费的写前预检，最终门禁必须在写后。
2. 保存06d已授权的--new模型恢复特例；普通use复核不得切档。失败不得点击提交，不因门禁失败自动重发。
3. tier/human/input/页面变化等错误由实际入口统一failure.record截图+JSON，记录可得分数/缩放及原因；桌面端不发浏览器Ctrl+0。测试中截图/日志必须是假件或临时目录。
4. 06a未配置用途仍拒发；不借门禁迁移提前解决09f已答恢复或改变回执一次性规则。

## 验收测试（离线，含入口与负例）

- 从WorkBuddy _run/main到真实ask/send_and_wait执行假UI流程，断言write/readback→fresh document/tier check→consume→submit的顺序，不能只测试公共conversation.ask。
- 写入中改模型、换hwnd/pid/start/tab、文档丢失、human提示、正文不符或回执过期均拒绝提交；验证未消费的预检与最终一次消费。
- gate失败的真实入口恰好产生一次假截图+临时JSON/日志，含原因与适用字段；截图失败仍返回原失败结果。
- 成功只提交一次；发送失败回执作废；--new恢复后再读回、桌面不调用缩放操作。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不修旧回复归属/accepted中断恢复（第3组），不真实启动或重启WorkBuddy，不改其他端档位策略。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
