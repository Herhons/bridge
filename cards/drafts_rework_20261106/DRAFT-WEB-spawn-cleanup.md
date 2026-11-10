# DRAFT — WEB-spawn-cleanup

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 6 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:43`。


## 问题与文件行号证据

已创建窗口只有正常返回后的绑定失败才close；place/focus/读取异常会泄漏自己创建的窗口。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-WEB.txt:19`：`src/qi/browser.py:267-307,459-468` _spawn抛异常后外层拿不到hwnd；PWEB_spawn_exception_leak close=0。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-WEB.txt:25`：安装证据缺完整命令，需从已有记录补齐而非伪造执行。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 在创建归属明确后对后续全部失败统一收尾，原异常/诊断保留；必要时记录清理失败，不能误关复用的用户窗口或其他迟到窗口。
2. 把创建/等待/放置/聚焦/就绪/绑定的所有异常出口纳入资源生命周期，成功时保留目标窗口。
3. 安装命令从已有真实记录补入说明；未找到则明确缺失，不重新安装或写成已执行。

## 验收测试（离线，含入口与负例）

- 执行真实_spawn逻辑，仅ShellExecute/枚举/窗口API为fake；place、focus、ready/标题读取/绑定分别抛异常均close自己的新窗口一次。
- close也失败仍保留原失败；新窗口未识别、迟到、不属于本次、复用窗口不误关；成功不close。
- 不整体mock_spawn；全程无真实浏览器。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不打开Chrome、不改模板/音频依赖、不修其他WEB2逻辑。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
