# Round 4 会审材料（范围 23ce172..f5ef01a，含探活中发现的 B32–B34）

三部分：① 规格全文；② 本轮源码 diff（src + tools，**测试不在里面**）；
③ 每条回归用例的名字（测试代码在仓库 `tests/` 下，可按需查看）。

---

## 一、规格全文

# 自迭代循环规格

- 路径：`docs/self_iteration_spec.md`
- 日期：2026-11-11（Asia/Shanghai）
- 状态：交给 WorkBuddy 建造编排器的任务规格（当前这轮 bug 修复结束后开工）
- 授权：Herhons 于 2026-11-11 15:42（Asia/Shanghai）批准的一次性例外——把本循环按本规格交给 WorkBuddy 自建，不再逐步递卡。

本文件是轮次合同。编排器、会审、闸门和停机都按这里执行。Round 0 先审本规格本身，再进入代码轮。

**Herhons 的要点（2026-11-11）**：Round 0 的目的，是把自迭代循环从头到尾搭起来并证明它能跑通；用「会审本规格」作为第一条端到端试验。改进规格文本是次要的。网页端上传文件是循环的必备能力，修通并在 Round 0 证明上传可用，属于本轮必须交付的部分，不是可选项。

---

## 0. 目标

把 2026-11-11 已经跑通的仓库级会审，做成可重复、可停机的编排器：

推送快照 → 四家网页端独立审查（意见必须带可核对引用）→ 引用验真 → 匿名 Gemini 汇总 → 计划闸门（核实通过即施工，见 §10）→ 修复 → 归档。

循环自己根据第 12 节停机，而不是靠人盯。第一轮（Round 0）用审本规格来试验整条管道（含网页端上传）；重点是证明循环本身，不是把规格改到尽善尽美。

工作实例与教训见 `docs/REVIEW-20261111.md` 和 `evidence/review_20261111/`。引用验真的既有口径见 `evidence/review_20261111/verify/verify_paths.py`（B23）。

---

## 1. 授权与边界

- **开工时机**：WorkBuddy 完成当前这轮 bug 修复之后（以当时主线验收为准）。本规格不插队打断正在进行的修复。
- **一次性例外**：整条循环由 WorkBuddy 按本规格自建、自跑，不再由红莉栖逐步拆卡递送。这不解除下面的红线，也不解除 §10 的四种硬停。
- **仍然有效的红线**（`PLAN.md` 底线）：先备份、写改动说明、留回滚；向主线合并须 Herhons 点头；不改注册表 / 策略 / 其他扩展 / 登录 / 书签；不填密码、不绕验证码；不碰 `F:\Bridge\v0.0.8`；施工与会审期间遵守 UI 锁，不得关闭、静音或切走 Herhons 的直播窗口。
- **施工模型**：WorkBuddy · DeepSeek-V4-Pro（Herhons 2026-11-02 指定）。没有 V4 Pro，或 `workbuddy_choose` 读回失败，就停，**不回退**到 V4.1 Flash。
- **验收不得与施工同厂**：不得用 DeepSeek 验收编排器或代码轮修复。优先 OpenCode GLM（只读 / `accept_lite`），或 WorkBuddy **新对话** Hy4 preview（`workbuddy_use --new --model "Hy4 preview" --purpose accept`）。必须附命令、退出码、测试数（Ran / OK）；无证据即 FAIL。
- **会审家数**：四家网页端审查；有效回复至少来自 **3 家不同厂商**。DeepSeek 可以是四审之一，但不能单独把家数凑够（施工方同厂）。同一会审对同一家最多两轮（先读仓库，读不了再喂材料）。

---

## 2. 角色

| 角色 | 谁 | 做什么 |
|---|---|---|
| 编排器 | 本规格要求 WorkBuddy 建成的程序（复用现有 `bridge_server` / `live_job` / 账本 / UI 锁，另加脚本） | 推进一轮全部步骤、写归档、判断停机 |
| 施工 | WorkBuddy · DeepSeek-V4-Pro | 建造编排器；按通过闸门的计划改规格或代码；不得自行扩大范围 |
| 四审 | 网页端 `chatgpt` / `grok` / `gemini` / `deepseek` | 独立读本轮快照，给出 5–8 条带引用的意见 |
| 引用验真 | 确定性脚本，不经模型 | 核对比对快照真值树，给出真读 / partial / 未读 |
| 匿名汇总 | Gemini（`purpose=review`，最高档） | 只看去掉来源的幸存意见，做聚类、去重、冲突标注与优先级建议 |
| 计划闸门 | 编排器产出核实表与计划；核实通过即施工（§10） | 对着快照逐条核实，分类后再决定是否动手 |
| 验收 | 非 DeepSeek 的一方 | 只看规格、计划、证据，不看施工方的自我说明 |

---

## 3. 仓库与推送

- 公开仓库：https://github.com/Herhons/bridge
- 每一轮开始前，必须把本轮要审的内容推到评审能读到的分支（默认 `public`，若改用其他可读分支，提问和真值表必须用同一分支），并记下精确 commit SHA，作为本轮快照。
- 会审提问只给这个地址和 SHA。代码轮不预设问题、不点名模块。Round 0 必须点名 `docs/self_iteration_spec.md`。
- 推送前跑全量离线单测（`.githooks/pre-push`）。测试失败不准开轮。
- `evidence/` 与 `runs/` 不入库。

---

## 4. 循环总览

```
当前 bug 修复结束
        ↓
WorkBuddy 按本规格建造编排器（离线测试通过）
        ↓
本规格作为 docs/self_iteration_spec.md 提交并推送
        ↓
Round 0：用完整管道审本规格，按幸存意见修订规格与编排器
        ↓
Round 1, 2, …：代码轮
        ↓
第 12 节停机条件满足 → 停，通知 Herhons
```

每一轮（含 Round 0）都走同一条管道：

1. 冻结快照并推送。
2. 四家串行会审（`purpose=review`，最高档；席位严格串行，全程持 `runs/ui.lock`）。
3. 引用验真。
4. 对 `NO_ACCESS` 或「未读」的端改喂材料（每家最多再一轮），再验真。
5. 将通过验真的意见做成匿名包，交给 Gemini 汇总。
6. 计划闸门：对着快照逐条核实，产出计划。
7. 核实通过即施工（§10）；只有花钱、账号、删数据、动 v0.0.8 四种情形停下等 Herhons。
8. 修复 + 全量离线单测 + 本轮回归测试。
9. 归档到 `docs/iterations/round-N/`。
10. 判断停机条件；否则进入下一轮。

不得跳过 Round 0 直接开代码轮。没有通过闸门的计划，不得改代码或改本规格的合同条款。

---

## 5. Round 0：先审本规格

