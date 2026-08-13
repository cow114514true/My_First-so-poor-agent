"""OpenAI function schemas（tools 列表，9 个工具定义）。"""
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
            "description": "Execute a command in Windows. shell='cmd' (default) runs cmd.exe; shell='powershell' runs PowerShell with UTF-8 output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shell_cmd": {"type": "string",
                                "description": "The command to execute; syntax must match the chosen shell"},
                    "shell": {"type": "string", "enum": ["cmd", "powershell"],
                              "description": "Which shell to run in. Use 'powershell' for PowerShell-native tasks (registry, services, Get-* cmdlets); leave 'cmd' (default) otherwise."}
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
            "description": "Read a file. Small files return full content. Large files return a structure outline (defs/classes/imports with line numbers) instead — then fetch what you need with function= or start_line/end_line. Path is resolved relative to the agent's working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read (relative or absolute within working directory)"},
                    "function": {"type": "string", "description": "Name of a function/class to pull its full body (size-capped by the token budget)"},
                    "start_line": {"type": "integer", "description": "First line of a range to read, 1-based (size-capped by the token budget)"},
                    "end_line": {"type": "integer", "description": "Last line of a range to read, 1-based (size-capped by the token budget)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "count_tokens",
            "description": "Estimate the token count of a file (path=) or arbitrary text (text=). Provide exactly one. Use to gauge context/cost before reading large files or sending large content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to count (relative or absolute within working directory)"},
                    "text": {"type": "string", "description": "Arbitrary text string to count"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "⚠ OVERWRITES THE WHOLE FILE. Use to create new files or replace entire files. To change part of an existing file, prefer edit_file.",
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
            "name": "edit_file",
            "description": "Precisely edit a file: replace old_string with new_string. By default old_string must be unique (fails without writing if missing or not unique); pass replace_all=True to replace every occurrence (e.g. renaming a repeated word). Safer than write_file for modifying existing files. Empty new_string deletes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit (relative or absolute within working directory)"},
                    "old_string": {"type": "string", "description": "Exact existing text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text. Empty string deletes old_string."},
                    "replace_all": {"type": "boolean", "description": "False (default): old_string must be unique. True: replace every occurrence."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Read a local image file with the vision model (screenshots, diagrams, photos, UI captures) and get a text description or answer to your question. Requires user confirmation. Use when you need to see what's in an image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the image file (absolute, or relative to the working directory)"},
                    "question": {"type": "string", "description": "What to ask about the image. Omit for a general description."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Run a self-contained sub-task as an isolated sub-agent with its own fresh context window (shares the working directory and all other tools). Use when a sub-task is independent and doing it inline would bloat this conversation — e.g. a long multi-file coding job, an independent research chunk, a big write_file batch. Put EVERYTHING the worker needs in 'task' (file paths, requirements, constraints, expected deliverable) because it starts from a fresh context. The worker runs autonomously and returns its final answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Complete, self-contained instructions for the sub-agent, including any file paths, constraints and the expected deliverable."}
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "查询跨会话记忆。不输入 topic 则返回全部记忆索引（每条一行：topic + 日期 + 首行提示），便于挑选后再查询详情；输入关键词或主题则返回匹配条目的完整内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "要查询的主题或关键词。留空则返回全部记忆索引。"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "记住一条跨会话事实。同 topic 自动覆盖更新。系统级（system）用于用户偏好/行为准则，项目级（project，默认）用于本代码库事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "记忆主题，作为唯一标识用于去重覆盖"
                    },
                    "content": {
                        "type": "string",
                        "description": "要记住的完整事实，自包含、不依赖上下文即可读懂"
                    },
                    "level": {
                        "type": "string",
                        "enum": ["system", "project"],
                        "description": "分级：system=用户偏好/行为准则，project(默认)=本代码库事实"
                    }
                },
            "required": ["topic", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_act",
            "description": "Operate a real browser, ONE step per call. The session persists between calls (and across agent restarts for a named profile: cookies, localStorage, sessionStorage and last URL are restored). Each call performs a single action, then returns the page URL, title, a numbered list of interactive elements (each with a css= selector), and page text. Study the element list and drive the next step by number, or target any element with css=<selector> / xpath=<expression>. Actions: open(url=), click(target=), type(target=, text=), press_enter(), press_key(text='Space'/'Escape'/...), hover(target=), select_option(target=, text=), scroll(text='down'/'up'), wait(text=seconds), back(), screenshot(), close(). Pass locate=<keyword> to highlight matching elements at the top of the returned list — use it when you know what you're looking for (e.g. locate='投币' for a coin button). For video pause use press_key(text='Space'). For quick web searches or just reading a page, prefer search_web or use_ds_from_web; reserve browser_act for sites needing real interaction (login, forms, JS).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open", "click", "type", "press_enter", "press_key", "hover", "select_option", "scroll", "wait", "back", "screenshot", "close"]},
                    "url": {"type": "string", "description": "For open: the http(s) URL."},
                    "target": {"type": ["integer", "string"], "description": "For click/type/hover/select_option: element number from the last element list (1-based), or css=<selector> / xpath=<expression> to target any element."},
                    "text": {"type": "string", "description": "For type: text to fill. For select_option: the option value/label. For press_key: key name e.g. 'Space'/'Escape'/'Enter'. For scroll: 'down' or 'up'. For wait: seconds (max 10)."},
                    "locate": {"type": "string", "description": "Optional. Keyword to highlight in the returned element list. Matching elements are listed at the top marked with '★'. Use to find a specific control (e.g. locate='投币') without scanning the whole list."},
                    "profile": {"type": "string", "description": "Optional profile name for saved logins + session restore (e.g. 'github'). Omit for a clean session."},
                    "headless": {"type": "boolean", "description": "true = no window (default). false = show the browser window — use false when you need the user to log in or solve a captcha."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web (Tavily). Returns a list of result titles, URLs and content snippets. Use for fact lookup, latest information, docs, news — much faster than opening a browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {"type": "integer", "description": "Max results to return (default 5)."},
                    "search_depth": {"type": "string", "enum": ["basic", "advanced"], "description": "Basic = faster & cheaper (default). Advanced = deeper crawl."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Read a specific web page's cleaned text content (Tavily extract). Use when you have a concrete URL and want its content without opening a full browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to read."},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "visual_locate",
            "description": "当 browser_act 返回的元素清单描述不清（图标、图片按钮、'<el> ?' 等无文本元素）时用：对当前浏览器页面截图并给可交互元素画上编号框，让本地视觉模型定位目标元素，返回可点击的编号。拿到编号后再用 browser_act(click, target=编号) 执行动作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "要定位的元素描述，如 '搜索框'、'登录按钮'、'关闭弹窗的 X'"},
                    "profile": {"type": "string", "description": "可选：浏览器登录态名，与 browser_act 的 profile 一致"},
                    "headless": {"type": "boolean", "description": "可选：true = 无头（默认）。false = 显示窗口。"}
                },
                "required": ["target"]
            }
        }
    }
]

