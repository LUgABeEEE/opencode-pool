"""单实例：双击两次不该出现两个内核抢 8790。

做法是 Windows 上最省事也最可靠的一对：
  1. 命名互斥量（`Local\\...`）判断"是不是已经有一个我在跑"；
  2. 若已在跑，用 `FindWindowW(标题)` 找到那个窗口，恢复并抢到前台，
     让用户的第二次双击变成"把刚才那个叫回来"。

不用本地 socket / 文件锁：互斥量在进程崩溃时由内核回收，没有残留；
而 socket 探测要额外定义协议，还得处理端口被别的东西占用。

非 Windows（或 API 不可用）时全部退化为"永远算第一个实例"，
不影响源码在别的平台上跑起来。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

MUTEX_NAME = "Local\\OpenCodePoolClient.SingleInstance"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
SW_SHOW = 5
ASFW_ANY = -1


class SingleInstance:
    """拿住一个命名互斥量；`already_running` 为真表示该退出了。"""

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self.already_running = False
        self.available = False
        self._handle = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, True, name)
            self.available = True
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                self.already_running = True
                if handle:
                    kernel32.CloseHandle(handle)
            else:
                self._handle = handle
        except (AttributeError, OSError):
            self.available = False
            self.already_running = False

    def release(self) -> None:
        if self._handle:
            try:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except (AttributeError, OSError):
                pass
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def focus_existing(title: str, tries: int = 12, delay: float = 0.15) -> bool:
    """把标题匹配的已有 tk 窗口叫到前台；找不到返回 False。

    `tries` 次重试是为了覆盖"上一个实例刚抢到互斥量、窗口还没建出来"的
    那个几毫秒的空窗——这时直接判定"没有窗口"就会静默退出，用户看到的是
    "双击没反应"。
    """
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return False
    # 允许本进程抢前台（否则 SetForegroundWindow 可能被系统静默忽略）
    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)
    except (AttributeError, OSError):
        pass
    for _ in range(max(1, tries)):
        try:
            hwnd = user32.FindWindowW(None, title)
        except (AttributeError, OSError):
            return False
        if hwnd:
            try:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                user32.ShowWindow(hwnd, SW_SHOW)
                user32.SetForegroundWindow(hwnd)
                return True
            except (AttributeError, OSError):
                return False
        time.sleep(delay)
    return False
