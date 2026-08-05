# 网络能力升级执行文档

> 你依据本文档自己改代码。我不直接写代码，只给事实、位置、设计建议、验证标准。
> 读完第 0、1、2 节再动手。任何一步验证失败，先看第 5 节。

---

## 0. 目标与已定决策

**目标**：网络能力两步走——
1. **通用浏览器操作底座 `browser_act`**（替换 `browse_web`，治本）
2. **接入 Tavily**：新增 `search_web` + `fetch_url`

**已定决策**（不可改，除非你改变主意）：
- **① browser_act 单步操作**：每次调用只做**一个**动作（open/click/type/press_enter/scroll/wait/back/screenshot/close），返回**页面状态 + 可交互元素清单（编号）**，主 agent 看清单选下一个动作，逐步驱动浏览器。会话在多次调用间持久。
- **① 专用浏览器线程**：所有 Playwright 调用归入单一专用线程（`BrowserExecutor`），根治 `cannot switch to a different thread`。
- **① goto 用 `domcontentloaded` + 短超时**，不再死等 `networkidle`。
- **① 替换 `browse_web`，不并存**。
- **① 视觉本轮不做**，只留 `screenshot` 动作返回图片路径作钩子（接本地视觉模型是后话）。
- **② Tavily**：免费额度够用（1000 credits/月）。`search_web`（基础搜索 1 credit）+ `fetch_url`（extract 每 5 URL 1 credit，失败不扣费）。用 **stdlib `urllib`** 实现，**零新 Python 依赖**。
- **② `TAVILY_API_KEY` 环境变量**，同 `DS_KEY` 套路。
- **D1 会话内监控：搁置**，本阶段不做。

---

## 1. 现状解剖：网络工具现在长什么样、弱在哪

| 工具 | 实现位置 | 现状弱点（真实对话证据） |
|---|---|---|
| `browse_web` | `browse.py` `browse_web_mock` (195–233)；动作解析在 `_execute_instructions` (70–166) | ① **一次性正则猜动作**：给一串自然语言指令，正则抠出 click/search/type 各执行一遍，看不到页面状态，猜错就拉倒。<br>② **goto 死等 `networkidle`**：GitHub 之类大页 60s 超时（`conversations/chat_20260803_230605.json` 有 `Page.goto: Timeout 60000ms exceeded`）。<br>③ **线程错乱**：`cannot switch to a different thread (which happens to have exited)` 在多个会话反复出现（如 `chat_20260804_011403.json`、`chat_20260805_153143.json`），模型被逼着改走 `use_ds_from_web`。 |
| `use_ds_from_web` | `ds_web.py` `use_ds_from_web_mock` (146–156) | 独立职责（DeepSeek 联网搜索/识图），**本轮只给它加线程归属，逻辑一行不改**。它与 `browse_web` 共享 `_get_playwright()` 单例，线程单例在不同线程用必崩——所以必须一起收进 executor。 |
| `get_weather` | `shell_tools.py` (11–12) | 假数据（写死 Cloudy 7~13°C）。**不在本轮范围**，留作后话（换真实天气 API）。 |

---

## 2. 设计建议

### 2-1 专用浏览器线程 `BrowserExecutor`（放`ds_web.py`）

Playwright sync API 要求所有调用在**同一个线程**。现在 `_get_playwright()` 的单例 `_shared_pw` 可能在线程 A 创建、线程 B 使用 → 崩。解决：一个专用线程 + 任务队列，所有浏览器操作经它执行。

建议代码（直接加进 `ds_web.py`，`browse.py` 引用）：

```python
import queue
import threading

class BrowserExecutor:
    """把所有 Playwright 调用归入单一专用线程。"""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="browser-executor", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            fn, result_q = self._jobs.get()
            try:
                result_q.put(("ok", fn()))
            except Exception as e:
                result_q.put(("error", e))

    def call(self, fn):
        """在浏览器线程执行 fn，返回其结果；异常上抛给调用方。"""
        result_q = queue.Queue()
        self._jobs.put((fn, result_q))
        status, value = result_q.get()
        if status == "error":
            raise value # 为什么这里要手动引发异常？
        return value
_executor = BrowserExecutor()
```

然后：
- `use_ds_from_web_mock` 整个函数体包一层 `return _executor.call(work)`（`_ds_session` 全局读写都收进 executor 线程）。
- `_get_playwright()` 只在 executor 线程内被调用（docstring 更新）。

