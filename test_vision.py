"""One-shot vision test for the local llama.cpp server.

Usage:
    python test_vision.py 图片.png ["你想问什么"]

Defaults to asking the model to describe the image.
Requires start_local_llm.bat to be running first.
"""
import sys
import os
import base64

from openai import OpenAI

BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
MODEL = os.environ.get("LLM_MODEL", "Qwen3.5-9B")
DEFAULT_Q = "描述这张图片的内容。"

_MIME = {
    "jpg": "jpeg", "jpeg": "jpeg", "png": "png",
    "webp": "webp", "bmp": "bmp",
}


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    img_path = sys.argv[1]
    question = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_Q

    if not os.path.exists(img_path):
        print(f"[Error] 图片不存在: {img_path}")
        sys.exit(1)

    ext = os.path.splitext(img_path)[1].lstrip(".").lower() or "png"
    mime = _MIME.get(ext, ext)

    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    client = OpenAI(api_key="sk-local", base_url=BASE_URL)

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                    {"type": "text", "text": question},
                ],
            }],
            max_tokens=2048,
        )
    except Exception as e:
        print(f"[Error] 请求失败：{e}")
        print("确认已运行 start_local_llm.bat 且服务在 127.0.0.1:8080")
        sys.exit(1)
    content = resp.choices[0].message.content
    reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
    if content:
        print(content)
    elif reasoning:
        print(reasoning)
    else:
        print("[模型未返回任何内容]")


if __name__ == "__main__":
    main()
