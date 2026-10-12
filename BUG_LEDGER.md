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

### Bug 31. 折叠档案 sidecar 被当成会话加载 → 历史被换成档案条目（422 + "历史保存失败"）

- **现象**：用户跑一轮任务后，面板弹出
  `ERROR: Error code: 422 - ... Failed to deserialize the JSON body into the target type:
  messages[0]: missing field 'role' at line 1 column 17088`；同时"今天几条历史都保存失败"——
  `conversations/` 里出现 3 个**同样大小**的 `chat_*.json`（30624 字节），打开是
  **2 条没有 `role` 的档案条目**（`ts/messages/inserted/shape/added_tokens/removed_tokens`）。
- **怎么发现**：422 说 `messages[0]` 缺 `role`，而 messages[0] 本该是 system —— 说明内存里的
  `messages` 本身坏了。于是按结构（只打印 keys/长度，不复述内容）检查那几个文件：它们是
  **档案数组**，不是会话。回头看 `/load` 的列目录口径
  `f.startswith("chat_") and f.endswith(".json")` —— sidecar 名是
  `<会话>.json.archive.json`，**同样满足这个条件**，而它内容是 list，于是被归为"有效会话"。
- **根因**：v2.13 引入 sidecar（`conversations/<会话名>.archive.json`）时，只给**保存清理**逻辑
  加了 `.archive.json` 例外（`tui.py` 的清理循环），**没给出 `/load` 的列目录**。当时
  `sorted(reverse=True)` 下 `chat_20261011_202701.json.archive.json` 排在
  `chat_20261011_202701.json` 之前（前缀相同、后缀更长者更大），所以 `/load 1`
  正好加载了 sidecar → `_load_file` 直接 `_agent_messages.clear(); extend(loaded)`，
  没有任何校验 → 会话被替换成档案条目；此后每次请求都 422，每次保存都写出坏历史，
  用户看到的就是"历史保存失败"。
- **修复**（三层防线，缺一不可）：
  1. `tui.is_conversation_file()` 明确排除 `.archive.json`（列表、加载、autosave 恢复三处共用）；
  2. `context.conversation_problems()` + `tui.read_conversation()`：**先校验再动历史**，
     加载失败时当前会话原封不动（校验点：list 非空、每项是对象且有 `role`）；
  3. `runner.chat_impl` 发请求前的护栏：结构坏了直接 emit `error` 事件并拒绝发送，
     把"API 天书"换成"请 /load 有效会话或 /clear 重置"。
- **回归**：`test_conversation_problems_detects_bad_shapes`、
  `test_sidecar_is_never_listed_or_loaded_as_conversation`（含 `inspect.getsource` 接线断言）、
  `test_chat_refuses_corrupted_history`。
- **附带**：`chat_*.json` 与 sidecar 同名的坑在文件系统层面无法避免，故凡"按名字筛会话"的地方
  都必须走 `is_conversation_file`——这条要写进交接文档，别再用裸的 `endswith(".json")`。

---

### Bug 32. 回合结束"零输出"：终局消息只 return 不发事件 → agent 静默停止工作

- **现象**（用户原话："agent 出现了两次静默停止工作"）：模型干到一半，思考面板的字流停住，
  聊天面板**什么都不出现**，状态回 `idle`。用户只能重开会话，任务就此断在半路。
- **证据**（2026-10-11 20:52:34 那次，`conversations/chat_20261011_205234.json`）：
  - 第 47 条 user 消息是同一条 `[Self-check] 你刚才只输出了思考过程…` **重复 3 次**，
    第 48 条是 `_STUCK_MSG` → 说明连续 4 轮 API 调用（3 次 nudge + 1 次中止）**全部无输出**；
  - `debug_filtered.txt` 最后一条（20:52:09）`thinking=… response=`（空），内容正是它在追查
    "第 11 节那 3 个失败是既存还是我引入的" —— 它卡住的位置就是它当时的任务；
  - 压缩日志（`tmp/compaction_error.log`）同期为空，排除压缩。
