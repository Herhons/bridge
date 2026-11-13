# Bridge v0.0.9

本地程序化调用多个模型端的工具。它不通过 API，而是用 Windows UI Automation、
截图模板匹配和剪贴板，驱动 8 个真实客户端：4 个网页端（ChatGPT / Gemini / Grok /
DeepSeek，跑在 Chrome 标签里）和 4 个桌面端（Codex / DeepSeek Harness / OpenCode /
WorkBuddy）。上面套一个本地 HTTP 后端，按端点串行执行「选档 → 读回确认 → 提问 →
收答 → 落证据」。

## 快速开始

```text
py -3.10 -m pip install -r requirements.txt     # numpy / opencv-python-headless / uiautomation / pycaw
py -3.10 src/scripts/bridge.py start     # 127.0.0.1:18765
curl http://127.0.0.1:18765/health              # {"ok": true, "version": "0.0.9"}
```

单端直调：`py -3.10 src/scripts/deepseek.py use "你的问题" --purpose review`
批量：`py -3.10 src/scripts/bridge.py ask --endpoints chatgpt,gemini --purpose review --prompt-file q.txt`

退出码与 stage 见 `docs/exit-codes.md`（0 正常 / 1 通用失败 / 3 资源缺失 / 4 用户占用 / 5 档位未确认 …）。

## 8 个端与档位

| 形态 | 端点 | 选档方式 |
|---|---|---|
| 网页 | chatgpt / gemini / grok / deepseek | 模型 + 推理强度 / 深度思考 |
| 桌面 | codex / dsh / opencode / workbuddy | 模型 + 强度（UIA 控件树） |

`purpose` 三档：`test`（最低）、`review` / `accept`（最高）。每档要求的 target 写在
`docs/tiers.json`，发送前由 `tier_guard` 读回执校验收。

## 目录职责

| 目录 | 职责 |
|---|---|
| `src/qi` | 内核：锁、定位、闸门、会话、账本、服务端 |
| `src/scripts` | 20 个 CLI 入口（每端 choose/use 一对 + server/workflow/notify） |
| `tools` | 诊断与批量工具（`live_job`、`arch_stats`、`submission_audit`、`verify_all`） |
| `tests` | 离线单测（1563 例，全部不需要真机 UI） |
| `docs` | 档位表、服务配置、退出码、统计（`ARCH-STATS.md` 由脚本生成） |
| `evidence` / `runs` | 运行取证与账本（不入库） |

## 四个关键机制

- **`uilock`**：一把跨进程文件锁，整轮持有。前台只有一个焦点，所以四席串行，不是并发。
- **`tier_guard`**：`choose` 读回确认后落一张回执，`use` 发送前必须拿它对上，否则拒发。
- **`submissions`**：以 `sha256(提问)` 为键的持久账本，六态（draft / submitting / accepted /
  unknown / failed_retryable / failed_terminal）。unknown 与 accepted 都会拦住重发，属 fail-closed。
- **证据**：每次失败落 `evidence/fail/*.json|png`，每次运行追加 `runs/YYYYMMDD.jsonl`。

## 测试

```text
env -u PYTHONPATH py -3.10 -X utf8 -B -m unittest discover -s tests
```

离线守卫生成临时 `_selftest/`、`_tmp/`（已 ignore）。真机验证用 `tools/verify_all.py`。

推送前会跑全量（`.githooks/pre-push`，约 160 秒，失败即拒绝推送）。钩子已入库，
新克隆的仓库启用一次即可：

```text
git config core.hooksPath .githooks
```

## 文档索引

| 文件 | 内容 |
|---|---|
| `docs/ARCH-STATS.md` | 代码关系统计（模块、依赖、重复度、覆盖） |
| `docs/exit-codes.md` | 退出码 / stage / 档位表（测试会校验一致性） |
| `docs/known_gaps.md` | 明确决定不做的项 |
| `docs/tiers.json` | 端与档位白名单 |
| `docs/bridge_server.json` | 端口、超时、席位数、并发 |
| `CALIBER.md` | 验收口径与状态机合同 |
| `CHANGES.md` | 批次索引（B17/B19 为当前） |
| `PLAN.md` | 当前阶段与历史快照 |

## 已知限制

- **deepseek** 输入框单次粘贴上限 2000 字，超了整段被吞；代码按 1500 字分块绕过。
- **grok** 会把粘贴文本里的换行折叠，多行提问会 `input_mismatch`；需要单行材料。
- **gemini** 提交后 3 秒内若无落点证据（提问块 / 新回复 / 生成中 / 会话 id），判 `submit_not_landed` 并重试一次。
- 全局串行：一轮最多 4 席，单席最高约 16 分钟，四席可能 20–60 分钟；期间不能动键鼠。
- `evidence/` 与 `runs/` 不入库。7 个测试模块依赖现场抓的 UIA dump 作夹具，**缺夹具时整模块跳过**（输出形如 `Ran 1428 … OK (skipped=7)`）；本地留了夹具就照常跑满。
