# 交接文档 — 下个会话从这里继续

> 状态日期：2026-08-05。本会话已完成跨会话记忆（remember/recall）并修复 review 发现的 bug。以下是**当前项目结构**与仍需注意的事项。

---

## 一、这是什么

本地 llama.cpp Qwen 编码 agent：`loop_agent_v2.py`（OpenAI 兼容客户端，流式）+ `tui.py`（Textual 三面板 TUI）。后端：`local-3.5` / `local-3.6`（qwen 9B/35B）或 DeepSeek（`MODEL_BACKEND` 环境变量切换，见 `BACKEND_SWITCH_GUIDE.md`）。

核心架构：`loop_agent_v2.py` 是**薄壳**（106 行，持有可变全局 + 再导出公共 API），纯实现全部在 `loop_agent_core/` 包。外部（tui/测试）import 薄壳符号，改动实现不破接口。

---

## 二、当前项目结构

```
agent/
├── loop_agent_v2.py          # 薄壳：可变全局 + 组装 core + 再导出公共 API（~106 行）
├── loop_agent_core/          # 纯实现包，依赖无环，不反向 import 薄壳
│   ├── config.py             # WORK_DIR/_PROFILES_DIR 锚点、_backend_config、cfg、client、CURRENT_MODEL
│   ├── tokens.py             # estimate_tokens（本地 char 启发式 / DS tokenizer 双模式）、_read_budget
│   ├── schemas.py            # tools 列表 = 11 个 OpenAI function schema（9 基础 + recall/remember）
│   ├── prompts.py            # system_prompt、WORKER_SYSTEM_PROMPT
│   ├── shell_tools.py        # get_date/get_weather/exec_shell mock（queue 参数注入，不读全局）
│   ├── file_tools.py         # _resolve_path（沙箱）、build_outline、extract_function、read/count/write mock
│   ├── ds_web.py             # DeepSeek 网页自动化（log_in/upload/input/enter/get_response）
│   ├── browse.py             # browse_web（Playwright）全套
│   ├── xmlutil.py            # DSML 清洗 + XML 工具调用解析：_DSML_RE/_strip_dsml/_normalize_toolcall_xml
│   │                         #   _parse_xml_tool_calls/_strip_toolcall_xml/_is_garbage_content/_XML_TOOLCALL_RE
│   ├── validation.py         # validate_tool_result 工具结果自检（含 recall/remember 分支）
│   ├── context.py            # _append_msg/_find_groups/_is_trim_notice/_prune_messages/_rollback_tool_round
│   │                         #   （纯函数，只操作传入的 conv dict）+ render_prune_notice（计数累加）
│   │                         #   + _prep_context：三层总入口（L0 去重 → L1 摘要 → L2 裁剪 + 频率预算）
│   ├── compaction.py         # 上下文压缩：L0 dedup_tool_results / L1 compaction_span·split_blocks·
│   │                         #   build_block_prompt·run_rolling_summary·apply_compaction /
│   │                         #   档案 record_archive·expand_last·find_archive·persist/load_archive
│   ├── events.py             # _default_on_event、事件类型
│   ├── runner.py             # chat_impl 主循环、run_tools、_stream_strip_xml、_safe_create、_xml_to_tool_calls
│   └── memory.py             # 跨会话记忆：_load/_save、recall_mock、remember_mock、build_memory_injection
│                             #   _MEM_SYSTEM_PATH（锚定 agent 代码目录 ../memories/system.json）
│                             #   _MEM_PROJECT_PATH（锚定 WORK_DIR/memories/project.json）
├── tui.py                    # Textual 三面板 TUI（import 薄壳的 chat/messages/CURRENT_MODEL）
├── test_code_index.py        # 18 项，纯逻辑回归（不触网）
├── test_multi_agent.py       # 7 项，多 agent 隔离/事件/守卫（monkeypatch，不触网）
├── test_progress.py          # 悬空测试（引用不存在的 _agent_status），跑会 SKIP
├── MEMORY_GUIDE.md           # 跨会话记忆实现指导书（本会话产物，已落地）
├── BACKEND_SWITCH_GUIDE.md   # 后端切换指南
├── README.md                 # 使用说明 + 目录树
├── memories/                 # 运行时生成（首次 remember 时自动 mkdir）：
│   ├── system.json           #   系统级记忆（用户偏好/行为准则）
│   └── project.json          #   项目级记忆（本代码库事实）
├── conversations/            # 会话记录 JSON
├── profiles/                 # 浏览器登录态（browse_web profile）
└── *.bak / backup_conflict_files/ / .DS_Store / 图片等杂物
```

**依赖方向（无环）**：`config ← tokens ← file_tools`；xmlutil/validation/context/events 是叶子；`runner → 叶子`；`loop_agent_v2.py → core 全部`。唯一历史遗留的 delegate ↔ chat 环用「运行时查壳」解决（薄壳 `_run_tools` 的 lambda 运行时才查 `delegate_task_mock`，保留 monkeypatch 语义）。

---

## 三、外部契约（改动时一条都不能破）

