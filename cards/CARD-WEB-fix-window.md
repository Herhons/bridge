# CARD-WEB-fix 网页端新窗口绑定失败 + 目标文档数量为 0（属第 2 项收发，CARD-05 会审的前置）
## 现象（2026-11-01 06:0x 巡检）
1. gemini_use（两次）、grok_use（一次）都报 stage=no_window、submission=not_sent。日志里先打印「Chrome 在播音频: True」，然后「新窗口 hwnd=…」，接着就绑不上窗口。留下了 2 个 Gemini、1 个 Grok 的 Chrome 窗口没关。证据：evidence\review_card05_gemini.txt、evidence\review_card05_grok.txt、runs\20261101.jsonl。
2. opencode_use --new 报「目标文档数量为 0，无法绑定」（src\qi\ui.py 第 90 行）；workbuddy_use --list 也报目标文档 0，但它发送正常。
## 巡检已核实的事实
- 本机 Python 没装 pycaw（import pycaw 报 ModuleNotFoundError）。src\qi\audio.py 设计成没装时返回 True（保守），所以 chrome_playing_audio() 现在永远是 True，网页端每次都会新开窗口。电脑当时空闲 3 小时以上，并没有人在放视频。
## 初步判断（要施工方核实，不是定论）
- 新开窗口后，绑定逻辑可能还在找旧窗口，或者新窗口还没加载完就去找文档节点，所以报 no_window。
- 「目标文档数量为 0」可能是同一类问题：控件树里取文档节点的条件太死，或者等待时间太短。
## 做什么
1. 新窗口路径修好：新开 Chrome 窗口后要能等到页面加载完并绑定成功；绑定失败要把自己开的窗口关掉，不留垃圾窗口。
2. pycaw 缺失的处理：把 pycaw 写进 requirements.txt 并 pip 安装（安装命令和输出记到 evidence）；同时缺 pycaw 时要在输出里明确提示「音频检测不可用」，不要静默当成在播放。
3. 修 ui.py 的目标文档查找，让 opencode_use --new 和 workbuddy_use --list 能找到文档；失败时输出里带上实际找到的窗口/文档信息方便排查。
4. 补离线测试 tests\test_cardweb.py，覆盖：缺 pycaw 的提示、新窗口绑定失败要关窗、文档数为 0 时的报错信息。
## 怎样算完成
全量 unittest 全过；真机：gemini_use 和 grok_use 各发一条最低档 token 消息拿到回复、opencode_use --new 发一条最低档消息拿到回复、workbuddy_use --list 读到文档数大于 0。原始输出和退出码存到 evidence\cardweb_live.txt。
## 允许改
src\qi\browser.py、src\qi\audio.py、src\qi\ui.py、src\scripts\gemini_use.py、grok_use.py、opencode_use.py（只为接上修好的函数）、tests\test_cardweb.py（新建）、requirements.txt、CHANGES-20261030.md、progress.json；改之前先备份到 _backup_20261101\。
## 禁止碰
F:\Bridge\v0.0.8、其他端的 .py、注册表/策略、登录、audit\；不合并。
## 最后一步
写 progress.json，并运行 notify_done.py --card WEB --status <done|stuck|failed> --note "<一句原因>"。