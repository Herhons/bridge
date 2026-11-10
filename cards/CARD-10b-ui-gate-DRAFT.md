# CARD-10b UI 脚本规范与安全门禁 — DRAFT（10a 验收后再施工；方法有变，施工前单独会审，ChatGPT 建议）

来源同 CARD-10a。冲突取更严一侧，已注明提议方。

1. *_ui.py 命名（冲突：Gemini 主张一次性改名；Grok、ChatGPT 主张新文件+旧名薄转发）
   采用：新 *_ui.py 为唯一实现 + 旧名薄转发，并加 Gemini 的约束（更严的合集）：旧名只做参数转发、不得保留第二份实现（ChatGPT）；旧名触发 DeprecationWarning 并注释 # DEPRECATED: use xxx_ui.py（Grok）；src\ 与 tools\ 内禁止引用旧名（Gemini）；docs\ui_scripts.md 写明两张卡周期后删除转发（Gemini）。兼容测试：新旧入口同参数时 stdout/stderr/退出码一致，--help/--list 正常（ChatGPT）。
2. @ui_entry 装饰器（Gemini 提出，三家同意 AST 只查结构）
   - *_ui.py 的入口函数必须挂 @ui_entry(require_lock=True, require_tier=True)；AST（BR001/BR002）只查有没有挂。
   - 运行时顺序固定（ChatGPT，防竞态）：拿 runs\ui.lock（跨进程）→ 确认锁有效 → 用户活动检测 → 校验目标窗口与焦点 → 立即合成输入 → 完成后释放。用户活动时停下等待，不抢焦点；检查失败不得退化成坐标点击。--help/--list 不做空闲检测。锁覆盖 choose → 读回 → use 全链路。
3. 档位回执（ChatGPT，最严）：回执绑定端点、目标档位、实际读回档位、choose 结果、窗口 hwnd/tab、任务 id、创建时间与过期时间、单次使用标记。use 发送前重新读 UIA 实际档位，与回执不一致就停止发送（沿用现有退出码 5）。
4. 唯一输入出口（ChatGPT）：新增 src\qi\ui_input.py，全仓只有它能调用 pyautogui/SendInput/keybd_event/mouse_event/PostMessage/剪贴板写入/uiautomation 的 Click/SendKeys；其他模块经它调用。BR004 = AST 导入 + 调用点扫描（含别名、getattr 字符串，Grok）。只读 UIA 封装放 src\qi\uia_read.py（Gemini）。
5. 不设通用 --i-know（ChatGPT，最严；Grok 的备选是严格记录后允许，未采用）。BR003：可执行代码中禁止定义或传入 --force/--i-know；确有特殊操作时单独定义用途明确的参数，并且不能绕过锁、活动检测、档位校验。现有 --force 参数的移除需保持退出码语义（改为拒绝并提示）。
6. 「不改业务行为」改写为（ChatGPT）：不改任务内容、选模逻辑、发送目标和业务结果；允许增加明确的前置安全门禁，门禁不满足时用已有失败码退出。
完成标准（草）：全量单测通过、≥10a 基线；BR001–BR004 各有违规/合法成对测试；新旧入口兼容测试；真机只冒烟一个 *_ui.py --list（持锁）。