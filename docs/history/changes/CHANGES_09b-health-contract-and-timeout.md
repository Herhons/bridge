# CHANGES_09b-health-contract-and-timeout — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

/health严格返回ok/version两字段，修正旧三字段断言。扩展API以Promise.race+AbortController保留截止时间到JSON正文完整读取结束，health/endpoints/job及其他API均有默认3000ms完整响应上限；无效JSON不再当空对象成功。面板配对/初始化并行发起endpoints与health，健康提示不等端点先返回。新增实际JS模块无网络fetch桩测试：无头、挂起/慢正文、endpoints/job截止、401/404/坏JSON、成功后定时器清理、单例poll解阻以及实际initialize函数体并发接线。未加载扩展或浏览器。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:00:31.814289+08:00

```text
Ran 794 tests in 45.464s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_health_full1.txt`；结构化结果：`unittest_health_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/chrome-extension/bridge-api.js`
- `src/chrome-extension/sidepanel.js`
- `src/qi/browser.py`
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
- `tests/test_card09a.py`
- `tests/test_card09b.py`

新增代码/测试支持文件：

- `evidence/WEB-spawn-cleanup-evidence.md`
- `src/qi/submissions.py`
- `tests/health_deadline_harness.mjs`
- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework06a_purpose.py`
- `tests/test_rework06b_presend.py`
- `tests/test_rework06c_composite.py`
- `tests/test_rework07_attribution.py`
- `tests/test_rework08_integration.py`
- `tests/test_rework09a_idempotency.py`
- `tests/test_rework09a_lock.py`
- `tests/test_rework09a_workflow.py`
- `tests/test_rework09b_deadline.py`
- `tests/test_rework09f_recovery.py`
- `tests/test_rework_web_cleanup.py`

累计统一diff：`patches/09b-health-contract-and-timeout.patch`；该阶段源码快照：`checkpoints/09b-health-contract-and-timeout.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
