# Agent 追踪与回放（Trace & Replay）施工文档

本文只写「做什么、在哪做、做成什么样」，接续该文档的 §编号引用其规格细节。

---

## 1. 交付物

| # | 交付物 | 路径 | 阶段 |
|---|---|---|---|
| 1 | LLM 网关 | `loop_agent_core/llm.py` | 0 |
| 2 | 工具分类表 | `loop_agent_core/toolclass.py` | 0 |
| 3 | trace 写入器 + 事件 schema + 分级 | `loop_agent_core/trace.py` | 1 |
| 4 | checkpoint 写入 / seek / 自检 | `loop_agent_core/checkpoint.py` | 1 |
| 5 | cassette 读写 + 完整度与模式校验 | `loop_agent_core/cassette.py` | 1 |
| 6 | trace CLI（抽取 / 导出 / 清理 / 列表） | `tools/trace.py` | 1 |
| 7 | 旧会话迁移器 | `tools/trace_migrate.py` | 2 |
| 8 | 回放 transport（指纹 + 游标 + strict） | `loop_agent_core/replay.py` | 3 |
| 9 | L3 最小化（ddmin） | `tools/shrink.py` | 5 |
| 10 | 运行时产物目录 | `tmp/traces/<session_id>/` | 1 |
| 11 | 入库 fixture | `tests/fixtures/replay/<name>/` | 3 |
| 12 | 测试套件 | `tests/test_trace_*.py`、`tests/test_replay_*.py`、`tests/test_shrink_*.py` | 1–5 |

新增依赖：`zstandard`。测试套件沿用 `run_tests.py` 的「纯逻辑、不触网、不依赖本地服务」定位并并入其中。

---

## 2. 代码落点表

落点以**符号名**为准；行号是参照，会随同文件其他改动漂移。同一符号出现多处时给全。

| 落点 | 施工内容 | 阶段 |
|---|---|---|
| `loop_agent_core/runner.py:862` `_emit` | 转发给 trace 写入器 | 1 |
| `loop_agent_core/runner.py:868` `_notice(level, message)` | 改签名为 `_notice(level, code, message)`，调用点各补一个稳定 `code`（调用点一律走 `_noticec(key, **fmt)`，`runner.py:907`：code / level / 文案三处只有一份来源） | 0 |
| `runner.py` 各 return 分支 | 发 `turn_end` 事件，`reason` 取闭集枚举 | 0 |
| `runner.py:833` `_safe_create` | 改走 `llm.create(kind="chat", ...)` | 0 |
| `runner.py:992` `next_turn` | 结果写入事件字段 `turn` | 1 |
| `runner.py:1016`（主循环）与 `:1199`（收尾段）的 `tool_round` / `final_round` | 写入事件字段 `tool_round` | 1 |
| `runner.py:1041`、`:1217` `_prep_context` 之后 / `:833` `_safe_create` 之前 | 写 `pre_request[i]` 快照 | 1 |
| `runner.py:941` | 一轮全部结算动作之后，写 `post_turn` 快照 | 1 |
| `runner.py:535` `run_tools` | 两通道结果、`tool_call` / `tool_result` 事件、回放时 B 类串行 | 1 / 4 |
| `runner.py:478` `_store_tool_args` | 占位改为可重取（原文落 `tool_call` 事件） | 1 |
| `runner.py:513` `_call_tool` 入参层 | 回放时 B 类路径正向重写（重写只改执行用的副本，会话历史里那条 assistant 消息仍是逻辑路径） | 4 |
| `runner.py:1055`、`:1232` `_record_usage` | 发 `token_usage` 事件 | 1 |
| `runner.py:503-505` `_SYNC_MAX` / `_TOOL_TIMEOUT` / `_WORKER_TIMEOUT` | 进 `limits` 事件 | 1 |
| `context.py:104` `_rollback_tool_round` | 发 `tool_round_rollback` 事件 | 1 |
| `context.py:164` `render_prune_notice` / `:221` `_prep_context` | 发 `prune` 事件 | 1 |
| `compaction.py` 决策点 | 发 `compaction` 事件 | 1 |
| `config.py:161-162` headroom `db_path` | 改读 `HEADROOM_STORE_URL` | 0 |
| `config.py:38` `_compact_k()` | 发 `limits` 事件；提供 `reset_compact_k_cache()` | 1 |
| `config.py:155-168` | 网关构造 headroom 包裹层，包裹对象为 transport | 0 |
| `loop_agent_v2.py:182` `_summary_client()` | 改走 `llm.create(kind="summary", ...)` | 0 |
| `vision.py:3` | 改走 `llm.create(kind="vision", ...)` | 0 |
| `tui.py:39` `is_conversation_file()` | 迁移器复用其口径 | 2 |

---

## 3. 阶段 0：LLM 出口归一与断言面

### 3.1 `llm.py` 网关

```python
# 模块级状态，保持可注入
def configure(*, transport=None, client=None) -> None
def current_transport() -> Transport
def create(*, kind: str, **kwargs):  # kind ∈ {"chat", "summary", "vision"}
    ...
def is_local_backend() -> bool
```

Transport 协议：

| 方法 | 语义 |
|---|---|
| `create(kind, **kwargs)` | 与 `openai` 的 `chat.completions.create` 同签名、同返回类型 |

Transport 实现：

| 实现 | 行为 |
|---|---|
| `RealTransport` | 直连 `OpenAI` 客户端 |
| `RecordTransport(inner, writer)` | 转调 `inner`，同时把请求体与逐 chunk 响应写入 trace |
| `ReplayTransport(cassette)` | 从 cassette 取响应 |

装配顺序：`RealTransport`/`ReplayTransport` 在内侧，headroom 包裹层在外侧：

```python
client = _maybe_wrap_headroom(<transport>.as_client())
llm.configure(transport=...)
```

三处出口改造后各自调用点：

| 调用点 | 改造 |
|---|---|
| `runner.py:367` `_safe_create(conv, **kwargs)` | 转调 `llm.create(kind="chat", **kwargs)`；保留 `runner.client` 变量作为可替换注入缝 |
| `loop_agent_v2.py:182` `_summary_client()` | 返回 `llm` 的 summary kind 视图；保留函数名与零参签名供测试替换 |
| `vision.py:5` | 移除模块内 `OpenAI(...)` 构造，改调 `llm.create(kind="vision", ...)` |

### 3.2 headroom 接线

1. `config.py:143` 的 store 路径改为 `os.environ.get("HEADROOM_STORE_URL") or f"{WORK_DIR}/tmp/headroom.jsonl"`（保持无 env 时行为不变）。
2. 回放时把 `HEADROOM_STORE_URL` 指向 `tmp/traces/<sid>/headroom.jsonl`。
3. 请求体录两份：`headroom_transform` 事件携带 pre-headroom 体，`llm_request` 携带 post-headroom 体。
4. `meta.json` 固化 `headroom_version` / `headroom_mode` / `model_context_limits`；加载 cassette 时比对，不匹配即抛「cassette 过期」。

### 3.3 `toolclass.py`

```python
TOOL_CLASS: dict[str, ToolClass] = {
    # name -> {"class": "A"|"B"|"C", "path_args": (...), "rewrite_scope": "structured"|"best_effort"|None}
    "read_file": {"class": "A", ...},
    "write_file": {"class": "B", "path_args": ("path",), "rewrite_scope": "structured"},
    "shell_cmd": {"class": "B", "rewrite_scope": "best_effort"},
    ...
}
def class_of(name: str) -> str            # 未登记 → 抛 UnclassifiedToolError
def path_args_of(name: str) -> tuple[str, ...]
```