### 2-2 `browser_act`（重写 `browse.py` 的 `browse_web_mock`）

**核心契约**：每次调用做**一个**动作，返回 `URL + Title + 元素清单 + 页面文本（截断 4000）`。

- **保留**：`_validate_url`、`_get_browser_page`（`(profile, headed)` 缓存 = 持久会话）、`_save_profile`、`_extract_page_text`、`_take_screenshot`。
- **删除**：`_execute_instructions`（70–166，一次性正则）。
- **新增**三个函数（元素编号机制）：

```python
_ELEMENT_SELECTORS = (
    "button", "a[href]", "input:not([type='hidden'])",
    "textarea", "select", "[role='button']", "[role='link']", "[role='textbox']",
)

def _visible_elements(page):
    """按固定选择器顺序收集可见元素——列表展示和编号点击必须用同一顺序。"""
    els = []
    for sel in _ELEMENT_SELECTORS:
        for el in page.locator(sel).all():
            try:
                if el.is_visible():
                    els.append(el)
            except Exception:
                continue
    return els

def _describe(el):
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()") or "el"
        role = el.get_attribute("role")
        kind = role or {"a": "link", "input": "input", "textarea": "input",
                        "select": "select"}.get(tag, tag)
        label = (el.get_attribute("placeholder") or el.get_attribute("aria-label")
                 or el.get_attribute("title")
                 or (el.inner_text() or "").strip().replace("\n", " ")
                 or (el.get_attribute("name") or ""))
        return f"<{kind}> {label.strip()[:60]}"
    except Exception:
        return "<el> ?"

def _list_interactive_elements(page, limit=30):
    return [f"[{i+1}] {_describe(el)}" for i, el in enumerate(_visible_elements(page)) if i < limit]

def _find_element(page, target):
    try:
        idx = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"target 必须是元素清单里的编号，拿到: {target!r}")
    els = _visible_elements(page)
    if 1 <= idx <= len(els):
        return els[idx - 1]
    raise ValueError(f"元素编号 {idx} 超出范围 (1-{len(els)})。用最新返回的元素清单。")
```

- **`browser_act_mock` 动作表**：

| action | 参数 | 说明 |
|---|---|---|
| `open` | `url` | `_validate_url` 后 goto，`wait_until="domcontentloaded", timeout=30000`，再 `try: wait_for_load_state("networkidle", timeout=5000) except: pass`，最后 `wait_for_timeout(500)` |
| `click` | `target` | `_find_element(page, target).click()`，`wait_for_timeout(800)` |
| `type` | `target`, `text` | `_find_element(page, target).fill(text)` |
| `press_enter` | — | `page.keyboard.press("Enter")`，`wait_for_timeout(800)` |
| `scroll` | `text` | `"down"`（默认）→ `PageDown`，`"up"` → `PageUp` |
| `wait` | `text` | `min(int(text or 1), 10)` 秒 |
| `back` | — | `page.go_back(wait_until="domcontentloaded", timeout=30000)` |
| `screenshot` | — | 存 `screenshots/`，返回路径（视觉钩子，本轮不喂模型） |
| `close` | — | `browser.close()`，清掉 `_browser_sessions` 对应 key |

- **返回格式**：`[browser_act] action: X` → `OK: ...` → `URL:` → `Title:` → `Elements:` + 编号清单 → `--- Page Content ---` + 截断文本。
- **失败统一**：`[Error] browser_act failed: ...`（`validation.py` 依赖此前缀）。
- **整个函数体包进 `_executor.call(work)`**：`return _executor.call(work)`。

### 2-3 Tavily（新增 `tavily_tools.py`）

- 端点：`POST https://api.tavily.com/search`、`POST https://api.tavily.com/extract`。
- 计费（免费 1000 credits/月）：基础搜索 1 credit/次；extract 每 5 成功 URL 1 credit，**失败不扣费**。
- 建议代码（stdlib，零依赖）：

