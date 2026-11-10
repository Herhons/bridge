# DRAFT — 06a-workbuddy-purpose

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 2 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:39`。
依赖：09a-lock-integration自测完成后实施；06b不得绕过本卡拒发语义。

## 问题与文件行号证据

--purpose虽必填，WorkBuddy空白名单使test最低档、review/accept最高档要求失去约束。不能在本次离线任务猜测真实模型排名。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06a.txt:21`：`docs/tiers.json:142-149` 的test/review/accept均为空；`src/qi/tier_guard.py:540-566` 记table_unconfigured仍ok=true。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06a.txt:24`：旧测试只断言空表声明；P06A_unconfigured_tier_accepts证实accept可被任意模型放行。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 空表、缺用途、格式错误或未知端点/模型一律按tier_unconfirmed/退出5拒发，给出可诊断原因；不把table_unconfigured当成功。
2. 只使用有可核实来源的用途白名单；无法离线确认WorkBuddy最低/最高模型时保留未配置并拒发，不虚构白名单。测试中可注入明确的fixture配置。
3. 保持回执TTL、身份、原子消费与失败作废；未配置或用途不符不能触发写入/提交。

## 验收测试（离线，含入口与负例）

- 真实WorkBuddy _run入口配合假窗口/输入，空表test/review/accept均退出5且无业务提交；缺用途仍由argparse拒绝。
- fixture配置中低档test可通过、低档review/accept拒发、高档review/accept可通过、高档test按最低档表拒发；未知模型、未知用途/空表不能放行。
- 真实tier_guard.check/gate执行，验证原因、回执失效和无提交；不把gate整体mock成成功。保留其他端已有用途与身份/过期负例。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不猜测WorkBuddy当前可用模型及排名，不真实choose/use，不实现06b最后时刻门禁或06c联合回执。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