`tui.py`：
- `from loop_agent_v2 import chat, messages as _agent_messages`
- `loop_agent_v2.CURRENT_MODEL`、`loop_agent_v2.system_prompt`
- **写** `loop_agent_v2._shell_output_queue = ...`（薄壳再注入 `exec_shell_win_mock` 的 queue）

`test_code_index.py`（~25 个符号）：`build_outline`、`extract_function`、`estimate_tokens`、`WORK_DIR`、`read_file_mock`、`count_tokens_mock`、`_parse_xml_tool_calls`、`_strip_toolcall_xml`、`_is_garbage_content`、`_stream_strip_xml`、`_store_tool_args`、`_xml_to_tool_calls`、`_run_tools`、`_append_msg`、`_prune_messages`、`_find_groups`、`_is_trim_notice`、`_prune_cap`、`_rollback_tool_round`、`_DSML_RE`、`messages`、`_msg_size` 等。

**上下文压缩契约（v2.13 新增，v2.14 扩充，薄壳再导出）**：`dedup_tool_results`、`should_compact`、`compaction_span`、`split_blocks`、`apply_compaction`、`expand_last`、`latest_compaction`、`record_archive`、`persist_archive`、`load_archive`、`run_rolling_summary`、`validate_summary`、`summary_reject_reason`、`build_block_prompt`、`compaction_cap`、`placeholder_text`、`cstate`、`next_turn`、`reset_failures`、`failure_hint`、`log_compaction_failure`、`_prep_context`、`render_prune_notice`、`_compact_k`、`_compact_state`、`clear_compact_state`、`persist_archive_sidecar`、`adopt_archive`、`clear_archive`、`expand_compaction`、`compact_session`、`summarize_block`。`chat_impl` 新增 `summarize_fn=None`（**默认值不可去掉**）。工具数 11 → 12（新增 `recall_archive`）。

`test_multi_agent.py`：`tools`、`TOOL_CALL_MAP`、`WORKER_SYSTEM_PROMPT`、`_in_worker`、`delegate_task_mock`、`chat`、`validate_tool_result`；**monkeypatch** `la.chat` / `la.delegate_task_mock`。

`BACKEND_SWITCH_GUIDE.md`：`loop_agent_v2.client.base_url`、`python loop_agent_v2.py` CLI。

---

## 四、关键机制（改这里务必看懂）

### 1. 后端差异（最重要）
- **本地 Qwen**：工具调用写成 **reasoning/content 里的 XML**（`<tool_call>` 块），由 `_parse_xml_tool_calls` 解析，合成 id `xml0`。
- **DeepSeek**：工具调用走 OpenAI JSON `tool_calls`（分片按 index 累积到 `tc_frags`）。**只在 `_is_local_backend()` 时解析 XML 调用**——DeepSeek 不认合成 id，回送会 400（曾踩过，见 `_safe_create`/`_rollback_tool_round`）。
- `cfg["max_tool_rounds"]`：两个 local 和 DS 现在都是 12。

### 2. worker 隔离（delegate_task）
- `_in_worker` 布尔 + worker system prompt 双保险，深度 1 禁止嵌套。
- `chat_impl` 两处 API 调用一律 `messages=conv["messages"]`（修过 worker 把主线程历史发给模型的 bug）。
- **worker 不能调** `delegate_task`/`recall`/`remember`——`run_tools` 的 `is_worker` 守卫统一拦截（`loop_agent_v2._run_tools` 传 `is_worker=_in_worker`）。memory 内还有 `_worker_check` 钩子（薄壳设 `memory_mod._worker_check = lambda: _in_worker`）。
- worker 有自己的独立 conv（`delegate_task_mock` 里新建），主会话共享全局 `messages`。`chat()` 用 `_conv["messages"] is messages` 判断是否主会话（`is` 而非 `==`）。

### 3. 跨会话记忆（本会话新增）
- 两个工具：`remember(topic, content, level)`（存/覆盖，同 topic 精确覆盖）+ `recall(topic)`（查，前 5 条按 ts 倒序）。
- **注入**：`chat_impl` 在主会话首次调用时，把 `build_memory_injection()` 结果作为一条 user 消息前置。守卫是 `startswith("[长期记忆")` 检查——**已有则不再注入**（修过 Bug B：每轮重复注入堆积）。
- 系统级记忆注入**完整 content**；项目级只注入 **topic 索引**，详情按需 `recall` 查。
- `content.strip()`：只去两端空白，中间换行保留（已向用户澄清，勿改回）。
- 记忆路径：`system.json` 锚定 agent 代码目录（跟代码走），`project.json` 锚定 `WORK_DIR`（跟工作目录走）——为将来 WORK_DIR 参数化解耦。

### 4. XML/DSML 清洗链路
四入口都接 `_strip_dsml`/`_normalize_toolcall_xml`：`_parse_xml_tool_calls`、`_strip_toolcall_xml`、`_stream_strip_xml`、`_is_garbage_content`。Qwen3 复数标签格式（invoke/parameter name=）归一化成旧格式（function=/parameter=）再解析。

