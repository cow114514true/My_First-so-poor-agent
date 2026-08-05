# 跨会话记忆（Memory）实现指导书

## 你要做什么

给 agent 加两个新工具——`remember`（存）和 `recall`（查），背后是一个 `memories/` 目录里两个 JSON 文件。存/查逻辑全在新增模块 `loop_agent_core/memory.py`，然后从薄壳注册到工具表和主循环里。

读完这份指导书后你从头写，我随时回答具体问题。

---

## 第一步：新增 `loop_agent_core/memory.py`

这是唯一新增的模块文件，～100 行。

### 1.1 模块头部

```python
"""跨会话记忆：remember/recall 工具实现 + 会话开始注入。"""
import json
import os
import sys
from datetime import datetime, timezone
```

### 1.2 路径常量

定义两个模块级变量：

- `_MEM_SYSTEM_PATH`：系统级记忆文件绝对路径
  - 算法：取 `os.path.dirname(os.path.abspath(__file__))`（即 `loop_agent_core/` 目录），`os.path.join` 其上级目录 `".."`，再 `join("memories", "system.json")`，最后 `os.path.abspath` 解析。
  - 语义：跟 agent 代码走，不跟 WORK_DIR。
- `_MEM_PROJECT_PATH`：项目级记忆文件绝对路径
  - 从 `loop_agent_core.config` import `WORK_DIR`（位置已定义在 config.py），`os.path.join(WORK_DIR, "memories", "project.json")`，再 `os.path.abspath` 解析。
  - 语义：跟工作目录走，未来 WORK_DIR 变参数时自动解耦。

> 这两条路径就是"分级"的落地——system.json 锚定 agent 本体，project.json 锚定工作目录。现在两者恰好同目录纯属巧合。

### 1.3 `_load_memories(path) -> list`

输入：JSON 文件绝对路径。  
返回：`list[dict]`，每个 dict 是一条 MemoryRecord。

边界处理（三个分支，优先级从上到下）：

1. **文件不存在**（`os.path.exists(path)` 为 False）→ 返回 `[]`（不崩溃、不报错）。
2. **文件存在但 JSON 解析失败**（`json.load` 抛任何异常）→ `sys.stderr.write(f"[Error] memories/{文件名} corrupted: {异常信息}\n")`，返回 `[]`。
3. **正常** → `json.load(open(path, "r", encoding="utf-8"))` 返回。

> 设计意图：损坏时返回空数组而非崩溃——recall/remember 调用不应该因为磁盘问题打断 agent 主循环。

### 1.4 `_save_memories(path, records: list) -> None`

输入：文件路径 + 要写入的 MemoryRecord 列表。  
无返回值。

**写入前务必先 `os.makedirs(os.path.dirname(path), exist_ok=True)`**——首次 `remember` 时 `memories/` 目录还不存在，这行确保不炸。

写文件：`json.dump(records, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)`。

> `ensure_ascii=False` 保证中文不乱码，`indent=2` 保证你手动翻看时能读。

### 1.5 `recall_mock(topic: str = "") -> str`

recall 工具的实际实现，**工具只需要一个 topic 参数**（schema 里你会定义成可选）。

#### 业务逻辑流程图

```
topic 为空?
  ├─ YES → 取两文件全部记录，合并后按 ts 倒序，取前 5 条
  └─ NO  → 遍历每条 record，判断 topic.lower() 是否包含在 (record["topic"] + " " + record["content"]).lower() 中
           → 命中则收集
           → 按 ts 倒序，取前 5 条
           → 零命中 → 返回 "无相关记忆。可用 remember(topic, content) 添加。"
```

#### 关键实现细节

- **两个文件都扫**：`system_records = _load_memories(_MEM_SYSTEM_PATH)`，`project_records = _load_memories(_MEM_PROJECT_PATH)`，合并两个列表。
- **ts 排序**：每条 record 的 `"ts"` 字段是 ISO 字符串，直接字符串比较即可（ISO 格式天然按时间排序）。`sorted(合并后的列表, key=lambda r: r.get("ts", ""), reverse=True)`。
- **前 5 条截断**：`hits[:5]`。这个 cap 和你已有的 `read_file` token 预算上限是同一哲学——不让记忆撑爆上下文窗口。
- **双文件都损坏/不存在**：返回 `"暂无记忆。可用 remember(topic, content) 添加。"`（不崩溃）。

#### 返回格式

必须返回**自然文本，不是 JSON**——这个是返回给模型的，模型读文本比读 JSON 轻松。

```
[系统级记忆]
- 本地模型注释语言: 用户明确要求... (2026-08-04)
[项目级记忆]
- token估算实现: 双模式估算器... (2026-08-03)
```

