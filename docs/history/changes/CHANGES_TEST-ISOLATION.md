# CHANGES_TEST-ISOLATION — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

每个测试模块在导入产品模块前自动启用offline_guard，直接运行指定unittest命令或单文件discover都无需外部PYTHONPATH包装。默认日志、失败PNG/JSON、回执、ui.lock、progress、输入标记与应用状态重定向到本轮_selftest；TEMP/TMP到WIP/_tmp，运行中禁止修改源码/配置或其他目录。Screen.grab仅返回2×2合成图；原生截图/输入/剪贴板/窗口改动、UIA真实根、外部网络/未注册本地服务、普通应用启动被拒绝。只允许本进程绑定的回环假HTTP、继承守卫的Python子进程、已审查的Node测试harness与可跟踪测试子进程的taskkill。守卫初始化失败退出87；吞掉意外守卫异常仍在退出时返回86。

新增默认真实failure留证/输入标记隔离、真实无UI子进程继承、写盘/原生API/网络/应用启动拒绝与吞异常子进程非零退出测试，加整套末尾守卫检查。正常主进程及其子进程意外事件0；专门的expected-fail-child子轮故意触发1条outside_write并断言EXIT=86，作为反例证据保留，不掩盖/删除日志。预期反例另写expected_denials。真实icacls只可按固定参数收紧临时文件的当前用户权限；Windows子进程命令行使用系统CommandLineToArgvW解析。首次守卫调试误拦临时ACL且使用不合适的POSIX引号解析，失败日志isolation_full1保留；修复的是测试守卫，不放宽产品互斥或断言。此守卫防测试误操作，不是对恶意Python/原生代码的安全沙箱；不随正常产品CLI启用。不把合成截图或假UI自测当成真机验收。原目录保护文件哈希检查另见ORIGINAL_INTEGRITY.json；外部并发运行文件与无法读取的真实ui.lock单列，不修改或清理。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T05:40:29.987955+08:00

```text
Ran 814 tests in 47.113s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_final_full.txt`；结构化结果：`unittest_final_full_result.json`。

运行方式：literal command; external guard environment removed; repository bootstrap only。

本轮默认隔离目录：`F:\Bridge\v0.0.9_codex_wip\_selftest\direct_20261106_054030_076544_18744`。

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
- `tests/test_20261030.py`
- `tests/test_card02.py`
- `tests/test_card03.py`
- `tests/test_card04.py`
- `tests/test_card04b.py`
- `tests/test_card04c.py`
- `tests/test_card05.py`
- `tests/test_card06a.py`
- `tests/test_card06b.py`
- `tests/test_card06c.py`
- `tests/test_card06d.py`
- `tests/test_card07.py`
- `tests/test_card08.py`
- `tests/test_card09a.py`
- `tests/test_card09b.py`
- `tests/test_card09e.py`
- `tests/test_card09f.py`
- `tests/test_cardgem.py`
- `tests/test_cardweb.py`
- `tests/test_cardweb2.py`
- `tests/test_conversation.py`
- `tests/test_desktop_controls.py`
- `tests/test_progress.py`
- `tests/test_verifier.py`
- `tests/test_web_cli.py`

新增代码/测试支持文件：

- `REWORK_STATUS.json`
- `evidence/09e-workbuddy-restart-evidence.json`
- `evidence/09e-workbuddy-restart-evidence.md`
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
- `tests/OFFLINE.md`
- `tests/health_deadline_harness.mjs`
- `tests/offline_guard/__init__.py`
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
- `tests/test_rework_restart_evidence.py`
- `tests/test_rework_test_isolation.py`
- `tests/test_rework_web2_evidence.py`
- `tests/test_rework_web_cleanup.py`
- `tests/test_zz_offline_integrity.py`
- `tools/evidence_contract.py`
- `tools/sync_rework_plan.py`

累计统一diff：`patches/TEST-ISOLATION.patch`；该阶段源码快照：`checkpoints/TEST-ISOLATION.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
