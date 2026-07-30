import os
from openai import OpenAI
import sys
import json
import re
import ipaddress
from datetime import datetime
from urllib.parse import urlparse
import subprocess
from playwright.sync_api import sync_playwright
import time

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_date",
            "description": "Get the current date",
            "parameters": { "type": "object", "properties": {} },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather of a location, the user should supply the location and date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": { "type": "string", "description": "The city name" },
                    "date": { "type": "string", "description": "The date in format YYYY-mm-dd" },
                },
                "required": ["location", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exec_shell_win",
            "description": "Execute the shell(cmd[default] or powershell) command in windows_os",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_cmd": {"type": "string",
                                "description":"Commands you want to execute in cmd or powershell"}
                },
                "required":["shell_cmd"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "use_ds_from_web",
            "description": """Using this tool function,you will get two ability
            'First': 'you could read pictures but only konw the words,tables such things in pictures',
            'Second': 'you could get the latest infomations by enabling the search with network using this tool when you need to find ways to solve problems'""",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "When you need to pass pictures you need provide this parameter or this would be empty string"
                    },
                    "ask_prompt": {
                        "type": "string",
                        "description": "This parameter is necessary,you need to provide this to tell deepseek on web to know the content of picture you pass or the infomations you want to know"
                    }
                },
                "required": ["file_path","ask_prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Path is resolved relative to the agent's working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read (relative or absolute within working directory)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates new files, overwrites existing ones. Path is resolved relative to the agent's working directory. Use exec_shell_win with 'dir' to list existing files first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write (relative or absolute within working directory)"},
                    "content": {"type": "string", "description": "The complete file content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browse_web",
            "description": "Browse any website — navigate, click, fill forms, scroll, extract content. Use for accessing the open web: Google searches, documentation, articles, any URL. For DeepSeek's built-in search/image-recognition, prefer use_ds_from_web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to. Must start with http:// or https://"
                    },
                    "instructions": {
                        "type": "string",
                        "description": "What to do, in plain English. 'extract the page content' to just read. 'search for X' to find search box and submit. 'click the Login button then fill the form' for interactions. 'scroll down and extract the article' to scroll first."
                    },
                    "output": {
                        "type": "string",
                        "enum": ["text", "screenshot", "both"],
                        "description": "Return format. 'text' = page text (default). 'screenshot' = image (feed to use_ds_from_web for analysis). 'both' = both."
                    },
                    "profile": {
                        "type": "string",
                        "description": "Browser profile name for saved logins (e.g. 'github', 'taobao'). Omit for a clean session."
                    },
                    "headed": {
                        "type": "boolean",
                        "description": "Show browser window. Default false (headless)."
                    }
                },
                "required": ["url", "instructions"]
            }
        }
    }
]

def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")

def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"

def exec_shell_win_mock(shell_cmd):
    """Execute shell command with real-time output pushed to _shell_output_queue."""
    q = _shell_output_queue  # ponytail: set by TUI before agent runs, None in headless

    if q:
        q.put(("shell_start", shell_cmd))

    proc = subprocess.Popen(
        shell_cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    all_lines = []
    try:
        for line in proc.stdout:
            all_lines.append(line)
            if q:
                q.put(("shell_line", line))
    except Exception:
        pass

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        if q:
            q.put(("shell_line", "\n--- TIMED OUT after 120s ---\n"))

    stdout_text = "".join(all_lines)

    if q:
        q.put(("shell_done", proc.returncode))

    return json.dumps({
        "stdout": stdout_text,
        "stderr": "",
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
    }, ensure_ascii=False, indent=2)

# ponytail: shared queue for TUI live shell output; None in headless mode
_shell_output_queue = None

WORK_DIR = os.path.dirname(os.path.abspath(__file__))

def _resolve_path(path):
    """解析路径，限定在 WORK_DIR 内。越界则抛异常。"""
    if os.path.isabs(path):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(WORK_DIR, path))
    # 规范化后检查是否在 WORK_DIR 内
    if os.path.commonpath([resolved, WORK_DIR]) != os.path.abspath(WORK_DIR):
        raise ValueError(f"Access denied: '{path}' resolves outside working directory '{WORK_DIR}'")
    return resolved

