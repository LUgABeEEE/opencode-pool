"""配置：v2 结构 + v1 自动迁移 + 校验。

v2 的心智模型：
  providers  一次上游接入（adapter + base_url + 可选模型白名单）
  accounts   N 把凭据，各自指向一个 provider；账号级字段（session_prefix/headers）自己留着

v1（2026-11-04 之前的版本）是"三个 opencode 账号写死在顶层"，这里自动搬成 v2，
保证老配置、老 state 文件都能直接跑。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import adapters

SCHEMA_VERSION = 2

DEFAULT_PROVIDER_OPTS = {
    "usage_refresh_seconds": 90,
    "upstream_timeout": 900,
}


@dataclass
class Provider:
    id: str
    adapter_id: str
    base_url: str = ""
    models: list[str] = field(default_factory=list)   # 空 = 该适配器声称支持的全部
    headers: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    @property
    def adapter(self):
        adapter = adapters.get(self.adapter_id)
        if adapter is None:
            raise LookupError(f"provider {self.id} 引用了不存在的适配器 {self.adapter_id}")
        return adapter

    def serves(self, model: str | None) -> bool:
        if not self.models or not model:
            return True
        return model in self.models or "*" in self.models

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "adapter": self.adapter_id,
            "base_url": self.base_url,
            "models": self.models,
        }


@dataclass
class Config:
    listen: str = "127.0.0.1:8790"
    relay_token: str = ""
    credentials_yaml: str = "/data/agent/.dsh/.credentials.yaml"
    secrets_file: str = "~/.dsh/opencode-pool/secrets.json"
    state_file: str = ""
    pid_file: str = ""
    upstream_timeout: float = 900
    # 余量低于这个值的账号不参与调度：贴着死线的号上游常"先接流、再不出字"，
    # 比直接 503 更糟（客户端一直转）。想榨干最后一滴可以设 0。
    min_headroom: float = 2.0
    # 每个账号同时最多几个在飞请求。上游对并发很敏感（实测 5+ 并发就回裸 429），
    # 排队比撞限流体面。0 = 不限制。
    max_inflight: int = 1
    # 同一账号两次请求的最小间隔（秒）。实测上游是"按速率"限流：
    # 8 连击回裸 429，间隔 1.5s 串行则 6/6 通过。
    min_interval_seconds: float = 1.2
    # 全池都在瞬时限流冷却时，最多陪客户端等多久（秒）再回 503。
    # 只有一个号时，这个等待决定了"排队"还是"直接失败"。
    queue_wait_seconds: float = 60.0
    usage_refresh_seconds: int = 90
    # 会话粘性：同一对话（指纹 = model + system + 首条 user）在 TTL 内钉在同一个账号上，
    # 保住上游 prompt cache。0 = 关闭粘性（每轮独立选号）。
    #
    # 默认 12 小时而不是 30 分钟：DeepSeek 的上游 prompt cache 保留时长是小时级，
    # 会话停 30 分钟再回来时缓存往往还活着，钉子先过期反而把有效缓存作废了。
    # TTL 只是"安全上界"，真正的回收交给 STICKY_MAX 的容量淘汰（最久未用先走）。
    sticky_ttl_seconds: int = 43200
    # 结构化请求日志：一行一个 JSON（append-only），便于 stats.py / jq / pandas 直接吃。
    # "" = 关闭。轮转到 .1/.2/… 并只保留 request_log_keep 份。
    request_log_path: str = "~/.dsh/opencode-pool/requests.jsonl"
    request_log_max_bytes: int = 20 * 1024 * 1024
    request_log_keep: int = 3
    providers: dict[str, Provider] = field(default_factory=dict)
    accounts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version: int = SCHEMA_VERSION
    source_path: str = ""       # 这份配置从哪读的；写回必须写回同一份

    # —— 加载 ——
    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        cfg = cls.from_dict(raw)
        cfg.source_path = os.path.abspath(path)
        base = os.path.dirname(os.path.abspath(path))
        for attr in ("state_file", "pid_file", "credentials_yaml", "secrets_file"):
            value = getattr(cfg, attr)
            if value and not os.path.isabs(value):
                setattr(cfg, attr, os.path.join(base, value))
        return cfg

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        raw = dict(raw or {})
        cfg = cls()
        for key in ("listen", "relay_token", "credentials_yaml", "secrets_file",
                    "state_file", "pid_file", "request_log_path"):
            if key in raw and raw[key] is not None:
                setattr(cfg, key, raw[key])
        if raw.get("upstream_timeout"):
            cfg.upstream_timeout = float(raw["upstream_timeout"])
        if raw.get("queue_wait_seconds") is not None:
            cfg.queue_wait_seconds = float(raw["queue_wait_seconds"])
        if raw.get("min_interval_seconds") is not None:
            cfg.min_interval_seconds = float(raw["min_interval_seconds"])
        if raw.get("max_inflight") is not None:
            cfg.max_inflight = int(raw["max_inflight"])
        if raw.get("min_headroom") is not None:
            cfg.min_headroom = float(raw["min_headroom"])
        if raw.get("usage_refresh_seconds"):
            cfg.usage_refresh_seconds = int(raw["usage_refresh_seconds"])
        if raw.get("sticky_ttl_seconds") is not None:
            cfg.sticky_ttl_seconds = max(0, int(raw["sticky_ttl_seconds"]))
        if raw.get("request_log_max_bytes") is not None:
            cfg.request_log_max_bytes = max(0, int(raw["request_log_max_bytes"]))
        if raw.get("request_log_keep") is not None:
            cfg.request_log_keep = max(0, int(raw["request_log_keep"]))

        providers = raw.get("providers") or {}
        for pid, spec in providers.items():
            cfg.providers[pid] = Provider(
                id=pid,
                adapter_id=spec.get("adapter") or "openai-compatible",
                base_url=spec.get("base_url") or "",
                models=list(spec.get("models") or []),
                headers=dict(spec.get("headers") or {}),
                options=dict(spec.get("options") or {}),
            )

        accounts = raw.get("accounts") or []
        if not providers:
            # v1：账号平铺在顶层，全部属于同一个 opencode 上游
            cfg.version = 1
            pid = "opencode-go"
            cfg.providers[pid] = Provider(id=pid, adapter_id="opencode-go")
            cfg.warnings.append("配置是 v1 结构，已自动迁移为 v2（providers + accounts）")
        cfg.accounts = []
        for spec in accounts:
            account = dict(spec)
            account.setdefault("provider", next(iter(cfg.providers)))
            cfg.accounts.append(account)
        return cfg

    # —— 校验 ——
    def validate(self) -> list[str]:
        problems = list(self.warnings)
        for pid, provider in self.providers.items():
            adapter = adapters.get(provider.adapter_id)
            if adapter is None:
                problems.append(f"provider {pid}: 未知适配器 {provider.adapter_id}"
                                f"（已加载：{[a.id for a in adapters.all()]}）")
                continue
            if not (provider.base_url or adapter.base_url):
                problems.append(f"provider {pid}: 缺少 base_url，适配器 {adapter.id} 也没有默认值")
        names = set()
        for account in self.accounts:
            name = account.get("name")
            if not name:
                problems.append("account: 缺少 name")
                continue
            if name in names:
                problems.append(f"account {name}: 重名")
            names.add(name)
            pid = account.get("provider")
            provider = self.providers.get(pid)
            if provider is None:
                problems.append(f"account {name}: provider {pid} 不存在")
                continue
            adapter = adapters.get(provider.adapter_id)
            if adapter is None:
                continue
            merged = {**provider.options, **account}
            merged.setdefault("base_url", provider.base_url or adapter.base_url)
            problems.extend(f"account {name}: {p}" for p in adapter.validate(merged))
        if not self.accounts:
            problems.append("没有配置任何 account")
        return problems

    def to_raw(self) -> dict:
        """写回磁盘用的原始结构（保留 providers/accounts 的用户写法）。"""
        return {
            "version": SCHEMA_VERSION,
            "listen": self.listen,
            "relay_token": self.relay_token,
            "credentials_yaml": self.credentials_yaml,
            "secrets_file": self.secrets_file,
            "state_file": self.state_file,
            "pid_file": self.pid_file,
            "usage_refresh_seconds": self.usage_refresh_seconds,
            "min_headroom": self.min_headroom,
            "max_inflight": self.max_inflight,
            "min_interval_seconds": self.min_interval_seconds,
            "queue_wait_seconds": self.queue_wait_seconds,
            "upstream_timeout": self.upstream_timeout,
            "sticky_ttl_seconds": self.sticky_ttl_seconds,
            "request_log_path": self.request_log_path,
            "request_log_max_bytes": self.request_log_max_bytes,
            "request_log_keep": self.request_log_keep,
            "providers": {
                pid: {
                    "adapter": prov.adapter_id,
                    **({"base_url": prov.base_url} if prov.base_url else {}),
                    **({"models": prov.models} if prov.models else {}),
                    **({"headers": prov.headers} if prov.headers else {}),
                    **({"options": prov.options} if prov.options else {}),
                } for pid, prov in self.providers.items()
            },
            "accounts": self.accounts,
        }

    def provider_for(self, account: dict) -> Provider:
        return self.providers[account["provider"]]

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "listen": self.listen,
            "state_file": self.state_file,
            "usage_refresh_seconds": self.usage_refresh_seconds,
            "min_headroom": self.min_headroom,
            "max_inflight": self.max_inflight,
            "min_interval_seconds": self.min_interval_seconds,
            "queue_wait_seconds": self.queue_wait_seconds,
            "sticky_ttl_seconds": self.sticky_ttl_seconds,
            "request_log_path": self.request_log_path,
            "request_log_max_bytes": self.request_log_max_bytes,
            "request_log_keep": self.request_log_keep,
            "providers": {pid: p.to_dict() for pid, p in self.providers.items()},
            "accounts": self.accounts,
        }
