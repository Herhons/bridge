# CHANGES_B19 — 修好 deepseek 与 gemini

状态：**两端都已修好并真机验证**。全量单测 1476 通过（基线 1452 + 新增 24）。
未合并、未通知。

WIP：`F:\Bridge\v0.0.9_codex_wip`；动手前的 6 个源码文件备份在 `_backup_batch19/`（后缀 `_bak-b19`）。

## 一、修了什么

### deepseek（走通用链 `flow.cli → flow.ask → conversation.ask`）

| # | 根因 | 修法 |
|---|---|---|
| 1 | **写入可能静默失败**：`write()` 先键入 3 个字，其余走粘贴；粘贴只等 `PASTE_SETTLE=2.5s`，**超时不报错**，`finally` 立刻还原剪贴板后正常返回。于是"只写进前 3 个字"要等到后面的 `input_mismatch` 才暴露 | 新增 `WriteNotLanded`（stage=`input_not_landed`）。打开 `write_verify` 后：长文本用 `PASTE_SETTLE_LONG=8s`、轮询改用轻量 `read_value()`（不再每轮截屏找图）、**超时未落地就抛**；`write()` 收到后先 `clear()` 再重写（最多 3 次） |
| 2 | **长回答采集空转**：`copies` 靠屏幕模板找动作条，长回答把它顶出视口 → `copies` 恒 0 → 等待循环每轮 `continue` → 25 分钟后 `no_reply`，而页面上回答早已完整 | 把 `deepseek` 加入 `UIA_TEXT_SITES`，复用 B17 为 dsh 写的只读 UIA 文本兜底（`uia_text_candidates`，只在 `positional` 就绪、copies 为空时生效，带截图证据）。**不需要滚动页面** |
| 3 | **判空语义错位**：`_empty_probe` 用"发送键可用 ⇒ 非空"，而 deepseek 有草稿时按钮本来就深蓝 → 自己留下的 3 字草稿被判成用户草稿，之后每次 `draft_present` | `_empty_probe` 改"文本优先"（`composer_empty` 判空即空），只对 `empty_by_text=True` 的端生效；另加 `adopt_self_draft`：输入框内容是**本提问前缀**时认成自己的残草清掉，用户自己写的仍 `draft_present` |
| 4 | **产品限制（不是 bug）**：输入框**单次粘贴上限 2000 字** —— 2000 进得去，2001 整段被吞掉，不截断也不报错 | 实测分块可绕过（1500+1500=3000 全在）。新增 `Site.paste_chunk`，deepseek 设 1500，`_paste_rest()` 分块粘、每块单独确认、最后一块用整段提问做归一化比较 |

### gemini（走专用链 `gemini.cli → ask → _send_and_wait`）

| # | 根因 | 修法 |
|---|---|---|
| 5 | **提交后没有落点校验**：`_submit` 点完即返回，之后只靠"提问块出现 / 多一条回复"判 `accepted`，判不到就轮询到 900 秒。真机现场：页面停在 `/app`、输入框已空、侧栏却新建了会话 | 提交后设 3 秒落点窗口，四种证据任一即可 `accepted`：提问块、新回复、**生成中（busy）**、**地址里出现会话 id**。3 秒无证据 → 清草稿重写重发一次，再给 3 秒 → 仍无则立即 `submit_not_landed`（带 `reason=composer_empty/draft_stuck`） |
| 6 | **新建对话不校验落点**：`open_new_chat` 点完 `sleep(0.9)` 就返回 `ok:clicked` | 点完校验三件事：地址里没有会话 id（回到 /app）、输入框干净、页面上没有上一轮消息块。不满足再点一次，两次都不行 → `newchat_not_landed`。**读不出树时保持 `ok=True, verified=False`**（不可读 ≠ 未落地） |
| 7 | `flow.looks_empty` 不认 gemini 占位 `问问 Gemini` → `clear_composer` 误报"仍有 10 字" | `looks_empty` / `clear_composer` 接 `site.placeholders`；gemini 填 `("问问 Gemini", "Ask Gemini")` |

## 二、新增 stage（都映射 `FAIL`，不新增数字码）

| stage | 含义 |
|---|---|
| `input_not_landed` | 文字没进输入框，一个字没发 |
| `submit_not_landed` | 点了发送，限定时间内没有任何落点证据 |
| `newchat_not_landed` | 点了「发起新对话」但仍在旧会话 |

