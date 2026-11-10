# CHANGES_WEB-spawn-cleanup — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

_spawn通过finally覆盖place/focus/ready异常和普通失败，ensure_site的新窗口绑定/保存也在finally清理；清理失败不盖住原异常。仅处理已唯一识别的新HWND，多窗口并发归属不明时不猜、不放置、不关闭；已有用户窗口保留。新增实际_spawn假系统API路径及绑定保存失败测试，旧复用窗口负例保留。未启动真实浏览器。pycaw历史安装完整命令仍未在既有记录中找到，见evidence/WEB-spawn-cleanup-evidence.md；未将pip show当安装命令，也未伪造或重装。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:57:02.104997+08:00

```text
Ran 793 tests in 45.284s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_web_cleanup_full1.txt`；结构化结果：`unittest_web_cleanup_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
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

新增代码/测试支持文件：

- `evidence/WEB-spawn-cleanup-evidence.md`
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
- `tests/test_rework_web_cleanup.py`

累计统一diff：`patches/WEB-spawn-cleanup.patch`；该阶段源码快照：`checkpoints/WEB-spawn-cleanup.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
