"""结构化请求日志：`requests.jsonl`（一行一个 JSON，append-only）。

给人看的散文留给 `relay.log`；这里只写排障/分析要用的字段，且**不写**任何
prompt / response 正文与 Authorization。字段缺失一律 `null`（不要省略键），
下游 `jq` / `pandas` / `scripts/stats.py` 可以直接吃。

轮转：文件超过 `max_bytes` 就切成 `.1` / `.2` / …，只保留 `keep` 份。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime

OUTCOMES = ("ok", "failover", "throttled", "client_error", "passthrough", "no_account")

TOKEN_KEYS = ("input", "cache_read", "output", "reasoning",
              "cache_write_5m", "cache_write_1h")


def new_req_id() -> str:
    """8 位随机十六进制：**不派生自请求内容**（隐私，也无法反推 prompt）。"""
    return secrets.token_hex(4)


def iso_now() -> str:
    """本地时区 + 毫秒的 ISO8601，例如 2026-11-06T17:40:12.345+08:00。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _number(value):
    """数字才收；bool / None / 非数字一律 None（不要用 0 顶替缺失）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def token_fields(usage: dict | None) -> dict:
    """从适配器 `extract_usage` 的结果里取上游真实 tokens；缺失写 null。"""
    tokens = (usage or {}).get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    return {key: _number(tokens.get(key)) for key in TOKEN_KEYS}


def allowance_field(usage: dict | None):
    return _number((usage or {}).get("allowance"))


def new_request_record(method: str, path: str, model: str | None,
                       stream: bool, sticky: str = "off") -> dict:
    return {
        "ts": None,
        "req_id": new_req_id(),
        "method": method,
        "path": path,
        "model": model,
        "stream": bool(stream),
        "account": None,
        "provider": None,
        "status": None,
        "upstream_status": None,
        "outcome": None,
        "queue_ms": None,
        "ttfb_ms": None,
        "total_ms": None,
        "bytes": None,
        "retries": 0,
        "failovers": 0,
        "sticky": sticky,
        "tokens": {key: None for key in TOKEN_KEYS},
        "allowance": None,
        "error_reason": None,
    }


class RequestLogger:
    """线程安全的 append-only JSONL writer（含按大小轮转）。"""

    def __init__(self, path: str, max_bytes: int = 20 * 1024 * 1024, keep: int = 3):
        self.path = os.path.expanduser(path or "")
        self.max_bytes = max(0, int(max_bytes or 0))
        self.keep = max(0, int(keep or 0))
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def write(self, record: dict) -> None:
        if not self.enabled:
            return
        payload = {key: value for key, value in record.items()
                   if not key.startswith("_")}
        payload["ts"] = payload.get("ts") or iso_now()
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._rotate_if_needed()
                directory = os.path.dirname(self.path) or "."
                created = not os.path.exists(self.path)
                os.makedirs(directory, mode=0o700, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                if created:
                    os.chmod(self.path, 0o600)   # 日志里有账号/模型，不该对同机其他人可读
            except OSError as exc:
                print(f"[reqlog] write failed: {exc}", flush=True)

    def _rotate_if_needed(self) -> None:
        if not self.max_bytes:
            return
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.max_bytes:
            return
        if self.keep <= 0:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            return
        oldest = f"{self.path}.{self.keep}"
        try:
            os.unlink(oldest)             # 先删最老，再把 .N-1 → .N 右移
        except OSError:
            pass
        for idx in range(self.keep - 1, 0, -1):
            src = f"{self.path}.{idx}"
            if os.path.exists(src):
                try:
                    os.replace(src, f"{self.path}.{idx + 1}")
                except OSError:
                    pass
        try:
            os.replace(self.path, f"{self.path}.1")
        except OSError:
            pass