- **根因**：`runner.chat_impl` 的 4 类终局消息只 `return`，不发事件 ——
  `_STUCK_MSG`（思考循环）、`[Response filtered…]`（乱码）、`[Interrupted by user]`（中断）、
  兜底消息；而 `tui._run_agent` 把 `chat()` 的**返回值直接丢掉**
  （`tui.py` 的 `_h_response_done` 注释自证"runner 当前不发此事件"）→ 面板零输出。
  注入的 nudge（`_EMPTY_NUDGE` / `_WRAPUP_NUDGE`）同样不可见，且 `_inject_user_nudge`
  在末条是 user 时会**合并**，于是 3 次催办叠进同一条历史消息，用户一条也看不到。
- **修复**：
  1. `runner` 新增 `_notice(level, message)` → `{"type": "notice", "level", "message"}`，
     覆盖全部终局分支 + 每次 nudge 注入（含"已用 12/16 轮工具调用，已要求模型收尾"）；
  2. 思考循环额外落一条 `turn_end:stuck` 到 `debug_filtered.txt`（notice 会随滚动消失，这条可 grep）；
  3. `tui` 注册 `_h_notice` 渲染到聊天面板（warn 用 ⚠，info 用 ·）；
  4. `_STUCK_MSG` 不再写死"换用 DeepSeek 后端"——实测云端后端同样会卡在只输出思考，
     而这条文案恰好出现在 DeepSeek 会话里（`12/16 轮` → `max_tool_rounds=16`），属于反向建议。
- **回归**：`test_runner_visibility.py`（新套件，假 client 驱动真循环；含"空流/中断/收尾催办
  都必须发 notice"与 `tui` 接线断言）。

### Bug 33. `edit_file` 找不到锚点时的"相近行"候补被空行占满（空行恒真）

- **现象**：模型改文件时 `edit_file` 报 `'…' not found in tui.py.`，随后给出的
  `Possible close matches` 全是空内容 —— 实测原话（`chat_20261011_202701.json` 第 43 条
  self-check 消息）：`line 5: / line 14: / line 28: / line 33: / line 34:` 后面**什么都没有**。
- **根因**：`file_tools.edit_file_mock` 判相近用的是
  `old_string[:5] in stripped or stripped[:5] in old_string`。空行的 `stripped` 是 `""`，
  而 `""` 恒为任意字符串的子串 → 每个空行都成了候选，且按行号排在前 5 就把真候选挤掉了。
  模型拿不到任何纠错线索，只能盲目重试；实测那次它最后**放弃 `edit_file`，改写一个
  Python 脚本批量改盘**（`_patch_think.py`）——绕过了确认与备份，风险高得多。
- **修复**：抽出 `_close_match_hint()`：空行不再参与（唯一的过滤条件就是"不能是空行"）；
  前缀命中优先，前缀对不上时用 `difflib` 相似度兜底（只扫长度相近的行，避免大文件退化）。
- **回归**：`test_runner_visibility.py::test_edit_hint_never_lists_blank_lines`
  （旧实现下该用例必红：候补里出现 `line 2: ` 这种空内容）。

### Bug 34. 用 findstr 过滤命令输出：结论被滤掉、失败显示成 success → agent 反复重试直到卡死

- **现象**（用户："agent 又卡在 findstr 上了"）：agent 收尾折叠工作时，最后几步反复是
  `python test_tui_markdown.py 2>&1 | findstr /C:…`，每次只回两三行，然后进入思考循环、
  以 `_STUCK_MSG` 收场。
- **两个坑，都会单独要命**：
  1. **returncode 不是被测程序的**。cmd.exe 管道的退出码取自**最后一个**命令，于是
     `python test_x.py 2>&1 | findstr …` 里 `success` / `returncode` 反映的是 findstr。
     真实证据（`chat_20261011_205234.json` 第 39 条工具结果）：三条 `[FAIL]` 同时带着
     `"returncode": 0, "success": true` → 模型据此认为"没失败"。
  2. **汇总行被滤掉**。findstr 只显示匹配行，而汇总行是 `test_tui_markdown: 全部通过`
     或 `N 项失败: [...]`；模型的 `/C:` 模式里只有 `[FAIL]` / `项失败` / `passed`，
     于是**测试全绿时反而什么都看不到**（`chat_20261011_211606.json` 第 35/36 条只回了
     两三条 `[PASS]`）。它只能换个模式再过滤 → 还是看不全 → 烧完工具轮次仍拿不到结论。
