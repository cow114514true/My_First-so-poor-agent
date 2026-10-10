# 运行时多模型切换 —— 可行性分析与改造设计

> 目标读者：后续接手改造的人（或未来的我）。
> 本文只做**分析 + 设计**，不含实现。实现前请先读 §1、§2。
> 与 [`BACKEND_SWITCH_GUIDE.md`](BACKEND_SWITCH_GUIDE.md) 是**互补**关系，见 §5——先读那篇的背景。

---

## 0. 一句话结论

**当前不支持运行时切换**，通过环境变量 `MODEL_BACKEND` 在 **import 时**三选一，选定后进程内不可变。
但架构是「窄腰」的：单模型假设集中在 `config.py` 加三个 helper，**没有散落到主循环**。
改造成「N 个 profile + 热切换」属**中等重构（约几百行）**，不是重写。

两个最容易被担心的问题，实测结论反而是好消息：
- **上下文继承几乎免费**：主对话消息是纯文本，天然可移植（§2）。
- **视觉 token 目前根本不在上下文里**：图片只在调用独立 VLM 的那一次临时构造（§3）。

真正要处理的只有三件事：**切换时的 size/裁剪重算**、**按模型重建 `extra_kwargs`**、**（若引入内联图片）图块降级**。

---

## 1. 现状解剖：单模型假设硬编码在哪

`config.py` 是唯一权威来源，其余模块引用它的**模块级单例**。

| 位置 | 现状 | 是否单模型假设 |
|---|---|---|
| `config.py` `_backend_config()` | 三个分支：`MODEL_BACKEND == "local-3.5"`（ctx 16384）/ `"local-3.6"`（ctx 8192）/ 其它→DeepSeek（ctx 65536）。各返回一份 dict | ✅ 选择逻辑在此 |
| `config.py` 模块末尾 | `cfg = _backend_config()`；`client = _maybe_wrap_headroom(OpenAI(...))`；`CURRENT_MODEL = cfg["model"]` | ✅ **import 时定死**，全项目 `from .config import cfg, client` 直接引用 |
| `config.py` `_is_local_backend()` | `os.environ.get("MODEL_BACKEND","").startswith("local")` → 布尔 | ✅ 不是「按模型取属性」，是布尔分支 |
| `runner.py` 两处 `create()` | `model=cfg["model"], ..., **cfg["extra_kwargs"]` | ✅ 每次调用读全局 `cfg` |
| `context.py` `_prune_cap()` | 用 `_is_local_backend()` 二选一算裁剪上限 | ✅ 布尔分支 |
| `tokens.py` `estimate_tokens()` | 同样 `_is_local_backend()` 二选一（本地打 `/tokenize` vs 一个 `tokenizer.json`） | ✅ 布尔分支 |
| `vision.py` | 已有**独立的第二套后端** `_vision_cfg()`，和主后端**完全脱钩**、也不可运行时切换 | ⚠️ 已是「多后端」雏形，但各自为政 |
| `config.py` headroom 包装 | `model_context_limits={cfg["model"]: cfg["ctx"]}` | ✅ 已按模型参数化，但只喂了一个模型 |
| 旧路径 `agent.py` | 直接把 `model="deepseek-v4-flash"`、`base_url=api.deepseek.com` 写死 | ✅ 历史遗留，与 TUI 主路径无关 |

**后果**：今天「切换模型」= 改环境变量 + **重启进程**。

---

## 2. 上下文继承评估

**好消息：主循环的消息是纯文本。**
主对话里的消息一律是 `{role, content: <str>}`（`runner.py` 工具结果用 `str(tool_result)` 拼），
**没有任何结构化 content list**——`image_url` 块只出现在 `vision.py` / `visual_locate.py` **内部**的临时请求里。
OpenAI 兼容的纯文本消息天然可移植，**同一份 `conv["messages"]` 可直接喂给任何兼容 API，继承基本免费。**

**但有 3 个模型耦合点会在切换时踩到：**

1. **`extra_kwargs` 是 DeepSeek 专属**（`reasoning_effort` + `extra_body.thinking`）。
   换到非 DeepSeek 后端可能 400。而 `runner.py:_safe_create()` 遇 **400 会回滚最后一轮工具调用**
   （为避免合成 tool_call_id 污染历史）——一个不兼容的 kwarg 会触发**假回滚**、污染历史。
   → 切换必须**按目标模型重建 `extra_kwargs`**。

2. **Token 记账会失效**。`conv["size"]` 是用**某一个** tokenizer 算出的滚动值，
   `_prune_cap()` / `_read_budget()` 也绑定当前后端。切换后 `size` 对新模型是错的。
   注意 `chunk.usage.prompt_tokens` 会在**成功发一次请求后**把 `size` 校正回真值，
   但那之前用它做裁剪判断是危险的。
   → 切换时**必须用新 tokenizer 重新估算 `conv["size"]`，并按新 `ctx` 重新裁剪**。

