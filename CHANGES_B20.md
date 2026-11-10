# CHANGES_B20 — 仓库整理、合并进 v0.0.9、四端会审

状态：整理与合并完成，四端会审与 Gemini 整合已完成。全量离线用例 1480 通过。

## 一、仓库整理（只动仓库卫生，不动业务代码）

- 目标仓 `v0.0.9` 建分支 `merge/wip-20261110`，打 `pre-merge-20261110` / `post-merge-20261110` 两个 tag。
- 4 个 `_backup_2026103x` 移出仓库到 `F:\Bridge\_archive\v009-prebranch`（未删）。
- 删掉散落物：`x.png`、`_shot_codex.png`、`@AutomationLog.txt`、4 个 `progress.json.bak-*`、`PLAN.md.bak-*`。
- `.gitignore` 补：`_selftest/`、`_tmp/`、`.chrome-window.json`、`progress.json.lock`、`runs/`。
- WIP 侧：22 个 `CHANGES_*.md` 归档到 `docs/history/changes/`（4 对完全相同的短名副本删除，`REWORK_STATUS.json` 里 17 条路径同步更新）；`patches/`、`COPY_BASELINE.json`、`ORIGINAL_INTEGRITY.json` 移到 `docs/history/audit/`。
- 新增 `README.md`（8 节：定位、快速开始、8 端与档位、目录职责、四个机制、测试、文档索引、已知限制）与 `CHANGES.md`（19 行索引，B17/B19 标当前）。

## 二、合并

`robocopy /E` 覆盖，排除 `.git`、`__pycache__`、`_selftest`、`_tmp`、`evidence`、`patches`、`checkpoints`、全部 `_backup_batch*`。保留目标侧 `requirements.txt` 与 `.git`。

带过去的状态：`docs/tiers.json`（317 行 / 8 端）、`docs/bridge_server.json`（补上 `max_endpoints_per_job=4` 与 `endpoint_concurrency=1`，`use_timeout` 180→120）、`progress.json`、`runs/submissions/`、`runs/tier/`、`REWORK_STATUS.json`。

补的测试夹具（`git add -f`，因为 `evidence/` 被 ignore）：`recheck_sources/` 17 份、`batch17/dsh_menu_open_S1.png`、`dsh_menu_not_open_model.png`、`r10.../LONG_EXPECTED.txt`、两份 evidence bundle 及其引用的 8 个 txt。

验证：目标居全量 **1480 通过**；`git clone` 到新目录再跑一遍，同样通过（证明入库内容自足）。

## 三、会审

材料：多行版 2758 字给 chatgpt / deepseek；单行版 1557 字给 grok；gemini 需要第三版（去掉行首编号与列表标记，2607 字）。四份原文在 `evidence/review_20261110/replies/`。

结果：chatgpt 393s / 3169 字、grok 53s / 1552 字、gemini 76s / 3792 字、deepseek 1505s（判超时，正文从页面取回）3097 字。

格式敏感度（实测）：gemini 会把 `1.` 自动编号、把 `- ` 变成 `•`；grok 会把换行折叠。所以一份材料喂四个端不可行，必须按端分词。

## 四、Gemini 整合

新开对话（`--new`），五轮：代码清单 → chatgpt → gemini → deepseek → grok+整合要求。每批 ≤3824 字符。整合结果 3456 字，存 `evidence/review_20261110/gemini_summary.md`；我的判断与后续计划在 `docs/REVIEW-20261110.md`。

## 五、会审期间修的自身缺陷

`gemini._clear_draft` 把占位文字 `问问 Gemini` 当成真草稿，导致"消息已发出、回答已回来"却被判 `draft_clear_failed`，后续轮次还撞 `conversation_busy`。已修（占位符视为空），附 `tests/test_b20_gem_clear.py` 4 条用例。

## 六、回滚

- 代码：`git reset --hard pre-merge-20261110`。
- 备份目录：`F:\Bridge\_archive\v009-prebranch\`。
- WIP 目录 `v0.0.9_codex_wip` 全程只增不删，原样保留。