实现要点：
- 把 hit 按 `level` 分组（system/project）。
- ts 只取日期部分（`ts[:10]`）。
- 如果某个文件返回空数组，对应分组不出现在输出中。
- 如果两个都空 → `"无相关记忆。"`。

### 1.6 `remember_mock(topic: str, content: str, level: str = "project") -> str`

remember 工具的实际实现，**工具签名就是这三个参数**（schema 里你会定义成 topic+content 必填，level 可选默认 "project"）。

#### 业务逻辑流程图

```
1. 参数校验
   topic 为空 or content 为空（strip 后）?
     → return "[Error] topic 和 content 不能为空。"

2. 确定目标文件
   level == "system" ? → path = _MEM_SYSTEM_PATH, label = "system"
   否则               → path = _MEM_PROJECT_PATH, label = "project"
   （level 就是两个值，不要搞太复杂的枚举校验）

3. 读现有文件
   records = _load_memories(path)

4. 去重 + 覆盖
   在 records 里找 record["topic"] == topic 的那条（精确匹配，不是子串）
     → 找到了：覆盖 content + 更新 ts，action = "已更新"
     → 没找到：追加一条新 dict，action = "已记住"

   新/更新的记录格式：
   {
       "topic": topic.strip(),
       "content": content.strip(),
       "level": label,
       "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")
   }

5. 写入
   _save_memories(path, records)

6. 返回
   return f"{action} [{label}] {topic}"
```

#### 关键设计决策

- **去重匹配是 topic 精确相等**，不是子串。这和你选的 D1（同 topic 覆盖）对齐。如果子串匹配，`remember("偏好", ...)` 会把 `"编码偏好"` 也覆盖掉——不对。
- **覆盖保留旧记录的其余字段**？不需要——每条记录就四个字段（topic/content/level/ts），覆盖 content+ts 即可。level 本身就存储着，覆盖后不变。
- **ts 用 UTC**：`datetime.now(timezone.utc).isoformat(timespec="seconds")`。用 UTC 避免时区混乱（你在 +8，但文件可能被别的机器读写）。

### 1.7 worker 守卫

回忆 Q7 决策：worker（delegate_task 子 agent）不能调 recall/remember。

**实现方式**：在 `memory.py` 底部放一个模块级变量：

```python
# 由薄壳在导入后设置：is_worker() 返回当前是否在 worker 上下文中
_worker_check = None  # 类型: () -> bool, 或 None
```

然后在 `recall_mock` 和 `remember_mock` 函数体的**最开头**加上：

```python
if _worker_check and _worker_check():
    return "[Error] recall 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。"
```

为什么用函数引用而不是 import `_in_worker`？因为 `_in_worker` 在薄壳 `loop_agent_v2.py` 里，而 `memory.py` 是 core 包——core 不应该反向 import 薄壳（违反依赖方向）。薄壳在初始化时设这个钩子：

```python
# loop_agent_v2.py 里（后面会提到）
import loop_agent_core.memory as memory_mod
memory_mod._worker_check = lambda: _in_worker
```

### 1.8 `build_memory_injection() -> str | None`

注入用函数，**由 runner 的 chat_impl 在会话开始前调用**。

#### 业务逻辑

```
1. 读 _MEM_SYSTEM_PATH → system_records
   读 _MEM_PROJECT_PATH → project_records

2. system_records 为空 AND project_records 为空?
     → return None  （无记忆可注入，不插假消息）

3. 组装文本块

   开头：
   "[长期记忆 — 以下是你跨会话记住的事实]"

   如果 system_records 非空：
     "### 系统级记忆"
     遍历： "- {topic}: {content}"

   如果 project_records 非空：
     "### 项目级记忆索引"
     topics = " / ".join(r["topic"] for r in project_records)
     输出一行： topics

   结尾（总是追加）：
     "---\n需要详情用 recall(主题) 查询。"

4. return 组装好的字符串
```

#### 关键点

- 系统级输出**完整 content**（常驻内存），项目级只输出** topic 列表**（索引，按需取）。这对应 Q4 决策。
- 返回 None 而非空字符串——调用方根据 None 判断"是否要追加消息"。
- 这个函数不负责追加消息——它只生成文本。追加时机在 runner 侧（第五步）。

---

## 第二步：修改 `loop_agent_core/schemas.py`

在文件末尾的 `tools` 列表里，`delegate_task` 的 `}` 之后，追加两个工具定义。总位置：tools 列表的倒数两个元素。

### `recall` 工具 schema

```python
{
    "type": "function",
    "function": {
        "name": "recall",
        "description": "查询跨会话记忆。输入关键词或主题，返回匹配的记忆条目。不输入 topic 则返回最近 5 条。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "要查询的主题或关键词。留空返回最近记忆。"
                }
            }
        }
    }
}
```