def read_file_mock(path):
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return f"[Error] File not found: {resolved}"
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        return f"[Error] Cannot read '{resolved}' as UTF-8 text (binary file?)"

def write_file_mock(path, content):
    resolved = _resolve_path(path)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as f:
        f.write(content)
    size = os.path.getsize(resolved)
    return f"Written {size} bytes to {resolved}"

# log_in upload_files enter_confirm input_prompt get_response
_shared_pw = None  # ponytail: single sync_playwright instance shared by all tools
_ds_session = None

def _get_playwright():
    """Return singleton sync_playwright instance. Safe to call from any thread."""
    global _shared_pw
    if _shared_pw is None:
        _shared_pw = sync_playwright().start()
    return _shared_pw

def log_in():
    pw = _get_playwright()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(storage_state="./profile.json")
    page = context.new_page()
    page.goto("https://chat.deepseek.com")
    return {
        "page": page,
        "context": context,
        "browser": browser,
        "playwright": pw
    }
def upload_files(session,file_path):
    page = session["page"]
    if file_path == "" or file_path.strip() == "":
        sys.stderr.write("Path is empty\n")
        return None
    else:
        page.set_input_files("input[type='file']",file_path)
        page.wait_for_timeout(2000)
        return "Upload success"

def input_prompt(session,ask_prompt):
    page = session["page"]
    if ask_prompt == "" or ask_prompt.strip() == "":
        sys.stderr.write("Empty prompt is not allowed\n")
        return None
    input_selectors = [
            "textarea[placeholder*='向DeepSeek提问']",
            "textarea[placeholder*='问题']",
            "div[contenteditable='true']",
            "textarea"
        ]
        
    input_el = None
    for sel in input_selectors:
        el = page.query_selector(sel)
        if el and el.is_visible():
            input_el = el
            break
        
    if not input_el:
        raise RuntimeError("未找到输入框元素")
        
    input_el.fill(ask_prompt)
    return input_el


def enter_confirm(session):
    page = session["page"]
    send_selectors = [
            "button[type='submit']",
            "button:has(svg[class*='send'])",
            "button[aria-label='发送']"
        ]
        
    sent = False
    for sel in send_selectors:
        btn = page.query_selector(sel)
        if btn and btn.is_visible() and btn.is_enabled():
            btn.click()
            sent = True
            break
        
    if not sent:
        input_selectors = [
            "textarea[placeholder*='向DeepSeek提问']",
            "textarea[placeholder*='问题']",
            "div[contenteditable='true']",
            "textarea"
        ]
        for sel in input_selectors:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.press("Enter")
                sent = True
                break
        if not sent:
            page.keyboard.press("Enter")

def get_response(session):
    page = session["page"]
    timeout = 300  # 5分钟，足够覆盖 DeepSeek 网页版搜索回复
    start_time = time.time()
    last_content = ""
    stable_count = 0
    min_elapsed_before_return = 3  # 至少等 3 秒再判定完成，避免刚生成第一段就截断

    response_selectors = [
        "[class*='message-assistant'] [class*='markdown']",
        "[class*='assistant'] [class*='content']",
        ".ds-markdown",
        "[class*='chat-message']:last-child"
    ]

    while time.time() - start_time < timeout:
        time.sleep(0.5)  # 避免忙循环，给页面渲染留时间

        current_content = ""
        for sel in response_selectors:
            els = page.query_selector_all(sel)
            if els:
                current_content = els[-1].inner_text()
                break

        if current_content and current_content == last_content:
            stable_count += 1
        else:
            stable_count = 0
            last_content = current_content

        elapsed = time.time() - start_time
        # 内容稳定 2 秒（4 次 × 0.5s）且至少过了 3 秒，认为回复完成
        if elapsed > min_elapsed_before_return and stable_count >= 4 and current_content:
            return current_content

    # 超时返回已有内容（可能不完整），不返回 None
    return last_content if last_content else "[get_response timeout] No response captured"

