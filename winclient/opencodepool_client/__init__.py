"""opencode-pool 的 Windows 桌面客户端。

外壳只做四件事，内核（`python -m opencode_pool serve`）一行不改：
  1. 托管内核进程：启停、日志、健康探测；
  2. 把池子状态变成能一眼看懂的东西：三档额度环、冷却倒计时、池内最坏点；
  3. 把中转接进别的客户端，并能一键还原；
  4. 常驻托盘、单实例、开机自启。
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
