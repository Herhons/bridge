# CARD-08 编码修复 + 第 7b 项自动推 + 第 3 项实机收尾（简单卡，不会审）

来源：CARD-07 验收时 workbuddy_use 打印含 \u200b 的回复，GBK 控制台 UnicodeEncodeError 崩溃；PLAN 第 3、7b 项。

1. 所有 src\scripts\*_use.py（及 *_choose.py、workflow_use.py）输出编码：入口处统一调用一个公共函数（放 src\qi\ 下，如 qi\console.py），对 sys.stdout/sys.stderr 执行 reconfigure(encoding="utf-8", errors="replace")；reconfigure 不可用时退回包装器；写文件一律 utf-8。打印前去掉零宽字符（\u200b \u200c \u200d \ufeff）不改变返回给调用方/写证据的原文。不许叠包装脚本。
2. 第 7b 项：WorkBuddy 停了自动推，按 PLAN.md「第 7 项子卡 CARD-07b」大纲实现：progress.json running 且 runlog 超过 N 分钟（默认 20，可配）无新行且无 stage=generating → workbuddy_use --continue 发「继续按卡施工」；同卡 30 分钟内最多推 1 次；连推 3 次无进展 → 停推并调用通知脚本叫红莉栖；推送记录写 runlog。提供 --dry-run，只判定不发送。
3. 第 3 项实机收尾：chatgpt_choose --list 真机读出当前模式与档位（聊天模式），证据写 evidence\card08_chatgpt_list.txt；若读不出，记录现象与截图路径，不要反复乱点。
4. 离线单测 tests\test_card08.py：含 \u200b 的回复在 cp936 流下不崩；7b 判定/限频/3 次停推/dry-run 各分支。

允许改：src\qi\console.py（新）、src\qi\autopush*（新）、src\scripts\*_use.py、*_choose.py、workflow_use.py 的入口编码几行、src\scripts\workbuddy_autopush.py（新）、tests\test_card08.py、CHANGES 追加、progress.json 本卡状态。
禁止：动 F:\Bridge\v0.0.8、opencode.json、注册表/策略/扩展/登录、其他卡已有测试的预期。
完成标准：py -3.10 -m unittest 全量通过（≥487 + 新测）；真机证据 evidence\card08_live.txt：workbuddy_use 回复含 \u200b 时 EXIT=0 正常打印（可让模型回一句含零宽字符的话，或离线重放）；workbuddy_autopush --dry-run 对当前 progress.json 的判定输出；第 3 项证据见上。完成后 progress.json 置 08 done，并调用通知脚本。
