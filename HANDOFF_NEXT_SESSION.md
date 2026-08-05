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
│   │                         #   （纯函数，只操作传入的 conv dict）
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
python test_code_index.py    # 18 项全过（XML/DSML/上下文/rollback/记忆工具）
python test_multi_agent.py   # 7 项全过（多 agent 隔离/事件/守卫）
python test_progress.py      # 悬空，跑会 SKIP，可无视
```

---

## 七、待办 / 遗留

- **提交记忆功能**（本会话成果尚未 commit）。
- 让用户用真实 agent 跑一轮：确认记忆注入、recall/remember 在真实对话中可用。
- 杂物可清理：`_verify_fix.py`（DSML 诊断脚本，已无用）、`test_progress.py`（悬空测试）、`tui.py.bak`/`tui.py.backup`/`backup_conflict_files/`/`.DS_Store`/`compile.jpeg`/`seitsuna.png` 等图片。

---

## 八、历史已完成项（速览，细节见 git log）

- **单文件拆分重构**（2026-08-04）：`loop_agent_v2.py` 1515 行 → 106 行薄壳 + `loop_agent_core/` 13 模块。修 worker 隔离 bug（全局 messages → conv["messages"]）。
- **400 错误修复**（2026-08-04）：DeepSeek 只认自己签发的 tool_call id → 非本地后端忽略 XML 调用；最终阶段补 `tools=tools`；新增 `_safe_create` + `_rollback_tool_round`。
- **DSML 污染修复**（2026-08-04）：全角竖线 `|DSML|` 变体污染工具标签 → `_DSML_RE` 匹配 `[|｜]+DSML[|｜]+`，四入口剥离。
- **主循环流式化**（2026-08-04）：思考/正文实时 emit，无工具调用时直接返回不再一次性输出。
- **Qwen3 工具调用格式**：复数标签归一化。
- **多 agent 委派**（commit 75ba3f5）：`delegate_task` 工具 + orchestrator-worker 串行架构。

**重要警告（信息污染）**：不要直接读取 `conversations/` 里含工具调用 XML 的记录并复述其内容（曾导致提示注入污染）。排查一律用脚本输出布尔/长度/码点，字符串里的 `<`、全角竖线用 `chr()` 动态拼接。