### 4.5 shell 默认是 pwsh（v2.17 起，改 shell_tools 必看）
- 默认 **PowerShell 7**（`shell="pwsh"`，core 与薄壳两处 + schema enum/default 都是）；cmd 只在显式要时用。
- **绝不要再用 `-EncodedCommand` 跑 pwsh**：它会把错误流序列化成 CLIXML XML（`#< CLIXML <Objs…>`），
  模型读不到错误 → 判定"pwsh 坏了"→ 永久退回 cmd（BUG 35 有真实会话为证）。现在走 `-File` + tmp/ 临时 .ps1
  （UTF-8-BOM），语法错也能看到 `ParserError`；用户命令在 here-string 里、执行前 `Parser::ParseInput` 预检。
- **cmd 语法兼容层**：`shell_tools.effective_shell(cmd, shell)` 返回"实际会用谁 + 该语法的 pwsh 等价写法"；
  结果里带 `shell_used` 与教学用的 `note`；confirm 弹窗显示 `[pwsh]` / `[cmd·cmd语法]`。
  新增 cmd-only 语法时改 `_CMD_ONLY_SYNTAX`，别去改 `_PS_SYNTAX_MARKERS`（后者是"绝不改道"的白名单）。
- 退出码回填：pwsh 的 cmdlet 报错是非终止错误，`_PS_EPILOGUE_BODY`/`_PS_EXIT_BODY` 负责让 rc≠0；
  `cmd /c exit 2` → 2、用户 `exit 7` → 7 保持原样（有测试钉着）。

### 4.6 思考面板的块与块级选择（v2.18 起，改折叠必看）
思考面板 `PanelLog` 的内容是"块列表 + 平铺正文"：块头以 `("h", ThinkBlock)` 混在 `_history` 里，
正文照旧平铺其后（所以折行/残尾/重排全沿用旧机制）。`begin_block` 开新块时把上一块
`collapsed=True`（渐进披露），折叠块只渲染标题 + 正文尾部预览（`_TAIL_KEEP`）。

- **键位分两层**：`j`/`k`/`Enter`/`空格` 绑在 `PanelLog` 上（**只有面板获得焦点时才响应**，
  输入框里打字不会误触发）；`z`（整体切换）/`o`（展开全部）留在 App 上，输入框里也能按。
- **块光标**：`self._cursor`（`_blocks` 下标，`begin_block` 时跟到最新块）。选中块标题反白
  （`_block_title` 里判断），`focus_block()` 移动后 reflow + `_scroll_to_cursor()`。
- **鼠标点标题**：`on_click` → `_line_index_at(event)`（鼠标 y 减边框、加 `scroll_offset.y`）
  → `block_at_line()` → `is_title_click()`。**两个易错点**：
  1. `_block_line`（块 key → 标题行号）必须在**每次 reflow 时重建**，且 `max_lines` 裁掉
     行首时要同步上移（`flush_pending` 里的 `drop`），否则点谁都折第一块；
  2. 折叠态的块要把**尾部预览那一行**也算进点击区，否则"看得见却点不动"。
- 折叠态判定同样靠 `_tail_history`：窗口若从块体中间开始会**往前补块头**，否则重排不知道
  这一块是折叠的、会把折起的内容整段铺回来。

### 5. 上下文压缩三层（v2.13 新增，改这里务必看懂）
`loop_agent_core/compaction.py` 是叶子，`context._prep_context()` 是唯一入口，`runner.chat_impl` 的两个调用点都走它。

- **顺序固定 L0 → L1 → L2**，只在**两次 API 调用之间**执行（流式进行中绝不压）。
- **L0 去重**：判定是 `(tool_call_key, result_digest)` **双条件**。`tool_call_key` 会对参数做 `json.loads` + `sort_keys` 归一化，所以 `read_file` 的不同区间/`function=`/路径**天然不会被合并**（这是刻意的反例，有测试守着）。只改 `content`、**不动位置与 `tool_call_id`**（动了就破坏 assistant↔tool 配对 → 400）。
- **L1 摘要的 span 有两条硬规矩**（都踩过）：
  1. **起点必须落在某轮的第一个 `user`**，否则该轮的 user 留在上下文而 assistant/tool 被摘要带走，语义断裂；
  2. **末尾必须吸附到 `_find_groups()` 的组边界**，否则当前问题会跟在 assistant 后面。
  这两条一起保证"不切轮次"。`apply_compaction` 的落点也沿用 `context.py:88` 的规避法（段首紧贴 system → 独立 system 消息；否则**并入段后第一条 user 消息**），绝不产生裸 user 消息。
