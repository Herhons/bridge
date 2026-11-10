# CARD-07 小修：回复检测与输入框（简单卡，不会审）

来源：06c/06d Hy4 验收与巡检实测。

1. workbuddy_use：Hy4 preview 下页面已有回复仍报 no_reply 超时（evidence\accept_06c06d_hy4_reply.txt）。修法：等待循环中每轮除现有判据外，用 UIA 检查本轮是否已出现完整回复（停止生成按钮消失/复制按钮出现/文本稳定 2 轮）；超时前最后再用 UIA 读一次，读到则成功返回并标 read_via=uia，读不到才报 no_reply。
2. gemini_use：回复检测超时。修法：以“停止按钮消失 + 回复文本连续 2 次不变”判完成，并加 UIA/DOM 文本兜底；写 runlog 标明判据。
3. codex_use：新版 Codex 找不到 cdx_composer。修法：先恢复/置前窗口（最小化则 restore，失败退 7），点击输入区域使其获焦后再找 "+"/composer；模板匹配失败时回退 UIA 找编辑框。
4. gemini_choose --list 读不出档位：模板过期。修法：确认 Chrome 缩放 100% 后重截 gemini 芯片/菜单模板（旧模板备份为 .bak-card07），读回分数达阈值。
5. 补单测（离线 mock）覆盖 1–3 的回退分支。

允许改：src\qi\workbuddy.py、src\qi\gemini*、src\qi\codex*、src\scripts\workbuddy_use.py、gemini_use.py、gemini_choose.py、codex_use.py、对应模板文件、tests\test_card07.py、CHANGES 追加、progress.json 本卡状态。
禁止：动 F:\Bridge\v0.0.8、opencode.json、注册表/策略、其他卡已有测试的预期。
完成标准：全量单测通过（≥447 + 新测）；真机证据写 evidence\card07_live.txt：workbuddy_use 一次回复读回、gemini_use 一句 ping 回复读回、codex_use（最低档）一句 ping 找到 composer 并发送、gemini_choose --list 读回档位。完成后 progress.json 置 07 done。
