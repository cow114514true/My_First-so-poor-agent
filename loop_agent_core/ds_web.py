"""DeepSeek 网页自动化：登录/上传/输入/发送/取回复（Playwright 单例共享）。"""
import os
import sys
import time
from playwright.sync_api import sync_playwright

from .config import _PROFILES_DIR

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
    profile_path = os.path.join(_PROFILES_DIR, "profile.json")
    context = browser.new_context(storage_state=profile_path) if os.path.exists(profile_path) else browser.new_context()
    page = context.new_page()
    page.goto("https://chat.deepseek.com")
    return {
        "page": page,
        "context": context,
        "browser": browser,
        "playwright": pw
    }


def upload_files(session, file_path):
    page = session["page"]
    if file_path == "" or file_path.strip() == "":
        sys.stderr.write("Path is empty\n")
        return None
    else:
        page.set_input_files("input[type='file']", file_path)
        page.wait_for_timeout(2000)
        return "Upload success"


def input_prompt(session, ask_prompt):
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


def use_ds_from_web_mock(file_path, ask_prompt):
    global _ds_session
    if _ds_session is None:
        _ds_session = log_in()
    if file_path and file_path.strip():
        upload_files(_ds_session, file_path)
    input_prompt(_ds_session, ask_prompt)
    enter_confirm(_ds_session)
    response = get_response(_ds_session)

    return response
