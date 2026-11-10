# DRAFT — 09a-durable-idempotency

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 4 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:41`。


## 问题与文件行号证据

仍可GET的旧job不应因超出200条扫描窗口丧失去重。持久化失败返回202也不能保证重启后的不重派。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:31`：`src/qi/server.py:619-632` 只扫最新200；P09A_dedup_201可重派r000。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:33`：`src/qi/server.py:613-617` 吞写盘错误仍202，缺失败拒绝接单。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 定义并实现持久request_id索引或完整可恢复查询，不新增未经批准的200条/TTL失效例外；重启、内存淘汰后仍指向原job。
2. 接单/去重查询/落盘/派发使用一致并发协议：同request_id并发只产生一个job，持久接单成功前不派发；失败返回明确非202且可安全重试。
3. 处理中/已完成/失败job均有明确重复响应；半写/损坏索引不能静默当作新请求重发，日志不泄漏敏感正文。

## 验收测试（离线，含入口与负例）

- 临时目录建立201及更多job，最旧request仍可GET且重复submit返回原job，不进入runner；重启和内存50淘汰后相同。
- 两线程/两个服务实例或受支持的单实例边界下重复提交只入队一次；写盘/replace失败不返回202且不派发。
- 半写记录、损坏索引、重启恢复、不带request_id的合同与队列上限负例；实际submit入口与持久化执行，UI runner为禁止操作假件。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不引入旧版SQLite架构，不修workflow或锁，不自动清理旧job/证据。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
