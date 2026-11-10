# CHANGES_WEB2-evidence — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

离线检索并建立WEB2原始证据索引：源文件哈希、精确行号/摘录、搜索候选与未满足的同轮fast链路。cardweb2有send/read同token但无最低档读回；card06b明确fast切换未跑；card07可支持Gemini最低档，不能顶替Grok。新增描述符校验与来源完整性测试，拒绝expert、其他端、错token、review claim充当最低档证据。本卡离线整理完成，live_evidence_status仍pending；原复验FAIL不改，没有新UI操作，不重放历史token。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:15:24.757582+08:00

```text
Ran 807 tests in 46.008s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_web2_evidence_full1.txt`；结构化结果：`unittest_web2_evidence_full1_result.json`。

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
- `tests/test_rework_web2_evidence.py`
- `tests/test_rework_web_cleanup.py`
- `tools/evidence_contract.py`
- `tools/sync_rework_plan.py`

累计统一diff：`patches/WEB2-evidence.patch`；该阶段源码快照：`checkpoints/WEB2-evidence.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
