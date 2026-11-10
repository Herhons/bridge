# CHANGES_B17 — 四个网页端 + 四个桌面端跑通

状态：**四个网页端 4/4 + 四个桌面端 4/4 均已达成，且桌面端连续两轮复现**（R4i / R4j 都是同一轮 `status=done`、quorum `recovered=4`、四条回复逐字等于 nonce）。
按服务端既有的 `max_endpoints_per_job=4` 分两轮交付（4 网页 + 4 桌面），"8 端同一 job"不在范围内（见「六」）。
未合并、未通知、未取得其他厂商独立验收。

WIP 目录：`F:\Bridge\v0.0.9_codex_wip`；本批次改动的原文件逐个备份在 `_backup_batch17/`（后缀 `_bak-b17`）。

## 一、这一批到底在修什么

一句话：**四个端各自的「定位 + 读数」在不同界面态下会给出错误结论，而错误的结论又被闸门当成真档位**。
表现都是 `choose_failed` / `unverified` / `tier_drift` / `input_mismatch` 这类退出码，但根因分散在
"读数被界面骗了"和"写入没等 UI 跟上"两件事上。

| # | 现象（真机） | 根因 | 修法 |
|---|---|---|---|
| 1 | chatgpt `tier_unconfirmed / tier_unreadable`（退出码 5） | 新版界面把模型选择做成 `Button`，只剩「选择 ChatGPT 模型」一个名字，无子文本、无 Value/HelpText；`sendgate.pre_send` 直接读芯片必然读空 | `sendgate.pre_send` 加 `reread=` 注入点；`chatgpt_use` 在**写入之前**取一次档位快照（`take_snapshot`），发送前的复核读快照（`snapshot_read`），过期/缺失一律返回 `None` 让闸门照旧拒发 |
| 2 | chatgpt `tier_drift`：模型被打回 `gpt6_luna` | 新版界面模型是**按会话**的：`--new` 再点一次「新聊天」会把 choose 设好的模型抹掉（强度是账号级的，反而留着） | `Site.blank_new` + `chatgpt_use._blank_new`：已经在空的新对话页就跳过点击（`stage=already_new`） |
| 3 | chatgpt 强度读不到 / 设错 | 强度弹层里是滑杆，UIA 只播报「最高，第 5 项，共 5 项。」；`chip.EFFORT_WORDS` 缺「最高」，且「高」会先命中 | `chip.EFFORT_WORDS` 首位加 `("最高","max")`；`_announce_effort` 先按「第 N 项」序号映射，再退回词串；`_effort_item` 给焦点点击用的真实中心点 |
| 4 | chatgpt 模型行点不到（在可视区外） | 旧代码只认 onscreen 行 | `_ui_model_rows` 带 `offscreen` 标记；`_find_item_uia` 先 `ScrollItemPattern.ScrollIntoView` 再重枚举；全隐藏时返回 `selected_offscreen` |
| 5 | grok `input_mismatch`：只写进前 3 个字 | 新输入框换成 `ComboBoxControl`，`Ctrl+V` 后约 1.3s 文本才进 UIA 值；旧代码 0.35s 就还原剪贴板，粘贴被打断 | `_paste_via_clipboard` 改成有界轮询（`PASTE_SETTLE=2.5` / `PASTE_POLL=0.25`）直到文本真的进输入框再还原剪贴板；`_read_written` 对"值只是提问前缀"的滞后形态再等一拍 |
| 6 | grok 草稿清不掉 | `Ctrl+A/Delete` 对 `ComboBoxControl` 无效 | `Conversation.clear` 读回非空时用 `GetValuePattern().SetValue("")` 兜底（实测清成 `'\n'`，视为空） |
| 7 | chatgpt `conversation_changed / document_changed`（其实发送成功、回复也读到了） | 临时会话 `/c/local-chatgpt:<uuid>` 落库成 `/c/<uuid>`，被当成换了会话 | `_local_chat_persisted()` 窄规则（只认这一种跳变）+ 收集循环里允许重新锚定一次 |
| 8 | dsh `copied_prompt`：复制到的总是提问本身 | 回答动作条还没渲染出来就判死 | `PROMPT_COPY_GRACE=20s` 有界等待；复制条数一变就重来 |
| 9 | dsh `interrupted`：复制回复后剪贴板没更新 | 动作条刚出现、坐标还在动画里，点空 | `except RuntimeError` 收进 `copy_error`，与 #8 共用同一段有界重试，超时才把异常抛回去 |
| 10 | dsh choose `panel_not_open` | 点「模型」行落在动画里 | 两份脚本各加"再点一次 + 多等 2.5s"，仍不行才判死 |
| 11 | **dsh `unverified` / `model=unknown`（score 0.348）** | **面板里"当前选中"那一行是浅灰 #F4F4F5，过不了 `>250` 的白色阈值，同一块面板被切成上下两段（上段搜索框 240×62、下段列表 240×218）。旧代码只取"面积最大的单块"，拿到的是下段 → 点 `panel[1]+16` 落在模型行而不是搜索框 → 接着 `Ctrl+A` 选中整页 → 芯片变白字蓝底，模板全崩（0.956 → 0.348）** | `panel_bbox` 把"同 x、同宽、竖直相邻（间隙 ≤ 一整行）"的碎块**合并**成一整块再挑，并要求面板贴在芯片上方；`set_model/set_effort` 前置一次 `_deselect_page`；读数 unknown 时清一次选中再读；`Ctrl+A` 改成**只有搜索框里真的有残留文本才发** |
| 12 | dsh 发送前 `tier_unreadable`（退出码 5，但截图里档位完全正确） | 同上：残留的整页选中让发送前的只读复核读到 `unknown` | `dsh_use.ask` 打字之前先 `_deselect_page`（此时输入框还空着，点击无副作用）；`_reread_tier` 仍然保持"一个地方都不点"的只读契约 |

