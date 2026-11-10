# 任务卡 02：修两个收发脚本 bug（Codex 选档弹层识别 + WorkBuddy 多输入框）

背景：CARD-01 已 done，但交 Codex 验收时卡住。原因一是 Codex 额度暂时用完（窗口显示“你已达到使用上限”），二是 `codex_choose.py --list` 一直报 `打不开卡片`：其实点芯片后弹层已经弹出（截图可见上方“极高”、中间“GPT-6 Luna >”、下方档位滑条），是脚本没认出弹层。另外 `workbuddy_use.py` 发不出去，因为 WorkBuddy 页面有 2 个可见 EditControl，其中一个是约 30x20 的空框，`visible_editor` 里 `ui.unique` 直接报错。这两个都属于计划第 2 项（Codex 端、WorkBuddy 端收发），先修它们才能恢复验收。

## 做什么
1. `src\qi\conversation.py` 的 `visible_editor`：多个可见输入框时，先去掉宽或高小于 40 像素的，再取面积最大的那个；只剩 0 个才报错。其他站点行为不变。
2. `src\scripts\codex_choose.py`：修好弹层识别，让 `--list` 在弹层已弹出时能正确读出当前模型和档位并关掉弹层（按 Esc 或点空白）。先用 `--probe` 和截图（存 evidence\，用 tools\look.py --title ChatGPT，Codex 窗口标题是 ChatGPT）弄清弹层长什么样，再改。
3. 新增 `tests\test_card02.py`：离线覆盖第 1 条（两框取大、去掉小框、只有一个框、没有框报错），用假节点，不碰真窗口。

## 怎样算完成
- 全量测试通过：`<user>\AppData\Local\Programs\Python\Python310\python.exe -X utf8 -B -m unittest discover -s tests`（PATH 上的 3.13 缺 uiautomation，别用）。
- 真机：`codex_choose.py --list` 输出正确的 model 和 effort（应为 GPT-6 Luna / 极高 或当时实际值），弹层被关掉。贴输出和截图路径。
- 真机：`workbuddy_use.py --list` 正常；不要用 workbuddy_use 给自己当前这个任务发消息。
- 不用真的给 Codex 发消息（额度用完了），也不要切 Codex 的模型。

## 允许改
`src\qi\conversation.py`、`src\scripts\codex_choose.py`；新增 `tests\test_card02.py`；在 CHANGES-20261030.md 末尾追加说明（改了什么、怎么回滚）。改之前先把这两个文件拷到 `_backup_20261031\` 下同样的相对路径。

## 禁止
不改其他 .py；不碰 F:\Bridge\v0.0.8；不调用 opencode/codex 等 CLI；不改注册表/系统设置/其他扩展；不合并；不在代码里写死地址或密钥；不点 Codex 的升级/充值。

## 最后一步（必做）
运行 `python src\scripts\notify_done.py --card 02 --status <done/stuck/failed> --note <一句原因>`。不管成没成都要报。