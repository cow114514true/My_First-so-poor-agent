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
- **验证**：`test_code_index.py::test_edit_backup_and_diff`——前置备份、失败恢复、diff 输出、不留 `.bak` 残留。

### Bug 22. diff 自检对未跟踪文件完全失效（Bug 21 的残留盲区）
- **现象**：`edit_file` / `write_file` 改**未跟踪**文件时，工具结果里没有任何 diff；而 `write_file` 的
  主用途恰恰是新建文件。叠加"成功即删 `.bak`"，这类编辑等于零验证、零回退。
- **怎么发现**：review 时实测 `git diff -- <untracked>` 返回空串（长度 0）；且 `git diff` 是
  "工作区 vs 索引"，同一文件里已有的历史未提交改动会混进同一个 hunk，把本次改动挤出
  `diff[:500]` 前缀截断窗口——模型看到无关改动会误判"改错了"。
- **修复**：`_git_diff_after_edit`（子进程 + git）→ 进程内 `difflib.unified_diff` 对比写前/写后字符串。
  未跟踪/非 git 仓库同样有 diff；只含本次改动；超预算时截断点落在改动之后并显式标注省略量；
  新建文件与内容无变化各有明确的文字说明（不给伪 diff）。
- **验证**：`test_code_index.py::test_edit_backup_and_diff`——未跟踪文件上的编辑必须含
  `-old`/`+new`、diff 头两行被剥掉、覆盖写入不混入历史改动、"无改动"有明确措辞、
  超大改动的省略量被标注。反向验证过：把 diff 打回空串该测试即失败（断言确为 load-bearing）。

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
### Bug 23. 压缩后的 span 起点落在 assistant 上 → 当前问题被错位到 assistant 之后
- **现象**：L1 摘要的区间起点若落在一个 `assistant` 消息上（该轮的第一个 `user` 被留在上下文里），
  摘要替换后保留部分就以"孤儿 assistant"开头；再叠加末尾未吸附到组边界，最终历史变成
  "… → assistant → 当前问题"，语义断裂且模型失去该轮的意图。
- **怎么发现**：`test_apply_and_expand_roundtrip` 断言 `msgs[new_q]` 必须是 user 且含当前问题，直接挂掉；
  手工打印 `_find_groups` 才看清起点是 `assistant`（用户轮的第一个 user 排在被选中的组之外）。
- **修复**：`compaction_span()` 两条硬规矩 —— 起点必须落在某轮的第一个 `user`（逐个 pop 候选组，
  连续多条 user 时也要能前移到最后一个候选）、末尾必须吸附到 `_find_groups()` 的组边界。
- **回归**：`test_span_never_cuts_tool_chain`、`test_apply_and_expand_roundtrip`。

### Bug 24. 滚动摘要的前缀无界 → "单次摘要输入上限"形同虚设
- **现象**：`run_rolling_summary` 把前序所有块的摘要拼接成前缀喂给下一块，前缀随块数线性增长
  （实测 385 → 770 → 1155 token…）。看起来每块输入受限，实际总输入无界，`COMPACT_BLOCK_TOKENS`
  这道防线被绕过，正是 arXiv 2605.23296 指出的"摘要保留率随输入断崖下跌"的成因。
- **怎么发现**：测试断言"每次调用的输入 ≤ 块上限"时，第 3 块实测 1579 token（块本身只有 ~100）。
- **修复**：`bounded_prefix()` 把累计摘要按 token 预算二分截断（**保留尾部**，越近的工作越重要），
  并在前缀首部留省略标记。
- **回归**：`test_rolling_summary_calls_and_prefix_stability`（断言前缀 token 有界且带省略标记）。

### Bug 25. 每次压缩产生两条重复档案（`record_archive` 被调两次）
- **现象**：一次压缩后 `_archive` 出现两条内容相同的条目，`/expand` 只还原一条、另一条残留，
  档案体积与 `COMPACT_ARCHIVE_MAX_CHARS` 预算都被浪费一倍。
- **怎么发现**：全链路冒烟脚本打印 `archive entries: 2`（预期 1）。
- **修复**：`apply_compaction` 内部已记录档案，`_prep_context` 里那次多余的 `record_archive` 删除。
- **回归**：`test_prep_context_chains_l0_then_l1` 断言 `len(conv["_archive"]) == 1`。

