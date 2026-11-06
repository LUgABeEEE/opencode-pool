"""OpenCode Go 适配器。

上游事实（2026-11-04 实测 opencode.ai/inference/go/v1）：
  * OpenAI / Anthropic / Responses 三协议同 base，同一把 key
  * `x-opencode-session` 必填（缺 → 400 MissingSessionID），值任意非空
  * GET /usage 返回 rolling(5h) / weekly / monthly，各带 percent + status + resetsAt
  * 超额 429 {"type":"GoUsageLimitError","metadata":{"limitName":"weekly"}} + Retry-After
  * 响应 usage.opencode.{allowance,billing_source,...} 可记账
"""
from __future__ import annotations

import hashlib
import json
import re

from .base import (Adapter, ErrorDecision, Field, QuotaSnapshot, WindowSpec, WindowState,
                   conversation_seed, parse_iso)

SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")

PASS_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "user-agent",
    "openai-beta",
    "anthropic-version",
    "anthropic-beta",
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
)


class OpenCodeGoAdapter(Adapter):
    id = "opencode-go"
    display_name = "OpenCode Go"
    description = "多协议网关：OpenAI / Anthropic / Responses，三档额度可观测"
    base_url = "https://opencode.ai/inference/go/v1"
    protocols = ("openai-completions", "anthropic-messages", "openai-responses")
    supports_quota = True

    fields = (
        Field("key_env", "API 密钥的环境变量名 / DSH 凭据名", kind="secret", required=True,
              sensitive=True, help="值从环境变量或 DSH .credentials.yaml 的 refs 段取"),
        Field("base_url", "Base URL", kind="url", help=f"默认 {base_url}"),
    )
    account_fields = (
        Field("session_prefix", "会话前缀", help="每号一个，用于 x-opencode-session 与 prompt cache 亲和"),
    )

    windows = (
        WindowSpec("rolling", "5 小时", scope="rolling", window_seconds=5 * 3600, hue="var(--w5)"),
        WindowSpec("weekly", "周", scope="periodic", window_seconds=7 * 24 * 3600, hue="var(--wk)"),
        WindowSpec("monthly", "月", scope="periodic", window_seconds=30 * 24 * 3600, hue="var(--wm)"),
    )

    # —— 请求 ——
    @staticmethod
    def conversation_seed(body: bytes | None) -> str:
        """对话指纹（model + 前两条消息）；规则在 base，调度层复用同一份。"""
        return conversation_seed(body)

    def session_id(self, account: dict, inbound: str | None, body: bytes | None = None) -> str:
        prefix = account.get("session_prefix") or f"dsh-pool-{account.get('name', 'x')}"
        route_header = SESSION_SAFE.sub("-", (inbound or "").strip())[:64]
        seed = self.conversation_seed(body) or route_header or "anon"
        # 路由头也混进去：同一对话在不同路由下仍保持各自稳定的缓存域
        digest = hashlib.sha1((seed + "|" + route_header).encode()).hexdigest()[:10]
        return f"{prefix}-{digest}"

    def build_headers(self, account: dict, key: str, inbound: dict, body: bytes | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {key}",
            "x-opencode-session": self.session_id(account, inbound.get("x-opencode-session"), body),
        }
        for name in PASS_REQUEST_HEADERS:
            value = inbound.get(name)
            if value:
                headers[name] = value
        headers.setdefault("Content-Type", "application/json")
        return headers

    # —— 观测 ——
    def probe(self, account: dict, key: str, http) -> QuotaSnapshot | None:
        url = f"{self.endpoint(account)}/usage"
        resp = http.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"usage HTTP {resp.status_code}")
        usage = (resp.json() or {}).get("usage") or {}
        snap = QuotaSnapshot(observed_at=__import__("time").time())
        for spec in self.windows:
            raw = usage.get(spec.key)
            if raw is None:
                continue
            pct = raw.get("percent")
            snap.windows[spec.key] = WindowState(
                percent=float(pct) if pct is not None else None,
                status=raw.get("status") or "unknown",
                resets_at=parse_iso(raw.get("resetsAt")),
                reset_raw=raw.get("resetsAt"),
            )
        return snap

    # —— 判定 ——
    def classify(self, status: int, body: bytes, headers: dict, account: dict) -> ErrorDecision:
        if status == 429:
            window, retry_after = None, None
            try:
                payload = json.loads(body or b"{}")
                window = ((payload.get("metadata") or {}).get("limitName")) or None
            except ValueError:
                pass
            raw_ra = headers.get("retry-after") or headers.get("Retry-After")
            if raw_ra:
                try:
                    retry_after = float(raw_ra)
                except (TypeError, ValueError):
                    retry_after = None
            if not window:
                # 没有 limitName 的 429 = 并发/瞬时限流（实测额度明明还有：
                # 周 46% / 月 23% 时照样 429）。这时候冷账号是错的 —— 唯一账号被冷
                # 就整池 503，团队成员跟着崩。改为同号短退避重试。
                return ErrorDecision(
                    action="retry",
                    scope="account",
                    cooldown_seconds=retry_after or 1.5,
                    reason="throttled",
                )
            return ErrorDecision(
                action="failover",
                scope="window",
                window=window,
                cooldown_seconds=0.0,
                reason=f"quota:{window}",
            )
        if status == 401:
            return ErrorDecision(action="failover", cooldown_seconds=1800, reason="auth:401")
        if status >= 500:
            return ErrorDecision(action="failover", cooldown_seconds=30, reason=f"upstream:{status}")
        # 4xx（403 地区/车型限制、400 参数错）换号也没用 → 原样回给客户端，不冷却
        return ErrorDecision(action="passthrough", detail=f"client:{status}")

    # —— 解析 ——
    def extract_usage(self, payload: dict) -> dict | None:
        usage = (payload or {}).get("usage") or {}
        extra = usage.get("opencode") or {}
        if not extra:
            return None
        return {
            "allowance": extra.get("allowance"),
            "list_cost": extra.get("list_cost"),
            "billing_source": extra.get("billing_source"),
            "tokens": extra.get("tokens"),
        }


ADAPTER = OpenCodeGoAdapter()