# ponytail: append-only — append new schemas here rather than inserting mid-list
# (avoids merge-conflict churn on every addition).

# search_code
tools.append({
    "type": "function",
    "function": {
        "name": "search_code",
        "description": "Search code with a regex pattern. Returns matching lines with line numbers, file paths, or match counts. Much faster than running cmd/findstr and avoids encoding issues. Use whenever you need to find where a function/variable/pattern is used.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex pattern to match against each line"},
                "path": {"type": "string", "description": "Directory or file to search. Default: '.' (project root). Relative paths resolve against the project root."},
                "glob": {"type": "string", "description": "File pattern filter, e.g. '*.py' (default), '*.{py,js}', '*.md'"},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                                "description": "'content' = matching lines with line numbers; 'files_with_matches' = file paths only; 'count' = match counts per file. Default: 'content'."},
            },
            "required": ["pattern"]
        }
    }
})

# code_index
tools.append({
    "type": "function",
    "function": {
        "name": "code_index",
        "description": "Get a project-level structural index of all code files under a directory. Shows each file's functions, classes, and imports with line numbers. Use this FIRST when exploring an unfamiliar project or directory — gives you the full map in one call, then use search_code for cross-file references and read_file(function=...) to pull specific implementations.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to index. Default: '.' (project root)."},
                "glob": {"type": "string", "description": "File pattern filter, e.g. '*.py' (default), '*.{py,js}'. Use '*.py' for Python projects."},
            },
            "required": []
        }
    }
})
