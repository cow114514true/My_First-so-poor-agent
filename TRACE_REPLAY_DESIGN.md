# Agent 追踪与回放（Trace & Replay）设计方案

> 修订：v12。v2 纳入 9 条评审意见；v3 纳入 2 条（指纹回放时实时计算、截断预算归因）；v4 补齐「轮次」的三层定义（§15）；v5 拆开 trace 与 cassette 两个产物（§4.8）；v6 补上路径归一化的两侧根表（§6.5）；v7 修掉错误的 strict 判据（§6.6）；v8 把 L3 的字典序目标落成两阶段 ddmin（§16）；v9 修掉 checkpoint 的**单流 zstd**（§5.2，seek 需要随机访问）；v10 定下**快照时机**（§5.3，每请求一份、抓在 `_prep_context` 与 `_safe_create` 之间）；v11 把「两条回放路不能混用」从口号变成 `cassette_meta.playback_mode` 字段 + 双向硬报错（§4.8）；v12 定下**断言身份只从 assert 级事件派生**，并指出 `turn_end` / `notice.code` 今天并不存在、必须新建（§16.7）；**v13 定下 §13 的 5 项待定**（trace 落 `tmp/traces/`、存储格式自研 JSONL、`delegate_task` 整体 stub、overlay 用目录副本且根留在 `WORK_DIR` 内、L3 一个 fixture 一个断言）。变更清单见 §14。

## 0. 一句话结论

L0 是「**可 seek 的结构化事件流**」，L1 是「**冻结 LLM 响应的回放客户端**」，中间夹一层「旧会话迁移」——迁移的价值不是让旧会话能回放（做不到），而是**先量出可回放比例这个天花板**，再决定 L1 投多少。

三层职责不重叠，别混：

| 层 | 职责 | 形态 |
|---|---|---|
| 复现面 | 让失败能重演 | llm_request / llm_chunk / llm_end |
| 断言面 | 让行为可判定 | emit_event（可断言级）+ 控制流事件 |
| 寻址面 | 让失败能**局部**重演 | checkpoint |

没有第三层，「40 步会话第 37 步炸」就只能整段重放，CI 时间不可接受。

两条贯穿全文的通用原则，多处依赖：

> **① 存数据，不存规则的产物。**（§6.4）
> **② 该发现的东西不归一化，只做归因。**（§6.2 / §4.7）

「轮次」是本文多处使用的单位，且**一词三义**——定义见 **§15**，别按直觉用。

---

## 1. 范围与非目标

**做**：LLM 调用 / 工具调用 / 控制流的结构化追踪；LLM 响应的冻结回放；失败轮次 → 离线回归用例。

**不做**（明确划掉，否则投入产出比崩）：
- 不追求浏览器 / 截图 / 时钟的位级保真 → C 类工具一律 stub（见 §7）
- 不追求「复现模型采样」→ DeepSeek 不保证可复现，回放喂的是录音响应，测的是**我们的代码怎么处理这个响应**
- 不做可视化 viewer（先文件，后接不看板）

---

## 2. 现状（已核对代码）

| 已有资产 | 覆盖到哪 | 缺什么 |
|---|---|---|
| `conversations/*.json`（113 份） | message 级轨迹，含 `tool_calls` + tool result（实测 33 条 = 1 system / 4 user / 14 assistant / 14 tool） | 请求/响应体、thinking、usage、控制流、checkpoint；且 `write_file` 参数被抹成占位 → 不可回放 |
| `api_error.log` / `debug_filtered.txt` / `cache_usage.log` / `error.log` | 单点证据 | 无统一 schema、无 session id、**不能 execute** |
| `tmp/headroom.jsonl` | per-request 审计遥测：tokens / block_breakdown / **stable_prefix_hash** / cache_alignment_score | 无 body。但 `stable_prefix_hash` 是现成钩子：回放时校验前缀稳定性（Bug 36 那类漂移可直接测） |
| `test_runner_visibility.py:90` `runner.client = _FakeClient(...)`、`test_compaction.py:757` `la._summary_client = lambda: fake` | 两个假 client 已存在 | 缝已存在，只是没正式化成接口 |

方法论上也已经指向这里：`BUG_LEDGER.md` 的手段表第 1 条就是「会话 JSON 留痕当调试证据」（Bug 13/14 的定位依据）；Bug 27 的「怎么发现」写得很直白——`except` 只 print、TUI 把 stdout 盖掉，只能**手工拿真实会话原文再复现一次**摘要调用才看到 `finish_reason=length`。本方案就是把这两件事从「事后 grep + 手工重放」升级成「可执行」。

---

## 3. 前置：LLM 出口归一（阶段 0）

当前出口有**三个**，只包 `config.client` 会漏掉压缩和视觉——而 Bug 23~31 全是压缩：

| 出口 | 位置 | 特征 |
|---|---|---|
| `client.chat.completions.create` | `config.py:159`（被 headroom 包一层），经 `runner._safe_create`（`runner.py:367`）调用 | 主循环、`stream=True`、`include_usage` |
| `_summary_client().chat.completions.create` | `loop_agent_v2.py:182` 自己 new 了一个裸 `OpenAI` | 压缩摘要器、非流式 |
| `from openai import OpenAI` | `vision.py:5` 自己 new | 视觉 |

→ 抽 `loop_agent_core/llm.py` 网关，所有调用点走 `llm.create(kind=...)`，`kind ∈ {chat, summary, vision}`。网关内部是一个 **transport 可插拔点**：`real` / `record` / `replay`。

网关必须保持现有的注入语义（薄壳 + 纯实现包的既有模式），别改成全局单例，否则 `runner.client` 的 monkeypatch 会失效，现有测试会碎。

### 3.1 headroom 的参与方式（已拍板）

**决定：回放 transport 放在 headroom 内侧**，即 headroom 继续包裹 replay client。这样 headroom 的重试 / 限流 / 成本追踪 / 前缀缓存逻辑在回放时**仍然真实运行**，反而被这几条测试覆盖到。

三条附带约束，缺一不可：

1. **必须先让 store 路径可配。** `db_path` 目前硬编码为 `WORK_DIR/tmp/headroom.jsonl`（`config.py:143`）→ 回放会把假 usage 灌进真实遥测（`test_compaction.py:23-25` 已经为同款污染做过一次环境变量隔离）。阶段 0 增加 `HEADROOM_STORE_URL` 覆盖，回放时指向 `tmp/traces/<sid>/headroom.jsonl`。
2. **cassette 的键打在「上线后」的请求体上**（post-headroom），同时把 pre-headroom 体一并录进 `headroom_transform` 事件。理由：`HEADROOM_MODE=audit`（默认）不改请求体，键打哪儿都一样；一旦切 `optimize` 就会改，此时只有记 pre/post 两份才能把「对不上」归因到 headroom 还是归因到我们的代码。
3. **meta.json 必须固化 headroom 版本 + mode + model_context_limits。** headroom 是第三方克隆（`.gitignore:52` 明确排除），升级会静默改变请求体 → 所有旧 cassette 集体失效。版本进 meta，不匹配就直接判「cassette 过期」，别让它伪装成 skip。

另：headroom 的 HF tokenizer 下载已由 `config.py:8` 的 `HEADROOM_HF_TOKENIZER_LOAD_TIMEOUT_SECS=0` 关掉，回放要复核没有别的出网点。

---

## 4. L0：结构化事件 trace

### 4.1 文件布局

```
tmp/traces/<session_id>/
  meta.json        # schema_version / canon_version / 起止时间 / 后端 / 模型 / headroom 版本+mode / env 快照 / trace_level
  trace.jsonl      # 追加写，一行一事件
  checkpoints/     # 快照（见 §5）
  migration_report.json   # 仅迁移产出时存在
```

这个目录只放 **trace（诊断物）**。**回放输入（cassette）不在这里**——它是由此抽取出来的独立契约产物，见 §4.8。

目录落在 `tmp/` 下（`.gitignore:27` 已覆盖该目录）。**存储格式自研 JSONL**；OTEL / GenAI 语义约定留作后续的派生视图（`python -m tools.trace export --format otel`），不进入 L0 的存储层（§13 已定项第 4 条）。

三条硬约束，都是踩过的：
- **写入永不抛**——沿用 `debuglog.py` 的约定：留痕工具失败不能变成新的失败点
- **必须 JSONL 追加，不能 SQLite**——`run_tools`（`runner.py:253`）用线程池并发执行工具，跨线程复用连接会炸（headroom 在 `config.py:141-143` 已经因为同样原因从 sqlite 改成 jsonl）
- **序列化必须容忍 SDK 对象**——`chunk.usage`、`delta` 是 SDK 对象不是 dict；Bug 3 就是「SDK 对象 JSON 序列化崩溃」。序列化器按 `_usage_field` 的鸭子类型口径写（对象和 dict 都取）

### 4.2 事件类型

**复现面**
- `llm_request`：kind / model / params / messages（经 §5 编码）+ **`turn` / `tool_round` 显式字段**（§15：轮次边界无法从 messages 反推，必须落成数据）。**不存 fingerprint**——理由见 §6.4
- `llm_chunk`：只在 chunk 真正携带信息时记
- `llm_end`：finish_reason / usage / latency / 是否中断

**断言面（分级，见 §4.3）**
- `emit_event`：`on_event` **按级别过滤后**转发

**动作面**
- `tool_call`：name + **原始 arguments**（两通道，见 §4.4）
- `tool_result`：`result_full` + `result_stored` + `truncation`（实际生效的 cap + 原始长度）+ 耗时 + 分类（§7）+ 是否来自回放

**控制流（独立事件类型，不是塞进 tool_result 的字段）**
| 事件 | 载荷 | 对应代码 |
|---|---|---|
| `nudge_injected` | kind: `empty`\|`wrapup`，attempt: n/3 | `_EMPTY_NUDGE` / `_WRAPUP_NUDGE` |
| `turn_end` | reason: `answer`\|`stuck`\|`garbled`\|`interrupted`\|`rounds_exhausted`\|`api_error`\|`fallback` | `_notice` + 各 `return`（**今天不存在，须新建**，见 §16.7） |
| `tool_round_rollback` | cause: `interrupt`\|`api_400` | `_rollback_tool_round` |
| `prune` | layer: `L0`\|`L1`\|`L2`，removed，reclaimed_tokens，cap | `_prep_context` |
| `compaction` | decision: `attempted`\|`skipped`\|`failed`，reason: `min_rounds`\|`max_per_session`\|`backoff`\|`summarizer_failed`，blocks，finish_reason | `compaction.py` 决策点 |
| `xml_toolcall_retry` / `thinking_only_retry` | round，count | `_warned_xml` / `_empty_retries` |
| `worker_dispatch` / `worker_settle` | task 摘要，结果摘要 | `_run_worker` |
| `interrupt` | cause，phase: `pre_round`\|`during_stream`\|`during_tools` | `interrupt_event` |
| `api_error` | status，body 摘要（沿用 `_dump_api_error` 口径） | `_safe_create` |
| `limits` | 决策时刻**实际生效**的预算旋钮快照（见 §4.7） | `_compact_k()` / `_prune_cap()` / `run_tools` cap / `_store_tool_args` |

**表里 `turn_end` / `notice` 是必须新建的事件，不是已有事件。** 今天终局信息以两种**不可用作断言身份**的形式存在：`_notice(level, message)` 的自由中文文案（`runner.py:392`，17 个调用点，**没有 code**）与 `debug_filtered.txt` 里的文本标签（`runner.py:543`、`runner.py:683` 的 `_dump_filtered("turn_end:stuck", ...)`——而 dump 在抽取 cassette 时本来就要丢掉）。理由与三处最小代码改动见 §16.7。

### 4.3 emit_event 分级（避免 trace 膨胀）

`on_event` 里绝大多数是流式渲染增量。但**流式的真相来源已经是 `llm_chunk`，在 emit_event 里再录一遍是纯重复**。按级别过滤：

