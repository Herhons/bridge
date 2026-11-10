# DRAFT — WEB2-evidence

状态：DRAFT，仅供后续评审；不是正式卡，未发送，未取得独立验收。
顺序：SUMMARY §三第 8 组。来源：`F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\SUMMARY.txt:45`。


## 问题与文件行号证据

现有功能/26测通过，但Grok最低档fast→--new原始读回不足；不等于已证实用了高档或功能失败。

- `F:\Bridge\v0.0.9\evidence\codex_recheck_20261106\CARD-WEB2.txt:28`：`evidence/cardweb2_live.txt:11-24,30-34` 缺同轮choose；`card06b_live.txt:313` 明说expert切fast未跑。

以上沿用已完成 recheck 的事实与结论，不重新评判16张卡；源代码行号为该报告记录的行号。

## 必需修改

1. 优先检索既有原始记录，能对应同轮fast读回→--new→唯一token回复→--read与退出码才补索引；不得拼接不同用途/轮次伪成一次实测。
2. 找不到则明确pending evidence。确需新实测，留给另一个明确允许真机发送的任务，并使用新唯一token，不重放旧提问。
3. Gemini后续card07已有flash_lite补证须正确引用，不无端重复发送。

## 验收测试（离线，含入口与负例）

- 证据入口清单核对命令、时间、用途/档位、token关联、reply/submission及EXIT；缺任一环则不宣称完整最低档验收。
- 排除expert/accept成功证据、纯文件存在、模板图片与mock日志作为fast真机证据。
- 本任务仅离线检索/草稿，不运行任何use或浏览器，不要求制造新测试。

实现后在独立 WIP 执行 `py -3.10 -X utf8 -B -m unittest discover -s tests -v`，保存完整输出、Ran/OK/EXIT、相对复制基线的差异及测试隔离记录。自测不等于后续其他厂商独立验收。

## 不在范围内

不修改收发代码、不推翻WEB2功能层通过事实、不在本任务补发。

本次统一边界：只允许在 `F:\Bridge\v0.0.9_codex_wip\` 实施代码/测试；原 `v0.0.9` 仅可在本草稿目录创建新文件，不改任何既有文件、正式卡、PLAN或progress。不运行真实UI发送、浏览器、WorkBuddy或外部通知；不调用通知收尾；不覆盖历史证据。不因旧卡含真机/状态更新条款而越过本次限制。
