# CHANGES_09e-self-input-all-entries — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

三个桌面端的choose/use共六个真实idle入口调用共享guard；六份Desk的click/press/hotkey/type_text均写共享合成输入标记。标记只在5秒有效期及有限时钟容差内生效，损坏/缺wall/过期/越界tick不忽略真人输入，保留32位tick回绕计算。运行入口缺少qi锁运行时改为退出8，不再降级成无锁UI；定位函数仍独立，公共安全行为明确依赖qi。新增六入口自己的idle函数和24个输入原语假OS测试、后续真人输入、损坏/未来/过期标记及tick回绕。没有真实键鼠/剪贴板操作。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:05:50.201595+08:00

```text
Ran 798 tests in 46.945s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_self_input_full1.txt`；结构化结果：`unittest_self_input_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `docs/exit-codes.md`
- `docs/tiers.json`
- `src/chrome-extension/bridge-api.js`
- `src/chrome-extension/sidepanel.js`
- `src/qi/browser.py`
- `src/qi/chip.py`
- `src/qi/gemini.py`
- `src/qi/guard.py`
- `src/qi/progress.py`
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
- `tests/test_rework09e_self_input.py`
- `tests/test_rework09f_recovery.py`
- `tests/test_rework_web_cleanup.py`

累计统一diff：`patches/09e-self-input-all-entries.patch`；该阶段源码快照：`checkpoints/09e-self-input-all-entries.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
