"""后端配置：模型/base_url/client/WORK_DIR 锚点。"""
import os
from openai import OpenAI

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # agent/（包上一层，即项目根）
_PROFILES_DIR = os.path.join(WORK_DIR, "profiles")  # 浏览器登录态 profile 持久化目录
_TAVILY_API_KEY = os.environ.get("TAVILY_KEY", "")

def _is_local_backend():
    return os.environ.get("MODEL_BACKEND", "").startswith("local")


def _backend_config():
    if os.environ.get("MODEL_BACKEND") == "local-3.5":
        return {
            "api_key": "sk_local",
            "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "qwen-3.5-9B"),
            "extra_kwargs": {},
            "max_tool_rounds": 12,
            "ctx": 16384
        }
    elif os.environ.get("MODEL_BACKEND") == "local-3.6":
        return {
            "api_key": "sk_local",
            "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
            "model": os.environ.get("LLM_MODEL", "qwen-3.6-35B"),
            "extra_kwargs": {},
            "max_tool_rounds": 12,
            "ctx": 8192
        }
    else:
        return {
            "api_key": os.environ.get("DS_KEY"),
            "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            "model": "deepseek-v4-flash",
            "extra_kwargs": {
                "reasoning_effort": "high",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            "max_tool_rounds": 12,
            "ctx": 65536
        }


cfg = _backend_config()
_current_raw = os.environ.get("MODEL_BACKEND", "<unset>")
print(f"[backend] MODEL_BACKEND={_current_raw!r} → model={cfg['model']} base_url={cfg['base_url']}", flush=True)
CURRENT_MODEL = cfg["model"]
client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