只有 `topic` 一个可选参数（不在 `required` 里）。

### `remember` 工具 schema

```python
{
    "type": "function",
    "function": {
        "name": "remember",
        "description": "记住一条跨会话事实。同 topic 自动覆盖更新。系统级（system）用于用户偏好/行为准则，项目级（project，默认）用于本代码库事实。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "记忆主题，作为唯一标识用于去重覆盖"
                },
                "content": {
                    "type": "string",
                    "description": "要记住的完整事实，自包含、不依赖上下文即可读懂"
                },
                "level": {
                    "type": "string",
                    "enum": ["system", "project"],
                    "description": "分级：system=用户偏好/行为准则，project(默认)=本代码库事实"
                }
            },
            "required": ["topic", "content"]
        }
    }
}
```

`topic` 和 `content` 必填，`level` 可选（默认 `"project"`）。agent 看到 `enum` 就知道只有两个值。

---

## 第三步：修改 `loop_agent_core/validation.py`

在 `validate_tool_result(tool_name, tool_result)` 函数的现有规则列表中追加两条。

现有规则已经对每个工具做了专项检查（非空、长度、returncode 等）。追加逻辑不碰现有分支，在函数末尾（return 之前）加条件：

```python
if tool_name == "recall":
    # recall 可以返回"无相关记忆"、"暂无记忆"（约10+字符），所以允许≥10字符
    if len(str(tool_result)) < 10:
        return False, "recall 返回过短"
    return True, ""

if tool_name == "remember":
    ts = str(tool_result)
    if ts.startswith("[Error]"):
        return False, ts
    # "已记住" 或 "已更新" 开头即为正常
    if ts.startswith("已记住") or ts.startswith("已更新"):
        return True, ""
    return False, f"remember 结果异常: {ts[:100]}"
```

边界说明：
- recall 允许空结果——"无相关记忆。" 是合法结果（12 字符）。
- remember 的首层校验已在 `remember_mock` 函数里做了（topic/content 非空），这里只做结果层面的二次兜底。

---

## 第四步：修改 `loop_agent_core/runner.py`

### 4.1 修改 `run_tools` 函数签名和 worker 守卫

现有签名：
```python
def run_tools(use_tool_calls, emit, conv, tool_call_map, delegate_fn):
```

新签名：
```python
def run_tools(use_tool_calls, emit, conv, tool_call_map, delegate_fn, is_worker=False):
```

在函数体的 for 循环开头（`tool_name = tool.function.name` 之后，`try: tool_args = json.loads(...)` 之前），追加 worker 守卫：

```python
if is_worker and tool_name in ("delegate_task", "recall", "remember"):
    emit({"type": "tool_result", "tool_name": tool_name,
          "result": f"[Error] {tool_name} 在 sub-agent 内不可用。将你的结果返回给主 agent 处理。",
          "call_id": tool.id})
    # 追加 tool 结果消息后 continue（跳过实际工具调用）
    _append_msg({"role": "tool", "tool_call_id": tool.id,
                 "content": f"[Error] {tool_name} 在 sub-agent 内不可用。"}, conv)
    continue
```

这样 worker 守卫**集中在一处**处理三个工具的隔离，不用散落在各模块——未来加新"仅主控"工具也只改这里。

### 4.2 修改 `chat_impl` 函数签名

现有签名（最后两行）：
```python
def chat_impl(question, on_event, conv, tool_call_map, delegate_fn,
              default_on_event=_default_on_event):
```

新签名：
```python
def chat_impl(question, on_event, conv, tool_call_map, delegate_fn,
              default_on_event=_default_on_event, is_main_session=False):
```

### 4.3 在 `chat_impl` 入口注入记忆

在 `chat_impl` 函数体中，`_append_msg({"role": "user", "content": question}, conv)` 这行**之前**，插入：

```python
# 主会话启动时注入长期记忆（worker 不注入）
if is_main_session:
    from .memory import build_memory_injection
    injection = build_memory_injection()
    if injection is not None:
        _append_msg({"role": "user", "content": injection}, conv)
```

然后紧接原有逻辑：
```python
_append_msg({"role": "user", "content": question}, conv)
q_idx = len(conv["messages"]) - 1  # ← 这行一定要在注入之后，确保 q_idx 指向用户问题而非记忆消息
```

> 为什么 `q_idx` 要在注入后重新取？因为 `q_idx` 是裁剪边界——它之前的消息都是可裁剪的旧内容。记忆注入消息应该可被裁剪（窗口紧张时自动退化为"无记忆"），但用户问题不可以。

### 4.4 修改两处 `run_tools` 调用

runner.py 里有两处 `run_tools(...)` 调用（主循环一行、最终阶段一行），都追加 `is_worker=not is_main_session` 参数。

