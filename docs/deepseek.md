# deepseek 网页版（v0.0.9 第一对脚本）

按 v0.0.8 的拆法：`_choose` 只做选择，`_use` 只发提问。全流程**找图点击**，不读控件树。

```
src/qi/screen.py                截屏 / 找图 / 点击 / 输入 / 剪贴板
src/qi/library.py               资源库（缺图先报，exit 3）
src/scripts/deepseek_choose.py  深度思考 / 智能搜索
src/scripts/deepseek_use.py     提问 → 等生成 → 点复制 → 读剪贴板
tools/snip.py                   全屏截图 + 按区域裁模板
```

## 跑法

```bash
PY=C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe

$PY src/scripts/deepseek_choose.py --list              # 读两个开关
$PY src/scripts/deepseek_choose.py --think on
$PY src/scripts/deepseek_use.py "只回复两个字：收到"     # --new（默认）先点新对话
$PY src/scripts/deepseek_use.py "继续说说" --continue
```

前提：DeepSeek 页在屏幕上可见、没被别的窗口挡住（截的是屏幕，不是窗口）。

## 资源库（`src/assets/library/`）

| 名字 | 截的是什么 | 尺寸（本机实测） |
|---|---|---|
| `ds_newchat` | 侧栏「开启新对话」整颗按钮 | 238×38 |
| `ds_composer` | 输入框右下角的**麦克风图标**（定位锚点） | 22×24 |
| `ds_send_on` | 发送键**可用态**（深蓝圆+白箭头） | 38×38 |
| `ds_copy` | 回答动作条第一个图标（复制） | 22×22 |
| `ds_stop` | 生成中的停止键（深蓝圆+白方块） | 34×34 |
| `ds_think` | 「深度思考」胶囊（截"关"的样子，两态都能定位） | 92×34 |
| `ds_search` | 「智能搜索」胶囊 | 92×34 |

截法：

```bash
$PY tools/snip.py --shot                                        # 先看坐标
$PY tools/snip.py --save ds_copy --region 472,451,22,22
```

## 实测：模板分数不够用，必须看颜色

这是这一轮最值钱的三条发现，都是踩出来的：

1. **归一化相关对亮度不敏感。** 发送键「深蓝可点」和「浅蓝不可点」在 `TM_CCOEFF_NORMED` 下都是 **1.000**。
   → 能不能点，只能量颜色：可点 `B-R≈148`，不可点 `B-R≈59`，阈值取 100。
2. **开关两态也分不开。** 同一位置 `on 0.960 / off 0.996`，按分数会判错。
   → 改量 B-R：关（浅蓝描边）≈37，开（白底黑字）≈0.6，阈值 15/25。
   → 而且**读之前必须把鼠标移开**，悬停会把胶囊变灰底，读出来像"开"。
3. **输入框位置随状态变。** 新对话欢迎页在屏幕**中间**，一旦发出消息就移到**底部**。
   → 一律用锚点（麦克风）相对偏移，不写死坐标：`COMPOSER_OFFSET = (-300, -65)`。

## 流程里的几道闸

- **写没写进去**：输入后用「发送键是否变成深蓝」判，不靠像素差分。
- **生成结束**：`ds_stop` 出现再消失。没有这张图就退回"等复制键"。
- **复制哪一条**：`find_all(ds_copy)` 取 **y 最大**（回答在提问下面）。
- **复制到提问就是失败**：`reply == prompt` 直接报 `copied_prompt`，不当成功。
- **发送键找不到就不发**，绝不按 Enter 兜底。

## 已验证（2026-10-29，本机副屏 1440×2560）

| 用例 | 结果 | 耗时 |
|---|---|---|
| `--new "只回复两个字：收到"` | ok，reply=`收到`，generating_seen=true，copies=2 | 4.9s |
| `--continue "用一句话说明你是什么模型"` | ok，取到最新那条回答 | 4.5s |
| `choose --list` | think=off(37.0) / search=off(38.7) | <1s |
| `choose --think on` → `--list` → `--think off` | on 验证通过(0.6)，恢复 off(37.0) | ~3s |

找图单次约 230ms（4000×2560 虚拟桌面全屏）。

## 没验 / 已知限制

- 只在**浅色主题 + 100% 缩放 + 本机这块副屏**上验过。换主题或改缩放，模板全部要重截。
- 长回答把复制键顶出视口时找不到（没有滚动兜底）。
- 桌面端（codex / dsh / workbuddy / opencode）还没做，模板与网页端不通用。
- 页面被遮挡时找不到任何东西——必须先切到前台。
