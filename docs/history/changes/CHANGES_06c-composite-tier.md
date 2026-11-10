# CHANGES_06c — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

ChatGPT以同轮writer/model/effort完整回执执行用途联合核验；后写单维不能冒充联合成功。发送前读回全部必要维度，任一缺失/漂移/模式变化拒发。保持其他端单维语义；真实UI未测，交其他厂商后续验收。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:29:31.587234+08:00

```text
Ran 760 tests in 36.021s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_06c_full2.txt`；结构化结果：`unittest_06c_full2_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/qi/chip.py`
- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `src/qi/workbuddy.py`
- `src/scripts/chatgpt_choose.py`
- `tests/test_card06a.py`
- `tests/test_card06c.py`

新增代码/测试支持文件：

- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework09a_lock.py`

累计统一diff：`patches/06c.patch`；该阶段源码快照：`checkpoints/06c.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
