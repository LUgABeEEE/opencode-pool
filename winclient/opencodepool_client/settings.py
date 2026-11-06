"""客户端自己的设置（不是中转的配置）。

这份文件只放"界面怎么表现"，任何影响中转行为的参数都在 `pool.config.json` 里 —
— 两边混在一起会让"设置页保存"变成一次隐式重启，排障时说不清是谁改了什么。
"""
from __future__ import annotations

import json
import os
import tempfile

from . import paths

DEFAULTS = {
    "project_dir": "",
    "config_path": "",
    "python": "",
    "auto_start_service": True,
    "close_to_tray": True,
    "minimize_to_tray": False,
    "start_service_on_launch": True,
    "stop_service_on_exit": True,
    "refresh_seconds": 3,
    "geometry": "1240x800",
    "last_page": "dashboard",
    "autostart_windows": False,
    "confirm_destructive": True,
    "request_rows": 200,
}


class Settings:
    def __init__(self, data: dict | None = None):
        self._data = dict(DEFAULTS)
        if data:
            for key, value in data.items():
                if key in DEFAULTS and value is not None:
                    self._data[key] = value

    # —— 读写 ——
    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(paths.client_settings_path(), encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        return cls(raw)

    def save(self) -> None:
        target = paths.client_settings_path()
        directory = os.path.dirname(target)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".client-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # —— 取值 ——
    def __getitem__(self, key: str):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self._data[key] = value

    def update(self, **values) -> None:
        for key, value in values.items():
            if key in DEFAULTS:
                self._data[key] = value

    def as_dict(self) -> dict:
        return dict(self._data)

    # —— 派生路径 ——
    def project_dir(self) -> str:
        explicit = paths.expand(self["project_dir"])
        if explicit and os.path.isdir(explicit):
            return explicit
        return paths.guess_project_dir()

    def config_path(self) -> str:
        explicit = paths.expand(self["config_path"])
        return explicit or paths.default_config_path()

    def python(self) -> str:
        return paths.find_python(self["python"])

    def relay_log(self) -> str:
        return paths.relay_log_path(self.config_path())

    def refresh_seconds(self) -> int:
        try:
            return max(1, min(60, int(self["refresh_seconds"])))
        except (TypeError, ValueError):
            return 3

    def request_rows(self) -> int:
        try:
            return max(20, min(2000, int(self["request_rows"])))
        except (TypeError, ValueError):
            return 200