## 二、改动的文件

代码：

- `src/qi/chip.py` —— `EFFORT_WORDS` 增补「最高」→ `max`（必须排在「高」前面）
- `src/qi/conversation.py` —— 粘贴有界轮询、`clear()` 的 `SetValue` 兜底、复制失败/只复制到提问的有界重试、临时会话落库重锚
- `src/qi/flow.py` —— `Site.blank_new` 与 `_open_new_chat` 的"已是空新对话"短路；`door()` 透传 `reread`
- `src/qi/sendgate.py` —— `pre_send(..., reread=...)` 注入点
- `src/qi/server.py` —— job 证据透出 `source` / `basis` / `screenshot`（UIA 文本兜底要能追溯）
- `src/qi/workbuddy.py` —— 首页侧栏标签补「新会话」（旧标签一个都匹不上）
- `src/scripts/chatgpt_choose.py` —— UIA 滚动找行、强度弹层序号映射、芯片/滑杆双源回读、`_record_composite` 事后独立读数、`_dump_unverified` 落截图
- `src/scripts/chatgpt_use.py` —— 档位快照（`take_snapshot` / `snapshot_read` / `prepare`）、`_blank_new`
- `src/scripts/dsh_choose.py` / `src/scripts/dsh_use.py` —— 两份保持 lockstep：`panel_bbox` 碎块合并、`_deselect_page`、条件式 `Ctrl+A`、面板点击重试

数据/配置：

- `docs/tiers.json`（备份 `_backup_batch17/docs_tiers.json_bak-b17`）
- `docs/bridge_server.json` —— `stall_busy_ignore={"codex":45,"dsh":30}`；`endpoint_concurrency` 由 4 **降到 1**（见「六、环境与回滚」）

测试：

- 新增 `tests/test_b17_chatgpt_effort.py`（7）、`tests/test_b17_chatgpt_uia_scroll.py`（8）、
  `tests/test_b17_chatgpt_tier_snapshot.py`（23）、`tests/test_b17_dsh_geometry.py`（14）、
  `tests/test_b17_dsh_lockstep.py`（5）、`tests/test_b17_uia_text_fallback.py`（12）
- 改写 `tests/test_card06b.py` 的强度/词表用例

## 三、离线验证

命令（`tests/OFFLINE.md` 的标准写法，必须 `py -3.10`，3.13 上没有 `uiautomation`）：

```text
env -u PYTHONPATH py -3.10 -X utf8 -B -m unittest discover -s tests
Ran 1452 tests in 152.962s
OK
EXIT=0
```

完整输出：`evidence/batch17/unittest_b17_final8.txt`（基线 1382 → 本批 1452）。

