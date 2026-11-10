# CARD-09e 后端/UIA 稳定性修复（09b 会审三家一致要求拆出，简单卡不会审）

1. notify_done.py：写 progress.json 只更新本卡相关字段（读-改-写合并 + 同目录临时文件 os.replace + 文件锁），绝不覆盖 verdicts、queue 等其他字段。单测：预置 verdicts/queue → 调用后原样保留。
2. WorkBuddy 闲置后 UIA 文档丢失（choose/use 报 uia_docs_lost，docs=0）：workbuddy_choose/use 检测到后自动重启 WorkBuddy 一次（正常关闭→等待→启动→等 UIA 文档就绪），仍失败才退出 6；重启写 runlog。单测 mock。
3. ChatGPT 回复检测超时（页面已答完但 300s no_reply / 被光标判断打断 interrupted）：增加 UIA 兜底——按"最新一条回复/回答已完成"标记取最后一条助手回复文本；chatgpt_use 增加 --read 只读最后回复（现在 --read 不可用，退出 2）。
4. ChatGPT --new 不清残留草稿（choose --new 后 use 仍 draft_present）：新对话后检测输入框（UIA 名"询问 ChatGPT"的 Edit，注意不要误选地址栏）有草稿则 Ctrl+A/Delete 清空并读回为空。
5. ChatGPT/Grok 输入框把 Markdown 字符（* # 行首编号、URL 自动转链接）转成格式导致 input_mismatch：比对前对两侧做同样归一（去 Markdown 标记、链接取纯文本、合并空白）；必要时用粘贴纯文本方式写入。
6. Gemini：choose --new --writer dialog 报 unreadable（"两种模式都差得远"，gem_writer_tabs_dialog 0.987 已命中），use --new 因此 prepare_failed；--continue 正常。修模式判断或在写入器不可读但模板命中时按已命中处理并读回。
7. 空闲检测误报：各 *_use 启动时总报"你正在用电脑（空闲 0.7–1.9s）"，实为刚跑完的 *_choose 自己的输入；用脚本自身最后一次合成输入的时间戳排除。

允许改：上述脚本及 src\qi 相关模块、对应新测试、CHANGES、progress.json 本卡字段。禁止同 09b。完成标准：全量单测通过；真机只冒烟本卡改动的端（ChatGPT 新对话清草稿+发送+读回、WorkBuddy 重启路径、Gemini --new），已通过端不复测。
