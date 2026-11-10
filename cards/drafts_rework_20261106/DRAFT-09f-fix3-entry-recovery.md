# DRAFT — 09f-fix3-entry-recovery

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 3 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:40`。
依赖：09a锁协议、07回复归属；与06b最后时刻消费保持一致。

## 问题与文件行号证据

已有已答函数正确，但消费门禁让正常重跑根本到不了它；两次成功ask不能证明accepted后中断安全。服务外层180秒也抵消内部900秒等待。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09f-fix2.txt:27`：`src/qi/workbuddy.py:828-834` 已消费回执阻断普通重跑；ask和_already_replied均未进入。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09f-fix2.txt:36`：各use floor_timeout存在，server _run_use外限仍180；补充P09F_server_outer_timeout验证实际命令与外限。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09f-fix2.txt:26`：旧两次ask测试未制造accepted后中断，绕开真实入口；`:732-791` 重启路径不能替代普通重跑。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 普通入口先执行不产生发送动作的本轮已答/accepted恢复检查：已答清回填草稿并只读返回；未答但accepted/unknown只恢复等待或明确不重发，不能通过重新choose抹掉发送事实。
2. 已答只读路径无需新回执；真正新提交仍要求新鲜、未消费且用途正确的回执，恢复机制不得变成绕过门禁的新提交入口。
3. WorkBuddy普通重跑和UiDocsLost一次重启、Gemini/Grok accepted等待中回填均遵守同一禁止盲重发规则。
4. 高档900秒等待预算贯穿CLI参数、runner、endpoint总限、job/调用者截止时间并留准备/清理开销，底档保留自身策略。

## 验收测试（离线，含入口与负例）

- 通过真实use/main/_run入口首次假提交accepted后抛中断，回执已消费；第二次已答只读清回填且submit总数=1，不只调用conversation.ask两次。
- 第二次无回答但accepted/unknown、UiDocsLost、重启失败、不完整归属都不得重新提交；普通新问题无新回执仍退出5。
- Gemini/Grok在accepted等待期间回填草稿也清理且不提交；恢复失败保留submission。
- 捕获真实server构造的高档cmd/runner timeout和endpoint总预算，确保不早于900秒；用假时钟验证各外层，避免真实等待900秒。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不重放旧提问，不制造真实accepted，不修改既有09f-fix2证据或宣称原5测无效。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
