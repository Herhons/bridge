# 退出码 / stage / 档位回执（CARD-06a (d)）

> 这份表是**唯一一张**给人看的退出码表。代码里的常量在 `src/qi/exit_codes.py`，
> 两边对不上的时候以本文件和那个模块为准（`tests/test_card06a.py` 会检查本文件确实列出了每一行）。

## 一、统一退出码

| 码 | 含义 | 谁在用 |
|----|------|--------|
| 0 | 正常：`done` / `already` / `done_no_chip` / `list` / `probe` / `noop` / `answered` / `read` | 全部脚本 |
| 1 | 通用失败：`no_window`、`controls_not_found`、`unverified`、读不出档位、`unavailable` | 全部脚本 |
| 2 | 参数错（argparse 默认；也是「没给任何动作」） | 全部脚本 |
| 3 | 资源库缺模板（`Library.missing`） | 各 `*_choose` / `*_use` 入口 |
| 4 | 让位：`user_busy` / `user_took_over` / `not_foreground` / `aborted` | `guard` 与各 CLI |
| **5** | **档位回执不成立**：`stage=tier_unconfirmed` | `*_use`（发送前闸门） |
| **6** | **UIA 读不到文档**：`stage=uia_docs_lost` | `workbuddy_*`、`opencode_*`、网页端 `flow`/`gemini` |
| **7** | **窗口最小化且恢复失败**：`stage=window_minimized` | 各端取窗口处 |
| **8** | **UI 前台锁被占**：`stage=ui_busy`（`runs\ui.lock` 被另一个自动化占着，等超时） | `uilock.run`（各 `*_use` / `*_choose`） |
| **9** | **发送中途前台被换走**：`stage=concurrent_ui`（已清草稿退出） | 各 `*_use` 发送链路 |

### 冲突核对（做卡时逐脚本 grep 过）

- `src/` 里原先**没有任何** `return 5` / `return 6` / `return 7`。
- argparse 自己只用 **2**（参数错）和 **0**（`--help`）；`--help` 走 `SystemExit(0)`，不会被当成失败。
- **3** 已被「缺模板」占用、**4** 已被「让位」占用，所以新码从 5 起。
- 唯一语义不一致的是 `src/scripts/workflow_use.py`：它自己的文档串里 **3 = advance 后需要监工**。
  那是工作流状态机（只读写 `--file` 指定的 JSON，不开窗口、不发消息），
  与 5/6/7 无交集，保留原语义；**不要**拿它的 3 当「缺模板」看。

## 二、stage → 退出码

`exit_codes.STAGE_CODES` 里的映射（没有列进来的 stage 一律按 1 算，绝不默认成 0）：

| stage | 码 | 说明 |
|-------|----|------|
| `done` / `already` / `done_no_chip` / `list` / `probe` / `noop` / `skipped` / `answered` | 0 | 正常结束 |
| `bad_arg` / `bad_args` / `usage` | 2 | 参数问题 |
| `missing_assets` | 3 | 缺模板 |
| `user_busy` / `user_took_over` / `not_foreground` / `aborted` | 4 | 让位或中止 |
| `tier_unconfirmed` | 5 | 回执缺失/不符/过期/漂移/读不回档位 |
| `uia_docs_lost` | 6 | 唤醒 3 轮后仍然是 0 个文档 |
| `window_minimized` | 7 | 最小化且没能恢复 |
| `ui_busy` | 8 | `runs\ui.lock` 被另一个自动化占着（CARD-09f 第 1 条，独立于让位 4） |
| `concurrent_ui` | 9 | 发送中途前台标签/文档被换走，已清草稿（CARD-09f 第 3 条） |
| `already_answered` | 0 | 对话里已有本提问之后的回复，只读不发送（CARD-09f 第 6 条） |
| `zoom_not_100` | 1 | 页面缩放不是 100% 且 Ctrl+0 之后仍不是（CARD-06b 第 6 条） |
| `human_check` | 1 | 页面停在人机验证 / Unknown error，需要人来处理（CARD-06b 第 8 条） |
| `no_model_selector` | 1 | 强度弹层里没有「选择模型」入口，本模式没有模型选择器（CARD-06c 第 3 条，fail-closed） |
| `session_not_open` | 1 | 窗口停在首页/会话列表、没有 composer：开消息/选档**之前**就拒（UI-BOOTSTRAP-3；不点、不打字，也不用占位提问去把 composer 顶出来） |

## 三、档位回执（`src/qi/tier_guard.py`）

