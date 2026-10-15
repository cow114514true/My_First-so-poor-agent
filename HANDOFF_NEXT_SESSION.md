# 交接文档 — 下个会话从这里继续

> 2026-10-13。基线 commit `6564406`。

**只写三样**：① 代码里看不出来的约束与理由；② 拒绝过的方案；③ 未落地的下一步。函数怎么工作、有哪些模块与工具、各套件断言几条——不写，读代码与 `README.md`（模块与工具清单的唯一权威）。

历史过程去 `git log` / `BUG_LEDGER.md`（Bug 1–37），逐条设计去各自的 `*_DESIGN.md`。

---

## 一、代码里读不出来的约束

每条都是"读代码时觉得可以随便改、其实不行"。

### 后端
- `_worker_state = threading.local()`。曾写成全局布尔，并发子 agent 互踩。不要退回全局。
- XML 工具调用只在 `_is_local_backend()` 时解析。DeepSeek 不认合成 id `xml0`，回送必 400。`_safe_create` + `_rollback_tool_round` 专治此 400，不是冗余防御。
- `runner.chat_impl(..., is_main_session=False, summarize_fn=None)` 两个默认值不可去掉（薄壳与 worker 调用点依赖缺省）。
- worker 禁用工具靠 `run_tools(is_worker=...)` 统一拦截（`_WORKER_FORBIDDEN`），不靠各处 if。

### 记忆
- 注入用 `startswith("[长期记忆")` 去重。曾每轮堆积一次。系统级注入完整 content，项目级只注入 topic 索引。
- `content.strip()` 只去两端空白、保留中间换行。已与用户确认，不要"顺手修"。
- `system.json` 锚定 agent 代码目录，`project.json` 锚定 `WORK_DIR`。刻意区分两种归属。

### shell（BUG 35）
- 绝不用 `-EncodedCommand`。它把错误流序列化成 CLIXML，模型读不到错误 → 判定"pwsh 坏了" → 退回 cmd。现走 `-File` + `tmp/` 临时 `.ps1`（UTF-8-BOM），让 `ParserError` 可见。
- `_PS_SYNTAX_MARKERS`（"绝不改道"白名单）别动。新增 cmd-only 语法改 `_CMD_ONLY_SYNTAX`，并确认它不会把真正的 pwsh 命令推给 cmd。
- `pwsh` 默认值散在四处（schema enum/default、工具描述、core 与薄壳函数默认值）。改要一起改，曾漏过。

### 思考面板（v2.18）
- `_block_line`（块 key → 标题行号）每次 reflow 必须重建，`max_lines` 裁行首时同步上移（`flush_pending` 的 `drop`）。否则点谁都折第一块。
- 折叠块的点击区要算进尾部预览那一行。否则"看得见点不动"。
- 折叠态判定靠 `_tail_history`。窗口从块体中间开始时必须往前补块头，否则重排把折起内容整段铺回。
- `j/k/Enter/空格` 绑在 `PanelLog`、`z/o` 留在 App。分层有意，不是漏绑。