### Bug 26. `[Context trimmed]` 计数被下次裁剪抹掉
- **现象**：反复裁剪时提示被**重写**成"已删除 N 条"（最后一次的数量），模型只看到最新数字，
  低估自己累计丢了多少历史；而该提示正是模型判断"是否需要重新获取信息"的唯一依据。
- **怎么发现**：写测试时检查 `render_prune_notice` 的累加语义，发现旧实现直接 `format` 覆盖。
- **修复**：`render_prune_notice()` 解析已有前缀计数并**累加**，同时保持问题原文在提示之后。
- **回归**：`test_trim_notice_accumulates`、`test_disabled_matches_legacy_prune`。

### Bug 27. 摘要输出预算不足 → 整次压缩恒失败（"压缩失败（summarizer_failed）"刷屏之一）

- **现象**：真实长会话里每次压缩都在几秒后失败并回退硬裁剪，同样的输入重试永远失败；
  面板上连续出现"正在压缩上下文（4~5 块）… / 压缩失败（summarizer_failed）"。
- **怎么发现**：`summarize_block` 的 `except` 只 `print` 原始异常，而 TUI 下 stdout 被界面覆盖 —— 等于把证据丢了。
  于是用真实会话原文复现一次摘要调用，直接打印响应元数据，一次就看到了根因：
  `finish_reason=length`、`completion_tokens=1500`（正好等于 `COMPACT_MAX_TOKENS`）、
  `reasoning_tokens=680`、缺 3 个小标题。
- **根因**：两个叠加的预算问题。(a) `COMPACT_MAX_TOKENS=1500` 小于一个 ~7.6k token 真实块所需的
  摘要输出量（实测需要 1761，含思考）。(b) `deepseek-v4-flash` **默认就开思考**，把近一半输出预算
  花在 `reasoning_content` 上。截断后的摘要缺小标题 → `validate_summary` 不过 → 按"失败即整体放弃"
  设计作废整次压缩；下一轮再拿同样大小的块重试，于是永远失败。
- **修复**：默认值按论文的"摘要输出几乎不随输入变化"给足（DS `3000` / 本地 `1500`）；
  被截断时**升级一次**预算（`COMPACT_MAX_TOKENS_HARD`）重试；给摘要器下发
  `extra_body={"thinking":{"type":"disabled"}}`（仅云端后端，后端不认则自动摘掉重试）；
  小标题已齐、仅末尾被切时直接采用，不白扔调用。
- **回归**：`test_summarize_block_escalates_on_truncation`、`test_summarize_block_drops_unsupported_param`。
  真实 API 复核：同一块由"失败"变为成功（5 个小标题齐全，1130 token 输出）。

### Bug 28. 失败重试风暴 + 频率预算被"每轮归零"废掉（刷屏主因）

- **现象**：一轮长任务里每隔一个工具调用就打印一条"正在压缩上下文（N 块）/ 压缩失败"，
  一段时间内反复压缩；`COMPACT_MIN_ROUNDS`（最小间隔）看起来完全不起作用。
- **怎么发现**：读 `runner.chat_impl` 才发现 `conv` 是**每个用户轮新建**的包装 dict
  （薄壳 `{"messages": messages, "size": _msg_size, ...}`），而 `_compact_count` /
  `_compact_last_turn` / `_user_turn` 全写在 `conv` 上 → 每轮归零，
  `interval` 永远不成立、`MAX_PER_SESSION` 实际退化成"每轮上限"。
  又因为失败路径**不写** `_compact_last_turn`，同一轮的每次 API 调用都会重试一次
  （一轮 10 次工具调用 = 10 次全量摘要重试，每次 4~5 个 8k 块）。
