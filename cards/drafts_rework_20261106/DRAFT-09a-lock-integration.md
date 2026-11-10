# DRAFT — 09a-lock-integration

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 1 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:38`。


## 问题与文件行号证据

服务端持有 ui.lock 覆盖 choose→读回→use，子CLI的同锁入口却再次独占等待，造成父等子、子等父。简单以环境变量跳过锁会让无关CLI也能绕过互斥。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:25`：`src/qi/server.py:749-761,829-831` 父持锁后等待再次取锁的子CLI；`src/qi/uilock.py:256-263` 仅同线程重入。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09a.txt:28`：无UI子进程复现 child_exit=8/ui_busy；原AskTests的FakeRunner没有进入真实子锁。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 建立作用于同一根目录和同一锁会话的父子代持协议；核验真实内核持锁、父子进程身份/启动时间及不可误用的单次授权，不能只检查一个环境开关或pid。
2. server明确将授权仅交给自己启动的choose/use子进程；普通CLI继续通过同一内核互斥。锁覆盖choose→回执核对→use的整个服务端事务，子进程之间不得并行操作UI。
3. 拒绝伪造/缺失/过期/已释放/错误根目录或非当前父进程的授权；父退出或子超时/异常时不能留下绕过路径或让残留子进程与新CLI交错。
4. 不在进程全局环境中临时设置共享授权；授权按每次Popen传入并清理。保留退出8/ui_busy、同线程重入与死亡释放语义。

## 验收测试（离线，含入口与负例）

- 使用临时根目录、真实OS锁和真实Python子进程；执行实际server的端点链路与uilock.run入口，只用无UI fixture替换UI业务：choose回调→回执验证→use回调均进入，EXIT=0；不能整段mock ProcessRunner或hold。
- 同时启动无授权普通CLI，父事务期间退出8，结束后可进入；父子授权不得允许兄弟子进程同时进入UI区。
- 篡改授权、错误根目录、非父进程/启动身份不符、释放后重放、缺元数据、授权传递失败均不得执行业务回调。
- choose失败不执行use；子异常/超时/父进程死亡后不泄漏锁、不允许存活子进程与新持锁者交错；新的普通CLI最终可取锁。
- 保留真实子进程死亡释放、并发普通CLI互斥与同线程重入；同线程不同根目录不能误用重入跳过另一把锁。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不修request_id去重、workflow并发、900秒预算、auto-push或任何UI适配器；不重判09a整卡。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