```python
"""Tavily 搜索/读页工具（stdlib urllib 实现，零新依赖）。"""
import json
import urllib.request

from .config import _TAVILY_API_KEY

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def search_web_mock(query, max_results=5, search_depth="basic"):
    """网页搜索，返回标题/URL/内容片段列表。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_API_KEY 未设置。设置环境变量后重试。"
    data = _post(_SEARCH_URL, {
        "api_key": _TAVILY_API_KEY, "query": query,
        "max_results": max_results, "search_depth": search_depth,
    })
    results = data.get("results", [])
    if not results:
        return "[Error] 搜索无结果，换个 query 或稍后再试。"
    return "搜索结果:\n" + "\n".join(
        f"[{i+1}] {r.get('title','')}\n    {r.get('url','')}\n    {r.get('content','')[:300]}"
        for i, r in enumerate(results))


def fetch_url_mock(url, max_chars=8000):
    """读指定网页正文（Tavily extract 清洗版）。"""
    if not _TAVILY_API_KEY:
        return "[Error] TAVILY_API_KEY 未设置。设置环境变量后重试。"
    data = _post(_EXTRACT_URL, {"api_key": _TAVILY_API_KEY, "urls": [url]})
    results = data.get("results", [])
    if not results:
        return f"[Error] extract 无返回: {url}"
    r = results[0]
    content = (r.get("content") or r.get("raw_content") or "").strip()
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n...(截断, 共 {len(content)} chars)"
    return f"--- {r.get('url')} ---\n{r.get('title','')}\n{content}"
```

### 2-4 改动面清单（文件 → 改什么）

| 文件 | 改动 |
|---|---|
| `loop_agent_core/ds_web.py` | 加 `BrowserExecutor` + `_executor`；`use_ds_from_web_mock` 包 `_executor.call` |
| `loop_agent_core/browse.py` | 删 `_execute_instructions`；`browse_web_mock` → `browser_act_mock`；加 `_ELEMENT_SELECTORS`/`_visible_elements`/`_describe`/`_find_element` |
| `loop_agent_core/schemas.py` | `browse_web` schema (112–144) → `browser_act`；**追加** `search_web` + `fetch_url` schema |
| `loop_agent_v2.py` | import (24–26) 换 `browser_act_mock`；`TOOL_CALL_MAP` (52) `browse_web` → `browser_act`；**注册** `search_web`/`fetch_url` |
| `loop_agent_core/validation.py` | `browse_web` 分支 (39–43) → `browser_act`；**追加** `search_web`/`fetch_url` 轻量校验 |
| `loop_agent_core/prompts.py` | `### browse_web` 块 (11–15) → `### browser_act` 单步协议说明；line 35 的 `browse_web` → `search_web`/`browser_act` |
| `loop_agent_core/config.py` | 加 `_TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")`（放 5–6 行附近） |
| `README.md` | 同步工具列表（最后做，可后补） |

**外部契约提醒**（一条都不能破）：`tui.py` import 的 `chat` / `messages` / `CURRENT_MODEL` / `system_prompt` / `_shell_output_queue`，以及两个测试 import 的符号。**`browse_web` 不被任何测试引用**（已核实），替换安全。`_execute_instructions` 也只有 `browse.py` 内部用，可删。

### 2-5 `browser_act` 的 schema 描述（教模型怎么用）

要点：**一步一动作**、**看返回的元素清单选编号**、**搜索优先用 search_web / use_ds_from_web**、**浏览器只用于需要真交互的站**（登录/表单/JS）。actions 用 enum。

```python
{
    "type": "function",
    "function": {
        "name": "browser_act",
        "description": "Operate a real browser, ONE step per call. The session persists between calls. Each call performs a single action, then returns the page URL, title, a numbered list of interactive elements, and page text. Study the element list and drive the next step by number. Actions: open(url=), click(target=), type(target=, text=), press_enter(), scroll(text='down'/'up'), wait(text=seconds), back(), screenshot(), close(). For quick web searches or just reading a page, prefer search_web or use_ds_from_web; reserve browser_act for sites needing real interaction (login, forms, JS).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "click", "type", "press_enter", "scroll", "wait", "back", "screenshot", "close"]},
                "url": {"type": "string", "description": "For open: the http(s) URL."},
                "target": {"type": "integer", "description": "For click/type: element number from the last returned element list (1-based)."},
                "text": {"type": "string", "description": "For type: text to fill. For scroll: 'down' or 'up'. For wait: seconds (max 10)."},
                "profile": {"type": "string", "description": "Optional profile name for saved logins (e.g. 'github'). Omit for a clean session."},
                "headed": {"type": "boolean", "description": "Show the browser window. Default false (headless)."}
            },
            "required": ["action"]
        }
    }
}
```

