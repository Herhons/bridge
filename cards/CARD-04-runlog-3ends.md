# 任务卡 04：codex / dsh / opencode 接上运行日志和超时分型（计划第 4 项）

背景：CHANGES-20261030 第 4、6 项已给 flow.py / desk_flow.py / workbuddy.py 接了 `src\qi\runlog.py`（每次 *_use 结束往 `runs\YYYYMMDD.jsonl` 追加一行）和 `timeout_kind`（still_generating / no_reply）。但 `codex_use.py`、`dsh_use.py`、`opencode_use.py` 是单文件脚本，没走 flow/desk_flow，所以这三端现在不写运行日志，超时也不分型。第 1、2 项的 Codex 验收在等额度恢复；第 3 项（ChatGPT 切档）要在真网页上反复点击，留到白天有人、Codex 能验收时再做。本卡只做离线可测的第 4 项。

## 做什么
1. `src\scripts\codex_use.py`、`src\scripts\dsh_use.py`、`src\scripts\opencode_use.py`：每次运行结束（成功、超时、报错、参数错误后的正常退出都算；`--help` 不算）调用一次 `qi.runlog.record(<端名>, result)`，端名分别是 `codex`、`dsh`、`opencode`。result 里至少带 ok、stage、elapsed；有 hint/error 就带上。只记这些，不写提问和回复正文。写日志失败不能影响原来的输出和退出码。
2. 这三个脚本超时时，result 仍是 `stage=timeout`（兼容旧调用），并加上 `timeout_kind`：已发出且最后还在生成/有未稳定的回复 → `still_generating`；否则 → `no_reply`。判断方式尽量复用 conversation.py 里现成的逻辑，不要另写一套。
3. `--list` / `--probe` 这类只读模式：可以不记日志，或记一行 stage=list，二选一，在 CHANGES 里写清楚选了哪个。
4. 新增 `tests\test_card04.py`：离线打桩覆盖每个脚本至少 3 种情况：成功写一行日志、超时写一行且 timeout_kind 正确、runlog 写失败时脚本输出和退出码不变。用临时目录，不碰真窗口、不往真 `runs\` 写。

## 怎样算完成
- 全量测试通过：`<user>\AppData\Local\Programs\Python\Python310\python.exe -X utf8 -B -m unittest discover -s tests`（原 91 个 + 新增）。贴最后几行。
- 三个脚本的 `--help` 和 `--list` 退出码 0，输出和改之前一样（贴输出）。
- 不真的给 Codex、DeepSeek 网页、opencode 发提问（Codex 额度用完了，也省额度）。

## 允许改
`src\scripts\codex_use.py`、`src\scripts\dsh_use.py`、`src\scripts\opencode_use.py`；如确需小改 `src\qi\runlog.py`（只加不删、不改已有字段）；新增 `tests\test_card04.py`；在 CHANGES-20261030.md 末尾追加本卡说明（根因、改了什么、验证、没做到的、怎么回滚）。改之前先把要改的每个文件按原路径拷到 `_backup_20261031\` 下（已有同名备份就加后缀 `.bak-card04`，别覆盖旧备份）。

## 禁止
不改其他 .py；不碰 F:\Bridge\v0.0.8；不调用 opencode/codex 等 CLI；不改注册表/系统设置/其他扩展；不合并；不在代码里写死地址或密钥；不给 Codex 发消息；不切 WorkBuddy 模型；不用 workbuddy_use 给自己当前这个任务发消息。

## 最后一步（必做）
运行 `python src\scripts\notify_done.py --card 04 --status <done/stuck/failed> --note <一句原因>`。不管成没成都要报。