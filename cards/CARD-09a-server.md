# CARD-09a 本地后端 server（第 6 项拆卡 1/4，已会审）

来源：CARD-09-DRAFT 会审三家——Gemini(argon) REVISE、Grok(expert) APPROVE(附缺口)、ChatGPT(GPT-6 高) REVISE，原文在 evidence\card09_review\。合并结论：拆成 09a 后端 / 09b 插件移植+配对 / 09c 工作流面板 / 09d 第 8 项实机。本卡只做 09a，全部离线可测，不碰插件、不碰浏览器。
说明：ChatGPT 提到的 SQLite bridge_state.db、core/extension_server.py 属 v0.0.4 旧架构，v0.0.9 不存在，不采用；v0.0.9 唯一状态源仍是 progress.json。

1. 新建 src\qi\server.py + src\scripts\bridge_server.py（start / stop / status）。标准库 http.server 的 ThreadingHTTPServer，只绑 127.0.0.1，固定默认端口 18765（可在 docs\bridge_server.json 改）；端口被占或已有实例在跑 → 明确报错退出码，不抢占。单实例。
2. 鉴权：token 首次启动生成后持久化（%LOCALAPPDATA%\Bridge\v009\server.json，含 port 与 token），之后重启沿用，不每次重生成；提供 rotate 子命令。所有接口除 GET /health 外须带 Authorization: Bearer <token>，否则 401；Host 必须是 127.0.0.1:<端口>；Origin 只接受 chrome-extension:// 前缀或无 Origin（本机脚本）。token 不进 URL、不进日志。
3. CORS：实现 do_OPTIONS 预检，Access-Control-Allow-Origin 回显合法的 chrome-extension 来源，允许 Authorization、Content-Type。
4. 接口：GET /health；GET /endpoints（读 docs\tiers.json）；POST /ask {endpoints[], prompt, purpose} → 立即返回 202 + job_id；GET /jobs/<id> → 每端 状态/回复/错误码/退出码 分开列，某端失败不影响其他端（F3b）；GET /workflow?file=…、POST /workflow。
5. 执行约束：endpoint 只能取 tiers.json 白名单；purpose 必填，只能是 test/review/accept，无默认；每端顺序跑 choose（读回确认，看退出码和 runs\tier 回执）→ use，choose 未确认就不跑 use，该端记失败；子进程用固定脚本路径 + 参数数组，禁止 shell 拼接；每端设总超时、超时杀整棵进程树、输出截断上限；队列上限（如 20 个 job），超了 429。
6. UI 锁：前台只有一个焦点，所以所有端串行执行。用跨进程文件锁（runs\ui.lock，记 pid 与时间），锁覆盖一个端的 choose→读回→use 全程；锁带超时，持锁进程已死则自动释放。服务内 threading.Lock 不够，必须是文件锁，命令行直接跑 *_use 也要尊重同一把锁（只在 server 侧加锁的话，在 CHANGES 里写清限制）。
7. job 记录落盘 runs\jobs\<job_id>.json（原子写），内存只留最近 50 个；服务重启后 GET /jobs 仍能读到已落盘的 job；同一请求带相同 request_id 重复 POST 不重复派发。
8. /workflow：file 只允许 progress.json 或 progress-test.json 这两个 basename（防路径穿越）；POST 只接受合法状态迁移（草稿→已发→完成），非法迁移 409；写入用同目录临时文件 + os.replace，和 notify_done 并发时不损坏（加重试或文件锁）。
9. 离线单测 tests\test_card09a.py：mock 子进程，覆盖 401/错 Host/错 Origin/OPTIONS 预检、端口占用、202+轮询、失败隔离、choose 失败不跑 use、超时杀进程、锁释放（持锁进程死掉）、request_id 去重、重启后读 job、workflow 白名单/非法迁移/并发写。

允许改：src\qi\server.py（新）、src\scripts\bridge_server.py（新）、docs\bridge_server.json（新）、src\qi\ 下锁相关新文件、tests\test_card09a.py（新）、CHANGES 追加、progress.json 本卡状态。若 *_use 要接文件锁，只改入口几行并在 CHANGES 说明。
禁止：动 src\chrome-extension（09b 做）、F:\Bridge\v0.0.8、opencode.json、注册表/策略/扩展/登录；不真发任何 AI；不改其他卡测试预期；不开防火墙端口。
完成标准：py -3.10 -m unittest 全量通过（≥529 + 新测），命令和 Ran/OK 写进 evidence\card09a_unittest.txt；真机只做冒烟：bridge_server start → curl/py 请求 /health 200、无 token /endpoints 401、带 token /endpoints 200 → stop，输出写 evidence\card09a_live.txt（token 打码）。完成后 progress.json 置 09a done，并调用通知脚本。