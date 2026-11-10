# DRAFT — TEST-ISOLATION

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 9 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:46`。


## 问题与文件行号证据

整套测试全绿仍可能截真实屏幕、写生产运行日志和合成输入标记。先前隔离复跑不改变测试本身的默认副作用风险。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\READONLY_INCIDENT.txt:10`：`tests/test_card06a.py:798-807` 漏mock failure.record；`tests/test_card09e.py:258-265` 漏mock record；`tests/test_conversation.py:183-189` 漏标记隔离。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:54`：原ui.lock可能由外部持有；不得哈希失败后伪称一致或打断它。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 公用fixture把运行日志、failure截图/JSON、tier/锁/标记/状态及TEMP/TMP指向临时根目录；真实输入/屏幕抓取/剪贴板/通知/重启/外网均不可触达。
2. 全套运行的子进程继承运行根目录/隔离守卫，不依赖测试作者逐个记得mock；回环假HTTP与真实无UI锁子进程可用。
3. 加入整套副作用检查与可审计守卫事件；仓库外写入、真实截图/输入/通知尝试导致失败，不能吞掉守卫异常让suite假绿。
4. 不回滚或删除混有外部活动的日志，不触碰原ui.lock真实持有者。

## 验收测试（离线，含入口与负例）

- 完整unittest在临时运行根目录，输出Ran/OK/EXIT与0外部写入/真实UI/外网事件；父子进程均受保护。
- 故意遗漏mock的失败留证/Screen.type_text标记必须只落临时目录；故意尝试原目录写盘/截图/输入/外部通知时守卫确实拒绝且被测试断言。
- 前后SHA256/文件清单比较原src/tests/cards/PLAN/progress及可读既有文件，不把外部并发变化归本任务；受锁文件/外部活动单列，绝不清理掩盖。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不改变16卡功能判定、不宣称离线测试是真UI端到端、不写原evidence或生产运行日志。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