初始分类：

| 类 | 工具 |
|---|---|
| A | `read_file` / `list_dir` / grep / 代码索引 / `recall` / `recall_archive` |
| B | `write_file` / `edit_file` / `shell_tools` / `mark_coin` |
| C | `browser_act` / `ds_web` / `tavily` / vision / clipboard / `delegate_task` / `qq_bot` |

校验：进程启动时遍历 `TOOL_CLASS` 与实际注册工具表，发现未登记工具即抛错并列出工具名。

### 3.4 断言 id 词表

```
turn_end:reason=<answer|stuck|garbled|interrupted|rounds_exhausted|api_error|fallback>
exception:<ExcType>@<func>          # 项目内首个帧的函数名，不含行号
tool_error:<tool>:<code>
notice:<level>:<code>               # level ∈ {info, warn}
```

施工动作：

1. `chat()` 的每个 return 分支发一次 `_emit({"type": "turn_end", "reason": ...})`。
2. `_notice` 增 `code` 形参，调用点补 code。落地形态是一张 `NOTICE_CODES` 词表（`runner.py:257`，键 = code，值 = `(level, code, 文案模板)`）加一层 `_noticec(key, **fmt)`（`runner.py:907`）：code、level、文案只有一份来源，改文案不动 code（§9 #3）。
3. 异常 id 由 `traceback` 里首个项目内帧的函数名 + 异常类名拼装（`loop_agent_core/` 路径判定），落点是 `trace.assertion_id_for_error()`（`trace.py:180`）；`assertion_id()`（`trace.py:214`）/ `assertion_ids()`（`trace.py:243`）是它的消费侧。
4. 工具错误在工具包装层统一转成 `tool_error:<tool>:<code>` 并计入 assert 级事件。
5. 词表变更走 `cassette_format` converter（见 §7.3）。

---

## 4. 阶段 1：L0 trace

### 4.1 目录与文件

目录落在 `tmp/traces/`（`.gitignore:27` 的 `tmp/` 已覆盖）。存储格式为自研 JSONL；OTEL 导出是后续的派生视图（`python -m tools.trace export --format otel`），不进 L0 存储层。

```
tmp/traces/<session_id>/
  meta.json               # 会话元信息
  trace.jsonl             # 一行一事件，追加写
  trace.idx.json          # 行偏移索引：记录号 -> 字节偏移
  checkpoints/index.json  # request_index -> (frame_file, offset, comp_len, raw_len, blob_id)
  checkpoints/<group>.zst # 帧组
  headroom.jsonl          # headroom 遥测（由 HEADROOM_STORE_URL 指向）
  migration_report.json   # 迁移产出时存在
```

`meta.json` 字段：`schema_version` / `canon_version` / `trace_level`（`assert`\|`stream_summary`\|`stream`）/ `session_id` / `start_ts` / `end_ts` / `backend` / `model` / `headroom_version` / `headroom_mode` / `model_context_limits` / `trace_epoch`（`{"iso": ..., "injected_date_str": ...}`）/ `env_snapshot`。

### 4.2 写入器

```python
# loop_agent_core/trace.py
def open_writer(session_id: str, *, level: str = "assert") -> TraceWriter

class TraceWriter:
    def emit_record(self, record: dict) -> None      # 原始事件，始终写
    def emit_event(self, event: dict) -> None        # 按 level 过滤后写
    def write_checkpoint(self, snap: dict) -> int    # 返回 request_index
    def close(self) -> None
```

要求：

1. 内部加锁，可被 `run_tools` 的线程池并发调用。
2. 写入异常被捕获并记入 `error.log`，对调用方返回 `None`。
3. 存储形态：单个 `trace.jsonl` 文件句柄 + 追加写，供 `run_tools` 的线程池直接调用。
4. 序列化器按鸭子类型处理 SDK 对象：同时接受对象属性与 dict 取值（`usage`、`delta`、`chunk.choices`）。
5. `trace.idx.json` 随写入更新（每行 flush 后记 offset）。

### 4.3 事件清单

| 事件 | 级别 | 载荷字段 | 发射点 |
|---|---|---|---|
| `llm_request` | record | `request_index` / `turn` / `tool_round` / `kind` / `model` / `params` / `messages` | `llm.create` |
| `llm_chunk` | record | `request_index` / `seq` / `content` / `reasoning_content` / `tool_calls`（按 `index` 分片）/ `usage` / `finish_reason` / `choices_empty` | `llm.create` |
| `llm_end` | record | `request_index` / `finish_reason` / `usage` / `latency_ms` / `interrupted` | `llm.create` |
| `headroom_transform` | record | `request_index` / `messages_pre` / `messages_post` | headroom 包裹层 |
| `token_usage` | assert | `request_index` / `turn` / usage 各项 | `runner.py:1055`、`:1232` |
| `tool_call` | assert | `call_id` / `turn` / `tool_round` / `name` / `arguments_raw` / `class` / `rewrite` | `run_tools` |
| `tool_result` | assert | `call_id` / `name` / `class` / `result_full` / `result_stored` / `truncation` / `duration_ms` / `from_replay` | `run_tools` |
| `limits` | assert | 生效旋钮值 + B 组记账值（见 §4.5） | `_compact_k()` / `_prune_cap()` / `run_tools` cap / `_store_tool_args` |
| `notice` | assert | `level` / `code` / `message` | `_notice` |
| `turn_end` | assert | `reason` | 各 return 分支 |
| `nudge_injected` | assert | `kind`(`empty`\|`wrapup`) / `attempt` | `_inject_user_nudge` 调用点 |
| `prune` | assert | `layer`(`L0`\|`L1`\|`L2`) / `removed` / `reclaimed_tokens` / `cap` | `_prep_context` |
| `compaction` | assert | `decision`(`attempted`\|`skipped`\|`failed`) / `reason` / `blocks` / `finish_reason` | `compaction.py` |
| `tool_round_rollback` | assert | `cause`(`interrupt`\|`api_400`) | `context.py:95-104` |
| `xml_toolcall_retry` | assert | `round` / `count` | `_warned_xml` |
| `thinking_only_retry` | assert | `round` / `count` | `_empty_retries` |
| `worker_dispatch` / `worker_settle` | assert | `task_digest` / `result_digest` | `_run_worker` |
| `interrupt` | assert | `cause` / `phase`(`pre_round`\|`during_stream`\|`during_tools`) | `interrupt_event` |
| `api_error` | assert | `status` / `body_digest` | `_safe_create` |
| `self_check` | assert | `content` | `run_tools` 自检合并点 |
| `error` | assert | `where` / `exc_type` / `message` | 异常捕获点 |
| `stream_summary` | stream_summary | `request_index` / `round` / `thinking_chars` / `response_chars` / `emitted_response_chars` / `first_token_ms` | 每轮派生 |
| `response_chunk` / `thinking_chunk` | stream | 逐 chunk 文本 | 仅 `trace_level=stream` |

### 4.4 分级与转发

`emit_event` 按 `trace_level` 过滤：`assert` 级始终写，`stream_summary` 默认写，`stream` 级默认丢弃。`on_event` 转发同一套过滤结果。

### 4.5 两通道与自检闭环

`tool_result` 同时记 `result_full`（`runner.py:316` 的 emit 值）与 `result_stored`（`runner.py:318-325` 的 conv 值，cap 6000 本地 / 10000 云端），并记 `truncation = {"cap": <生效值>, "raw_len": N, "recorded_at": ...}`。

