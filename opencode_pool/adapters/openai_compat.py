"""通用 OpenAI 兼容适配器：任何"一把 key + 一个 base_url"的上游都能进池子。

它不可观测余量（supports_quota=False），所以调度退化为「轮询 + 失败冷却」——
这正是把新号源接进来的最低成本路径：写 0 行代码，配置里加一段 provider 即可。
"""
from __future__ import annotations


from .base import Adapter, ErrorDecision, Field, WindowSpec


class OpenAICompatAdapter(Adapter):
    id = "openai-compatible"
    display_name = "OpenAI 兼容"
    description = "任意 /v1 兼容上游（无余量观测，按轮询 + 冷却调度）"
    base_url = ""
    protocols = ("openai-completions",)
    supports_quota = False

    fields = (
        Field("key_env", "API 密钥的环境变量名 / DSH 凭据名", kind="secret", required=True,
              sensitive=True),
        Field("base_url", "Base URL", kind="url", required=True,
              help="例如 https://api.example.com/v1"),
    )
    windows: tuple[WindowSpec, ...] = ()

    def build_headers(self, account: dict, key: str, inbound: dict,
                      body: bytes | None = None) -> dict:
        headers = {"Authorization": f"Bearer {key}"}
        for name in ("content-type", "accept", "user-agent", "openai-beta"):
            value = inbound.get(name)
            if value:
                headers[name] = value
        headers.setdefault("Content-Type", "application/json")
        # 上游若要求额外的固定头，配置里写 headers: {...} 直接透传
        for name, value in (account.get("headers") or {}).items():
            headers[name] = str(value)
        return headers

    def classify(self, status: int, body: bytes, headers: dict, account: dict) -> ErrorDecision:
        if status == 429:
            retry_after = 60.0
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    pass
            return ErrorDecision(action="failover", cooldown_seconds=retry_after, reason="rate-limited")
        if status == 401:
            return ErrorDecision(action="failover", cooldown_seconds=1800, reason="auth:401")
        if status >= 500:
            return ErrorDecision(action="failover", cooldown_seconds=30, reason=f"upstream:{status}")
        return ErrorDecision(action="passthrough", detail=f"client:{status}")

    def extract_usage(self, payload: dict) -> dict | None:
        usage = (payload or {}).get("usage") or {}
        if not usage:
            return None
        return {"tokens": usage, "allowance": None}


ADAPTER = OpenAICompatAdapter()
