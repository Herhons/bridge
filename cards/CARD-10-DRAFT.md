# CARD-10 开发方法：规则化防回归（借鉴 Lauren/poteto tibo.dog）— DRAFT r1，待三家会审

<host> 11-02 16:4x 批准。目标：同类错误不靠验收发现，而由规则/测试/构建自动拦截。不改业务行为。

1. 每修一个 bug 必带一条 lint 规则或回归测试
   - 引入 ruff（pyproject.toml 配置，固定版本写入 requirements-dev.txt），基础规则集 E,F,W,B,I。
   - 自定义检查放 tools\lint\bridge_checks.py（AST，离线），由 tools\lint\run_lint.py 统一调用，退出码 0/1。
   - 卡模板新增必填段「本卡新增的防回归规则/测试」；CHANGES 每条 bug 修复须引用规则 ID（BR0xx）或测试名。
   - 首批回填今日已知 bug：Markdown 字符/URL 归一（input_mismatch）、复制按钮点错（取问题气泡）、GBK 打印崩溃（\u200b）、notify_done 覆盖 verdicts/queue、--new 重置模型（tier_drift）。
2. 动屏幕的脚本命名 *_ui.py，规则强制三件事
   - 改名采用「新增 *_ui.py 为真实实现 + 旧名保留薄转发」，不破坏现有调用/文档；清单写入 docs\ui_scripts.md。
   - BR001：*_ui.py 入口必须调用用户活动检测（空闲判断/ui.lock）后才能合成输入。
   - BR002：*_ui.py 发送路径必须先持有有效档位回执（tier receipt）。
   - BR003：全仓禁止 --force（argparse 定义与调用均禁止）；确需绕过用显式 --i-know 且写 runlog（会审决定是否允许）。
   - BR004：非 *_ui.py 文件禁止 import pyautogui / SendInput / 鼠标键盘合成 API。
3. choose/use 可回放日志
   - 每次 choose/use 追加一行 JSONL 到 runs\replay\YYYYMMDD.jsonl：时间、脚本、argv、档位回执摘要、输入文本 SHA256+原文（可配置脱敏）、UIA 关键读数、结果码、回复文本 SHA256 与路径、耗时。
   - tools\replay.py：离线把记录喂给解析/判定层（mock UIA），断言结果码与当次一致；验收可引用 replay 通过代替真机复测（仅限未改 UI 驱动层的卡，符合「测过的不再测」规则）。
   - 不记录 token、opencode.json key、cookie；规则 BR005 扫描日志写入点禁止这些字段。
4. 时间与体积红线（失败即构建失败）
   - 配置 docs\redlines.json：单次发送耗时上限（按端，默认 600s）、单个 .py 文件行数上限（默认 800，现超限文件列白名单并标注「待拆」，只许减不许增）、单函数行数上限（默认 120，同样白名单）。
   - tools\check_redlines.py 读 runlog/replay 与源码，超限退出 1；tools\test_all.py 末尾串上 lint + redlines。

允许改：pyproject.toml、requirements-dev.txt、tools\lint\*、tools\replay.py、tools\check_redlines.py、tools\test_all.py、新增 *_ui.py 与旧名转发、src 中写 replay 日志的点、tests 新测试、docs\ui_scripts.md、docs\redlines.json、cards\TEMPLATE.md、CHANGES、progress.json 本卡字段。
禁止：v0.0.8 任何文件；opencode.json 及其 key；Codex；改变现有退出码语义；真机发送。
完成标准：py -3.10 全量单测通过且数量不少于 09e 后基线；run_lint 与 check_redlines 在当前仓库退出 0（白名单内）；每条 BR 规则各有一个「违规样例必报错」的测试；replay 至少覆盖 workbuddy/gemini/grok/chatgpt 各一条样例记录。真机只冒烟：一个 *_ui.py 转发后的 --list 正常。

## 会审问题（发给 Gemini argon / Grok expert / ChatGPT GPT-6 高）
1. *_ui.py 改名用「新文件+旧名转发」是否合适，还是一次性改名？
2. BR001–BR005 用 AST 自定义检查是否足够，有无更轻办法？
3. replay 日志记录原文是否有隐私/体积问题，保留期多久？
4. 红线默认值（600s / 800 行 / 120 行）是否合理，白名单策略是否会被滥用？
5. 本卡是否应拆成 10a（lint+红线）/10b（_ui 改名+规则）/10c（replay）？