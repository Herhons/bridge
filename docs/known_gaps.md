# Bridge v0.2.2 已知限制与真机注意事项

1. **DeepSeek 网页端 2000 字粘贴上限**
   - DeepSeek 网页输入框单次粘贴超过 2000 字符会被整段吞掉；`config/endpoints.json` 已默认设置 `"paste_chunk": 1500` 分块粘贴绕过。
2. **Grok 网页端换行折叠**
   - Grok 输入框会折叠多行粘贴中的换行符；`config/endpoints.json` 已默认开启 `"single_line_paste": true`。
3. **Gemini 网页端提交落点延迟**
   - Gemini 提交后通常需要约 2~3 秒页面才会出现新回复节点，等待轮询与 `stable_seconds`（默认 2.5s）已覆盖该窗口。
4. **OpenCode 桌面端回复读回**
   - OpenCode Electron 界面的复制按钮可能需要悬停才可见；`endpoints/opencode.py` 默认启用只读 SQLite 兜底（`~/.local/share/opencode/opencode.db`），不修改数据库。
5. **Hermes 与 Grokbot 首次真机校准**
   - 两个新增端目前使用通用桌面/聊天窗口 UIA 选择器，且 `model_selector` 留空（跳过切模型）；首次在 Windows 上运行前请先执行 `py -3.10 bridge.py snapshot hermes` 和 `py -3.10 bridge.py snapshot grokbot`，按实时控件名微调 `config/endpoints.json`。
6. **平台限制**
   - 实时 UIA 与 `PrintWindow` 截图兜底仅在 Windows 10/11 上运行；离线单元测试可在 Linux/macOS/Windows 任意环境直接执行。