用 AST 抠单个 `FunctionDef` + 手写 namespace `exec` 的既有测试风格仍在用，所以新加的模块级 helper
一律通过 `globals().get(...)` 软引用（取不到就跳过），否则 `test_b16_dsh_choose` /
`test_desktop_controls` 会 `NameError` —— 这次踩过一次，26 条 error，改回软引用后恢复。

## 四、环境与回滚

回滚（逐个原文件）：

| 现文件 | 备份 |
|---|---|
| `src/scripts/chatgpt_choose.py` | `_backup_batch17/src_scripts_chatgpt_choose.py_bak-b17` |
| `src/scripts/dsh_choose.py` | `_backup_batch17/src_scripts_dsh_choose.py_bak-b17` |
| `src/scripts/dsh_use.py` | `_backup_batch17/src_scripts_dsh_use.py_bak-b17` |
| `src/qi/conversation.py` | `_backup_batch17/src_qi_conversation.py_bak-b17` |
| `src/qi/server.py` | `_backup_batch17/src_qi_server.py_bak-b17` |
| `src/qi/workbuddy.py` | `_backup_batch17/src_qi_workbuddy.py_bak-b17` |
| `docs/tiers.json` | `_backup_batch17/docs_tiers.json_bak-b17` |
| `docs/bridge_server.json` | `_backup_batch17/bridge_server.json.bak` |
| `src/assets/library/dsh_*.png` | `_backup_batch17/templates/dsh_*.png` |

`chip.py` / `sendgate.py` / `flow.py` / `chatgpt_use.py` 没有单独备份（只加了新分支与注释，没有破坏性改写）；
要整体回到本批之前，请用 `COPY_BASELINE.json` 的复制基线，不要逐文件拼。

`endpoint_concurrency` 4 → 1，理由不是"降速换稳"，而是**四个端本来就不可能真并行**：
`*_choose.py` / `*_use.py` 走 `uilock.run()`，**整轮持同一把磁盘 UI 锁**（前台只有一个焦点）。
并发 4 只会让 4 个进程一起排在锁上、各自带一份超时等待，超时还会变成退出码 8 的假故障。
降到 1 是把"排队"挪回调度层，语义不变。实测整轮 2.5–3.7 分钟（R4j 147s / R4i 222s）。
恢复方法：把 `docs/bridge_server.json` 里的 `endpoint_concurrency` 改回 4 再重启后端
（`src/scripts/bridge_server.py start --foreground`），但那样会重新引入锁竞争。

## 五、真机结果

网页端（四个一起，同一轮 job）：

| 端 | status | 耗时 | 回复 |
|---|---|---|---|
| chatgpt | done | 126.27s | `BRIDGE-B17-S2c-9acda85d` |
| gemini | done | 110.53s | `BRIDGE-B17-S2c-9acda85d` |
| grok | done | 117.64s | `BRIDGE-B17-S2c-9acda85d` |
| deepseek | done | 119.49s | `BRIDGE-B17-S2c-9acda85d` |

quorum 4/4，证据 `evidence/batch17/job_S2c.json`、`live_S2c.txt`。

桌面端单席（分别跑通）：

| 端 | 结果 | 耗时 | 回复 |
|---|---|---|---|
| chatgpt（网页单席，验证档位链路） | answered | 28.5s | `BRIDGE-B17-GPT-b3f8` |
| grok（单席） | answered | 10.2s | `BRIDGE-B17-GROK-e6a3` |
| dsh（单席） | answered | 4.4s | `BRIDGE-B17-DSH-c7e2` |

桌面端四端同一轮：

| 轮次 | codex | opencode | dsh | workbuddy | 失败点 |
|---|---|---|---|---|---|
| R4c | done | done | failed | done | dsh `copied_prompt` |
| R4d | done | done | failed | done | dsh `interrupted` |
| R4e | done | done | failed | done | dsh choose `panel_not_open` |
| R4f | done | done | failed | done | dsh choose `panel_not_open` |
| R4g | done | done | failed | done | dsh choose `panel_not_open` |
| R4h | done | done | failed | done | dsh `unverified`（面板被切成两段 → 点错位置 → 整页全选 → 模板崩） |
| **R4i** | **done 94.2s** | **done 101.5s** | **done 16.6s** | **done 10.6s** | **无（4/4）** |
| **R4j** | **done 18.9s** | **done 101.5s** | **done 16.8s** | **done 11.1s** | **无（4/4，复现）** |