def use_ds_from_web_mock(file_path,ask_prompt):
    global _ds_session
    if _ds_session is None:
        _ds_session = log_in()
    if file_path and file_path.strip():
        upload_files(_ds_session, file_path)
    input_prompt(_ds_session, ask_prompt)
    enter_confirm(_ds_session)
    print("[DEBUG] enter_confirm 完成，即将调用 get_response\n")
    response = get_response(_ds_session)
    print("[DEBUG] get_response")

    return response

# ── browse_web: general-purpose web browser ──

def _validate_url(url: str) -> str:
    """Block localhost, private IPs, file://. Return normalized URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http/https allowed, got: {parsed.scheme}")
    host = parsed.hostname or ""
    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"):
        raise ValueError(f"Access denied: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP address, allow hostname
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"Access denied: private/loopback network {host}")
    return url


# ponytail: shared browser instances keyed by (profile, headed)
_browser_sessions = {}
_PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


def _get_browser_page(profile: str, headed: bool):
    """Return (page, ctx, browser) for the given profile. Lazily creates and caches."""
    key = (profile or "__default__", headed)
    if key in _browser_sessions:
        sess = _browser_sessions[key]
        try:
            # Return existing page (reuse tab)
            return sess["page"], sess["ctx"], sess["browser"]
        except Exception:
            pass  # stale session, recreate

    pw = _get_playwright()
    browser = pw.chromium.launch(headless=not headed)

    # Load storage state if profile exists
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json") if profile else None
    if storage_path and os.path.exists(storage_path):
        ctx = browser.new_context(storage_state=storage_path)
    else:
        ctx = browser.new_context()

    page = ctx.new_page()
    _browser_sessions[key] = {"page": page, "ctx": ctx, "browser": browser, "playwright": pw}
    return page, ctx, browser


def _save_profile(profile: str, ctx) -> None:
    """Persist browser storage state (cookies, localStorage) to disk."""
    if not profile:
        return
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    storage_path = os.path.join(_PROFILES_DIR, f"{profile}.json")
    ctx.storage_state(path=storage_path)


def _execute_instructions(page, instructions: str) -> str:
    """Parse instructions and execute browser actions. Returns a summary."""
    actions_done = []

    # ── click ──
    for m in re.finditer(
        r'click\s+(?:on\s+)?(?:the\s+)?["\']?(?P<target>.+?)["\']?\s*(?:button|link|element|tab)?\s*(?:$|[,;.]|\s+(?:and|then|after|to))',
        instructions, re.I,
    ):
        target = m.group("target").strip().rstrip('"\' ')
        if not target:
            continue
        try:
            el = (
                page.get_by_role("button", name=target).first
                or page.get_by_role("link", name=target).first
                or page.get_by_text(target, exact=True).first
            )
            # Click based on element type
            found = page.get_by_role("button", name=re.compile(target, re.I)).first
            if not found or not found.is_visible():
                found = page.get_by_role("link", name=re.compile(target, re.I)).first
            if not found or not found.is_visible():
                found = page.get_by_text(target).first
            if found and found.is_visible():
                found.click()
                actions_done.append(f"clicked '{target}'")
                page.wait_for_timeout(1500)
        except Exception as e:
            actions_done.append(f"click '{target}' failed: {e}")

    # ── type / fill ──
    for m in re.finditer(
        r'(?:type|fill|enter|input)\s+["\']?(?P<text>.+?)["\']?\s+(?:in|into|on)\s+(?:the\s+)?["\']?(?P<target>.+?)["\']?\s*(?:field|input|box|form)?\s*(?:$|[,;.]|\s+(?:and|then))',
        instructions, re.I,
    ):
        text = m.group("text").strip().rstrip('"\' ')
        target = m.group("target").strip().rstrip('"\' ')
        if not text or not target:
            continue
        try:
            # Find input by placeholder, label, or nearby text
            el = (
                page.get_by_placeholder(target).first
                or page.get_by_label(target).first
                or page.locator(f"input[name*='{target}']").first
            )
            if not el or not el.is_visible():
                # Fallback: find any visible input near the target text
                el = page.get_by_text(target).first
                if el and el.is_visible():
                    # Try to find input near this text
                    pass  # too complex for v1
            if el and el.is_visible():
                el.fill(text)
                actions_done.append(f"typed '{text}' into '{target}'")
                page.wait_for_timeout(500)
        except Exception as e:
            actions_done.append(f"type '{text}' failed: {e}")

    # ── search ──
    for m in re.finditer(
        r'search\s+(?:for\s+)?["\']?(?P<query>.+?)["\']?(?:\s*$|\s*(?:and|then|after|\.))',
        instructions, re.I,
    ):
        query = m.group("query").strip().rstrip('"\' ')
        if not query:
            continue
        try:
            # Find search input
            search_el = (
                page.get_by_role("searchbox").first
                or page.get_by_placeholder(re.compile(r"search|搜索", re.I)).first
                or page.locator("input[type='search']").first
                or page.locator("input[name='q']").first  # common search param
            )
            if search_el and search_el.is_visible():
                search_el.fill(query)
                search_el.press("Enter")
                actions_done.append(f"searched for '{query}'")
                page.wait_for_timeout(2000)
        except Exception as e:
            actions_done.append(f"search '{query}' failed: {e}")

    # ── scroll ──
    if re.search(r'scroll\s+down', instructions, re.I):
        page.keyboard.press("PageDown")
        actions_done.append("scrolled down")
        page.wait_for_timeout(500)
    if re.search(r'scroll\s+up', instructions, re.I):
        page.keyboard.press("PageUp")
        actions_done.append("scrolled up")
        page.wait_for_timeout(500)

    # ── wait ──
    wait_m = re.search(r'wait\s+(\d+)\s*(?:seconds?|s)', instructions, re.I)
    if wait_m:
        secs = min(int(wait_m.group(1)), 10)  # cap at 10s
        page.wait_for_timeout(secs * 1000)
        actions_done.append(f"waited {secs}s")

    return "; ".join(actions_done) if actions_done else "no actions executed"


def _extract_page_text(page) -> str:
    """Extract readable text from the page."""
    try:
        title = page.title() or ""
        # Remove script/style content for cleaner text
        body_text = page.evaluate("""() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll('script, style, noscript, nav, footer, [role="navigation"]').forEach(el => el.remove());
            return clone.innerText || '';
        }""")
        # Clean up excessive whitespace
        body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
        return f"Title: {title}\n\n{body_text}"
    except Exception as e:
        return f"[extract text failed: {e}]"


def _take_screenshot(page) -> str:
    """Take screenshot, save to temp file, return path."""
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    page.screenshot(path=path, full_page=False)
    return path


def browse_web_mock(url, instructions, output="text", profile="", headed=False):
    """Browse any website and return content."""
    url = _validate_url(url)
    page, ctx, browser = _get_browser_page(profile, headed)

    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1000)  # extra settle time for SPAs

        action_summary = _execute_instructions(page, instructions)

        # Wait for any post-action navigation
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # timeout on networkidle is OK

        result_parts = [f"[browse_web] URL: {url}"]
        if action_summary:
            result_parts.append(f"Actions: {action_summary}")

        if output in ("text", "both", ""):
            text = _extract_page_text(page)
            # Truncate very long pages
            if len(text) > 8000:
                text = text[:8000] + f"\n\n... (truncated, {len(text)} chars total)"
            result_parts.append(f"--- Page Content ---\n{text}")

        if output in ("screenshot", "both"):
            screenshot_path = _take_screenshot(page)
            result_parts.append(f"--- Screenshot saved ---\n{screenshot_path}\n[Use use_ds_from_web with this path to analyze. Ask briefly: 'Describe this screenshot concisely in plain text. No fluff.']")

        # Persist profile state on each use
        _save_profile(profile, ctx)

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"[Error] browse_web failed: {e}"


def validate_tool_result(tool_name, result):
    """返回 (is_valid, critique)。
    critique 为空表示通过；不通过时 critique 直接作为质疑消息喂给模型。"""
    text = str(result) if result else ""

    # -- 通用检查 --
    if not result:
        return False, f"Tool '{tool_name}' returned empty/None. Check for timeout, selector mismatch, or network issue."
    if len(text) < 10:
        return False, f"Tool '{tool_name}' result is too short ({len(text)} chars). Possibly truncated or failed silently."

    # -- 工具专属检查 --
    if tool_name == "exec_shell_win":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return True, ""  # 非标准输出不过度质疑
        stderr = data.get("stderr", "")
        returncode = data.get("returncode", 0)
        issues = []
        if returncode != 0:
            issues.append(f"returncode={returncode} (command failed)")
        if stderr and stderr.strip():
            issues.append(f"stderr is non-empty: {stderr[:200]}")
        if issues:
            return False, f"Shell result suspicious: {'; '.join(issues)}. Retry with corrected command or handle the error."

    elif tool_name == "use_ds_from_web":
        if "[get_response timeout]" in text or "[Error]" in text:
            return False, f"Web capture failed: {text[:200]}. Retry or simplify the ask_prompt."
        if len(text) < 50:
            return False, f"Web capture too short ({len(text)} chars), likely truncated. Retry with shorter ask_prompt or check page state."

    elif tool_name == "browse_web":
        if "[Error]" in text and "Access denied" in text:
            return False, f"URL blocked: {text[:200]}. Use a different URL."
        if text.startswith("[Error]"):
            return False, f"browse_web failed: {text[:300]}. Retry with adjusted instructions or check the URL."

    elif tool_name == "get_weather":
        if "Error" in text or "fail" in text.lower():
            return False, f"Weather result looks wrong: {text[:200]}. Verify location and date, retry."

    elif tool_name == "read_file":
        if text.startswith("[Error]"):
            return False, f"Read failed: {text[:200]}. Check path and try again."

    elif tool_name == "write_file":
        if text.startswith("[Error]") or "Access denied" in text:
            return False, f"Write failed: {text[:200]}. Check path and retry."
        if "Written" in text and "bytes" in text:
            return True, ""  # 明确成功

    # get_date: 基本不可能出错，跳过

    return True, ""

# -- ponytail: response quality guard, prevents DSML/garbage from polluting messages --
def _is_garbage_content(text: str) -> bool:
    """True if the response looks like DSML garbage / XML leakage."""
    if not text or not text.strip():
        return True
    t = text.strip()
    # DSML markers
    if "<DSML" in t or "<dsml" in t or "|DSML|" in t:
        return True
    # Raw XML declaration
    if t.startswith("<?xml"):
        return True
    # Excessive markup density (angle brackets >30% of chars, but skip legit code)
    angle_count = t.count("<") + t.count(">")
    if len(t) > 100 and angle_count / len(t) > 0.3:
        return True
    return False

TOOL_CALL_MAP = {
    "get_date": get_date_mock,
    "get_weather": get_weather_mock,
    "exec_shell_win": exec_shell_win_mock,
    "use_ds_from_web": use_ds_from_web_mock,
    "browse_web": browse_web_mock,
    "read_file": read_file_mock,
    "write_file": write_file_mock,
}


client = OpenAI(
    api_key=os.environ.get("DS_KEY"),
    base_url="https://api.deepseek.com"
)

system_prompt = """You are a helpful, self-critical assistant.

