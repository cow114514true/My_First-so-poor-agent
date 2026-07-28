from playwright.sync_api import sync_playwright
import json
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # 设置 headless=True 可无头运行
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://chat.deepseek.com")
    print(page.title())  # 输出页面标题
    #page.screenshot(path="./screenshot.png")
    input("log in ...")
    page.wait_for_load_state("networkidle")
    state = context.storage_state(path="./profile.json")
    with open("profile.json","w") as f:
        json.dump(state,f)
    browser.close()


