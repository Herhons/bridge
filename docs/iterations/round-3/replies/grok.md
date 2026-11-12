可访问=是
打开过的材料：附件 material.md 全文（规格 + 当前实现 + 四待决）。
1. A 账本 accepted 永久拦截
① 三、3.1
②「现在的后果：
accepted属于拦截集合，该提问此后永远发不出去，只能人工
submission_clear
。」
③ 问题：accepted 一旦写入即进入 BLOCKING_STATES，页面重置后无自动降级路径，合法重发被锁死。
④ 失败场景：gemini 汇总消息上屏后导航回 /app（0 消息、附件仍挂），账本保持 accepted，同一 prompt 永久拒发，循环卡死。
⑤ 怎么改：降级证据顺序严格如下：① 当前 view 满足 prompt_nodes=0 且 composer_empty=False（或 URL 回到 /app 且消息数 0）；② 记录 scope 与当前 scope 不匹配；③ 无 L3 证据。满足则降为 unknown（不降 draft），写审计日志且只允许一次；pending 仍拦截直至人工或新 purpose。
⑥ 离线测试：夹具账本 accepted + mock view 空消息异 scope → 断言降 unknown 且 pending 仍拒；同 scope 不降；draft 不受影响。
2. B 驱动适配器第一步防第三套栈
① 2.4 与 三、3.2
②「各端驱动只做"原子动作"，不自己重试、不管锁、不写账本。」
③ 问题：SiteDriver 仅协议无人实现，现有 conversation.ask + *_use.py 两套栈，第一步抽错即出现第三套。
④ 失败场景：适配器内含重试/锁/账本逻辑，或新驱动直接调旧栈，行为分叉且后续迁移失控。
⑤ 怎么改：第一步只抽 gemini 已有最稳的 upload-script 为无状态 SiteDriver.upload；总控 sisyphus 独占锁/账本/超时。不抽 choose/ensure 的重试。静态检查：测试扫描禁止新文件同时 import conversation 与 *_use，且 SiteDriver 实现不得出现 reserve/pending/ui.lock。
⑥ 离线测试：AST 扫描断言无第三发送入口；fixture 驱动仅实现 upload，调用时不触碰 ledger。
3. D 剪贴板事务防破坏用户内容
① 三、3.4
②「paste 路径写剪贴板后不做写后校验（harvest 阶段有
clipboard_seq校验）。」
③ 问题：写后无校验，异常时中间状态或用户原内容无法恢复。
④ 失败场景：写时被抢占或进程异常退出，用户剪贴板丢失，且发送内容错误引发 input_mismatch。
⑤ 怎么改：最小事务：写前 snapshot=get()+sha；写入目标；立即读回核对 sha；任何失败（含核对不符）立即 restore snapshot。全程 try/finally，仅本进程持有期间操作。
⑥ 离线测试：mock 剪贴板，写前有用户内容，写入失败或读回不匹配时断言恢复原内容；成功路径断言用户内容最终仍在。
4. C 孤儿锁入口不全
① 2.5 与 三、3.3
②「
uilock.clear_stale()只在iterate.py的 review/summarize 入口调用；sisyphus.py、
*_use.py、submission_clear.py都不调。」
③ 问题：残留锁只在部分入口清理，其他路径直接被挡住。
④ 失败场景：上一轮 iterate 异常留锁，sisyphus next 或
_use 启动被锁卡死，无法续跑。
⑤ 怎么改：clear_stale() 挂到 sisyphus.py status/next 启动时、
_use.py 主入口发送前、submission_clear.py。失败语义：OSError 记 warning 且 fail-closed 拒发并写 STOP（不阻塞进程）。
⑥ 离线测试：创建残留锁文件调用入口断言被清；模拟 clear 抛 OSError 断言不崩溃且有日志。
5. E grok 收卷主路径回归
① 2.3
②「已按「拼接后的整段」定位气泡起止，但收卷主路径仍用旧的单块匹配。」
③ 问题：定位已改拼接，收卷仍单块，含独立 ** 节点时仍可能 prompt_gone。
④ 失败场景：提问被拆 9~10 节点（含 ** 独立节点），定位成功但收卷失败 → timeout 或重复发送。
⑤ 怎么改：收卷主路径同步改为拼接后整段匹配，并过滤独立 ** 节点。
⑥ 离线测试：构造多节点 UIA 树（含独立 **），断言定位+收卷都命中拼接文本；旧单块路径红、新路径绿。
6. F 上传绝对路径与验真分母
① 2.3
②「上传路径必须绝对：文件对话框按自己的当前目录解析，相对路径表现为
dialog_stuck。」
③ 问题：未强制所有上传入口 abs，且验真按结构化条数计时 §5.2 与「第 5.2 节」可能误判。
④ 失败场景：相对路径投喂 dialog_stuck；或章节号归一不全导致真读被判未读。
⑤ 怎么改：所有 upload 前 Path.resolve().absolute()；quote_check 锁定空白归一后「章节号+逐字引句」，并显式认 §5.2 ≡ 第5.2节。
⑥ 离线测试：相对路径输入断言 resolve 绝对；quote_check 夹具含两种章节写法断言均命中。