---

## 3. 实施步骤（每步独立可验证）

**Step 1 — `ds_web.py` 加 executor + 包 `use_ds_from_web`**
验证：`python -c "import loop_agent_v2"` 不炸；有 DeepSeek 登录态时跑一轮 `use_ds_from_web` 确认旗舰功能没坏。

**Step 2 — `browse.py` 重写为 `browser_act`**
验证：`python -c "import loop_agent_v2"` 不炸。命令行直调：
```bash
python -c "from loop_agent_core.browse import browser_act_mock; print(browser_act_mock('open', url='https://example.com')[:500])"
```
应返回 `[browser_act] action: open` + URL + Title + 元素清单 + 页面文本，**不出现 thread-switch 错误**。

**Step 3 — `schemas.py` 换 schema + `loop_agent_v2.py` 注册**
验证：
```bash
python -c "import loop_agent_v2; print([t['function']['name'] for t in loop_agent_v2.tools])"
```
应含 `browser_act`、不含 `browse_web`。

**Step 4 — `validation.py` + `prompts.py`**
验证：
```bash
python -c "import loop_agent_v2; print(loop_agent_v2.validate_tool_result('browser_act', '[Error] browser_act failed: Access denied: localhost'))"
```
应返回 `(False, ...)`。

**Step 5 — Tavily（`tavily_tools.py` + `config.py` + schema + 注册 + validation）**
验证：设 `TAVILY_API_KEY` 后：
```bash
python -c "from loop_agent_core.tavily_tools import search_web_mock; print(search_web_mock('python asyncio')[:400])"
```
应返回带 URL 的搜索结果。未设 key 时应返回 `[Error] TAVILY_API_KEY 未设置`，不崩溃。

**Step 6 — 回归**
```bash
python test_code_index.py    # 18 项全过
python test_multi_agent.py   # 7 项全过
```

---

## 4. 验证清单（复制即用）

**DeepSeek 模式**（需 `DS_KEY`，或本地模式同理）：
```bash
python loop_agent_v2.py
# 输入: 打开 example.com 看看上面有什么
#   → 模型应 browser_act open → 拿到元素清单 → 点击/滚动，全程无 thread-switch 错误
# 输入: 帮我搜一下 python 3.13 的新特性
#   → 应触发 search_web（Tavily 已接入后），返回带链接的结果
# 输入: 帮我看看 https://docs.python.org/3/whatsnew/3.13.html 讲了什么
#   → 应触发 fetch_url，返回清洗后的正文
```

**验证失败的信号**：
- `cannot switch to a different thread` 再出现 → executor 没包全（`use_ds_from_web` 或 `browse_act` 有操作漏在 executor 外）。
- goto 再超时 → 主等待还是 `networkidle`，没改 `domcontentloaded`。
- 元素点击对不上 → `_visible_elements` 顺序变了或 `_list`/`_find` 用了不同函数。

---

## 5. 坑与提示

1. **executor 死锁**：`browser_act` 的 `work()` 内部**绝不能再调 `_executor.call`**（嵌套会死锁）。所有 Playwright 操作都在 `work()` 里写完，只包一层。
2. **元素编号一致性**：`_list_interactive_elements` 和 `_find_element` 必须走**同一个** `_visible_elements`（同选择器、同顺序），否则编号对不上。
3. **`_get_playwright` 只在 executor 线程调用**——`use_ds_from_web` 内部也一样，所以必须一起包进去。
4. **goto 主等待用 `domcontentloaded` + 短超时**，`networkidle` 只作 5s 兜底 try/except，别再当主等待。
5. **validation 依赖 `[Error] browser_act failed` 前缀**——保持这个返回格式，别改。
6. **Tavily extract 是重新爬取**，可能滞后或对 JS 重/登录墙页面失败——失败不扣费，直接回落 `browser_act`（浏览器）或重试即可。
7. **别动 `tui.py`**。外部契约符号一个都不能丢。
8. `browser_act` 一次调用一次 API 往返——**本地 Qwen 模式**下模型会逐轮调工具，多步浏览可能慢；元素清单能大幅降低它乱点概率，属预期收益。
9. 视觉（截图喂本地 Qwen）是后话：`screenshot` 动作返回路径即钩子，先别接。

---

改完 + 验证通过后，把结果告诉我。如果哪一步卡住，把报错贴出来。
