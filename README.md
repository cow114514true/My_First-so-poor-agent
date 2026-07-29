# DeepSeek Agent with Web Bridge

一个基于 DeepSeek API 的多轮对话 Agent，集成了工具调用（Function Calling）能力，并通过 Playwright 桥接 DeepSeek 网页版，实现图片理解、联网搜索等扩展功能。

## 项目结构

```
.
├── Get_State.py          # 登录 DeepSeek 网页版，保存浏览器状态
├── loop_agent_v2.py      # 主 Agent 程序，支持多轮对话和工具调用
├── profile.json          # 浏览器登录状态（运行 Get_State.py 后生成）
└── README.md
```

## 功能特性

- **多轮对话**：基于 DeepSeek API 的连续对话能力，自动维护对话历史
- **工具调用（Function Calling）**：Agent 可自主决定调用以下工具：
  - `get_date`：获取当前日期(简单复制粘贴api文档测试用的)
  - `get_weather`：查询天气（Mock 实现）(简单复制粘贴api文档测试用的)
  - `exec_shell_win`：在 Windows 上执行 CMD 或 PowerShell 命令
  - `use_ds_from_web`：通过 Playwright 控制 DeepSeek 网页版，实现：
    - 图片识别（上传图片让 DeepSeek 网页版解读）
    - 联网搜索（利用 DeepSeek 网页版的联网搜索功能获取最新信息）
- **流式输出**：最终回答以流式方式逐字输出，体验更流畅

## 前置要求

- Python 3.8+
- DeepSeek API Key（设置环境变量 `DS_KEY`）
- 已安装 Playwright 浏览器（用于网页版交互）

## 安装

### 1. 克隆项目

```bash
git clone <repository-url>
cd <project-directory>
```

### 2. 安装依赖

```bash
pip install openai playwright
playwright install chromium
```

### 3. 设置 API Key

```bash
# Windows (CMD)
set DS_KEY=your-deepseek-api-key

# Windows (PowerShell)
$env:DS_KEY="your-deepseek-api-key"

# Linux / macOS
export DS_KEY="your-deepseek-api-key"
```

## 使用指南

### 第一步：登录 DeepSeek 网页版并保存状态

运行 `Get_State.py`，在打开的浏览器中手动登录 DeepSeek 账号：

```bash
python Get_State.py
```

浏览器会自动打开 `https://chat.deepseek.com`，请在控制台提示 "log in ..." 时完成登录操作(注意！需要切回控制台回车)。登录完成后，程序会自动保存浏览器状态到 `profile.json`。

> **注意**：这一步只需要执行一次，后续 Agent 会自动使用保存的登录状态，无需重复登录。

### 第二步：运行 Agent

```bash
python loop_agent_v2.py
```

启动后，你可以在终端中与 Agent 进行多轮对话：

```
多轮对话🤣（输入 'exit' 退出）

😎 You: 今天天气怎么样？
🤖: 我帮你查一下...
```

### 使用示例

#### 1. 执行 Shell 命令

```
😎 You: 帮我查看当前目录下的文件列表
Agent 决定调用工具: ...
tool result for exec_shell_win: ...
🤖: 当前目录下的文件有：...
```

#### 2. 图片识别

```
😎 You: 帮我识别这张图片里的文字
Agent 决定调用工具: ...
（Agent 会自动打开 DeepSeek 网页版，上传图片并获取识别结果）
🤖: 图片中的文字是：...
```

#### 3. 联网搜索

```
😎 You: 2026年最新的人工智能发展趋势是什么？
Agent 决定调用工具: use_ds_from_web
（Agent 会自动打开 DeepSeek 网页版，启用联网搜索并获取结果）
🤖: 根据最新信息，2026年AI发展趋势包括...
```

## 核心设计说明

### Agent 工作流程

```
用户输入 → 调用 DeepSeek API（带工具定义）
         ↓
    是否有工具调用？
    ├─ 否 → 流式输出最终回答
    └─ 是 → 执行工具 → 将结果返回给 API → 重复上述流程
```

### 网页桥接机制

`use_ds_from_web` 工具通过 Playwright 控制浏览器，模拟用户操作 DeepSeek 网页版：

1. 加载 `profile.json` 中的登录状态
2. 导航到 `https://chat.deepseek.com`
3. 可选上传图片文件
4. 输入提示词并发送
5. 等待并抓取回复内容

这种设计巧妙地将 DeepSeek 网页版的能力（图片识别、联网搜索）集成到了 API 驱动的 Agent 中。

### 工具调用映射

```python
TOOL_CALL_MAP = {
    "get_date": get_date_mock,
    "get_weather": get_weather_mock,
    "exec_shell_win": exec_shell_win_mock,
    "use_ds_from_web": use_ds_from_web_mock
}
```

## 注意事项

1. **Windows 专属**：`exec_shell_win` 工具目前仅支持 Windows 系统
2. **浏览器可见**：当前 `use_ds_from_web` 工具以非无头模式运行（`headless=False`），方便调试；如需后台运行可修改为 `headless=True`
3. **API 成本**：使用 `deepseek-v4-flash` 和 `deepseek-v4-pro` 模型会产生 API 费用，请留意用量
4. **登录状态有效期**：`profile.json` 中的登录状态可能过期，需要定期重新运行 `Get_State.py` 刷新

## 自定义与扩展

### 添加新工具

1. 在 `tools` 列表中定义工具 schema
2. 实现对应的 Mock 函数
3. 在 `TOOL_CALL_MAP` 中注册映射关系

### 修改模型

在 `chat()` 函数中，可以修改 `model` 参数切换 DeepSeek 模型：

```python
model="deepseek-v4-flash"  # 或 deepseek-v4-pro
```

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