## Tool use
- Use tools whenever needed. Do not guess when a tool can give a definitive answer.
- After receiving a tool result, critically evaluate it: Does it make sense? Is it complete? Is it internally consistent?
- If a [Self-check] message flags a tool result as questionable, seriously reconsider it. Retry the tool with corrected parameters, or explain why the result is actually usable.

### browse_web
- For general web browsing: Google searches, documentation, articles, any website.
- Put site-specific instructions in plain English: "click the Login button", "search for Python asyncio", "scroll down and extract the article text".
- Default returns page text. Use output="screenshot" when the page has complex layout/charts, then feed the screenshot path to use_ds_from_web for analysis.
- When using use_ds_from_web on a screenshot, ask concisely: "Describe this screenshot briefly. No fluff."

### use_ds_from_web
- For DeepSeek's built-in web search and image recognition. Prefer browse_web for direct URL access.

## When to ask the user instead of guessing
- If the user's request is ambiguous (multiple interpretations with different outcomes), ask for clarification.
- If you need information only the user can provide (file paths, credentials, preferences, personal context), ask — do not fabricate.
- If a tool repeatedly fails and you cannot resolve it, tell the user what went wrong and ask how to proceed.
- Time-sensitive queries (weather, news, current events): always verify via use_ds_from_web or browse_web before answering.