| 级别 | 内容 | 默认 |
|---|---|---|
| `assert` | `notice` / 全部控制流 / `tool_calls` / `tool_result` / `self_check` / `error` / `worker_*` / `token_usage`（每轮一次） | **记** |
| `stream_summary` | 每轮一条派生摘要：`{round, thinking_chars, response_chars, emitted_response_chars, first_token_ms}` | 记 |
| `stream` | 逐 chunk 的 `response_chunk` / `thinking_chunk` | 不记 |

关键：**用 `stream_summary` 而不是「不记」**。Bug 32（回合结束零输出、agent 静默停止工作）的可观测行为正是「这一轮到底有没有东西上屏」——`emitted_response_chars` 就是它的断言量，而它不需要逐 token 的体量。`trace_level` 写进 `meta.json`，需要时把整个会话录成 `stream` 级。

### 4.4 工具结果与参数的两通道

`run_tools` 里同一个结果存在两个不同字符串：

- `emit` 出去的是**完整**结果（`runner.py:316`）
- 进 `conv` 的是**按预算截断**的版本，cap 6000（本地）/ 10000（云端）（`runner.py:318-325`）

两者都要录，因为用途不同：
- `result_full` → 工具级断言、后续 B 类工具重执行比对
- `result_stored` → **参与 §6 的 fingerprint**（模型看到的是这份），也是 A 类回放要吐回去的那份

只录一份必然出问题：只录 full，回放时 fingerprint 对不上；只录 stored，工具级断言被截断信息误导。

同理 `write_file` 的 arguments 有两个版本：`_store_tool_args`（`runner.py:187`）把 `content` 抹成 `[omitted N chars]` **存进 conv**（所以它出现在 messages 里、参与 fingerprint），而 `tool_call` 事件里必须留**原始完整** arguments 供 B 类真执行。

**回放侧的自检闭环**——两通道真正吃劲的地方：A / C 类回放时吐回的是 `result_full`，**不是** `result_stored`，然后让 `run_tools` 用**当前**的截断规则重新算一遍得到 `result_stored_now`，再与录制的 `result_stored` 比对：

| 比对结果 | 归因 | 策略 |
|---|---|---|
| 一致 | — | 通过 |
| 不一致，且 `limits` 里 cap 变了 | `truncation_budget_changed` | **硬失败**并点名旋钮（§4.7） |
| 不一致，cap 没变 | `truncation_logic_changed`（我们改了截断逻辑） | 报 drift；strict 模式失败 |
| 无 `truncation` 记录（旧 trace） | 无法归因 | 显式报「无法归因」 |

好处：「模型看到的内容量变了」被**正向检出**，而不是等下游指纹失配再反推——失配时你分不清是内容变了还是预算变了。B 类真执行走同一条闭环：真结果 → 重新截断 → 比对。

### 4.5 为什么必须录 chunk 序列，而不是拼好的结果

绝大多数 bug 在**流式解析层**，只存一份 `content` 字符串一个都回归不了：

| Bug | 依赖的流式细节 |
|---|---|
| 7 空 `choices` 尾块 → IndexError | `choices=[]` 且带 usage 的尾块 |
| 8 本地 Qwen 的 XML 工具调用 | `reasoning_content` 分片里的 `<tool_call>` |
| 9 合成 tool_call id → 400 | tool_calls 分片按 `index` 拼接、id 只在首片 |
| 27 `finish_reason=length` 截断 | finish_reason 必须在 `llm_end` 里 |

存储优化：空 delta 不记；只记 `reasoning_content` / `content` / `tool_call` 分片 / `usage` / 尾块标记。必须保留的边界情形：usage-only 尾块、`choices=[]`、`tool_calls` 按 index 分片、finish_reason。

### 4.6 并发工具的事件顺序

**先纠正一个前提**：`run_tools` 的并发阶段**只收集结果、不发射事件**（`runner.py:245-266`），结算阶段 `for tool in use_tool_calls` **按原调用顺序**逐条 `emit`（`runner.py:294-296`），且 `tool_result` 事件**已经带 `call_id`**（`runner.py:305`、`runner.py:316`）。所以「事件序列因并发而不确定」在现有实现下不成立——发射顺序本来就是确定的。

真正要定的三件事是：

1. **耗时/时间戳不进验收口径。** 不确定的只有 wall-clock。验收标准表述为「**事件类型序列一致**」，显式排除 `ts` / `duration_ms` / `latency`。（按 `call_id` 关联仍然做——不是为了排序，是为了让分析工具不依赖全局位置，以及让 worker / C 类事件的归属明确。）
2. **回放时 B 类工具必须串行执行。** 录制时可以并发（`_SYNC_MAX=8`），但两个 B 类工具写同一路径时并发交错不可复现。回放按录制的**发起顺序**串行执行 B 类，A / C 类无所谓。这条比 call_id 更要紧，是新增的确定性要求。
3. `worker_start` 在并发阶段就发射（`runner.py:282`），但它是顺序 `for t in delegated` 发射的，可保持现状；`worker_done` 在结算阶段发（`runner.py:314`），与工具结果同序。

### 4.7 预算旋钮是一等公民

本项目的 bug **集中在预算旋钮上，不在业务逻辑上**：

| 旋钮 | 位置 | 相关 bug |
|---|---|---|
| `result` 截断 cap 6000 / 10000 | `runner.py:321` | 上下文预算 |
| `_store_tool_args` 500 字符阈值 | `runner.py:189` | 与 Bug 21/22 的可回放性直接相关 |
| `COMPACT_MAX_TOKENS` 1500/3000、`_HARD` 2000/6000 | `config.py:77-79` | **Bug 27**（预算不足 → 整次压缩恒失败） |
| `trigger_ratio` / `block_tokens` / `keep_rounds` | `config.py:68-71` | **Bug 24**（前缀无界，预算形同虚设） |
| `dedup_min_chars` / `dedup_min_saved` | `config.py:63-64` | L0 去重 |
| `min_rounds` / `max_per_session` / `max_fails` / `max_backoff` | `config.py:82-87` | **Bug 28**（重试风暴 + 频率预算被废） |
| `_prune_cap()` | `context.py`（`runner.py:448` 调用） | Bug 26 / 30 |
| `_TOOL_TIMEOUT` / `_WORKER_TIMEOUT` / `_SYNC_MAX` | `runner.py:201-203` | 超时兜底 |

所以每个决策点落一条 `limits` 事件，记录**实际生效**的值——**不是默认值**：`_compact_k()` 会读 env 覆盖，必须记 resolve 之后的结果，否则报告会指向错误的数字。

**`limits` 事件还要带当时的 B 组记账值**（`_compact_count` / `_compact_token` / `_compact_last_turn` / `_dedup_saved` / `_compact_fails` / `_compact_retry_turn`，十来个整数）：这是 §5.3 里 **B 组还原自检唯一的 ground truth**——没有它，退避计数还原错了不会有人发现，而 Bug 28 恰是这类状态丢失。`_last_call_ts` 不进自检（按 kind 分桶的墙钟时间，属 §6.2 的 `tolerate` 槽位）。

两条用途：

1. **归因**：回放时任何内容差异先比 `limits`——旋钮变了就点名旋钮硬失败，而不是笼统报「prompt 漂移」（§4.4 的表）
2. **回归**：Bug 27 那类测试不再断言魔数，而是断言 `limits.max_tokens == 3000 → escalate → 6000` 这条**决策序列**。预算 bug 从此可测——这是本项目最缺的一类断言

---

### 4.8 trace 与 cassette 是两个产物，不是两个视图

前面把 LLM 请求/响应也写进了 `trace.jsonl`，容易让人以为「一个文件装两样东西」。**它们是两个产物，演进代价差一个数量级，必须分开定版。**

| | trace（诊断物） | cassette（回放输入） |
|---|---|---|
| 消费者 | 人（grep / 分析） | 回放 transport + 回归测试 |
| 生命周期 | 会话级，§9.1 定期清理 | **长期**：L2 导出后进 `tests/fixtures/replay/` 并入库 |
| 格式演进代价 | 低——旧 trace 读不了就读不了 | **高——旧 fixture 集体失效** |
| 版本轴 | `schema_version`（自由演进，**无兼容承诺**） | `cassette_format`（**契约**：只增不改，破坏性变更必须配 converter） |
| 能否丢 | 能（§12） | **不能**——它是被 git 依赖的测试输入 |

**生成方式：trace 是唯一实时写入的日志，cassette 从 trace 抽取。**

```
python -m tools.trace extract-cassette tmp/traces/<sid> --from-request 37 --out tests/fixtures/replay/<name>/
```

核心理由正是上表第 4 行：**旧 cassette 不受 trace schema 演进影响**——因为 cassette 是**抽取出来的产出物**，不是 trace 的一个视图。trace 规格改了只影响「下次抽取」，不会追溯性地弄坏已经提交的 fixture。

（备选是双写：同一批内存记录同时落两个文件。不推荐——两个 writer 有漂移风险，而且 cassette 的形状仍会被 trace 的记录结构牵动，解耦不彻底。抽取模型的代价是抽取器必须忠实，这由「抽出来的 cassette 能不能复现那次失败」自检兜住。）

**cassette 必须自足。** 进了 git 的 fixture 不会带着 trace 走，所以回放 + 断言需要的东西全都要进 cassette：

| 内容 | 为什么必须在 cassette 里 |
|---|---|
| `llm_request` / `llm_chunk` / `llm_end` | 指纹匹配 + 冻结响应 |
| 工具原始 arguments + `result_full` | B 类真执行 / A、C 类回放吐回（§4.4） |
| `truncation` 记录 + `limits` 快照 | §4.4 的自检闭环**在回放时**要跑，缺了无法归因 |
| checkpoint | seek 到第 i 个请求（§5） |
| `cassette_meta`：`format` / **`workspace_root`** / `completeness` / **`playback_mode`** | 版本契约 + **路径归一化要用的录制侧根**（§6.5）+ 拒绝降级输入 + **回放走哪条路**（见下） |
| **`assert` 级事件**（§4.3） | 断言面：`turn_end` / `notice` / 控制流 / `turn` 标签 |

即 **cassette = assert 级事件 + 复现数据 + 寻址数据 + 元信息**；更丰富的东西（`stream_summary`、debug 级、原始诊断 dump）留在 trace 里，抽取时丢弃。

**完整度声明（必须有）。** 降级来源不止一种（旧会话没有 LLM 体、`write_file` 参数被抹掉、C 类被 stub），回放器必须能**拒绝**而不是静默补空：

```json
"cassette_meta": {
  "format": 1,
  "workspace_root": "C:\\Users\\...\\agent",   // 录制时的根：归一化用它，不是本机 WORK_DIR（§6.5）
  "playback_mode": "fingerprint",              // "fingerprint" | "positional"，必填，缺失即报错（见下）
  "completeness": {"llm": "full", "tools": "partial"},
  "degraded_reasons": ["args_omitted:write_file", "c_class_stubbed:browser_act"]
}
```

硬规矩：**`llm: "missing"` 的 cassette 不允许进入 LLM 回放路径**（§8.1 矩阵里 `body_missing` 那两格只能用于分析）。否则测试会在一个「模型从未回过话」的世界里通过。

#### 回放模式是字段，不是回放器的推断

§16.6 把回放分成两条路（人工 fixture 走 §6 指纹匹配，L3 收缩产物走**按位消费**），并写明「两条路径不能混用」。但原文只写到口号为止——**没有任何机制拦得住走错**。所以它是 `cassette_meta` 的**必填字段**：

| 字段 | 取值 | 为什么要显式 |
|---|---|---|
| `playback_mode` | `"fingerprint"` / `"positional"` | 决定回放器**是否做请求指纹比对**。靠推断（如「有 shrink 记录就按位」）正是原则①禁止的那种事后反推 |
| `derived_from` | `positional` 必填：源 trace 标识 + ddmin 运行记录 | 「为什么这条 fixture 只能用弱模式」必须可见，否则以后没人敢动它 |
| `expected_assertion` | `positional` 必填：断言身份（`turn_end` 原因 / 异常类型 / 断言 id） | §16.4 要求 `FAIL_with(expected_assertion)`——**ddmin 的谓词需要它作为输入**，所以它必须是数据，不能是测试文件里的注释 |

