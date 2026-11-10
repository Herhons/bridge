# BRIDGE 会审工作台 · 0.0.9

Chrome **Manifest V3** 扩展。点工具栏图标直接打开 Chrome 原生**侧栏**（不再有弹窗），
在侧栏里配对本地后端 `127.0.0.1:18765`，提交会审任务、轮询结果。

**不要双击 `sidepanel.html`，也不需要单开网页服务。**需要 Chrome 116 或更新版本。

## 0.0.9 改了什么

- 入口只剩侧栏：去掉 `action.default_popup`，`background.js` 只做一件事
  `chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true})`。
  旧版的 popup 与侧栏会同时打开、各自轮询、抢写 `storage.local`，现在这条路没有了。
- `manifest.json` 带固定 `key`（只放公钥），扩展 ID 因此固定为
  `odjanlclocbikpjlkcfepnfbcejlapkd`；后端 `docs\bridge_server.json`
  的 `allowed_extension_id` 与它一致。**私钥 .pem 不在仓库里**，
  只在本机 `%LOCALAPPDATA%\Bridge\v009\`，且 `.gitignore` 排除 `*.pem`。
- 配对：把 `%LOCALAPPDATA%\Bridge\v009\server.json` 里的 `token` 粘进侧栏配对区。
- 联网范围收紧：只有 `host_permissions: http://127.0.0.1:18765/*`，
  CSP `connect-src 'self' http://127.0.0.1:18765`。

## 加载到 Chrome（本卡不自动安装）

1. 地址栏打开 `chrome://extensions`，开启右上角「开发者模式」。
2. 「加载已解压的扩展程序」，选择 `F:\Bridge\v0.0.9\src\chrome-extension`。
3. 扩展卡片上的 ID 应当是 `odjanlclocbikpjlkcfepnfbcejlapkd`；不是就说明目录选错了
   或 `manifest.json` 的 `key` 被改过。
4. 固定到工具栏，点图标 → 直接开侧栏。
5. 改文件后回扩展管理页点「重新加载」。

## 配对与使用

1. 先在命令行把后端跑起来：`py -3.10 src\scripts\bridge_server.py start`
   （只绑 `127.0.0.1:18765`）。
   - `start` 起的后端是**独立进程**（`DETACHED_PROCESS`，父 job 允许时再加
     `CREATE_BREAKAWAY_FROM_JOB`），关掉终端不会带走它；在任何新终端用
     `bridge_server.py status` 确认（要看到 `running:true` 且 `pid_alive:true`）。
   - 它**不随开机自启、也不自动重启**；崩了/重启机器后再执行一次 `start`。
   - `status` 报 `mismatch:true` 说明 18765 上应答的**不是**本目录的后端
     （最常见：另一份 checkout 占着同一个端口）。先 `stop` 再 `start`。
   - 面板端口写死在 `bridge-api.js`（18765）。若 `%LOCALAPPDATA%\Bridge\v009\server.json`
     里的 `port` 被别的端口覆盖，面板会一直显示「后端离线」——这种情况重新 `start`
     一次（不带 `--port`）即可让 state 回到配置里的 18765，**token 不会变**。
2. 打开 `%LOCALAPPDATA%\Bridge\v009\server.json`，复制 `token` 的值。
3. 侧栏「配对本地后端」里粘贴 → 保存。token 只进 `chrome.storage.local`，
   启动时先 `setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})`，不进 `storage.sync`、
   不打印、不回显、不注入任何 content script。
4. 选席位、填任务、选用途（测试 / 会审 / 验收），点「开始会审」→ `POST /ask`。
   返回的 `job_id` 存进 `storage.local`，侧栏每 3 秒 `GET /jobs/<id>`；
   关掉侧栏再打开会先补一轮，未完成的任务立刻恢复。
5. 任务记录保留 24 小时，过期自动清理。

## 会看到的红字

| 现象 | 含义 |
| --- | --- |
| 顶部「后端离线（3 秒无响应）」 | `/health` 3 秒没回来：后端没起，或端口被占 |
| 配对区「请重新配对」 | 401：token 不对，或后端跑过 `rotate` 需要重新复制 |
| 任务行「任务已丢失」 | `GET /jobs/<id>` 404：后端重启后没有这个 job，重新提交即可 |

## 权限与边界

- 只用 `permissions: ["storage", "sidePanel"]` 与一条 `host_permissions`；
  没有 content script、没有 `chrome.scripting`、不读当前网页和 AI 会话。
- 不调用任何 AI：`/ask` 交给后端，后端再按 `docs\tiers.json` 调 `*_choose.py` / `*_use.py`。
- 附件只显示文件名和大小，不读取、不上传内容。
- 「结论汇总」「施工方」「验收方」仍是本地配置项；本卡不做自动汇总，也不交付施工。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `manifest.json` | MV3 声明、固定 key、最小权限、CSP |
| `background.js` | service worker：只设 `openPanelOnActionClick` |
| `sidepanel.html` | 侧栏唯一界面 |
| `sidepanel.js` | 配对、席位、提交、任务列表、结果渲染 |
| `bridge-api.js` | 后端唯一出口：`/health` `/endpoints` `/ask` `/jobs/<id>` |
| `poll-scheduler.js` | 单例轮询器（`start()` 幂等，不叠加定时器） |
| `styles.css` | 布局与状态色 |
| `assets/` | 本地图标；来源与授权见 `assets/SOURCES.md` |

手动验收清单见 `evidence\card09b_manual_checklist.md`（下张 09d 真机加载用）。