### 压缩（v2.13–v2.19，最容易改坏）
- 顺序固定 L0 → L1 → L2，只在两次 API 调用之间执行。流式中绝不压。
- L0 判定是 `(tool_call_key, result_digest)` 双条件，`tool_call_key` 做 `json.loads`+`sort_keys` 归一化。`read_file` 不同区间/路径天生不合并，是刻意反例、有测试守。只改 `content`，不动位置与 `tool_call_id`（动了破坏 assistant↔tool 配对 → 400）。
- L1 起点落在某轮第一个 `user`，末尾吸附 `_find_groups()` 组边界，落点沿用 `context.py` 的规避法。绝不产生裸 user 消息。
- L1 前缀用 `bounded_prefix_parts(parts, budget)` 压进预算。否则前缀随块数线性增长，`COMPACT_BLOCK_TOKENS` 形同虚设。
- 丢弃粒度是整块，不是字符偏移（BUG 36）。前缀缓存命中的是最长公共 token 前缀；按字符偏移从头部切会让截断点每块后移、相邻两块第一个 token 就不同 → 全 miss。别改回字符偏移（`test_prefix_stability_invariant` 会红）。`prefix_stable + miss == blocks - 1` 是恒等式，可当断言。
- 前缀稳定只在三处纯赚：摘要请求本身与实时前缀对齐、缓存已冷却时压缩、压缩后新前缀在两次压缩间逐字不变。在压缩结果上追求前缀稳定自相矛盾——能保住的前缀正是没省下的 token。别当万能优化目标。
- 缓存遥测：取不到 `cached_tokens` 就是 `None`，绝不假装 0。间隔按 `kind` 分开记、挂 `cstate`；挂 `conv` 每轮归零，`gap` 恒 `None`。
- 记账必须跨用户轮（BUG 28 根因）。`conv` 是每个用户轮新建的包装 dict，计数器写它上面每轮归零 → `interval` 永不成立。所有压缩记账经 `compaction.cstate(conv)`。新增计数器不要直接写 `conv[...]`。
- 频率预算只约束 L1。`should_compact()` 返回 `interval`/`budget`/`backoff`/`failed` 时仍必须走 `_finalize_prune`，绝不因预算不做 L2。
- `/compact` 的 cap 必须用 `_prune_cap()`，绝不传 0（BUG 30）。`cap` 是 L2 裁剪目标，`_prune_messages(0)` 删光可删组（实测 8 条→2 条）。
- 不要再引入"同一轮只压一次"的 token 闸门。`id(conv)` 会被地址复用骗到，`(轮次,size)` 让显式 `/compact` 静默无操作。曾两次想加、两次都错。
- `record_archive` 只由 `apply_compaction` 内部调一次。`_prep_context` 里再调产生重复档案。
- 摘要器用独立 OpenAI client：不复用被 headroom 包装的全局 client，不走 `runner._safe_create`（遇 400 会回滚主会话），不传 `tools`。`summary_extra_kwargs` 仅云端有值（`deepseek-v4-flash` 默认开思考、实测白吃 680~717 `reasoning_tokens`）；后端不认该参数时自动摘掉重试一次。
- `COMPACT_MAX_TOKENS` 不随块大小缩放（BUG 27）。DS 默认 3000；1500 时 7.6k 的真实块必被 `finish_reason=length` 截断。截断时升级一次到 `COMPACT_MAX_TOKENS_HARD`。
- 任一块失败/校验不过 → 不 apply、不落盘，回退 L2，绝不留半折叠。`interrupted` 单独分支、不计失败。
- TUI 下 stdout 被界面覆盖，绝不靠 `print` 留证据。诊断走 `tmp/compaction_error.log` 等文件。此条违反过不止一次。
- 阈值定义 `compaction_cap() = _prune_cap() * COMPACT_TRIGGER_RATIO`（默认 0.5）。不新增第二套 ctx 计算。
- worker 不压 L1，L2 对 worker 依然生效。
- 依据：arXiv 2605.23296（摘要保留率随输入断崖下跌，96k 输入仅 ~3%；同步压缩占端到端耗时最高 62%；运行间不稳定，余弦 0.62–0.86 → 必须留原文档案）。

### 会话文件 / 安全
- sidecar 叫 `<会话>.json.archive.json`，也匹配 `chat_*.json`。凡按文件名筛会话必须走 `context.conversation_problems()` / `tui.is_conversation_file()` / `tui.read_conversation()`，先校验再动 `_agent_messages`。别用裸 `endswith(".json")`（BUG 31）。
- 确认门 fail-closed：`confirm._ask is None` 时拒绝。非 TUI 入口下 `write_file`/`edit_file`/`analyze_image`/`exec_shell_win`/`use_ds_from_web(upload)` 默认全拒。自动放行必须显式声明（`confirm.set_policy("allow_all")` 或 `AGENT_CONFIRM_POLICY=allow_all`）。测试不要改回依赖隐式放行（`test_code_index.py`/`test_vision_tool.py` 现显式设策略）。
- 残留未闭环：V7/V10（浏览器工具仍在门外、`remember` 持久注入面）、V14/V15（人工轮换密钥、ACL 需用户决策）。见 `SECURITY_AUDIT_REPORT.md` §9.5。

