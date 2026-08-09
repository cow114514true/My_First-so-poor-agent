# 多 Agent 协作架构方案（orchestrator-worker，受控并发）

> **本文档说明实际已实现的架构**：同步工具线程池并发 + `delegate_task` 子 agent **受控并发**（线程池充当异步队列）。早期「`asyncio.Queue` 并行子 Agent」方案（`queue_manager.py` / `agent_registry.py`）经评估后**未采用**——子 agent 并发受后端约束门控（见「关键设计决策」）。`loop_agent_core/` 下**不存在** `queue_manager.py`、`agent_registry.py`。

## 📋 当前架构分析

### 核心组件
- `loop_agent_v2.py`：薄壳（~106 行），组装 core + 注入 `delegate_fn` + 维护 thread-local 递归守卫 `_is_worker()`
- `loop_agent_core/runner.py`：`chat_impl` 主循环 + `run_tools`（并发执行 + 统一结算，`delegate_task` 特判处）
- `loop_agent_core/prompts.py`：`WORKER_SYSTEM_PROMPT`（子 agent 专用 system prompt）
- `loop_agent_core/validation.py`：`validate_tool_result`（worker 结果自检）
- `test_multi_agent.py`：自检（不触网，monkeypatch）

### 实际执行模型
- 主循环 `chat_impl` 是**同步**的；`run_tools` 一轮内做两件事：
  - **同步工具**（`read_file`、`search_web`、`get_weather` 等）：`ThreadPoolExecutor` 并发执行（≤8）
  - **`delegate_task`**：子 agent 受控并发——本地后端 1 个、DeepSeek 后端 3 个（`_worker_pool_size()`）
- 并发阶段**只收集结果**（不碰 `conv`/`emit`，线程安全）；结算阶段**按原始调用顺序**发事件、落库、自检

## 🎯 关键设计决策：子 agent 并发为何受后端门控

**核心约束：本地 llama.cpp 只加载一个模型。**

- 多 Agent 本身不增加显存——**并行请求**（`llama-server --parallel N` 的 KV-cache）才增加显存；单槽 server 下多 worker 并发无收益（请求在 server 端排队）
- 实测硬约束：`--parallel 2` 会把每个槽位的 ctx 减半到 8192 → 触发 context-overflow 400（"超窗口"）。当前 server **不加 `--parallel`**，保持单槽满 ctx
- 因此 **`_worker_pool_size()` 按后端门控**：`1 if _is_local_backend() else 3`
- 同步工具并发**不受此约束**：工具是 I/O 密集，GIL 在 I/O 时释放，线程池就有真实并发，且不碰模型显存

## 🏗️ 核心架构

```
┌──────────────────────────────────────────────────────────┐
│                   主 Agent (loop_agent_v2)               │
│  ┌────────────────────────────────────────────────────┐  │
│  │           chat_impl (同步主循环)                    │  │
│  │   ┌────────────────────────────────────────────┐   │  │
│  │   │ run_tools：并发执行 + 统一结算              │   │  │
│  │   │  同步工具 → ThreadPoolExecutor(≤8)         │   │  │
│  │   │  delegate_task → worker 池(≤_worker_pool)  │   │  │
│  │   │      └─▶ delegate_fn(task)=delegate_task_mock│ │
│  │   └────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

拓扑：主控-工人（orchestrator-worker），`delegate_task` 动态工具触发，深度 1 禁止嵌套。

## 🔧 核心实现

### 1. 薄壳：`delegate_task_mock`（thread-local 递归守卫 + worker 黑盒）

`loop_agent_v2.py`：

```python
_worker_state = threading.local()  # 每线程 worker 标志：并发子 agent 互不干扰（非全局布尔）


def _is_worker():
    """当前线程是否运行在子 agent 内（递归守卫 / worker 内工具禁用判断）。"""
    return getattr(_worker_state, "in_worker", False)


def delegate_task_mock(task):
    """Run a sub-agent with a fresh isolated context. Returns its final text answer."""
    if _is_worker():
        return "[Error] delegate_task is not allowed inside a sub-agent. Handle the task directly in your current context."
    worker_conv = {"messages": [{"role": "system", "content": WORKER_SYSTEM_PROMPT}], "size": None}
    _worker_state.in_worker = True
    try:
        return chat(task, on_event=lambda e: None, _conv=worker_conv)
    finally:
        _worker_state.in_worker = False
```

- **Worker 是黑盒**：复用参数化后的 `chat(question, on_event, _conv)`，全新 conv（system prompt + task），返回最终正文作为工具结果回主 agent
- **内部流式全部抑制**：`on_event=lambda e: None`，只由 `run_tools` 发 `worker_start` / `worker_done` 事件
- **递归守卫（三保险）**：
  1. `_is_worker()` **thread-local**：并发 worker 各自独立的标志，互不干扰；worker 内再调 `delegate_task` → 直接返回错误串
  2. `run_tools` 的 `is_worker` 分支：`delegate_task`/`recall`/`remember` 在 worker 内一律拒绝（`runner.py` `_WORKER_FORBIDDEN`）
  3. worker system prompt 显式禁止 `delegate_task`

### 2. `run_tools`：并发执行 + 统一结算（runner.py）

`delegate_task` **不在 `TOOL_CALL_MAP`**，由 `run_tools` 分派。结构为两阶段：

```python
results = {}  # call_id -> result
worker_meta = {}  # call_id -> task（结算阶段补发 worker_done）

# ── 1) 并发执行阶段（只收集结果）────────────────────────
sync = [t for t in use_tool_calls
        if t.function.name != "delegate_task"
        and not (is_worker and t.function.name in _WORKER_FORBIDDEN)]