3. **工具调用协议 + 流式选项差异**。DeepSeek 用原生 `tool_calls`，同时有 XML 兜底解析
   （`xmlutil`，给不支持原生工具的模型）；`stream_options={"include_usage": True}` 也非所有 provider 支持。
   在「原生工具模型 ↔ XML 工具模型」之间切换会走**不同解析路径**。

（附带决策点：thinking 内容不入库，切模型即丢失历史推理。通常可接受，但需明确。）

---

## 3. 视觉 token 评估

**当前设计恰好绕开了难点。**
`analyze_image` / `visual_locate` 只在**发往独立 VLM 的那一次调用里临时**构造 `image_url` 块，
主循环拿到的**永远只是文本**。所以今天**不存在**「上下文里混着视觉 token」的问题。

**只有当你决定把图片喂进主对话**（主模型本身多模态）时，才需要处理：
- `image_url` 块细节是 provider 相关的（base64 data URI vs URL、`detail` 参数）；
- **不能再用文本 tokenizer 计图**——把 base64 塞进 `json.dumps` 计 token 会严重失真。
  必须按 provider 的图片计费规则估，或干脆不计、只信 API 返回的 `usage`；
- **切到纯文本模型时必须「降级」**：把每个 image 块换成文本占位符（`[image omitted]`
  或缓存的 VLM 描述）。最佳实践是**发图时同时保留一句文字描述**，这样降级近乎无损。

---

## 4. 建议的接缝（改造方向，非唯一解）

1. **`_backend_config()` → 具名 profile 注册表**。
   `cfg` 从「一个 dict」变成 `get_backend(name)` 返回一个**描述符**：
   ```
   { client, model, ctx, tokenizer, extra_kwargs,
     caps: {tools, xml_tools, vision, thinking},
     read_budget, prune_cap }
   ```
2. **把全局 `client`/`cfg`/`CURRENT_MODEL` 的引用改成「按调用时解析当前后端」**
   （一个 holder 对象或 `contextvar`，从而支持**每会话独立**后端）。
3. **用能力字段替换 `_is_local_backend()` 布尔分支**（`caps[...]` 驱动 `_prune_cap` / `estimate_tokens`）。
4. **会话带上 `model` 字段**；切换时：重新估算 `conv["size"]` → 按新 ctx 重裁剪 → 重建 `extra_kwargs`。
5. **视觉保持「工具化」**（当前最可移植）；若加内联图片，用结构化块 + **始终附文字描述**以便降级。
6. **headroom** 的 `model_context_limits` 已是按模型参数化，一并改成 per-model 即可。

---

## 5. 与 `BACKEND_SWITCH_GUIDE.md` 的关系

| | `BACKEND_SWITCH_GUIDE.md`（已有） | 本文 |
|---|---|---|
| 场景 | 二选一：DeepSeek ↔ 本地 llama.cpp | N 个模型 + 运行时热切换 |
| 切换时机 | **import 时**（环境变量 `MODEL_BACKEND`） | 进程内，按会话 |
| 范围 | 只改 `loop_agent_v2.py`（当时的单文件） | `config.py` + `runner`/`context`/`tokens` + 会话状态 |
| 结论 | 已落地 | 设计稿 |

那篇的「剥离后端专属参数」「未知参数被忽略」经验，在多模型场景**依然适用**，
但多模型多了「切换后重算 size/裁剪」这一层，是那篇没覆盖的。

---

## 6. 分级改造建议

- **L0（最小、当天可做）**：`_backend_config()` 扩成读一个 profile 名（如 `MODEL_BACKEND=deepseek` /
  `local-3.5` / …），保留现有单例结构 → **仍是重启式切换**，但新增模型只改一处。
- **L1（推荐）**：引入 profile 注册表 + `get_backend(name)` + `caps` 字段，把 `_is_local_backend()`
  的三处布尔分支换成能力查询。**不改交互**，行为等价。
- **L2（完整）**：会话级 `model` 字段 + 运行时切换命令 + size/裁剪重算 + （可选）切到纯文本时的图块降级。

**建议先落 L1 再谈 L2**：L1 是纯内部重构、可回归验证（行为应完全不变），
L2 才引入用户可见语义（历史里的图片、跨模型 token 口径）。

---

## 附录：改造后应验证的点

1. `MODEL_BACKEND` 未设 / 设成各 profile：`python -c "import loop_agent_core.config as c; print(c.CURRENT_MODEL, c.cfg['ctx'])"` 应打印对应值。
2. 切换后首轮请求不触发 `_safe_create` 的 400 回滚（检查 `api_error.log` 无新增）。
3. 长对话在切换后：`conv["size"]` 被重新估算，裁剪发生在**新** ctx 口径下。
4. 纯文本模型收到含图历史时，图块被降级为占位符而非报错。
5. headroom 的 `model_context_limits` 对新模型生效（看启动打印 `[headroom] wrapped ... model=...`）。
