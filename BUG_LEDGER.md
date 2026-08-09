# Bug 账本 — 项目开发以来遇到的 bug 全记录

> 整理日期：2026-08-09。素材来源：git 提交历史、`HANDOFF_NEXT_SESSION.md`、`BACKEND_SWITCH_GUIDE.md`、`NETWORK_UPGRADE_GUIDE.md`、`MEMORY_GUIDE.md`、测试文件、代码注释、`debug_filtered.txt` / `api_error.log` / `error.log`。
>
> 每条格式：**现象 → 怎么发现 → 根因 → 修复 → 验证**。按时间线排序，同一主题的 bug 归组。
> 附最后：**方法论**——这个项目实际用过的"找出 bug"的手段，以及它们怎么沉淀成防回归机制。

---

## 一、早期：网页桥接与输出质量（2026-07-29）

### Bug 1. 网页端内容捕获卡死 / 捕获内容不完整
- **现象**：`use_ds_from_web` 抓 DeepSeek 网页版回复时，要么卡住不动，要么抓到不完整内容。
- **怎么发现**：真实对话中反复出现，模型拿到的信息残缺。
- **根因**：网页端抓取流程对失败没有自愈能力——超时或截断后直接继续，不带任何校验。
- **修复**（`b9ddcb0`）：① 系统提示词强制"联网得到的信息必须用工具验证"；② 增加自我质疑纠错机制——工具结果无效时注入 `[Self-check]` 消息让模型重试（部分实现 ReAct 架构）。
- **验证**：后续所有 `validate_tool_result` + `[Self-check]` 链路都由这里生长出来，被 `test_code_index.py` 固化为回归用例。

### Bug 2. 响应里混入 DSML / 垃圾标记，污染上下文
- **现象**：模型输出里出现 `|DSML|...|DSML|` 之类垃圾标签，被原样存进历史、喂回 API，上下文越来越脏。
- **怎么发现**：真实对话观察，输出质量退化。
- **根因**：DeepSeek 网页版内容带服务端标记，未清洗。
- **修复**（`f0f52a6`）：加 response quality guard `_is_garbage_content()`，把含 DSML/垃圾的输出挡在落库前。
- **验证**：`test_code_index.py::test_dsml_*` 系列（污染标记 → 判为垃圾、剥离后只留正文）。

---

## 二、TUI 与对话保存（2026-07-30）

### Bug 3. SDK 对象 JSON 序列化崩溃
- **现象**：退出保存对话时崩溃——`conversations/` 写不进去。
- **怎么发现**：点退出直接 traceback。
- **根因**：消息历史里混着 OpenAI SDK 对象（`tool_calls` 等），`json.dump` 不能序列化。
- **修复**（`1f095b7`）：工具调用消息在落库前转成纯 dict；顺带重构 TUI 退出逻辑、增强 `/load`。
- **验证**：`/quit` 保存后 `conversations/` 可读、`/load` 能还原。v2.3 changelog 记为"修复 SDK 对象 JSON 序列化崩溃"。

### Bug 4. DSML 全角竖线变体污染工具标签
- **现象**：`_DSML_RE` 只匹配半角 `|DSML|`，实际污染带全角竖线 `｜DSML｜`，工具调用解析错乱。
- **怎么发现**：真实对话里工具链路被打断，排查 `_verify_fix.py`（诊断脚本只输出布尔/长度/码点）确认变体存在。
- **根因**：正则没覆盖全角变体；工具调用路径没接清洗。
- **修复**（`7a2d8c2`）：`_DSML_RE` 改为匹配 `[|｜]+DSML[|｜]+`，四个入口（`_parse_xml_tool_calls`/`_strip_toolcall_xml`/`_stream_strip_xml`/`_is_garbage_content`）统一接 `_strip_dsml`。
- **验证**：`test_code_index.py::test_dsml_*` 用 `chr(65372)`（全角竖线）构造污染串，断言能解析、能剥离、流式跨 chunk 也能剥。

### Bug 5. 未知工具错误不列出可用工具
- **现象**：模型调了一个不存在的工具，错误信息没有提示可用工具列表，模型瞎猜继续循环。
- **修复**（`7a2d8c2`）：未知工具错误里列出 `TOOL_CALL_MAP` 全部可用名，给模型纠错路径。

---

## 三、Windows 环境（2026-07-31）

