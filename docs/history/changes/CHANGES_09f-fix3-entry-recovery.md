# CHANGES_09f-fix3-entry-recovery — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

WorkBuddy普通入口在新对话和回执门禁前先只读查已答、清回填；已消费回执不再阻断已答恢复。新增submissions持久提交意图：提交前原子写unknown；accepted/unknown/answered不会因新choose或重启而自动重发，损坏记录也按unknown；确认not_sent才可新尝试。key保守覆盖端点+完整正文hash，跨对话/重启有效，不保存正文；因此有意重复同文问题也需要人工核对，不能偷偷清除旧提交事实。Gemini/Grok实际发送函数也接同一保护，并在accepted等待中清回填、同时确认值为空和发送键状态，不重复提交。

高档server请求的内层回复预算至少900秒、runner额外30秒，endpoint预算包含choose预算、回复及60秒余量；预算不足拒绝启动而不是缩短900秒。离线入口测试真实跑WorkBuddy main→ask→共享写入/submit→wait，首次accepted后UiDocsLost导致中断；第二次已答清回填且submit总数1、gate调用未增加。另测重启路径、unknown加新回执、日志损坏/磁盘失败、两Web端pending与accepted回填，以及真实server命令/外层预算。未做真实发送或真实重启。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:42:56.747753+08:00

```text
Ran 776 tests in 40.964s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_09f_full2.txt`；结构化结果：`unittest_09f_full2_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/qi/chip.py`
- `src/qi/gemini.py`
- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `src/qi/workbuddy.py`
- `src/scripts/chatgpt_choose.py`
- `src/scripts/grok_use.py`
- `tests/test_card06a.py`
- `tests/test_card06c.py`
- `tests/test_card07.py`

新增代码/测试支持文件：

- `src/qi/submissions.py`
- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework07_attribution.py`
- `tests/test_rework09a_lock.py`
- `tests/test_rework09f_recovery.py`

累计统一diff：`patches/09f-fix3-entry-recovery.patch`；该阶段源码快照：`checkpoints/09f-fix3-entry-recovery.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