---

## 二、外部契约（改名/改签名前必看）

- `tui.py` 读 `CURRENT_MODEL`、`system_prompt`，写 `loop_agent_v2._shell_output_queue`（薄壳据此注入 `exec_shell_win_mock` 的 queue）。
- 薄壳必须继续导出：`TOOL_CALL_MAP`、`_run_tools(use_tool_calls, emit, conv)`（3 参数签名 + 运行时查 `delegate_task_mock`，保留 monkeypatch 语义）、`_session_state`（`_compact`/`_archive` 两键）、`_archive()`、`_compact_state()`、`_summary_client()`、`summarize_block`、`recall_archive_mock`、`clear_archive`、`archive_sidecar_path`、`persist_archive_sidecar`、`adopt_archive`、`expand_compaction`、`compact_session`、`chat(question, on_event=None, _conv=None)`。
- `_archive()` 必须返回 `_session_state["_archive"]` 这同一个 list 对象（apply/expand 就地改）。
- core 侧 `memory._worker_check`、`confirm._worker_check` 由薄壳 import 后设置。
- 测试 import 的私有符号很多（`_parse_xml_tool_calls`、`_prune_cap`、`_find_groups`、`_msg_size` …）。改名前先 grep 测试目录，不要按"私有"判断可删。

---

## 三、测试

```bash
set DS_KEY=sk-dummy      # cmd / $env:DS_KEY="sk-dummy" (PowerShell)：必须先给值，见下
python run_tests.py      # 逐套件起子进程跑，退出码非 0 = 有失败
```

- 套件在 `tests/` 下。清单的唯一权威是 `run_tests.py` 的 `SUITES`（当前 **15** 个：`test_code_index` / `test_multi_agent` / `test_interrupt` / `test_vision_tool` / `test_shell_tools` / `test_tui_markdown` / `test_security_hardening` / `test_purpose` / `test_compaction` / `test_runner_visibility` / `test_trace` / `test_prod_recording` / `test_replay` / `test_migrate` / `test_shrink`）。新增套件必须加进去，否则不会跑到。
- 纯逻辑、不触网、不需要本地服务，但 **`DS_KEY` 必须有值**：`loop_agent_core/config.py` 在 import 时构造 `OpenAI()`，缺 key 直接 `OpenAIError: Missing credentials` → 只要 import 到 config 的套件全挂。给假值即可，不会真的调 API。此坑已写进 `config.py` 与 `run_tests.py` 注释。
- 套件落在 `tests/` 后，各文件的 `sys.path` 补丁与 `tmp/`、`conversations/` 锚点已改成"仓库根"。新加套件照抄 `tests/test_compaction.py` 开头那两行。
- `test_vision.py` 不在 SUITES 里，是**手动**视觉连通性脚本（需本地 llama.cpp）。别当漏登记去"补上"。

---

## 四、下一步（按优先级）