**两个方向的错配都必须硬报错，不能静默**（§16.6 已点出这两种失效模式，这里给它们机制）：

| 错配 | 后果 | 处理 |
|---|---|---|
| `positional` 的 cassette 走指纹匹配 | 必然失配 → 一堆莫名其妙的 skip，没人看得出原因 | **加载即报错**，告诉用户这条 fixture 只能按位回放 |
| `fingerprint` 的 cassette 走按位回放 | **静默关掉漂移检测**——prompt 漂到天边，测试照样绿 | **加载即报错**；要按位回放必须由 `shrink` 产出新 cassette，不许用开关绕 |

并且 `playback_mode` **缺失即报错，不给默认值**：两种默认都错——默认 `fingerprint` 会让 L3 产物加载后集体炸（噪声），默认 `positional` 会让所有人工 fixture 悄悄降级（危险）。

**`expected_assertion` 只能有一个真相源**：fixture 字段是真相源，测试**读它**，不许测试里再写一份。两份各写必然漂移，而漂移之后 fixture 会「仍然失败但在测别的 bug」（§16.4 第 1 条）。

**报告里两种模式必须视觉可辨**：`positional` 的回放报告要显式打「按位回放（**未做请求指纹比对**，仅保证响应序列）」。否则 CI 上一条绿的 L3 fixture 和一条绿的全保真 fixture 长得一样，**降级的保证会被当成完整的保证读**。

边界说明：这套机制防的是**不小心走错路**，不防「有人故意手改字段」。后者不是字段设计能解决的，靠 §9 的入库审查。

抽取后的自检与 §9 的 L2 导出是同一道闸：**抽出来立刻回放一次，能复现那次失败才允许导出。**

---

## 5. checkpoint：warm prefix 的寻址层

**「从第 i 次请求起回放」≠「取 messages 前缀」。** 要复原的是运行时状态。

### 5.1 状态清单（已核对代码）

| 组 | 内容 | 位置 |
|---|---|---|
| A conv 级 | `messages` / `size` / `_user_turn` / `_empty_retries` / `_archive` | `conv` |
| B 压缩记账 | `_compact_count` / `_compact_last_turn` / `_compact_token` / `_dedup_saved` / `_compact_fails` / `_compact_retry_turn` / `_last_call_ts`（按 kind 分桶） | **两处，见下** |
| C 薄壳 | `_archive` / `_compact` | `loop_agent_v2._session_state` |
| D 进程级 | `control.interrupt_event` / `config._compact_k_cache` / `config.client`（headroom 包裹层可能带 session 态）/ `events._headless_streaming` / `ds_web._executor` | 模块全局 |
| E 工作区 | manifest：路径 + mtime + size + sha256（小文件）/ mtime+size（大文件），只列 agent 触碰过的路径 | 文件系统 |
| F 前缀稳定性 | `stable_prefix_hash` | `headroom.jsonl`（回放时指向 trace 目录） |

**B 组是坑，单独说：** `cstate(conv)`（`compaction.py:65-75`）的语义是「`conv["_compact_state"]` 是 dict 就用它，否则用 `conv` 自己」。所以同一份记账状态**可能在 conv 上，也可能在薄壳的 `_session_state` 上**。快照必须两处都抓，否则 seek 之后退避计数和次数预算是错的——而 Bug 28（重试风暴 + 频率预算被每轮归零废掉）正是这类状态丢失的产物。

**D 组的 `config._compact_k_cache` 必须显式重置**：它是惰性 env 缓存（`config.py:38-90`），import 后改 env 读不到，测试已经踩过；seek 回放同理。

### 5.2 存储：阶段 1 全量快照，delta 后置

**阶段 1 用全量快照。** 但要说清量级：单份 ≈200KB 不是**一个会话**的体积，是**每次请求一份**——40 请求的会话原始量级在 8MB 上下。所以配套两件事，而不是立刻上 delta：

1. `checkpoints/` 走**按帧组压缩 + 字节偏移索引**，**不是单一 zstd 流**（理由见下）
2. 把「单会话 < 5MB」当**阶段 4 的判据**：分组压缩后仍在预算内 → 可能永远不需要 delta，省掉一整套版本控制逻辑

（快照**时机**是另一件事，见 §5.3。「每次请求一份」对，但 ≈200KB 是**请求体**的量级，不是**快照增量**的量级——checkpoint 不必存 messages。）

**为什么不能单一 zstd 流：seek 是 L3 的热路径。** 单流不可随机访问——seek 到第 N 个快照必须从流头解压到那儿。而 L3 的 ddmin 最坏要跑 **O(n²) 次重放**（§16.4 第 3 条），每次重放都要 seek 到某个起点，于是整体解码量被推到 **O(n³)**。会话越长越慢，正是这个结构造成的。

**分组粒度是真实取舍，方向必须写明：**

| 方案 | seek 成本 | 压缩率 |
|---|---|---|
| 单一流 | O(N) 解压 | 最好（跨快照冗余全吃到） |
| **每用户轮一个帧组**（推荐） | 组内最多解压 K 个快照（K = 该轮请求数） | 好——同轮内快照近乎相同，冗余仍留在组内 |
| 每快照独立帧 | O(1) | 最差——200KB 的 JSON 单独压，跨快照冗余全丢 |

**默认按用户轮分组**（或每 K=5 个请求一组）：组内保有几乎全部冗余，seek 的最坏解压量**有界且与 N 无关**。zstd 的每个 frame 本身可独立解码（frame 之间**不允许**互相引用），所以只要写的时候每组结束 `flush` 一次收尾当前 frame、记下字节 offset，从该 offset 起新建 reader 即可独立解出——**不需要 zstd seekable format 那套 seek table**。（真要用 seekable format，也得先确认 python-zstandard 是否暴露该 API，**别按假设写**。）

索引落 `checkpoints/index.json`：`request_index → (frame_file, offset, comp_len, raw_len)`。

**索引算错不会静默。** 本节的「还原自检」正是这里的安全网：从某偏移解出的快照要和**录制时该请求的 body** 对 hash，对不上就报错退出——索引错会立刻红，不会带着错状态继续跑。

**一条推广到其他产物的规则**：trace / cassette 是行式 JSONL，L3 的按位回放同样要从**中间某个请求**起读，所以它们也需要随机访问。做法更简单——**不压缩**，另建**行偏移索引**（`记录号 → 字节偏移`）即可 O(1) 跳转。这里省下的复杂度比压缩那点收益值钱。

**阶段 4 才引入 delta，且必须配套两条自检**（评审里最正确的一条：delta 链出错会让 seek **静默错位**）：

- delta 表达：`ops: [{drop: [i,j]}, {append: [...]}, {replace: [i,j], with: ...}]`——prune 会删中间消息，纯追加语义不成立
- **强制 base**：每 N 轮（默认 10）或 delta 链长 > 20 时插一个 base
- **还原自检（强制，不是尽力而为）**：seek 后用 base+delta 还原出 messages，与**录制时该请求的 body** 做 hash 比对。**每个请求体本来就完整录了**（§4.2），所以这个比对是免费的——冗余在这里恰好是安全网。比对不过 → seek **报错退出**，不许带着错状态继续跑

### 5.3 快照时机（本轮定）

**结论：每个请求一份，抓在 `_prep_context` 之后、`_safe_create` 之前。** 落点就两处——`runner.py:448`→`450`（工具轮）与 `runner.py:595`→`596`（最终流式阶段的 12 轮）。另加一个 **turn 末终态**快照（该轮全部结算动作之后），补上「最后一个请求没有后继」的那一格。

快照点集合 = `{pre_request[i] for 每个请求 i}` ∪ `{post_turn}`。每条显式带 `request_index` / `turn` / `tool_round`（§15 的三个编号），seek 目标按**字段寻址**，不靠位置猜。

**为什么「只在用户轮边界抓」不是精度差一点，而是不可用**，三条：

1. **`_prep_context` 每个请求前都跑**（`runner.py:448`、`runner.py:595`），它改的正是**本轮请求真正要发出去的东西**：L2 prune 从中间删消息、`render_prune_notice` 改当前问题前缀、L0 去重与 L1 摘要改 `_dedup_saved`/`_compact_*`。轮边界快照与轮内第 3 个请求的实际状态可以差很多——**差的这部分恰好是最常见的现场**。
2. **「从轮边界向前重算到请求 i」不可行**：`_prep_context` 可能触发摘要器 LLM 调用，回放时这些调用来自 cassette 的**同一条流**（§4.8 顺序消费）——为重建状态而重放它们会**打乱主游标**；而且压缩决策依赖当时的 token 计数与退避状态，本身不可复算。
3. **没有 `pre_request[i]` 快照，那个候选根本跑不了**。ddmin 的 seek 目标就是「第 i 个响应之后」（§16.3 的 positional spine），缺快照等于砍掉一块搜索空间，§16.1 的 **1-最小**保证随之失效——不是精度差一点，是**结论口径变假**。

**成本 objection 在结构上不成立：messages 不必进快照。**

- 请求体在 §4.2 已**完整录下**（§5.2 的还原自检正是靠这份「免费冗余」），所以 checkpoint 里 messages 只需一个 **hash 指针**；只有 `body_missing` 的降级 cassette（§8.1 矩阵）才内联 messages。这是原则①（存数据，不存规则的产物）的又一应用：**同一份数据不存两遍**。
- 真正必须单独抓的**增量**只有 B/C/D/E：B 组十来个标量、C 组两个字段、D 组几个模块全局、E 是「agent 触碰过的路径」manifest——**与会话规模无关**。
- 写时按内容 hash 去重：连续两个请求状态完全相同（很常见）就只写一份 blob、两个索引指向它。不用任何 delta 机制就拿到大部分收益。

**「无缺口」是可证的**：所有改状态的点——`_prep_context`（448/595）、`run_tools` 的 conv 追加、`_rollback_tool_round`（`context.py:95-104`）、`_record_usage`（`runner.py:462`/`610`）、`_store_tool_args`（`runner.py:187-197`）——后面紧跟的要么是下一个 `pre_request[i+1]`，要么是 `post_turn`。所以状态变完总会被某一格接住。

**顺带补掉一个自检缺口**：§5.2 的还原自检只覆盖 messages（A 组），B/C/D 组**没有 ground truth**——退避计数还原错了不会有人发现，而 Bug 28 恰是这类状态丢失。修法：§4.7 的 `limits` 事件**同时携带当时的 B 组记账值**（十来个整数，每条只多几十字节），restore 自检比对「messages hash + B 组记账」两项，都过才算还原成功。

---

## 6. 回放匹配：顺序消费 + 跳过窗口

游标模型，而不是哈希查表（哈希会因为动态时间戳、记忆注入、nudge 注入而全面 miss）。

### 6.1 指纹与漂移判定

```
loose       = sha256(canon(kind, model, params, messages))   # 匹配用（故意宽容）
strict_hash = sha256(raw(kind, model, params, messages))     # 仅作快速预检，**不是判据**（§6.6）
```

- **匹配只用 `loose`**——只有它决定游标走位
- `loose` 命中时：先看 `strict_hash` 是否相等；相等 → 确定无漂移，直接跳过 diff；不等 → 跑**结构化 diff 并按 volatile 槽位归类**（§6.2 槽位表 + §6.6 判据）
- 窗口内无 `loose` 命中 → 报错 + diff

**两个指纹都在回放时算，都不落盘**（§6.4）。这条设计消解「规范化容易漏 → 频繁 skip」：`loose` 是**故意宽容**的键，宽容掉的东西不是丢了，而是转到槽位归类的漂移报告通道。

### 6.2 规范化规格

**参与 `loose`（白名单，规范化后）**