### Bug 6. setup.bat 中文乱码
- **现象**：双击/运行 `setup.bat`，中文提示全是乱码。
- **怎么发现**：Windows cmd 直接运行观察。
- **根因**：脚本里 `chcp 65001` 把控制台切到 UTF-8，但 .bat 文件本身是 GBK 写的中文——切换后按 UTF-8 解析，全部错位。
- **修复**（`c537495`，commit 即 "Fixing encode problem"）：删掉 `chcp 65001 >nul`，用默认代码页。
- **验证**：cmd 运行 setup.bat 中文正常。

---

## 四、本地后端接入（2026-08-01）

### Bug 7. 流式结尾空 `choices` 块 → IndexError
- **现象**：本地 llama.cpp 模式最终回答最后一步必崩。
- **怎么发现**：`BACKEND_SWITCH_GUIDE.md` §2 记录"我连本地服务验证过"——实测 2053 块流式里最后 1 块 `choices=[]` 但 `usage` 有值（OpenAI 标准的 usage 尾块），`chunk.choices[0].delta` 直接 IndexError。
- **根因**：没判 `choices` 为空。
- **修复**：流式循环加 `if not chunk.choices: continue`。文档原话："漏判空 = 本地模式每次最终回答最后一步必崩"；顺带是 DeepSeek 潜在崩溃的预防性修复。
- **验证**：本地模式完整跑一轮最终回答。

---

## 五、本地模型 XML 工具调用 + 400（2026-08-03 ~ 08-04）

### Bug 8. 本地 Qwen 的 XML 工具调用被误判为垃圾，对话中断
- **现象**：本地 Qwen3.5 把 `<tool_call>` 写在 `reasoning` 里，`content` 为空，被 `_is_garbage_content` 当垃圾整段丢弃 → 该轮无正文、无工具调用，agent 卡死或乱答。
- **怎么发现**：本地后端真实对话跑起来后观察（HANDOFF："本地模型乱码导致解析崩溃"）。
- **根因**：本地模型用 XML 格式表达工具调用（DeepSeek 用 OpenAI JSON `tool_calls`），解析层不认。
- **修复**（`035aea0`）：`_parse_xml_tool_calls` 把 XML 解析成正常工具调用继续执行；Qwen3 复数标签格式（`invoke name=.../parameter name=...`）归一化成旧格式再解析。
- **验证**：`test_code_index.py::test_xml_tool_calls` 系列（旧格式/复数标签/含文本混合/带 DSML 污染）。

### Bug 9. DeepSeek 400：不认合成 tool_call id
- **现象**：DeepSeek API 回 400。
- **怎么发现**：接入 XML 解析后，合成 id（`xml0`）被回送给 DeepSeek 触发 400（HANDOFF："DeepSeek 只认自己签发的 tool_call id，合成 id 回送会 400（曾踩过）"）。
- **根因**：XML 解析只该用于本地后端；DeepSeek 必须用它自己签发并回传的 id。
- **修复**（75ba3f5 分包重构时）：`_is_local_backend()` 才解析 XML 调用；`_safe_create` 包装 `create()`，400 时先 `_rollback_tool_round` 回滚最后一轮工具调用（assistant+TC + tool 消息），让会话回到可用状态，再上抛；最终阶段补 `tools=tools`。
- **验证**：`test_code_index.py::test_rollback_tool_round`——构造含 `xml0` 的历史，断言回滚后只剩 system+user、size 减小；无可回滚时返回 False 且不动。

### Bug 10. 400 错误正文无处可查
- **现象**：400 来了只有 traceback，不知道 API 拒收的完整消息结构。
- **修复**（`a69fff0`）：`_safe_create` 在 400 时把错误正文 + 消息结构落盘 `api_error.log`（`runner.py` 里 `_dump_400` / `_diagnose_400`），事后能看。
- **验证**：`api_error.log` 生成、内容含错误 JSON 与消息形状。

---

## 六、多 agent 委派（2026-08-04）

### Bug 11. worker 把主线程历史发给模型
- **现象**：`delegate_task` 子 agent 回答用的是主会话的完整历史，上下文隔离失效。
- **怎么发现**：`test_multi_agent.py::test_worker_uses_own_conv` 断言暴露（该用例注释即"worker 隔离 bug 回归"）。
- **根因**：`chat_impl` 内部 API 调用仍写死用全局 `messages`，worker 传了自己的 conv 也被忽略。
- **修复**：`chat_impl` 两处 API 调用一律 `messages=conv["messages"]`。
- **验证**：`test_multi_agent.py` 用 monkeypatch 拦截 `create()`，断言 worker 调用收到的正是它自己的 conv。

