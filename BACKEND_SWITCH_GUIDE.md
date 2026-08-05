# 可切换后端接入执行文档

> 你依据本文档自己改 `loop_agent_v2.py`。我不直接写代码，只给事实、位置、设计建议、验证标准。
> 读完第 1、2 节再动手。任何一步验证失败，先看第 6 节。

---

## 0. 目标与已定决策

**目标**：让 `loop_agent_v2.py` 通过环境变量在「DeepSeek API」和「本地 llama.cpp（Qwen3.5-9B 视觉模型）」之间切换，DeepSeek 保留兜底。

**已定决策**（不可改，除非你改变主意）：
- **B 可切换**：环境变量开关，默认 DeepSeek
- **C 保守工具**：本地模式下工具循环更克制（不禁用，但收敛）

**学习目标**：理解 OpenAI 兼容 API 的参数差异、把「后端专属参数」隔离进配置块、给流式处理补可移植性。

**改动边界**：只改 `loop_agent_v2.py`。`tui.py` 只 import `chat` / `messages` / `system_prompt` / `_shell_output_queue`，**不需要动**。

---

## 1. 现状解剖：模型调用层在哪

要动的地方只有 4 处（行号以当前文件为准）：

| 位置 | 行号 | 现状 |
|---|---|---|
| client 初始化 | 684–687 | 写死 `base_url=https://api.deepseek.com` + `DS_KEY` |
| 非流式 create() | 773–780 | `reasoning_effort` + `extra_body`（工具调用轮用） |
| 流式 create() | 886–893 | 同上 + `stream_options`（最终回答用） |
| 流式循环 | 897–905 | `chunk.choices[0].delta` —— 有隐患（见 §2-2） |
| MAX_TOOL_ROUNDS | 766 | 硬编码 `6`，while 循环上限 |

**哪些是 DeepSeek 专有，本地会不会炸：**

| 参数/字段 | 出现在 | 作用 | llama.cpp 行为（已实测） |
|---|---|---|---|
| `reasoning_effort="high"` | 两次 create | DeepSeek 思考强度 | **静默忽略，不报错** |
| `extra_body={"thinking": {...}}` | 两次 create | DeepSeek 思考开关 | **静默忽略，不报错** |
| `reasoning_content`（响应字段） | 788、899 | 思考内容 | 不提供 → `getattr` 返回 None，**已安全，不用改** |
| `stream_options={"include_usage": True}` | 流式 | 返回 token 统计 | **支持**，但见 §2-2 的坑 |

`_is_garbage_content()`（DSML 过滤）只和 `use_ds_from_web` 抓 DeepSeek 网页有关，**与后端切换无关，不动**。

---

## 2. 实测结论（2026-08-01，我连本地服务验证过）

1. **未知参数被忽略**：把 `reasoning_effort` + `extra_body` 原样发给 llama-server，正常返回，无 400。→ 所以「剥离参数」不是正确性问题，是**整洁性**问题：两个后端用一个配置块，各带各的参数。
2. **流式结尾的 usage 块 `choices` 为空**（OpenAI 标准行为，llama.cpp 遵守）：共 2053 块，最后 1 块 `choices=[]` 但 `usage` 有值。你现在的 `chunk.choices[0].delta` 会在这一块 **IndexError**。**必须判空**：
   ```python
   for chunk in response_stream:
       if not chunk.choices:      # 结尾 usage 块，没有内容
           continue
       delta = chunk.choices[0].delta
       ...
   ```
   （如果 DeepSeek 也发空 choices 块，这个判空顺带修了一个潜在的 DeepSeek 崩溃；没发就是 no-op，安全。）
3. **`model` 字段被 llama-server 忽略**（除非 `--alias` 指定）。传什么都行。建议仍用变量存，方便以后读日志。
4. **tools 支持**：参数被接受，Qwen3.5 函数调用可用（本地会偶发乱调/JSON 坏，代码已有兜底，所以才有「保守」决策）。
5. **图片入口**：`content` 里用 `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`（`test_vision.py` 已验证）。**本轮 agent 不接识图**，只留作后续话题。

---

## 3. 接口设计建议（参考，非唯一解）

显式开关 `MODEL_BACKEND`，两个后端各一个配置 dict。**不要在 `chat()` 里塞 if-else**，把差异收敛到配置块：

