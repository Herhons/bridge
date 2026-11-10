# CARD-WEB2 网页端会审三处 bug：grok 发送键失配、gemini 新对话找不到 / 完成检测超时、grok_use 不能 --read（属第 2 项收发，会审的前置）
## 现象（2026-11-01 10:20–10:47 巡检会审 CARD-05 时）
1. grok_use --new：「没找到 grok_send_on（最高 0.490 / 阈值 0.95）」，stage=input_mismatch，没发出去。巡检只好手动点了发送键。证据 evidence\review_card05_grok_1022.txt、evidence\patrol_1025_review.png。
2. gemini_use --new 连续两次 stage=newchat_not_found（evidence\review_card05_gemini_1020.txt、_1022.txt），复用窗口时也一样。
3. gemini_use --tab 1 发出后 331 秒报 stage=timeout、timeout_kind=no_reply，但回页面用 --read 能读到完整回复（evidence\review_card05_gemini_1035.txt、review_card05_gemini_read_1041.txt）。也就是说回复早就完成了，脚本没检测到。之前 CARD-GEM 修过一次同类问题，仍复现。
4. grok_use.py 没有 --read（只读当前回复）参数，--read 被当成缺 prompt 报错（evidence\review_card05_grok_read_1041.txt）；巡检只能整页抓文本凑合。
5. 当时 Chrome 窗口在左侧竖屏（副屏，坐标为负），可能和模板缩放/坐标有关。
## 初步判断（要施工方核实，不是定论）
- 发送键/新对话按钮模板是在别的缩放或主题下截的，副屏竖屏下比例或颜色不同；纯模板匹配阈值 0.95 太死，可能需要优先走 UIA（按钮名/可访问性名称）再回落模板。
- gemini 完成检测可能还在等某个已经变样的「停止生成」按钮消失，或者回复区节点选错。
## 做什么
1. grok_use：发送键优先用 UIA 找（按名称/角色），找不到再走模板；模板失配时输出里带实际最高分和所在屏幕/缩放。必要时补截新模板（存到资源库原目录，旧模板备份）。
2. gemini_use：修 newchat_not_found（同样 UIA 优先）；修完成检测，让它在回复已完成时能在 30 秒内判定完成；超时前最后再读一次页面，读到回复就当成功返回，不要报 no_reply。
3. grok_use 加 --read [--tab N]：只读当前对话最新回复，不发消息，输出格式和 gemini_use --read 一致。
4. 补离线测试 tests\test_cardweb2.py：UIA 优先/模板回落的选择逻辑、超时前最后一读、grok --read 参数解析。
## 怎样算完成
全量 unittest 全过（现在是 189）；真机：gemini_use --new 和 grok_use --new 各发一条最低档 token 消息（例如「只回复 OK-WEB2」），都要 ok=true 且拿到回复，不能人工点；grok_use --read 能读到刚才的回复。原始输出和退出码存到 evidence\cardweb2_live.txt。测试用完的对话不用删。
## 允许改
src\qi\browser.py、src\qi\gemini.py、src\qi\ui.py、src\qi\screen.py（只限模板匹配相关函数）、src\scripts\gemini_use.py、src\scripts\grok_use.py、资源库里 grok/gemini 的模板图片（改前备份）、tests\test_cardweb2.py（新建）、evidence\cardweb2_live.txt（新建）、CHANGES-20261030.md、progress.json；改之前先备份到 _backup_20261101\。
## 禁止
F:\Bridge\v0.0.8、其他端（codex/dsh/opencode/workbuddy/chatgpt）的 .py、notify_done.py 的代码、注册表/策略、登录、audit\；不合并；不关、不动用户自己开的 Chrome 标签页。
## 最后一步
写 progress.json（card_id=WEB2，status=done/stuck/failed，note 一句原因），再运行 python src\scripts\notify_done.py --card WEB2 --status <done|stuck|failed> --note "<一句原因>"。不管成没成都要通知。