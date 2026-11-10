# CHANGES_B21 — 按会审意见优化

状态：三处 P0 已改、门禁已装、去重只记录结论。全量 **1504 用例通过**。

## 一、gemini 二次发送（会审戳中的真 bug）

**问题**：B19 的"提交后 3 秒无落点就重发一次"，在"第一次其实已提交、只是提问块还没渲染"的情况下会真的发两遍。判据 `textcmp.equal(prompt, again.value)` 只是自证（写进去必然读回一致），前面没有任何"确认没发出去"的检查。

**改法**（`src/qi/gemini.py`）：
- 新增 `SUBMIT_QUIET_WAIT=6.0` 与 `_landing_action()` 三态判定。
- **框里还留着我们这段字**（`draft_present`）= 端确实没收下 → 允许清空重写并再点一次；
- **框已空** = 歧义态 → 只开 6 秒静默窗，**绝不重点**；窗满仍无证据则 `submit_not_landed / composer_empty / unknown`；
- 重写后读回不一致 → `retry_readback_mismatch`、`not_sent`，不点击；
- 顺带修 `cleared_draft` 只存在局部变量、最终返回值恒为 None 的问题。

**回归**：`tests/test_b19_gem_landing.py` 从 5 条变 7 条。原来断言"框空也点两次"的用例改成断言**一次**；新增"框里有字才重试一次"与"静默窗内转 busy 仍算发出去"。

## 二、`lastreply` 竖排（真实 dump 复算出来的）

**问题**：`bubble_text` 用 `"\n".join(...)` 直接拼叶子文本。真实 chatgpt 气泡（`evidence/card09e_probe_chatgpt.txt` 283–648 行）有 **200 个叶子**，读出来就是 200 行、每行一个词。死代码 `_rows_to_text` 本来就是干这个的。

**改法**（`src/qi/lastreply.py`）：
- 接上 `_rows_to_text`；行归属改成"纵向重叠 ≥ 较矮高度 50%"（否则 bullet 会和上一行连成 `token。•`）；
- **行内保持文档序，不按 x 排序**——按 x 排会让中文乱码（实测字符集都变）；
- `_leaf_texts` 允许 `control=None`（离线夹具），`BoundingRectangle=None` 也跳过。

**效果**：200 行 → 53 行，去换行后**逐字相等**，无单字行，bullet 各自成行。

## 三、`desk_sites` schema 测试（唯一真正零覆盖的模块）

`tests/test_r12_desk_sites_schema.py` 10 条：键集、`name` 与键一致、`proc` 以 `.exe` 结尾、六个色彩通道 `0 ≤ min < max ≤ 255`、`send_size` 顺序、`composer`/`chip` 非 `(0,0)`、只有 opencode 有 `effort`、`copy_tpl` 以 `desk_` 开头、默认构造可用。专挡"复制一行改一半"。

> 会审说 `reject` / `lastreply` 也零覆盖——核实后不成立：`reject` 有 33 条（`tests/test_r11_reject_signals.py`），`lastreply` 也有（`test_card09e.LastReplyMarkers`）。这次给后者补的是**真实 dump** 用例。

## 四、pre-push 门禁

`.githooks/pre-push`（**入库**，LF，靠新增的 `.gitattributes` 的 `.githooks/** text eol=lf` 保证不被 CRLF 化）：从仓库根跑 `py -3.10 -X utf8 -B -m unittest discover -s tests -q`，失败打印最后 30 行并 `exit 1`。

- 启用：`git config core.hooksPath .githooks`（已在本机设好）。
- 正例：`sh .githooks/pre-push` → 0。
- 负例：临时克隆里塞一条必失败用例 → `git push` 被拒（`PUSH_EXIT=1`，远端分支未创建）。
- 不用 `tools/test_all.py`：它每次 `mkdir deliverables/tests/<stamp>`（已跟踪目录），会污染工作区。

## 五、choose 去重：不做，只记录

见 `docs/DEDUP-20261110.md`。要点：0.91–0.94 是 choose↔use，不是 choose↔choose（后者只有 0.21–0.57）；真正重复的只有 13 个基座函数约 500 行；三套菜单/读回/写回算法不可统一；lockstep 测试是有意的单文件契约。

## 六、验证与回滚

```text
env -u PYTHONPATH py -3.10 -X utf8 -B -m unittest discover -s tests
Ran 1504 tests in 157.351s
OK
```

新增 22 条用例（lastreply 12、desk_sites 10），b19 落点套件 +2。回滚：`git revert <本次提交>`；门禁删 `.git/hooks/pre-push` 即可。
