# 自迭代循环状态（Herhons 10:42 新规后）

更新时间：2026-11-12 18:05
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

## Round 3（2026-11-12 15:05 起）

| 项 | 结果 |
|---|---|
| 会审 | 三席（gemini 全程拒答，argon 连「1+1」都拒；chatgpt 2097 字 / deepseek 4528 字 / grok 2308 字） |
| 验真 | **chatgpt 6/7、deepseek 6/6、grok 6/7 → 三家 true_read**，`meets_three_vendors=true` |
| 汇总 | **chatgpt 代汇总**（gemini 不可用），新会话 `6af56c00…`，1093 字 |
| 施工 | P1 账本降级、P2 剪贴板事务、P3 孤儿锁入口、P4 驱动边界 AST、P5 提问碎片、P6 匿名清洗、P7 上传文件存在性 |
| 遗留 | ~~R3-2~~、~~R3-5~~ 已于 Round 4 施工；R3-6 驱动实现迁移待做 |

**口径更正**：`_squash` 之前只去空白，模型抄写时丢掉行内代码的反引号就会被判"没逐字引用"。
改为同时忽略 `` ` `` 与 `*` 后，Round 1/2/3 的复算全部升为 true_read——此前"chatgpt 在转述"的结论是**错的**，
是取回截断 + 反引号两件事叠加造成的误判。

## Round 4 施工（16:31 起，承接已核实项，未新开会审）

- **R3-2 写账所有权**：`submissions.adopt_owned()` + `_Ledger.adopt(written)` 核对预约归属，
  窗口级判据（只比 site/hwnd）——按 `reconcile` 那套逐字段比会把正常首发全拒（已用旧用例验证）。
- **R3-5 匹配规则统一**：`conversation.merged_blocks/prompt_span` 为唯一实现，tools 只委托。
- **兜底保留证据**：`ask_failure`（stage/timeout_kind/reason/diagnostics/warnings）。
- 全量 **1716 用例 OK**。

## Round 5 施工（17:00 起，无新会审）

- **B32 首轮落库误判**：`_first_turn_landed()` 按路径形状判（`/app` → `/app/<id>`），
  不再用「接受后 2 次读」的窗口 —— gemini 的落库迟到 16 秒，老判据必然判成换会话。
  真机复验 `stage=answered`；7 条单测，旧代码红。
- **B33 会审预算**：`iterate.py` 原来直接吃 `cfg["review_timeout"]`（480），既绕过
  `tier_guard` 的 1500s 下限，又让父进程 `timeout=budget+180` 比子进程预算还短
  （先杀子进程）。新增 `_review_budget()`，7 个调用点全改；配置改 900。
- **B34 额度墙**：`quota_wall()` 只认整句、有回复键不判；命中即 `quota_exhausted`；
  `_submit` 在「额度墙 + 一个能点的发送键都没有」时报 `send_unavailable`（可重试），
  不再留下永久拦住的 unknown 假账。真机 `stage=send_unavailable`、12.1s 返回。
- **B35 子进程超时**：4 处 `subprocess.run(timeout=…)` 没接 `TimeoutExpired`，
  卡住时整个命令以 traceback 收场、归档无结果。新增 `_run_or_timeout()` 统一接住。
- **B36 锁用例隔离**：`test_b13_lock.TurnTests` 没隔离全局 `runs/turn/*.lock`，
  `started.wait(2)` 偶发超时，会**随机挡住 pre-push**（已实测撞到一次）。setUp 里
  把 `uilock.ROOT` patch 到临时目录。
- 全量 **1742 用例 OK**（另有一次 `progress.json` 5s 锁超时，单独重跑 3 次全绿，
  属负载抖动，未改常量）。

## Round 4 会审：归档失败（3 家凑不齐）

材料与快照已就绪（`round-4/`，快照 `820609ac`）。chatgpt 一席 582s/0 字，
页面提示「升级订阅或添加额度…明天 20:36 重置」——`Work` 用量耗尽，**需要花钱**，
按 §12 A1 停轮。未升级、未购买、未换账号。

**结论（17:25 复核）：本轮会审不成立，已归档。** 不是等不等的问题 —— 三家里
chatgpt 是额度墙（只有 `GPT-6.1 Sol` 一档，无免费入口）、gemini 是**账号级拒答**
（新会话问「用一句话描述春天」也回「因此没法帮到你」）。凑不够 §7.3 的 3 家，
不进汇总。材料与快照留在 `round-4/`（已更新到 `f5ef01a`，含 B32–B35），额度重置后可直接续跑。

## 近期已修（2026-11-12 14:31 起，STOP_handled_20261112_1431 的未决清单）

- **动作条顶出视口**：chatgpt 长回答的复制键落在窗口外（y=2061 / 窗口 1340），
  `copy()` 抛「光标没能移到」→ 整轮 timeout。现在候选全部不可达时改走 UIA 文本兜底，
  真机验证 `stage=answered / 2590 字 / warnings=["actions_offscreen"]`。
  同类保护对 deepseek 早已生效。
- gemini 清框、deepseek/grok 换行、grok 上传菜单、prompt 快照 SHA 四项复核为「已解决」，
  详见 `round-0/changes.md` 第 24 节。
