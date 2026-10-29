# deepseek 网页版（v0.0.9 第一对脚本）

按 v0.0.8 的拆法：`_choose` 只做选择，`_use` 只发提问。区别是全部改成**找图点击**，不读控件树。

```
src/scripts/deepseek_choose.py     深度思考 / 智能搜索 两个开关
src/scripts/deepseek_use.py        提问 → 等生成 → 点复制 → 读剪贴板
src/qi/screen.py                   截屏 / 找图 / 点击 / 输入 / 剪贴板
src/qi/library.py                  资源库（缺图先报）
tools/snip.py                      从全屏图裁模板进资源库
```

## 跑法

```bash
PY=C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe

$PY src/scripts/deepseek_choose.py --list          # 读两个开关当前状态
$PY src/scripts/deepseek_choose.py --think on
$PY src/scripts/deepseek_use.py "只回复两个字：收到"
$PY src/scripts/deepseek_use.py "继续" --continue
```

## 需要先截的图（`src/assets/library/`）

| 名字 | 截什么 | 必需 |
|---|---|---|
| `ds_composer` | 输入框里一小块稳定区域（**不要截整条**，占位文案一变就废） | 必需 |
| `ds_send_on` | 发送键**可用态**（有内容时可点的样子） | 必需 |
| `ds_copy` | 回答动作条里的复制键（**不是提问气泡上的那个**） | 必需 |
| `ds_newchat` | 「开启新对话」按钮 | `--new` 必需 |
| `ds_stop` | 生成中的停止键（用来判"生成结束"） | 可选，缺了就退回等复制键 |
| `ds_think_on` / `ds_think_off` | 深度思考开/关两种样子 | `_choose` 必需 |
| `ds_search_on` / `ds_search_off` | 智能搜索开/关两种样子 | `_choose` 必需 |

截法：

```bash
$PY tools/snip.py --shot                                        # 先看坐标
$PY tools/snip.py --save ds_composer --region 800,760,260,36
```

裁完会打印方差；方差接近 0 说明裁到了纯色块，当模板会到处误命中。

## 设计上的几条硬规矩

1. **发送键找不到就不发**，绝不退回按 Enter —— 半截内容会被发出去。
2. **复制读不到就报错**，不做文本猜测 —— 长回答猜必错。
3. **复制键取最靠下的那个** —— 提问和回答都有复制键，回答在下面。
4. **不新开窗口、不移动窗口、不切标签页**，只操作屏幕上现有的页面。
5. 两张状态图（on/off）分数太接近时判 `unknown`，不硬猜。

## 边界（这套方案的固有代价）

- **页面被遮挡就找不到**：截的是屏幕，不是窗口。跑之前把 DeepSeek 页放前台。
- **主题 / 缩放 / 文案一改，模板就废**：需要重新截。按键精灵也一样。
- **不同显示器缩放比例不同**时，模板只在截它的那块屏上有效。
- **全屏找一张 842px 宽的图约 230ms**（4000×2560 虚拟桌面实测）。

## 状态

- 代码已编译通过；`_choose --list`、`_use` 无参分支已跑通（报缺图 / 打用法）。
- **端到端未验**：资源库里还缺 deepseek 的图，且当前没有打开的 DeepSeek 页面。