Round 0 是循环的第一轮。审查对象不是业务代码，而是本规格本身。这样做是为了端到端跑通自迭代流程（含必须修通并验证的网页端上传）；规格文字的打磨是次要的。

### 5.1 进入条件

同时满足才可开跑：

- 当前这轮 bug 修复已经结束。
- WorkBuddy 已按本规格建成编排器，离线单测通过。编排器应能走完第 4 节管道（允许用夹具 / 录好的回复做干跑，建造阶段不强制先真机四席一次跑完）。
- 本规格已作为 `docs/self_iteration_spec.md` **提交并推送**到评审可读分支，快照 SHA 已记下。

### 5.2 审什么

把已提交的 `docs/self_iteration_spec.md` 交给四家，走完整管道：四审 + 引用验真 + 匿名 Gemini 汇总。提问在通用会审合同上改为：

- 明确对象是这份规格，不是「随便找仓库里的代码毛病」。
- 请审查：步骤缺口、停机条件漏洞、闸门是否可被绕过、与现有红线 / `CALIBER.md` / `known_gaps.md` 的冲突、引用验真是否不适用于审规格、材料投喂是否可能再次污染源码、同厂偏差、无法归档或无法复现的设计。
- 每条必须引用本规格的章节号，并附一句从规格里逐字抄出的原句（验真按第 9.1 节核对）。若批评涉及仓库现状，再按第 9.1 节补代码引用。
- 仍要求首行 `可访问=是` 或 `可访问=否`。读不了仓库或读不了该文件，只回 `NO_ACCESS`。

### 5.3 引用验真（Round 0 变体）

代码轮的「核心命中」是 `src/qi/` 或 `src/scripts/`。Round 0 改为：

- **真读** = 命中 ≥ 3 且命中率 ≥ 0.7，且至少一条落在 `docs/self_iteration_spec.md`。分段粘贴时，命中以「章节号 + 逐字引句」为准，验真在规格全文中查找该引句（空白归一，见第 9.1 节）。上传时，章节或行号能对上快照里的规格文本亦可。
- 同时引用的其他仓库路径仍按路径验真；路径不存在则该条引用不计命中，整条意见在核实阶段作废。
- **partial** = 命中 ≥ 1 且命中率 ≥ 0.4，但不满足真读。
- **未读** = 其余（含命中 0，或命中率 < 0.4）。
- 未读的回复不进入匿名汇总，也不进入计划。

### 5.4 修订什么

通过闸门且幸存的意见，由 WorkBuddy 同时修订：

- 本规格（`docs/self_iteration_spec.md`）；
- 编排器实现，使其行为与修订后的规格一致。

若意见要求改变轮次合同（阶段、闸门、停机条件、评审厂商集合、匿名规则、授权边界），属于大改，必须先得到 Herhons 点头，不得擅自改合同后继续跑。

### 5.5 归档与出口

全部产物归档到 `docs/iterations/round-0/`（见第 11 节），至少包括：修订前的规格快照、四家原文、验真报告、匿名包与 Gemini 汇总、核实与计划、Herhons 的决定（如有）、规格 diff、编排器变更说明与测试证据。

Round 0 结束之后：

- 若只有文字澄清、例子、归档字段等小改：直接进入代码轮（Round 1），不再重复审规格。
- 若合同被实质修改：最多再跑一轮 Round 0，目录用 `docs/iterations/round-0b/`，且须 Herhons 点头后才能开跑。两轮之后无论是否还有意见，停止 Round 0，向 Herhons 汇报，等待是否进入代码轮。
- 不得在 Round 0 未完成（或未按上面出口归档）时开始代码轮。

---

## 6. 代码轮（Round 1 起）

- 审查对象：本轮快照上的仓库源码。提问不预设问题、不点名模块，沿用 2026-11-11 会审提问结构（见第 7.2 节）。
- 引用验真：**真读** = 命中 ≥ 3 且命中率 ≥ 0.7，且至少一条落在 `src/qi/` 或 `src/scripts/`。partial / 未读阈值与第 5.3 节相同。引用形态随该端本轮投喂方式而定（见第 9.1 节）：上传时用文件路径 + 行号；分段粘贴时用章节或函数名 + 一句逐字引文，验真在对应源码中查找该引句（空白归一）。脚本以本轮 SHA 的树为真值，不要写死分支名。正式实现应迁到仓库内固定位置（建议 `tools/quote_check.py`），并用测试锁死归一规则与阈值。
- 修复范围只覆盖本轮计划里「成立」或「部分成立且已按缩小后的范围批准」的条目。`known_gaps.md` 里已决定不做的项，除非 Herhons 重新决策，否则保持不做，并在计划里引用原条目。
- 每轮必须跑全量离线单测。真机只覆盖本轮改动触及的端（Herhons 2026-11-02 范围规则）。
- 每条被采纳的修复必须带一条回归测试：旧代码红、新代码绿。

---

## 7. 四家会审

### 7.1 席位与发送

- 四家固定为网页端：`chatgpt`、`grok`、`gemini`、`deepseek`。不要把 Codex / OpenCode / WorkBuddy 桌面端加进四审。
- 严格串行：上一席发出并读完回复（或失败退出）后，才开下一席。全程持 `runs/ui.lock`。
- `purpose=review`（最高档）。超时下限走现有 `tier_guard` / `REVIEW_TIMEOUT`（900s）口径，不得用测试档或把高档会审判成 300s 超时。
- 遵守防重发：同一提问在该端已 `accepted`，只读不重发。输入用「先键入几个字符激活，其余走剪贴板，并保存恢复原剪贴板」。

### 7.2 代码轮提问合同（继承 2026-11-11 R2）

提问须包含：公开仓库地址、本轮 SHA、下面这些约束（措辞可整理，但合同点不能少）：

1. 请对方自己把仓库读一遍，指出最该改的地方。不预设问题，不点名模块。
2. 首行写 `可访问=是` 或 `可访问=否`，并列出真正打开过的仓库相对路径。只列真打开过的，不要列猜测存在的。
3. 不得运行代码，不得操作 8 个客户端，结论只来自读源码。读不出、看不懂就直说。个别文件打不开（404 或要登录）不要编造内容。若此刻打不开仓库，只回 `NO_ACCESS`。
4. 给 5 到 8 条意见，按自认为的收益从高到低排。每条先写引用，再写：为什么是问题（引用看到的具体事实），现在怎么做，破坏了哪个不变量，要不要改接口，没有证据怎么验收，建议的回归测试（什么情况下旧代码不通过、改后通过）。引用形态随投喂方式：上传用文件路径与行号；分段粘贴用章节或函数名，并附一句逐字引文。
5. 引用必须能在所喂材料或快照里对上（规则见第 9.1 节）。路径写错、引句找不到，整条意见作废。定位不到具体位置、只给方向的意见不采纳。
6. 不要泛泛而谈。「建议增加测试」「建议模块化」这类放到任何仓库都成立的句子没有价值。每条都要落到具体文件和具体代码事实。
7. 如果认为某处看起来像问题、其实不该改，也可以单列一条讲理由。不要复述提问说明，不要贴大段源码。回复控制在 2000 字内。

