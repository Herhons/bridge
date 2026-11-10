# DRAFT — 04c-log-note

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 7 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:44`。


## 问题与文件行号证据

原idle异常与hint缺口已修，但卡面要求note也保留，note-only错误被降为stage(no hint)。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-04c.txt:24`：`src/qi/runlog.py:29-69` 不读note；P04C_note_dropped丢NOTE-MUST-SURVIVE。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 将note纳入失败原因策略，明确hint/note/error共存时优先级或合并方式，避免吞掉可操作原因。
2. 保持成功error=null、异常原样抛出、三端每次恰好一条日志；不更改CLI既有参数。

## 验收测试（离线，含入口与负例）

- 实际failure_note与done→record入口：note-only、hint/note/error组合、空白/非字符串、异常原对象、success均符合约定。
- codex/dsh/opencode失败入口各恰好一行临时runlog，保留原idle/hint回归；不只mock最终record。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不重判已通过的核心异常修复，不写原运行日志。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