### 1) Trace & Replay 施工（阶段 0–5 已落地，欠账见下）
- 规格与落点：`TRACE_REPLAY_DESIGN.md`（v13）+ `TRACE_REPLAY_BUILD.md`（§1 交付物表、§2 代码落点表、§3–§8 各阶段规格、§9 验收清单、§10 已定项）。落点以**符号名**为准，行号会随同文件其他改动漂移。
- 已落地：阶段 0（`llm.py` 网关 / `toolclass.py` / 断言 id 词表）、阶段 1（`trace.py` / `checkpoint.py` / `cassette.py` / `tools/trace.py`，含 §4.10 生产开录）、阶段 2（`tools/trace_migrate.py`）、阶段 3（`replay.py`）、阶段 4（`overlay.py` + L2 工具策略）、阶段 5 的**阶段 1（轮级 ddmin）**。阶段 5 的阶段 2（历史组）只留 `history_group_units()` 接口。
- 验收：#1–#38 全在套件里，`run_tests.py` 的 `SUITES` 共 **15** 项，`test_trace.py` 35 条 / `test_replay.py` 48 条 / `test_migrate.py` 14 条 / `test_shrink.py` 11 条 / `test_prod_recording.py` 10 条。
- 生产开录怎么用：`TRACE_ENABLED=1 python loop_agent_v2.py`（TUI 直接 `python tui.py`）。开关只在会话起点求值一次；档位归 `TRACE_LEVEL`（默认 `assert`）；失败现场记 `retention=failure` 默认保留，`python -m tools.trace clean --older-than 30d` 只清正常会话，清失败现场要显式 `--include-failures`。
- 入库 fixture 在 `tests/fixtures/replay/`：`fingerprint_tool_round`（文件级指纹 + 一次 `get_date`）、`positional_stuck`（真失败现场，3 条响应、`turn_end:reason=stuck`，L3 唯一真实输入）、`l2_file_ops`（B 类写 → A 类读 → 递归扫描，L2 三类重写的真覆盖）。`tests/fixtures/replay/_regenerate.py` 是同源闸门：重录只写 `tmp/`、`--check` 不写盘；改了请求形状后 `test_committed_fixtures_are_regenerable_and_drift_is_caught` 会按设计报警。**换检出目录会误报**（`meta.json` 写死绝对 `workspace_root`），故不要在 `git worktree` 里跑套件。
- 已知欠账（按价值排）：
  1. `tools/trace.py` 的 `cmd_export` 有 `--shareable` 参数但**完全没接线**，只做 `--format otel|json` 派生视图。按 §8.4 口径要么做成诚实的最小版本（抹 URL query / 路径 / 用户名并在 meta 标 `redacted`，明确不做正文语义脱敏、且承认脱敏后不可回放），要么把参数删掉。
  2. L3「真删成功」的**证据很薄**：唯一真现场 0 删除，而这是真结论不是没试到（`positional_stuck` 只有 3 条响应，只留 2 条时 `_empty_retries` 到不了预算，`reason=stuck` 那条饱和分支根本不触发）。轮级 ddmin 真删成功、`repatch_b_group` 删除后重算、`unconsumed` 非空这三条路径从未被真跑过——要拿到得先造一份**多用户轮**的真现场。
  3. CLI 层与真 fixture 之间没有用例：`test_cli_replay_overlay_flags` 用的是合成 cassette，只证明命令行参数透传到 `ReplaySession`。
  4. 回放匹配器是否改成「按签名分桶的 FIFO 游标」（支持代码合法地增删/重排调用）待拍板；动的是已入库的回放核心。
  5. L3 报告里 `replays`（含导出后复核）与 `derived_from` 里记的数字不一致。
  6. `tools/trace.py` 的汇总行打印「请求 ? 条」：cassette 的 `meta.json` 没有 `request_count` 键，纯显示问题。
  7. 生产的真实条件**部分**已验证：`TRACE_LEVEL=stream` 落在生产入口（`test_stream_level_is_honoured_at_production_entry`，真 CLI 入口断言 `response_chunk` 落盘）、TUI 的 Esc 中断（`test_tui_escape_interrupts_agent_and_closes_trace`，慢流后端里按键 → `turn_end:reason=interrupted`）、CLI 的 Ctrl-C（`test_ctrl_c_still_closes_as_interrupted`）三条已覆盖。**仍未验**：真网络 / 真 key、真 TTY（全是 headless 驱动）、30 天保留期、>5MB 告警。Esc 用例里两处 headless 专属的坑写在了启动器注释里（进程级 `interrupt_event` 会从前一次会话漏进来，得自己清；焦点在 TextArea 时 escape 不冒泡到 App 绑定，故先试 `pilot.press` 再直接调绑定动作）。
- 唯一一次拿它排查真问题的记录：`debug_filtered.txt` 里那条 `turn_end:stuck / retries=4 / reasoning=我再想想`。开录后 trace 一次给出三件手工留痕给不了的事实——nudge 被拼进末条 user（四轮 `msgs=2`、字节数 122 → 292 → 462 → 632，用户原话被逐轮改写）、`llm_request` 4 次而 `thinking_only_retry` 只有 3 条（计数先加后判，第 4 次请求已经发出去）、`validate_events()` 报 `request_index 回退：3 → 0`（`set_tool_round()` 清零了请求号）。三条都已修并补了回归用例。那条现场**未入库**（`tmp/_probe_stuck/`，一个真 token 都没有），只作诊断材料。
- CONTENT：本文件不存放施工细节，只放下个会话必需的坐标与欠账；规格一律查 `TRACE_REPLAY_BUILD.md`。