### 7.3 有效回复

- 至少 3 家不同厂商给出「真读」（含材料补喂后再验真达到真读）的回复，本轮会审才算齐。
- 同一家最多两次：第一次读仓库；若 `NO_ACCESS` 或验真为未读，第二次改喂材料。两次仍不够，记失败，不得编造回复。
- 凑不够 3 家：本轮不得进入汇总与修复；归档失败原因，按第 12 节决定重试、等待或停机。

---

## 8. 材料投喂

仅当某家读不了仓库（`NO_ACCESS`、未读、或明确要文件）时使用。教训来自 2026-11-11：给 Gemini / DeepSeek 的材料脚本做了全局 `.replace('**', '')`，把 Python 字典解包也剥掉，两家据此报告了不存在的「必崩」。

投喂方式由配置选择，可按本轮设定，也可按厂商分别设定，取值为 `upload`（网页端上传文件）或 `paste`（分段粘贴正文）。两者都是正式的运行模式，由配置选定；分段粘贴不是上传失败后的自动降级。网页端上传是循环的必备能力，须在 Round 0 修通，并有一次通过的自检验证「文件已落到该端、对方按文件内容作答」。上传自检未通过时，不得把该次运行记成上传模式成功，也不得把上传从合同里拿掉或写成可选项。

规则：

- 只允许处理行首 Markdown 标记、围栏的语言标签、以及确定不会出现在源码里的装饰性字符。**禁止**全局替换 `**`、反引号、下划线或缩进。
- 材料必须取自本轮快照原文，可复现。上传时文件须带路径与行号；分段粘贴按章节切开，不要把一个函数从中间切开却不标明续篇。
- **upload**：将本轮快照材料作为文件上传到该网页端。Round 0 必须证明这条路径可用。
- **paste**：按章节切开正文；每段开头标明「第X段/共N段」；最后一段声明材料已全部发完，请开始审查。须遵守各端字数与换行限制：DeepSeek 网页粘贴按约 1500 字分块（输入框约 2000 字上限，超了会被整段吞掉）；Grok 会折叠粘贴中的换行，多行提问可能 `input_mismatch`。
- 实际采用的配置值、方式与投喂字节数写入归档。不得把四家已经给出的意见泄漏到另一家的会审提问或材料里。

---

## 9. 引用验真与匿名汇总

### 9.1 引用验真

- 纯脚本，不经模型。引用形态取决于该端本轮配置的投喂方式。
- **paste（分段粘贴）**：从意见中抽取「章节号或函数名 + 一句逐字引文」；在对应的规格或源码中查找该引句。查找前做空白归一（合并连续空白，换行视为空白）。引句找不到，则该条不计命中。
- **upload（上传）**：代码轮从回复中抽取文件路径与行号，以 `git ls-tree -r --name-only <SHA>` 为真值表，并核对该行存在。规格审可用章节号或行号对上快照文本。路径归一至少包括：去掉 GitHub URL 前缀、反斜杠转正斜杠、去掉末尾 `:行号`、去掉 `blob/main/` 一类前缀。
- 上述规则以 `tools/quote_check.py` 为准，并由测试锁死。判定阈值见第 5.3 节（Round 0）和第 6 节（代码轮）。
- 只有「真读」进入匿名包并作为修复依据。partial 可进附录，不得直接施工，除非核实阶段独立读码后由 Herhons 批准。
- 验真报告必须进入本轮归档。

### 9.2 匿名 Gemini 汇总

目的：避免汇总模型看到「这是我上一轮写的」或厂商名称，从而偏袒或自我回声。

匿名包必须做到：

- 去掉厂商名、模型名、座次、能指向某家的 URL，以及「我作为某模型」一类自称。
- 意见改用中性编号（F1、F2、…）。可以保留验真结果（真读 / 命中率），但不写是谁的意见。
- 只保留核实所需的引用片段，去掉会暴露身份的套话。
- 明确指示 Gemini：只根据包里的意见做聚类、去重、冲突标注和优先级建议；不要猜来源；不要发明包里没有的新缺陷。若汇总者要补充，必须单独列为「汇总者新增」，并再走引用验真与计划闸门，不能直接施工。

汇总本身走 Gemini 网页端，`purpose=review`，持锁串行，原文归档。

---

## 10. 计划闸门

对匿名汇总后的每条意见（含「汇总者新增」），推进器必须产出核实表，口径对齐 `docs/REVIEW-20261111.md`：

| 判定 | 含义 | 下一步 |
|---|---|---|
| 成立 | 对着快照读码或读规格，引用为真，问题存在 | 直接施工 |
| 部分成立 | 现象在，但范围或机制说错了 | 按缩小后的范围施工 |
| 不成立 | 看错、材料被污染、或已有不变量禁止这种改法 | 不改，写明理由 |
| 已知取舍 | 已在 `known_gaps.md` / `CALIBER.md` / 本规格声明不做 | 不改，引用原条目 |
| 待真机 | 离线无法判定 | 列入待真机清单，不进本轮施工 |
| 触碰红线 | 见下面「四种必须停下的情形」 | 停，等 Herhons |

核实必须引用 `文件:行号` 或规格章节，并写清怎样一眼看出成立或不成立。禁止只复述会审原文。
2026-11-11 的教训要写进计划模板：会审范围可能过大（例如「桌面四端绕过账本」，核实后只剩 workbuddy），必须缩小后再改。

**闸门已关闭（2026-11-12 10:42，Herhons 批准）**：核实通过即施工，不再等 Herhons 点头。
本节优先于 §5.4 中「大改须批准」的表述；但**修改本规格文本本身**仍按 §5.4 处理，除非 Herhons 当次明确授权。
架构重构同样放开：四家会审有共识、且 Gemini 汇总把它列为高优先级，就直接改。

### 四种必须停下等 Herhons 的情形

只有下面四种：

1. 需要花钱（付费额度、订阅、API 计费）；
2. 需要账号登录或注册；
3. 会删除用户数据（桌面文件、回收站、用户文档）；
4. 会改动 Bridge v0.0.8。

除这四种之外，不得以「等批准」为由停工。计划仍必须在修复之前落盘（`plan.md`），
但落盘是为了可复核，不是等签字；改完即提交并推送公开分支。

---

## 11. 归档

每轮一个目录：`docs/iterations/round-N/`。Round 0 为 `round-0/`，复跑为 `round-0b/`。

