# CARD-06d 小修：workbuddy_use --new 保持回执模型 / 读回复剪贴板回退 UIA（简单卡，不会审）

来源：06b/06c Hy3 验收实测（evidence\accept_06b06c_hy3_reply.txt）。

1. workbuddy_use --new：新开对话后 WorkBuddy 会把模型重置为默认。修法：新对话后读回当前模型，若与回执（tier_guard）里的模型不同，自动按回执模型重新选择并再次读回；仍不一致则 fail-closed，退出码 5 (tier_unconfirmed)，stage=model_reset_after_new，不发送。
2. workbuddy_use 读回复：复制按钮后剪贴板未更新时（比较复制前后内容/序列号），回退为 UIA 读取本轮回复文本，输出里标 read_via=uia；两者都失败才报错，并写 runlog。
3. 补单测（离线 mock）：覆盖 --new 模型重置→重选成功、重选失败退 5、剪贴板未更新→UIA 回退、两者都失败。

允许改：src\qi\workbuddy.py、src\scripts\workbuddy_use.py、src\scripts\workbuddy_choose.py（如需）、tests\test_card06d.py、CHANGES 追加、progress.json 本卡状态。
禁止：动 F:\Bridge\v0.0.8、opencode.json、注册表/策略、其他卡已有测试的预期。
完成标准：全量单测通过（≥412 + 新测）；真机 workbuddy_choose --model <非默认> 后 workbuddy_use --new --purpose test 发一句 ping，读回模型一致且回复可读；证据写 evidence\card06d_live.txt。完成后 progress.json 置 06d done。