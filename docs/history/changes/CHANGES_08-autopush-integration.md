# CHANGES_08-autopush-integration — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

自动推送在跨进程autopush事务锁内重读判定/记账，UI事务内按用途配置choose→检查新回执→continue；真实runner复用09a可核验父子持锁协议。WorkBuddy空配置仍拒绝，不猜模型。推送前及choose后重新确认progress未变；高档传900秒+外层余量。只读already_answered独立记录而不计新推；仅sent=true/accepted才计一次真实提交（即使后续等回复失败）。dry-run不建锁、不选档、不写盘。新增真实无UI子CLI+真实gate验证过期/消费回执更新、两实例只提交一次、缺新回执拒绝及already_answered计数。旧CLI假件改成配置明确且真正写回执，不再把裸exit0当发送证据。没有接真实调度或实际推送。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:53:18.660579+08:00

```text
Ran 788 tests in 45.104s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_autopush_full1.txt`；结构化结果：`unittest_autopush_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/qi/chip.py`
- `src/qi/gemini.py`
- `src/qi/progress.py`
- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `src/qi/workbuddy.py`
- `src/qi/workflow.py`
- `src/scripts/chatgpt_choose.py`
- `src/scripts/grok_use.py`
- `src/scripts/workbuddy_autopush.py`
- `tests/test_card06a.py`
- `tests/test_card06c.py`
- `tests/test_card07.py`
- `tests/test_card08.py`

新增代码/测试支持文件：

- `src/qi/submissions.py`
- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework07_attribution.py`
- `tests/test_rework08_integration.py`
- `tests/test_rework09a_idempotency.py`
- `tests/test_rework09a_lock.py`
- `tests/test_rework09a_workflow.py`
- `tests/test_rework09f_recovery.py`

累计统一diff：`patches/08-autopush-integration.patch`；该阶段源码快照：`checkpoints/08-autopush-integration.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