| 项 | 规范化 |
|---|---|
| `kind` / `model` | 原样 |
| params | 只取语义项：`temperature` / `top_p` / `max_tokens` / `reasoning_effort` / `extra_body.thinking.type` / `tools_sha256`。传输项（`timeout` / `stream_options`）剔除 |
| messages | `role` + 规范化后的 `content` + `tool_calls`（name + 规范化 arguments，**按 name 排序**以消除并行调用顺序抖动）+ `tool_call_id` |
| `tools` schema | 只进 **hash**，不进正文 |
| 压缩摘要块 | 原样参与（见下方「跨会话」） |

**volatile 槽位表（从 `loose` 替换成占位符；原值永远保留在原始记录里，`strict` 不另存一份）**

「策略」列决定 §6.6 的判定强度：
- **`reinject`** = 该值在回放时**必须基于录制值重新注入**，所以判定时**要求值也相等**（做不到就是 bug）
- **`tolerate`** = 本质不可复现，允许不等，但差异**必须进报告**

| 槽位 | 替换为 | 策略 | 依据 / 重注入方式 |
|---|---|---|---|
| `ts` 时间戳 / 日期文本 | `$TS` | **reinject** | 固定 `TRACE_EPOCH`；实际注入的串落 `meta.json`（§6.4），回放按它重注入 |
| `memory` 记忆注入整块 | `$MEMORY` | **reinject** | 标记 `"[长期记忆"`（`runner.py:426-433`）。`memories/` 是**外部状态**，回放必须重注入**录制的那段文本**，不许重新读盘 |
| `nudge` | `{"$nudge": "empty"\|"wrapup"}`, attempt → `$N` | **reinject** | `_EMPTY_NUDGE` / `_WRAPUP_NUDGE` |
| `trim_notice` | `{"$trim_notice": "$N"}`（数字归一） | **reinject** | `_PRUNE_NOTICE` + `render_prune_notice` 的**累加**语义（`context.py:8,141,147`；Bug 26） |
| `workspace_path` | `$WORKSPACE/...` | **reinject** | 两侧各自根表，见 §6.5 |
| `ids`（session / request / message id） | 剔除 | —（不进键） | 传输层签发，与语义无关 |
| `timing` 耗时 / 时间串 | `$MS` / `$TMP` | **tolerate** | 墙钟时间本质不可复现 |

**明确不进键**

- `conv["size"]`——它是状态不是请求体，本来就不会出现在 messages 里
- usage / token 计数——只存在于事件里，不进 messages。若某天后端把 usage 回显进 messages，那是 bug，应当**报错而不是静默归一**
- **fingerprint 本身**——规范化规格的产物，一律不落盘（§6.4）

另一个刻意**不**归一化的东西：**工具结果（`result_stored`）原样进 `loose`**。把它归一成「摘要 + cap 标记」会让键更稳，但代价是容忍了工具输出的一切变化——而「模型看到的工具结果变了」恰恰是最该被发现的。所以保持原样，预算类变化交给 §4.7 的 `limits` 归因。**该发现的东西不归一化，只做归因。**

**跨会话说明**：压缩摘要内容参与 `loose`，因为它是模型生成的实义内容。这意味着 cassette **不支持跨会话复用**（同一份 cassette 只对产出它的那次会话有效）。这是有意的取舍：跨会话复用需要把摘要也归一化，那会把「压缩结果变了」这类真问题一起掩盖掉。

### 6.3 签名分桶的 FIFO 游标

游标不止一条：**按请求签名分桶，每个桶一条独立的 FIFO 游标**。签名只取 `kind`
（`replay.request_signature()`），不含 model / params / messages——这些由 §6.1 的 `loose`
负责判定，桶只决定「从哪条流里按顺序取」。

```python
buckets = { signature: [录制位置, ...] }   # 升序
at      = { signature: 已消费到的下标 }
W       = 8                                # strict 模式 W = 0（锁步按桶成立）
```

- 现场请求按自己的 `signature` 只进**本桶**，在 `[at[sig], at[sig]+W]` 内搜 `loose` 命中。
- 命中本桶下标 `k`：`at[sig]..k-1` 记为 **skipped**，前移，写 `replay_skip` 事件
  （含被跳过那几条的 loose / strict 差异摘要），`at[sig] = k+1`。
- 本桶窗口内无命中：抛 `NoMatchInWindow`（点名签名桶与已消费数），输出两侧 diff。
- 跑完后**本桶里没人取的录制请求要进报告**（`report.unconsumed`）：分桶让「代码合法地
  删掉一个调用」不再让后续整体错位，代价就是那条录制的响应没人取——不显式报出来，
  「录制 5 条、现场只用了 3 条」会被绿色通过掩盖。

**为什么必须分桶**：混 kind 的会话（chat 与摘要器 summary 交错）里，全局单游标会把
「一次 kind 交错」算成整体错位——现场先发 summary、录制里 summary 在第 2 位，窗口 0 的
全局游标第一步就 `NoMatchInWindow`，可两条响应其实都在。分桶后各 kind 各走各的 FIFO，
「跨 kind 重排调用」与「合法删掉某 kind 的调用」都自然成立；只有「凭空新增调用」
（现场比录制多要一个同签名响应）仍必须失败——cassette 里没有多余响应可给。

**skip 必须计入报告**，否则「跳了 30 条也算过」就是假绿。断言钩子：
- 钉死用例：`assert skips == 0`
- 漂移观测：`assert skips <= N`
- 删调用观测：`assert len(report.unconsumed) == N`

副产品（当一等功能做）：**skip 的位置就是 prompt 漂移的位置**，把「代码改动导致发给模型的东西变了」从隐性变成显性 diff。默认只报告、不失败——否则每调一次提示词都要重录，两周后没人维护。

### 6.4 指纹在回放时实时计算，不落盘

`llm_request` **不存 fingerprint**（§4.2），只存原始 messages + params。回放时用**当前**规范化规格，对「录制侧」和「现场侧」分别实时算 `loose` / `strict` 再比对。

理由是规格必然演进：一旦发现某个字段该剔除，落盘的指纹会**全部失效、必须重录**——而重录意味着你再也回放不了历史现场，正好废掉整套机制存在的理由。存原始数据、回放时算，规格就能独立演进，代价只是一次 hash（可忽略）。

由此得到贯穿全文的原则 ①：

> **存数据，不存规则的产物。**

推论与落点：

| 是「数据」→ 落盘 | 是「规则的产物」→ 不落盘 |
|---|---|
| `TRACE_EPOCH` 的**值**：会话真实时间、实际注入的日期串（`meta.json`） | 「把日期串替换成 `$TS`」这条规则 |
| 原始 messages / params / `result_full` / `result_stored` / `truncation` 记录 | `fingerprint_loose` / `fingerprint_strict` |
| `limits` 事件里**实际生效**的旋钮值 | 由旋钮推导出的任何派生量 |

版本由此**分成两个、彼此独立**：
- `schema_version` —— 只管**数据布局**，变了就得 migrate（§8）
- `canon_version` —— 只管**规范化规格**，变了**不需要重录**，只影响重放时的计算结果（记在 meta 里便于排查，但不参与匹配）

### 6.5 路径归一化：两侧各自提供根表

§6.2 原来那句「overlay 路径 → `$WORKSPACE`」**不够**，它只描述了回放侧。录制侧根本没有 overlay：B 类工具当时跑在**真实 `WORK_DIR`** 上，所以录下来的 messages 与工具结果里带的是**真实绝对路径**。只在回放侧替换，两侧的 `loose` 必然对不上。

**规则：两侧都存原始值，两侧都在「算指纹时」归一化。** 这是原则 ① 的推论——归一化是规则的产物，不落盘；落盘的是原始路径（否则回放就没法用真路径重执行 B 类工具）。

归一化 = 一组 `(根, 占位符)` 映射，**按最长前缀优先**替换；两个上下文各自提供自己的根表：

| 上下文 | 根表 |
|---|---|
| 录制 | `WORK_DIR → $WORKSPACE`、`_PROFILES_DIR → $PROFILES`（`config.py:10-11`）等 |
| 回放 | `overlay_root → $WORKSPACE`（B 类被重定向到 overlay）；C 类相关目录被 stub，通常不进键 |

三条实现约束：

1. **录制侧的根必须落进 cassette**（`cassette_meta.workspace_root`），回放时用**录制时的根**归一化，**不是**用本机的 `WORK_DIR`。理由很实际：fixture 入库后可能在另一台机器、另一个检出目录、甚至 CI 上回放——`C:\Users\A\...\agent` 录的路径在 `C:\Users\B\...\agent` 下永远匹配不上。换成「用录制的根剥出相对结构」，立刻**跨检出可移植**。
2. **必须按最长前缀优先。** §7.2 把 overlay 根放在 `tmp/overlay/<sid>/`，也就是**在 `WORK_DIR` 里面**，于是同一条路径有两种合法替换：
   - `WORK_DIR/tmp/overlay/<sid>/a.py` → 先替 `overlay_root` = `$WORKSPACE/a.py`
   - 同一条 → 先替 `WORK_DIR` = `$WORKSPACE/tmp/overlay/<sid>/a.py`

   两者不等。最长前缀优先（等价于「剥离匹配到的最长根」）是确定性的解，**必须用测试钉住**——否则它会表现成随机的 skip，极难排查。
3. **B 类的正向重写不改变消息。** 回放时只重写**执行入参**（§7.2 第 1 条，落点 `_call_tool`），**不碰**追加进 conv 的那条 assistant `tool_calls` 消息——它来自录音的 LLM 响应，本来就带录制时的原始路径。碰了反而制造不对称。

### 6.6 strict 模式的正确判据

§11 原先写的「`strict` 回放 `skips == 0` 且 `loose == strict`」**是错的**，而且错得有代表性，值得记下来：

`loose == strict` 只在**规范化什么都没做**时成立。只要 messages 里出现任何一个易变段（时间戳、记忆注入、trim 提示……），两侧 hash 必然不等——**哪怕回放完美无缺**。于是这条判据：

- 在**正常**会话上（有记忆注入或日期）**必然假红**
- 在**没有**易变段的会话上**恒真**，而此时它并没有给出 `loose` 匹配之外的任何信息

一句话：它测的是「归一化有没有生效」，不是「回放像不像」。**hash 之间的相等关系根本承载不了保真语义**——这也是它无法回答「具体哪一段变了」的原因。

**正确判据**（`strict` 模式下四条全中才算通过）：

1. `loose(录制) == loose(现场)`——模型可见的内容骨架没变
2. 其余差异**全部落在声明的 volatile 槽位内**（§6.2 的槽位表）
3. 其中 `reinject` 槽位**值也必须相等**——原值已落盘、能重注入，做不到就是 bug
4. `tolerate` 槽位允许不等，但**必须进报告**（附录制值 vs 现场值）

所以 `strict` 的正确定义不是「再算一个 hash」，而是「**跑一次结构化 diff，把每个差异归类到槽位**」。`strict_hash` 只保留一个用途：**快速预检**——相等则可跳过 diff（此时必然无漂移），不等则进入上面的归类流程。**它不是保真判据。**

**strict 不适用时要显式拒绝。** 若 cassette 含 `tolerate` 槽位且现场值不同，strict 模式应报「**strict 不适用：`<槽位>` 不可复现**」，而不是抛一个隐晦的指纹不匹配——后者会让人花半天去查一个本来就不该用 strict 的场景。

**哪些易变值真的能逐字复现？** 把边界说清楚，别承诺做不到的事：

| 易变值 | 能否逐字复现 | 机制 |
|---|---|---|
| 日期 / 时间文本 | ✅ | 回放时**重注入录制的串**（原值在 `meta.json`） |
| 记忆注入内容 | ✅ | 重注入录制文本；**不许重新读 `memories/`**（它已经变了） |
| `tool_call_id` | ✅ | 回放响应来自 cassette，id 随之冻结；`runner.py:513` 的 `tc{idx}` 兜底只在其上派生 |
| `[Context trimmed]` 累计计数 | ✅ | 由录制的 `limits` + 同序 prune 决策重算；`limits` 变了会被 §4.7 提前拦下 |
| 工具结果里的**墙钟耗时** | ❌ | 本质不可复现 → 只能是 `tolerate` 槽位 |