下列内容必须能对上（文件名可调整）：

- `snapshot.json`：远程仓库、分支、SHA、日期（Asia/Shanghai）、编排器版本
- `prompt.md`：实际发出的会审提问
- `replies/`：四家原文（归档保留来源，仅供事后追溯；送给 Gemini 的是匿名包）
- `quote_check.md` 与 `quote_check.json`
- `materials/`：若有补喂，含原文、「未做全局 `**` 替换」的处理说明，以及本轮各端配置的投喂方式（`upload` / `paste`）与实际采用值
- `anonymous_pack.md`
- `gemini_summary.md`
- `verification.md`：逐条核实表
- `plan.md`：本轮计划，含核实结论与施工项
- `decisions.md`：Herhons 的批准或否决（时间、原话或指向）
- `changes.md`：实际改动与测试证据（命令、退出码、Ran / OK）
- `spec.diff`：Round 0 必有
- `STOP.md` 或 `CONTINUE.md`：为什么停，或为什么进入下一轮

入库的是这些文档和可公开报告。截图、完整 job JSON、账本放在 `evidence/iterations/round-N/`，不入库。

---

## 12. 停机条件

推进器默认自跑，**不逐轮等 Herhons**。出现下列情形之一时停轮，写 `docs/iterations/round-N/STOP.md`。

### A. 必须停下等 Herhons（不得自行绕过）

1. 需要花钱（付费额度、订阅、API 计费）；
2. 需要账号登录或注册；
3. 会删除用户数据（桌面文件、回收站、用户文档）；
4. 会改动 Bridge v0.0.8。

### B. 技术性停机（记录后结束循环，不等签字）

1. 会审齐了，验真后没有任何「真读」幸存意见，或核实后全部为不成立 / 已知取舍 / 待真机；
2. 连续两轮核实后可修的成立项为空，或只剩重复提出、已在上一轮归档否决且无新证据的意见；
3. 同一张计划连续推 3 次没有可验证进展（单测仍红，或改动被回滚）；
4. 凑不够 3 家有效会审，按第 7.3 节重试之后仍然不够；
5. 施工模型不可用，或会审 / 汇总因额度、窗口、锁冲突无法完成；
6. 修复后离线全量单测失败，且在本轮计划范围内无法在「一次回滚 + 一次重做」之内变绿；
7. 发现触碰红线（密码、注册表、误关用户窗口等）。

代码轮默认最多 5 轮（Round 1–5）。跑满 5 轮仍未收敛 → 写 STOP 汇总，**不再自行开第 6 轮**。
停机时 STOP 要说明触发了哪一条、已经完成什么、未决什么；不得把「还有几条看着像问题」当成继续跑的理由。

---

## 13. 编排器建造范围

允许复用现有 `bridge_server`、`tools/live_job.py`、提交账本、`tier_guard`、UI 锁。新建脚本放在 `tools/` 与 `src/qi/` 的合适位置，不要再复制一套发送栈。

最低交付：

- 能按第 4 节推进一轮。Round 0 与代码轮走同一管道，审查对象与「核心命中」路径可配置。
- 投喂方式可读配置（本轮默认，及按厂商覆盖），`upload` 与 `paste` 两条路径都要能走通。网页端上传须有自检；Round 0 必须证明上传可用。
- `tools/quote_check.py`（或同等模块）带离线测试：用 2026-11-11 的回复夹具断言当时真读的端仍为真读，并覆盖路径归一、代码核心命中、规格核心命中，以及空白归一后的引句查找。
- 材料打包器必须有测试锁死：源码里的 `**` 和反引号不得被剥掉。
- 匿名化脚本必须有测试：输出中不得出现作为来源的 `chatgpt` / `grok` / `gemini` / `deepseek`（大小写不敏感）及常见模型名。代码路径里偶尔出现这些词时，只允许出现在引用路径中，不得写成「来自某端」。
- 干跑模式：用固定夹具走完全管道，不碰真机 UI，便于验收。
- 状态机落盘。进程被杀后能从本轮已完成的步骤续跑，且不违反防重发（同一提问已 `accepted` 不得再发）。
- 变更说明与回归测试。

允许改的目录：`tools/`、`src/qi/`（编排状态与纯函数）、`tests/`、`docs/`（本规格、`iterations` 模板、README 索引）。禁止：碰注册表；把 `evidence/` 强行入库；改 `F:\Bridge\v0.0.8`；借机重构与编排无关的发送语义。

---

## 14. 编排器验收

1. 离线全量单测为绿。新增用例至少覆盖：验真阈值（含引句空白归一）、材料保真、投喂配置（`upload` / `paste`）、匿名化、无计划不得修、§10 四种硬停不得绕过、以及能单测的停机条件。
2. 干跑 Round 0：夹具规格 + 夹具四家回复 → 验真 → 匿名包 →（可用夹具代替真 Gemini）→ 计划；在测试临时目录检查 `round-0/` 结构完整。
3. 建造阶段不强制真机一次跑完四席。真正的第一次真机四席，就是第 5 节的 Round 0，且必须在规格已经提交并推送之后。Round 0 真机须证明网页端上传路径工作，不能只跑分段粘贴。若建造阶段做真机冒烟，只允许单席、低风险的连通性检查，并遵守 UI 锁与直播窗口红线。
4. 验收方为非 DeepSeek；证据三件套：命令、退出码、测试数。

---

## 15. 明确不做

- 不在本规格里重做「自动把 `unknown` 降级后重发」或拆掉 `ui.lock` 上生产并发（见 `known_gaps.md` 与 `docs/REVIEW-20261111.md` 的不做项），除非 Herhons 重新决策。
- 不把桌面端加进四审席位。
- 不让施工方模型同时担任本轮唯一核实人或唯一验收人。
- 不在会审提问或材料里泄露其他厂商的意见。
- 不等待红莉栖逐步递卡——本规格就是任务卡。但 §10 的四种硬停仍然有效。

---

## 附录 A. 与既有文档的关系

| 文档 | 关系 |
|---|---|
| `docs/REVIEW-20261111.md` | 本循环的人工样例：四审、验真、逐条核实、缩小范围、材料污染教训 |
| `evidence/review_20261111/` | 提问、原文、材料、验真脚本与报告，作夹具与口径来源 |
| `PLAN.md` | 红线、会审家数、档位、UI 锁、防重发、验收分工；本规格不推翻这些底线 |
| `CALIBER.md` | 状态机与验收口径；改它属于大改 |
| `docs/known_gaps.md` | 已决定不做的项；改它属于大改 |
| `docs/exit-codes.md` / `docs/tiers.json` | 发送与档位合同；改语义属于大改 |


---

