# CARD-06b 发送安全门禁：档位读不到即拒发 + 输入归一 + 缩放/最小化/人机验证（r2）
来源：06b r1 经三家会审（Gemini argon / Grok expert / ChatGPT 聊天 GPT-6+高，三家均 REVISE），证据 evidence\review_card06b_{gemini,grok,chatgpt}.txt。原 r1 的适配/配置项（1/2/3/7/11/12）拆到 CARD-06c。
本卡只做机制，不加新端适配。

1. 档位读不到一律拒发（三家一致选 A）：*_use 发送前复核档位，芯片模板得分低于阈值、或读回为空，立即退出码 5（tier_unconfirmed，reason=tier_unreadable），不放行、不带警告放行、不自动重截模板后继续。允许先用 UIA 文本读一次作兜底，UIA 读到且与回执一致才算确认。
2. 档位回落不在 use 里自动重选：重试/新对话后读回值与回执不一致，退出码 5（reason=tier_drift），提示重跑 *_choose；use 自己不点菜单。
3. 发送前最后一刻复核：复核放在写入文本之后、点发送之前；复核后页面若发生导航/新对话/窗口切换，拒发。
4. 回执加固：runs\tier\<端>.json 改为原子写入（写临时文件后 rename）；回执里已有 hwnd/pid/process_start_time，use 消费时全部校验，不符即退出码 5（reason=receipt_identity）。
5. 输入归一只作用于比对，不改发出去的正文：ChatGPT/Grok 编辑器会把行首「- 」「* 」「1. 」「4. 」等转成列表并吃掉标记（实测 Grok 吃掉「N. 」导致 input_mismatch，1195 字读回 1192/1193 字）。比对时双方都去掉行首列表标记、统一换行(\r\n→\n)、去首尾空白再比；正文原样发送。input_mismatch 的 hint 里给出第一处差异的行号与前后 20 字。
6. 缩放：choose/use 开始前检测页面缩放（模板尺度），非 100% 时只对浏览器窗口发 Ctrl+0，然后重新检测，仍非 100% 则退出（stage=zoom_not_100），并记录。多尺度兜底匹配限定尺度 0.8–1.25，并记录实际得分与尺度；多尺度不能替代唯一性校验（同一模板多处命中视为失败）。
7. 最小化：所有 *_choose / *_use 遇到最小化窗口先还原（06a 已修 grok_choose），统一走同一函数；还原失败退出码 7。
8. 人机验证：检测到 Cloudflare 勾选框 / Unknown error 时，*_use 不点击、不重试，返回 stage=human_check（Unknown error 单独 reason=unknown_error），并自动截屏存证。巡检层可像正常用户点一次勾选框，点不过再记日志。
9. 失败自动留证：所有 tier_* / zoom_* / human_check / input_mismatch 失败，统一存一张截图 + 模板得分 + 当前缩放到 evidence\fail\<端>_<时间>.png/.json，统一 JSON 格式与日志格式。
10. 已知问题（本轮实测）：chatgpt_choose --new --writer chat --model gpt6 --effort high 返回 ok/before=high/after=high，但截图芯片显示 Medium（假读回，证据 evidence\chatgpt_false_readback_0547.png）；同时 gpt_model_gpt6 模板不命中却仍报 ok。修：任一维度未读回即整体 ok=false；读回必须来自本次截图/UIA，不能沿用滑杆键盘位置推断。
11. 删除 src\scripts\opencode_use.py 约 1033 行的重复行（06a GLM 验收指出）。

完成标准：单测全过 + 新测覆盖 1–11；真机：故意把 Grok 芯片模板换成错图，grok_use 必须退出 5（tier_unreadable）；Grok 发一段含「1. 」行首的文本不再 input_mismatch；证据 evidence\card06b_live.txt。
禁止：不碰 v0.0.8；不改注册表/策略/显卡驱动；不读 opencode.json 密钥；改前备份。
