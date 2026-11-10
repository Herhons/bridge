# CARD-09b 插件移植 + 配对（第 6 项拆卡 2/4，已会审）

会审：Gemini(argon) REVISE、Grok(expert) REVISE、ChatGPT(GPT-6 高) REVISE，原文 evidence\card09b_review_*.txt。三家一致：架构保留；顺带修的后端/UIA 问题全部拆出（见 CARD-09e）；补 token 边界、Origin 精确匹配、sidePanel 生命周期、静态断言测试。Origin 分歧合并：有 Origin 必须精确等于 chrome-extension://<固定ID>，无 Origin 只放行本机脚本且仍必须带 token（Gemini 指出扩展 GET 可能不带 Origin，ChatGPT 要求拒绝缺失 Origin，取折中并在 CHANGES 写明）。

1. 只读复制 F:\Bridge\v0.0.8\src\chrome-extension 到 v0.0.9\src\chrome-extension（v0.0.8 一个字节都不改，复制前后对 v0.0.8 算 SHA256 写进 evidence\card09b_v008_hash.txt 证明未改）。version 改 0.0.9，name/description 去掉"空壳"字样。
2. manifest：加固定 "key"（只放公钥；生成的私钥 .pem 不进仓库，放 %LOCALAPPDATA%\Bridge\v009\ 并在 .gitignore 排除 *.pem）；由 key 算出扩展 ID 写进 docs\bridge_server.json 的 allowed_extension_id。permissions 只 storage、sidePanel；host_permissions 只 "http://127.0.0.1:18765/*"；CSP connect-src 'self' http://127.0.0.1:18765。
3. 去掉 action.default_popup，新增 background service worker 只做一件事：chrome.sidePanel.setPanelBehavior({openPanelOnActionClick:true})。主界面全部在 sidePanel，不再有 popup 和 sidePanel 双开竞争。
4. 配对：设置区粘贴 token → chrome.storage.local；启动时 storage.local.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"})；不进 sync、不进 console/日志、不注入任何 content script。401 → 红字"请重新配对"。
5. 后端（server.py 小改）：Origin 规则按上面合并结论；token 比较改 hmac.compare_digest；server.json 写入后用 icacls 限当前用户（失败只警告）；/health 只回 {ok,version}。
6. sidePanel 对接：/endpoints 列端点；/ask 带 purpose 必选、request_id；返回 job_id 存 storage.local（支持多个，带创建时间，24h 过期清理）；轮询 /jobs/<id> 单例定时器（重开面板或连点不叠加 setInterval）；面板重开立即恢复未完成 job；后端离线 /health 3 秒超时即红字（≤10s，F5④）；后端重启后 job 404 → 提示"任务已丢失"。
7. 离线测试 tests\test_card09b.py（py 读文件静态断言，不起浏览器）：manifest 合法 MV3、key 存在、host_permissions/CSP 字符串精确、无 default_popup、无 content_scripts、js 里无 storage.sync 与 console 打印 token；后端用 test client 模拟 OPTIONS 预检 + 正确 ID 放行、错 ID 拒、无 Origin 无 token 拒、无 Origin 带 token 放行、compare_digest；js 轮询单例逻辑用正则/结构断言或 node（有就跑，没有就跳过并注明）。
8. evidence\card09b_manual_checklist.md：写清下一张 09d 真机手动加载清单（加载已解压、ID 与 allowed_extension_id 一致、配对、离线红字、关开面板恢复）。本卡不自动安装插件、不开浏览器。

允许改：src\chrome-extension\（新）、src\qi\server.py、docs\bridge_server.json、.gitignore、tests\test_card09b.py（新）、CHANGES 追加、progress.json 本卡状态。
禁止：动 F:\Bridge\v0.0.8（只读复制）、opencode.json、注册表/策略/其他扩展/登录；不真发 AI；不改其他卡测试预期；不开防火墙；不用 --force。
完成标准：py -3.10 -m unittest 全量通过（≥583 + 新测），命令与 Ran/OK 写进 evidence\card09b_unittest.txt；真机只冒烟本卡改动：bridge_server start → 正确 Origin 带 token /endpoints 200、错 Origin 403、无 token 401 → stop，写 evidence\card09b_live.txt（token 打码）。完成后 progress.json 只改本卡相关字段置 09b done（不要整体覆盖 verdicts/queue），再调用通知脚本。