---

## 第五步：修改 `loop_agent_v2.py`（薄壳）

### 5.1 新增 import

在现有 import 块追加：

```python
from loop_agent_core.memory import recall_mock, remember_mock
import loop_agent_core.memory as memory_mod
```

### 5.2 注册工具到 TOOL_CALL_MAP

在 `TOOL_CALL_MAP` dict 末尾（`write_file_mock` 之后）追加：

```python
"recall": recall_mock,
"remember": remember_mock,
```

### 5.3 设置 worker 守卫

在模块级别、`TOOL_CALL_MAP` 定义之后加一行：

```python
memory_mod._worker_check = lambda: _in_worker
```

（`_in_worker` 已在薄壳中定义）

### 5.4 修改 `_run_tools` 包装函数

现有：
```python
def _run_tools(use_tool_calls, emit, conv):
    return _core_run_tools(use_tool_calls, emit, conv, TOOL_CALL_MAP,
                           lambda task: delegate_task_mock(task))
```

改为：
```python
def _run_tools(use_tool_calls, emit, conv):
    return _core_run_tools(use_tool_calls, emit, conv, TOOL_CALL_MAP,
                           lambda task: delegate_task_mock(task),
                           is_worker=_in_worker)
```

### 5.5 修改 `chat()` 函数

现有：
```python
def chat(question, on_event=None, _conv=None):
    ...
    return chat_impl(question, on_event, _conv, TOOL_CALL_MAP,
                     lambda task: delegate_task_mock(task))
```

改为追加 `is_main_session` 参数——只有在 `_conv["messages"] is messages`（全局 messages 列表）时才为 True：

```python
def chat(question, on_event=None, _conv=None):
    global _msg_size
    if _conv is None:
        _conv = {"messages": messages, "size": _msg_size}
    is_main = _conv["messages"] is messages
    try:
        return chat_impl(question, on_event, _conv, TOOL_CALL_MAP,
                         lambda task: delegate_task_mock(task),
                         is_main_session=is_main)
    finally:
        if _conv["messages"] is messages:
            _msg_size = _conv["size"]
```

> 用 `is` 而非 `==` 比较列表——只有全局 messages 是**同一个对象**，worker 的 conv["messages"] 是独立新列表。这个判断保证：只有主会话注入记忆、worker 不注入。

---

## 最后一步：验证

用这几个命令从头到尾测试：

```bash
# 0. import 不报错
python -c "import loop_agent_v2; print('OK')"

# 1. 首次（无 memories/ 目录）recall
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['recall']('随便'))"
# 期望："无相关记忆。可用 remember(topic, content) 添加。"

# 2. 写入一条项目级记忆
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['remember'](topic='测试', content='这是一条测试记忆'))"
# 期望："已记住 [project] 测试"

# 3. 召回
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['recall']('测试'))"
# 期望：显示 [项目级记忆] 含 "测试: 这是一条测试记忆"

# 4. 同 topic 覆盖
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['remember'](topic='测试', content='更新后的内容'))"
# 期望："已更新 [project] 测试"
# 检查文件内容：cat memories/project.json
# 期望：只有一条 "测试"，content 是 "更新后的内容"

# 5. 系统级记忆
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['remember'](topic='偏好', content='用户喜欢简短回答', level='system'))"
# 期望："已记住 [system] 偏好"
cat memories/system.json
# 期望：一条记录，level 是 "system"

# 6. 空参数校验
python -c "import loop_agent_v2; print(loop_agent_v2.TOOL_CALL_MAP['remember'](topic='', content=''))"
# 期望："[Error] topic 和 content 不能为空。"

# 7. 已有测试不挂
python test_multi_agent.py
python test_code_index.py
# 期望：全绿
```

---

## 你写的三个模块的职责总结

| 你写 | 做什么 | 文件 |
|------|--------|------|
| memory.py | 文件读写、recall 匹配、remember 去重覆盖、injection 组装、worker 守卫钩子 | 新增 |
| schemas.py | 追加 recall/remember 两个工具定义 | 修改 |
| validation.py | 追加 recall/remember 两条验证规则 | 修改 |
| runner.py | 修改 run_tools 签名追加 is_worker、chat_impl 签名追加 is_main_session、入口注入 memory | 修改 |
| loop_agent_v2.py | import + TOOL_CALL_MAP 注册 + worker 守卫设置 + _run_tools/is_main_session 传参 | 修改 |

---

## 不做的（做过决策，不要实现）

- 会话结束自动总结
- worker 可访问记忆
- embedding/语义相似度匹配
- 任何超过本文所述的"额外特性"

---

有具体问题随时问——比如"这行为什么这么写""这个边界我该怎么测"。
