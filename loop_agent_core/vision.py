"""本地视觉模型接口：analyze_image 工具（Qwen VLM via llama.cpp，OpenAI 兼容接口）。"""
import base64
import os

from openai import OpenAI

from .config import WORK_DIR
from . import confirm

_MIME = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "bmp": "bmp"}
DEFAULT_Q = "描述这张图片的内容。"


def _vision_cfg():
    """视觉后端配置。切换：VISION_BACKEND=local|luna（默认 local）。
    luna 模式默认走 ofox 网关，key 由 VISION_API_KEY 提供（setx 设一次即持久）。"""
    if os.environ.get("VISION_BACKEND") == "luna":
        key = os.environ.get("VISION_API_KEY", "")
        if not key:
            raise RuntimeError("VISION_BACKEND=luna 但 VISION_API_KEY 未设置。"
                               "先执行一次: setx VISION_API_KEY sk-of-...")
        return {
            "base_url": os.environ.get("VISION_BASE_URL", "https://api.ofox.io/v1"),
            "model": os.environ.get("VISION_MODEL", "openai/gpt-5.6-luna"),
            "api_key": key,
            "max_tokens": int(os.environ.get("VISION_MAX_TOKENS", "1024")),
        }
    return {
        "base_url": os.environ.get("VISION_BASE_URL", "http://127.0.0.1:8080/v1"),
        "model": os.environ.get("VISION_MODEL", "Qwen3.5-9B"),
        "max_tokens": int(os.environ.get("VISION_MAX_TOKENS", "1024")),
    }


def _resolve_image_path(path):
    # 模型常写 Windows 惯例路径 C:\Users\%USERNAME%\...，需展开环境变量
    path = os.path.expandvars(path)
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(WORK_DIR, path))


def analyze_image_mock(path, question=None):
    """读本地图片 → 本地视觉模型 → 返回文字分析。确认门把关（TUI 弹窗）。

    图片路径不走 _resolve_path 沙箱：图片通常在 WORK_DIR 外（截图/照片），
    由确认门而非路径白名单做安全边界。
    """
    resolved = _resolve_image_path(path)
    if not os.path.exists(resolved):
        return f"[Error] Image not found: {resolved}"
    if not confirm.request("analyze_image", resolved):
        return "[Denied by user]"
    ext = os.path.splitext(resolved)[1].lstrip(".").lower() or "png"
    mime = _MIME.get(ext, ext)
    try:
        with open(resolved, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        return f"[Error] Cannot read image: {e}"
    cfg = _vision_cfg()
    try:
        resp = OpenAI(api_key=cfg.get("api_key", "sk_local"), base_url=cfg["base_url"]).chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                {"type": "text", "text": question or DEFAULT_Q},
            ]}],
            max_tokens=cfg["max_tokens"],
        )
    except Exception as e:
        return (f"[Error] Vision request failed: {e}. "
                "Confirm the vision server is running (start_local_llm.bat, model 3.5).")
    content = resp.choices[0].message.content
    reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
    return content or reasoning or "[Model returned no content]"