---

## 7. 工具三类

| 类 | 判定 | 回放策略 | 本项目的工具 |
|---|---|---|---|
| **A 只读且确定性** | 不改外部状态，结果可冻结 | 回放录制结果（吐 `result_full`，见 §4.4） | `read_file` / `list_dir` / grep / 代码索引 / `recall` |
| **B 有副作用但可隔离** | 改动可限制在 overlay 内 | overlay 工作区**真执行** + 前后快照 | `write_file` / `edit_file` / `shell_tools` / `mark_coin` |
| **C 外部或不可逆** | 依赖网络/浏览器/人 | 默认 stub 回放结果，显式 opt-in 才真跑 | `browser_act` / `ds_web` / `tavily` / vision / clipboard / `delegate_task` / `qq_bot` |

### 7.1 分类声明

`TOOL_CLASS` 表在代码里声明，每项至少含：`class` / `path_args`（哪些参数是路径）/ `rewrite_scope`。

**未分类的工具 → 报错**，不是默认按 A 处理。否则新加一个工具静默走 A，产出假绿——这是整套机制最容易腐化的地方。

### 7.2 overlay 路径映射

不重写路径就等于**回放时直接写真实工作区**。三段式：

1. **正向重写（入参）**：回放时把 B 类工具 `path_args` 里的路径重写到 overlay 根（`tmp/overlay/<sid>/`），保持相对结构。
   - **必须在工具分发缝做，不能在各工具内部做**：工具自己会 `os.path.abspath(os.path.join(WORK_DIR, path))`（如 `vision._resolve_image_path`），进到工具里再改就晚了。落点是 `_call_tool`（`runner.py:211`）的入参层。
   - `shell_cmd` 这类**命令串里嵌路径**的参数无法结构化重写 → 按 `WORK_DIR` 子串做字符串级 best-effort 替换，并在 trace 里标 `rewrite: best_effort`。**做不到精确就显式标记，不能假装做到了。**
2. **反向映射（出参）**：结果里的 overlay 路径必须**在进入 `conv` 之前**换回逻辑路径。否则回放时进 conv 的内容与录制时不同 → messages 变了 → §6 指纹连锁失配，而且断言拿到的路径不可比。
   反向映射是 **best-effort**（`shell_cmd` 输出、报错文本里嵌的路径都可能漏）。所以 §6.5 的 `$WORKSPACE` 归一化是**第二道防线**：漏网的路径差异在指纹层被吸收，不会伪装成内容漂移。
3. **跨类污染**：A 类读同一路径时，先查 overlay，有则读 overlay，无则回放录音。这条是第二容易出假绿的缝。

配套：overlay 根在 `loose` 指纹里归一成 `$WORKSPACE`（§6.2），两道防线一起上。

### 7.3 其余三条硬规矩

1. **回放时 B 类串行**（§4.6 第 2 条）——并发写同一路径不可复现。
2. **时钟**：`get_date` 逻辑上属 A 但不确定。回放固定 `TRACE_EPOCH`，并在请求体里替换动态日期段，否则每条录音的 diff 都不同、永远匹配不上。
3. **`delegate_task` 用整体 stub**（§13 已定项第 1 条）：它返回录制下来的 `result_full`，子树的 LLM 调用不录、不回放。子 agent 内部的失败排查走子 agent 自己的会话 trace，不做 sub-cassette。

---

## 8. 迁移层：L0 与 L1 之间

```
python -m tools.trace_migrate conversations/*.json --out tmp/traces/
```

### 8.1 可回放比例：两个正交维度，不是一条阶梯

旧会话缺的东西是**正交**的：LLM 响应体和工具参数互不相干。用一条三级阶梯会把「LLM 体全缺但工具参数完整」误判成不可回放。改成 2×3 矩阵，**按轮次（不是按会话）**统计：

| | `args_ok` | `args_partial` | `args_omitted` |
|---|---|---|---|
| `body_ok` | **完全可回放** → 直接产出可执行用例 | 可回放 LLM 侧，B 类降级为 stub | 可回放 LLM 侧，B 类不可回放 |
| `body_missing` | **可重建调用序列**（分析用，不能回放 LLM） | 仅可分析 | 不可用 |

- `body_ok` = 有 LLM 请求体 + 流式 chunk + `finish_reason`
- `args_ok` = 每个工具调用的**原始** arguments 都在（`write_file` 的 content 未被 `_store_tool_args` 抹掉，`runner.py:187`）
- **统计单位 = 用户轮**（§15）。一个用户轮内有多个 LLM 请求、多个工具调用，所以两轴都要在轮级聚合：
  - `body_ok(轮)` = 该轮内**全部** LLM 请求体都在；缺任一即 `body_missing`
  - `args_ok(轮)` = 该轮内**全部**工具调用原始参数都在；部分在 → `args_partial`；一个都没有 → `args_omitted`
  - 轮级结论之外还要能**下钻**到请求级 / 工具调用级，否则「哪一轮可回放」与「哪个工具不可回放」都答不出

报告必须给：各格计数 + **是哪些工具导致降级**（`write_file` 占多少）。后者才真正指导投入：如果失败轮次集中在 write_file 密集的轮次，那 **L1（LLM 回放）对它们帮助有限，得先做 L2**——这个结论只能从迁移报告里读出来。

### 8.2 迁移注意

- 复用 `tui.is_conversation_file()` 的口径（`tui.py:45`）：只收 `chat_*.json`，排除 `.archive.json` / `.bak`（Bug 31 踩过：折叠档案 sidecar 被当成会话加载）
- `.autosave*.json` 单独处理——它是中断现场，反而更有价值
- 迁移器写的是 **trace**（共用 trace schema + `schema_version`），同时按 §8.1 的矩阵**抽取 cassette**，盖上 `cassette_format` + 完整度声明（§4.8）。注意旧会话抽出来必然是**降级 cassette**（`llm: "missing"`），回放器必须据此**拒绝**，而不是静默 stub
- 每份产出 `migration_report.json`（§8.1 矩阵 + 降级 reason）
- 旧会话**没有 `limits` 记录**：其降级判定只能靠「参数是否被抹掉」这类可观测量，不能靠旋钮归因（§4.4 表的最后一行「无法归因」就是这个场景）

**迁移的价值不是「能回放」，是三件**：① 113 份真实会话变成有结构可查询的语料（现在只能 grep）；② 从语料里挑出值得变成测试的失败轮次（L3 的输入）；③ **量出可回放比例这个天花板**——先量再投，避免 L1 做完才发现历史接不上。

---

## 9. Redaction 两级

**L1 — 写入时，始终开**
原则：**只抹掉 harness 会在回放时重新提供的值**（所以保真不受损）。范围：env 密钥（`DS_KEY` / `VISION_API_KEY` / `TAVILY_KEY` / QQ secret / `sk-*` 模式）、`Authorization` header、cookie、clipboard 内容。替换成 `{"$redacted": "env:DS_KEY", "len": 35}`——保留长度和来源，回放时由代码重新读 env。

> **反例警示**：`_store_tool_args` 的「省略」**不是** redaction，是**数据丢失**——它让 Bug 21/22（覆盖无备份、diff 自检失效）的现场永久不可回放。规矩：不可逆的省略不许做；要么可逆，要么回放时能重取。（这也是原则 ① 的另一种说法：落盘的是数据，不是「为了省地方而做的加工产物」。）

**L2 — 导出时，显式 opt-in**
`trace export --shareable`（计划中，尚未实现；`cmd_export` 上未接线的参数已删）：额外抹用户正文、文件内容、路径、URL query。用途是把用例提交进仓库。

- **硬规矩：L2 产物必须通过回放自检才允许导出。** 红了说明 redaction 破坏了保真，或测试本来就在偷看明文。
- 只有 L2 导出物进 `tests/fixtures/replay/`。

**入库 fixture 必须可再生，而且这件事要能被验证。**
fixture 由 `tests/fixtures/replay/_regenerate.py` 重放整条链路（录制 → `extract_cassette` → 内建自检回放）产出，所以它永远与产出它的代码同源。但「老 cassette 还能播」只证明**旧基线仍然兼容**，证明不了**今天的录制器还会录出同样的东西**——两回事会分别以两种方式坏掉：前者坏在回放报错（看得见），后者坏在基线悄悄变成"没人知道它现在代表什么"（看不见）。所以生成器带一个只读闸门：

```
python tests/fixtures/replay/_regenerate.py           # 重录入库 fixture
python tests/fixtures/replay/_regenerate.py --check   # 只校验；漂移则退出码非 0
```

`--check` 把 fixture 重录到 `tmp/` 下（**不碰**入库目录：校验不许有副作用），再和入库版本比**稳定投影**——事件流去掉 `ts`/`seq`/`run_id`/`session` 这类天然不同的字段，加上 `cassette_meta` 的契约字段（`canon_version`/`playback_mode`/`completeness`/`assertion_ids`/`checkpoint_count`/`derived_from`）。于是"改了 canon、改了词表、加了事件字段却忘了重录 fixture"当场变成退出码非 0，而不是等到某次回放失败再去猜原因。

这条闸门成立的前提是**录制必须可复现**，而这要求把两处进程级状态在 run 起点复位（都不报错、但会让同一段历史录两次得到不同的基线）：

| 残留 | 渗到哪里 | 症状 |
|---|---|---|
| 上一轮的 `request_index` | `run_start` 的 `request_index`（"开跑之前"却挂着上一轮末尾的请求序号） | 两次录制的第 1 条事件就不同；回放侧看到自相矛盾的 trace |
| headless 打印标志 `_headless_streaming` | checkpoint 的 D 组（进程状态快照） | 同一段历史两次录制的 checkpoint 对不上，回放看起来像"环境变了" |

另外，录制的时间锚用 `TRACE_EPOCH_ISO` 钉死（`YYYY-MM-DDTHH:MM:SS`，只给入库 fixture 与自检用）：不钉的话每次重录都带着"今天几号"，差异到底是代码改了还是日期翻了就永远说不清。

### 9.1 访问控制

- `tmp/traces/` **默认仅当前用户可读**：POSIX `0700`；Windows 上 `os.makedirs(mode=0o700)` **不生效**，必须显式加 ACL（`icacls <dir> /inheritance:r /grant:r "%USERNAME%":(OI)(CI)F`）。**如实记录这条差异，别写成 chmod 就完事。**
- `trace clean --older-than 30d` + 体积预算提示（单会话超 5MB 告警）。**只清 trace**：已抽取 / 已提交的 cassette 不受影响——要长期保留的失败现场，**先抽取 cassette 再让它被清掉**（抽取 = 晋升为契约产物，§4.8）
- **CI 不持久化原始 trace**，只留 L2 导出物；加一条**泄漏闸**：提交里出现 `tmp/traces/**` 路径即失败（`.gitignore:27` 的 `tmp/` 已覆盖该目录，无需新增规则）
- 本地单用户场景不夸大风险，但共享开发机上 trace 里确实有用户正文和文件路径——按上面前提默认收紧

---

## 10. 落地顺序

| 阶段 | 内容 | 独立价值 |
|---|---|---|
| 0 | `llm.py` 网关（三出口归一）+ `TOOL_CLASS` 声明表 + `HEADROOM_STORE_URL` 可配 | 压缩/视觉的调用终于可观测 |
| 1 | L0：事件 schema（含分级、两通道、控制流、`limits`）+ **全量快照** + `tmp/traces/` + **trace→cassette 抽取器**（§4.8；阶段 3 消费） | grep 从「翻 JSON」变「查结构」；失败现场从此可晋升为契约产物 |
| 2 | **迁移旧会话 → L0 degraded + §8.1 矩阵报告** | 量出天花板；113 份语料可用 |
| 3 | L1：replay transport（双指纹 + 游标 + 跳过窗口 + strict）+ 两个回放测试（Bug 27、Bug 7） | 失败轮次第一次变成可执行回归 |
| 3.5 | **L3 最小化算法调研**——定目标/边界/导出格式，产出见 **§16** | 避免阶段 5 才发现算法边界不对 |
| 4 | L2：overlay + 三类策略 + 路径映射 + 跨类污染防护；**并决定是否需要 delta** | 复现写文件类 bug |
| 5 | L3：ddmin 实现 → 自动产出用例 | 真正省 CI 时间 |