## 二、本轮源码改动（unified diff）

```diff
diff --git a/src/qi/conversation.py b/src/qi/conversation.py
index 03ec3b9..e9a30df 100644
--- a/src/qi/conversation.py
+++ b/src/qi/conversation.py
@@ -176,6 +176,40 @@ ACCEPT_NAV_GRACE_READS = 2
 # 连续这么多次仍读不出才按 `identity_unreadable` 退出（与换会话分开记账）。
 IDENTITY_UNREADABLE_READS = 10
 
+# 2026-11-12：**用量硬墙**要快速失败，不能当"还在生成"等满整档预算。
+# 实测 chatgpt Work 额度用完时页面写着「你暂时已用完工作用量 / 升级订阅或添加额度…」，
+# 收卷侧看不出区别 —— 没有回复键、也不 busy，于是白等到 `REVIEW_TIMEOUT`（1500s）。
+# 这不是绕过额度（一个字节都不碰），只是把"等 25 分钟"换成"当场认输"。
+# 词条取**整句**而不是关键词：审查内容里出现"额度""升级"是常态，短词必误判。
+QUOTA_WALL = {
+    "chatgpt": ("你暂时已用完工作用量",
+                "升级订阅或添加额度",
+                "You've reached your limit",
+                "You’ve reached your limit"),
+}
+
+
+def quota_wall(site, view):
+    """页面是不是「用量已用完」的硬墙；命中返回那一条文案，否则 None。
+
+    只在**没有回复键**时判：有回复就说明这一轮确实有产出，额度提示可能是别的
+    残留（比如侧栏历史里的同类会话标题），不能拿它盖掉真答案。
+    """
+    markers = QUOTA_WALL.get(str(site or "").strip().lower())
+    if not markers:
+        return None
+    try:
+        if list(getattr(view, "copies", None) or ()):
+            return None
+        text = ui.text_of(view.root) or ""
+    except (RuntimeError, COMError, AttributeError):
+        return None
+    for marker in markers:
+        if marker in text:
+            return marker
+    return None
+
+
 # 2026-11-07（R6）：**同一个文档、同一个 URL** 的 SPA 会话切换。
 # opencode 的每个会话都是应用自己的一个标签页：切会话时 document runtime id 和
 # URL（`oc://renderer/index.html`）都不变，变的只有应用画出来的**会话名和标签条**。
@@ -604,6 +638,18 @@ class Conversation:
                     raise SendUnavailable(
                         f"发送键存在但全部不可用（{len(named)} 个）：端不接受输入",
                         present=len(named))
+                # 2026-11-12：**点不了发送**也可以是额度问题 —— 前提是页面自己说了。
+                # chatgpt 的 Work 额度用完后，输入区被额度卡替换，发送键要么不存在、
+                # 要么常年灰着；旧代码报 `interrupted`，账本记 unknown（BLOCKING），
+                # 这条提问就被永久拦住（要人工清账），而实际上一个字都没发。
+                # 有额度墙证据时归到 `send_unavailable`（可重试态），额度恢复即可重发。
+                # 判据用 `not buttons`（一个能点的都没有），不是 `not named` ——
+                # 实测额度墙下"发送"按钮还在，只是 disabled。
+                wall = quota_wall(self.site, view)
+                if wall and not buttons:
+                    raise SendUnavailable(
+                        f"发送键不可用（{len(named)} 个存在 / 0 个可用），"
+                        f"且页面提示用量已用完：{wall}", present=len(named))
                 raise RuntimeError("可用发送按钮: 找到 0 个控件")
             time.sleep(0.5)
             view = self.read()
@@ -705,6 +751,38 @@ def prompt_nodes(view, prompt):
     return found
 
 