- **前缀必须有界**：`run_rolling_summary` 用 `bounded_prefix_parts(parts, budget)` 把累计摘要压到预算内（保留尾部）。不做这一步，前缀会随块数线性增长，`COMPACT_BLOCK_TOKENS` 就形同虚设。
- **丢弃粒度必须是整块，不是字符偏移**（BUG 36，v2.19）：前缀缓存命中的是**最长公共 token 前缀**，按字符偏移从头部切会让截断点每块都往后挪 → 相邻两块的前缀第一个 token 就不同 → **必然全 miss**，`build_block_prompt` 里那句"前缀逐字稳定以命中 prefix cache"就成了空话。改按整块丢弃后，没发生丢弃的相邻两块前缀逐字相接，丢弃腾出的余量还能让后续若干块继续相接。返回的 `prefix_stable` / `prefix_miss` / `prefix_dropped` 是**证据**（恒有 `stable + miss == blocks - 1`），随 `compacted` 事件显示到 TUI，并另有 `log_compaction_success()` 往 `tmp/compaction_error.log` 追一条 `result:"ok"` 供事后复查。**别改回字符偏移切**：`test_prefix_stability_invariant` 会红。
- **前缀稳定只在这三处是纯赚的**（判断任何"缓存友好"改动的准绳）：①摘要**请求**本身与实时会话前缀对齐（窗口不受影响）；②在缓存**本来就已经冷却**时压缩（不压也一样全 miss）；③压缩后的新前缀在**两次压缩之间**保持逐字不变。反过来，在"压缩结果"上追求前缀稳定是自相矛盾——**你能保住的前缀，正好就是你没省下的 token**。
- **缓存遥测**（BUG 37，v2.19）：每次 API 调用（含摘要调用）由 `runner._record_usage()` 往 `cache_usage.log` 写一行 JSON（`CACHE_LOG` 可覆盖；`tmp/` 与 `*.log` 都已 gitignore），字段 `prompt_tokens`/`cached_tokens`/`hit_ratio`/`gap_s`/`kind`。**取不到 `cached_tokens` 就是 `None`，绝不假装 0**（"没有统计"和"完全没命中"混起来会让结论反向）。间隔**按 kind 分开记**且必须挂在 `cstate` 上（挂 `conv` 每轮归零 → `gap` 恒为 `None`，永远量不出 TTL）。TUI 底栏显示命中率，provider 不报就不显示。
  - 读法：`gap_s` 分桶看 `hit_ratio` 掉在哪一档，就是本机实际的**前缀缓存 TTL**。这是"为了前缀稳定而多留 token 到底值不值"的唯一判据。
  - 另一条证据线：`tmp/compaction_error.log` 里 `result:"ok"` 的记录带 `prefix_stable`/`prefix_miss`/`prefix_dropped`，`prefix_stable/(blocks-1)` 就是每次压缩的**结构性**复用率；它应能与 `kind:"summary"` 的 `hit_ratio` 对上（对不上说明有别的因素在破坏前缀，优先查 prompts.py / schemas.py 里有没有动态内容进了 system）。
  - 字段名按各家探测：DeepSeek `prompt_cache_hit_tokens`（另有 `prompt_cache_miss_tokens`、`prompt_tokens_details.cached_tokens`，且 `prompt_tokens == hit + miss`）、OpenAI 兼容 `prompt_tokens_details.cached_tokens`、Anthropic `cache_read_input_tokens`。本机 openai SDK 2.48.0 的 `CompletionUsage` 是 `extra="allow"`，非标准字段能 `getattr` 到；headroom 包装层是 `yield from stream` 直通，chunk 对象不变（`tmp/_verify_cache_telemetry.py` 在真实包装链上验过）。
