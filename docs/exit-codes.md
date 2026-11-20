# Bridge v0.2.2 退出码约定

| 退出码 | 含义 | 典型触发场景 |
|---:|---|---|
| `0` | 正常完成 | `ask` 收到完整回复、`read` 成功、`status` 窗口在线、`models`/`list` 正常返回 |
| `1` | 通用失败 | 超时（`timeout`）、未知端点、非预期运行异常 |
| `2` | 参数错误 | CLI 参数缺失或非法 |
| `3` | 资源缺失 | 目标窗口未打开（`window_not_found`）、找不到输入框（`composer_not_found`）或目标控件未命中 |
| `4` | 用户取消 | 开启 `--confirm` 后用户在发送前拒绝（`cancelled_by_user`） |
| `5` | 档位未确认 | 指定了模型/档位校验但目标端未能确认（`tier_unconfirmed`） |
| `6` | 重复提交拦截 | 账本中该 `endpoint + sha256(prompt)` 已处于 `submitting`/`accepted`/`unknown`（`duplicate`）；如需重发可传 `--force` 或执行 `bridge.py ledger clear` |
