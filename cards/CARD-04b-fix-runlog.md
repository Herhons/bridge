# CARD-04b 修 CARD-04 被打回的两条（属第 4 项）

## 打回原因（Codex 09:21 验收）
1. “三端 --list 均退出 0”没达到：运行日志第 17 行记录了 Codex list 失败，tests 第 226 行反而把 Codex 返回 1 写成了预期。
2. “报错也记录一次”没覆盖完整入口：三端的初始化在异常保护区外面，离线注入初始化异常时，三端 runlog.record 的调用次数都是 0。

## 做什么
- codex_use.py / dsh_use.py / opencode_use.py：把初始化（打开窗口、建会话）放进异常保护区，任何异常都要 runlog.record 一次（带 timeout_kind 或 error 类型），然后照原样退出。
- Codex --list：用 CARD-02 修好的弹层识别（codex_choose 那一套）让 codex_use --list 能在真机退出 0；如果 codex_use 有自己的卡片打开逻辑，就改成复用 codex_choose 的。
- tests：删掉“Codex 返回 1 算预期”的断言；新增三端“初始化抛异常 → record 恰好 1 次”的测试。

## 怎样算完成
- python -m unittest 全过，测试数不能少于 106
- 真机 codex_use --list、dsh_use --list、opencode_use --list 都退出 0，原始输出贴进 evidence\card04b_list.txt
- --help 输出和改之前逐字一致

## 允许改的文件
src\scripts\codex_use.py、dsh_use.py、opencode_use.py、src\qi\runlog.py、tests\test_card04.py、新建 tests\test_card04b.py、CHANGES-20261030.md（追加）

## 禁止碰
F:\Bridge\v0.0.8、其他 .py、注册表/策略、登录、audit\。改之前先把要改的文件备份到 _backup_20261031。

## 最后一步（固定）
把结果写进 progress.json，并调用 python src\scripts\notify_done.py --card 04b --status <done|stuck|failed> --note "<一句原因>"。不管成没成都要通知。