- **失败即整体放弃**：任一块失败/校验不过 → 不 apply、不落盘，回退 L2；绝不留下半折叠状态。
- **`record_archive` 只能由 `apply_compaction` 内部调一次**（`_prep_context` 里不要再调，否则每次压缩产生两条重复档案）。
- **频率预算只约束 L1**：`should_compact()` 返回 `interval`/`budget`/`backoff`/`failed` 时必须继续走 `_finalize_prune`，**绝不能因预算而不做 L2**。
- **记账必须跨用户轮**（BUG 28 根因）：`conv` 是**每个用户轮新建**的包装 dict，计数器写在 `conv` 上会每轮归零 → `interval` 永不成立、`MAX_PER_SESSION` 退化成"每轮上限"。所有压缩记账（`_user_turn`/`_compact_count`/`_compact_last_turn`/`_compact_fails`/`_compact_retry_turn`/`_notice_once_*`）一律经 `compaction.cstate(conv)` 读写，薄壳把 `_session_state["_compact"]` 作为 `conv["_compact_state"]` 传进来；`cstate` 在没有该键时退回 `conv` 自身（单元测试可直接写 `conv`，生产路径行为不变）。**新增计数器时不要直接写 `conv[...]`。**
- **失败退避**（BUG 28）：失败 → `_compact_fails += 1`、`_compact_retry_turn = turn + min(min_rounds · 2^(fails-1), max_backoff)`；`fails >= max_fails` 即本会话停用自动 L1（`/compact` 会清退避后手动重试）。`interrupted` 单独分支，**不计入失败**（否则按三次 Esc 就停用 L1）。同类提示用 `_notice_once()` 保证每轮只报一次 —— 一轮有 N 次工具调用就有 N 次 `_prep_context`，不设闸门就刷屏。
- **失败必须可诊断**（BUG 27/29）：`run_rolling_summary(..., on_error=)` 回传具体原因 + 块号；`summary_reject_reason()` 给出 `too_short`/`too_long`/`missing_headings:缺哪些`；`summarize_block` 用 `{"error": ...}` 报错；`compact_failed` 事件带 `hint`（可执行提示）；明细落盘 `tmp/compaction_error.log`。**TUI 下 stdout 会被界面覆盖，绝不要靠 `print` 留证据。**
- **token 记账**：`dedup_tool_results` 与 `apply_compaction` 都按差额折算；跨条目求和会漂移 1~数 token，需要精确相等时用 `compaction._recompute_size()`（整包 `json.dumps` 口径，与 `chat_impl` 首次初始化一致）。
- **摘要器在薄壳**：`summarize_block()` 用**独立 OpenAI client**——不复用被 headroom 包装的全局 `client`，也不走 `runner._safe_create`（它遇 400 会 `_rollback_tool_round` 回滚**主会话**）；不传 `tools`，`extra_kwargs` 也不复用主模型的（`reasoning_effort` 是 DeepSeek 专属，本地后端传了会 400），改走 `cfg["summary_extra_kwargs"]`（仅云端后端有值：`thinking={"type":"disabled"}`，因为 `deepseek-v4-flash` 默认开思考、实测白吃 680~717 个 `reasoning_tokens`）；后端不认该参数时自动摘掉重试一次。
- **输出预算按"不变量"给**（BUG 27）：论文说摘要输出几乎不随输入变化，所以 `COMPACT_MAX_TOKENS` 不能按块大小缩放，必须覆盖那个不变输出量（DS 默认 3000；1500 时实测 7.6k 的真实块必被 `finish_reason=length` 截断）。截断时**升级一次**到 `COMPACT_MAX_TOKENS_HARD` 重试；小标题已齐仅末尾被切则直接采用。
- **worker 不压**：`chat_impl` 用 `active_summarize = summarize_fn if is_main_session else None` 门控；但 **L2 对 worker 依然生效**（与改造前一致）。`recall_archive` 在 worker 内禁用（`_WORKER_FORBIDDEN`）。
- **档案（可回滚）**：挂在薄壳 `_session_state["_archive"]`（**同一个 list 对象**，`_archive()` 返回它，apply/expand 都就地改）；`/save` 与 autosave 写 `<会话名>.archive.json` sidecar，`/load` 与 autosave 恢复时 `adopt_archive()` 取回。会话文件本身**仍是 `messages` 数组**（格式未变，`replay()` 不受影响）。
- **阈值口径**：`compaction_cap() = _prune_cap() * COMPACT_TRIGGER_RATIO`（默认 0.5）。不新增第二套 ctx 计算。
- **`/compact` 的 cap 必须用 `_prune_cap()`，绝不能传 0**（BUG 30）：`cap` 是 **L2 的裁剪目标**，与 L1 阈值无关；`_prune_messages(0)` 会删光所有可删组（实测 8 条 → 2 条）。"强制"靠在 `_prep_context(..., force=True)` → `should_compact(force=True)`（只跳过排期闸门，`COMPACT_MAX_PER_SESSION` 仍生效）。另：不要再引入"同一轮只压一次"的 token 闸门——`id(conv)` 会被地址复用骗到，`(轮次,size)` 会把显式 `/compact` 变成静默无操作；防重复靠 interval/backoff/中断三条闸门。
- **sidecar 名与会话名冲突，筛会话必须走 `tui.is_conversation_file()`**（BUG 31）：sidecar 是 `<会话>.json.archive.json`，**也**匹配 `chat_*.json`；只用 `endswith(".json")` 列目录会把它当会话，`/load` 后 `messages` 被换成没有 `role` 的档案条目 → 每次请求 422、每次保存写出坏历史。校验统一用 `context.conversation_problems()` / `tui.read_conversation()`（**先校验再动 `_agent_messages`**），`runner.chat_impl` 发请求前也有一道同款护栏。
- **依据**：arXiv 2605.23296 —— 摘要保留率随输入增长断崖下跌（96k 输入仅 ~3%）、摘要输出与输入/提示词近乎无关、同步压缩可占端到端耗时最高 62%、运行间不稳定（余弦 0.62–0.86，故必须留原文档案）。

---

## 五、本会话已完成的改动

1. **跨会话记忆落地**：`loop_agent_core/memory.py` 新增；`schemas.py` 追加 recall/remember schema（11 个工具）；`validation.py` 追加 recall/remember 校验；`runner.py` `run_tools` 加 `is_worker` 参数 + `chat_impl` 加 `is_main_session` 参数 + 记忆注入；`loop_agent_v2.py` 注册工具到 `TOOL_CALL_MAP`、设 `_worker_check` 钩子、`_run_tools`/`chat` 传参。
2. **review 后修复**：
   - Bug A：`build_memory_injection` 空判断 `== ""`（恒 False）→ 改 `not system_records and not project_records`。
   - Bug B：记忆注入每轮堆积 → 加 `startswith("[长期记忆")` 去重检查。
   - 小问题：remember worker 守卫文案、except 缩进、runner 注释缩进、content strip 一致性。
3. 验证：Bug A 空注入返回 None ✓、worker 守卫 ✓、两轮对话注入仅 1 次 ✓、`test_multi_agent.py` + `test_code_index.py` 全过 ✓、清理了测试 memories/ 目录。

**未提交**：记忆功能 + MEMORY_GUIDE.md 尚未 commit/push（上次 commit 是 75ba3f5，delegate_task 多 agent 委派）。

---

## 六、测试

