# CHANGES_09a-durable-idempotency — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

request_id查询不再限最新200条，内存淘汰/服务重建后仍从全部历史job去重。接单使用跨Engine/进程requests.lock包住查询与初次落盘，持久化成功后才放入内存并派发；初次落盘失败返回503且没有内存假job/入队。损坏或无法核实的历史记录拒绝新派发，不能静默按新请求重发。新增251历史、重建、磁盘失败安全重试、两个Engine并发和损坏历史负例。查询为线性扫描，耗时会随历史job数量增长；未擅自设新的过期窗口，也未引入旧版数据库。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:45:56.235757+08:00

```text
Ran 780 tests in 41.321s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_idempotency_full1.txt`；结构化结果：`unittest_idempotency_full1_result.json`。

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
- `tests/test_rework09a_idempotency.py`
- `tests/test_rework09a_lock.py`
- `tests/test_rework09f_recovery.py`

累计统一diff：`patches/09a-durable-idempotency.patch`；该阶段源码快照：`checkpoints/09a-durable-idempotency.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