- **另一条同源错误**：把 **PowerShell 的 `;`** 用在 cmd 命令里 →
  `FINDSTR: Cannot open ; / Cannot open echo / Cannot open ===RESTORE===`，
  整条 `git stash` 对照实验静默失效（第 41 条工具结果，`returncode: 1`），
  于是"这三条红是我引入的吗"始终没有答案 —— 它后来自己也在思考里承认
  "my earlier stash test failed to run the test (findstr parse error)"。
- **修复**：
  1. `shell_tools.exec_shell_win_mock`：命令里含 `|` 时，结果里附 `note`（通用管道提示；
     命中 findstr 再加一条"汇总行会被滤掉"的说明）；
  2. `prompts.py`：明确禁止用 findstr 过滤测试结论（要看结论就别过滤，或在 Python 侧过滤），
     并写明 `shell='cmd'` 是 cmd.exe、只能用 `&&`/`&` 串联，不得用 `;`；
  3. `test_shell_tools.py::test_pipe_commands_get_a_note`（管道必有提示、findstr 有专属提示、
     非管道不加噪音）。
- **注**：这一条不是"模型笨"——工具把唯一的判据（退出码）换成了别人的、又把结论行删掉，
  任何模型都只能靠猜。工具应当保证"结论可见"，这与 Bug 32 是同一类问题。

### Bug 35. 提示词把模型锁死在 cmd，而 pwsh 的报错是 CLIXML 垃圾 → 它再也不用 pwsh

- **现象**（用户原话）："模型有点被提示词卡死了偏向于使用较古老的 cmd，即使后面鼓励使用 pwsh 也没有使用"。
- **四重锁**（缺一不足以解释"鼓励也没用"）：
  1. **schema 直接教它用 cmd**：`shell` 的 enum 是 `["cmd","powershell"]`，描述写着
     "Use 'powershell' for PowerShell-native tasks (registry, services, Get-* cmdlets);
     leave **'cmd' (default)** otherwise" —— 除注册表/服务外一律 cmd；
  2. **工具描述与函数默认值都是 cmd**：`description: "shell='cmd' (default)…"`、
     `exec_shell_win_mock(shell="cmd")`（core 与薄壳两处），省略参数就落到 cmd；
  3. **pwsh 的错误是 CLIXML XML**：原实现用 `-EncodedCommand`，它会把错误流序列化成
     `#< CLIXML <Objs Version="1.1.0.1" …><S S="Error">_x001B_[31;1mParserError: …`。
     真实会话 `chat_20260812_175615.json` 第 11/17 条就是这坨东西；模型第 13 条的结论是
     **"PowerShell 不支持 `<< 'EOF'` 语法。我来换个方式："** 然后**显式**改成
     `{"shell":"cmd"}`，此后整场再没用过 pwsh（第 19 条它自己摸索出"写临时 .py 再跑"，
     但把这个功劳记在了 cmd 头上）。**它连错误信息都读不到，当然学不会 pwsh。**
  4. `<<EOF` 这类 **bash 语法在 cmd 与 pwsh 里都不存在**，而提示词从没说过；模型两轮都
     试 heredoc（第 10/16 条），失败后归因给了 shell 而不是语法本身。
