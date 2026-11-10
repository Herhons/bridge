# DRAFT — 08-autopush-integration

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 5 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:42`。
依赖：09a锁、06a/06b、09f入口恢复。

## 问题与文件行号证据

停顿20分钟后的旧回执已过期/消费，自动推没有choose不能通过门禁；只读已答成功又可能误增推送计数。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-08.txt:22`：`src/scripts/workbuddy_autopush.py:72-75,154-160` 只有use；20分钟阈值大于120秒TTL。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-08.txt:26`：exit0的already_answered也被记作新推；runner假成功未验证真实门禁。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 在同一UI事务中安全串接用途档位choose→确认→新回执→continue；采用09a可核验父子代持，禁止自锁。
2. 只在确实新提交时增加push计数/冷却；already_answered、拒发、unknown/accepted恢复有独立结果，不能为取得新回执而绕过禁止重发。
3. 并发调度实例对判定/占位/计数原子处理；保留20分钟、30分钟、三次停推、generating和dry-run纯度。

## 验收测试（离线，含入口与负例）

- 真实autopush入口及真实tier_guard配合假UI，旧回执过期/消费后先choose产生新回执再提交；choose失败/档位不符不执行use提交。
- already_answered不算新推，accepted/unknown不盲重发；并发两个实例最多一次提交，正确计数。
- 实际子CLI锁合同无自锁、普通CLI不能交错；dry-run不choose、不use、不通知、不写生产状态；阈值边界用假时钟。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不接真实计划任务、不真推或真choose、不改变阈值与停推规则。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
