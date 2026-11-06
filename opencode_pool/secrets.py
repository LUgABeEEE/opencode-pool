"""面板可写的密钥仓：一个 0600 的 JSON，放在工作区之外。

为什么不在 `pool.config.json` 或仓库里：配置要能随便看、随便备份，密钥不能跟着进 git。
为什么不用 DSH 的 `.credentials.yaml`：那是 dsh-web 自己的库，面板去改它有互相覆盖的风险；
这里做**只读回退**——DSH 里已有的 key 照样能用，但面板写的一律落本仓，且优先级更高。

文件形态（明文 + 0600，和 DSH 自己存 key 的方式一致；没有主密码可用来加密）：
    {"version": 1, "secrets": {"OPENCODE_API_KEY_03": {"value": "…", "updated_at": 1793…}}}
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time

DEFAULT_PATH = "~/.dsh/opencode-pool/secrets.json"


def fingerprint(value: str) -> str:
    """给密钥一个不可逆的短指纹，让面板能区分"换没换"而不暴露内容。"""
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


class SecretStore:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path or DEFAULT_PATH)
        self._data: dict[str, dict] | None = None
        # 同一进程里多个线程/多个实例读写同一份文件：读写都要串行化
        self._lock = threading.RLock()

    # —— 磁盘 ——
    def _load(self) -> dict[str, dict]:
        """每次都从磁盘读：文件很小，读一次比"缓存失效策略"更可靠。

        L3 的根因就是缓存：一次读失败会把空结果留在内存里，之后 key 明明
        已落盘也一直返回 None（面板换 key 静默不生效、readiness 误报缺凭据）。
        mtime 也不是可靠失效信号 —— 文件系统时间戳粒度可达毫秒级，紧挨着的
        外部写入可能拿到相同 mtime。所以这里不做缓存，读失败只影响当次调用。
        """
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f)
                secrets = raw.get("secrets") or {}
                self._data = {k: dict(v) for k, v in secrets.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._data = {}
            return self._data

    def _save(self, data: dict[str, dict]) -> None:
        """把给定的完整快照写盘；写完把内存态对齐成"刚写下的内容"。"""
        payload = {"version": 1, "secrets": {k: dict(v) for k, v in data.items()}}
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # 目录收敛到 0700：密钥仓不该被同机其他人列出来
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            with self._lock:
                self._data = payload["secrets"]
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # —— 读写 ——
    def get(self, name: str) -> str | None:
        entry = self._load().get(name)
        return (entry or {}).get("value") or None

    def set(self, name: str, value: str) -> None:
        if not name or not value:
            raise ValueError("密钥名与值都不能为空")
        with self._lock:
            data = dict(self._load())
            data[name] = {"value": value.strip(), "updated_at": time.time()}
            self._save(data)

    def delete(self, name: str) -> bool:
        with self._lock:
            data = dict(self._load())
            if name in data:
                del data[name]
                self._save(data)
                return True
            return False

    def describe(self, name: str) -> dict:
        with self._lock:
            entry = dict((self._load().get(name) or {}))
        value = entry.get("value") or ""
        return {
            "configured": bool(value),
            "source": "pool" if value else "",
            "fingerprint": fingerprint(value),
            "updated_at": entry.get("updated_at"),
        }
