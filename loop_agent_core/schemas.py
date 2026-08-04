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
    }
]
