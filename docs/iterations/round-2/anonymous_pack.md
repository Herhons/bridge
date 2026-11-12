## F1（验真：true_read）

A S1 安全原语仍可被绕过
src/qi/safe_ui.py
decide_point(...)
：点之前，目标点必须落在目标窗口的客户区内、命中的窗口属于目标进程、且不是桌面/任务栏那几类窗口。」
「点之前，目标点必须落在目标窗口的客户区内、命中的窗口属于目标进程」
hit_class
，但浏览器渲染的是子窗口/iframe：
WindowFromPoint
Chrome_RenderWidgetHostHWND
，其 PID 与目标一致、类名不在禁用表，于是“进程对、类名对”通过，但点进去的是 iframe 内嵌的第三方控件或页面重排后错位的元素；此外坐标是在判定前算好的，判定与真正
之间若发生窗口移动、DPI 缩放、页面重排，
decide_point
decide_keys
after_foreground
只在“发完”再核，发键瞬间的焦点滞后（UIA 读
focused_role
与真实焦点不同步）仍会打偏。
点进 iframe 内的「删除」控件；判定时窗口正确，
前 200ms 用户拖动窗口，按键落到别处；高 DPI 下
client_rect
是物理坐标，判定恒为 outside 或恒为 ok。
(1) 点位判定改为“先取
ElementFromPoint
/UIA 命中元素，再核该元素的
ControlType
与所属 frame URL 白名单”，不只看 hwnd；(2) 判定与发键合并为原子步：判定后立即
，且发键前再取一次
与坐标，不一致即 ABORT；(3) 坐标统一在判定入口做 DPI 归一（
GetDpiForWindow
换算），单测锁死逻辑/物理换算；(4) iframe 内控件默认禁止坐标点击，只允许 UIA
，读不到就报错而非盲点。
离线夹具构造：同 PID 的 render widget 类、iframe 内元素、DPI=150% 的 client_rect、判定与发键之间坐标变化的序列，断言分别返回
forbidden_frame
dpi_mismatch
stale_point
并 ABORT；统计
调用次数为 0。
B 四阶段账本最小改动面与迁移
submissions.py
只有 draft/submitting/accepted/unknown；会审要求拆成 staged→dispatched→accepted(有证据)→completed，且「点了发送」不得直接记 accepted。」
「「点了发送」不得直接记 accepted」
现有账本把“动作发生”当“状态达成”，
被当终态用于防重发，新增
后旧记录没有该字段，续跑时读旧账本会缺状态或误判。
进程在“已点发送、未读回复”时被杀，重启读到
直接跳过，回复永远不归档；或旧记录缺
导致状态机非法跃迁异常。
只加字段与合法跃迁表（
dispatched→unknown
的写入点从“点击后”移到“读到回复且
prompt_hash
匹配后”；旧记录迁移用一次性脚本把
且无回复证据的降为
，迁移脚本本身带 dry-run 与计数报告，不删记录。防重发判定改为“
及以后只读不重发”，不再单看
离线测试：构造旧格式账本（仅 accepted/unknown），跑迁移断言计数与降级正确；状态机单测断言
staged→accepted
dispatched→completed
非法；续跑夹具断言
记录只读不重发且会继续读回复。
C 会话锚点接线点与判定顺序
src/qi/session_anchor.py
_ask_upload_body
，没有先读锚点。」
「不一致且已 accepted → session_lost（
，保留账本交人工）」
resolve()
的判定顺序正确，但接线点选在
太靠后：发送前才判，若此时已
而无法回跳；而“回跳”需要锚点 URL 在
阶段就可用，且回跳动作本身要持
__use.py
之后、发送之前，
session_lost
，本轮直接失败但账本已
，下一轮重试又撞
navigate
在持锁下回跳锚点 URL 并复核
且漂移 → 直接
anchor is None
new_allowed
）时可达，并用单测断言其余分支不可达。
前置漂移，断言回跳只发一次导航、
、锁在回跳期间被持有；静态扫描断言
D 驱动迁移第一步
src/qi/drivers/__init__.py
SiteDriver
协议（ensure_session/choose_model/upload/submit/read_reply）」
「39 个 qi 模块 + 20 个脚本收敛成「总控 + 各端驱动」的第一步该做什么、不该做什么。」
若第一步就按协议重写四端驱动，等于同时改协议、改实现、改发送栈，回归面覆盖全部四端；而 §13 又禁止“借机重构与编排无关的发送语义”，第二套发送栈风险最高。
迁移中某端驱动未接完，
tools/sisyphus.py
已按新协议调用，发送走到半新半旧两条栈，重复点击/重复发送回归。
第一步只做“薄适配器”：为四端各写一个
*_use.py
的入口函数，不改其内部；
抽成单例被适配器共享，但先不替换各端内部调用。不抽：发送栈内部、清草稿、收卷逻辑（这些留到第二步按端逐个替换）。验收标准是“适配器路径与原路径发送序列字节级一致”，用录制回放夹具锁死，确保没有第二套栈。
离线回放：录制现有
chatgpt_use
一次完整发送的动作序列，跑适配器路径，断言两次序列一致、点击次数相同；静态断言
drivers/*
不 import 坐标点击 API。
E S2 回声过滤边界
tools/iterate.py::_drop_prompt_echo
「与提问重合且长度 >= 8 的块按回声丢掉；以「可访问」开头的抬头保留」
「与提问重合且长度 >= 8 的块按回声丢掉」
按“重合长度 ≥8”丢块会误伤：回复中逐字引用提问里的规格原句（§5.2 要求逐字引句）恰好与提问重合，会被当回声丢掉，导致验真找不到引句；反之提问里短句被回复复述不足 8 字则漏丢。
某家按合同逐字抄「「点了发送」不得直接记 accepted」作依据，该块与提问重合被丢，验真判未读，整条意见作废。
回声判定改为“块与提问的编辑距离/最长公共子串占比 > 阈值且该块不是引号包裹的引用块”，并保留“以「」或 > 开头的块”不丢；丢弃动作写日志（块哈希+原因）进归档，便于复核误伤。
夹具构造“回复含提问原句作引号引用”“回复含提问原句无引号”“回复含短复述”三例，断言前两例保留、第三例丢弃，且日志含块哈希。
F S4 推进器职责边界
条目、按 ID 断点续跑、命中四种硬停即拒；不决策。」
“不决策”方向对，但
的条目由谁写、条目判定（成立/部分成立）由谁做没有在 S4 里锁死；若
sisyphus.py
读到的 plan 含未经 §10 核实的条目，它会照跑，等于把决策偷偷放回推进器。
匿名汇总的「汇总者新增」被直接写进 plan 而未经核实，
按 ID 执行，绕过闸门。
入口加校验：每个 plan 条目必须带
（成立/部分成立/…）与
evidence
（文件:行号或章节），缺失或
为“待核实”即拒跑并写 STOP；续跑断点以条目 ID + verdict 为键，不以汇总文本为键。
夹具给“缺 verdict 的 plan 条目”“verdict=不成立但被列为施工项”两例，断言
拒跑并产出 STOP 说明；正常 plan 续跑断言从正确 ID 续起、不重复已 completed 条目。
本回答由 AI 生成，内容仅供参考，请仔细甄别
内容由 AI 生成，请仔细甄别
附件里那行关键内容是什么？只回答那一行，不要解释。
附件是自迭代流程规格（docs/self_iteration_spec.md）。请审这份规格本身：首行写 可访问=是 或 可访问=否；给 5-8 条意见，按收益排序；每条必须先写规格章节号，再用「」逐字抄一句原文作依据；然后写清为什么是问题、会怎样失败、怎么改、没有真机怎么验收。不要泛泛而谈，不要复述提问，2000 字内。
附件 material.md 就是规格全文，请**只依据附件**审这份规格，不要再去仓库或任何路径找文件。首行写 可访问=是（或 可访问=否，仅当你读不到附件时）。给 6 条意见，按收益排序；每条先写章节号（如 §5.3），再用「」逐字抄一句附件原文作依据；然后写清为什么是问题、会怎样失败、怎么改、没有真机怎么验收。不要复述提问，不要泛泛而谈，2000 字内。
附件 material.md = 规格全文 + 代码库现状 + 已知真机问题。只依据附件评审，不要去找仓库或路径。首行写 可访问=是 或 可访问=否。给 6 条意见，按收益排序；每条严格五段：①章节号或 文件:行号；②「逐字抄附件原文一句」（不许用「」给自己的话加引号）；③问题；④会在什么情况下失败；⑤具体怎么改；⑥没有真机时用什么离线测试验收。必须覆盖五个方向：A 稳定——「屏幕坐标点击+全局按键」如何结构性防误伤桌面/其他窗口；B 架构——一个总控脚本 + 各家 use/choose 模块，给出模块边界、统一接口、迁移顺序；C 会话回归——如何判断是否还在本轮对话、不在时怎么找回去；D 重复点击与「拿不到回复就新开对话」的共同根因与一次性收口；E 推进器「西西弗斯」只负责交接与续跑、按 某端 汇总的计划推进，这个分工是否合理。2500 字内。

## F2（验真：true_read）

可访问=是【B. 四阶段账本最小改动面与旧记录迁移】① 三、剩余待办 1（及 submissions.py）② 「现在 submissions.py 只有 draft/submitting/accepted/unknown；会审要求拆成 staged→dispatched→accepted(有证据)→completed，且「点了发送」不得直接记 accepted。」③ 问题：旧账本把「发出动作（点击/回车）」与「端已接收」混为 accepted，断电或闪退时无法区分「未点出去」和「已点但未确认上屏」；直接改枚举又会破坏存量账本的防重发判断。④ 失败场景：在 _ask_upload_body 点下发送按钮瞬间超时或进程被杀，旧逻辑若提前记 accepted 则丢提问（永远只读空气），若停在 submitting 重跑又会二次点击造成同题双发。⑤ 具体怎么改：在 submissions.py 将状态机定为 staged（输入框/附件就绪）→ dispatched（已触发点击/回车，写下时间戳但未验真）→ accepted（拿到端上屏证据：URL 生成会话 ID、用户气泡出现或 composer 清空）→ completed（读完有效回复），保留 unknown 作异常兜底。做惰性读取迁移（不动磁盘旧文件）：from_dict 读入旧记录时，draft 映射为 staged；submitting 映射为 dispatched；旧 accepted 若带回复路径则映射为 completed，否则保为 accepted。防重发规则改为：accepted/completed 只读不发；dispatched 先走页面回读验真，有上屏证据则升为 accepted，无证据则报错停机交人工，禁止自动重发（遵守 §15 不自动降级重发）。⑥ 离线验收：单测喂入含 draft/submitting/accepted 的旧 JSON 夹具断言迁移映射无损；构造「仅调用 mark_dispatched 未传 evidence 即调 mark_accepted」断言抛 ValueError；断言 dispatched 状态重入时返回 VERIFY_ONLY 而非 ALLOW_SEND。【C. 会话锚点接进发送路径的接线点与判定顺序】① 二、S3 src/qi/session_anchor.py② 「尚未接线：_ask_upload_body 仍在发送前直接 start_new_chat，没有先读锚点。」③ 问题：resolve() 末尾漏了「有锚点、URL 不匹配、且未 accepted」的返回分支（会隐式返回 None）；且 _ask_upload_body 无脑调 start_new_chat()，在超时重跑或页面漂移时会直接冲掉本轮会话。④ 失败场景：某席提问刚 accepted 正在生成，页面因重定向或焦点误触跳回首页；重跑进入 _ask_upload_body 直接 start_new_chat() 新开对话再发一遍，违反 §7.1 防重发并污染双会话。⑤ 具体怎么改：先补齐 session_anchor.py::resolve() 末尾分支：当 bool(anchor.url) 且 not anchor.accepted 时返回 {"action": "navigate", "url": anchor.url, "reason": "drifted_before_accepted"}，若 not anchor.url 则返回 new_allowed。接线点选在 tools/iterate.py::_ask_upload_body 内部（已在 __use.py 持 runs/ui.lock 串行锁之后，不破坏现有锁模型），替换原 start_new_chat() 调用，严格按四步判定：1) 读落盘锚点调 resolve(anchor, current_url)；2) stay 则跳过新建，若账本已 accepted 直接跳到读回复；3) navigate 则浏览器跳回 anchor.url，重验 URL 匹配后再继续填充发送，回跳仍不匹配则报 SESSION_LOST；4) new_allowed 才调 start_new_chat()，并在拿到新会话 URL（或发完产生 session ID）及账本升为 accepted 时，分两次原子落盘更新锚点；5) session_lost 立即中止当前席、写错误码 SESSION_LOST 退出，绝不调 start_new_chat()。⑥ 离线验收：单测覆盖 resolve() 四种返回（含补齐的 navigate 分支）；用假浏览器驱动测 _ask_upload_body：当 anchor.accepted=True 且 current_url 漂移时，断言 start_new_chat 调用次数为 0 且返回 SESSION_LOST；当 accepted=False 且漂移时，断言先调用 navigate(anchor.url)。【A. S1 安全原语绕过漏洞：DPI、重排、焦点滞后与同进程弹窗/iframe】① 二、S1 src/qi/safe_ui.py② 「decide_point(...)：点之前，目标点必须落在目标窗口的客户区内、命中的窗口属于目标进程、且不是桌面/任务栏那几类窗口。」③ 问题：decide_point 和 decide_keys 存在四处物理绕过：1) 只验 hit_pid == target_pid，未区分同进程的顶层窗口/原生文件框或内嵌跨域 iframe（如登录框、广告层、扩展弹窗全属同一浏览器 PID）；2) 坐标未锁 DPI 缩放比与窗口 rect 快照，计算点位到点击之间若发生布局重排（下载栏弹出、窗口缩放），点位就会偏移；3) decide_keys 只校验瞬时 focused_role，但点击 composer 后 UIA 焦点树更新滞后（常达 50–200ms），或焦点落在浏览器地址栏/搜索框（同样是 Edit 角色），此时发 Ctrl+A/Delete 会清空地址栏或打到页面 body 触发快捷键。④ 失败场景：在 _clear_composer 点击输入框后立即发 Ctrl+A + Delete，因焦点实际上还停在浏览器外框或刚弹出的同进程「恢复页面」气泡上，或因 150% DPI 虚拟化导致坐标越界点中网页侧边栏「删除会话」按钮，直接删掉历史记录。⑤ 具体怎么改：1) decide_point 增加参数 hit_top_hwnd、target_hwnd、expected_rect、current_rect、dpi_scale：要求 hit_top_hwnd == target_hwnd（拦截同 PID 的非目标弹窗/对话框），且 expected_rect == current_rect（防重排/移动）、dpi_scale 与取点时一致（否則返回 layout_or_dpi_shifted）；2) decide_keys 收紧：当 is_dangerous(keys) 为真时，除 focused_role in {"Edit", "Document"} 外，新增参数 focused_is_composer: bool（校验 UIA 焦点控件的 bounding_rect 与 composer 区域相交，排除地址栏/搜索框及无关 iframe）和 focus_stable_ms: int >= 80（要求连续两次采样焦点 HWND 与 role 一致，消除焦点滞后）。⑥ 离线验收：在 test_safe_ui.py 构造四组用例：同 pid 不同 top_hwnd、点前 current_rect 偏移 2px、焦点为 Edit 但 focused_is_composer=False（模拟地址栏）、focus_stable_ms=10，断言全部返回 PointVerdict(False, ...)。【D. 39 模块 + 20 脚本的驱动迁移第一步：门面包裹而非重写】① 二、S4 src/qi/drivers/__init__.py（及三、剩余待办 3）② 「只定义了 SiteDriver 协议（ensure_session/choose_model/upload/submit/read_reply）；」③ 问题：39 个 qi 模块与 20 个脚本散落各端分支，若第一步直接重写各端 DOM/UIA 逻辑进 drivers/，会瞬间制造出第二套发送栈，违反 §13「不要再复制一套发送栈；禁止借机重构与编排无关的发送语义」。④ 失败场景：新写 drivers/某端.py 时漏掉了旧 live_job.py 里的剪贴板保存恢复或 tier_guard 900s 超时下限，导致 Round 0 真机跑出超时或冲掉用户剪贴板。⑤ 具体怎么改：第一步只做「薄门面（Facade）收口 + 编排层解耦」：1) 抽什么：新增 src/qi/drivers/legacy_adapter.py，实现 SiteDriver 五个方法，内部 100% 委托调用现有的 session_anchor、workbuddy_choose、upload.py、submissions.py 与 live_job.py，并在此门面内统一卡死 S1（safe_ui）、S3（锚点判定）和 B（四阶段账本流转）；让 tools/iterate.py 只依赖 SiteDriver 接口；2) 不抽什么：本轮绝不搬移或重写 39 个模块里的底层坐标计算、剪贴板键入和 20 个独立 CLI 脚本的入口参数；3) 防第二套发送栈：在 legacy_adapter.py 外禁止任何新脚本直接 import 底层按键/点击函数，由单测扫描 AST 锁死。⑥ 离线验收：写 AST 静态检查单测：断言 tools/iterate.py 与 tools/sisyphus.py 不直接调用 start_new_chat 或裸发键函数，只调 SiteDriver；用 FakeDriver 跑通干跑管道。【E. S2 回声过滤边界：长公共子串误杀合规引用意见】① 二、S2 tools/iterate.py::_drop_prompt_echo② 「# 与提问重合且长度 >= 8 的块按回声丢掉；以「可访问」开头的抬头保留」③ 问题：按「与 prompt 重合且长度 ≥ 8」丢弃块过于粗暴。Round 0 和代码轮的分段粘贴（paste）模式把规格或源码装进了 prompt（§8），而合同要求评审「每条必须引用本规格的章节号，并附一句从规格里逐字抄出的原句」（§5.2 / §7.2）。这些逐字引句必然与 prompt 重合且远超 8 个字符。④ 失败场景：四审严格按合同给出逐字引句，_drop_prompt_echo 把含引句的段落整块当成回声删掉，导致送进 quote_check.py 的回复丢失全部引文，命中数归 0，真读回复被误判为「未读」作废（触发 §12.B.1 假停机）。⑤ 具体怎么改：收紧 _drop_prompt_echo 边界：1) 只在回复文本的前序连续块（出现第一条正式意见编号 1. / ① 或首个代码块之前）执行回声剥离，一旦进入正文意见区立即停止剥离；2) 即使在前序区，单块必须满足「去空白后与 prompt 某段完全相等，或整块 85% 以上字符属于 prompt 的连续子串且长度 ≥ 40」才丢弃；3) 任何含引用标记（> 、「」、文件:行号、§）的块一律保留。⑥ 离线验收：单测构造夹具：将 material.md 作为 prompt，回复正文含 6 条带 20 字逐字引句的意见，断言经 _drop_prompt_echo 处理后 6 条引句 100% 保留，且开头完整复读的提问导语被干净剥除。【F. S4 推进器 sisyphus.py 职责边界：缺前置闸门核验与回归单测闭环】① 二、S4 src/qi/drivers/__init__.py + tools/sisyphus.py（及 §10、§12.B.3/B.6）② 「tools/sisyphus.py 读 plan.md 条目、按 ID 断点续跑、命中四种硬停即拒；不决策。」③ 问题：sisyphus.py 仅按 plan.md ID 续跑并防四种硬停，但缺少 §4/§10/§12 要求的三个硬性执行边界：1) 未校验同目录下 verification.md 是否存在且对应条目判定为「成立 / 部分成立」（防无核实盲修）；2) 未拦截「修改 docs/self_iteration_spec.md 合同条款却无 decisions.md 授权」（§10 明确规格文本本身仍按 §5.4 须批准）；3) 单条施工后未强制绑定「跑离线单测 → 失败则一次回滚+一次重做 → 仍红则写 STOP.md（§12.B.6）」及「同一张计划连续推 3 次无进展即停（§12.B.3）」的计数器。④ 失败场景：plan.md 混入一条「待真机」或未核实条目，或施工改坏了单测，sisyphus.py 断点续跑无限重试超过 3 次仍不写 STOP.md，或未经授权直接篡改了 self_iteration_spec.md。⑤ 具体怎么改：在 sisyphus.py 不做业务决策的前提下补齐三道机械闸门：1) 入口校验：执行任一 plan ID 前，核对 verification.md 中该 ID 状态必须属于 {"成立", "部分成立"}，若改动触及 docs/self_iteration_spec.md 则检查 decisions.md 存在 Herhons 批准记录，否则拒跑；2) 尝试计数：在状态文件记录每个 plan_hash 的推进次数 attempt_count，>= 3 无进展立即触发 §12.B.3 写 STOP.md；3) 单测与回滚闭环：每条 ID 施工完毕必须调用离线单测命令并记录退出码与 Ran / OK，若非 0 则执行 git checkout -- . 回滚并允许重做 1 次，二次仍红立即触发 §12.B.6 停机。⑥ 离线验收：单测构造三组夹具：1) 无 verification.md 或状态为「待真机」的 plan.md；2) 触及 self_iteration_spec.md 但无 decisions.md；3) 模拟单测连续 2 次返回 exit code 1 或同一 plan 连续跑 3 次；断言 sisyphus.py 均拒绝继续并生成写出对应条款编号的 STOP.md。