+def merged_blocks(texts):
+    """把所有块拼成一整串，并记录每块占的字符区间。
+
+    **提问定位与答案取回共用这一处**（2026-11-12 Round 3 会审：两边各写一套，
+    改一边忘另一边）。markdown 强调符号要去掉——grok 把 `**` 渲染成独立节点，
+    留在串里会把提问切成两半。
+    """
+    parts, spans, offset = [], [], 0
+    for text in texts:
+        squashed = "".join(str(text or "").split()).replace("*", "")
+        parts.append(squashed)
+        spans.append((offset, offset + len(squashed)))
+        offset += len(squashed)
+    return "".join(parts), spans
+
+
+def prompt_span(texts, needle: str, start_at: int = 0):
+    """在拼接文本里找 `needle`，返回 `(首块, 末块, 结束偏移)`；找不到返回 None。"""
+    if not needle:
+        return None
+    merged, spans = merged_blocks(texts)
+    pos = merged.find(needle, start_at)
+    if pos < 0:
+        return None
+    end = pos + len(needle)
+    first = next((i for i, (_a, b) in enumerate(spans) if b > pos), None)
+    last = next((i for i, (_a, b) in enumerate(spans) if b >= end), None)
+    if first is None or last is None:
+        return None
+    return first, last, end
+
+
 def prompt_fragments(view, prompt, *, min_chars: int = 12, limit: int = 12):
     """提问被渲染成多块时，找出属于它的叶子文本节点。
 
@@ -1231,6 +1309,34 @@ def _local_chat_persisted(before, now) -> bool:
         and not new.startswith("/c/local-")
 
 
+def _first_turn_landed(before, now) -> bool:
+    """首轮新会话「落库」：容器地址 -> 该容器下的具体会话地址。
+
+    与 `_local_chat_persisted` 同一类，但按**路径形状**判，**不看第几次读**。
+
+    为什么必须按形状：gemini 换地址发生在**答案出现的那一刻**。2026-11-12 实测
+    新会话问「请只回复两个字：收到」：发送后地址一直是 `/app`，到第 16 秒答案
+    才出来，地址同时变成 `/app/1bd58577…`。旧判据是"接受后 2 次读之内"，而答案
+    出来时已经读到第 40 次上下 —— 明明发成功、答案也在页面上，却被判成
+    `conversation_changed`（`reason=document_changed`），一次会审就这样白跑。
+
+    只认「容器路径 -> 容器下的一段 id」：
+      · gemini   `/app`       -> `/app/<id>`
+      · chatgpt  `/c/local-…` -> `/c/<uuid>`（原 B17 路径，保留）
+    反例必须继续拒：`/app/<id1>` -> `/app/<id2>`（真的换了会话）。
+    """
+    old = ((before or {}).get("path") or "").rstrip("/")
+    new = (now or {}).get("path") or ""
+    if not old or not new:
+        return False
+    if _local_chat_persisted(before, now):
+        return True
+    if old != "/app":
+        return False
+    rest = new[len("/app/"):] if new.startswith("/app/") else ""
+    return bool(rest) and "/" not in rest
+
+
 def _rect_of(node):
     """控件的屏幕矩形；读不到返回 None（不当成 0，也不猜）。"""
     try:
@@ -1522,7 +1628,9 @@ def _ask_impl(conversation, prompt, timeout=180.0, pre_send=None, plain=False,
             # 这段完全跳过，老调用方行为不变。
             if ledger is not None:
                 if reserved_by_door:
-                    ledger.adopt()
+                    refused = ledger.adopt(written)
+                    if refused is not None:
+                        return refused
                 else:
                     refused = ledger.reserve(written)
                     if refused is not None:
@@ -1658,6 +1766,25 @@ def _ask_impl(conversation, prompt, timeout=180.0, pre_send=None, plain=False,
             heartbeat.beat_view("waiting", view, reads=hb_reads,
                                 accepted=accepted, baseline=baseline,
                                 composer_len=len(view.value or ""))
+            # 用量硬墙：页面明说额度用完，就别按"还在生成"等到整档预算。
+            # 只读判断，不改任何页面状态；命中即收工，让上层去决定等额度还是换端。
+            wall = quota_wall(getattr(conversation, "site", None), view)
+            if wall:
+                # 额度墙是页面上的硬事实：无论有没有被判过接受，都别再等下去。
+                # 但两种情形要分开记 —— 判过接受的那条记录**不许改口**：
+                # 改成 `not_sent` 会让账本降成可重发（B24 只拦旧记录，不拦本轮
+                # 刚 reserve 的 submitting），额度一恢复就会真的再发一遍。
+                if accepted:
+                    return {**state, "stage": "quota_exhausted",
+                            "reason": "quota_wall", "quota_text": wall,
+                            "hint": "页面提示用量已用完（硬墙）。这条 accepted 是在额度墙"
+                                    "期间写的，可能并没有真的发出去；额度恢复后若要重发，"
+                                    "先用 tools/submission_clear.py 清掉这条记录"}
+                return {**state, "stage": "quota_exhausted",
+                        "reason": "quota_wall", "quota_text": wall,
+                        "sent": False, "submission": "not_sent",
+                        "hint": "页面提示用量已用完（硬墙）：未发送，不重试、不绕过，"
+                                "等额度恢复或换端"}
             prompts = prompt_nodes(view, prompt)
             if not prompts:
                 # 整段匹配失败时退回「碎片认领」：只影响"提问还在不在"的判断，
@@ -1712,10 +1839,18 @@ def _ask_impl(conversation, prompt, timeout=180.0, pre_send=None, plain=False,
                     # conversation_changed）。这**不是**换会话，是同一段对话在落库，
                     # 所以允许重新锚定一次——但只认"我们的提问还在页面上"，且只认
                     # 这条临时 id → 真实 id 的窄路径，别的路径变化照旧拒。
-                    if prompts and _local_chat_persisted(accepted_ident, ident):
+                    #
+                    # 2026-11-12 扩成 `_first_turn_landed`：gemini 的落库发生在
+                    # **答案出现那一刻**（实测第 16 秒、读到第 40 次上下），
+                    # "接受后 2 次读之内"的窗口根本覆盖不到，于是判成换会话。
+                    if prompts and _first_turn_landed(accepted_ident, ident):
                         accepted_ident = ident
                         anchor_path = path_of(view.url)
-                        baseline = len(view.copies)
+                        # 落库迟到时答案可能已经上屏：**别把它划进基线**，
+                        # 否则重新锚定等于把刚到的答案当成历史，白等到超时。
+                        # 只有"确实还没有回复"才重设基线（原来那一支的语义）。
+                        if not _has_reply_after(nodes_now, prompts, view.copies):
+                            baseline = len(view.copies)
                         time.sleep(0.4)
                         continue
                     return {**state, "stage": "conversation_changed",
@@ -2130,13 +2265,27 @@ class _Ledger:
         self.request_id = (os.environ.get("BRIDGE_REQUEST_ID") or "").strip() or None
 
     # ------------------------------------------------------------ 提交前
-    def adopt(self):
+    def adopt(self, written=None):
         """门禁已经替本调用落过账（`pre_send` 回报 `reserved=True`）。
 
