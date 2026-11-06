"""适配器契约：所有"号源差异"都收敛到这里，调度器与面板不认识任何具体上游。

对照上游实现学到的结构：
  * gpt-load  `internal/channel/spec`：模块 = 声明(字段/连接/路由/能力) + 通用执行引擎
  * new-api   `relay/channel.Adaptor`：URL / header / body 转换 / usage 解析各一个方法
这里取两者的交集，用 Python 协议表达：差异是**数据**（Field/WindowSpec），
行为是**方法**（headers / probe / classify / usage），注册表负责发现。
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# 种子公式版本号：改动它 = 作废所有上游 prompt cache（session id 与钉子会整体换域）。
# 升版本必须写进文档（docs/configuration.md 的会话粘性一节），否则没人知道缓存为什么全miss。
SEED_VERSION = 1


def _content_text(content: Any) -> str:
    """把消息 content 摊平成文本：str / [{type:text,text:...}] / 前置块列表都能吃。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return "" if content is None else str(content)


def conversation_seed(body: bytes | None) -> str:
    """对话指纹：model + system + 首条 user 消息。

    必须对「对话增长」不敏感：只取**不会随轮次变化**的三样东西 ——
    模型名、系统提示（顶层 `system` 或 `messages[0].system`）、第一条 `role=user`
    的消息。assistant / tool 消息一律忽略，否则第 2 轮多出几条消息指纹就变了，
    粘性会在第二轮断掉（anthropic `/v1/messages` 形状尤其明显：首轮只有 1 条消息）。

    没有 user 消息（`/v1/embeddings`、`/v1/completions` 这类没有对话形状的请求）
    **返回空串**：调用方据此退回"不钉号、按普通规则选号"。绝不能退化成 model
    指纹 —— 那会把同模型的嵌入请求全钉死在一个账号上。

    调度层的会话粘性（`Pool.conversation_key`）与适配器的 `x-opencode-session`
    头共用这一份规则，保证"钉在哪个号"和"缓存域"永远是同一把尺子。

    公式只依赖请求内容（无时间/随机数/进程 id），所以进程重启前后 session id
    完全一致，重启不会白丢上游缓存。`SEED_VERSION` 是公式版本号：**改动它等于
    作废上游缓存**（所有 session 域换新），升版本必须在文档里注明。
    """
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""

    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []

    system_text = _content_text(payload.get("system"))
    if not system_text and messages:
        head = messages[0]
        if isinstance(head, dict) and head.get("role") == "system":
            system_text = _content_text(head.get("content"))

    first_user = ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            first_user = _content_text(msg.get("content"))
            break
    else:
        return ""          # 没有 user 消息 → 不是对话，不钉号
    if not first_user.strip():
        return ""          # 空的 user 消息（只有图片等）也不足以稳定区分对话

    model = str(payload.get("model") or "")
    return "\x1f".join((f"v{SEED_VERSION}", model[:200],
                        system_text[:400], first_user[:400]))


# ── 声明部分：面板、CLI、校验器都读这几个描述，不认识具体上游 ──────────────
@dataclass(frozen=True)
class Field:
    """一个账号需要填的字段（值不入日志；sensitive 的字段面板要打码）。"""

    key: str
    label: str
    kind: str = "text"          # text | url | secret | int | bool
    required: bool = False
    sensitive: bool = False
    help: str = ""


@dataclass(frozen=True)
class WindowSpec:
    """一档额度窗口。opencode 是 5h/周/月，别的上游可能是日/月或单档预算。

    scope 与 window_seconds 让存储层与图表不必知道上游语义：
      scope  : "rolling" | "periodic" | "budget"   （滚动窗口 / 固定周期 / 总量预算）
      seconds: 窗口长度，None 表示由上游自报（如预算没有周期）
    """

    key: str
    label: str
    scope: str = "periodic"
    window_seconds: int | None = None
    hue: str = "var(--w5)"      # 面板沿用，纯展示


