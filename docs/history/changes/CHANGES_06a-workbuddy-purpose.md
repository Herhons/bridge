# CHANGES_06a — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

空/缺失/格式错误的用途表不再结构性放行；真实check/gate返回tier_unconfirmed/5并作废本次回执。WorkBuddy真实模型最低/最高的可信列表无法在离线任务核实，因此保留原空表、默认拒发；不声称WorkBuddy已可按用途真机发送。入口级假UI测试使用临时配置，覆盖test/review/accept未配置拒发、配置后正反向用途约束及零提交。保留TTL/身份/一次性消费规则。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:15:56.115784+08:00

```text
Ran 746 tests in 34.482s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_06a_full2.txt`；结构化结果：`unittest_06a_full2_result.json`。

## 相对复制基线的累计差异

修改：

- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `tests/test_card06a.py`

新增代码/测试支持文件：

- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework09a_lock.py`

累计统一diff：`patches/06a.patch`；该阶段源码快照：`checkpoints/06a.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