```python
def _backend_config():
    """返回当前后端的配置。切换：环境变量 MODEL_BACKEND=local"""
    if os.environ.get("MODEL_BACKEND") == "local":
        return {
            "api_key": "sk-local",                       # llama-server 默认不校验 key
            "base_url": os.environ.get("LLM_BASE_URL",
                        "http://127.0.0.1:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "qwen3.5-9b"),
            "extra_kwargs": {},                          # 剥离 DeepSeek 专有参数
            "max_tool_rounds": 3,                        # C: 保守
        }
    return {
        "api_key": os.environ.get("DS_KEY"),
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "extra_kwargs": {
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        "max_tool_rounds": 6,
    }
```

然后：
- `client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])`
- 两次 `create()` 都改成 `model=cfg["model"], messages=..., stream=..., tools=tools, **cfg["extra_kwargs"]`（流式那次再额外加 `stream_options={"include_usage": True}`）
- `MAX_TOOL_ROUNDS = cfg["max_tool_rounds"]`（去掉硬编码 6）
- `reasoning_content` 的 `getattr` 处理原样保留——它就是为「字段可缺」写的，本地正好缺。

**保守工具的第二个杠杆（可选，一行）**：本地模式下给 `system_prompt` 末尾追加一句「除非必须，优先直接回答，不要频繁调用工具」。改全局 `system_prompt` 或构造时按 cfg 拼接都行。

---

## 4. 实施步骤（每步独立可验证）

**Step 1 — 加配置块 `_backend_config()`**
验证：在 agent 目录分别跑
```bash
python -c "import loop_agent_v2; print(loop_agent_v2.client.base_url)"
export MODEL_BACKEND=local   # Windows cmd: set MODEL_BACKEND=local
python -c "import loop_agent_v2; print(loop_agent_v2.client.base_url)"
```
前者应打印 `https://api.deepseek.com`，后者应打印 `http://127.0.0.1:8080/v1`。（client 在模块顶层构造，所以 env 要在 import 前设好。）

**Step 2 — 改非流式 create()**（工具轮）
把 model/extra_kwargs 换成 cfg 取值。验证：改完先不跑，继续下一步，避免半成品报错。

**Step 3 — 改流式 create() + 循环判空**
流式 create 同样接 cfg；循环里加 `if not chunk.choices: continue`。这一步是**正确性修复**，漏了本地必崩。

**Step 4 — MAX_TOOL_ROUNDS 参数化**
去掉 `766` 行硬编码，读 `cfg["max_tool_rounds"]`。

**Step 5 — 回归验证**
跑第 5 节清单。

---

## 5. 验证清单（复制即用）

**DeepSeek 模式**（需要 `DS_KEY` 环境变量存在，这是你原有的）：
```bash
python loop_agent_v2.py
# 输入: 今天日期多少     → 应触发 get_date 工具并答出日期
# 输入: exit
```

**本地模式**：
```bash
# 1. 起服务（另开一个终端）
start_local_llm.bat

# 2. 本终端切后端
export MODEL_BACKEND=local    # Windows: set MODEL_BACKEND=local

# 3. 跑
python loop_agent_v2.py
# 输入: 你好                        → 正常回复（验证流式 + 空choices修复）
# 输入: 今天日期多少                 → 观察工具调用是否触发、是否克制（保守值3）
# 输入: 描述一下 example.png         → 模型应表示"看不到图"（agent 没接识图工具），
#                                      这正说明后端已切到本地
```

**服务没起时的行为**：openai SDK 会抛连接错误。你可以接受原样（会打印 traceback），或在 `chat()` 里 try/except 给一句友好提示——这是你自己加的健壮性，不强制。

---

## 6. 坑与提示

1. **最大的坑**：流式结尾空 `choices`。漏判空 = 本地模式每次最终回答最后一步必崩。
2. **本地小模型会乱调工具**：可能连续几次调用无意义工具直到轮数上限。保守值 3 就是为此。观察完可以把 `max_tool_rounds` 调到 2 或加系统提示。
3. **`reasoning_content` 别删**：它是 `getattr(msg, 'reasoning_content', None)`，DeepSeek 有值、本地没有，删了会破坏 DeepSeek 的思考显示。
4. **`extra_kwargs` 不要包含 `stream_options`**：流式专用，只在流式调用里传。
5. **TUI 不需要改**。改完 `loop_agent_v2.py` 后 `tui.py` 直接用 `python tui.py` 就能切到本地（记得先 `set MODEL_BACKEND=local`）。
6. **识图进 agent 是后续话题**：需要给 agent 加一个"读图工具"（把图片 base64 喂给本地模型）。本轮不做。

---

改完 + 验证通过后，把结果告诉我。如果哪一步卡住，把报错贴出来。
