# Round 3 核实表

快照：`f67fd8d2`。输入：`gemini_summary.md`（chatgpt 代汇总）+ 三份真读原文。
核实方式：本地读码 + 真机复现；每条给 `文件:行号` 或可复现命令。

| # | 汇总项 | 判定 | 依据 | 处置 |
|---|---|---|---|---|
| R3-1 | 页面重置后 accepted 永久拦截，需降级规则 | **成立** | `submissions.py:57` `BLOCKING_STATES` 含 accepted；`demote()` 原本不存在 | **已完成**：`demote()` 只允许 accepted→unknown、一次性、留审计；`pending()` 仍拦截（`tests/test_b30_submission_demote.py` 7 条） |
| R3-2 | 写账所有权（reservation_id），拒发不得改写旧账 | **成立** | `conversation.py:_Ledger.finish` 只判 `self.reserved` 布尔，没有绑定本次预约身份；过期回执可被当成本次预约 | **留下一轮**（要动 `_Ledger`/门禁接口，风险高于本轮） |
| R3-3 | 孤儿锁应统一入口，失败语义要明确 | **部分成立** | `uilock.hold()` 已按 `_holder_dead` 自愈（Round 0 第 3 项），所以 `*_use.py` 已被覆盖；真正缺的是**不碰网页的入口**（sisyphus）没有体检 | **已完成**：`sisyphus.lock_housekeeping()`，清理失败只告警不阻断（它不动 UI） |
| R3-4 | 剪贴板要写后校验、按归属恢复 | **成立** | `conversation.py:_paste_via_clipboard` 原来只比序列号、且无条件恢复原剪贴板 | **已完成**：`clipboard_matches()` 写后逐字核对（不一致重写一次再拒）、恢复前确认仍属本事务 |
| R3-5 | 收卷要定位与取回共用同一套匹配 | **成立** | grok 提问被拆成 9~10 节点时 `prompt_nodes()` 返回 0 → `prompt_gone`；取回侧已有 `_prompt_span`，收卷侧没有 | **部分完成**：收卷侧加 `prompt_fragments()` 仅做存在性判断（足够挡住误判）；主路径统一留待下一轮 |
| R3-6 | 驱动第一步只抽最稳路径，并静态防第三套栈 | **成立** | `src/qi/drivers/__init__.py` 只有协议，无实现；无任何静态约束 | **部分完成**：新增 AST 检查（禁 import 发送栈/账本/锁、禁 ask/reserve/pending）；实现迁移留下一轮 |
| R3-7 | 上传统一绝对路径 | **成立** | 已修 `_absolute()`；但**路径不存在**时仍会弹对话框并卡住 | **已完成**：入口加 `Path(path).is_file()` → `file_not_found` |
| R3-8 | 验真：章节等价写法 + 空白归一 | **成立（且比会审说的更严重）** | `_squash` 只去空白；Round 1–3 实测大量引句**逐字正确**，仅因模型丢掉行内代码的反引号而判不中 | **已完成**：`_squash` 同时去 `` ` ``/`*`；三轮复算全部升为 true_read |
| 附 | 汇总者新增：匿名包带来源署名与界面残渣 | **成立** | 实测包首行是 `ChatGPT说：可访问=是`；根因 `\bChatGPT\b` 在中文前不成立 | **已完成**：去说话人/残渣 + `assert_anonymous()` 泄漏即抛 |

## 不成立 / 不改

- 「F3-1 用新 purpose 绕过旧账」：与会审主流（继续阻断）冲突，且会让账本可被"换个 purpose"绕过 → **不采纳**。
- 「status 也清锁」：纯读命令不该改盘 → 只在 `next/done` 与 UI 调度前清理，`status` 只报告。

## 冲突裁定

- 降级门槛：采纳 F1/F2 的「page_empty 必填 + scope_mismatch 或 no_l3 之一」；不采纳"只凭页面空白"。
- 剪贴板恢复：采纳「先确认序号/内容仍属本事务再恢复」。
