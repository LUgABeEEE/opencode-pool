"""开机自启：写当前用户的 Run 键（不需要管理员）。

源码运行时注册的是 `run_client.py` 的绝对路径；打包后注册 exe 自身。
一律带 `--minimized`，开机时不弹窗，只落托盘。
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "OpenCodePoolClient"


def _command(entry_script: str = "", extra: str = "--minimized") -> str:
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        return f'"{exe}" {extra}'.strip()
    python = sys.executable
    base = os.path.basename(python).lower()
    if base.startswith("python"):
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(pythonw):
            python = pythonw
    script = entry_script or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                          "run_client.py")
    return f'"{python}" "{script}" {extra}'.strip()


def current_command() -> str:
    """读注册表里现有的值（没有则空串）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (ImportError, FileNotFoundError, OSError):
        return ""


def enabled() -> bool:
    return bool(current_command())


def enable(entry_script: str = "") -> tuple[bool, str]:
    command = _command(entry_script)
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        return True, command
    except (ImportError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def disable() -> tuple[bool, str]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True, ""
    except FileNotFoundError:
        return True, ""
    except (ImportError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def launch_command_preview() -> str:
    return _command()


def powershell_startup_target() -> str:
    """给"我想自己用任务计划程序"的用户一段可直接粘的命令。"""
    command = _command()
    return (f'schtasks /Create /TN "OpenCodePoolClient" /TR "{command}" '
            f'/SC ONLOGON /RL LIMITED /F')


__all__ = ["enabled", "enable", "disable", "current_command",
           "launch_command_preview", "powershell_startup_target"]
