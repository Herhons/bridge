# DRAFT — 07-reply-attribution

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 3 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:40`。


## 问题与文件行号证据

主循环两轮稳定只证明文本稳定，不证明属于本轮。本轮提问未认领、回复数未增加时不得返回answered。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-07.txt:19`：`src/qi/workbuddy.py:593-616,474-496` 没有prompt_at仍收旧回复；`:571-622,641` baseline只在最后读部分使用。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-07.txt:20`：baseline=1且无本提问仍以OLD-ANSWER返回answered；旧主循环测试容许该缺口。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 主循环和final_read共用归属判据：优先本轮prompt锚点，缺锚点必须有可信的发送前后新增回复证据；未证实则保持unknown/no_reply。
2. 处理复制键数变化、旧键替换、UI虚拟化，不单靠稳定非空或任何新增控件作归属；明确提交accepted与回答answered的区别。
3. 保留busy/完整回复/两轮稳定/超时最后读，日志记录归属依据，未知时不得自动重发。

## 验收测试（离线，含入口与负例）

- 真实wait_reply主循环重放P07_old_reply：无本提问、baseline未增、仅旧回答必须不成功；final_read同判据。
- prompt在旧回答后、新回答前才接受；无prompt但可靠新增本轮回复可成功；旧复制键替换/虚拟化/生成中/文本半截均不能冒充新轮。
- WorkBuddy实际ask/入口使用发送前baseline；不整体mock最终归属函数。超时保留submission并不重发。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不通过延长等待掩盖归属，不真实发送，不修900秒或回执入口恢复。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
