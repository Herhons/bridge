# Round 2 核实表

快照：`be18ac3`。输入：`gemini_summary.md`（2 家真读）+ `appendix_partial.md`。

| # | 汇总项 | 判定 | 依据 | 处置 |
|---|---|---|---|---|
| R2-1 | S1 仍可被物理绕过（同进程弹窗、iframe、DPI、重排、焦点滞后） | **成立** | `safe_ui.decide_point` 原只比 pid 与客户区，`WindowFromPoint` 命中的可能是同进程弹窗；无布局快照比对 | **已修**（P1：加 `hit_top_hwnd`/`expected_rect` 校验；`point_verdict` 取 `GetAncestor(GA_ROOT)`） |
| R2-2 | 四阶段账本（staged→dispatched→accepted→completed） | **成立** | `submissions.py` 只有 draft/submitting/accepted/unknown；`_Ledger` 在无上屏证据时也可能写 accepted（本轮又见一次：gemini 汇总假 accepted） | 留下轮（需动发送栈核心） |
| R2-3 | 会话锚点未接线 | **成立** | `_ask_upload_body` 发送前直接 `start_new_chat`，从不读锚点 | **已修**（P2：`_session_plan` + `_remember_anchor`，`session_lost` 时不发） |
| R2-4 | 驱动收敛第一步不该重写底层 | **成立** | `drivers/__init__.py` 只有协议；尚无适配器 | 留下轮（先做 `legacy_adapter`） |
| R2-5 | S2 回声过滤会误删合规引文 | **成立** | 原按「重合 ≥8 字」一刀切，会把逐字引用的规格原句删掉 | **已修**（P3：含「」/§/`文件:行号` 的块豁免） |
| R2-6 | 推进器缺前置核实与三振停机 | **成立** | `sisyphus.py` 原来只看 plan 与硬停词，没有核实闸门 | **已修**（P4：`verification_ok` 闸门 + `MAX_ATTEMPTS=3`） |
| 附 | 采集端尾部混入历史提示词 | **成立** | `read_answer_text` 已加 `_drop_prompt_echo`（Round 1 S2） | 已修，本轮再加引文豁免 |
| 附 | 孤儿 ui.lock | **部分成立** | `clear_stale()` 已按 pid 清墓碑（Round 0 修复第 3 项），但只在 `cmd_review/summarize` 入口调用 | 留下轮（挂到 sisyphus 入口） |
| 附 | 剪贴板残留 | **部分成立** | harvest 有 `clipboard_seq` 校验；paste 路径无写后哈希 | 留下轮 |

## 备注

本轮会审只有 2 家真读（deepseek 4/5、gemini 5/6），未达 §7.3 的 3 家。
按 Herhons 10:42「不要停、一路跑」继续，偏离记在 `decisions.md`。
