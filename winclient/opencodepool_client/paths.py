"""路径解析：项目根 / 客户端数据目录 / 中转配置与日志。

客户端有两种运行形态：
  * 源码运行（`python -m opencodepool_client`）：`__file__` 就在仓库里，
    项目根 = 本包的上一级目录。
  * PyInstaller 打包（`OpenCodePoolClient.exe`）：`sys.frozen` 为真，`__file__`
    落在 `%TEMP%\\_MEIxxxx`，靠它找项目根会指到临时目录。此时只用用户设置里
    记的 `project_dir`，找不到再让用户在设置页手动指。

所有函数都不抛异常：路径猜错只会让界面显示"未找到"，不会让客户端起不来。
"""
from __future__ import annotations

import os
import sys

APP_NAME = "opencode-pool-client"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """客户端自己的数据目录：`%APPDATA%\\opencode-pool-client`。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def client_settings_path() -> str:
    return os.path.join(app_dir(), "client.json")


def client_icon_path() -> str:
    return os.path.join(app_dir(), "app.ico")


def client_log_path() -> str:
    return os.path.join(app_dir(), "client.log")


def home() -> str:
    return os.path.expanduser("~")


def expand(path: str) -> str:
    """展开 `~` 与环境变量；空值返回空串（调用方自己决定默认值）。"""
    if not path:
        return ""
    try:
        return os.path.expandvars(os.path.expanduser(str(path)))
    except (KeyError, ValueError):
        return str(path)


def _looks_like_project(path: str) -> bool:
    if not path:
        return False
    entry = os.path.join(path, "opencode_pool", "__main__.py")
    return os.path.isfile(entry)


def guess_project_dir() -> str:
    """在没有用户设置时的候选项目根：源码位置 → 当前目录 → 逐级上溯。"""
    candidates: list[str] = []
    if not frozen():
        here = os.path.dirname(os.path.abspath(__file__))          # …/winclient/opencodepool_client
        candidates.append(os.path.dirname(here))                    # …/winclient
        candidates.append(os.path.dirname(os.path.dirname(here)))   # …/opencode-pool
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    candidates.append(exe_dir)
    candidates.append(os.getcwd())
    for start in list(candidates):
        probe = start
        for _ in range(4):
            if _looks_like_project(probe):
                return probe
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
    return ""


def default_config_path() -> str:
    """与 `python -m opencode_pool init` 一致：`~/.dsh/opencode-pool/pool.config.json`。"""
    return os.path.join(home(), ".dsh", "opencode-pool", "pool.config.json")


def config_dir(config_path: str) -> str:
    return os.path.dirname(expand(config_path)) or os.path.join(home(), ".dsh", "opencode-pool")


def relay_log_path(config_path: str) -> str:
    """中转进程的控制台输出落这里（与 dsh-plugin 的 relay.log 同名同位置）。"""
    return os.path.join(config_dir(config_path), "relay.log")


def state_path(config_path: str, state_file: str) -> str:
    """`state_file` 在配置里通常是相对名（pool_state.json），相对配置目录解析。"""
    if not state_file:
        return os.path.join(config_dir(config_path), "pool_state.json")
    raw = expand(state_file)
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        return raw
    return os.path.join(config_dir(config_path), raw)


def resolve_relative(config_path: str, value: str, fallback: str) -> str:
    """配置里的相对路径一律按配置目录解析（与 CLI 的 cwd 无关，更符合直觉）。"""
    raw = expand(value)
    if not raw:
        return os.path.join(config_dir(config_path), fallback)
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        return raw
    return os.path.join(config_dir(config_path), raw)


def find_python(preferred: str = "") -> str:
    """给子进程用的解释器：优先用户指定，其次当前解释器。

    `pythonw.exe` 不带控制台，子进程 `print` 会写进 None，中转的日志就丢了；
    所以一律换回同目录的 `python.exe`。
    """
    for candidate in (expand(preferred), sys.executable):
        if not candidate:
            continue
        path = candidate
        base = os.path.basename(path).lower()
        if base.startswith("pythonw") and not frozen():
            sibling = os.path.join(os.path.dirname(path), "python.exe")
            if os.path.isfile(sibling):
                path = sibling
        if os.path.isfile(path):
            return path
    return sys.executable


def python_has_requests(python: str) -> tuple[bool, str]:
    """中转唯一运行时依赖是 requests；提前探一次，别等子进程秒退再说。"""
    import subprocess

    try:
        proc = subprocess.run(
            [python, "-c", "import requests,sys;print(requests.__version__)"],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"exit {proc.returncode}"
    return True, (proc.stdout or "").strip()
