# CHANGES_07-reply-attribution — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

WorkBuddy主循环和final_read共用attributed_reply：可见本轮提问后才接受其后的回复；提问虚拟化后仅在此前同轮/同文档已确认提问、相同文档runtime ID、回复数增加且正文不在发送前旧文本集中时才允许兜底。没有可靠锚点、裸baseline计数、控件替换、历史加载、旧正文重复、文档切换都保持unknown。ask真实捕获发送前快照，剪贴板须与已归属UIA正文一致，否则只用已归属UIA文字。旧正例夹具现在显式包含本轮问题，不再把无锚旧回复当成功。新增8项含主循环、最后一读、实际ask和虚拟化/旧剪贴板反例。未做真实UI收发验证。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:33:55.939951+08:00

```text
Ran 768 tests in 36.501s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_07_full2.txt`；结构化结果：`unittest_07_full2_result.json`。

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
- `tests/test_card07.py`

新增代码/测试支持文件：

- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework07_attribution.py`
- `tests/test_rework09a_lock.py`

累计统一diff：`patches/07-reply-attribution.patch`；该阶段源码快照：`checkpoints/07-reply-attribution.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
