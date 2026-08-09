"""中断控制：共享取消事件。TUI 按 Esc 时 set，runner/shell 在检查点查询。"""
import threading

interrupt_event = threading.Event()
