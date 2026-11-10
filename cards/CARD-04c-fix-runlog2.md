# CARD-04c 修 CARD-04/04b 复验被打回的两条（Codex 11:52）
1. 三端的空闲等待（idle_seconds）放在了异常保护区外面。向它注入异常后，runlog.record 一次都没记。要把它挪进保护区，保证恰好记 1 次，然后原样抛出。
2. Codex --list 打不开卡片时，输出里有 hint="打不开卡片"、退出码是 1，但写进日志时 error=null，hint 丢了。要把结果里的 hint/note/error 一起传给 runlog。
## 怎样算完成
unittest 全过，且测试数 ≥108。新增两类测试：三端 idle 异常各记 1 次；Codex list 失败时日志里带 hint。--help 输出逐字不变。三端真机 --list 都退出 0，原始输出存到 evidence\card04c_list.txt。
## 允许改
src\scripts\codex_use.py、dsh_use.py、opencode_use.py、src\qi\runlog.py、tests\test_card04*.py（可新建 test_card04c.py）、CHANGES-20261030.md、progress.json；改之前先备份到 _backup_20261031。
## 禁止碰
F:\Bridge\v0.0.8、其他 .py、注册表/策略、登录、audit\
## 最后一步
写 progress.json，并运行 notify_done.py --card 04c --status <done|stuck|failed> --note "<一句原因>"。