### 2) 攒真实前缀缓存数据（埋点已就绪，成本最低）
- 现只有 2 条样本（`hit_ratio` 0.97 @ `gap_s:null`；0.84 @ `gap_s:84.8s`），不足以判断任何事。
- 真实 TUI 跑几轮长会话，穿插"停 1 分钟 / 停 5 分钟 / 连续快问"三种节奏，按 `gap_s` 分桶看 `kind="chat"` 的 `hit_ratio`。
  - 5 分钟档仍高 → TTL 够长，继续投入（下一步是滞后丢弃）。
  - 几百秒后掉到 0 → DeepSeek 缓存对本机节奏无意义，这条优化线直接停掉。
  - `kind="summary"` 单独看，与 `prefix_stable/(blocks-1)` 对照。对不上说明有别的因素破坏前缀，优先查 `prompts.py`/`schemas.py` 有无动态内容混进 system。

### 3) 滞后丢弃（hysteresis，等 §四.2 数据再决定）
现状整块丢弃在块大小均匀时仍每块丢一次，稳态每块 miss。要"丢弃后连续多块稳定"成为常态，得让前缀长到 ~2× 预算再回落到 ≤ 预算。
代价明确：单次摘要输入上界从 `block_tokens + 预算` 变成 `block_tokens + 2×预算`，与"单次摘要输入不超过 `COMPACT_BLOCK_TOKENS`"这条硬约束冲突。要做就得同时改注释与文档承诺，别偷偷放宽。验收用 `prefix_stable/(blocks-1)` 前后对比。

### 4) 真实后端复验压缩（v2.14 遗留）
真实 TUI 分别用 DS 与本地后端跑一次长任务，确认：面板不再被"压缩失败"刷屏（至多每轮一条且带原因）、`/expand` 与 sidecar 正常、本地后端 `api_error.log` 无新增。随后按实测调 `COMPACT_TRIGGER_RATIO` 与 `COMPACT_BLOCK_TOKENS`（都是环境变量，不用改代码）。单发健康检查：`python tmp/check_compaction_real.py`。

### 5) 命令补全（已评估未做，1~2h）
命令 9 条：`/clear /revoke /policy /load /compact /expand /save /quit /help`。坑与结论详版在 commit `6c91a28` 提交说明。
- 输入框是多行 `TextArea`，Textual 8.2.8 的 `Suggester` 只接单行 `Input` → 自己写（`TextArea.Changed`、`cursor_location`、`replace`、`move_cursor` 够用，不碰内部）。
- Tab/Enter/Esc 被 `TextArea._on_key` 自己吞，`↑/↓` 是它的 BINDINGS。别跟派发顺序较劲，写 `class PromptTextArea(TextArea)` 覆写 `_on_key`：菜单开着才接管，否则 `await super()._on_key(event)`。
- `/help` 漏列 `/revoke`、`/policy`。顺手抽 `_COMMANDS` 表做单一来源，补全菜单与 `/help` 共用。
- 触发保守：只在"当前行、光标词以 `/` 开头"时弹。Esc 优先级：`_agent_busy` 时必须仍是中断任务，空闲且菜单开着才关菜单。发送始终只走 Ctrl+J。

### 6) 其他已知未闭环
- `PanelLog._MAX_HISTORY = 1000`：重排时静默丢弃更早的重播历史（实测写 2746 条后只剩 1000）。与卡顿无关，独立一个活儿。
- `PanelLog.max_lines = 5000`：流式中 `RichLog.write` 的裁剪与重排后的裁剪可能让可见窗口差几行（有意为之的尾部保留）。
- `TUI._shell_queue` 无界（只在面板可见时消费）：面板隐藏时执行高产出命令，内存仍增长。需设上限（SECURITY_AUDIT V12）。
- 多模型运行时切换：设计稿 `MULTI_MODEL_SWITCH_DESIGN.md`，未开工。

