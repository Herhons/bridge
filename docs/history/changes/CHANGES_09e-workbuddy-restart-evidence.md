# CHANGES_09e-workbuddy-restart-evidence — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

离线检索并索引WorkBuddy重启原始资料；card09e_live明确完整链路在OpenCode运行，WorkBuddy只做AppID/exe/docs预检，rehearsal原始JSON明确close_skipped。保存原始哈希与行号摘录、检索候选和缺失链路，不用另一个app、预检、review claim或离线恢复测试冒充。新增端点/步骤顺序/close_skipped/来源完整性负例。本卡允许范围内的离线整理完成，live_evidence_status仍pending；没有关闭/启动任何应用或发送消息。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:21:09.288751+08:00

```text
Ran 809 tests in 46.141s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_restart_evidence_full1.txt`；结构化结果：`unittest_restart_evidence_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `PLAN.md`
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

- `evidence/09e-workbuddy-restart-evidence.json`
- `evidence/09e-workbuddy-restart-evidence.md`
- `evidence/WEB-spawn-cleanup-evidence.md`
- `evidence/WEB2-evidence.json`
- `evidence/WEB2-evidence.md`
- `evidence/recheck_sources/CARD-04c.txt`
- `evidence/recheck_sources/CARD-05.txt`
- `evidence/recheck_sources/CARD-06a.txt`
- `evidence/recheck_sources/CARD-06b.txt`
- `evidence/recheck_sources/CARD-06c.txt`
- `evidence/recheck_sources/CARD-06d.txt`
- `evidence/recheck_sources/CARD-07.txt`
- `evidence/recheck_sources/CARD-08.txt`
- `evidence/recheck_sources/CARD-09a.txt`
- `evidence/recheck_sources/CARD-09b.txt`
- `evidence/recheck_sources/CARD-09e.txt`
- `evidence/recheck_sources/CARD-09f-fix2.txt`
- `evidence/recheck_sources/CARD-GEM.txt`
- `evidence/recheck_sources/CARD-PLAN-r1.txt`
- `evidence/recheck_sources/CARD-WEB.txt`
- `evidence/recheck_sources/CARD-WEB2.txt`
- `evidence/recheck_sources/SUMMARY.txt`
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
- `tests/test_rework_plan.py`
- `tests/test_rework_restart_evidence.py`
- `tests/test_rework_web2_evidence.py`
- `tests/test_rework_web_cleanup.py`
- `tools/evidence_contract.py`
- `tools/sync_rework_plan.py`

累计统一diff：`patches/09e-workbuddy-restart-evidence.patch`；该阶段源码快照：`checkpoints/09e-workbuddy-restart-evidence.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