`write_file` 的 arguments 同样两处：`tool_call.arguments_raw` 存原始完整参数；`conv` 内的 assistant 消息仍是 `_store_tool_args` 的占位，占位形如 `{"$omitted": {"len": N, "sha256": ...}}`，回放时从 `tool_call.arguments_raw` 重取。

回放时按下列流程归因（A / C 类吐 `result_full`，B 类真执行取真结果）：

```
result_stored_now = truncate(result_full, limits_now)
if result_stored_now == result_stored_recorded: 通过
elif limits.cap != limits_recorded.cap:         抛 TruncationBudgetChanged(点名旋钮)
elif limits 无记录:                              抛 Unattributable
else:                                           记 drift（strict 模式失败）
```

`limits` 事件载荷：

```json
{
  "request_index": 12, "turn": 3, "tool_round": 1,
  "knobs": {"result_cap": 10000, "store_tool_args_threshold": 500,
            "compact_max_tokens": 3000, "compact_max_tokens_hard": 6000,
            "trigger_ratio": 0.5, "block_tokens": 8000, "keep_rounds": 3,
            "dedup_min_chars": 1000, "dedup_min_saved": 200,
            "min_rounds": 3, "max_per_session": 20, "max_fails": 3, "max_backoff": 32,
            "prune_cap": 32000, "tool_timeout": 150, "worker_timeout": 600, "sync_max": 8},
  "accounting": {"_compact_count": ..., "_compact_token": ..., "_compact_last_turn": ...,
                 "_dedup_saved": ..., "_compact_fails": ..., "_compact_retry_turn": ...}
}
```

`knobs` 记录 resolve 之后的生效值（`_compact_k()` 读 env 后的结果）。

### 4.6 chunk 记录集

记录内容：`reasoning_content` 增量、`content` 增量、`tool_calls` 分片（含 `index` / `id` / `function.name` / `function.arguments` 分片）、`usage`、`finish_reason`、`choices_empty` 标记。跳过空 delta。

### 4.7 checkpoint

状态分组与快照字段：

| 组 | 内容 | 快照方式 |
|---|---|---|
| A conv | `messages` / `size` / `_user_turn` / `_empty_retries` / `_archive` | `messages` 存 hash 指针（`body_missing` 的降级 cassette 内联全文） |
| B 压缩记账 | `_compact_count` / `_compact_last_turn` / `_compact_token` / `_dedup_saved` / `_compact_fails` / `_compact_retry_turn` | `cstate(conv)` 取到的那处对象 + `loop_agent_v2._session_state` 两处都抓 |
| C 薄壳 | `loop_agent_v2._session_state._archive` / `._compact` | 两字段 |
| D 进程级 | `control.interrupt_event` / `config._compact_k_cache` / `config.client` 的 headroom session 态 / `events._headless_streaming` / `ds_web._executor` | 记录当前值；restore 时清空 `_compact_k_cache`、clear `interrupt_event`、重建 `ds_web._executor` |
| E 工作区 | 路径 + mtime + size + sha256（小文件）/ mtime + size（大文件），只含 agent 触碰过的路径 | manifest |
| F 前缀稳定性 | `stable_prefix_hash` | 值 |

快照时机：每个请求一份，落在 `_prep_context` 之后、`_safe_create` 之前（`runner.py:1041`、`:1217` 的 `_checkpoint("pre_request", q_idx)`）；每轮结束追加一份 `post_turn`（`runner.py:941`）。

快照记录：

```json
{"kind": "pre_request" | "post_turn",
 "request_index": i, "turn": t, "tool_round": r,
 "messages_hash": "...", "messages_inline": null,
 "B": {...}, "C": {...}, "D": {...}, "E": {...}, "F": "..."}
```

存储：帧组按**用户轮**（或每 5 个请求）切分，组内多份快照合成一个 zstd frame，组尾 `flush` 后记录 `(frame_file, offset, comp_len, raw_len)` 到 `checkpoints/index.json`；相同内容按 hash 去重为同一 blob，多个索引指向它。

seek 流程：

```
1. 读 index.json 取目标 request_index 的 (frame_file, offset, raw_len)
2. 从 offset 起新建 zstd reader，解压出 blob
3. 把 blob 的 messages_hash 与录制时该请求的 `llm_request.messages` 取 hash 比对
4. 比对不一致 → 抛 CheckpointRestoreMismatch 并退出
5. restore：清空 `config._compact_k_cache`、clear `interrupt_event`、回填 B/C/D/E 组、比对 B 组记账值与该请求 `limits.accounting`
```

`trace.jsonl` / `cassette.jsonl` 以明文 JSONL 落盘，随机访问用 `trace.idx.json` 的行偏移索引。

### 4.8 trace → cassette 抽取器

```
python -m tools.trace extract-cassette tmp/traces/<sid> --from-request 37 --out tests/fixtures/replay/<name>/
```

产出目录：

```
tests/fixtures/replay/<name>/
  cassette.jsonl          # assert 级事件 + 复现数据 + 寻址数据
  meta.json               # cassette_meta
  checkpoints/index.json
  checkpoints/<group>.zst
```

cassette 内容清单：

| 内容 | 说明 |
|---|---|
| `llm_request` / `llm_chunk` / `llm_end` | 指纹匹配与冻结响应 |
| `tool_call.arguments_raw` + `tool_result.result_full` | B 类真执行 / A、C 类吐回 |
| `truncation` + `limits` | 回放时跑 §4.5 闭环 |
| checkpoint + `index.json` | seek 到第 i 个请求 |
| assert 级事件 | 断言面 |
| `cassette_meta` | 版本、根、模式、完整度 |

cassette 收录范围：assert 级事件 + 复现数据 + 寻址数据 + 元信息。`stream_summary`、`stream`、原始诊断 dump 留在 trace 目录。

`cassette_meta`：

```json
{
  "cassette_format": 1,
  "canon_version": 1,
  "workspace_root": "C:\\Users\\...\\agent",
  "playback_mode": "fingerprint",
  "completeness": {"llm": "full", "tools": "full"},
  "degraded_reasons": [],
  "headroom": {"version": "...", "mode": "audit", "model_context_limits": {...}},
  "trace_epoch": {"iso": "...", "injected_date_str": "..."},
  "derived_from": null,
  "expected_assertion": null
}
```

校验规则（加载时执行）：

| 条件 | 行为 |
|---|---|
| `playback_mode` 缺失 | 抛 `CassetteFormatError` 并退出 |
| `playback_mode == "fingerprint"` 而调用方要求按位回放 | 抛 `PlaybackModeMismatch` |
| `playback_mode == "positional"` 而回放器走指纹匹配 | 抛 `PlaybackModeMismatch` |
| `playback_mode == "positional"` 且 `derived_from` / `expected_assertion` 为空 | 抛 `CassetteFormatError` |
| `completeness.llm == "missing"` 且进入 LLM 回放路径 | 抛 `CassetteIncomplete` |
| `headroom.version` / `headroom.mode` 与运行环境不一致 | 抛 `CassetteExpired` |

抽取后立即回放一次；能复现目标失败才允许写入目标目录。

### 4.9 访问控制与清理