-        只登记"本轮预约过"，**不再读 pending**——再读一次就会把刚写的那条
-        submitting 当成上一次，把合法首发拦掉（B26）。
+        **不再读 pending**——再读一次就会把刚写的那条 submitting 当成上一次，
+        把合法首发拦掉（B26）。但要核对这条预约**是不是本次调用的**：
+        只信门禁的布尔值，过期回执会被当成本次预约，终态写账就会改掉旧记录。
         """
+        scope = None
+        if written is not None:
+            try:
+                scope = self._sub.scope_from_view(written, site=self.site)
+            except Exception:               # noqa: BLE001 - 读不到身份就不比对
+                scope = None
+        owned = self._sub.adopt_owned(self.site, self.prompt, scope=scope, root=self.root)
+        if not owned.get("ok"):
+            return {"ok": False, "stage": "tier_unconfirmed",
+                    "reason": owned.get("reason"), "code": tier_guard.TierUnconfirmed.code,
+                    "submission": "unknown", "sent": False,
+                    "hint": "预约不属于本次调用：不点击、不记账"}
         self.reserved = True
+        return None
 
     def reserve(self, written):
         """落账。返回 None = 放行；返回 dict = 就地拒发（原样交回调用方）。"""
diff --git a/src/qi/submissions.py b/src/qi/submissions.py
index 3a87e4f..3b4b4a4 100644
--- a/src/qi/submissions.py
+++ b/src/qi/submissions.py
@@ -637,6 +637,36 @@ def finish(endpoint, prompt, result, *, root=None, scope=None, now=None):
         _write(path, record)
 
 
+def adopt_owned(endpoint, prompt, *, scope=None, root=None) -> dict:
+    """这次调用能不能"认领"门禁事先写好的那条预约。
+
+    为什么需要：`_Ledger.adopt()` 原来只把 `reserved` 置 True，等于"门禁说预约过，
+    我就信"。过期回执（上一轮/别的会话留下的）于是会被当成本次预约，后续终态写账
+    就会改掉旧记录（2026-11-12 Round 3 会审三方都点了这条）。
+
+    判据只到「同一个窗口、同一个站」这一层：
+      · 记录不存在            -> reservation_missing
+      · 站点不同              -> site_mismatch
+      · 两边都有 hwnd 且不同  -> hwnd_mismatch
+    为什么不逐字段比 `reconcile` 那一套：发送时新会话还没生成 session_id，
+    doc_rid 也会因为新开对话而变；按那里比会把**每一次正常首发**都拒掉。
+    adopt 只需要挡住"上一轮/别的窗口留下的过期回执"，窗口级身份足够，
+    真正的严格比对在恢复阶段由 `reconcile()` 做。
+    """
+    record = read(endpoint, prompt, root=root)
+    if not record:
+        return {"ok": False, "reason": "reservation_missing"}
+    stored = clean_scope(record.get("scope"))
+    now = clean_scope(scope)
+    if stored and now:
+        if stored.get("site") and now.get("site") and stored["site"] != now["site"]:
+            return {"ok": False, "reason": "site_mismatch"}
+        if stored.get("hwnd") is not None and now.get("hwnd") is not None \
+                and stored["hwnd"] != now["hwnd"]:
+            return {"ok": False, "reason": "hwnd_mismatch"}
+    return {"ok": True, "v2_state": (normalize(record) or {}).get("v2_state")}
+
+
 #: 允许降级的证据键。三项里 `page_empty` 必填，另两项至少一项——
 #: 单凭"页面空白"不足以降级（可能只是还没渲染），必须再有"身份/证据不支持"的旁证。
 DEMOTE_REQUIRED = "page_empty"
diff --git a/tools/iterate.py b/tools/iterate.py
index 2cefe98..a6cb59d 100644
--- a/tools/iterate.py
+++ b/tools/iterate.py
@@ -44,6 +44,27 @@ def load_config() -> dict:
     return cfg
 
 
+def _review_budget(cfg) -> int:
+    """会审等答案的预算：**必须**过一遍 `tier_guard.floor_timeout`。
+
+    规格 §7.1：`purpose=review` 走 `REVIEW_TIMEOUT` 下限，不得把高档会审判成
+    小超时。`*_use.py` 的 CLI 入口本来就会 floor，但 `iterate.py` 是直接调
+    `conversation.ask` / `subprocess.run`，绕过了那层 —— 2026-11-12 实测：
+    配置写 480，`conversation.ask` 就在 480s 收工（Round 4 chatgpt 582s 报 timeout
+    正落在这条线上），而同一份配置交给 `gemini_use.py` 时子进程自己抬到 1500，
+    **父进程 `timeout=budget+180` 反而先把子进程杀了**，答案就在页面上却收不回来。
+
+    所以这里统一取 floor 后的值，父进程的 subprocess 超时也基于它算。
+    """
+    from qi import tier_guard
+
+    try:
+        raw = float((cfg or {}).get("review_timeout") or DEFAULT_CONFIG["review_timeout"])
+    except (TypeError, ValueError):
+        raw = float(DEFAULT_CONFIG["review_timeout"])
+    return int(tier_guard.floor_timeout("review", raw))
+
+
 def git(*args: str) -> str:
     return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8").stdout.strip()
@@ -265,36 +286,17 @@ def _join_by_lines(items) -> str:
 
 
 def _merged_blocks(texts):
-    """把所有块拼成一整串，并记录每块占的字符区间。
+    """委托：实现只在 `qi.conversation.merged_blocks`（定位与取回共用一套规则）。"""
+    from qi import conversation
 