## 三、改动文件

- `src/qi/conversation.py` —— `WriteNotLanded`、`write_verify`/`adopt_self_draft`/`paste_chunk`、`read_value()`、`_paste_rest()`、`_paste_via_clipboard` 落地校验、`_ask_impl` 映射、`UIA_TEXT_SITES` 加 deepseek、`composer_empty(placeholders=)`
- `src/qi/flow.py` —— `Site` 四个新字段、`_empty_probe` 文本优先、`looks_empty`/`clear_composer` 接占位符、`ask` 透传三个开关
- `src/qi/gemini.py` —— `_send_and_wait` 落点校验 + 一次重试、`open_new_chat` 落点校验、`SUBMIT_LANDING_WAIT=3.0`
- `src/qi/exit_codes.py` —— 三个新 stage
- `src/scripts/deepseek_use.py` —— `write_verify` / `adopt_self_draft` / `empty_by_text` / `paste_chunk=1500`
- `src/scripts/gemini_use.py` —— `placeholders`

新增测试（24 例）：`tests/test_b19_ds_write.py`(8)、`test_b19_ds_draft.py`(7)、`test_b19_ds_collect.py`(4)、`test_b19_gem_landing.py`(5)。
改动既有用例 2 处（都是契约变更，非迁就）：`test_card09f` 的粘贴调用签名、`test_b17_uia_text_fallback` 的白名单断言。

## 四、验证

离线：

```text
env -u PYTHONPATH py -3.10 -X utf8 -B -m unittest discover -s tests
Ran 1476 tests in 154.347s
OK
```

完整输出：`evidence/b19_unittest_final.txt`。

真机（`purpose=review`，两份材料）：

| 材料 | 端 | 结果 | 耗时 | 回复长度 |
|---|---|---|---|---|
| 714 字 | deepseek | done | 19.06s | 1339 |
| 2285 字 | deepseek | done | 19.41s | 1529 |
| 714 字 | gemini | done | 202.61s | 2749 |
| 2285 字 | gemini | done | 56.42s | 3813 |

证据：`evidence/b19_ds_714.json`、`b19_ds_2285c.json`、`b19_gem_714b.json`、`b19_gem_2285.json`。
四条都是实质评审意见，没有拒答。

## 五、过程中的两个"不是 bug"

1. **deepseek 输入框单次粘贴上限 2000 字**：2000 进、2001 整段被吞（不截断、不报错）。
   长度阶梯实测：800/1200/1600/2000 全部进得去；2001/2050/2200/2500 全部为 0。
   分块 1500+1500=3000 可以。这就是 `paste_chunk` 的由来。
2. **gemini 的持久化账本会挡住重发**：B18 那次卡在 `/app` 的尝试留下 `state=unknown` 的记录，
   之后同一条提问一律 `interrupted / submission_pending`。按报错里写的正规流程，
   核对现场（页面空、无回答）后把该记录移出账本、留证在
   `evidence/b18_review/submission_cleared/gemini-e83c1141….json`，不是删除。

## 六、回滚

| 现文件 | 备份 |
|---|---|
| `src/qi/conversation.py` | `_backup_batch19/src_qi_conversation.py_bak-b19` |
| `src/qi/flow.py` | `_backup_batch19/src_qi_flow.py_bak-b19` |
| `src/qi/gemini.py` | `_backup_batch19/src_qi_gemini.py_bak-b19` |
| `src/qi/exit_codes.py` | `_backup_batch19/src_qi_exit_codes.py_bak-b19` |
| `src/scripts/deepseek_use.py` | `_backup_batch19/src_scripts_deepseek_use.py_bak-b19` |
| `src/scripts/gemini_use.py` | `_backup_batch19/src_scripts_gemini_use.py_bak-b19` |

## 七、仍然存在的限制

- deepseek 的单次粘贴上限是**产品侧**的，`paste_chunk=1500` 只是绕过；如果哪天它把
  总长度也限死，还会再撞。撞上时会明确报 `input_not_landed`，不再静默。
- gemini 的 3 秒落点窗口对"极慢网络"可能误判为没发出去，兜底是**重发一次**；
  两次都无证据才报错，且 `submission` 会如实标 `unknown`（框已空时）。
- `UIA_TEXT_SITES` 现在有两个端（dsh、deepseek），别人的副本路径不变。