- **修复**：
  - 默认彻底翻过来：schema enum→`["pwsh","cmd"]` + `default:"pwsh"` + 描述改成
    "Defaults to PowerShell 7 … pass shell='cmd' only when you specifically need cmd.exe
    syntax"；`shell_tools.exec_shell_win_mock` 与薄壳包装的默认参数都改成 `"pwsh"`；
  - **不用 `-EncodedCommand` 了**：改 `-File` 跑 tmp/ 下的临时 .ps1（UTF-8-BOM，5.1 也认中文）。
    报错变纯文本，语法错也可见（`ParserError: “(” 后应为表达式。(第 1 行)`），
    引号/`$`/`%`/反斜杠路径原样，退出码正常传递；用户命令放在 here-string 里再用
    scriptblock 执行，**语法预检**（`Parser::ParseInput`）放在执行前 —— 否则未闭合的括号
    会把结尾的退出码回填一起吞掉、报错里露出内部代码；写不了临时文件时退回 `-Command`。
  - **cmd 语法兼容层**（`effective_shell()`）：命令像 cmd 语法（`dir /b`、`%VAR%`、`2>nul`、
    `for /f`、`if exist`、`%~dp0`…）就用 cmd 跑，并在结果里回一句 pwsh 等价写法
    （`Get-ChildItem -Name`、`$env:VAR`、`2>$null`…）——**兼容的同时把正确写法教会它**；
    带 PowerShell 特征（`$`/`Get-`/`Select-`…）的一律不改道（防 `-Format "%Y%m%d"` 误判）。
    confirm 弹窗显示**实际**会用的解释器：`[pwsh]` / `[cmd·cmd语法]`。
  - 结果里加 `shell_used` 字段：模型能立刻看到"这次到底是谁跑的"。
  - 提示词新增：默认 pwsh 及常用写法、**禁止 bash 语法的 `<<EOF`**（要跑多行就
    write_file 落 .py/.ps1 再执行）、`shell='cmd'` 时才用 `&&`/`&` 且不得用 `;`。
  - 补退出码回填：pwsh 的 cmdlet 报错是**非终止**错误，脚本照样 exit 0
    （`Get-ChildItem /b` → rc=0 + 一条错误），现在 `$?` 会被回填成 rc=1；
    `cmd /c exit 2` → rc=2、用户自己 `exit 7` → rc=7 都保持原样。
- **回归**：`test_shell_tools.py` 6 → 9 项（默认是 pwsh、cmd 语法改道 + 提示、
  PowerShell 语法不被改道、错误不是 CLIXML、语法错可见、cmdlet 错误 rc≠0、临时脚本清理）。

### Bug 36. "前缀逐字稳定以命中 prefix cache"只是一个注释：按字符偏移切前缀，每次调用都必然 miss

- **现象**：`build_block_prompt` 的布局（固定 system + 前序累计摘要 + 当前块置末尾）是刻意
  照着 "prefix-aware target-at-end" 写的，注释也写着"前缀逐字稳定以命中 prefix cache"。
  但 `bounded_prefix()` 超预算时是**从头部按任意字符偏移**截断的（二分找最小切点），
  而前缀随块数单调增长、预算固定 → **截断点每块都往后挪一格** → 相邻两块的前缀
  **第一个 token 就不同**，除 `SUMMARIZER_SYSTEM_PROMPT` 外全部 miss。
  注释描述的性质从来没成立过，也没人发现——因为**根本没有埋点能看出来**。
- **怎么发现**：用户提出"摘要生成后不是全量替换上下文，而是追加在末尾或替换部分以保持前缀稳定"，
  逐条核对代码时发现：会话级 `apply_compaction` 的 `start` 一般在索引 1 附近（`compaction_span`
  只保留 `keep_rounds` 轮），真正逐字不变的只有 `messages[0]` 那条 system；而摘要链这处
  本该成立的前缀稳定，被 `bounded_prefix` 的字符偏移截断抵消。
  关键判断依据：**前缀缓存命中的是最长公共 token 前缀，你能保住的前缀正好就是你没省下的 token**
  —— 缓存保留与窗口腾挪是同一个量的两端，1:1 对换。所以"前缀稳定"的价值只在三处：
  ①摘要**请求**本身与实时会话前缀对齐（纯赚，窗口不受影响）；②在缓存**本来就已经冷却**时压缩
  （纯赚，不压也一样全 miss）；③压缩后的新前缀**在两次压缩之间**保持逐字不变。
  把它用在"压缩结果"上则是自相矛盾。
