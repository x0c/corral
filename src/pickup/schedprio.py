"""提升交互界面在系统高负载下的调度优先级。

macOS 上未标注 QoS 的线程默认为 Default：系统忙时会被标成 User Interactive
的 App（浏览器、IDE）抢光 CPU，表现为 pickup「自己并不重却卡成 ppt」。
这里把 TUI 主线程抬到 User Interactive，把喂画面 / 喂通道的后台线程抬到
User Initiated。Linux / Windows 做同语义的尽力而为（nice / 进程优先级类）。

任何平台调用失败都静默忽略——优先级是锦上添花，绝不能挡启动。
"""
from __future__ import annotations

import os
import sys

# Apple sys/qos.h
_QOS_USER_INTERACTIVE = 0x21
_QOS_USER_INITIATED = 0x19

# Windows KERNEL32
_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


def boost_interactive() -> None:
    """TUI 主线程：用户正在看着、卡了就是界面冻住。"""
    if sys.platform == "darwin":
        _darwin_set_thread_qos(_QOS_USER_INTERACTIVE)
    elif sys.platform == "win32":
        _windows_set_process_above_normal()
    _best_effort_nice(-5)


def boost_ui_worker() -> None:
    """喂界面的后台线程（抓帧、控制通道读）：用户在等回显，但次于主线程。"""
    if sys.platform == "darwin":
        _darwin_set_thread_qos(_QOS_USER_INITIATED)
    # Windows / Linux：进程级优先级已在 boost_interactive 里抬过；线程级无廉价 API。


def _darwin_set_thread_qos(qos_class: int) -> None:
    try:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint32, ctypes.c_int]
        lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
        # relative_priority：0 是该类内最高；负值才降低。argtypes 会把 int 转成 c_uint32。
        lib.pthread_set_qos_class_self_np(qos_class, 0)
    except Exception:
        return


def _windows_set_process_above_normal() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, _ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception:
        return


def _best_effort_nice(delta: int) -> None:
    """Unix nice：无特权时常失败，忽略即可。"""
    if not hasattr(os, "nice"):
        return
    try:
        os.nice(delta)
    except (OSError, PermissionError):
        return
