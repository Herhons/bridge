# DRAFT — 09b-health-contract-and-timeout

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 6 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:43`。


## 问题与文件行号证据

health合同仅{ok,version}，多port未获改卡授权；3秒只覆盖响应头使正文与初始化仍可永久挂起。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09b.txt:24`：`src/qi/server.py:1183-1185` 多port；`tests/test_card09b.py:377-382` 旧预期偏离合同。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09b.txt:28`：`src/chrome-extension/bridge-api.js:36-68` fetch头到即clearTimeout，正文可挂起；P09B_health_body_deadline已复现。
- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-09b.txt:31`：initialize先await无期限endpoints，pollJob也可能阻塞唯一tick。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. health严格只返回ok/version，更新旧迎合实现的断言。
2. 截止时间覆盖请求发起、响应头、完整正文/JSON读取，超时abort且界面可恢复；endpoints/job亦设有界可诊断预算。
3. 健康状态不被endpoints挂起阻塞，单例poll tick超时后可释放并恢复；401/404等错误保持原合同。

## 验收测试（离线，含入口与负例）

- 真handler分支断言精确字段集合；真实JS模块+假fetch/时钟覆盖无头、头到正文永不结束、慢正文、JSON错误、401、404、abort和恢复。
- 实际initialize与pollJob挂起时健康提示仍出现、tick随后可继续；不是只正则检查3000。
- 不安装/加载Chrome、不连生产服务；Node测试无网络。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不修改扩展权限/配对/ID，不自动部署；不把09d真实加载验收移进本卡。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