- `*_choose` 读回确认后原子写 `runs\tier\<端>.json`（临时文件 + `os.replace`）。
- `*_use` 发送前必须过闸：校验 → 只读复核 → 在 `runs\tier\<端>.lock` 上加排它锁把回执标成
  `consumed`。**失败、部分失败也作废**，必须重新 choose。
- 有效期 **120 秒**；时间戳在未来（超过 2 秒时钟抖动）直接拒；绑 `hwnd + pid + 进程启动时间 + tab`。
- 档位表在 `docs/tiers.json`（`test`=最低档，`review`/`accept`=最高档），代码里不写死端名和档名。
- `--purpose test|review|accept|accept_lite` 无默认值：**真发送时必填**；
  `--list`（`workbuddy_use`、`opencode_use --list`）和 `--read`（`gemini_use`/`grok_use`）不发送，不要求。

### 用途与验收结论（CARD-06c 第 2/6 条）

| purpose | 含义 | 结论怎么写 |
|---------|------|-----------|
| `test` | 最低档，只验链路 | — |
| `review` | 最高档，会审 | — |
| `accept` | 最高档，验收 | `PASS` |
| `accept_lite` | 独立白名单，只给 opencode 的 GLM-5.3-Flash-Free | `PASS(GLM-lite)` |

`accept_lite` **不是** accept 的低配版：它只证明"这条链路能收发"，不能当成旗舰档验收过。
所以结论必须写 `PASS(GLM-lite)` 以示区分（`tier_guard.purpose_label()` / `opencode_use` 的输出里带
`purpose_label` 字段）。它只登记在 `tiers.json` 的 `opencode.model` 里，别的端用了会按
`purpose_not_in_table` 拒发。

### 按模式取白名单（CARD-06c 第 2 条）

回执里记 `writer`（ChatGPT 的 chat/work）。`tiers.json` 的维度条目可以带 `writers.<模式>` 覆盖表：

    chatgpt.model  默认 = 工作模式的 8 档；writers.chat = gpt56 / gpt6
    chatgpt.effort 默认 = 工作模式五档；  writers.chat = instant / mid / high

`use` 按回执里的 `writer` 取表：端上声明了 `writer_modes` 时，回执里的模式必须在里面（否则
`writer_not_in_table`）；该模式没有覆盖表就用默认表（work 就是这么走的，不用把 8 个档抄两遍）。
老回执没有 `writer` 字段的，仍旧按默认表（向后兼容）。

### 06b 起新增的 `reason`（都在 `stage=tier_unconfirmed` 或 `stage=human_check` 下）

| reason | 含义 | 谁产生 |
|--------|------|--------|
| `tier_unreadable` | 发送前读不回档位（模板低于阈值、读回为空、同一模板多处命中、UIA 也读不到） | `tier_guard.gate`（CARD-06b 第 1 条） |
| `tier_drift` | 读到了，但与回执里的 `readback` 不符；use 不切档、不重选 | `tier_guard.gate`（第 2 条） |
| `receipt_identity` | 回执绑定的 `hwnd / pid / process_start_time` 对不上（换窗口或客户端重启） | `tier_guard.check`（第 4 条） |
| `page_changed` | 档位复核之后、点击之前页面导航/新对话/窗口切换了，拒发 | `qi.sendgate.page_confirm`（第 3 条） |
| `writer_not_in_table` | 回执里的 `writer` 在 `tiers.json` 里没登记（CARD-06c 第 2 条），不回落默认表 | `tier_guard.check` |
| `cloudflare` | 人机验证页面（脚本不点、不重试，交给巡检层） | `qi.human.HumanCheck`（第 8 条） |
| `unknown_error` | 页面自己的「Unknown error」，不归到人机验证里 | 同上 |

### 失败留证（CARD-06b 第 9 条）

`tier_*`、`zoom_*`、`human_check`、`input_mismatch` 失败时，统一落：

    evidence\fail\<端>_<YYYYmmdd-HHMMSS>.png    目标窗口（拿不到就整屏）
    evidence\fail\<端>_<YYYYmmdd-HHMMSS>.json   统一结构（stage/reason/hwnd/window.zoom_percent/templates）
    runs\YYYYMMDD.jsonl                          追加一行 result=fail_evidence

截图失败只影响证据，不影响主流程；JSON 里 `screenshot` 记 null。

### 已知限制（如实登记，不假装做到了）

1. **workbuddy 的档位表没登记最低/最高档**：模型列表由账号下发、菜单要点击才展开，
   没有离线可枚举的清单。所以本端 `tier_check` 记为 `table_unconfigured`：
   回执仍然必须存在、新鲜、与 UIA 当前模型一致，但不比「最低/最高」。补齐 `docs/tiers.json` 后自动生效。
