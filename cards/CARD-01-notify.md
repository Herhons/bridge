# 任务卡 01：进度文件 + 完工通知脚本

## 做什么
在 F:\Bridge\v0.0.9 新增：
1. `src\qi\progress.py`：读写 `progress.json`（放在 v0.0.9 根目录）。字段：card_id, status（done/stuck/failed/running）, note（一句话，不含提示词和回复全文）, updated_at（北京时间 ISO）。
2. `src\scripts\notify_done.py`：参数 `--card <id> --status <状态> --note <一句话>`。先写 progress.json，再向环境变量 `BRIDGE_NOTIFY_URL` 发一个 HTTP POST（JSON 同上，带 `BRIDGE_NOTIFY_KEY` 作为请求头）。没设这个环境变量就只写文件、不报错。通知失败要重试 2 次，仍失败就在 progress.json 里记 notify_failed=true，不影响退出码。
3. 测试 `tests\test_progress.py`：覆盖写入、覆盖更新、无环境变量、通知失败四种情况，不真的联网（用本地假服务器或打桩）。

## 怎样算完成
- `python -X utf8 -B -m unittest discover -s tests` 全部通过（原有 58 个 + 新增）
- 手动跑一次 `python src\scripts\notify_done.py --card 01 --status done --note test`，progress.json 正确生成

## 允许改
只新增上面 3 个文件；可在 CHANGES-20261030.md 末尾追加说明。

## 禁止
不改任何已有 .py；不碰 F:\Bridge\v0.0.8；不调用 opencode/codex 等 CLI；不改注册表/系统设置；不在代码里写死任何地址或密钥。

## 最后一步（必做）
运行 notify_done.py，把本卡结果（done/stuck/failed + 一句原因）报上来。不管成没成都要报。