- `tmp/traces/` 创建时设权限：POSIX `0o700`；Windows 执行 `icacls <dir> /inheritance:r /grant:r "%USERNAME%":(OI)(CI)F`。
- `python -m tools.trace clean --older-than 30d`：作用域为 `tmp/traces/` 下的 trace 目录；`tests/fixtures/replay/` 下的 cassette 由 git 管理。
- 单会话目录超过 5MB 时输出告警。
- 忽略规则：`.gitignore:27` 的 `tmp/` 已覆盖该目录。
- CI 检查：提交内容中出现 `tmp/traces/**` 路径即失败。CI 只保留 L2 导出物。

### 4.10 生产怎么开录

§4.1-§4.9 定的是 L0 trace 的**格式**，本节定的是**谁在什么时候开录、什么时候收尾**。在此之前写入器只被测试、迁移器与回放器调用过，生产入口从不建档——机制全绿，而真实会话一条事件都不落盘。

**开关（单一判据）**

- 判据是 `TRACE_ENABLED`，取值 `1` / `true` / `yes` / `on` / `strict`，读它的函数是 `loop_agent_core.trace.tracing_enabled()`（`trace.py:339`）。不存在第二套开关。
- 判定只在**会话起点求值一次**，决定「这次会话录不录」。进了 `SessionRecording.start()` 之后再改环境变量也不改主意：半路开关会让 trace 同时缺前半段和后半段，比不录更难查。
- 档位仍由 `TRACE_LEVEL` 决定（默认 `assert`）。录制壳传 `level=None` 让档位落回 `TRACE_LEVEL`，不在这里钉死第二个档位入口。
- 开关关着时**一根文件都不产生**——不是「建一个空目录」。`session_recording()` 返回一个未激活的壳，`start()` / `finish()` 都是空操作。

**会话边界取在哪**

入口是 `loop_agent_core.trace.SessionRecording`：

```
def session_recording(session_id=None, **kw) -> SessionRecording   # trace.py:1058
def SessionRecording.start()                                        # trace.py:1002
def SessionRecording.finish(reason=None) -> dict | None             # trace.py:1020
```

一次会话 = 一次**进程级入口**运行，不是一次 `chat()` 调用：

| 入口 | 边界 | 位置 |
|---|---|---|
| TUI | `AgentTUI.run()` 的整个生命周期 | `tui.py:1275-1290` |
| CLI | `if __name__ == "__main__"` 的输入循环 | `loop_agent_v2.py:402-418` |

为什么不用 `chat()` 当边界：`chat()` 的一次调用是一个用户轮，而 `messages` 是**进程级**的——一次 TUI 运行就是一段连续对话，`/clear` 与 `/load` 换的是对话内容、不是换一份 trace 文件。更要紧的是 trace 的 `request_index` 要跨轮只增（回放按它定位请求），按问题开档会让每个问题都从 0 重新数，checkpoint 键位互相覆盖。

**收尾时机**

- 收尾走 `with`（即 `try/finally`）：正常退出、异常退出、Ctrl-C 都补 `run_end` 与 `trace.idx.json`（`close_writer()` 补 `run_end`，`write_index()` 写索引，都在 `finish()` 里）。
- `finish()` 幂等，且**收尾失败不外抛**：录制是诊断设施，不能成为「会话挂掉」的原因。失败只追加一行到 `<run_dir>/error.log`。
- 退出理由映射：`KeyboardInterrupt → interrupted`、其余异常 `→ exception`、正常结束 `→ normal`。
- CLI 的 `EOFError`（管道读尽）在 `with` **内部**接住，算正常收尾；`KeyboardInterrupt` 不接，让它穿出 `with`，`__exit__` 才能记成 `interrupted`。

**TUI 的线程边界（不接这里等于没接）**

写入器存在 `contextvars` 里，而 TUI 的 agent 跑在 `threading.Thread` 里，`threading.Thread` **不传播 contextvars**。所以开线程的地方必须显式 `contextvars.copy_context()`，用 `ctx.run(...)` 启动（`tui.py:1825-1831`）。不拷的话 `trace.current_writer()` 在子线程里恒为 `None`：目录建了、`run_start` 写了，而 `llm_request` / `tool_call` / `turn_end` 一条都落不下来。

**目录与 manifest**

- 目录 `tmp/traces/<session_id>/`，根取 `trace.trace_root()`（`TRACE_DIR` 可覆盖），不另造路径。`session_id` 缺省由 `new_run_id(entry)` 生成。
- `meta.json` 记真 `session_id` / `backend`（`MODEL_BACKEND`，默认 `deepseek`）/ `model`（`config.cfg["model"]`）/ `trace_level`，另加 `entry`（`tui` / `cli`）。`backend` / `model` 取不到时退化成空串或默认值，不为一个元数据字段让会话起不来。
- 薄壳给 `conv` 打的 `_session_id` 取自 `trace.current_session_id()`，于是 `turn_start` / `turn_end` 的 `session` 与 `run_start` 的 `session` 是同一个值。

**失败保留 / 正常清理**

- 会话收尾时扫一遍断言 id（§3.4 词表，`assertion_id()` / `assertion_ids()`），判据是 `is_failure_assertion()`：`tool_error:*`、`exception:*`，以及 `turn_end:reason=<非 answer/interrupted>`（`stuck` / `garbled` / `rounds_exhausted` / `api_error` / `fallback`）。另补一条 `run_end:reason=exception`——会话是从入口崩出去的，可能里面一条断言都没有。
- 结论写进 `meta.json` 的 `retention`：有失败断言记 `failure`（另记 `failure_assertions` 列表与 `end_reason`），否则记 `normal`。`notice` 不进失败口径：它是诊断通道（提示 / 降级 / 跳过压缩），不代表「这次会话坏了」。
- 默认**都留着**，清理是显式动作：`python -m tools.trace clean --older-than 30d` 只清 `retention=normal` 的会话；失败现场要再带 `--include-failures` 才动。判据只**加字段、不动事件流**，所以标错也不会污染录像本身。

**遥测隔离**

录制侧不碰 `HEADROOM_STORE_URL`，正常行为继续写 `tmp/headroom.jsonl`；只有回放时才把它指向 `tmp/traces/<sid>/headroom.jsonl`（§3.2、§9 #32）。

---

## 5. 阶段 2：迁移旧会话

### 5.1 CLI 与输入过滤

```
python -m tools.trace_migrate conversations/*.json --out tmp/traces/
```

- 只收 `chat_*.json`，判定复用 `tui.is_conversation_file()`（`tui.py:45`）；排除 `.archive.json` / `.bak`。
- `.autosave*.json` 单独标记为 `interrupted_session` 并正常迁移。
- 每份输入产出一个 trace 目录 + 一份 `migration_report.json`，并按 §4.8 抽取降级 cassette。

### 5.2 可回放比例矩阵

按**用户轮**统计两轴：

| | `args_ok` | `args_partial` | `args_omitted` |
|---|---|---|---|
| `body_ok` | 完全可回放 | LLM 侧可回放，B 类降级 stub | LLM 侧可回放，B 类不可回放 |
| `body_missing` | 可重建调用序列 | 可分析 | 不可用 |

判定规则：

- `body_ok` = 该轮全部 LLM 请求体 + 流式 chunk + `finish_reason` 均在。
- `args_ok` = 该轮全部工具调用的原始 arguments 均在。
- 轮级聚合：任一请求体缺失 → `body_missing`；部分工具参数缺失 → `args_partial`；全部缺失 → `args_omitted`。
- 报告同时提供请求级与工具调用级明细。

### 5.3 `migration_report.json`