### Bug 12. 递归守卫用全局布尔 → 并发 worker 互相污染
- **现象**：多个 worker 并发时，A 的 `_in_worker` 标志串到 B。
- **修复**（`a69fff0`）：改为 `threading.local()` 的 `_worker_state.in_worker`，每线程独立。
- **验证**：`test_multi_agent.py::test_worker_flag_is_thread_local`（两线程各自设置互不干扰）。

---

## 七、网络升级：浏览器与搜索（2026-08-05）

### Bug 13. Playwright 线程错乱崩溃 `cannot switch to a different thread`
- **现象**：`browse_web` 在多个真实会话反复崩（`NETWORK_UPGRADE_GUIDE.md` 点名的 `chat_20260804_011403.json`、`chat_20260805_153143.json`），模型被逼着改走 `use_ds_from_web`。
- **怎么发现**：从 `conversations/` 的会话 JSON 里 grep 错误串定位的——这是"会话留痕当调试证据"的典型。
- **根因**：Playwright sync API 要求所有调用在**同一线程**；`_get_playwright()` 的单例 `_shared_pw` 可能在线程 A 创建、线程 B 使用。`use_ds_from_web` 与 `browse_web` 共享这个单例，所以两个工具都中招。
- **修复**（`f6e9799`）：`ds_web.py` 新增 `BrowserExecutor`——专用浏览器线程 + 任务队列，所有 Playwright 操作（含 `use_ds_from_web`）一律 `_executor.call(fn)`。
- **验证**：`python -c "from loop_agent_core.browse import browser_act_mock; print(browser_act_mock('open', url='https://example.com')[:500])"` 返回页面状态，**不出现 thread-switch 错误**。

### Bug 14. goto 死等 `networkidle` 超时
- **现象**：GitHub 之类大页 goto 60s 超时（`chat_20260803_230605.json` 里有 `Page.goto: Timeout 60000ms exceeded`）。
- **怎么发现**：同样从会话 JSON 里 grep 到超时痕迹。
- **根因**：`networkidle` 对长轮询/长连接页面几乎永不满足。
- **修复**：`browser_act` 主等待改 `domcontentloaded` + 短超时 30s，`networkidle` 只作 5s 兜底 try/except。
- **验证**：`browser_act` open 大页不再超时；文档断言"goto 再超时 → 主等待还是 networkidle，没改"。

### Bug 15. `browse_web` 一次性正则猜动作
- **现象**：给一串自然语言指令，正则抠出 click/search/type 各执行一遍——看不到页面状态，猜错就拉倒。
- **怎么发现**：真实对话中浏览任务频繁失败（弱设计，非崩溃，但体验差）。
- **修复**：重写为 `browser_act` 单步协议——每次调用一个动作，返回 URL+Title+元素清单（编号），模型看清单选下一步。删除 `_execute_instructions`（70–166 行正则）。
- **验证**：`test_interrupt.py::test_tools_list_has_hover_select`（动作表含 hover/select_option）+ 文档验证清单。

### Bug 16. SPA 会话丢失
- **现象**：profile 恢复后，SPA 站点（需要 sessionStorage 的）登录态丢失。
- **修复**（v2.9 changelog）：profile 恢复 URL + `sessionStorage`（`_load_session`/`_save_profile` 序列化存储）。

### Bug 17. 记忆注入每轮重复堆积
- **现象**：`build_memory_injection()` 结果作为 user 消息前置，每轮对话都注入一次，越堆越多。
- **怎么发现**：review 后手动跑两轮对话验证发现（HANDOFF Bug B）。
- **根因**：注入无去重守卫。
- **修复**：注入前检查已有消息 `startswith("[长期记忆")` 则不再注入。
- **验证**：两轮对话注入仅 1 次。

### Bug 18. `build_memory_injection` 空判断恒为 False
- **现象**：`system_records == ""` 之类判断——记录是 list，`== ""` 恒 False，逻辑永不成立。
- **怎么发现**：review 逐行过代码（HANDOFF Bug A）。
- **修复**：改 `not system_records and not project_records`。
- **验证**：空 memories/ 目录时注入返回 `None`（不插假消息）。

