# CHANGES_06b — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

WorkBuddy发送入口改为不消费的写前预检与写后、提交前的新鲜窗口/文档只读复核；最后门禁才消费回执，并接人机检查与统一失败留证。新增真实_run→ask→send_and_wait的假UI入口用例验证顺序和漂移拒发。保留06a空表拒发与06d新对话恢复特例。未解决09f普通入口的已答恢复草稿。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:21:14.388799+08:00

```text
Ran 753 tests in 35.236s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_06b_full3.txt`；结构化结果：`unittest_06b_full3_result.json`。

## 相对复制基线的累计差异

修改：

- `src/qi/server.py`
- `src/qi/tier_guard.py`
- `src/qi/uilock.py`
- `src/qi/workbuddy.py`
- `tests/test_card06a.py`

新增代码/测试支持文件：

- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework09a_lock.py`

累计统一diff：`patches/06b.patch`；该阶段源码快照：`checkpoints/06b.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
