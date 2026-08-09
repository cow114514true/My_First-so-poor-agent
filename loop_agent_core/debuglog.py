"""极简错误日志：append 到 WORK_DIR/error.log。取代裸 `except: pass` 的静默吞错。

用法：`except Exception as e: log_error("write_file backup", e)`
日志函数自身失败绝不抛（否则吞错的点又变成抛错点）。
"""
import os
import threading

_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "error.log")
_lock = threading.Lock()


def log_error(context, exc):
    try:
        with _lock:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{context}] {type(exc).__name__}: {exc}\n")
    except Exception:
        pass  # 日志失败不抛：吞错是日志工具的兜底，不是问题