```json
{"session": "...", "units": "turn", "matrix": {"body_ok/args_ok": 7, "body_ok/args_partial": 2, ...},
 "degraded_tools": {"write_file": 5, "browser_act": 2},
 "per_turn": [{"turn": 1, "body": "ok", "args": "partial", "degraded_tools": ["write_file"]}]}
```

---

## 6. 阶段 3：L1 回放

### 6.1 指纹计算

```python
loose       = sha256(canon(kind, model, params, messages))
strict_hash = sha256(raw(kind, model, params, messages))
```

两个指纹都在回放时对「录制侧」与「现场侧」分别计算，只存在于内存。

`canon` 白名单：

| 项 | 规范化 |
|---|---|
| `kind` / `model` | 原样 |
| params | 取 `temperature` / `top_p` / `max_tokens` / `reasoning_effort` / `extra_body.thinking.type` / `tools_sha256` |
| messages | `role` + 规范化 `content` + `tool_calls`（name + 规范化 arguments，按 name 排序）+ `tool_call_id` |
| `tools` schema | 只进 `tools_sha256` |
| 压缩摘要块 | 原样参与 |

指纹输入白名单：`kind`、`model`、params 语义项、messages 规范化结果、`tools_sha256`、压缩摘要块。`result_stored` 原样进入指纹。`conv["size"]`、usage / token 计数、fingerprint 自身写在事件里，供排查读取。

### 6.2 volatile 槽位表

| 槽位 | 替换为 | 策略 |
|---|---|---|
| `ts` 时间戳 / 日期文本 | `$TS` | `reinject` |
| `memory` 记忆注入整块 | `$MEMORY` | `reinject` |
| `nudge` | `{"$nudge": "empty"\|"wrapup"}`，attempt → `$N` | `reinject` |
| `trim_notice` | `{"$trim_notice": "$N"}` | `reinject` |
| `workspace_path` | `$WORKSPACE/...` | `reinject` |
| session / request / message id | 剔除 | 不进键 |
| `timing` 耗时 / 时间串 | `$MS` / `$TMP` | `tolerate` |

重注入实现：

| 槽位 | 重注入方式 |
|---|---|
| `ts` | 回放按 `meta.json.trace_epoch.injected_date_str` 注入 |
| `memory` | 注入来源为录制文本；识别标记 `"[长期记忆"`（`runner.py:1001`） |
| `nudge` | 按 `_EMPTY_NUDGE` / `_WRAPUP_NUDGE` 注入 |
| `trim_notice` | 按 `render_prune_notice` 累加语义（`context.py:8,164`）与录制的 `limits` 重算 |
| `workspace_path` | 按 §6.4 根表重写 |

### 6.3 签名分桶的 FIFO 游标

游标按请求签名分桶，桶内 FIFO（`replay.request_signature()` / `ReplayTransport.buckets`）：

```python
signature = kind                     # 只取 kind；model/params/messages 交给 loose 指纹
buckets   = { sig: [录制位置, ...] }  # 升序
at        = { sig: 已消费下标 }
W         = 8                        # strict 模式 W = 0（按桶锁步）
```

- 现场请求只进自己的签名桶，在 `[at[sig], at[sig]+W]` 内搜 `loose` 命中。
- 命中本桶下标 `k`：`at[sig]..k-1` 记为 skipped，发 `replay_skip` 事件（含两侧 loose / strict 差异摘要），`at[sig] = k+1`。
- 本桶窗口内无命中：抛 `NoMatchInWindow`（消息点名签名桶与已消费数），输出两侧 diff。
- 跑完把**没人取的录制请求**收进 `report.unconsumed` 并在报告里列出（`ReplaySession.__exit__` 回收）。
- 报告输出 skip 计数与每条 skip 的位置；漂移计入报告，测试通过与否由用例声明的阈值决定（钉死用例加 `assert skips == 0`，删调用用例断言 `len(report.unconsumed)`）。
- 支持「跨 kind 重排 / 合法删调用」；**不支持凭空新增调用**——同签名现场多发而录制没有剩余响应时照样 `NoMatchInWindow`。

### 6.4 路径归一化

归一化在算指纹时执行，落盘一律存原始路径。映射表：

| 上下文 | 根表 |
|---|---|
| 录制 | `WORK_DIR → $WORKSPACE`、`_PROFILES_DIR → $PROFILES` |
| 回放 | `overlay_root → $WORKSPACE` |

规则：

1. 多根候选时按最长前缀优先替换。
2. 回放侧用 `cassette_meta.workspace_root` 作为录制侧根。
3. 替换只在入参与 `canon` 计算阶段执行；写入 `conv` 的 assistant `tool_calls` 消息保持录制内容。

测试钉住：`WORK_DIR/tmp/overlay/<sid>/a.py` → `$WORKSPACE/a.py`。

### 6.5 strict 判据

`strict` 模式流程：

```
1. loose_recorded == loose_live ；不等 → 失败并输出 diff
2. 对 messages 跑结构化 diff，把每处差异归类到 §6.2 槽位
3. 存在未归类的差异 → 失败
4. 存在 reinject 槽位且值不相等 → 失败（报「strict 不适用：<槽位> 不可复现」）
5. tolerate 槽位值不等 → 记入报告，通过
```

`strict_hash` 只用于预检：两侧相等则跳过第 2 步，不等则进入第 2 步。

逐字可复现范围：

| 易变值 | 复现方式 |
|---|---|
| 日期 / 时间文本 | 重注入录制串 |
| 记忆注入内容 | 重注入录制文本 |
| `tool_call_id` | 由 cassette 响应冻结 |
| `[Context trimmed]` 累计计数 | 按 `limits` + 同序 prune 决策重算 |
| 工具结果里的墙钟耗时 | 记入 `tolerate` 报告 |

### 6.6 回放 transport 与模式

```python
# loop_agent_core/replay.py
class ReplayTransport:
    def __init__(self, cassette_dir: str, *, mode: str | None = None): ...
    def create(self, *, kind: str, **kwargs): ...
```

- `mode` 显式传入时与 `cassette_meta.playback_mode` 比对，不一致抛 `PlaybackModeMismatch`。
- `fingerprint` 模式：§6.1–6.5 全流程。
- `positional` 模式：按位消费 `llm_request` 序列对应的响应，跳过请求指纹比对；报告首行输出「按位回放（跳过请求指纹比对，仅保证响应序列）」。
- `positional` 模式的失败判据取 `cassette_meta.expected_assertion`：在该次回放的 assert 级事件序列中查找该 id，找到即算复现。

**多轮回放**（`run_replay(..., all_turns=False, reseed=False)`，API 默认关、CLI `trace replay` 默认开）：

- `Cassette.turns()` 按 `turn_start` 把事件流切成用户轮，每轮带 `{index, start, end, request_indices, question, assertion_ids}`；载荷被 L3 删光、只剩控制流标记的空轮（无 `llm_request`）直接跳过——否则收缩器删掉某轮载荷后会多出一个「有 `turn_start` 没请求」的幻影轮。
- `all_turns=True` 时逐轮调用 `chat_fn(question, _conv=conv)`，会话自然累积；任一轮解析不出用户问题就抛 `ReplayError` fail-closed，绝不静默少跑一轮。`reseed=True` 时每轮从该轮首个请求的 `pre_request` 快照重建 `conv`，用于隔离轮间状态。报告 `report.turns` 列出实际回放的轮。
- 回放期间抑制长期记忆注入：录下的 user 文本已含注入内容，重注会改请求指纹、对不上。
- L3 的 `replay_once` 固定传 `all_turns=True`，于是「删除一个轮单元」在谓词里就是真的少一次 `chat()`。

