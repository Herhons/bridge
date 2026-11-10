# DRAFT — 09e-self-input-all-entries

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 6 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:43`。


## 问题与文件行号证据

三单文件入口未接合成输入标记，脚本自己的刚输入被算成人类活动，导致默认2秒等待。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09e.txt:32`：`src/scripts/codex_use.py:288,dsh_use.py:113,opencode_use.py:189` 仍局部算idle，未读取mark。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09e.txt:33`：相同tick共享guard=999，三个真实函数=1秒；旧测试只测共享guard/Screen。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 将所有实际use/choose/Desk输入原语和idle入口接入同一时间戳比较/记录语义，避免只修共享guard。
2. 保持真实后续用户输入优先让位，标记读失败/过期/跨进程/32位tick回绕有明确保守策略；不把所有输入都当自身。
3. 标记输出可配置且测试用临时根目录，不修改原runs。

## 验收测试（离线，含入口与负例）

- 三个真实main/idle入口与Desk假系统tick验证own=999、人类后续输入=真实idle并让位；独立P09E反例回归。
- Screen与Desk实际输入原语（底层OS输入fake）均写正确标记；标记缺失/损坏/过期、并发、tick回绕及后续真实输入负例。
- 断言无真实SendInput、剪贴板写入和仓库外标记。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不降低让位阈值，不真实输入，不补WorkBuddy重启实测。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
