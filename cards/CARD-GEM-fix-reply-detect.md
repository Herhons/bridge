# CARD-GEM-fix gemini_use 检测不到已经完成的回复（属第 2 项收发）
## 现象（2026-10-31 11:4x）
gemini_use --timeout 240 报 timeout/no_reply，submission=unknown。但 Gemini 页面上早就有完整回复了，末尾带 VERDICT: REVISE，下面也有“答得好/答得不好/重做/复制”的动作条。
## 已有证据
evidence\gemini_bug_1215.png（屏幕截图）、gemini_bug_1215_uia.txt（控件树文本）、gemini_bug_1215_script_log.txt（脚本输出）。
脚本日志里有：找到 gem_writer_tabs_dialog 1.000，没找到 gem_send_on（最高 0.652，阈值 0.95）。页面上的模式按钮现在显示“当前模式为 Argon”，输入框占位文字是“问问 Gemini”。
## 初步判断（要施工方核实）
发送键模板和现在的页面不匹配了（模式改名 Argon 或者发送键样式变了）。结果是“发送成功”和“生成结束”这两个判据都失效了，submission 判不出来，回复也一直等不到。控件树里有“答得好/复制”按钮，而且回复文本在“Gemini 说”节点下面，这些可以拿来做完成判据。
## 做什么
1. 完成判据改成以控件树为主：出现“答得好”或“复制”按钮，同时“Gemini 说”节点下的文本稳定 3 秒，就算完成。发送键模板只作为兜底。
2. submission 的判断：控件树里出现了这次发出的提问文本，就算 accepted。
3. 加 --read：只读当前 Gemini 标签页最后一条回复，不发送。
4. 补离线测试：用 gemini_bug_1215_uia.txt 这份真实控件树做夹具，能读出 VERDICT 行。
## 怎样算完成
unittest 全过；真机发一条最低档 token 消息，能正确拿到回复；--read 能读出这次 11:4x 的会审回复。原始输出存到 evidence\cardgem_live.txt。
## 允许改
src\scripts\gemini_use.py、src\qi 下 gemini 相关的模块、tests\test_cardgem.py（新建）、模板目录里 gemini 的模板（只能新增，不能删）、CHANGES-20261030.md、progress.json；改之前先备份。
## 禁止碰
F:\Bridge\v0.0.8、其他端的 .py、注册表/策略、登录、audit\
## 最后一步
写 progress.json，并运行 notify_done.py --card GEM --status <done|stuck|failed> --note "<一句原因>"。