每个阶段独立有用。**阶段 2 之前不动 L1**——要靠它先量天花板。

---

## 11. 验收标准

- 同一会话跑两次，`trace.jsonl` 的事件**类型序列**一致（显式排除 `ts` / `duration_ms` / `latency`）；按 `call_id` 关联后同样一致
- 离线回放 Bug 27 场景：无网、无本地服务，断言 `finish_reason=length` → 升级预算 → 成功，全程 < 2s
- 离线回放 Bug 7 场景：空 `choices` 尾块不抛 IndexError
- `strict` 回放：`skips == 0`，且按 **§6.6 的四条判据**通过（`loose` 相等 + 差异全部落在 volatile 槽位内 + `reinject` 槽位值相等 + `tolerate` 差异已进报告）；cassette 含不可复现差异时 strict 必须**显式报「不适用」**，而不是抛指纹不匹配。permissive 回放的每个 skip 在报告里可解释（给出首个分叉 message）
- seek 还原自检：base+delta 还原的 messages 与录制请求体 hash 一致；不一致必须报错退出
- **seek 成本有界**：seek 到第 N 个请求所需解压的快照数 ≤ 帧组大小 K，**与 N 无关**（§5.2）；trace / cassette 用行偏移索引做到 O(1) 跳转
- **回放模式是字段不是推断**：`cassette_meta.playback_mode` 缺失 → 加载即报错；`positional` cassette 走指纹匹配、或 `fingerprint` cassette 走按位回放 → 都**加载即报错**，不许静默 skip / 静默降级（§4.8）
- `positional` cassette 缺 `derived_from` / `expected_assertion` → 加载即报错；`expected_assertion` 与测试断言**同源**（测试读 fixture 字段），且 `positional` 的回放报告必须显式标出「未做请求指纹比对」（§4.8）
- **断言身份只从 assert 级事件派生**（§16.7）：测试代码里的 `assert` **不参与** ddmin 身份；改 `notice` 文案 / 改异常所在**行号** → 断言 id **不变**；改异常所在**函数名** → id 变（有意，两个函数里的 `IndexError` 是两个 bug）
- **控制流事件真的存在**：`turn_end` 在 `chat()` 的**每个** return 分支都发一次且 reason 落在枚举内；`_notice` 带稳定 `code`（改文案不改 code）；`_dump_filtered` 的文本标签**不得**被当作断言身份（§16.7）
- 断言词表改名后，旧 fixture 的 `expected_assertion` **显式报错或经 converter 迁移**，不许静默变成「匹配不上就当通过」（§16.7）
- **快照无缺口**：任意请求 i 都能命中 `pre_request[i]`，seek **不需要**「从轮边界前向重算」；快照显式带 `request_index` / `turn` / `tool_round` 可字段寻址（§5.3）
- **还原自检同时覆盖 A 与 B 组**：messages hash + B 组记账值都要与录制侧一致（§5.3；B 组 ground truth 来自 §4.7 的 `limits` 事件）
- 未分类工具 → 启动即报错（用测试钉住）
- **改动任一 `limits` 旋钮（如 cap 6000→8000）→ 回放硬失败且点名该旋钮**；旋钮不变而截断逻辑变 → 只报 drift
- **只改规范化规格、不重录 → 旧 cassette 仍可回放**（证明 §6.4「指纹不落盘」生效）
- **同一会话在两个不同检出目录下各录一次 → `loose` 指纹序列一致**（证明绝对根没进键，§6.5）
- overlay 嵌套在 `WORK_DIR` 内时，路径归一化**确定**：按最长前缀优先，`WORK_DIR/tmp/overlay/<sid>/a.py` 必须归一成 `$WORKSPACE/a.py` 而不是 `$WORKSPACE/tmp/overlay/<sid>/a.py`（§6.5）
- B 类路径重写后**真实 `WORK_DIR` 文件的 mtime / sha256 不变**（防「回放写进真仓库」）
- 迁移 113 份会话不崩；矩阵报告每格计数 + 降级工具归因
- **L3 收缩产出只能声称 1-最小**（§16.1），且必须锚定**同一个断言**——失败变成另一个失败时该收缩无效（§16.4 第 1 条）；测试预算耗尽必须如实报「未收敛 + 当前最优」
- 回放套件**离线可跑**，继承 `run_tests.py` 的「纯逻辑，不触网」定位，可直接并入
- trace 产物落在 `tmp/traces/<session_id>/`，`trace clean --older-than 30d` 只作用于该目录，CI 闸拦 `tmp/traces/**` 路径（§13.1 第 2 条）
- 存储层为自研 JSONL：L0 写入器不依赖任何 OTEL 字段；`python -m tools.trace export --format otel` 只读取 trace 生成派生视图（§13.1 第 4 条）
- overlay 用 `shutil.copy2` 建立目录副本（不依赖 symlink / junction 权限），根为 `tmp/overlay/<sid>/`；`grep` / `glob` / `shell_cmd` 的重写目标为 overlay 根（§13.1 第 3 条）
- `delegate_task` 回放返回录制的 `result_full`，子树 LLM 调用不进入主 cassette（§13.1 第 1 条）
- 一个 fixture 只带一个 `expected_assertion`；同一 trace 的多个失败导出多个 fixture（§13.1 第 5 条）

---

## 12. 风险与退路

| 风险 | 处理 |
|---|---|
| **trace** 格式演进导致老录音失效 | **可接受**：trace 是诊断物，`schema_version` 自由演进、无兼容承诺、按 §9.1 定期清理 |
| **cassette** 格式演进导致旧 fixture 失效 | **必须防**：cassette 是回放输入且已入库，走 `cassette_format` **契约**（只增不改，破坏性变更必须配 converter），与 `schema_version` 解耦（§4.8） |
| **规范化规格演进**导致老旧记录失效 | **不成立**：指纹回放时实时算、不落盘；规格有独立 `canon_version`——`schema_version` / `cassette_format` / `canon_version` 三者互不牵连（§6.4） |
| **预算旋钮被改动后回放静默通过** | `limits` 记录生效值 → 旋钮差异硬失败并点名；截断闭环正向比对 `result_stored`（§4.4 / §4.7） |
| headroom 升级静默改变请求体 | 版本 + mode 进 `meta.json`，不匹配即判 cassette 过期（§3.1） |
| 断言过度耦合逐字 prompt | 断言只打在事件序列 / `turn_end` 原因 / 消息结构上；逐字差异走 §6.6 的槽位归类报告，默认不失败 |
| **把 `loose == strict` 当保真判据** | 该判据只在无易变段时成立、有易变段时必然假红，已废弃；改用 §6.6 四条判据（差异必须归类到槽位） |
| 「回放通过」的假安全 | 六道闸：未分类工具报错、skips 计入报告、`limits` 归因、B 类跨类污染显式处理、路径反向映射在入 conv 前完成、L2 导出必须回放自检 |
| provider 不回传 `reasoning_content` | 记 `null`，回放按 `null` 走；**不伪造** |
| trace 膨胀 | emit_event 分级 + `llm_chunk` 为唯一流式真相源 + 快照 zstd；单会话 > 5MB 告警 |
| 路径 best-effort 重写失效 | trace 里标 `rewrite: best_effort`；`shell_cmd` 属此类，不假装精确 |

---

## 13. 已定与未决

### 13.1 已定（v13）

| # | 事项 | 结论 | 落点 |
|---|---|---|---|
| 1 | `delegate_task` 要不要做 sub-cassette | **整体 stub**：返回录制的 `result_full`，子树 LLM 调用不录、不回放。依据：子 agent 的产出已经作为该工具的 `result_full` 录下，父会话回放不失真；sub-cassette 唯一多给的是「进子 agent 内部调试」，而子 agent 有自己的会话 trace | §7.3 第 3 条 |
| 2 | trace 放 `traces/` 还是 `tmp/traces/` | **`tmp/traces/<session_id>/`**：`tmp/` 已在 `.gitignore:27` 覆盖，无需新增忽略规则，与同目录 `tmp/headroom.jsonl` 一致；trace 天然含完整 prompt 与工具输出，入库物只有抽取后的 `tests/fixtures/replay/` | §4.1、§9.1、§10 阶段 1 |
| 3 | overlay 用目录副本还是文件系统快照；根是否移出 `WORK_DIR` | **目录副本 + 根留在 `tmp/overlay/<sid>/`**：副本与 Windows 权限无关（symlink 需特权）；根留 `tmp/` 下与现有产物一致、被 gitignore 覆盖；嵌套前缀歧义由 §6.5 最长前缀规则消解；递归扫描类工具（`grep` / `glob` / `shell_cmd`）的重写目标为 overlay 根 | §7.2、§6.5 |
| 4 | schema 对齐 OTEL / GenAI 语义约定还是自研 | **自研 JSONL** 作存储格式：`checkpoints` / `limits` / `canon` / 完整度声明都是本项目专有字段，OTEL 无对应槽位且映射没有消费方；OTEL 留作派生视图 `python -m tools.trace export --format otel`，不进 L0 存储层 | §4.1 |
| 5 | L3 导出粒度 | **一个 fixture 一个失败轮次、一个 `expected_assertion`**：该字段是单值，ddmin 谓词 `FAIL_with(expected_assertion)` 按单值比对；同一 trace 的多个失败抽多个 fixture，各带自己的断言身份 | §4.8、§16.4 |

### 13.2 未决（待裁定）

1. 是否把「L3 热路径上的操作必须声明复杂度与 N 的关系」加进本文档的通用检查项。
2. 是否把「每条声明的自检/保证必须写明 ground truth 从哪来；每个『不能混用 / 不能悄悄如何』必须指明是哪个字段或哪次报错在拦」加进通用检查项。

---

## 14. 变更清单

### v13

§13 的 5 项待定全部裁定（结论与依据见 §13.1）：

| 事项 | 落点 |
|---|---|
| 1 `delegate_task` → 整体 stub，子 agent 的失败排查走它自己的会话 trace | §7.3 第 3 条改写 |
| 2 trace 目录 → `tmp/traces/<session_id>/` | §4.1 文件布局、§9.1 访问控制（含泄漏闸路径）、§8 迁移层 CLI、§9.1 trace→cassette 抽取命令、§3.1 headroom store 指向、§10 阶段 1 行 |
| 3 overlay → 目录副本 + 根留在 `tmp/overlay/<sid>/`，递归扫描类工具重写到 overlay 根 | §13.1 第 3 条（正文 §7.2 的 overlay 根位置不变） |
| 4 存储格式 → 自研 JSONL；OTEL 只作派生视图 | §4.1 新增一句 |
| 5 L3 粒度 → 一个 fixture 一个失败轮次、一个 `expected_assertion` | §13.1 第 5 条（§4.8 / §16.4 现有规格不变） |

### v12