-    为什么要拼：grok 把一条提问拆成 9~10 个节点（`**`、`只依据第一章`、`**`…），
-    任何"单块匹配"的判据都会在气泡中间断掉。拼起来才能确定气泡的**起止块**。
-    """
-    parts, spans, offset = [], [], 0
-    for text in texts:
-        # 去掉 markdown 强调符号：grok 把 `**` 渲染成**独立节点**，
-        # 留在拼接串里会把提问切成两半，needle 永远匹配不上（2026-11-12 实测）。
-        squashed = "".join(text.split()).replace("*", "")
-        parts.append(squashed)
-        spans.append((offset, offset + len(squashed)))
-        offset += len(squashed)
-    return "".join(parts), spans
+    return conversation.merged_blocks(texts)
 
 
 def _prompt_span(texts, needle: str, start_at: int = 0):
-    """在拼接文本里找 `needle`，返回 `(首块, 末块, 结束偏移)`；找不到返回 None。"""
-    if not needle:
-        return None
-    merged, spans = _merged_blocks(texts)
-    pos = merged.find(needle, start_at)
-    if pos < 0:
-        return None
-    end = pos + len(needle)
-    first = next((i for i, (_a, b) in enumerate(spans) if b > pos), None)
-    last = next((i for i, (_a, b) in enumerate(spans) if b >= end), None)
-    if first is None or last is None:
-        return None
-    return first, last, end
+    """委托：实现只在 `qi.conversation.prompt_span`。"""
+    from qi import conversation
+
+    return conversation.prompt_span(texts, needle, start_at=start_at)
 
 
 def is_progress_noise(text: str) -> bool:
@@ -786,7 +788,7 @@ def _ask_upload_body(site: str, prompt: str, material: Path, cfg, state=None,
         state.mark_chunk(prompt_id, "sending")     # 发送**之前**落盘，崩溃后不留空白
     _refresh_tier(site)                  # 回执一次性；上一次失败也会把它用掉
     conv = _conversation(screen, site)
-    result = conversation.ask(conv, ask_text, timeout=cfg["review_timeout"],
+    result = conversation.ask(conv, ask_text, timeout=_review_budget(cfg),
                               purpose="review")
     if state is not None:
         state.mark_chunk(prompt_id, "sent" if result.get("ok") else "unknown",
@@ -795,14 +797,20 @@ def _ask_upload_body(site: str, prompt: str, material: Path, cfg, state=None,
                      prompt_id, accepted=bool(result.get("ok")
                                               or result.get("submission") == "accepted"))
     if not result.get("ok") and result.get("stage") in ("timeout", "interrupted", "harvest_timeout"):
+        # 兜底会**覆盖** result：把 ask 的失败细节先留一份，否则事后完全看不出
+        # 为什么这一席要走兜底（Round 3 的 grok 就是这样丢掉了 timeout_kind）。
+        failure = {key: result.get(key) for key in
+                   ("stage", "timeout_kind", "reason", "diagnostics", "warnings")
+                   if result.get(key) is not None}
         harvested = harvest_reply(screen, site, wait=float(cfg.get("harvest_wait", 420)))
         if harvested["ok"]:
-            result = harvested
+            result = {**harvested, "ask_failure": failure}
         else:
             # 复制键点不到时，直接从控件树读（答案确实在，只是动作条不可点）。
             text = read_answer_text(screen, site, ask_text)
             if len(text) >= 300:
-                result = {"ok": True, "stage": "text_extracted", "reply": text}
+                result = {"ok": True, "stage": "text_extracted", "reply": text,
+                          "ask_failure": failure}
     result["feed"] = "upload"
     result["upload"] = up.as_dict()
     return result
@@ -849,7 +857,7 @@ def _ask_upload_script(site: str, prompt: str, material: Path, cfg, state=None)
         state.mark_chunk(prompt_id, "sending")
     _refresh_tier(site)                  # 回执一次性，发送前重拿一条
     script = ROOT / "src" / "scripts" / f"{site}_use.py"
-    budget = int(cfg["review_timeout"])
+    budget = _review_budget(cfg)
     proc = subprocess.run(
         [sys.executable, str(script), ask_text, "--purpose", "review",
          "--continue", "--timeout", str(budget)],
@@ -972,7 +980,7 @@ def _ask_via_script(site: str, prompt: str, material_text: str, cfg,
                 state.mark_chunk(chunk["id"], "sent", reconciled=True)
                 continue
         _refresh_tier(site)              # 档位回执是一次性的：每段都要重拿
-        budget = cfg["review_timeout"] if index == total else int(
+        budget = _review_budget(cfg) if index == total else int(
             cfg.get("chunk_timeout", 300))
         cmd = [sys.executable, str(script), body, "--purpose", "review",
                "--timeout", str(int(budget))]
@@ -1019,7 +1027,7 @@ def _ask_paste(site: str, prompt: str, material_text: str, cfg, state=None) -> d
     if chunks:
         _clear_composer(_screen_for(site), site)      # 上一轮残草会判 draft_present
     for index, chunk in enumerate(chunks, start=1):
-        budget = cfg["review_timeout"] if index == len(chunks) else int(
+        budget = _review_budget(cfg) if index == len(chunks) else int(
             cfg.get("chunk_timeout", 300))
         last = flow.ask(site_obj, chunk, mode=("new" if index == 1 else "continue"),
                         timeout=budget, purpose="review", verbose=False)
@@ -1246,7 +1254,7 @@ def cmd_summarize(args, cfg) -> int:
                     "2) 标出冲突；3) 按对本项目的好处排序；4) 你要补充的新缺陷单独列在「汇总者新增」。"
                     "不要猜来源，1500 字内。")
         script = ROOT / "src" / "scripts" / "gemini_use.py"
-        budget = int(cfg["review_timeout"])
+        budget = _review_budget(cfg)
         proc = subprocess.run([sys.executable, str(script), short, "--purpose", "review",
                                "--continue", "--timeout", str(budget)],
                               cwd=ROOT, capture_output=True, text=True,
@@ -1272,7 +1280,7 @@ def cmd_summarize(args, cfg) -> int:
                 return 1
             _refresh_tier("gemini")          # 回执一次性
             conv = _conversation(screen, "gemini")
-            result = conversation.ask(conv, prompt, timeout=cfg["review_timeout"],
+            result = conversation.ask(conv, prompt, timeout=_review_budget(cfg),
                                       purpose="review")
 
     url = ""
@@ -1330,7 +1338,7 @@ def cmd_upload_check(args, cfg) -> int:
     _upload_settled(site, screen)
     conv = _conversation(screen, site)
     result = conversation.ask(conv, "附件里那行关键内容是什么？只回答那一行，不要解释。",
-                              timeout=cfg["review_timeout"], purpose="review")
+                              timeout=_review_budget(cfg), purpose="review")
     reply = (result.get("reply") or "").strip()
     ok = token.lower() in reply.lower()
     payload = {"ok": bool(result.get("ok")) and ok, "upload": up.as_dict(),

```

## 三、回归用例清单（只列名字，正文在仓库里）

### `tests/test_b31_adopt_owned.py`
B31：门禁写好的预约必须核对"是不是本次调用的"。

- `test_matching_scope_is_accepted`
- `test_different_window_is_refused`
- `test_different_session_alone_does_not_block`
- `test_missing_record_is_refused`
- `test_unreadable_scope_does_not_block`

### `tests/test_b31_ask_failure.py`
B31：走兜底时必须保留 ask 的失败细节。

- `test_failure_snapshot_is_taken_before_fallback`
- `test_both_fallback_paths_carry_it`

### `tests/test_b31_prompt_rule_shared.py`
B31：提问定位与答案取回必须共用同一套匹配。

- `test_same_input_same_span`
- `test_tail_echo_agrees_too`
- `test_tools_does_not_reimplement_it`

### `tests/test_b32_first_turn_landed.py`
B32：首轮新会话「落库」判据要按路径形状，不能按"读了几次"。

- `test_gemini_container_to_session_is_landing`
- `test_chatgpt_local_to_real_is_still_landing`
- `test_another_session_under_the_same_container_is_not_landing`
- `test_unrelated_path_change_is_not_landing`
- `test_late_landing_is_not_a_conversation_change`
- `test_answer_already_visible_when_landing_happens_is_collected`
- `test_moving_to_another_session_is_still_refused`

### `tests/test_b33_review_budget.py`
B33：`iterate.py` 的会审预算必须过 `tier_guard.floor_timeout`。

- `test_small_config_is_raised_to_the_review_floor`
- `test_larger_config_is_kept`
- `test_missing_or_bad_config_still_floors`
- `test_default_budget_is_not_below_the_floor`
- `test_no_raw_review_timeout_in_calls`
- `test_only_the_last_chunk_gets_the_review_budget`

### `tests/test_b34_quota_wall.py`
B34：用量硬墙要快速失败，不能当"还在生成"等满整档预算。

- `test_chatgpt_wall_is_detected`
- `test_reply_beats_the_wall`
- `test_other_sites_ignore_it`
- `test_clean_page_is_not_a_wall`
- `test_wall_ends_the_turn_without_waiting`
- `test_wall_does_not_overwrite_an_accepted_turn`
- `test_wall_without_any_send_button_is_retryable`
- `test_wall_with_a_disabled_send_button_is_retryable`
- `test_without_wall_evidence_keeps_the_original_error`
- `test_wall_on_another_site_does_not_change_the_error`

---

## 四、请重点回答

1. `adopt_owned()` 只比到窗口级（site/hwnd）是否足够挡住过期回执？
   按 `reconcile()` 逐字段比会把正常首发全拒（旧用例已证），这个折中是否可接受？
2. `prompt_span()` 收成一处后，收卷主路径仍用单块匹配 + 碎片兜底。
   这种「两套入口、一套实现」是否够，还是必须把主路径也换掉？
3. `ask_failure` 只保留 5 个字段，够不够事后定位？还该带什么？
4. `_first_turn_landed()` 按路径形状判「首轮落库」：`/app` → `/app/<id>`、
   `/c/local-…` → `/c/<uuid>`。这个白名单还会漏掉哪些端/形态？会不会放走真的换会话？
5. `quota_wall()` 只认四句整词、且有回复键就不判。会误伤吗（比如审查内容本身含
   「升级订阅或添加额度」）？还该加什么护栏？
6. 这批改动最可能引入什么回归？（剪贴板、账本、收卷各给一个具体场景）
7. 其余自由。