R4i 明细（`evidence/batch17/job_R4i.json`、`live_R4i.txt`、`r4i_nonce.txt`）：

```text
nonce = BRIDGE-B17-R4i-e46665f3
status = done  quorum = {'required': 3, 'recovered': 4, 'met': True, 'clamped': False}
codex      done  94.20s  reply='BRIDGE-B17-R4i-e46665f3'  choose: gpt6_luna        (readback 一致)
opencode   done 101.48s  reply='BRIDGE-B17-R4i-e46665f3'  choose: deepseek_v41_flash_free
dsh        done  16.58s  reply='BRIDGE-B17-R4i-e46665f3'  choose: deepseek_v41_flash (10.0s，含面板重试)
workbuddy  done  10.61s  reply='BRIDGE-B17-R4i-e46665f3'  choose: DeepSeek-V4.1-Flash
ALL_EXACT_AND_DONE = True
```

R4j（紧接着的复现轮，`job_R4j.json`）：`BRIDGE-B17-R4j-cc04705b`，
codex 18.91s / opencode 101.53s / dsh 16.77s / workbuddy 11.14s，四条 reply 逐字一致，
`ALL_EXACT_AND_DONE = True`。

### dsh 这一条为什么确定是它（不是"选择失败"）

- **故障现场**：`job_R4h` 报 `after=unknown, score=0.348`，但同一时刻的截图里芯片明明写着
  `DeepSeek-V4.1-Flash Off` —— 见 `evidence/batch17/dsh_chip_now_zoom.png`（白字压在蓝色选中底色上）。
- **人为复现**：点正文 → `Ctrl+A` → `--list` 得到 `{'model':'unknown','score':0.348,'effort':'unknown','effort_score':0.541}`，
  与失败数字**逐位一致**。
- **面板被切成两段**：`evidence/batch17/dsh_panel_full.png`；离线重算 `panel_bbox`，
  旧逻辑返回下半段 `(883, 1010, 240, 218)`（点 `+16` 落在模型行上），
  新逻辑返回整块 `(883, 899, 240, 329)`（点 `+16` 落在搜索框 `y=915`）。
- **修复后**：污染态下 `--model deepseek_v41_flash` → `ok=true, score=0.956`；
  真实切换 `luna ↔ deepseek` 双向 `0.996 / 0.965`；读数 `--list` 回到 `0.956 / 0.979`。

## 六、未验项与已知限制

- **"8 端同一轮"不在本次范围，且是配置层就不允许**：`docs/bridge_server.json` 的
  `max_endpoints_per_job` 是 **4**（B17 之前的备份 `_backup_batch17/bridge_server.json.bak`
  里同样是 4，不是本批改的）。服务端拒绝第 5 席时的原文就写着
  「一个 job 最多 4 席…请分两轮：先 4 个网页端会审，再 4 个桌面端安排任务」。
  所以本批按设计交付**两轮**：4 网页（S2c）+ 4 桌面（R4i / R4j）。把上限抬到 8 需要改配置
  并重跑约 12–15 分钟（串行），已与需求方确认**不做**。
  那次尝试的原始 400 报错留在 `evidence/batch17/live_R5.txt`（提交阶段就被拒，没有 job 落盘）。

- **dsh 的 `gpt6_luna` 没有「推理等级」行**：切到 luna 之后 `--effort` 会返回 `no_effort_row`。
  当前档位表里 dsh 的 test/review/accept 都用 `deepseek_v41_flash`，所以不影响本批结论；
  但如果以后要"luna + 高强度"，得先确认它的强度是不是账号级、要不要另走一条读法。
- **`Ctrl+A` 仍然是全局键**：现在只在"搜索框里确实有残留文本"时才发，但极端情况下（占位模板误判）
  仍可能落到页面上。已有 `_deselect_page` 兜底把读数救回来，不过这是"事后补救"，不是"事前避免"。
- **`endpoint_concurrency` 留在 1**：已经论证过它不是"降低要求"（四端本来就抢同一把 `uilock`），
  但没有在 4 下重新跑过整轮；要回到 4 需重新评估锁竞争超时。
- **未做**：gemini/deepseek 的单席截图链路复验（本批只跑了它们所在的四端整轮）。