| 评审意见 | 落点 |
|---|---|
| §16.4 说断言 id 参与比对，但断言从哪来——测试代码里的 `assert`（则 ddmin 谓词需要测试代码暴露 id），还是 trace 里的 `turn_end` 事件（则 id 可从事件派生）？ | **新增 §16.7「断言身份从哪来：只从 assert 级事件派生」**：选**事件流**，`assert` **不参与** ddmin 身份，四条理由（可达性——收缩后 assert 可能执行不到，身份就成了执行路径的副产品 / `file:line` 随编辑漂移 / 谓词是回放驱动的，不该 import 测试框架 / v11 已定 fixture 字段是真相源，方向不能反）。**但「事件里有」是假前提**：`turn_end` 今天不存在，终局信息只以 `_notice(level, message)` 的自由中文文案（`runner.py:392`，17 处，**无 code**）和 `_dump_filtered("turn_end:stuck", ...)` 的文本标签（`runner.py:543`、`runner.py:683`，写进抽取时就要丢掉的 dump）存在 → §4.2 的 `turn_end` / `notice` 是**必须新建的事件**，配三处最小代码改动（`chat()` 每个 return 分支发 `turn_end` + `_notice` 增稳定 `code` + 异常身份用 `类型@项目内首个帧的函数名`、**不含行号**）。给出断言 id 词表；多重失败取「id 是否出现」+ 首次出现位置；词表是 **`cassette_format` 级契约**（只增不改、改名配别名，**不新增第四个版本轴**）；写明**能力边界**（纯业务值断言不可派生 → 不能自动最小化）；白捡：`expected_assertion` 可在抽取/收缩时自动填，§16.4 的「失败变成另一个失败」退化成字符串比较。配套：§4.2 表格行标注「今天不存在，须新建」+ 表后说明、§11 新增三条验收 |

### v11

| 评审意见 | 落点 |
|---|---|
| §16.6 说 L3 产物走按位回放、人工 fixture 走指纹匹配，「两条路不能混用」，但 `cassette_meta` 里没有区分字段 | §4.8 新增「**回放模式是字段，不是回放器的推断**」：`playback_mode`（`fingerprint`/`positional`）**必填、缺失即报错**；`positional` 追加 `derived_from` + `expected_assertion`（后者是 ddmin 谓词的输入，§16.4）；**两个方向的错配都加载即报错**（走错指纹会失配、走错按位会静默关掉漂移检测）；`expected_assertion` 单一真相源（测试读 fixture 字段）；`positional` 报告必须显式标「未做请求指纹比对」；写明边界（防误用，不防手改）。§16.6 尾部改为指向 §4.8；§4.8 自足清单表格更新；§11 新增两条验收 |

### v10

| 评审意见 | 落点 |
|---|---|
| B 组状态快照的**时机**仍未明确（§5.1 说两处都抓、§5.2 说全量快照，但在哪个时机抓没写），而它直接决定 seek 精度：轮边界抓会丢同轮内压缩尝试的状态变化，每请求抓则「完整但成本高」 | **新增 §5.3「快照时机」**：定「每请求一份，抓在 `_prep_context` 之后、`_safe_create` 之前」（`runner.py:448`→`450`、`runner.py:595`→`596`）+ 加一个 turn 末终态补最后一格 + 三条「轮边界不可用」的机制理由（`_prep_context` 每请求都改要发的东西 / 前向重算会打乱 cassette 主游标且不可复算 / 缺快照则 ddmin 的 1-最小口径变假）+ 用「请求体已录、快照只存 hash 指针」结构性消掉成本 objection +「无缺口」可证 + 补掉 B 组还原**没有 ground truth** 的自检缺口（`limits` 事件携带 B 组记账值）。配套：§5.2 加指向 §5.3 的说明并纠正「200KB 是请求体不是快照增量」、§4.7 `limits` 增带 B 组、§11 新增两条验收 |

### v9

| 评审意见 | 落点 |
|---|---|
| 单一 zstd 流无法随机访问，每次 seek 要从头解压，会话越长越慢 | §5.2 改为**按帧组压缩 + 字节偏移索引**，并写出真实代价（ddmin O(n²) 重放 × O(N) 解压 = **O(n³)**，这才是「越到后面越慢」的来源）+ 三种分组粒度的取舍表（默认每用户轮一组）+ 说明 frame 可独立解码故**不需要** seekable format + 索引错不会静默（复用还原自检）+ 推广到 trace/cassette 用行偏移索引、不压缩 + §11 新增「seek 成本有界」验收 |

### v8

| 评审意见 | 落点 |
|---|---|
| ddmin 是单目标算法，字典序需要改造 | **新增 §16「L3 最小化算法」**：§16.1 对齐 ddmin 的谓词语义与 **1-最小**口径；§16.2 指出两目标**正交**（调用由响应驱动，与历史长短无关）；§16.3 两阶段 ddmin + spine 守卫（放宽为「不得新增或重排」，允许压缩调用消失）；§16.4 三处必改（**断言锚定** / 守卫 / 测试预算）；§16.5 字典序比较器的等价写法；§16.6 与 §6 指纹匹配的冲突及按位回放路径。§14 与 §10 的对应条目改为指向 §16 |

### v7

| 评审意见 | 落点 |
|---|---|
| `strict` 含原始 messages（时间戳 / session_id / id），要求 `skips==0` 且 `loose==strict` 就必须逐字复现这些动态值 | 承认这是**逻辑错误**（该判据只在无易变段时成立，有易变段时必然假红）→ **新增 §6.6「strict 模式的正确判据」**（四条判据 + strict 不适用要显式拒绝 + 「哪些易变值真能逐字复现」表）+ §6.1 把 `strict_hash` 降级为仅作快速预检 + §6.2 归一化段改为 **volatile 槽位表**（新增 `reinject` / `tolerate` 策略列）+ §6.3 更新 strict 定义 + §11 判据改写 + §12 新增风险行 |

### v6

| 评审意见 | 落点 |
|---|---|
| 路径归一化没说清录制侧也要替；只在回放侧替会对不上 | **新增 §6.5「路径归一化：两侧各自提供根表」**（两侧都存原始值、都在算指纹时归一化 + 录制/回放两张根表 + 三条实现约束）+ §6.2 表行改为指向 §6.5 + §4.8 `cassette_meta` 增 `workspace_root` 并进自足清单 + §7.2 说明反向映射是 best-effort、`$WORKSPACE` 是第二道防线 + §11 三条验收（跨检出目录一致 / 最长前缀 / 嵌套确定性）+ §13 overlay 位置列入未决 |

### v5

| 评审意见 | 落点 |
|---|---|
| trace 与 cassette 是不同产物、演进代价不同 | **新增 §4.8**（两产物对照表 / 抽取模型 / cassette 自足清单 / 完整度声明 / 抽取自检）+ §4.1 声明该目录只放 trace + §8.2 修正原先「共用 schema_version」的写法 + §12 拆成两行风险（trace 可弃 / cassette 是契约）+ §9.1 `trace clean` 豁免 cassette + §10 阶段 1 加抽取器 |

### v4

| 评审意见 | 落点 |
|---|---|
| 「轮次」的确切定义缺失 | **新增 §15「术语：轮次的三层定义」**（用户轮 / 工具轮 / 工具链组，各带代码权威来源与包含关系）+ **§15.1 证明轮边界不可从 messages 反推**（三种合成 user 消息）+ §4.2 加 `turn` / `tool_round` 显式字段 + §8.1 补轮级聚合规则 + §14 收缩单元改为两粒度复合（**并纠正原先「user turn 边界」的写法**） |

### v3

| 评审意见 | 落点 |
|---|---|
| 1 指纹应回放时实时计算，不落盘 | §4.2 去掉 `fingerprint_*` 字段 + **§6.4 新节**「存数据，不存规则的产物」+ `schema_version` 与 `canon_version` 分离 + §11 新增「只改规格不重录」验收 + §12 风险行 |
| 2 `result_stored` 截断与指纹的交互 | §4.4 新增「回放吐 `result_full` → 重新截断 → 比对 `result_stored`」闭环 + **§4.7 新节**「预算旋钮是一等公民」（`limits` 事件 + 旋钮清单 + bug 分布依据）+ 归因三分支 + §6.2 说明工具结果**刻意不归一化** |

### v2

| 评审意见 | 落点 |
|---|---|
| 1 Fingerprint 规范化规格 | §6.1 双指纹 + §6.2 白名单/归一化表 + 「匹配键不是内容校验」原则 |
| 2 Delta 链复杂度 | §5.2 阶段 1 全量快照 + zstd，delta 后置到阶段 4，**强制**还原自检 + 每 N 轮强制 base |
| 3 并发工具事件顺序 | §4.6 —— 纠正前提（顺序本就确定、`call_id` 本就有）；改为定耗时口径、B 类回放串行、worker 事件顺序 |
| 4 Overlay 路径映射 | §7.2 正向重写（分发缝，早于工具内 abspath）+ 反向映射（入 conv 前）+ 跨类污染 + best-effort 显式标记 |
| 5 headroom 参与方式 | §3.1 **拍板**：replay transport 置于 headroom 内侧 + 三条约束（store 路径可配 / 键打 post-headroom 且录 pre-post / 版本进 meta）；从 §13 移出 |
| 6 emit_event 过大 | §4.3 三级分级 + `stream_summary` 派生摘要（保住 Bug 32 的断言量） |
| 7 L3 提前调研 | §10 新增阶段 3.5；边界见下 |
| 8 可回放比例定义 | §8.1 改为 2×3 矩阵（两正交维度）+ 按轮次 + 降级工具归因 |
| 9 traces 访问控制 | §9.1 0700 / Windows ACL 如实差异 / trace clean / CI 泄漏闸 |

### 附：阶段 3.5（L3 调研）的边界条件（v2 起，v4 修订）
- **目标**：保留失败断言，最小化 ① LLM 调用次数 ② 消息总长；字典序（先压调用次数，再压长度），避免权重调参。**ddmin 是单目标算法，字典序必须改造——方案见 §16**（两阶段 + 三处必改：断言锚定 / spine 守卫 / 测试预算）
- **两个粒度复合**（§15）：**删除单元 = 用户轮**（整车删，保证不留孤儿 assistant）；**不可分割子单元 = `_find_groups()` 的组**（`context.py:28-51`，它已经把 `assistant(tool_calls)` 与后续 `tool` / `[Self-check]` 消息绑成一组，**别再造第二套**）。两者缺一不可：只用用户轮会拆散工具链；只用 `_find_groups` 组会把「问句」和「答句」算成两个独立组，删掉问句就留下孤儿 assistant
- **删轮次会改变 `_user_turn`，进而改变压缩决策**（`min_rounds` / `_compact_last_turn` / 退避 `retry_at = _user_turn + 3`，`compaction.py:340-348`）→ 收缩后的 checkpoint **B 组状态必须重算，不能照抄**；也因此**缩小后的用例只能断言「失败本身」，不能断言完整事件序列**（被删轮次必然改变 `compaction` / `prune` 的决策序列）。ddmin 的存活判据正是「失败仍能复现」，这条自洽
- **不可删**：首轮、失败轮、以及**全部控制流事件**（它们是断言面）。删掉等于把断言依据删了
- **fixture 血缘**：每个导出用例保留指回源 trace 的指针 + 一条「从源会话重新生成」的命令。cassette 只对产出它的那份代码有效，prompt 一改就红——**把它当诊断物优先、回归物其次**，红了能一键重生，而不是手工修 fixture

---

## 15. 术语：轮次的三层定义

中文「轮」在本文档里被三样东西共用。按**代码里的权威来源**定名，不要按直觉互换：

| 术语 | 定义 | 权威来源 | 用途 |
|---|---|---|---|
| **用户轮**（turn） | 一次 `chat()` 调用 = 一个用户问题及其全部产出 | `compaction.next_turn()`：`_user_turn` 每轮 +1（`compaction.py:78-83`，在 `runner.py:418` 调用） | §8.1 统计单位；§14 的**删除**单位；压缩频率预算（`min_rounds` / `_compact_last_turn` / 退避 `retry_at`） |
| **工具轮**（tool round） | 用户轮内的一次 LLM 请求 | `tool_round`（主循环，上限 `cfg["max_tool_rounds"]`）/ `final_round`（收尾流式阶段，上限 12）（`runner.py:442`、`runner.py:591`） | `_WRAPUP_NUDGE` 的「已使用 N/M 轮工具调用」；`limits` 归因的定位粒度 |
| **工具链组**（tool-chain group） | `assistant(tool_calls)` + 其后的 `tool` 消息 + `[Self-check]` user 消息，直到角色改变 | `context._find_groups()` priority 0（`context.py:28-51`） | 上下文裁剪的**不可分割**单元；§14 收缩的**子**单元 |