- **修复**：压缩记账改挂跨轮存活的 `_session_state["_compact"]`（`compaction.cstate()` 统一取用；
  没有该键时退回 `conv` 自身，测试与生产路径都不受影响）；失败时记 `_compact_fails` 并设
  `_compact_retry_turn`（退避 `min_rounds × 2^(连续失败-1)`，封顶 `COMPACT_MAX_BACKOFF`），
  连续 `COMPACT_MAX_FAILS` 次停用自动 L1（`/compact` 手动解锁，且手动路径清退避）；
  同类提示每轮只报一次（`_notice_once`）；Esc 中断不计入失败（否则按三次 Esc 就停用 L1）。
- **回归**：`test_cross_turn_attempt_cadence`（E2E：5 个用户轮只允许 2 次尝试）、
  `test_compaction_failure_backoff_stops_retry_storm`、`test_compaction_failure_hard_stop_then_manual_reset`、
  `test_compact_state_survives_new_conv_wrapper`、`test_interrupted_compaction_is_not_a_failure`。

### Bug 29. 压缩失败只有"summarizer_failed"一句话，没有可诊断信息

- **现象**：`compact_failed` 事件只有 `{"reason": "summarizer_failed"}`；接口报错、输出截断、
  模型没按小标题写、被用户中断，四种完全不同的故障在面板和日志里长得一模一样。
- **怎么发现**：定位 Bug 27 时几乎无从下手 —— 只能靠另写脚本复现，无法从产品自身拿到任何线索。
- **修复**：`run_rolling_summary(..., on_error=)` 回传具体原因 + 块号；`summary_reject_reason()`
  返回 `too_short` / `too_long` / `missing_headings:缺哪些` 等具体原因（`validate_summary` 保留为布尔包装）；
  `summarize_block` 用 `{"error": ...}` 显式报错；`failure_hint()` 把原因翻成可执行的一句话
  （"调大 COMPACT_MAX_TOKENS / 调小 COMPACT_BLOCK_TOKENS"…）；失败明细追加写
  `tmp/compaction_error.log`（TUI 下 `print` 等于丢弃，这是本次定位困难的直接原因）。
- **回归**：`test_summary_reject_reason_is_specific`、`test_rolling_summary_reports_error_reason`、
  `test_failure_hint_and_log`。

### Bug 30. `/compact` 把 `cap=0` 传进 L2 → 低于阈值时把历史删光（且连压缩都没试）

- **现象**：`/compact` 在**低于触发阈值**的会话上直接调用，历史被删到只剩 `system` + 当前问题
  （复现：8 条 → 2 条），而且**一次压缩都没尝试**。
- **怎么发现**：为 Bug 28 写回归测试时顺手核对 `/compact` 的落点。它是自己在失败提示里
  推荐的"自救路径"，所以在推荐之前必须先确认它不毁数据。
- **根因**：`compact_session()` 为了让 L1"强制触发"把 `cap=0` 传给了 `_prep_context`，
  但 `cap` 是 **L2 的裁剪目标**（L1 的阈值走 `compaction_cap()`，与它无关）。于是
  "强制"完全没生效，破坏却生效了：`_prune_messages(0, ...)` 的循环条件是
  `while size > 0`，永远不成立 → 把**所有**可删组删光。
- **修复**：`cap` 一律用正常的 `_prune_cap()`；"强制"改为 `_prep_context(..., force=True)` →
  `should_compact(force=True)` 只跳过**排期类**闸门（阈值/间隔/退避/硬停），
  会话次数上限 `COMPACT_MAX_PER_SESSION` 仍然生效。
- **顺带删掉的死代码**：原来的"同一轮只压一次" token 闸门 key 只能取 `id(conv)` 或
  `(轮次, size)`，前者会被 CPython 地址复用骗到，后者会把"上下文未变时显式再来一次 /compact"
  静默变成无操作（测试实测命中）。它要防的场景已被 interval / backoff / 中断三条闸门覆盖。
- **回归**：`test_compact_command_never_wipes_history`。

---

## 附：杂项清理清单（非 bug，历史遗留）

- `test_progress.py` 悬空（引用不存在的 `_agent_status`），跑会 SKIP，可删。
- `_verify_fix.py`（DSML 诊断脚本，Bug 4 已修后无用）。
- `tui.py.bak` / `tui.py.backup` / `backup_conflict_files/` / `.DS_Store` / 测试图片（`compile.jpeg`、`seitsuna.png` 等）。
