# -*- coding: utf-8 -*-
"""QQ 私聊(C2C)机器人 -> 远程调用 Claude Code / 本机 loop_agent。

架构: 你给机器人发私聊消息 -> QQ平台(WebSocket长连接) -> 本脚本 -> 后端 -> 回复回QQ
不需要公网服务器/内网穿透，机器人无需进群。

环境变量:
    QQ_APPID        机器人 AppID (必填)
    QQ_SECRET       机器人 AppSecret (必填)
    QQ_ALLOW_USERS  私聊触发者白名单(逗号分隔 user_openid)。
                    QQ_BACKEND=local 时必须设置(本地 agent 能执行 shell 命令);
                    否则为空=允许任何人触发
    QQ_BACKEND      后端: claude(默认, claude -p) | local(本机 loop_agent)
    CLAUDE_EXE      可选, claude 可执行文件路径
"""
import asyncio
import os
import shutil
import subprocess
import sys

import aiohttp
import botpy
from botpy.message import C2CMessage

IMAGE_EXTS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/bmp": "bmp"}

# npm 装的 claude 是 claude.CMD 垫片，裸名 subprocess 跑不了，用 shutil.which 解析出完整路径
CLAUDE_EXE = os.environ.get("CLAUDE_EXE") or shutil.which("claude") or "claude"
ALLOW_USERS = {u for u in os.environ.get("QQ_ALLOW_USERS", "").split(",") if u}
QQ_BACKEND = os.environ.get("QQ_BACKEND", "claude").lower()

# local 后端会改全局 messages，串行化防并发；import 放函数里避免 bot 启动就加载重依赖
_local_lock = asyncio.Lock()


def ask_local_agent(text: str) -> str:
    """用本机 loop_agent 回答一条消息。"""
    from loop_agent_v2 import chat

    try:
        return chat(text) or "（没有生成回复）"
    except Exception as exc:  # noqa: BLE001
        return f"（出错了：{exc}）"


async def _answer(text: str) -> str:
    if QQ_BACKEND == "local":
        async with _local_lock:
            return await asyncio.to_thread(ask_local_agent, text)
    return await asyncio.to_thread(ask_claude, text)


def ask_claude(text: str) -> str:
    """用 Claude Code 头less 模式回答一条消息。"""
    prompt = (
        "你是 Claude，一个 AI 助手。请认真回答用户的问题。"
        "不要调用任何工具。\n\n"
        f"用户消息：{text}"
    )
    try:
        proc = subprocess.run(
            [CLAUDE_EXE, "-p", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        out = (proc.stdout or "").strip()
        return out or (proc.stderr or "").strip() or "（没有生成回复）"
    except Exception as exc:  # noqa: BLE001
        return f"（出错了：{exc}）"


class QQBot(botpy.Client):
    async def _download_image(self, message) -> str:
        """下载消息里第一张图片到 tmp/，返回本地路径；无图或下载失败返回空串。"""
        imgs = [a for a in (message.attachments or []) if (a.content_type or "").startswith("image/")]
        if not imgs:
            return ""
        ext = IMAGE_EXTS.get((imgs[0].content_type or "").lower(), "png")
        url = imgs[0].url
        token = self.http._token
        await token.check_token()
        headers = {"Authorization": token.get_string(), "X-Union-Appid": token.app_id}
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"qq_{message.id}.{ext}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        # 部分 CDN 图床匿名可拉；带 token 失败时回退匿名重试
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp2:
                            if resp2.status != 200:
                                print(f"[IMG] HTTP {resp2.status}，下载失败")
                                return ""
                            data = await resp2.read()
                    else:
                        data = await resp.read()
        except Exception as exc:  # noqa: BLE001
            print(f"[IMG] 下载异常: {exc}")
            return ""
        with open(path, "wb") as f:
            f.write(data)
        print(f"[IMG] 已保存 {path}")
        return path

    async def on_c2c_message_create(self, message: C2CMessage):
        uid = message.author.user_openid
        text = (message.content or "").strip()
        print(f"[C2C] user_openid={uid} msg={text}")
        if ALLOW_USERS and uid not in ALLOW_USERS:
            return
        img_path = await self._download_image(message)
        if img_path:
            text = f"{text}\n[用户发来一张图片，本地路径：{img_path}。请用 analyze_image 工具查看图片内容并回答。]".strip()
        if not text:
            return
        reply = await _answer(text)
        await message.reply(content=reply[:1800])


async def _amain(appid: str, secret: str) -> None:
    client = QQBot(intents=botpy.Intents(public_messages=True))
    await client.start(appid=appid, secret=secret)


def main() -> None:
    appid = os.environ.get("QQ_APPID", "")
    secret = os.environ.get("QQ_SECRET", "")
    if not appid or not secret:
        sys.exit("缺少 QQ_APPID / QQ_SECRET 环境变量，请在 run_qq_bot.bat 里配置")

    if not ALLOW_USERS:
        if QQ_BACKEND == "local":
            sys.exit("QQ_BACKEND=local 时必须设置 QQ_ALLOW_USERS（本地 agent 能执行 shell 命令，不能开放给所有人）")
        print("[WARN] QQ_ALLOW_USERS 为空: 任何人私聊机器人都能触发 Claude Code，建议设置白名单")

    asyncio.run(_amain(appid, secret))


if __name__ == "__main__":
    main()
