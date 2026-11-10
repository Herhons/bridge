# CARD-10c 可回放日志 + 运行时 watchdog — DRAFT（10b 后施工；方法有变，施工前单独会审）

来源同 CARD-10a。三家一致：原文默认不存，只存哈希与脱敏摘要。

1. 三级记录（ChatGPT）
   - L1 运行记录（默认开）：时间、脚本名、档位、run_id/request_id/correlation_id、schema_version、结果码、各阶段耗时。
   - L2 诊断（默认开，必须脱敏）：UIA 关键读数＝目标区域控件子树的序列化（ControlType/Name/AutomationId/BoundingRectangle，Gemini），文本长度，输入/回复 SHA256，脱敏摘要（前 32 + 后 8 字符或正则替换，Grok）。
   - L3 原文（默认关）：必须显式开启，单独存放 runs\replay\blobs\<sha256>.txt（去重，Gemini）。
   - argv 不整体写入，只写白名单字段；路径用相对路径或 artifact id（ChatGPT）。
2. 保留期（冲突：Gemini 7 天/200MB；Grok 14–30 天；ChatGPT L1 7 天、L2 30 天、L3 24 小时最长 7 天）。取最严：L1 7 天；L2 7 天；L3 24 小时；runs\replay 总量上限 200MB，写入时顺带清理。tests\fixtures\replay\ 下的脱敏/合成样例长期保留。runs\replay\ 写进 .gitignore。
3. 统一出口（三家）：只能经 write_replay_log() 写入，内置字段白名单 + sanitize（递归清洗键名匹配 /token|key|cookie|auth|secret/i 的字段以及长 Base64/Hex 串）。BR005 = AST 检查模块外没有直接打开 runs\replay 的地方 + 用虚拟敏感串做泄漏测试，覆盖公共序列化路径（Grok）。
4. 职责边界（ChatGPT）：JSONL 只是附属日志；progress.json 仍是唯一状态源（09 会审决定，不引入 SQLite）。
5. replay 的边界（ChatGPT，最严）：tools\replay.py 完全离线，用 mock UIA，禁止合成输入和网络请求，不改状态；golden fixture 的预期值手工固定，不能拿当前输出当预期。replay 不能替代 UI 驱动层真机冒烟；只有代码/配置/依赖哈希都没变时，才可以复用旧的验收证据。
6. watchdog（Gemini、ChatGPT；Grok 主张按端收紧）：按端、按阶段分别计时（拿锁、选档、输入、等回复、读回复），在运行时由进程自己判超时并返回独立超时码，带上阶段名；不放进离线 test_all。默认值：普通档等回复 180s；高档（review/accept）等回复 ≥900s（09f，<host> 认可，比 Gemini/Grok 的 600s 宽：因为实测 4 分 10 秒的思考已经超过 300s，宁可多等也不重发）；其余阶段从 runlog 的 P95 算出后写进 redlines.json。AI 服务慢不算代码回归。
完成标准（草）：四个端点（workbuddy/gemini/grok/chatgpt）各一条手工固定的 golden fixture；泄漏测试通过；全量单测通过。