---

## 7. 阶段 3.5 / 5：L3 最小化

### 7.1 目标与谓词

```python
def test(candidate) -> "FAIL" | "PASS" | "UNRESOLVED"
# FAIL 的条件：回放后 assert 级事件序列中出现 candidate.expected_assertion 对应的 id
```

判等**严格照抄回放器的成员判定**：id 由 `trace.assertion_id()` / `trace.assertion_ids()`
派生，成员判定用 `exp in assertion_ids`（与 `replay.py` 的 `ReplayReport.finalize()` 同一
写法）。收缩器里不长出第二套 id 口径——两套口径就是两个「同一个失败」的定义。

三态口径：

- `FAIL`：目标 id 在序列里。
- `PASS`：目标 id 不在。**出现另一个失败 id 也判 `PASS`**，不是 `UNRESOLVED`：断言 id 只由
  `reason` 字符串决定（§16.7），一次**不同**的失败若恰好同 reason 会被误判成同一个失败，
  所以「换了个失败」必须保守地当成「没复现」——否则收缩器会把 bug A 的现场缩成 bug B 的
  现场。换成了哪个 id 记进报告的 `substituted_assertion`，只记录、不做别的动作。同理
  **不引入概率判据**（「跑 N 次里有 M 次失败就算失败诱导」）：那会让「大多数时候报另一个
  reason」的候选被接受，是把单次静默变成统计静默。
- `UNRESOLVED`：只有两个来源，且不能扩展——① 超时；② 确定性护栏（同一个候选连跑两次、
  断言签名不一致）。按 delta debugging 原义，`UNRESOLVED` **当 `PASS` 处理**：保守拒绝该
  候选、继续删。超时必须是**独立状态**，不能与「没复现」共用一条代码路径——两者都会导致
  「这个候选不要」，但一个是判据不足（预算/环境），一个是判据完整（现场真的不在了），
  混在一起以后就再也分不出是收缩器在保守还是回放器在坏。`--runs` 只服务第 ② 条，不参与判等。

输出口径：`1-minimal`（删去任一剩余单元即不再 FAIL）。

### 7.2 两阶段 ddmin

```python
c1    = ddmin(cassette, units=turns,          test=FAIL_same_assertion)
spine = positional_spine(c1)
c2    = ddmin(c1,       units=history_groups, test=FAIL_same_assertion,
              guard=no_new_or_reordered_responses)
```

- 阶段 1 单元 = 用户轮；阶段 2 单元 = `_find_groups()` 的组（`context.py:37-51`）。
- 单元固定项：首轮、含失败断言的轮、全部 assert 级控制流事件。实现上靠**结构**保证：删除单元只覆盖可删载荷事件（`llm_request` / `llm_chunk` / `llm_end` / `token_usage` / `tool_call` / `tool_result`），控制流事件不进任何单元，于是「永不删」不需要额外判据。
- 分层删：轮单元之下再切出响应包（一次请求的 `llm_request` 到 `llm_end`）与工具包（`tool_call` 与配对的 `tool_result`），先删大块再细分。
- 依赖保持：`tool_result` 不能在没有对应 `tool_call` 时存活、反之亦然。回放前先做投影校验，不合法的候选直接判 `PASS` 且**不花回放预算**——否则回放器会以 `ReplayToolMissing` / 漂移拒掉，等于白烧一次预算。
- 阶段 2 的单元从「对话态」派生：`tools/shrink.py` 的 `history_group_units()` 取失败轮最后一个 `llm_request` 的 `messages` 与 `q_idx`，调用 `context._find_groups()` 切组，再把每个组映射回产生它的事件（助手消息 → 该次请求的响应包络，工具消息 → 配对的 `tool_call` / `tool_result`）。映射不出来源的消息（用户提问、注入的 nudge）所在的组一律 pin 住、不参与删除。注意 `_find_groups()` 只看当前提问**之前**的消息，因此失败轮为首轮时历史组为空——这是如实结论，不是接口没接上；报告 `guard.why` 会写明这一情况。候选守卫由 `spine_guard()` 承担。
- 阶段 2 守卫：候选的响应序列相对 `spine` 只允许减少或保持，出现新增或重排即判该候选无效；响应数减少时记入报告。
- 收缩后 checkpoint 的 B 组状态按新序列重算并覆盖源快照值。

### 7.3 预算、报告与导出

- 重放次数上限 `--max-replays`（默认 500）。耗尽时输出 `"converged": false` 与当前最优候选。
- 报告字段：`cassette` / `units` / `replays` / `converged` / `result`（`1-minimal`）/ `expected_assertion` / `response_count_before` / `response_count_after` / `calls_before` / `calls_after` / `skipped_units`。
- 另加三栏，各自解决一个「读报告时问不出口」的问题：
  - `unconsumed`：录制了但回放中**从未被消费**的事件清单。收缩过程里它就是死代码清单——一条响应从没被取走，说明它对这次现场没有贡献；ddmin 要靠反复试删才得到的结论，这里一眼能读。空表示每条录制事件都被消费过。
  - `critical_elements`：少了它就复现不了的那几个元素。比「删掉了什么」好读：删掉的可能几十条，决定失败的就那么几个。固定项（首轮、含失败断言的轮、控制流/断言面事件）也一并列出来，读的人才知道哪些是判据、哪些是没试过。
  - `substituted_assertion`：`PASS` 时换成了哪个 id（§7.1）。为 `null` 表示没换、或这个候选压根没跑到断言面。
- 导出：产出新 cassette，`playback_mode` 置 `"positional"`，`derived_from` 填源 trace 标识 + 本次 shrink 记录，`expected_assertion` 填失败 id。
- 词表或 `cassette_format` 升级时提供 converter；`expected_assertion` 无法解析时抛错并列出待迁移 id。

### 7.4 纳入范围

可进入 L3 的失败：`expected_assertion` 能由 assert 级事件派生的失败。业务值断言用于人工 fixture，其断言写在测试文件内。

导出粒度：一个 fixture 一个失败轮次、一个 `expected_assertion`。同一 trace 出现多个失败时抽多个 fixture，各自带自己的 `expected_assertion` 与 `derived_from`。

---

## 8. 阶段 4：L2 overlay 与工具策略

### 8.1 overlay 目录

`tmp/overlay/<sid>/`，按 `WORK_DIR` 相对结构镜像被触碰的路径。

建立方式：会话开始时把被触碰路径从 `WORK_DIR` 拷成目录副本（`shutil.copy2` 保留权限与时间戳），副本不依赖 symlink / junction 权限。

### 8.2 三段式路径映射

1. **正向重写**：在 `_call_tool`（`runner.py:211`）入参层，把 `TOOL_CLASS[name].path_args` 指定的参数重写到 overlay 根，保持相对结构。
   - 递归扫描类工具（`grep` / `glob` / `shell_cmd`）的重写目标为 overlay 根。
   - `rewrite_scope == "best_effort"` 的参数（如 `shell_cmd` 的命令串）按 `WORK_DIR` 子串做字符串级替换，并在 `tool_call` 事件标 `"rewrite": "best_effort"`。
2. **反向映射**：工具出参里的 overlay 路径在写入 `conv` 之前换回逻辑路径；漏网部分由 `$WORKSPACE` 归一化吸收。
3. **跨类污染**：A 类读路径时先查 overlay，命中则返回 overlay 内容，未命中则吐录制结果。

