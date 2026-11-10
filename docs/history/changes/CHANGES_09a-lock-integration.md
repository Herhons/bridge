# CHANGES_09a — isolated WIP self-test

状态：实施与离线自测完成；未合并、未通知、未取得其他厂商独立验收。

原目录：`F:\Bridge\v0.0.9`（只读）；WIP：`F:\Bridge\v0.0.9_codex_wip`。复制基线：COPY_BASELINE.json。

父进程保持ui.lock主字节锁，server.run_process每次Popen通过child_launch显式下发随机会话/单次子启动授权，并在启动后登记子PID和启动时间。子CLI的真实uilock.run核验规范路径、父子PID/启动时间、会话/授权、未释放元数据以及实际内核锁；不接受仅靠环境开关跳锁。父子共享内核lease，child-entry互斥；父进程死后存活子仍持lease，普通CLI不能交错。普通CLI需同时独占主锁和lease。同线程重入按规范路径和PID隔离。

真实无UI fixture测试覆盖Engine→Popen→子uilock.run的choose/回执/use、事务间隙普通CLI拒绝、连续授权无全局环境泄漏、篡改/错根/过期/缺身份/无内核锁/未登记/兄弟复用/再委托拒绝、不同根重入、异常/超时、父死亡后的子lease。业务fixture不导入任何UI adapter，没有运行产品的真实choose/use。

原套件存在默认failure截图、日志和synthetic_input标记泄漏。新增tests/offline_guard/sitecustomize.py仅在本次测试环境启用：写盘及TEMP/TMP限WIP、外网和真实UI输入/窗口操作禁止、默认Screen.grab返回2×2合成图；runlog/failure/mark重定向到分轮_selftest。守卫不是功能验收，未修改旧单测预期。最初两个夹具调试失败日志保留（09a_target1/2）；NUL误报已修正，后续完整运行无守卫事件。

边界：只防本机误用，不防能改代码/元数据的恶意本地用户；普通互斥和实际Windows子进程已测，POSIX路径未在本机实测。未修去重/workflow/外层900秒等其他草稿。

## 验证

命令：`py -3.10 -X utf8 -B -m unittest discover -s tests -v`

开始时间（Asia/Shanghai）：2026-11-06T04:10:39.790216+08:00

```text
Ran 741 tests in 33.131s
OK
EXIT=0
GUARD_EVENTS=0
```

完整输出：`unittest_09a_full1.txt`；结构化结果：`unittest_09a_full1_result.json`。

## 相对复制基线的累计差异

修改：

- `src/qi/server.py`
- `src/qi/uilock.py`

新增代码/测试支持文件：

- `tests/offline_guard/sitecustomize.py`
- `tests/test_rework09a_lock.py`

累计统一diff：`patches/09a.patch`；该阶段源码快照：`checkpoints/09a.json`。

运行产物（日志、临时文件、CHANGES、patch、checkpoints）不算产品修改。后续阶段可能继续改同一文件，以此快照与diff追溯本阶段。最终原目录完整性另见ORIGINAL_INTEGRITY.json。
