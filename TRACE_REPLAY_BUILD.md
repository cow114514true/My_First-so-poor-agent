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

| 落点 | 施工内容 | 阶段 |
|---|---|---|
| `loop_agent_core/runner.py:386` `_emit` | 转发给 trace 写入器 | 1 |
| `loop_agent_core/runner.py:392` `_notice(level, message)` | 改签名为 `_notice(level, code, message)`；17 个调用点（`runner.py:445,503,505,531,538,541,548,583,593,641,643,668,679,682,688,697` 及 `_STUCK_NOTICE`）各补一个稳定 `code` | 0 |
| `runner.py` 各 return 分支 | 发 `turn_end` 事件，`reason` 取闭集枚举 | 0 |
| `runner.py:367` `_safe_create` | 改走 `llm.create(kind="chat", ...)` | 0 |
| `runner.py:418` `next_turn` | 结果写入事件字段 `turn` | 1 |
| `runner.py:442` / `:591` 的 `tool_round` / `final_round` | 写入事件字段 `tool_round` | 1 |
| `runner.py:448`、`:595` `_prep_context` 之后 / `:450`、`:596` `_safe_create` 之前 | 写 `pre_request[i]` 快照 | 1 |
| `runner.py` 一轮全部结算动作之后 | 写 `post_turn` 快照 | 1 |
| `runner.py:233-341` `run_tools` | 两通道结果、`tool_call` / `tool_result` 事件、回放时 B 类串行 | 1 / 4 |
| `runner.py:187-197` `_store_tool_args` | 占位改为可重取（原文落 `tool_call` 事件） | 1 |
| `runner.py:211` `_call_tool` 入参层 | 回放时 B 类路径正向重写 | 4 |
| `runner.py:462`、`:610` `_record_usage` | 发 `token_usage` 事件 | 1 |
| `runner.py:201-203` `_TOOL_TIMEOUT` / `_WORKER_TIMEOUT` / `_SYNC_MAX` | 进 `limits` 事件 | 1 |
| `context.py:95-104` `_rollback_tool_round` | 发 `tool_round_rollback` 事件 | 1 |
| `context.py:141` `render_prune_notice` / `_prep_context` | 发 `prune` 事件 | 1 |
| `compaction.py` 决策点 | 发 `compaction` 事件 | 1 |
| `config.py:143` headroom `db_path` | 改读 `HEADROOM_STORE_URL` | 0 |
| `config.py:38-90` `_compact_k()` | 发 `limits` 事件；提供 `reset_compact_k_cache()` | 1 |
| `config.py:159` | 网关构造 headroom 包裹层，包裹对象为 transport | 0 |
| `loop_agent_v2.py:182` `_summary_client()` | 改走 `llm.create(kind="summary", ...)` | 0 |
| `vision.py:5` | 改走 `llm.create(kind="vision", ...)` | 0 |
| `tui.py:45` `is_conversation_file()` | 迁移器复用其口径 | 2 |

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
2. `_notice` 增 `code` 形参，调用点补 code。
3. 异常 id 由 `traceback` 里首个项目内帧的函数名 + 异常类名拼装（`loop_agent_core/` 路径判定）。
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
| `token_usage` | assert | `request_index` / `turn` / usage 各项 | `runner.py:462`、`:610` |
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

快照时机：每个请求一份，落在 `_prep_context` 之后、`_safe_create` 之前（`runner.py:448`→`450`、`runner.py:595`→`596`）；每轮结束追加一份 `post_turn`。

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
| `memory` | 注入来源为录制文本；识别标记 `"[长期记忆"`（`runner.py:426-433`） |
| `nudge` | 按 `_EMPTY_NUDGE` / `_WRAPUP_NUDGE` 注入 |
| `trim_notice` | 按 `render_prune_notice` 累加语义（`context.py:8,141,147`）与录制的 `limits` 重算 |
| `workspace_path` | 按 §6.4 根表重写 |

### 6.3 游标走位

