# CHANGES_04c-log-note — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

runlog.failure_note新增note-only原因保留，优先级固定hint>error>note>stage，空白字段跳过、原文不裁改、成功始终error=null。新增三桌面端真实record落盘恰好一行及note保留、组合优先级、空白/非字符串与成功携带旧失败文本负例；原idle异常原样抛出和hint入口回归随全套继续通过。只写临时日志。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:08:16.043143+08:00

```text
Ran 802 tests in 46.993s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_log_note_full1.txt`；结构化结果：`unittest_log_note_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/chrome-extension/bridge-api.js`
- `src/chrome-extension/sidepanel.js`
- `src/qi/browser.py`
- `src/qi/chip.py`
- `src/qi/gemini.py`
- `src/qi/guard.py`
- `src/qi/progress.py`
- `src/qi/runlog.py`
- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `src/qi/workbuddy.py`
- `src/qi/workflow.py`
- `src/scripts/chatgpt_choose.py`
- `src/scripts/codex_choose.py`
- `src/scripts/codex_use.py`
- `src/scripts/dsh_choose.py`
- `src/scripts/dsh_use.py`
- `src/scripts/grok_use.py`
- `src/scripts/opencode_choose.py`
- `src/scripts/opencode_use.py`
- `src/scripts/workbuddy_autopush.py`
- `tests/test_card06a.py`
- `tests/test_card06c.py`
- `tests/test_card07.py`
- `tests/test_card08.py`
- `tests/test_card09a.py`
- `tests/test_card09b.py`
- `tests/test_desktop_controls.py`

新增代码/测试支持文件：

- `evidence/WEB-spawn-cleanup-evidence.md`
- `src/qi/submissions.py`
- `tests/health_deadline_harness.mjs`
- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework04c_note.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework07_attribution.py`
- `tests/test_rework08_integration.py`
- `tests/test_rework09a_idempotency.py`
- `tests/test_rework09a_lock.py`
- `tests/test_rework09a_workflow.py`
- `tests/test_rework09b_deadline.py`
- `tests/test_rework09e_self_input.py`
- `tests/test_rework09f_recovery.py`
- `tests/test_rework_web_cleanup.py`

累计统一diff：`patches/04c-log-note.patch`；该阶段源码快照：`checkpoints/04c-log-note.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