2. **codex 仍然不做只读复核**：Codex 的档位只有打开卡片才读得到，开卡片要点两下，已经不是只读。
   硬做等于「use 顺手动界面」，比不做更危险。06b 起它显式传 `require_readback=False`，
   结果里带 `tier_readback="unsupported"`——**是登记过的未验，不是静默放行**；
   只读通道属 CARD-06c 的适配项。dsh / opencode 走模板芯片读取，读不到即 `tier_unreadable`。
3. **chatgpt 的聊天模式读不到模型名**：芯片只显示强度（`高`），模型名只在列表里
   （要点击才展开，不算只读）。所以聊天模式的 model 回执在 use 侧会按
   `tier_unreadable` 拒发；强度回执（chip 的档位词，instant/mid/high）可以正常复核。
   → 聊天模式要发得出去，choose 的**最后一步必须是 `--effort`**（回执落在 effort 维度）。
   CARD-06c 第 2 条只把聊天模式的表登记进去、让 use 按 writer 取表；
   "只读读回聊天模型名"仍然需要点击展开列表，本轮没有做。
4. **没 chip 模板的档位不能用作回执**：`grok auto/build`、`chatgpt` 的「默认（推荐模型集）」
   点完读不回原名，`readback` 记 `null`，use 会因为 `readback_missing` 拒发——这是设计如此。
5. **回执不防恶意伪造**：本机单用户，谁都能写 `runs\tier\*.json`。它挡的是「忘了切档 / 拿旧回执重放」，
   不是「有人故意骗脚本」。所以不引入密钥、不做签名（加了也挡不住同机同用户，只会制造错觉）。

## 四、按退出码排查

| 看到 | 先看哪里 | 怎么办 |
|------|----------|--------|
| 5 | `runs\tier\<端>.json` 在不在、`age` 多少 | 重跑对应的 `*_choose`，**紧接着**发（120 秒内） |
| 6 | `runs\YYYYMMDD.jsonl` 里 `result=uia_docs_lost` 的 `uia_probe` 行 | 看 `docs`/`urls` 每轮是多少；客户端该重启就由人重启，脚本不杀进程 |
| 7 | 同上，`stage=window_minimized` | 手动把窗口还原再跑；脚本不会拿 `-32000` 哨兵坐标去点 |


### WIP 06c: composite receipt

ChatGPT requires `composite_version=1`, `writer`, and `dimensions.model/effort`
with target/readback pairs from one final tree snapshot. The primary scalar
fields remain for server receipt checks. A legacy single-axis receipt is rejected
with `composite_missing` / exit 5. Before submit, one read-only UIA snapshot must
confirm writer, model and effort; missing labels are `tier_unreadable`, any drift
is `tier_drift`. If the current chip exposes only effort, sending fails closed.
No model is inferred from a previous receipt or a menu selection command.

---

## 五、采集停摆错误码（T3-1，2026-11-07）

读不到回答时不再一路静默等到超时。`wait_reply` 连续 3 次读到**同一类**结构问题时
立刻返回，并在结果里带 `error_code`。`stage` 仍然是 `timeout`（老调用方按它分支），
另加 `timeout_kind=stalled` 区分。

| `error_code` | 含义 | 还能不能等 |
|---|---|---|
| `STREAMING_IN_PROGRESS` | 页面还在生成（停止键可点） | **能等**——这是唯一的例外 |
| `CONTAINER_BOUNDARY_UNPROVEN` | 有文字，但没有可证明的消息容器边界 | 不能等，等下去也不会变好 |
| `ATTRIBUTION_UNVERIFIED` | 读到了回复，但证明不了它是本轮答案 | 不能等 |
| `SESSION_ID_MISSING` | 账本里没有会话 id，这条提交永远无法自动恢复 | 不能等 |
| `REPLY_NEVER_APPEARED` | 已经收下了，但超过 `TRANSIENT_MAX_SECONDS`（120s）仍然读不到任何回答正文 | 不能等（B10 新增） |

判定细则：
- 只在**连续 3 次完全相同**的结构原因时判死；抖动（`loose_text`/`no_text` 交替）不判。
- `no_text` / `no_copy` / `no_group` 属"这一轮还没读到"，**容忍 120 秒**
  （B10 起不再是"永远不判死"：此前一个从头读不到复制键的页面会一路等到 900 秒）。
- `busy=True` 时任何原因都不判死，继续等。
- 早退目标 3–5 秒，远早于 F5 的 10 秒上限。

实现见 `src/qi/replycodes.py`（纯函数，可离线穷举）与 `src/qi/workbuddy.py` 的
`reply_text_detail` / `attributed_reply_detail` / `wait_reply`。
