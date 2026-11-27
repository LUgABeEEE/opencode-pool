"""双击启动入口（给 .cmd 快捷方式 / 开机自启用）。

和 `python -m opencodepool_client` 等价，只是多了"把本目录塞进 sys.path"，
这样从任何工作目录双击都能跑起来，不依赖 cwd。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from opencodepool_client.app import main  # noqa: E402  (path 先就位)

if __name__ == "__main__":
    sys.exit(main())