## Answer quality
- Distinguish clearly between facts you verified with tools and inferences you are making.
- If uncertain about anything, state your uncertainty explicitly."""

messages = [
    {"role": "system", "content": system_prompt}
]

# -- ponytail: headless 模式的状态追踪，仅 _default_on_event 使用 --
_headless_streaming = False

def _default_on_event(event):
    """headless 模式下复刻原有 print 行为。"""
    global _headless_streaming
    etype = event.get("type")

    if etype == "token_usage":
        print(f"[DEBUG]:token{event.get('usage')}\n")
    elif etype == "thinking":
        content = event.get("content", "")
        print(f"🧠 Thinking ({len(content)} chars): {content[:300]}{'...' if len(content) > 300 else ''}")
    elif etype == "thinking_chunk":
        pass  # headless: thinking in streaming is bundled with answer
    elif etype == "tool_calls":
        calls = event.get("calls", [])
        print(f"Agent调用工具: {[c['name'] for c in calls]}")
    elif etype == "tool_result":
        print(f"tool result for {event.get('tool_name')}:\n {str(event.get('result', ''))[:300]}\n")
    elif etype == "self_check":
        print(f"Tool result validation failed: {event.get('critique')}\n")
    elif etype == "response_chunk":
        if not _headless_streaming:
            print("🤖: ", end="")
            _headless_streaming = True
        print(event.get("content", ""), end="", flush=True)
    elif etype == "response_done":
        print(f"🤖: {event.get('content', '')}")
    elif etype == "stream_usage":
        print(f"\n流式总量：{event.get('usage', '')}")
        _headless_streaming = False
    elif etype == "status":
        pass

def chat(question, on_event=None):
    global messages

    def _emit(event):
        if on_event:
            on_event(event)
        else:
            _default_on_event(event)

    # 添加用户问题
    messages.append({"role": "user", "content": question})
    
    MAX_TOOL_ROUNDS = 6
    tool_round = 0

    # 循环处理工具调用
    while tool_round < MAX_TOOL_ROUNDS:
        tool_round += 1
        # 调用 API（不使用流式）
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            stream=False,
            tools=tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}}
        )
        
        _emit({"type": "token_usage", "usage": response.usage})

        response_msg = response.choices[0].message
        use_tool_calls = response_msg.tool_calls

        # 提取 reasoning/thinking 内容
        reasoning = getattr(response_msg, 'reasoning_content', None)
        if reasoning:
            _emit({"type": "thinking", "content": reasoning, "round": tool_round})
        
        # 如果没有工具调用，跳出循环
        if not use_tool_calls:
            content = response_msg.content or ""
            if _is_garbage_content(content):
                messages.append({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"})
            else:
                messages.append({"role": "assistant", "content": content})
            break
        
        # 有工具调用
        _emit({"type": "tool_calls", "calls": [{"name": tc.function.name, "args": tc.function.arguments} for tc in use_tool_calls]})
        
        # 添加助手的工具调用消息（plain dict，避免 SDK 对象无法 JSON 序列化）
        # 垃圾检测：content 可能夹带 DSML
        safe_content = response_msg.content or ""
        if _is_garbage_content(safe_content):
            safe_content = ""
        messages.append({
            "role": response_msg.role,
            "content": safe_content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                }
                for tc in response_msg.tool_calls
            ]
        })
        
        all_tools_make_sense = True
        tool_results = []
        # 执行所有工具
        for tool in use_tool_calls:
            tool_name = tool.function.name
            try:
                tool_args = json.loads(tool.function.arguments)
            except json.JSONDecodeError as e:
                tool_result = f"[Error] Invalid JSON arguments: {e}\nRaw: {tool.function.arguments}"
                all_tools_make_sense = False
            else:
                try:
                    tool_fn = TOOL_CALL_MAP.get(tool_name)
                    if tool_fn is None:
                        available = ", ".join(TOOL_CALL_MAP.keys())
                        tool_result = f"[Error] Unknown tool: '{tool_name}'. Available tools: {available}"
                        all_tools_make_sense = False
                    else:
                        tool_result = tool_fn(**tool_args)
                except Exception as e:
                    tool_result = f"[Error] Tool execution failed: {e}"
                    all_tools_make_sense = False

            if tool_result is None:
                tool_result = "[Error] Tool returned None (timeout or empty response)"

            _emit({"type": "tool_result", "tool_name": tool_name, "result": str(tool_result), "call_id": tool.id})
            messages.append({
                "role": "tool",
                "tool_call_id": tool.id,
                "content": str(tool_result),
            })

            tool_results.append(
                {
                    "tool_name": tool_name,
                    "tool_result": tool_result,
                    "tool_call_id": tool.id
                }
            )

            is_valid, critique = validate_tool_result(tool_name, tool_result)
            if not is_valid:
                _emit({"type": "self_check", "tool_name": tool_name, "critique": critique})
                messages.append(
                    {
                        "role": "user",
                        "content": f"[Self-check] Tool '{tool_name}' result is questionable:\n{critique}\n\nPlease critically evaluate this result and retry the tool if needed. If the result is actually usable, explain why and proceed."
                    }
                )
        # 继续循环，让助手处理工具结果
    
    # 所有工具调用完成后，流式输出最终答案
    # 注意：最后一条消息已经是助手的响应了，但我们想要流式输出，所以重新请求一次
    # 移除最后一条助手消息，用流式重新生成
    last_msg = messages[-1]
    if last_msg.get("role") == "assistant" and last_msg.get("content"):
        # 已有完整回答，直接输出
        _emit({"type": "response_done", "content": last_msg["content"]})
        return
    # 用流式输出最终答案
    response_stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        stream=True,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
        stream_options={"include_usage": True}
    )
    
    full_response = ""
    _emit({"type": "status", "state": "generating"})
    for chunk in response_stream:
        delta = chunk.choices[0].delta
        if getattr(delta, 'reasoning_content', None):
            _emit({"type": "thinking_chunk", "content": delta.reasoning_content, "round": tool_round})
        if delta.content:
            _emit({"type": "response_chunk", "content": delta.content})
            full_response += delta.content
        if chunk.usage is not None:
            _emit({"type": "stream_usage", "usage": chunk.usage})

    # 保存流式生成的助手回复（带垃圾检测）
    if _is_garbage_content(full_response):
        messages.append({"role": "assistant", "content": "[Response filtered: detected garbled output. Please rephrase your request.]"})
    else:
        messages.append({"role": "assistant", "content": full_response})
        
# 交互式连续对话
if __name__ == "__main__":
    print(" 多轮对话🤣（输入 'exit' 退出）\n")
    while True:
        question = input("😎 You: ")
        if question.lower() == 'exit':
            break
        chat(question)