- **修复**：
  - `bounded_prefix()` → `bounded_prefix_parts(parts, budget)`：丢弃粒度改成**整块**
    （`parts` 本来就是每块摘要的列表）。没发生丢弃的相邻两块前缀逐字相接；丢弃腾出的余量
    还能让后续若干块继续相接（旧实现做不到：它每次都在挪截断点）。
    单个块摘要自身超预算时**整块保留**（它已被 `max_tokens_hard` 封顶，而切半块恰好破坏了
    本函数存在的理由）。
  - 返回 `prefix_stable` / `prefix_miss` / `prefix_dropped` 三个**证据字段**
    （恒有 `stable + miss == blocks - 1`，比值就是摘要链的缓存复用率），
    随 `compacted` 事件一路显示到 TUI（"前缀稳定 3/4 块"）——**没有可观测数字的"优化"等于没做**。
  - 成功也落盘：`log_compaction_success()` 往**同一个** `tmp/compaction_error.log` 追加一条
    `result:"ok"`（与失败记录靠该字段区分）。面板提示会随滚动消失，而
    `prefix_stable/(blocks-1)` 是这次改动的唯一验收指标，必须能事后复查。
- **回归**：`test_prefix_stability_invariant`（预算充足时逐块相接；超预算时整块保留、
  绝不出现半块；**丢弃之后的后续小块必须继续逐字相接**）；
  `test_rolling_summary_calls_and_prefix_stability` 补 `prefix_stable + prefix_miss == blocks - 1`
  与"预算充足 → `prefix_stable == blocks - 1`"。

### Bug 37. 没有任何前缀缓存埋点 → "为前缀稳定多留 token 值不值"永远无法证伪

- **现象**：整个上下文压缩层建立在"缓存友好"这个前提上，但代码只取了 `usage.prompt_tokens`
  （`runner.py` 两处），**从没读过缓存命中字段**。缓存存活多久（TTL）——由 provider 决定、
  且决定"留前缀"这张赌注的赔率——**在当前实现里根本无法测量**。
- **怎么发现**：讨论 Bug 36 时问"怎么证明这个优化有效"，发现答不上来：没有任何数据。
- **修复**：`runner._record_usage()` 每次 API 调用记一行到 `cache_usage.log`（`CACHE_LOG` 可覆盖），
  字段含 `prompt_tokens` / `cached_tokens` / `hit_ratio` / `gap_s`（距上次同类调用）/ `kind`；
  字段名按各家兼容探测（DeepSeek `prompt_cache_hit_tokens`、OpenAI 兼容
  `prompt_tokens_details.cached_tokens`、Anthropic `cache_read_input_tokens`）；
  摘要调用用 `kind="summary"` 分开记。**取不到就是 `None`，绝不假装 0**——
  "没有统计"与"缓存完全没命中"混起来会让结论反向。TUI 底栏显示命中率（provider 不报就不显示）。
- **两个实测细节**：
  1. **间隔必须按 kind 分开记**：摘要调用与主对话是两个不同的前缀，共用一根时间戳会把
     "用户停了 5 分钟"抹成几秒，把 TTL 量短，从而错误地得出"留前缀没意义"。
  2. **间隔必须挂跨轮状态**（`cstate`）：挂 `conv` 会每轮归零，`gap` 恒为 `None`，永远量不出 TTL。
- **验证**：`tmp/_verify_cache_telemetry.py` 在**真实包装链**（headroom `HeadroomClient`
  → `runner._cache_hit_tokens`）上跑通，`cached_tokens=750`/`hit_ratio=0.75`，且 chunk 对象
  是**同一个**（`_wrap_stream` 是 `yield from stream` 直通）。字段名与官方文档一致
  （`usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` / `prompt_tokens_details.cached_tokens`，
  且 `prompt_tokens == hit + miss`）；本机 openai SDK 2.48.0 的 `CompletionUsage`
  是 `extra="allow"`，`getattr` 能直接读到非标准字段。
- **回归**：`test_runner_visibility.py` 6 → 9 项（三种字段形状 + 真 0 命中 + 字段缺失、
  跨轮间隔与按 kind 隔离、事件必须带 `cache` 统计且原 `usage` 对象不变）。

---

## 附：杂项清理清单（非 bug，历史遗留）

- `test_progress.py` 悬空（引用不存在的 `_agent_status`），跑会 SKIP，可删。
- `_verify_fix.py`（DSML 诊断脚本，Bug 4 已修后无用）。
- `tui.py.bak` / `tui.py.backup` / `backup_conflict_files/` / `.DS_Store` / 测试图片（`compile.jpeg`、`seitsuna.png` 等）。
