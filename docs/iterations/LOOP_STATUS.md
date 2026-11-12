# 自迭代循环状态（Herhons 10:42 新规后）

更新时间：2026-11-12 14:05
规则：计划闸门已关闭（规格 §10）；停机只分 A 类（花钱/登录/删数据/动 v0.0.8）与 B 类（技术性）。
本文不是"等批准"的 STOP，而是**交接与续跑用的状态**。

## 已跑轮次

| 轮 | 会审 | 真读 | 汇总（新对话） | 施工 | 结果 |
|---|---|---|---|---|---|
| Round 1 | 四席（2087/1875/6392/3147 字） | deepseek 5/5、gemini 5/5 | `daba2074c268d3ef`，2743 字 | S1 safe_ui、S2 回声过滤、S3 会话锚点模块、S4 总控骨架 | 已推送 `be18ac3` |
| Round 2 | 三席 + grok 补测（6114/2137/3934/2238 字） | **deepseek 4/5、gemini 5/6、grok 5/6（补测）** | `2d744528ebfb94d6`，2329 字 | P1 顶层/布局校验、P2 锚点接线、P3 引文豁免、P4 核实闸门+三振 | 已推送 `28853d4`；验收口径修复见 round-0/changes.md 第 23 节 |

全量回归：**1662+ 用例 OK**（每次推送前 pre-push 也跑一遍）。

> 2026-11-12 14:05 起，Round 2 的 chatgpt 回复已用修好的取回逻辑重新抓取（2137 字，7 对引号），
> 数字从 1/4 更正为 1/7（1 页眉 + 6 条）。grok 的 5/6 来自同日补测，见 round-0/changes.md §23.3。

## 未跑（Round 3–5 的候选，按 Round 2 排序）

1. **四阶段账本**：`submissions.py` 拆 staged→dispatched→accepted(有上屏证据)→completed；
   旧记录惰性迁移；accepted 必须有证据（本轮又抓到一次"没发却记 accepted"）。
2. **legacy_adapter**：`src/qi/drivers/legacy_adapter.py` 把现有 use/choose 薄封装成 SiteDriver，
   总控只调接口；用 AST 扫描防出现第二套发送栈。
3. **孤儿锁挂到入口**：`sisyphus.py` 启动时调 `uilock.clear_stale()`（现在只在 review/summarize 调）。
4. **剪贴板事务锁**：paste 路径写剪贴板后核对哈希，异常时恢复快照。
5. **验真再收口**：目前真读门槛仍是「按意见条数 ≥3 且 ≥0.7」；两轮都只有 2 家真读，
   若长期如此，应按 §10 由推进器提议调整并记录（改阈值需 Herhons 点头）。

## 硬停检查（A 类）

本阶段**没有**触碰：无花钱、无登录/注册、无删除用户数据、未动 `F:/Bridge/v0.0.8`。
两次险情均已按「否」取消并加固（见 round-0/changes.md 第 19、16 节）。

## 怎么续跑

```bash
py -3.10 -B tools/sisyphus.py status --round round-2   # 看条目状态
py -3.10 -B tools/sisyphus.py next   --round round-2   # 取下一条（会先校验核实+硬停+三振）
py -3.10 -B tools/iterate.py review --round round-3 --site gemini --force
```

注意：`sisyphus.py` 只做交接与续跑，不决策；计划由「Gemini 汇总 + 推进器核实」产出。