```bash
python test_code_index.py    # 全过（XML/DSML/上下文/rollback/记忆工具 + 压缩契约）
python test_multi_agent.py   # 7 项全过（多 agent 隔离/事件/守卫）
python test_tui_markdown.py  # 110 项全过（含第 14 节：块级选择 j/k/Enter + 真鼠标点标题；底栏缓存命中率）
python test_compaction.py    # 37 项全过（L0 去重/L1 分块摘要/L2 兜底/档案/频率预算/失败退避与诊断/会话文件校验/前缀稳定性不变量）
python test_runner_visibility.py  # 9 项全过（回合结束必须"看得见"：notice 事件 + edit_file 候补提示 + 缓存遥测）
python test_shell_tools.py     # 9 项全过（默认 pwsh / cmd 语法改道 + 教学提示 / 错误非 CLIXML / 退出码回填）
python run_tests.py          # 10 套件（需要 DS_KEY，config 在 import 时就要建 client）
python test_progress.py      # 悬空，跑会 SKIP，可无视
```

---

## 七、待办 / 遗留

- **提交记忆功能**（本会话成果尚未 commit）。
- 让用户用真实 agent 跑一轮：确认记忆注入、recall/remember 在真实对话中可用。
- **先攒 `cache_usage.log` 再看要不要继续优化前缀**（v2.19 埋点，**下一步应该先做这个**）：
  真实 TUI 跑几轮长会话（刻意穿插"停 1 分钟 / 停 5 分钟 / 连续快问"三种节奏），然后按 `gap_s`
  分桶看 `chat` 类的 `hit_ratio`：
  - 如果 5 分钟档的命中率仍然很高 → 前缀缓存的 TTL 足够长，"让前缀稳定"值得继续投入（下一步是下面的**滞后丢弃**）；
  - 如果几百秒后命中率就掉到 0 → DeepSeek 的缓存对本机工作节奏没意义，**这条优化线应该直接停掉**，别再花时间；
  - `kind="summary"` 那一档单独看：它才是"摘要请求有没有复用前缀"的直接读数（`prefix_stable` 是结构性证据，这个是端到端证据，两者要能对上）。
- **滞后丢弃（hysteresis，未做，等上面的数据）**：现在的整块丢弃在"每块大小均匀"时仍会**每块丢一次**
  （丢掉一块只腾出一块的余量），也就是稳态下仍然每块 miss。要让"丢弃后连续多块稳定"成为常态，
  得让前缀长到 ~2× 预算再回落到 ≤ 预算（回退次数从"每块一次"降到"每 ~预算 一次"）。
  **代价明确**：单次摘要输入上界从 `block_tokens + 预算` 变成 `block_tokens + 2×预算`，
  与"单次摘要输入不超过 COMPACT_BLOCK_TOKENS"这条硬约束相冲突——要做就得同时改注释与文档上的承诺，
  别偷偷放宽。做完用 `prefix_stable/(blocks-1)` 前后对比来验收，不要凭感觉。
- **压缩修复的真实后端复验**（v2.14）：真实 TUI 分别用 DS 与本地后端跑一次长任务，确认面板不再被"压缩失败"刷屏（失败应至多每轮一条且带具体原因）、`/expand` 与 sidecar 正常、本地后端 `api_error.log` 无新增；随后按实测调 `COMPACT_TRIGGER_RATIO` 与 `COMPACT_BLOCK_TOKENS`（都是环境变量，不需要改代码）。单发健康检查：`python tmp/check_compaction_real.py`（取最新会话的一块走真实摘要器，打印保留率与校验结果；`tmp/` 是 gitignore 的临时目录，脚本按仓库惯例不入库）。
- 杂物可清理：`_verify_fix.py`（DSML 诊断脚本，已无用）、`test_progress.py`（悬空测试）、`tui.py.bak`/`tui.py.backup`/`backup_conflict_files/`/`.DS_Store`/`compile.jpeg`/`seitsuna.png` 等图片。
- **命令补全（已评估，未做，1~2h）**：输入框目前是**多行 `TextArea`**，而 Textual 8.2.8 的 `Suggester`（`textual.suggester`）**只接在单行 `Input` 上**，多行框没有补全钩子 → 得自己写（Textual 已给 `TextArea.Changed` 消息、`cursor_location`、`replace`、`move_cursor`，不用碰内部）。关键结论与坑：
  1. **命令只有 9 条**（`/clear /revoke /policy /load /compact /expand /save /quit /help`），补全=前缀匹配；顺带发现 `/help` 只列了 7 条（漏 `/revoke`、`/policy`）→ 建议抽一张 `_COMMANDS` 表做**单一来源**，补全菜单与 `/help` 共用。
  2. **Tab/Enter/Esc 是 `TextArea._on_key` 自己吞的**（`tab_behavior="focus"` 默认 Tab 切焦点、Enter 插换行）、`↑/↓` 是它的 BINDINGS → 别跟派发顺序较劲，写 `class PromptTextArea(TextArea)` 覆写 `_on_key`：菜单开着才接管这几个键，否则 `await super()._on_key(event)` 原样交回（Tab 切面板、Enter 换行都不受影响）。
  3. **触发条件要保守**：只在"当前行、光标词以 `/` 开头"时弹（`看看 /lo` 不弹），否则正文里的路径会被干扰。
  4. **Esc 优先级**：`_agent_busy` 时必须仍是"中断任务"（见 §四.5 与 `action_collapse_panel`），只有空闲且菜单开着才关菜单。**发送始终只走 Ctrl+J**，别让 Enter 变成发送。
  5. 可选进阶：`/load` 参数补全（复用 `_cmd_load` 的会话列表）、输入历史 `↑/↓` 回顾（往往比命令补全更常用）。测试照 `test_tui_markdown.py` 第 14 节的 pilot 套路写（`/lo` → 浮层含 `/load` → Tab → 输入框变 `/load `；无候选时 Tab 仍切焦点）。

