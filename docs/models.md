# Bridge 模型策略与实时清单（v0.2.3）

单一事实源：`config/model_policy.json`。调用时建议优先使用 `--profile test|routine|council`，不再在任务卡里写死具体模型名。

## 场景强度策略（`--profile`）

| 档位 | 说明 | 收费拦截（Cost Guard） |
|---|---|---|
| `test` | 冒烟、连通性、回归与跨屏测试 | **强制免费 / `0.00x`**；若未确认免费档或检测到非零倍率（如 `0.11x`、`1.00x`），直接拒发并返回退出码 `5` |
| `routine` | 日常跑腿、轻度整理 | 允许低倍率/标准模型 |
| `council` | 多 AI 会审、架构评审、复杂排障 | 优先各端高强度推理模型 |

## 各端模型对照（最近真机核验：2026-11-20）

| 端 | `test`（免费） | `routine`（日常） | `council`（会审） | 当前实测菜单项与倍率 |
|---|---|---|---|---|
| `gemini` | `hydrogen` | `neon` | `carbon` | `日常帮助 4 Hydrogen`（免费）、`Neon`、`Carbon` |
| `workbuddy` | `free`（自动匹配 `0.00x`） | `hy4` | `ultimate` | `Hy4 flash 限时免费 0.00x`（免费）、`DeepSeek-V4.1-Flash 0.11x`（**已收费**）、`Hy4 0.29x`、`均衡 0.50x`、`GLM-5.3 0.69x`、`极致 1.00x` |
| `deepseek` | `default` | `default` | `think` | 默认对话（免费）、深度思考 R1 |
| `grok` | `fast` | `auto` | `expert` | `Fast`、`Auto`、`Expert` |
| `grokbot` | `default` | `default` | `default` | 客户端默认模型 |
| `hermes` | `freellm` | `freellm` | `freellm` | FreeLLMAPI 免费池 |
| `chatgpt` | `gpt56` | `gpt6` | `gpt61_sol` | GPT-5.6 / GPT-6 / GPT-6.1 Sol |
| `codex` | `default` | `default` | `gpt61_sol` | Codex 默认 / GPT-6.1 Sol |
| `dsh` | `deepseek` | `deepseek` | `luna` | DeepSeek / Luna |
| `opencode` | `glm_free` | `deepseek_free` | `claude_opus` | GLM Free / DeepSeek Free / Claude Opus |

## 随时刷新某端当前真实模型列表

```powershell
py -3.10 bridge.py models workbuddy --live
py -3.10 bridge.py models gemini --live
```

该命令会打开模型下拉框、采集所有可见选项并标注 `free` / `paid` / `unknown`，随后自动按 `Esc` 关闭菜单，不发送任何消息。
