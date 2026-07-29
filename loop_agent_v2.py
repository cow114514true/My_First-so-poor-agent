import os
from openai import OpenAI
import sys
import json
from datetime import datetime 
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
    }
]

def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")

def get_weather_mock(location, date):
    return f"Weather in {location} on {date}: Cloudy 7~13°C"

def exec_shell_win_mock(shell_cmd):
    result = subprocess.run(shell_cmd,shell=True,capture_output=True,text=True)
    return json.dumps({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "success": result.returncode == 0
    },ensure_ascii=False,indent=2)

# log_in upload_files enter_confirm input_prompt get_response
_ds_session = None

def log_in():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(storage_state="./profile.json")
    page = context.new_page()
    page.goto("https://chat.deepseek.com")
    return {
        "page": page,
        "context": context,
        "browser": browser,
        "playwright": playwright
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
    timeout = 120  # 2分钟，DeepSeek 网页版搜索回复通常需要 30-60 秒
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

def is_result_make_sense(tool_name,result):
    if not result:
        return False
    if "error" in str(result).lower() or "fail" in str(result).lower():
        return False
    if len(str(result)) < 10:
        return False
    return True

TOOL_CALL_MAP = {
    "get_date": get_date_mock,
    "get_weather": get_weather_mock,
    "exec_shell_win": exec_shell_win_mock,
    "use_ds_from_web": use_ds_from_web_mock
}


client = OpenAI(
    api_key=os.environ.get("DS_KEY"),
    base_url="https://api.deepseek.com"
)

system_prompt = """You are a helpful assistant. If you need to use tools, just use them! When you have all the information, provide a complete answer;
                If the result is time-sensitive and not universally applicable, then you must call the web search tool(using use_ds_from_web) to verify!It is important!
                Such as weather or other infomation like this"""

messages = [
    {"role": "system", "content": system_prompt}
]

def chat(question):
    global messages
    
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
        
        response_msg = response.choices[0].message
        use_tool_calls = response_msg.tool_calls
        
        # 如果没有工具调用，跳出循环
        if not use_tool_calls:
            # 保存助手响应（非工具调用）
            messages.append({"role": "assistant", "content": response_msg.content})
            break
        
        # 有工具调用
        print(f"Agent调用工具: {response_msg.tool_calls}")
        
        # 添加助手的工具调用消息
        messages.append(response_msg)
        
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
                        tool_result = f"[Error] Unknown tool: {tool_name}"
                        all_tools_make_sense = False
                    else:
                        tool_result = tool_fn(**tool_args)
                except Exception as e:
                    tool_result = f"[Error] Tool execution failed: {e}"
                    all_tools_make_sense = False

            if tool_result is None:
                tool_result = "[Error] Tool returned None (timeout or empty response)"

            print(f"tool result for {tool_name}:\n {tool_result}\n")
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

            if not is_result_make_sense(tool_name, tool_result):
                print("Need to recall tool\n")
                messages.append(
                    {
                        "role": "user",
                        "content": f"Tool {tool_name}' result have some problems, please retry and solve it"
                    }
                )
        # 继续循环，让助手处理工具结果
    
    # 所有工具调用完成后，流式输出最终答案
    # 注意：最后一条消息已经是助手的响应了，但我们想要流式输出，所以重新请求一次
    # 移除最后一条助手消息，用流式重新生成
    last_msg = messages[-1]
    if last_msg.get("role") == "assistant" and last_msg.get("content"):
        # 已有完整回答，直接输出
        print(f": {last_msg['content']}")
        return
    # 用流式输出最终答案
    response_stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        stream=True,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}}
    )
    
    full_response = ""
    print("🤖: ", end="")
    for chunk in response_stream:
        if chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            print(content, end="", flush=True)
            full_response += content
    print("\n")
    
    # 保存流式生成的助手回复
    messages.append({"role": "assistant", "content": full_response})
        
# 交互式连续对话
if __name__ == "__main__":
    print(" 多轮对话🤣（输入 'exit' 退出）\n")
    while True:
        question = input("😎 You: ")
        if question.lower() == 'exit':
            break
        chat(question)