```python
cursor, W = 0, 8      # strict 模式 W = 0
```

- 在 `[cursor, cursor+W]` 内搜 `loose` 命中。
- 命中 `cursor+k`：`cursor..cursor+k-1` 记为 skipped，发 `replay_skip` 事件（含两侧 loose / strict 差异摘要），`cursor = cursor+k+1`。
- 窗口内无命中：抛 `NoMatchInWindow`，输出两侧 diff。
- 报告输出 skip 计数与每条 skip 的位置；漂移计入报告，测试通过与否由用例声明的 `skips` 阈值决定（钉死用例加 `assert skips == 0`）。

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

---

## 7. 阶段 3.5 / 5：L3 最小化

### 7.1 目标与谓词

```python
def test(candidate) -> "FAIL" | "PASS" | "UNRESOLVED"
# FAIL 的条件：回放后 assert 级事件序列中出现 candidate.expected_assertion 对应的 id
```

输出口径：`1-minimal`（删去任一剩余单元即不再 FAIL）。

### 7.2 两阶段 ddmin

```python
c1    = ddmin(cassette, units=turns,          test=FAIL_same_assertion)
spine = positional_spine(c1)
c2    = ddmin(c1,       units=history_groups, test=FAIL_same_assertion,
              guard=no_new_or_reordered_responses)
```

- 阶段 1 单元 = 用户轮；阶段 2 单元 = `_find_groups()` 的组（`context.py:28-51`）。
- 单元固定项：首轮、含失败断言的轮、全部 assert 级控制流事件。
- 阶段 2 守卫：候选的响应序列相对 `spine` 只允许减少或保持，出现新增或重排即判该候选无效；响应数减少时记入报告。
- 收缩后 checkpoint 的 B 组状态按新序列重算并覆盖源快照值。

### 7.3 预算、报告与导出

- 重放次数上限 `--max-replays`（默认 500）。耗尽时输出 `"converged": false` 与当前最优候选。
- 报告字段：`cassette` / `units` / `replays` / `converged` / `result`（`1-minimal`）/ `expected_assertion` / `response_count_before` / `response_count_after` / `calls_before` / `calls_after` / `skipped_units`。
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

**L1（写入时，始终开启）**：抹掉 harness 会在回放时重新提供的值——env 密钥（`DS_KEY` / `VISION_API_KEY` / `TAVILY_KEY` / QQ secret / `sk-*` 模式）、`Authorization` header、cookie、clipboard 内容。替换为 `{"$redacted": "env:DS_KEY", "len": 35}`。所有省略采用可逆或可重取形式。

**L2（导出时，`--shareable`）**：

```
python -m tools.trace export --shareable tmp/traces/<sid> --out <dir>
```

额外抹用户正文、文件内容、路径、URL query。L2 产物通过回放自检后写入 `tests/fixtures/replay/`。

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
| 27 | `test_migration_matrix` | 113 份会话迁移不崩；矩阵每格计数 + 降级工具归因 |
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

---

## 10. 已定项

| # | 事项 | 结论 |
|---|---|---|
| 1 | `delegate_task` 与 sub-cassette | 整体 stub，返回录制的 `result_full`；子树 LLM 调用由子 agent 自己的 trace 覆盖（§8.3） |
| 2 | trace 目录 | `tmp/traces/<session_id>/`；`.gitignore:27` 的 `tmp/` 已覆盖，无需新增规则；CI 闸拦 `tmp/traces/**`（§4.1、§4.9） |
| 3 | overlay 建立方式与根位置 | 目录副本（`shutil.copy2`），根留 `tmp/overlay/<sid>/`；递归扫描类工具重写到 overlay 根（§8.1、§8.2） |
| 4 | 存储格式 | 自研 JSONL；OTEL 作为派生视图 `python -m tools.trace export --format otel`，不进 L0 存储层（§4.1） |
| 5 | L3 导出粒度 | 一个 fixture 一个失败轮次、一个 `expected_assertion`（§7.4） |