@dataclass
class WindowState:
    percent: float | None = None
    status: str = "unknown"     # ok | rate-limited | unknown | error
    resets_at: float | None = None      # epoch seconds
    reset_raw: str | None = None        # 上游原始时间串，排查用

    def to_dict(self) -> dict:
        return {
            "percent": self.percent,
            "status": self.status,
            "resets_at": self.resets_at,
            "reset_raw": self.reset_raw,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WindowState":
        return cls(
            percent=d.get("percent"),
            status=d.get("status") or "unknown",
            resets_at=d.get("resets_at"),
            reset_raw=d.get("reset_raw"),
        )


@dataclass
class QuotaSnapshot:
    windows: dict[str, WindowState] = field(default_factory=dict)
    observed_at: float = 0.0

    def to_dict(self) -> dict:
        return {"observed_at": self.observed_at, "windows": {k: v.to_dict() for k, v in self.windows.items()}}


@dataclass
class ErrorDecision:
    """一次失败该怎么处理。调度器只执行，不判断。"""

    action: str                 # failover | passthrough | retry
    cooldown_seconds: float = 0.0     # 仅 action=failover 且 scope=account 时用
    scope: str = "account"      # window | account
    window: str | None = None
    until: float | None = None  # 绝对时间（额度打满时用窗口重置时刻）
    reason: str = ""
    detail: str = ""            # 只进日志的心跳，不回给客户端


# ── 行为部分 ──────────────────────────────────────────────────────────────
class Adapter(ABC):
    id: str = ""
    display_name: str = ""
    description: str = ""
    base_url: str = ""
    protocols: tuple[str, ...] = ("openai-completions",)
    fields: tuple[Field, ...] = ()
    windows: tuple[WindowSpec, ...] = ()
    supports_quota: bool = False
    """上游是否提供可查询的余量。False 时调度退化为轮询 + 冷却。"""

    # 账号顶层可选字段（session_prefix 等）由 adapters 自己声明，配置层统一校验
    account_fields: tuple[Field, ...] = ()

    # —— 端点 ——
    def endpoint(self, account: dict) -> str:
        return (account.get("base_url") or self.base_url).rstrip("/")

    def build_headers(self, account: dict, key: str, inbound: dict,
                      body: bytes | None = None) -> dict:
        """返回发往上游的请求头；inbound 是客户端原始头（小写键）。

        `body` 给需要"按对话派生状态"的适配器用（opencode 用它算会话亲和指纹）。
        """
        raise NotImplementedError

    def map_path(self, path: str) -> str:
        """客户端路径 → 上游路径。默认假设上游 base 已带 /v1。"""
        return path[3:] if path.startswith("/v1/") else path

    # —— 观测 ——
    def probe(self, account: dict, key: str, http) -> QuotaSnapshot | None:
        return None

    # —— 判定 ——
    @abstractmethod
    def classify(self, status: int, body: bytes, headers: dict, account: dict) -> ErrorDecision:
        raise NotImplementedError

    # —— 解析 ——
    def extract_usage(self, payload: dict) -> dict | None:
        return None

    def extract_models(self, payload: dict) -> list[str]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]

    # —— 校验 ——
    def validate(self, account: dict) -> list[str]:
        problems = []
        for f in self.fields:
            if f.required and not account.get(f.key):
                problems.append(f"缺少字段 {f.key}")
        return problems

    # —— 描述（面板与 /v1/pool/schema 用）——
    def descriptor(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "protocols": list(self.protocols),
            "supports_quota": self.supports_quota,
            "fields": [f.__dict__ for f in self.fields],
            "account_fields": [f.__dict__ for f in self.account_fields],
            "windows": [w.__dict__ for w in self.windows],
        }


def now() -> float:
    return time.time()


def parse_iso(value: Any) -> float | None:
    """容忍上游几种时间写法：ISO8601(带 Z) / epoch 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
