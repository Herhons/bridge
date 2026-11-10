# 任务卡 03：修 workbuddy_use.py --list（第 2 项收尾）

背景：CARD-02 已 done（测试 78 OK，codex_choose --list 真机已正常，visible_editor 已能在多输入框里选最大的）。剩一个问题：`workbuddy_use.py --list` 在 argparse 阶段就退 2（`the following arguments are required: prompt`），因为 `src\qi\workbuddy.py` 的入口在 sending=True 时把 prompt 设成必填。这属于计划第 2 项（WorkBuddy 端收发）。Codex 额度还没恢复，CARD-01/02 的 Codex 验收先挂起，等额度回来一起补。

## 做什么
1. `src\qi\workbuddy.py` 的入口：带 `--list` 时 prompt 可以不给，只读出当前模型和输入框状态后退出 0；不带 `--list` 时没给 prompt 仍要报错（行为和现在一样）。不要改发送、等待回复的逻辑。
2. 新增 `tests\test_card03.py`：离线覆盖 4 种情况：`--list` 不带 prompt 能过参数解析、`--list` 带 prompt 也能过、不带 `--list` 也不带 prompt 报错、正常带 prompt 能解析。用打桩，不碰真窗口。

## 怎样算完成
- 全量测试通过：`<user>\AppData\Local\Programs\Python\Python310\python.exe -X utf8 -B -m unittest discover -s tests`（原 78 个 + 新增）。
- 真机：`workbuddy_use.py --list` 退出码 0，输出模型（应为 DeepSeek-V4.1-Flash）。贴输出。
- 不要用 workbuddy_use 给自己当前这个任务发消息，也不要切 WorkBuddy 的模型。

## 允许改
`src\qi\workbuddy.py`；新增 `tests\test_card03.py`；在 CHANGES-20261030.md 末尾追加说明（改了什么、怎么回滚）。改之前先把 `src\qi\workbuddy.py` 拷到 `_backup_20261031\src\qi\workbuddy.py`。

## 禁止
不改其他 .py；不碰 F:\Bridge\v0.0.8；不调用 opencode/codex 等 CLI；不改注册表/系统设置/其他扩展；不合并；不在代码里写死地址或密钥；不给 Codex 发消息。

## 最后一步（必做）
运行 `python src\scripts\notify_done.py --card 03 --status <done/stuck/failed> --note <一句原因>`。不管成没成都要报。