### 7) 工作区现状
- HEAD = `git log --oneline -1`（本文档与 `TRACE_REPLAY_BUILD.md` 的校正在最近三个 `docs(trace)` 提交里），工作区干净。Trace & Replay 的阶段 0–5 就是最近这一串提交：`c119ac1` toolclass → `ed8fc0f` llm → `385fbdd` trace → `581bd5c` checkpoint → `a65f65f` cassette → `c69e1d5` replay → `12cafa0` 生产开录 → `1a65514`/`9ee6332` 迁移器 → `6623f29` overlay → `17481ef` shrink → `f36bffa`/`e101ba1`/`9eb6e79` 三处修复 → `b430658` 重录 fixture → `28f7294` 文档。
- 根目录已整理（2026-10-13）：11 个测试脚本 → `tests/`（`git mv` 保留历史）；`convolution.ts`、`demo_headroom.py`（依赖被排除的 headroom）、`draw_square.ps1`+`square.png`（画图产物）、`deepseek_tokenizer.py`（孤儿 HF 片段，无人引用）、`agent/`（与 agent 产品无关，实为 mark_coin.ps1）→ `archive/`。**`archive/` 只在本机，不入库**（`deepseek_tokenizer.py` 已从版本库删除，副本留在本地归档）。
- 刻意不入库的杂物（见 `SECURITY_AUDIT_REPORT.md` 的"明确排除"）：`archive/`（本地归档）、`headroom/`、`test_claude/`（两个第三方克隆，合计约 145MB）、`tools_work/`、`git/`（空仓库）、`screenshots/`（运行时产出，已 gitignore）。**没动它们**，因为都已在 ignore 规则里；要真删得单独确认。
- **`tokenizer.json`（7.8MB）与 `tokenizer_config.json` 不能移**：`loop_agent_core/tokens.py:20` 从 `WORK_DIR`（=仓库根）读它做精确计数。曾差点被当杂物归档。
- `Get_State.py` 不是杂物：README §使用指南第一步就是拿它登录 DeepSeek 存 `profile.json`。
- 已被 gitignore：`tmp/`、`*.log`、`memories/`、`conversations/`、`profiles/`、`profile.json`、`headroom/`、`test_claude/`、`debug_filtered.txt`。

---

## 五、并行会话纪律

- 编辑前先 `git diff --stat` 记文件基线。`tui.py` 的 mtime 变了，说明别的会话写过。
- 整文件覆盖式写盘会静默冲掉别的会话的改动，并行时不要用。
- 要验证某个 commit 是否自洽，用 `git worktree add tmp/xxx <sha>` 单独检出跑 `run_tests.py`。

## 六、信息污染警告

不要直接读取并复述 `conversations/` 里含工具调用 XML 的记录（曾导致提示注入污染）。排查一律用脚本输出布尔/长度/码点；字符串里的 `<`、全角竖线用 `chr()` 动态拼接。

## 七、诊断入口速查

| 想查 | 去哪 |
|---|---|
| 缓存命中率 / TTL | `cache_usage.log`（字段见下） |
| 压缩成败与前缀稳定证据 | `tmp/compaction_error.log`（`result:"ok"` / 失败原因+块号） |
| API 400 现场 | `api_error.log`、`runner._dump_api_error` |
| 被过滤的乱码内容 | `debug_filtered.txt`（`FILTERED_LOG` 可覆盖）、`turn_end:stuck` |
| 浏览器元素捕获耗时 | `tmp/browse_timing.log` |
| 压缩健康检查 | `python tmp/check_compaction_real.py` |
| bug 的根因/修复/验证 | `BUG_LEDGER.md`（Bug 1–37） |
| 安全模型与残留风险 | `SECURITY_AUDIT_REPORT.md`（§9.5 残留） |

`cache_usage.log` 字段：`prompt_tokens`/`cached_tokens`/`hit_ratio`/`gap_s`/`kind`（`CACHE_LOG` 可覆盖路径）。`cached_tokens` 字段名按各家探测：DeepSeek `prompt_cache_hit_tokens`、OpenAI 兼容 `prompt_tokens_details.cached_tokens`、Anthropic `cache_read_input_tokens`。