if sync:
    with ThreadPoolExecutor(max_workers=_SYNC_MAX) as ex:
        fut = {ex.submit(_call_tool, t, tool_call_map): t for t in sync}
        for f in as_completed(fut):
            cid, res = f.result()
            results[cid] = res

if not is_worker:
    delegated = [t for t in use_tool_calls if t.function.name == "delegate_task"]
    if delegated:
        with ThreadPoolExecutor(max_workers=_worker_pool_size()) as ex:
            futs = []
            for t in delegated:
                task = str(json.loads(t.function.arguments).get("task", ""))
                worker_meta[t.id] = task
                emit({"type": "worker_start", "task": task[:200]})  # 主线程发，先上屏
                futs.append((t, ex.submit(_run_worker, task, delegate_fn)))
            for t, f in futs:
                results[t.id] = f.result()

# ── 2) 结算阶段：按原调用顺序，逐分支与旧逻辑一致 ─────────
for tool in use_tool_calls:
    # 中断→回滚；worker 内禁用工具→错误；发 worker_done/tool_result；
    # 落库(截断) + validate_tool_result + [Self-check]
    ...
```

- **`_worker_pool_size()`**：`1 if _is_local_backend() else 3` —— 线程池本身就是「异步队列」+ 并发上限，无需额外信号量/`asyncio.Queue`
- **`_SYNC_MAX = 8`**：同步工具并发上限
- **事件只从主线程发**（worker 生命周期事件在结算阶段补发，worker_start 在提交前发），避免工作线程直接碰 `emit`
- **顺序由 `call_id` 关联**：并发结果按原始 `use_tool_calls` 顺序结算，消息历史与工具调用顺序一致
- `delegate_fn` 由薄壳注入（`_run_tools` 包装传 `lambda task: delegate_task_mock(task)` 与 `is_worker=_is_worker()`），保留 monkeypatch 语义

### 3. Worker system prompt（prompts.py:72）

```python
WORKER_SYSTEM_PROMPT = system_prompt + """
## Sub-agent mode
You are a sub-agent executing a single delegated task in an isolated context window. You share the working directory and have access to all tools.
- Work autonomously on the task you were given. Do not call delegate_task (not available to you).
- Do not ask the user for clarification — make reasonable assumptions, state them, and proceed.
- Return a concise, self-contained final answer that the caller can use directly."""
```

### 4. 上下文隔离

裁剪函数 `_append_msg` / `_find_groups` / `_prune_messages` / `run_tools` 全改为接收 `conv = {"messages": [...], "size": n}`。`chat(question, on_event=None, _conv=None)` wrapper 为全局会话同步 `_msg_size`；worker 传独立 conv（`is_main = _conv["messages"] is messages`），**不碰全局**。文件系统 `WORK_DIR` 是天然共享状态。

### 5. 质量闭环

worker 结果直接走现有 `validate_tool_result` + `[Self-check]` 注入（runner.py 结算阶段），主 agent 自审，无专门 reviewer worker。短结果豁免：`validation.py:14` 跳过 `delegate_task` 的短结果质疑（worker 合法返回 `"Done."` 等极短结果）。

### 6. 事件流

- `worker_start`：task 前 200 字符（提交 worker 前由主线程发）
- `worker_done`：task 前 200 字符 + result 前 300 字符（结算阶段补发）
- worker 内部流式（reasoning/content）全部抑制，不污染主会话 UI

## 📝 任务分类原则

| 类别 | 处理方式 | 示例 |
|------|----------|------|
| 同步工具 | `ThreadPoolExecutor` 并发（≤8） | `get_weather`、`read_file`、`search_web`、`use_ds_from_web`、`get_date` |
| 委托任务 | worker 池受控并发（本地 1 / DeepSeek 3） | 代码分析、复杂计算、独立长任务 |

核心原则：只把「委托」走子 Agent 路径；同步工具并发执行（I/O 密集，线程池即够），避免过度异步化。

## 🚀 本地并行的前提（如确需 >1 worker）

DeepSeek 后端已支持多 worker 并发。本地后端如需 1 个以上 worker：

1. `llama-server` 必须加 `--parallel N`（`-np`；`-p` 是无效短参），并按 N 槽位限并发
2. 每个槽位的 ctx 会除以 N——当前 `--parallel 2` 已实测触发 8192 ctx 超窗口 400。需要更大的 `--context-size`，或接受每个 worker 更小的上下文
3. 当前同步 `chat_impl` 消费完整流后才执行工具，单槽不会死锁；并行后要复查每个槽位的占用时机

**在本地满足以上前提之前，`_worker_pool_size()` 返回 1（等价串行 + 单槽满 ctx）是正确选择。**

## 🔄 兼容性与自检

- ✅ 保持 `loop_agent_v2.py` 对外接口不变（`chat(question, on_event=None, _conv=None)`）
- ✅ 兼容现有事件系统（`on_event` 回调；TUI 事件队列线程安全）
- ✅ 递归守卫 thread-local，并发 worker 互不干扰，不破坏主会话正常 `delegate_task`
- ✅ 自检：`test_multi_agent.py`（不触网，monkeypatch）覆盖：tool 定义、worker system prompt、递归守卫、delegate 分发与事件、worker 上下文隔离、validate、`chat_impl` 传 conv messages、**并发混合轮**（同步工具 + delegate 顺序结算）

---

**下一步行动**（按需）：
1. 本地并行：先调 `--parallel` + ctx 量化槽位窗口预算，再改 `_worker_pool_size()`
2. 需要「任务跨轮存活、主循环先干别的」时，再引入 submit+轮询协议（当前线程池 + 轮内阻塞已够用）
3. 其余（Phase 2-4 的队列/注册表/监控）在确认并行需求前**不做**