三者的包含关系：**用户轮 ⊃ 工具轮 ⊃ 工具链组**。
- 一个用户轮 = 若干工具轮（`tool_round` 用尽后还有最多 12 个 `final_round`）
- 一个工具轮里可能一次发起多个工具调用 → 可能产生**多个工具链组**（并发调用时；`run_tools` 一次收一批）

### 15.1 用户轮边界**不能**从 messages 反推

本节最重要的一条。三种合成 user 消息都会打破「`role == "user"` 即轮边界」的假设：

| 来源 | 行为 | 后果 |
|---|---|---|
| `[Self-check]` | `run_tools` 把自检合并成一条 user 消息**追加在工具结果之后**（`runner.py:336-341`） | user 消息出现在**工具轮中间** |
| nudge 注入 | `_inject_user_nudge`：末条是 user 就**合并进它**，否则追加（`runner.py:168-175`） | 同上，且末条被合并时**根本没有新消息**，边界更隐蔽 |
| `[Context trimmed]` | `render_prune_notice` 把裁剪计数**写进当前问题的 content 前缀**（`context.py:86-91`）；旧提示以独立 user 消息形态存在（`_is_trim_notice`，`context.py:54-55`） | user 消息可能是「问题 + 提示」的混合体 |

**代码本身已经承认了这件事**：`_find_groups` 专门把 `[Self-check]` 的 user 消息**折进工具链组**（`context.py:40`），而不是把它当作新轮的开始。

所以这是原则 ① 的又一个应用：**用户轮编号是运行事实，不是可从 messages 推导的属性。** L0 必须把 `turn`（及 `tool_round`）作为**显式字段**写进 `llm_request` / `tool_call` / 控制流事件，事后分析一律读字段、不做启发式重推——否则将来再加一种合成 user 消息，历史 trace 的轮次划分就整体错位（Bug 17 / 26 那一族）。

---

## 16. L3 最小化算法（阶段 3.5 调研产出）

§14 原来只写「目标取字典序（先压调用次数，再压长度）」。**ddmin 是单目标算法**——它内建的偏好是「单元更少」，没有地方放第二个目标。要用字典序必须改造，下面是改法。

### 16.1 先对齐 ddmin 实际要什么

ddmin 需要的是一个**谓词** `test(c) ∈ {FAIL, PASS, UNRESOLVED}`，不是代价函数；产出是 **1-最小**（再删任何一个单元都会 PASS），**不是全局最小**。所以：

> 结论口径只能写「**1-最小**」，不能写「最小」。这条要进验收，否则永远不达标。

### 16.2 两个目标其实正交

| 目标 | 自由度 | 收缩单元 |
|---|---|---|
| 主：调用次数 | 保留哪些**响应** | **用户轮**（§15）——删一轮就少掉那轮的若干次调用 |
| 次：消息长度 | 保留哪些**历史消息** | `_find_groups()` 的组（§14）——删历史**不减少**调用 |

关键：**这两个自由度正交**。删历史消息不改变 LLM 调用次数——调用由**响应序列**驱动（响应里有工具调用，循环才再调一次），与历史长短无关。所以次目标的搜索**原则上不可能破坏主目标**。这正是字典序能拆成两阶段的依据。

### 16.3 两阶段 ddmin（推荐实现）

```
c1    = ddmin(cassette, units=turns,          test=FAIL_same_assertion)
spine = positional_spine(c1)                  # 锁定：按位消费的响应序列
c2    = ddmin(c1,       units=history_groups, test=FAIL_same_assertion,
              guard=no_new_or_reordered_responses)
```

- **阶段 1**：单元 = 用户轮 → 得 1-最小的调用序列
- **阶段 2**：单元 = 历史组，外加 spine 守卫

**守卫要写成「不得新增或重排响应」，不是「响应序列必须完全不变」。** 因为有一个真实反例：历史被压短后，**压缩摘要器那一次调用可能不再发生**——调用次数反而**减少**了。这属于主目标被顺带改善（不会让字典序失效），但如果守卫写成「完全不变」就会把它误判成无效收缩。所以：允许**减少**，但减少必须**进报告**——因为它意味着次目标搜索意外动了主目标，值得记一笔。

### 16.4 三处必改（缺一个就会静默出错）

1. **`FAIL` 必须锚定到「同一个断言」。** 输入变小后，原来的失败可能消失，也可能**变成另一个失败**。后者会让 fixture「仍然失败」却测的是别的 bug——ddmin 会把这种收缩当成有效，**悄悄把现场换掉**。所以谓词是 `FAIL_with(expected_assertion)`：断言身份（哪条 `turn_end` 原因 / 哪个异常 / 哪个断言 id）参与比对。**这是最容易漏、后果最坏的一条**，它也是收缩产物除 `playback_mode` 之外唯一必须携带的元信息——落在 `cassette_meta.expected_assertion`（§4.8）。
2. **阶段 2 的 spine 守卫**（§16.3 的放宽版）。
3. **测试预算。** ddmin 最坏 O(n²) 次重放，单元一多就不可接受。设上限（如 500 次重放），耗尽就**如实报告「未收敛 + 当前最优」**，不要假装收敛。

### 16.5 原理上的等价写法（可以不实现）

不堆两阶段也行——把 ddmin 内建的「单元更少」偏好换成**字典序比较器**：

```
c' ≺ c   当且仅当   (calls(c'), len(c')) <lex (calls(c), len(c))   且   FAIL_same_assertion(c')
```

这样一次运行同时压两个目标。代价是每个候选都要跑完整重放才能算出二元组，而两阶段版本在阶段 1 结束时搜索空间已经很小。**建议先实现两阶段**：好实现、好调试，且每一阶段的结果都能单独解释。

### 16.6 与 §6 指纹匹配的冲突（必须承认）

收缩会**改写历史**，于是现场请求不再等于录制的请求 → §6 的指纹匹配在收缩产物上**必然失配**。所以收缩后的 fixture 必须走一条**按位消费**的回放路径：

- 保证「**响应序列相同**」，**不保证**「请求相同」
- 因此收缩产出**不能断言请求内容**（`loose` 相等不成立），**只能断言失败本身**
- 这与 §14 早已写下的结论（「缩小后的用例只能断言失败本身」）是同一件事，这里补上机制原因

按位回放是**更弱**的保证，所以**只用于 L3 产物**；人工挑选的 fixture 仍走 §6 的正常指纹匹配路径。两条路径不能混用，也不能让 L3 产物悄悄走成指纹匹配（那会失配）或让人工 fixture 悄悄走成按位（那会失去漂移检测）。

**「不能混用」不是靠约定，是靠字段**：这个区分落成 `cassette_meta.playback_mode`（必填）+ 两个方向的**加载即报错**，规格见 §4.8「回放模式是字段，不是回放器的推断」。`positional` 还必须带 `derived_from` 与 `expected_assertion`（后者是 ddmin 谓词的输入，§16.4）。

### 16.7 断言身份从哪来：**只从 assert 级事件派生**（本轮定）

§16.4 要求谓词是 `FAIL_with(expected_assertion)`，v11 把 `expected_assertion` 落成必填字段，但**它从哪来**没写。两条候选必须挑一条：测试代码里的 `assert`，还是事件流。

**选事件流；测试代码的 `assert` 不参与 ddmin 身份。** 四条理由：

1. **可达性。** 收缩会删掉整段历史与轮次，测试文件里某个 `assert` 可能**根本执行不到**。执行不到时测试「通过」，ddmin 把这个候选判成 PASS 丢掉——看着正常，但它意味着**断言身份不再是断言的属性，而是执行路径的副产品**。
2. **稳定性。** 代码断言的天然 id 是 `file:line`，随任何编辑漂移；测试重构一次，所有已入库 fixture 的 id 全部失效，而 `derived_from` 里的 ddmin 记录会指向一堆不存在的行号。
3. **耦合方向。** §16.3 的两阶段 ddmin 是**回放驱动**的。让谓词去 import / 执行别人的测试文件，等于把测试框架焊进寻址层；而 L3 的主要价值恰恰是「脱离测试代码也能收缩」。
4. **数据方向。** v11 已定 fixture 字段是 `expected_assertion` 的真相源、测试**读它**。若身份本该由测试代码产生，这个方向就是反的。

**但「从事件派生」今天做不到——因为那个事件不存在。** 现在终局信息以两种**不可用**的形式存在：

| 今天的形式 | 位置 | 为什么不能当 id |
|---|---|---|
| `_notice(level, message)` 的自由中文文案 | `runner.py:392`，17 个调用点 | **没有 code**。改一次措辞，断言身份就变 |
| `_dump_filtered("turn_end:stuck", ...)` 的**文本标签** | `runner.py:543`、`runner.py:683` | 写进 `debug_filtered.txt`，是诊断 dump——按 §4.8 抽取 cassette 时**就要丢掉**。把身份建在要丢的东西上 |

所以 §4.2 的 `turn_end` / `notice` **不是「已有事件」，而是必须新建的事件**，并配三处最小代码改动：

1. **`turn_end` 落成真事件**：`_emit({"type": "turn_end", "reason": <enum>})`，在 `chat()` 的每个 return 分支各发一次（现有分支已隐含 answer / stuck / garbled / interrupted / rounds_exhausted / api_error / fallback，但只散在 `_notice` 文案与 `"[Interrupted by user]"` 这类返回串里）。
2. **`_notice(level, message)` → `_notice(level, code, message)`**：`code` 是稳定标识、一等参数；文案可自由改。17 个调用点各补一个 code。
3. **异常与工具错误的身份**：`exception:<ExcType>@<项目内首个帧的函数名>`——**不含行号**（行号随编辑漂移），但**含函数名**（两个不同函数里的 `IndexError` 是两个 bug）；工具错误取 `tool_error:<tool>:<code>`，同样要求工具侧有稳定 code（与第 2 点同款问题）。

**断言 id 词表**（结构化、可派生；这是代码侧契约）：

```
turn_end:reason=<answer|stuck|garbled|interrupted|rounds_exhausted|api_error|fallback>
exception:<ExcType>@<func>          # 项目内首个帧的函数名，不含行号
tool_error:<tool>:<code>
notice:<level>:<code>               # level ∈ {info, warn}
```

**多重失败**：谓词是「目标 id 在该次回放的 assert 级事件序列中**出现**」，报告带**首次出现位置**（第几个请求 / 第几轮），`derived_from` 记原始运行的时间序供人核对。同一 id 出现多次不需要区分——身份是 id，不是出现次序。

**词表是契约，不是普通常量**：改 code 会让已入库 fixture 的 `expected_assertion` 失效。规矩与 `cassette_format` 同款——**只增不改，改名必须保留别名或显式报错**，不许静默变成「匹配不上就当通过」。**不新增第四个版本轴**（§4.8 已有 `schema_version` / `cassette_format` / `canon_version` 三个，再切一个的维护成本大于收益）：词表变更视为 `cassette_format` 的破坏性变更，按 §4.8 配 converter。

**能力边界（必须写明）**：只有**能表达成上述运行时断言**的失败才允许进 L3 自动收缩。纯业务值断言（「回答里必须包含 X」「这个字符串必须出现在那个文件里」）**不可派生**，因此不能自动最小化——人工 fixture 照旧可以在测试代码里写 `assert`，但那些断言**不参与 ddmin 身份**。两条路再次分开，与 §16.6 / §4.8 是同一条原则。

**白捡的一个好处**：`expected_assertion` 可以**在抽取 / 收缩时自动填**——从当次失败的 assert 级事件流里取第一个非正常终局即可，不需要人来认领。于是 §16.4 说的「原失败可能变成另一个失败」退化成**两个字符串的比较**：机械、可测、无法悄悄换现场。

