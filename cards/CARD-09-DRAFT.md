# CARD-09 草稿（第 6 项：插件移植 + 工作流面板）— 会审用

背景：v0.0.8 的 Chrome 插件（src\chrome-extension，MV3，popup+sidePanel，权限只有 storage/sidePanel，CSP connect-src 'none'）是纯界面空壳，没接调度。v0.0.9 已有 src\scripts\*_choose/*_use（UI 驱动各 AI 端，带档位回执 tier_guard、runlog、progress.json、notify_done、workflow_use）。第 8 项 F1–F6 是最终判据（见 PLAN.md）。

方案：
1. 移植：把 v0.0.8 插件只读复制到 v0.0.9\src\chrome-extension（v0.0.8 不改），版本号 0.0.9。
2. 本地后端：新建 src\qi\server.py + src\scripts\bridge_server.py，只绑定 127.0.0.1:<端口>，标准库 http.server，无第三方依赖。启动时生成随机 token 写到 %LOCALAPPDATA% 下文件，插件首次在设置里粘贴 token（不进 chrome.storage.sync，只放 local）。接口：
   - GET /health；GET /endpoints（来自 docs\tiers.json）
   - POST /ask {endpoints[], prompt, purpose} → 每端串行或并发调用 *_choose 读回 + *_use（子进程，沿用现有退出码），返回 job_id
   - GET /jobs/<id> → 各端 状态/回复/错误码，分端显示；某端失败不影响其他端（F3b）
   - GET/POST /workflow → 读写 progress 文件（可指定 progress-test.json，F4），状态 草稿→已发→完成
3. 插件：manifest 加 host_permissions 仅 http://127.0.0.1:<端口>/*，CSP connect-src 同址；popup.js startPreview() 接 /ask，轮询 /jobs；后端未启动 10 秒内红字提示（F5④）。
4. 工作流面板：侧栏新 Tab，显示卡片列表与状态，轮询 /workflow 每 3 秒（外部 notify_done 改动 10 秒内刷新）。
5. 并发：浏览器各端在不同标签页，但前台只有一个焦点；计划后端内部用锁串行操作 UI，"并发"只是逻辑上一起发出、各自等待回复。
6. 测试：tests\test_card09.py 离线测 server（mock 子进程）、token 校验、失败隔离、workflow 读写；node 不保证有，JS 只做语法/资源引用检查脚本。
7. 安全：拒绝非 127.0.0.1 来源、Origin 必须是 chrome-extension://<id>，无 token 401；不改注册表/策略/其他扩展；不自动安装插件（第 8 项 <host> 手动加载 + 登录测试账号）。

请审：方案是否可行、遗漏、风险、拆卡建议。结论第一行写 VERDICT: APPROVE 或 REVISE，然后列要点（≤15 条）。
