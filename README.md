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
  - `get_date`：获取当前日期
  - `get_weather`：查询天气（Mock 实现，建议搭配联网搜索使用）
  - `exec_shell_win`：在 Windows 上执行 CMD 或 PowerShell 命令
  - `use_ds_from_web`：通过 Playwright 控制 DeepSeek 网页版，实现：
    - 图片识别（上传图片让 DeepSeek 网页版解读）
    - 联网搜索（利用 DeepSeek 网页版的联网搜索功能获取最新信息）
- **工具结果验证**：内置 `is_result_make_sense()` 函数，自动检测工具返回结果是否有效，无效时触发 Agent 重试
- **循环保护**：最大工具调用轮次限制（默认 6 轮），防止无限循环
- **流式输出**：最终回答以流式方式逐字输出，体验更流畅
- **Thinking 启用**：通过 `extra_body` 启用 DeepSeek 的思考链（CoT）能力，提升复杂推理质量

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

浏览器会自动打开 `https://chat.deepseek.com`，请在控制台提示 "log in ..." 时完成登录操作。登录完成后，程序会自动保存浏览器状态到 `profile.json`。

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
Agent调用工具: ...
tool result for exec_shell_win: ...
🤖: 当前目录下的文件有：...
```

#### 2. 图片识别

```
😎 You: 帮我识别这张图片里的文字
Agent调用工具: use_ds_from_web
（Agent 会自动打开 DeepSeek 网页版，上传图片并获取识别结果）
🤖: 图片中的文字是：...
```

#### 3. 联网搜索

```
😎 You: 2026年最新的人工智能发展趋势是什么？
Agent调用工具: use_ds_from_web
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
    └─ 是 → 执行工具 → 验证结果有效性
              ├─ 有效 → 将结果返回给 API → 继续循环
              └─ 无效 → 自动追加重试提示 → 继续循环
```

### 工具结果验证机制

`is_result_make_sense()` 函数检测工具返回结果是否有效：
- 结果不能为空（None 或空字符串）
- 结果不能包含 "error" 或 "fail" 关键词（不区分大小写）
- 结果长度至少 10 个字符

当检测到无效结果时，Agent 会自动向对话追加一条用户消息，提示工具结果存在问题，请求重新尝试，从而实现自动修正。

### 网页桥接机制

`use_ds_from_web` 工具通过 Playwright 控制浏览器，模拟用户操作 DeepSeek 网页版：

1. 加载 `profile.json` 中的登录状态
2. 导航到 `https://chat.deepseek.com`
3. 可选上传图片文件
4. 输入提示词并发送
5. 等待并抓取回复内容（超时 120 秒，适应联网搜索场景）

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

### 循环保护

设置 `MAX_TOOL_ROUNDS = 6`，防止 Agent 在工具调用循环中无限迭代。如果达到最大轮次仍未完成，将直接输出当前已有回答。

### System Prompt 设计

```python
system_prompt = """You are a helpful assistant. If you need to use tools, just use them! When you have all the information, provide a complete answer;
                If the result is time-sensitive and not universally applicable, then you must call the web search tool(using use_ds_from_web) to verify!It is important!
                Such as weather or other infomation like this"""
```

System Prompt 明确要求 Agent 对时效性信息（天气、新闻、事件等）优先使用联网搜索工具验证，而非依赖内部知识。

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `timeout`（get_response） | 120 秒 | 网页版响应超时时间 |
| `MAX_TOOL_ROUNDS` | 6 | 最大工具调用轮次 |
| `model` | deepseek-v4-flash | DeepSeek API 模型 |
| `reasoning_effort` | high | 推理努力程度 |
| `thinking` | enabled | 是否启用思考链 |

## 注意事项

1. **Windows 专属**：`exec_shell_win` 工具目前仅支持 Windows 系统
2. **浏览器可见**：当前 `use_ds_from_web` 工具以非无头模式运行（`headless=False`），方便调试；如需后台运行可修改为 `headless=True`
3. **API 成本**：使用 DeepSeek API 会产生费用，请留意用量
4. **登录状态有效期**：`profile.json` 中的登录状态可能过期，需要定期重新运行 `Get_State.py` 刷新
5. **网页版响应时间**：联网搜索场景下，DeepSeek 网页版可能需要 30-60 秒生成回复，`get_response` 已设置 120 秒超时

## 自定义与扩展

### 添加新工具

1. 在 `tools` 列表中定义工具 schema
2. 实现对应的 Mock 函数
3. 在 `TOOL_CALL_MAP` 中注册映射关系
4. （可选）在 `is_result_make_sense` 中增加该工具的特殊验证逻辑

### 修改模型

在 `chat()` 函数中，可以修改 `model` 参数切换 DeepSeek 模型：

```python
model="deepseek-v4-flash"  # 或 deepseek-v4-pro
```

### 调整循环轮次

修改 `MAX_TOOL_ROUNDS` 变量：

```python
MAX_TOOL_ROUNDS = 10  # 增加允许的轮次
```

## 更新日志

- **v2**：增加工具结果验证机制、最大轮次限制、Thinking 启用、优化超时配置
- **v1**：初始版本，支持基础工具调用和多轮对话

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