---

## 八、稳定性专项（2026-08-09）

### Bug 19. /load 加载覆盖当前会话、崩溃丢会话
- **现象**：`/load` 换对话时当前未保存内容直接没了；进程崩溃时整个会话丢失。
- **修复**（`a69fff0`）：`/load` 前自动保存当前会话；新增 `/save` 命令；每轮工具调用 `autosave` 轮转 + 启动恢复对话框 + `atexit` 应急保存；`PanelLog` markup 加固 + `_history` 有界化。
- **验证**：`test_interrupt.py` / `test_code_index.py` 相关回归 + 手动 /load 流程。

### Bug 20. 静默吞错：裸 `except: pass`
- **现象**：`browse.py` 的 restore goto / save_profile、`file_tools` 的备份清理、`shell_tools` 的 stdout 读取，都是裸 `except: pass`——真实错误被吞，出问题无从查。
- **怎么发现**：排查稳定性问题时扫代码发现（提交 0f83187 就是为此）。
- **修复**：新增 `debuglog.log_error(context, exc)` 落 `error.log`；真正该忽略的地方（networkidle 超时、.bak 清理失败）加注释说明"这是故意忽略，为什么"。
- **验证**：`run_tests.py` 一键跑 4 套件，退出码接 CI。

### Bug 21. write/edit_file 覆盖无备份、失败即毁文件
- **现象**：模型 `write_file`/`edit_file` 改坏一个文件，原内容没了，无法回退。
- **修复**（`a69fff0`）：写前备份 `.bak`、写失败自动恢复、返回附 git diff 供模型自检。
- **验证**：`test_code_index.py::test_edit_git_guard`——前置备份、失败恢复、diff 输出、不留 `.bak` 残留。

---

## 方法论：这个项目实际怎么"找出 bug"的

按频率排序，这些手段共同构成了发现 bug 的闭环：

| # | 手段 | 例子 | 沉淀成什么 |
|---|------|------|-----------|
| 1 | **会话 JSON 留痕当调试证据** | 从 `conversations/*.json` grep 到 `Page.goto Timeout`、`cannot switch to a different thread` | 是 Bug 13/14 的定位依据 |
| 2 | **连真实服务实测** | 连本地 llama-server，数了 2053 块流式找出空 choices 尾块 | 修复后写进 `BACKEND_SWITCH_GUIDE` 验证清单 |
| 3 | **回归测试断言** | worker 隔离、400 rollback、DSML 变体、edit 备份 | `test_code_index.py` / `test_multi_agent.py` / `test_interrupt.py` / `test_vision_tool.py` + `run_tests.py` |
| 4 | **代码 review** | 记忆注入 Bug A（空判断恒 False）、Bug B（每轮堆积） | review 记录进 `HANDOFF_NEXT_SESSION.md` |
| 5 | **运行 traceback** | SDK 序列化崩溃、流式 IndexError | 直接修 |
| 6 | **消除静默吞错** | 裸 `except: pass` → `log_error` | `debuglog.py` + `error.log` |
| 7 | **400 诊断落盘** | 400 时把错误正文+消息结构写盘 | `api_error.log` + `_dump_filtered` |

三条贯穿始终的原则：
1. **错误要能看见**——吞错的 finally 修成落日志（Bug 20），400 落盘（Bug 10），被过滤内容落盘 `debug_filtered.txt`（`runner._dump_filtered`）。看不见的 bug 等于不存在。
2. **本地后端是压力测试器**——OpenAI 协议的每个"标准假设"（流式空 choices、XML 工具调用、合成 id）几乎都是本地 Qwen 先踩出来的，DeepSeek 反而没炸过。
3. **修复必须留回归**——四个测试套件 + `run_tests.py` 一键跑，每个 Bug 9/11/12/18/21 都有对应断言，防止下个功能把它们又放出来。

---

## 附：杂项清理清单（非 bug，历史遗留）

- `test_progress.py` 悬空（引用不存在的 `_agent_status`），跑会 SKIP，可删。
- `_verify_fix.py`（DSML 诊断脚本，Bug 4 已修后无用）。
- `tui.py.bak` / `tui.py.backup` / `backup_conflict_files/` / `.DS_Store` / 测试图片（`compile.jpeg`、`seitsuna.png` 等）。
