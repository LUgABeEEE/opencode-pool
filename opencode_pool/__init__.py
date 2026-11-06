"""OpenCode 号池中转（opencode-pool）。

分层（与 gpt-load / new-api 学到的分工一致）：
  adapters  上游差异：字段声明、窗口声明、请求头、余量探测、失败判定、用量解析
  config    一次接入（provider）与 N 把凭据（account）的分离，含 v1→v2 迁移
  pool      与上游无关的调度：挑号、冷却、记账、持久化
  server    HTTP：OpenAI 兼容入口 + 管理 API + 控制台
"""
__version__ = "0.1.0"

from .adapters import Adapter, Adapter as BaseAdapter  # noqa: F401  (re-export for extensions)
from .config import Config, Provider  # noqa: F401
from .pool import Account, KeyStore, Pool  # noqa: F401

__all__ = ["__version__", "Adapter", "BaseAdapter", "Config", "Provider", "Pool", "Account", "KeyStore"]