### 8.3 回放执行策略

| 类 | 回放行为 |
|---|---|
| A | 吐 `result_full` |
| B | 在 overlay 内真执行，按录制的发起顺序**串行** |
| C | 吐录制结果；`opt_in` 显式开启时真跑 |

另两条：

1. `get_date` 类工具回放时返回 `trace_epoch.injected_date_str`。
2. `delegate_task` 用整体 stub 的返回结果：直接吐录制的 `result_full`，子树 LLM 调用由子 agent 自己的 trace 覆盖。

### 8.4 Redaction

**先看清要抹的是什么。** trace 是一次真实会话的完整录制：用户正文、模型输出、每个工具调用的完整参数、每个工具返回的完整内容。**工具读一个文件，文件全文就在录制里；工具写一个文件，写进去的内容也在录制里。** 所以「要不要抹」这个问题，实际问的是「这段录像里有多少东西是我不想公开的」。

四处需要抹，各有各的理由：

| 抹什么 | 为什么 |
|---|---|
| 用户正文 | 隐私。需求描述里会夹带真实代码片段、报错、字段名、客户信息、临时口令——这是用户的话，不是工具的口径，规则拦不住 |
| 文件内容 | 隐私 + 商业机密。`read_file` 读到的可能是私有仓库源码、`.env`、内部文档——那是**组织的资产**，不是调试素材 |
| 路径 | 泄露「这台机器上有什么」。`C:\Users\<用户名>\source\repos\myCode\agent\...` 不需要内容敏感，路径本身就在说用户名、私有仓库、目录结构 |
| URL query | 秘密最常藏在这里（`?api_key=`、`?token=`、`?signature=`），而「URL 不算敏感」是常见误判 |

**两层，因为可识别性不同。**

**L1（写入时，始终开启）**：抹掉 harness 会在回放时重新提供的值——env 密钥（`DS_KEY` / `VISION_API_KEY` / `TAVILY_KEY` / QQ secret / `sk-*` 模式）、`Authorization` header、cookie、clipboard 内容。替换为 `{"$redacted": "env:DS_KEY", "len": 35}`。所有省略采用可逆或可重取形式。这一层**不该被当成选项**：它有名字、有固定形状，规则定得准，成本几乎为零。

**L2（导出时，计划中的 `--shareable`）**：

> 尚未实现：`cmd_export` 上那个未接线的 `--shareable` 参数已删除（§10 #6），不要照下面这行敲。这段留作 L2 的设计口径。

```
python -m tools.trace export --shareable tmp/traces/<sid> --out <dir>   # 计划中，未实现
```

额外抹用户正文、文件内容、路径、URL query。这一层存在，是因为**这些没有可靠的识别规则**：脚本判断不了「这段文本里有没有客户数据」——一段普通需求描述和一段必须保密的合同条款长得一模一样。所以分工是 L1 处理**能确定**的，L2 处理**不确定的一律抹掉**。

**核心矛盾：抹掉正文，回放就复现不了。** 极端做法（路径换占位符、内容换 `[REDACTED]`）会让回放结果对不上、断言落空；更坏的是它**看起来还在测，实际测的是别的 bug**。而回放靠**请求指纹**认请求（§6.1，`loose_fingerprint` 把 `messages` 规范后算 SHA256）——事后抹正文会直接改掉指纹，回放连「这是第几个请求」都找不到。

所以入库判据不是「敏感度高低」，而是**bug 挂在内容上还是挂在机制上**：

| 分类 | 判据 | 能否入库 | 例 |
|---|---|---|---|
| 机制类 | 换掉本轮输入仍复现 | 能，当典型与反例 | 截断预算算错、路径重写漏了一个参数、并发顺序反了 |
| 内容类 | 只有那份具体输入才复现 | 不能 | 某个私有构建脚本在特定参数下炸了 |

第二种**抹了就复现不了**——它测的就是那份内容。所以它不该入库，不是「抹干净了就能入」。

因此，入库那份应当**在录制源头就是干净的**：把机制类的现场录在一个专门的合成仓库里（假路径、假内容），而不是拿真实工作目录的录像去事后抹。这样指纹算得出来、对得上，输入从头到尾没敏感过，而且 fixture 永远可再生（§11 的 `_regenerate.py` 闸门正是为此）。事后脱敏只留给一种情形——**现场必须原样重放**；而那种现场不入库。L2 产物写入 `tests/fixtures/replay/` 前仍须通过回放自检与路径归一化。

**旧会话迁移物为什么不入库**（§5 的延续结论）：它们是真实工作现场的完整录制（文件名、内容、本机路径都在），且没有 LLM 面、过不了回放自检。它们的用途是诊断语料，留在 `tmp/` 下按需重跑，不当作 fixture。

---

## 9. 验收测试清单

| # | 测试 | 断言 |
|---|---|---|
| 1 | `test_trace_determinism` | 同一会话跑两次，`trace.jsonl` 事件类型序列一致（排除 `ts` / `duration_ms` / `latency`）；按 `call_id` 关联后一致 |
| 2 | `test_turn_end_events` | `chat()` 每个 return 分支都发过 `turn_end`，`reason` 落在枚举内 |
| 3 | `test_notice_codes` | 每个 `_notice` 调用点带 code；改文案后 `notice:<level>:<code>` 不变 |
| 4 | `test_exception_id` | 同一异常的函数名参与 id、行号不参与 id |
| 5 | `test_unclassified_tool` | 未登记工具 → 启动抛 `UnclassifiedToolError` |
| 6 | `test_replay_bug27` | 离线回放：`finish_reason=length` → 升级预算 → 成功，全程 < 2s |
| 7 | `test_replay_bug7` | 离线回放：空 `choices` 尾块不抛 IndexError |
| 8 | `test_strict_four_criteria` | strict 四条判据全中才通过；含不可复现差异时报「strict 不适用：<槽位>」 |
| 9 | `test_strict_precheck` | `strict_hash` 相等时跳过结构化 diff，不等时进入归类流程 |
| 10 | `test_skip_report` | permissive 回放每个 skip 可解释（给出首个分叉 message）；钉死用例 `skips == 0` |
| 11 | `test_checkpoint_restore_selfcheck` | messages hash + B 组记账值都与录制侧一致；不一致抛 `CheckpointRestoreMismatch` |
| 12 | `test_seek_cost_bounded` | seek 到第 N 个请求的解压次数 ≤ 帧组大小 K，与 N 无关 |
| 13 | `test_line_offset_index` | trace / cassette 按行偏移索引 O(1) 跳转到任意请求 |
| 14 | `test_playback_mode_required` | `playback_mode` 缺失 → 加载抛错 |
| 15 | `test_playback_mode_mismatch` | 两个方向的错配都抛 `PlaybackModeMismatch` |
| 16 | `test_positional_requires_derived_from` | `positional` 缺 `derived_from` / `expected_assertion` → 加载抛错 |
| 17 | `test_positional_report_banner` | `positional` 报告含「未做请求指纹比对」 |
| 18 | `test_assertion_id_from_events` | `expected_assertion` 由 assert 级事件派生；测试代码断言不参与 id |
| 19 | `test_assertion_vocab_rename` | 改 code 后旧 fixture 抛错或经 converter 迁移 |
| 20 | `test_snapshot_no_gap` | 任意请求 i 命中 `pre_request[i]`；快照字段可寻址 |
| 21 | `test_limits_knob_changed` | 改任一旋钮（如 cap 6000→8000）→ 回放抛 `TruncationBudgetChanged` 并点名旋钮 |
| 22 | `test_truncation_logic_drift` | 旋钮不变而截断逻辑变 → 只报 drift |
| 23 | `test_canon_version_independent` | 只改规范化规格、不重录 → 旧 cassette 仍可回放 |
| 24 | `test_path_normalization_cross_checkout` | 两个检出目录各录一次 → `loose` 序列一致 |
| 25 | `test_path_normalization_longest_prefix` | 嵌套 overlay 路径按最长前缀归一 |
| 26 | `test_overlay_does_not_touch_repo` | B 类重写后真实 `WORK_DIR` 文件 mtime / sha256 不变 |
| 27 | `test_migration_matrix` | 真实语料（`conversations/` 经 `tui.is_conversation_file()` 过滤后 108 份）迁移不崩；矩阵每格计数 + 降级工具归因 |
| 28 | `test_migration_degraded_rejected` | `llm: "missing"` 的 cassette 进入 LLM 回放路径时抛错 |
| 29 | `test_shrink_anchor` | 失败变成另一个失败时该候选判 PASS |
| 30 | `test_shrink_converged_flag` | 预算耗尽输出 `converged: false` + 当前最优 |
| 31 | `test_shrink_guard` | 响应新增或重排 → 候选无效；响应减少 → 记入报告 |
| 32 | `test_headroom_store_isolation` | 回放写入 `tmp/traces/<sid>/headroom.jsonl`，真实遥测文件不变 |
| 33 | `test_replay_offline` | 回放套件在无网、无本地服务下通过 |
| 34 | `test_trace_dir_under_tmp` | writer 在 `tmp/traces/<session_id>/` 下建目录与文件 |
| 35 | `test_overlay_recursive_rewrite` | `grep` / `glob` / `shell_cmd` 的重写目标为 overlay 根 |
| 36 | `test_delegate_task_stub` | `delegate_task` 回放返回录制的 `result_full` |
| 37 | `test_l3_one_assertion_per_fixture` | 单个 fixture 只带一个 `expected_assertion`；多失败导出多个 fixture |
| 38 | `test_cli_entry_records_trace` | 真实入口（TUI / CLI）在 `TRACE_ENABLED=1` 下留下 `tmp/traces/<sid>/`：`validate_events` 零问题、`run_start` 首条、`run_end` 末条、`meta.json` 有 `session_id` / `backend` / `model` / `trace_level`；开关关闭时一个字节都不产生；`clean` 默认不删 `retention=failure` 的现场（§4.10） |

