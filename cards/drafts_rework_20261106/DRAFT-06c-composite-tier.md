# DRAFT — 06c-composite-tier

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 2 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:39`。
依赖：06a用途拒发规则；保持06b发送门禁合同。

## 问题与文件行号证据

最高档要求chat/GPT-6/高同时成立，现有单维回执丢掉model证据，effort正确也可能误放行低模型。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06c.txt:21`：`src/scripts/chatgpt_choose.py:1595-1613` model被effort覆盖；`src/qi/tier_guard.py:526-545,641-673` 只看receipt.dimension。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-06c.txt:23`：chat+gpt56+high review仍gate_ok=true；旧测试仅验证两张JSON列表。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. ChatGPT choose同一次完整读回writer/model/effort，以一次原子写保存联合目标/读回/原文与同一窗口身份；后写单维不能覆盖前维并被当联合成功。
2. check和发送前只读复核按用途验证所有必要维度，不仅receipt.dimension；writer漂移、任一缺失/不符均退出5，单维旧回执不可冒充联合回执。
3. chat模式test/review/accept各依照tiers表；Work无模型入口仍no_model_selector并作废旧回执，不能伪造model。
4. 复用现有原子消费/TTL/身份/--new规则，保持其他端单维回执兼容；记录联合字段和明确失败维度。

## 验收测试（离线，含入口与负例）

- 真正ChatGPT choose入口假控件读回chat/gpt6/high后只原子写一份完整回执；后写effort不丢model；单维旧回执拒绝联合用途。
- 实际use/pre_send入口、真实gate：gpt56+high、gpt6+mid、writer变work、缺model/effort、换文档/身份、任一维不可读/过期均拒发且无提交。
- 同轮正确最高档review/accept与正确最低档test通过；用途错配不通过。每个维度在写入期间漂移都被最后复核发现。
- Work无model入口拒发并清旧回执；其他端单维/并发消费回归全绿。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不制造实时模型可用性证据，不将GLM额度受限的链路证据改称token回复，不修07回复归属。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
