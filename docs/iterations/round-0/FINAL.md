# Round 0 结案（按 §12 停机）

时间：2026-11-12 09:37（Asia/Shanghai）
决定：Herhons 选择「**维持现状**」——§5.3 的阈值与「逐字」口径**都不改**，本轮按 §12 停机。
对应记录：`STOP_handled_20261112_0937.md`（原 `STOP_20261112_0330.md`）

---

## 1. 四席最终结果

| 端 | 字数 | 命中 | 命中率 | 判定 | 归档 |
|---|---:|---:|---:|---|---|
| chatgpt | 1714 | 9/10 | 0.90 | **true_read** | `replies/chatgpt.md` |
| grok | 2101 | 7/11 | 0.64 | partial | `replies/grok.md` |
| gemini | 3173 | 4/7 | 0.57 | partial | `replies/gemini.md` |
| deepseek | 0 | — | — | **unread**（NO_ACCESS） | 无回复，见 §4 |

`meets_three_vendors = false`（有效真读 1 家，门槛 3 家）。
原始判定见 `quote_check.json`，复算命令见 §6。

**没有进入 §5.5 之后的任何步骤**：匿名包、Gemini 汇总、核实表、计划一律未做——
按规格它们以「3 家真读」为前提，前提不成立就不产出，避免用 partial 意见冒充会审结论。

---

## 2. 三份意见原样交给 Herhons 人工采信

三份回复**未经匿名化、未经汇总、未经删改**，原文见 `replies/`。
下面只是索引，不是替代阅读。

**chatgpt（true_read，8 条）**
1. 独立验收没有必经闸门（§4、§1、§9.2）
2. 批准没有绑定执行对象（§10）
3. 锁合同与 `CALIBER.md` / `PLAN.md` / `known_gaps.md` 明确冲突（§7.1）
4. 位置命中不能证明「真读」（§9.1、§5.3）
5. 材料清洗白名单仍可能破坏源码（§8）
6. 崩溃恢复缺少消息级身份（§7.1、§13）
7. 单轮恢复缺少停机预算（§12）
8. 归档不能保证独立复核（§11）

**grok（partial，6 条）**：§8 与 Round 0 的上传绑定；§5.3 阈值对规格审过严；§10 大改闸门把「改合同」一律列大改；§12 缺 Round 0 重试出口；§9.2 汇总者与四审同源；§4/§13 状态机与账本边界。

**gemini（partial，6 条）**：§5.2 与 §9.1 引句形态冲突（上传模式可能判 0 命中）；§10 核实表执行主体未定义；§8 上传只在「首次读仓库失败」时触发；§4/§6 小改提交到哪个分支未定义；§7.1 全程持锁与残留锁；§6/§9.1 命中率分母口径未定义。

> 注意：三家都独立指向了同一个问题域——**验真口径（§5.3/§9.1）**。grok 与 gemini 明确要求放宽或改分母，chatgpt 则从反面指出「位置命中不能证明真读」。两边其实在说同一件事，方向相反。Herhons 已决定本轮不改，此处仅如实记录。

---

## 3. 本轮实际达成的（与未达成的）

达成：
- 网页端上传在 **chatgpt / gemini / grok** 三端跑通，并各有一条真实会审回复；
- 规格快照已公开可达（`6b049b32…` 修正后），会审方可核对原文；
- 编排器六步（快照→发问→验真→匿名→汇总→归档）中，**前两步与验真/归档有真机实证**，匿名/汇总仍只有离线用例。

未达成：
- 3 家真读（只有 1 家）；
- 因此 §5.5 的汇总、核实表、计划全部未产出；
- deepseek 一席完全没有回复。

---

## 4. 遗留问题（两条，都已定位到具体位置）

### 4.1 deepseek 上传未打通

- **附件按钮是纯图标，没有可读名字**。`src/qi/upload.py` 的 `ATTACH["deepseek"] = {"button": ("附件","上传")}` 在真机上匹配不到，
  返回 `attach_not_found`（`detail.wanted = ["附件","上传"]`）。
  需要按「输入框左侧第一个可点图标」的坐标点，或先在页面里确认 `input[type=file]` 对应的 UIA 节点。
- **分段粘贴不被接受**。9 段全部落地后，它回的是：

  ```
  可访问=否
  仓库 Herhons/bridge 与附件在当前环境无法读取……我手上只有你分段贴出的部分文本，且无法核对
  ```

  按合同属 `NO_ACCESS`，**不计有效席**。
- 含义：deepseek 只有「上传」一条路；点不开附件就等于这席永远缺席。

### 4.2 截断引句被算作未命中

- `src/qi/iteration.py` 的 `QUOTE_RE = [「“"']([^」”"']{6,120})[」”"']` 抓到的是**模型写下的那段字**。
  模型常把原句尾巴截掉（例：`「真读 = 命中 ≥ 3 且命中率 ≥ 0.7，且至少一条落在 docs/self`），
  而 `_squash` 只去空白、不去省略号或残缺括号，于是逐字比对失败。
- 影响可量化：grok 严格版 4 条未命中里 **2 条是截断引句**、2 条是编造路径；gemini 同类更多。
- 本轮按 Herhons 决定**不动口径**。若将来要改，改动点只有两处：`QUOTE_RE` 的截断容忍、以及命中率分母的定义（按意见条数而非全文正则总数）。

---

## 5. 边界声明

- 未修改 `docs/self_iteration_spec.md` 一个字节；
- 未开启 Round 1 / round-0b，未进入代码轮；
- 未把 partial 意见写进任何「汇总」或「计划」产物；
- 停在等 Herhons 的状态。

---

## 6. 复算与复现

```bash
# 重新判定四席（读 replies/ + 快照真值表）
py -3.10 -B tools/iterate.py verify --round round-0

# 单席重跑（各自会新建会话、上传材料、发送短指令）
py -3.10 -B tools/iterate.py review --round round-0 --site chatgpt --force
py -3.10 -B tools/iterate.py review --round round-0 --site gemini  --force
py -3.10 -B tools/iterate.py review --round round-0 --site grok    --force
```

投喂方式记录在 `docs/iteration.json`（`feed`: chatgpt/grok = `upload`，gemini = `upload-script`，deepseek = `paste`）。
证据：`evidence/iterations/round-0/*.json`（含各席 stage / submission / 失败原因）。
