# CARD-06a 档位回执强制 + UIA 文档丢失唤醒 + 最小化窗口（r2，三家会审合并）
会审（均先 choose 最高档并读回）：Gemini(argon) REVISE evidence\review_card06_gemini_0345.txt；Grok(expert) REVISE evidence\review_card06_grok_0510.txt；ChatGPT(聊天 GPT-6 + 思考强度「高」) REVISE evidence\review_card06_chatgpt_0512.txt。拆卡：本卡 06a 先做，06b（网页端选档/模板/输入问题）另卡排队。
## 做什么
(a) 档位回执 src\qi\tier_guard.py
1. *_choose 读回确认后原子写入（临时文件+os.replace）runs\tier\<端>.json：schema_version、端、目标档、读回档、hwnd、pid、进程启动时间、tab 标识、UTC 时间戳(单调时钟起点另记)、读回原文。
2. *_use 新增必填 --purpose test|review|accept（无默认）；test=最低档，review/accept=最高档，档位表放 docs\tiers.json，代码里不写死。
3. 校验：缺失/schema 不符/读回档不符/超 120 秒/时间戳在未来/hwnd+pid+启动时间不一致 → 退出码 5，stage=tier_unconfirmed，提示先跑对应 *_choose。
4. 消费：发送动作之前用文件锁原子标记 consumed（失败、部分失败也作废，必须重新 choose），防并发重放。
5. 防漂移：use 发送前只读复核当前 UI 档位（能读的端），与回执不符按 5 拒发；use 绝不切档。
6. 安全边界写进 docstring：回执只防误用不防恶意伪造（本机单用户），不引入密钥。
(b) UIA 文档丢失 src\qi\ui.py（opencode_use、workbuddy_use 共用）
每轮重新取 Root、不复用旧元素；SetForegroundWindow 拿真焦点 → NotifyWinEvent / 根节点 FindAll → 必要时鼠标在窗口内小幅移动；最多 3 轮、间隔 2 秒、总限时；每轮日志写入 runlog。仍为 0 → 退出码 6 stage=uia_docs_lost，不杀进程、不自动重启。
(c) 最小化 src\qi\choose.py 等取窗口处
查 IsIconic → ShowWindow(SW_RESTORE) → 轮询直到 IsIconic=False 且矩形非 -32000、宽高大于阈值（不以 ShowWindow 返回值为准）；失败退出码 7 stage=window_minimized，不抛异常。修 grok_choose --list 在最小化时 move_to(-31970,-31880) 抛 RuntimeError。
(d) 退出码：先审计全部端现有退出码及调用方，在公共模块统一常量与 stage 映射，确认 5/6/7 与 argparse 的 2 等不冲突，登记到 docs。
## 完成标准
全量 unittest 全过（现 215）+ 新增 tests\test_card06a.py，覆盖：回执各分支、篡改、过期/未来时间、并发消费、发送失败作废、UI 档位被手动改、唤醒重试计数与超时、窗口恢复失败、退出码表。真机：choose 后 use --purpose test 成功一次、无回执拒发一次；Chrome 最小化时 grok_choose --list 不崩；workbuddy_use --list 能读出文档。输出存 evidence\card06a_live.txt。
## 禁止
F:\Bridge\v0.0.8、注册表/策略、登录、audit\、opencode 配置文件；不合并。