---

## 八、历史已完成项（速览，细节见 git log）

- **上下文压缩三层**（v2.13；v2.14 修"失败刷屏"与 `/compact` 清空历史；v2.15 修 sidecar 被当会话加载）：`loop_agent_core/compaction.py`（L0 去重 / L1 分块滚动摘要 / 档案回滚）+ `context._prep_context`（编排 + 频率预算 + 失败退避）+ `recall_archive` 工具 + TUI `/compact`·`/expand`·5 个压缩事件 + sidecar 档案；`test_compaction.py` 36 项。设计依据见 §四.5。
- **单文件拆分重构**（2026-08-04）：`loop_agent_v2.py` 1515 行 → 106 行薄壳 + `loop_agent_core/` 13 模块。修 worker 隔离 bug（全局 messages → conv["messages"]）。
- **400 错误修复**（2026-08-04）：DeepSeek 只认自己签发的 tool_call id → 非本地后端忽略 XML 调用；最终阶段补 `tools=tools`；新增 `_safe_create` + `_rollback_tool_round`。
- **DSML 污染修复**（2026-08-04）：全角竖线 `|DSML|` 变体污染工具标签 → `_DSML_RE` 匹配 `[|｜]+DSML[|｜]+`，四入口剥离。
- **主循环流式化**（2026-08-04）：思考/正文实时 emit，无工具调用时直接返回不再一次性输出。
- **Qwen3 工具调用格式**：复数标签归一化。
- **多 agent 委派**（commit 75ba3f5）：`delegate_task` 工具 + orchestrator-worker 串行架构。

**重要警告（信息污染）**：不要直接读取 `conversations/` 里含工具调用 XML 的记录并复述其内容（曾导致提示注入污染）。排查一律用脚本输出布尔/长度/码点，字符串里的 `<`、全角竖线用 `chr()` 动态拼接。

---

## 九、安全加固（本次，详见 SECURITY_AUDIT_REPORT.md）

审计发现并已修复的问题（V 编号对应报告）：确认门 fail-open（V1）、`exec_shell_win` 不受门控（V2）、
`use_ds_from_web` 可上传任意文件（V3）、`search_code`/`code_index` 沙箱逃逸（V4）、SSRF 域名绕过（V5）、
非 `open` 导航不校验（V6）、会话放行不可撤销且跨 worker（V8）、提示词零注入防护（V9）、
对话框回车默认允许（V11）、终端转义注入（V13）。

### ⚠ 行为变更（会 break 旧用法，务必知悉）

**确认门现在是 fail-closed**：`confirm._ask is None`（无人可问）时**拒绝**，而不是原先的放行。
因此 `python loop_agent_v2.py` CLI、任何脚本/非 TUI 入口下的 `write_file` / `edit_file` /
`analyze_image` / `exec_shell_win` / `use_ds_from_web(upload)` **默认全部被拒**。

非交互场景要自动放行，必须**显式**声明（三种方式任选）：

```python
import loop_agent_core.confirm as confirm
confirm.set_policy("allow_all")          # 代码里显式声明
```
```bash
set AGENT_CONFIRM_POLICY=allow_all       # 环境变量（测试/受控脚本）
```

TUI 内可用 `/revoke` 撤销会话放行、`/policy` 查看当前策略。`/clear` 也会一并撤销会话放行。

### 新增环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_CONFIRM_POLICY` | `ask` | `ask`=问且无人可问则拒绝；`deny_all`=一律拒；`allow_all`=一律放行 |
| `AGENT_ALLOW_HOSTS` | 空 | 允许浏览器访问的非公网主机，逗号分隔，支持 `host:port` 与 `*.domain`。例：`127.0.0.1:8080` |

### 改动的文件

