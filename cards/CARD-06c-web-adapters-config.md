# CARD-06c 网页端适配与配置：ChatGPT 聊天选档 / Work 无选择器 / Gemini 新对话 / GLM 验收档（排队，06b 后发）
来源：06b r1 拆出（1/2/3/7/11/12），三家会审意见见 evidence\review_card06b_*.txt。
1. chatgpt_choose 支持聊天模式选档：芯片（Medium/中/High/高，悬停「思考强度」）→ 滑杆层（即时/中/高）→ 点标题「高 ›」进模型列表（GPT-6 ✓ / GPT-5.6 Sol）。聊天最高档 = GPT-6 + 高；--list 读回两项，中英文都认。输入框空/未聚焦时先点输入框再定位芯片。证据 evidence\chatgpt_chat_gpt6_high_0548.png。
2. tiers.json 给 chatgpt 增加 chat 模式条目（model: gpt6/gpt56；effort: instant/mid/high；review/accept = gpt6+high），回执记录 writer=chat；use 按 writer 取对应白名单（本轮 chatgpt_use --purpose review 因表里没有 chat 被 purpose_mismatch 拒发，门禁本身正确）。
3. Work 模式找不到模型入口：返回 stage=no_model_selector，fail-closed，不沿用上次档位。
4. 不用 Ctrl+Shift+M（被 Chrome 截成个人资料菜单）；改点芯片，点前唯一性校验并校验与麦克风/发送键距离，点后复核弹层出现。
5. gemini_choose --new 的 newchat_not_found：侧栏折叠/展开两态都能找到新对话。
6. GLM 验收档：三家一致反对把 glm-5.3-flash-free 塞进 accept（会破坏 accept=最高档语义）。改为 tiers.json 的 opencode 新增独立用途 accept_lite（白名单仅 glm-5.3-flash-free），opencode_choose 能选并读回它，opencode_use --purpose accept_lite 可发；验收结论标「PASS(GLM-lite)」，与 accept 区分。
7. Grok 菜单项模板 grok_item_* 已于 05:42 由巡检按当前缩放重截（旧图备份 _backup_20261102\library_0542\），本卡加单测确认 grok_choose 用新图能选中。
完成标准：单测全过 + 新测；真机 chatgpt_choose --writer chat --effort high --model gpt6 --list 读回 GPT-6/高；opencode_choose 选 glm 并 opencode_use --purpose accept_lite 收发成功；证据 evidence\card06c_live.txt。禁止同 06b。
