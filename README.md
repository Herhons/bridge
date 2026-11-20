# Bridge v0.2.2

通用 AI 互调工具：**1 个总控入口 + 10 个独立端脚本**，以 Windows UIA 为主、截图模板识别为辅。

## v0.2.2 核心修复

- 修复 `uiautomation 2.0.29` 下 `_AutomationClient` 路径错误（BUG-01），恢复内层 `DocumentControl` 穿透与单次 `CacheRequest` 拉树，并显式暴露 `snapshot_degraded`。
- 排除地址栏/侧边栏搜索框误命中（BUG-02/08），为 `hermes` 和 `grok` 会话页补齐底部 `fallback_composer_ratio`。
- 修复 `--model` 假切换与菜单残留（BUG-03/04/05/07）：`default` 跳过切换、禁止同名假 toggle、失败按 `Esc` 关菜单、同步 Gemini `4 Hydrogen / Neon / Carbon` 档位别名。
- 增加发送回执校验与 `"正在工作"` 状态拦截（BUG-06），消灭 `grok` / `grokbot` 假成功。
- 清洗 DeepSeek 思考头/页脚噪声并按本轮提问锚定正文（BUG-09）。
- 新增 `--save <路径>` 参数，支持将 AI 完整回复直接落盘为本地文件。

## 快速开始（Windows）

```powershell
py -3.10 -m pip install -r requirements.txt
py -3.10 -m unittest discover -s tests -v
py -3.10 bridge.py status
py -3.10 bridge.py ask gemini "你好" --model hydrogen --headed --save .\runs\reply.txt
```