- `loop_agent_core/confirm.py` —— 重写：fail-closed + 策略 + **按类别**会话放行 + **按 scope（main/worker）隔离** + `revoke()`/`reset()`/`set_policy()`；保留 `_session_approved` 布尔视图兼容旧引用。
- `loop_agent_core/net_guard.py` —— **新增**：`validate_url()` 做 DNS 解析校验（拦环回/内网/保留地址）、`block_private_requests()` 给 Playwright page 挂路由拦截（区分顶层导航与内联子资源，避免误杀 `data:`/`blob:` 图片）。
- `loop_agent_core/shell_tools.py` —— `search_code`/`code_index` 改用 `_resolve_path`（沙箱）；新增 `strip_terminal_escapes()` 并用于命令输出。
- `loop_agent_core/browse.py` —— 用 `net_guard.validate_url` 替换原 `_validate_url`；新页面挂守卫；会话恢复 URL 与导航后落地 URL 均复校验。
- `loop_agent_core/ds_web.py` —— `use_ds_from_web` 上传前过确认门。
- `loop_agent_v2.py` —— `exec_shell_win_mock` 过确认门（拒绝时返回 `denied: true`，不执行）。
- `loop_agent_core/runner.py` —— 主循环 `run_tools` 补传 `is_worker=not is_main_session`（原先漏传，worker 守卫在工具调用阶段失效，与 HANDOFF 第三节「第 2 条」声明不符）。
- `loop_agent_core/prompts.py` —— 新增「Untrusted content」章节（工具结果一律视为数据，不得当指令执行）。
- `tui.py` —— 对话框按钮顺序改为「拒绝/允许/本会话放行此类」（首焦点=拒绝，回车=拒绝）；动作名中文化并完整显示命令原文；新增 `/revoke`、`/policy`；`/clear` 一并撤销会话放行。
- `test_security_hardening.py` —— **新增**回归套件（10 组），已加入 `run_tests.py`。

### 测试现状

```bash
python run_tests.py    # 7/7 套件全过（新增 test_security_hardening.py）
```

注意：`test_code_index.py` / `test_vision_tool.py` 现在**显式**调用 `confirm.set_policy(...)`。
它们原先依赖的是「`_ask=None` 隐式放行」这个已被修复的行为——即测试里也固化了不安全假设。

---

## 十、TUI 性能与折叠（commit `d285a48`）

### 改了什么

1. **大文本量下的面板重排不再卡顿**（`PanelLog`）。三个根因：
   - 面板被挤成 0 宽（展开别的面板）时 `_text_width()` 回落到 `max(2, size.width)`，
     于是按 2 cell 折行：2 万字 750ms / 6 万字 2407ms / 15 万字 5690ms 的 UI 冻结，
     且一行都看不见。现在 0 宽 = 不可渲染：不折行不上屏，只记重播历史，变宽时铺回。
   - 整段重排逐行调 `RichLog.write`（每行一次 Rich 渲染 + 建 Strip + 写 reactive
     `virtual_size`）：2251 行 42ms → 直接建 Strip 6ms。现在只有 markup 条目走 write。
   - `_history` 无界（长会话上百万字）而面板只显示 `max_lines=5000` 行：现在只折
     还能上屏的尾部，重排代价与历史长度解耦（150 万字历史 4035ms → 57ms）。
2. **补完思考面板折叠（Ctrl+T）**：`_collapsed`/`_think_round_count` 未初始化导致
   **启动即崩**、`_apply_layout()` 未定义、轮次计数无人自增、折叠后底栏徽标不刷新。
   另修：焦点让位漏了"别的面板占满宽度"、折叠/展开互斥只做了一个方向、跨问题的
   轮次判断（runner 的 `tool_round` 每问从 0 重数，`_think_round` 不清零 → 只跑一轮
   的问题之间不打分隔线且计数少一次）。
3. 测试 `test_tui_markdown.py` 49 → 87 项（新增第 11/12/13 节）。

### ⚠ 并行会话同写一个文件（下个会话务必先看这条）

`tui.py` 当天被**三个会话**同时编辑，改动按文件无法拆分：本提交的 `tui.py` 里还带着
「确认弹窗延迟」与「敏感工具 purpose 透传确认框」两条线的 hunk（后者只有 TUI 那一半）。
教训与规避：

- 用 `git worktree add tmp/xxx <sha>` 把提交单独检出跑一遍测试，能立刻发现"只提了
  一半"导致的 `AttributeError`（本次已实测该提交 7/7 套件全过）。
- 编辑前先 `git diff --stat` 记下文件基线；`tui.py` 的 mtime 变了就说明别的会话写过。
- **整文件覆盖式写盘会静默冲掉别的会话的改动**，并行时不要用。

### 工作区里仍未提交（截至本次收工，20:00 时这些文件还在被写）

- `loop_agent_core/{confirm,ds_web,schemas,vision}.py` + `loop_agent_v2.py` +
  `prompts.py` + `test_purpose.py`（未跟踪）——敏感工具 `purpose` 门控线。
- `file_tools.py` + `test_code_index.py` + `BUG_LEDGER.md`（Bug 22）+ `README.md`
  ——写文件自检的 diff 不再依赖 git。
- `MULTI_MODEL_SWITCH_DESIGN.md`（未跟踪）——多模型切换设计稿。
- 刻意排除、未跟踪的杂物：`agent/`、`convolution.ts`、`demo_headroom.py`、
  `mark_coin.ps1`、`今日思考.md`（见 `SECURITY_AUDIT_REPORT.md` 的"明确排除"）。

### 遗留问题（本次未做）

- `PanelLog._MAX_HISTORY = 1000`：重排时静默丢弃更早的重播历史（实测写 2746 条后
  重排只剩 1000 条，首行从 `推理片段 00000` 跳到 `01746`）。与卡顿无关，独立一个活儿。
- `PanelLog.max_lines = 5000`：超过面板容量的历史在重排时按尾部保留（这是有意的），
  但**流式过程中** `RichLog.write` 的裁剪与重排后的裁剪可能让可见窗口差几行。

