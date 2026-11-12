# Round 1 核实表（逐条对快照/源码）

快照：`90efeda`（`snapshot.json`）
输入：`gemini_summary.md` 六组聚类 + `appendix_partial.md`（chatgpt/grok，未进匿名包）
核实方式：本地读码，逐条给出 `文件:行号`；不采信会审原文的措辞。

| # | 汇总项（聚类） | 判定 | 依据（文件:行号） | 本轮处置 |
|---|---|---|---|---|
| 1 | UI 焦点/输入防误触硬闸门 | **成立** | `src/qi/upload.py:80`（`_raw_click` 裸坐标点击，无归属校验）；`tools/iterate.py:252`（`_clear_composer` 仅事后比对前台）；`src/qi/screen.py:715/831`（`click_xy`/`hotkey` 直接投递） | **施工**（S1） |
| 2 | 幂等状态探针 + 四阶段账本 | **部分成立** | `tools/iterate.py:451/506`（`start_new_chat` 在发送路径上可重复调用）；`submissions.py:45-50` 只有 draft/submitting/accepted/unknown/failed_*，**没有 dispatched**；超时重试逻辑在 `conversation.py` 内，本轮不动 | 本轮只做 S3（会话锚点，抑制"超时即新开"）+ 记账字段说明；四阶段账本留下一轮 |
| 3 | 会话锚点与漂移自愈 | **成立** | `tools/iterate.py:130`（`start_new_chat` 只验"地址无会话 id"）；无"回到本轮会话"的统一入口 | **施工**（S3） |
| 4 | 统一总控 + 无状态端插件 | **成立（但属重构）** | `src/scripts/` 20 个脚本、`src/qi/` 39 个模块并行发送栈（`material.md` §2.1/2.2 已列）；`tools/iterate.py` 与 `*_use.py` 各有一套发送/收卷 | **本轮只落骨架**（S4：`tools/sisyphus.py` + `src/qi/drivers/__init__.py` 协议），不迁实现 |
| 5 | 推进器职责边界 + 独立核实 | **成立** | `tools/iterate.py:637`（`cmd_verify`）与 `:848`（`cmd_summarize`）目前由同一进程完成，无独立核实席；`verification.md` 本文件即人工核实产物 | 记录为流程约束（本轮由推进器逐条读码核实），独立席位留待接入非施工模型 |
| 6 | 上传/材料形态解耦的验真 | **成立** | 今日已改：`src/qi/iteration.py` 的 `verify_reply` 增 `core_text`，`cmd_verify` 用「规格+材料」做语料、只用规格判核心命中（见 `changes.md` 第 21 节） | **已完成**（本轮前半段） |
| 附 | 汇总者新增：剪贴板事务锁 | **部分成立** | `tools/iterate.py` 有 `clipboard_seq` 校验（harvest 阶段），但 paste 路径无"写后校验 SHA" | 留待下一轮 |
| 附 | F1 尾部混入 3 段原始 prompt | **成立** | `tools/iterate.py:178` `read_answer_text` 只按"提问回声"截断，未过滤**尾部**复述；实测 `replies/chatgpt.md` 末尾有 prompt 片段 | **施工**（S2） |

## 不成立 / 不改

- 「按行数判断该不该合并」（chatgpt 4））：行数不是判据，本轮不据此决策；架构按"职责边界"拆，不按行数。
- 「必须改 CDP/Playwright」：本轮不引入新依赖（会触碰"花钱/账号"边界），先用 UIA 定向 + 硬闸门收口。

## 冲突裁定

- 会话找回失败后的兜底：采纳 **已 accepted 就不新建**（F2-3），未 accepted 才允许回根新建。
- 迁移顺序：采纳 **先抽协议 + 薄壳**（F1-B），不在本轮直接替换四端实现。