---

## 10. 已定项

| # | 事项 | 结论 |
|---|---|---|
| 1 | `delegate_task` 与 sub-cassette | 整体 stub，返回录制的 `result_full`；子树 LLM 调用由子 agent 自己的 trace 覆盖（§8.3） |
| 2 | trace 目录 | `tmp/traces/<session_id>/`；`.gitignore:27` 的 `tmp/` 已覆盖，无需新增规则；CI 闸拦 `tmp/traces/**`（§4.1、§4.9） |
| 3 | overlay 建立方式与根位置 | 目录副本（`shutil.copy2`），根留 `tmp/overlay/<sid>/`；递归扫描类工具重写到 overlay 根（§8.1、§8.2） |
| 4 | 存储格式 | 自研 JSONL；OTEL 作为派生视图 `python -m tools.trace export --format otel`，不进 L0 存储层（§4.1） |
| 5 | L3 导出粒度 | 一个 fixture 一个失败轮次、一个 `expected_assertion`（§7.4） |
| 6 | 脱敏口径 | L1 抹密钥类，写入时始终开启（不是选项）；入库判据是「bug 与正文无关」（换掉输入仍复现），靠**录制源头用合成输入**保证，不靠事后抹正文——事后抹会改掉请求指纹，回放找不到那个请求；必须原样重放的现场不入库（§8.4）。`cmd_export` 上未接线的 `--shareable` 参数已删除（对外分享脱敏属 L2，不在 trace 导出层） |
| 7 | L3 谓词与报告口径 | 三态 `FAIL` / `PASS` / `UNRESOLVED`：判等照抄回放器的 `exp in assertion_ids`；「换了个失败」判 `PASS` 并记 `substituted_assertion`；`UNRESOLVED` 只有超时与确定性护栏两个来源、按 delta debugging 原义当 `PASS`，超时是独立状态；不引入概率判据。报告另加 `unconsumed` / `critical_elements` / `substituted_assertion` 三栏。阶段 1 轮级 ddmin 与阶段 2 历史组 ddmin 均已实现：阶段 2 由 `history_group_units()` 从对话态派生单元，`spine_guard()` 保证响应序列只减不增不重排，失败轮为首轮时历史组为空并在 `guard.why` 写明（§7.1、§7.2、§7.3） |
| 8 | 生产开录的开关、边界与保留策略 | 开关是单一判据 `TRACE_ENABLED`（`1` / `true` / `yes` / `on` / `strict`），只在会话起点求值一次，中途不改主意；档位仍归 `TRACE_LEVEL`；一次会话 = 一次进程级入口运行（TUI 取 `AgentTUI.run()`、CLI 取 `__main__` 输入循环），不取 `chat()`——`request_index` 要跨轮只增；收尾走 `try/finally` 且不外抛；失败现场（`tool_error:*` / `exception:*` / `turn_end:reason=<非 answer/interrupted>`）记 `retention=failure` 默认保留，正常会话记 `normal` 由 `python -m tools.trace clean --older-than 30d` 清，清失败现场要显式 `--include-failures`（§4.10、§4.9） |
| 9 | thinking-only 重试的口径 | 预算 3 次、**先判后加**：饱和后不再发第 4 次请求，在两处循环顶（主循环与收尾段）提前返回，守卫位于 `_prep_context` / `_checkpoint` **之前**（那两处会为一次并不存在的请求推进 `request_index`）；每轮注入 nudge 前先把该轮思考存成一条 assistant 消息（`reasoning:: …`），nudge 独立成条、**不改写用户原话**（§3.x 落点为 `runner.py` 的 `_append_thinking_turn` / `_nudge_is_last`） |
| 10 | `request_index` 的单调性 | 轮号与请求号是两件事：`set_tool_round()` **不清零** `request_index`，否则轮级事件（`notice` / `turn_end` / `checkpoint`）退回 `0`，`validate_events()` 会报 `request_index 回退`，回放侧也找不到归属。请求号只由 `writer.begin_request` 分配，跨轮只增（§4.5） |
| 11 | L3 的轮级删除空间 | 轮级 ddmin 的空间由**源现场的请求条数**决定：`positional_stuck` 只有 3 条响应，且 3 条都是「预算耗尽的证据」——只留 2 条时 `_empty_retries` 到不了预算，`reason=stuck` 那条饱和分支根本不会触发（§7.1 的 `FAIL` 判据是成员判定，见 §7.4）。要拿到「真删成功」的证据需要一份**多用户轮**的真现场。已补：入库 `tests/fixtures/replay/multiturn_stuck`（3 个用户轮真现场）+ `test_shrink_multiturn_real_turn_deletion`（真删 `turn=2#1 / turn=3#2`，响应 5→3、`repatch_b_group` 重算）+ `test_shrink_multiturn_budget_stops_at_baseline`（预算耗尽停在基线）；回放侧的多轮真跑见 `test_replay_all_turns_reproduces_every_user_turn`（§6.6） |
