# CARD-10a 规则化防回归·基建（ruff 冻结基线 + 回归测试 + 行数棘轮 + 测试名基线）

来源：CARD-10-DRAFT r1 三家会审（Gemini argon / Grok expert / ChatGPT GPT-6 高，均 REVISE，原文 evidence\card10_review\*_reply_1751.txt）。红莉栖合并；<host> 11-02 17:5x 批准直接施工（t1223u）。三家一致：拆成 10a/10b/10c，先做 10a。
前置：09f 已完成。本卡不改任何 UI 驱动行为、不改退出码语义、不跑真机发送。

1. Ruff（Gemini、Grok；ChatGPT 补固定版本）
   - pyproject.toml 只开 F、E9、I（Gemini，最严收口；W/B 不开）。ruff 精确版本写入 requirements-dev.txt，target-version=py310。
   - 存量告警一次性冻结：用 per-file-ignores 或 --add-noqa 生成基线，基线文件 docs\lint_baseline.md 列出数量；之后只许减。禁止全局 ignore；新增 ignore 必须写原因与范围（ChatGPT）。
   - 不做大规模自动格式化，不顺手改业务代码（Gemini、ChatGPT）。
2. 每修一个 bug 必带回归测试；lint 规则按需（ChatGPT，三家一致）
   - 测试放 tests\regression\，每条测试的 docstring 写 bug 编号/卡号。首批回填 5 条：
     a) Markdown 字符/URL 归一（input_mismatch）：特殊字符、转义、URL 参数、非 ASCII。
     b) WorkBuddy 复制按钮点错（取了问题气泡）：定位唯一性 + 文本校验（mock UIA）。
     c) GBK 打印零宽字符崩溃：子进程在 PYTHONIOENCODING=gbk 下输出 \u200b 不崩。这一条同时做成静态规则 BR006（Gemini）。
     d) notify_done 覆盖 verdicts/queue：字段隔离、并发更新、原有数据保留。
     e) --new 重置模型（tier_drift）：新建对话后档位保持/自动重选。
   - 卡模板 cards\TEMPLATE.md 新增必填段「本卡新增的回归测试/规则」，格式机器可解析（Grok）。CHANGES 每条修复引用测试名或 BR 编号。
   - docs\rules.md 一页列规则 ID 与说明；每条规则配「违规样例必报错 + 合法样例不报」成对测试（Grok）。
3. 行数棘轮（三家；取最严合集）
   - docs\redlines.json：单 .py 文件上限 800 行、单函数 120 行；500/80 只打警告不阻断（Gemini）。
   - 超限项白名单，每项必须有：精确文件路径（禁止目录/通配符，ChatGPT）、当前精确行数、owner、目标行数、拆分卡号、创建日期、超限原因（Grok+ChatGPT）。
   - tools\check_redlines.py：实际 > 登记值即失败；实际比登记值少 ≥20 行时提示收紧；登记值只能下降——用测试比对 git/备份中的上一版 redlines.json，上调或新增条目没有卡号即失败（ChatGPT：白名单本身也要有测试保护）。
   - 修 bug 需要在白名单文件里加行：本卡内等量精简，不许上调（Gemini，最严）。
   - 本卡只做静态源码检查，不读 runlog 计时（耗时红线移到 10c 运行时 watchdog，Gemini/ChatGPT 一致）。
4. 测试名基线（ChatGPT）
   - 生成 docs\test_baseline_09e.txt（09e 后全量测试名清单 + 通过数 709）。tools\check_test_baseline.py：基线内的测试名不得消失（改名/删除须在 docs 写理由），skip/xfail 数不得增加，通过数 ≥ 基线。
5. 统一入口 tools\test_all.py（ChatGPT）：全量单测 → ruff → bridge_checks（AST 框架，本卡只放 BR003 禁 --force 的扫描骨架 + BR006）→ redlines → 测试名基线。某项失败不阻止其余项收集，最后汇总并以非 0 退出。另提供单独的 tools\gate.py 本地门禁命令（Grok）。

允许改：pyproject.toml、requirements-dev.txt、tools\lint\*、tools\check_redlines.py、tools\check_test_baseline.py、tools\test_all.py、tools\gate.py、tests\regression\*、tests 其它新测试、docs\redlines.json、docs\rules.md、docs\lint_baseline.md、docs\test_baseline_09e.txt、cards\TEMPLATE.md、CHANGES、progress.json 本卡字段。安装 ruff 到 py -3.10 用户环境可以。
禁止：v0.0.8 任何文件；opencode.json 及其 key；Codex；改变退出码语义；改 src 业务逻辑；真机发送；--force；通用 --i-know。若跑真机探针必须先拿 runs\ui.lock（09f 规则）。
完成标准：py -3.10 -m unittest 全量通过且测试名覆盖 09e 基线、数量 ≥709；tools\test_all.py 在当前仓库退出 0；每条规则有成对测试；5 条回归测试各至少 1 个；redlines 白名单无通配符且每项字段齐全。真机：无（本卡不动 UI）。
产出：CHANGES 记录、evidence\card10a_unittest.txt、evidence\card10a_test_all.txt；progress.json 本卡 done。