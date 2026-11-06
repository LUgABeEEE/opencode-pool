"""调度核心：账号池、余量折算、失败处置、状态持久化。

这一层**不认识任何具体上游**：窗口从适配器声明里来，失败处置从适配器判定里来，
它只做三件事——挑号、记账、持久化。
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import adapters
from .adapters.base import (Adapter, ErrorDecision, QuotaSnapshot, WindowState,
                            conversation_seed)
from .reqlog import RequestLogger
from .secrets import SecretStore, fingerprint
from .config import Config, Provider

HISTORY_MAX = 240

# 会话钉子的上限：TTL 清理之外还要有硬顶，长期运行的进程不能无限长。
STICKY_MAX = 4096

# refresh_error 的凭据缺失前缀：/healthz 靠它把"没配 key"与"采样没跑"分开。
MISSING_KEY_PREFIX = "缺少凭据"


def _human(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} 天"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 60:.0f} 分钟"


# ── 凭据 ────────────────────────────────────────────────────────────────
class KeyStore:
    """解析顺序：环境变量 → 面板密钥仓（可写）→ DSH 的 .credentials.yaml（只读回退）。

    面板写的一律落密钥仓，不动 DSH 的库；DSH 里已有的 key 照样能直接跑。
    """

    def __init__(self, credentials_yaml: str, secrets_file: str = ""):
        self._path = credentials_yaml
        self.store = SecretStore(secrets_file)
        self._refs: dict[str, str] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _read_refs(path: str) -> dict[str, str]:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return {}
        m = re.search(r"^refs:\n((?:  [^\n]*\n)+)", text, re.M)
        if not m:
            return {}
        out = {}
        for line in m.group(1).splitlines():
            key, _, value = line.strip().partition(":")
            out[key.strip()] = value.strip().strip("'\"")
        return out

    def _dsh_refs(self) -> dict[str, str]:
        with self._lock:
            if self._refs is None:
                self._refs = self._read_refs(self._path)
        return self._refs

    def get(self, env_name: str) -> str | None:
        if not env_name:
            return None
        value = os.environ.get(env_name)
        if value:
            return value.strip()
        value = self.store.get(env_name)
        if value:
            return value
        return self._dsh_refs().get(env_name) or None

    def source(self, env_name: str) -> str:
        if not env_name:
            return ""
        if os.environ.get(env_name):
            return "env"
        if self.store.get(env_name):
            return "pool"
        if self._dsh_refs().get(env_name):
            return "dsh"
        return ""

    def describe(self, env_name: str) -> dict:
        """给面板看的密钥状态：有没有、从哪来、指纹（不可逆），永不回出明文。"""
        value = self.get(env_name)
        return {
            "slot": env_name,
            "configured": bool(value),
            "source": self.source(env_name),
            "fingerprint": fingerprint(value or ""),
            "editable": self.source(env_name) != "env",
        }

    def set(self, env_name: str, value: str) -> None:
        self.store.set(env_name, value)

    def delete(self, env_name: str) -> bool:
        return self.store.delete(env_name)


# ── 账号 ────────────────────────────────────────────────────────────────
@dataclass
class Account:
    name: str
    provider_id: str
    adapter: Adapter
    spec: dict
    key_env: str = ""
    enabled: bool = True
    windows: dict[str, WindowState] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    requests: int = 0
    errors: int = 0
    allowance_used: float = 0.0
    # prompt cache 观测：上游 usage.opencode.tokens 的累计。cache_read = 命中缓存
    # 的 token，input = 未命中（真正送进模型）的 token；命中率 =
    # cache_read / (cache_read + input)。粘性有没有保住缓存，看这两个数就行。
    cache_read_tokens: float = 0.0
    input_tokens: float = 0.0
    last_used: float = 0.0
    last_refresh: float = 0.0
    refresh_error: str = ""

    def effective(self, provider: Provider) -> dict:
        """适配器看到的账号视图：provider 级设置 + 账号级覆盖。"""
        merged = dict(provider.options)
        merged.update({k: v for k, v in provider.headers.items()})
        merged.update(self.spec)
        merged["name"] = self.name
        merged["base_url"] = self.spec.get("base_url") or provider.base_url or self.adapter.base_url
        headers = dict(provider.headers)
        headers.update(self.spec.get("headers") or {})
        merged["headers"] = headers
        return merged

    # —— 可用性 ——
    def blocked_window(self) -> str | None:
        for spec in self.adapter.windows:
            state = self.windows.get(spec.key)
            if state is None:
                continue
            if state.status not in ("ok", "unknown"):
                return spec.key
            if (state.percent or 0) >= 100:
                return spec.key
        return None

    def headroom(self) -> float | None:
        """余量：越小越该躲开。None = 这个上游不提供余量观测（走轮询）。"""
        if not self.enabled:
            return -1.0
        if self.cooldown_until > time.time():
            return -1.0
        if not self.adapter.supports_quota or not self.adapter.windows:
            return None
        if self.blocked_window():
            return -1.0
        values = []
        for spec in self.adapter.windows:
            state = self.windows.get(spec.key)
            if state is not None and state.percent is not None:
                values.append(100.0 - state.percent)
        return min(values) if values else None

    def available(self, floor: float = 0.0) -> bool:
        """现在能不能接单：启用、没在冷却、没有窗口打满、余量不低于水位线。

        会话粘性靠它判"钉子还作不作数"，/healthz 靠它判"可调度"，
        两处必须是同一套判据，否则面板说能用、探针说没号。
        """
        if not self.enabled:
            return False
        if self.cooldown_until > time.time():
            return False
        if self.blocked_window():
            return False
        headroom = self.headroom()
        if headroom is None:          # 上游不提供余量观测 → 只看冷却
            return True
        return headroom >= max(0.0, float(floor or 0.0))

    def to_state(self) -> dict:
        return {
            "windows": {k: v.to_dict() for k, v in self.windows.items()},
            "history": self.history[-HISTORY_MAX:],
            "cooldown_until": self.cooldown_until,
            "cooldown_reason": self.cooldown_reason,
            "requests": self.requests,
            "errors": self.errors,
            "allowance_used": self.allowance_used,
            "last_used": self.last_used,
            "last_refresh": self.last_refresh,
            "enabled": self.enabled,
        }

    def load_state(self, state: dict) -> None:
        for key, raw in (state.get("windows") or {}).items():
            self.windows[key] = WindowState.from_dict(raw)
        self.history = state.get("history") or []
        self.cooldown_until = state.get("cooldown_until") or 0.0
        self.cooldown_reason = state.get("cooldown_reason") or ""
        self.requests = state.get("requests") or 0
        self.errors = state.get("errors") or 0
        self.allowance_used = state.get("allowance_used") or 0.0
        self.last_used = state.get("last_used") or 0.0
        self.last_refresh = state.get("last_refresh") or 0.0
        if "enabled" in state:
            self.enabled = bool(state["enabled"])


# ── 池 ──────────────────────────────────────────────────────────────────
class Pool:
    def __init__(self, config: Config, keys: "KeyStore | None" = None):
        self.cfg = config
        # 可选注入密钥仓：单测传假 KeyStore，就不必读 os.environ / ~/.dsh，
        # 也不会因为测试类之间的环境变量泄漏而抖动（生产调用不传，行为不变）。
        self.keys = keys if keys is not None else KeyStore(config.credentials_yaml, config.secrets_file)
        # 结构化请求日志（requests.jsonl）：路径为空 = 关闭
        self.request_log = RequestLogger(
            getattr(config, "request_log_path", ""),
            getattr(config, "request_log_max_bytes", 0),
            getattr(config, "request_log_keep", 0),
        )
        self.accounts: list[Account] = []
        for spec in config.accounts:
            provider = config.provider_for(spec)
            adapter = provider.adapter
            self.accounts.append(Account(
                name=spec["name"],
                provider_id=provider.id,
                adapter=adapter,
                spec=spec,
                key_env=spec.get("key_env") or "",
                enabled=bool(spec.get("enabled", True)),
            ))
        self.lock = threading.Lock()
        self.http = requests.Session()
        self.http.trust_env = False
        self.started_at = time.time()
        self.recent: collections.deque[float] = collections.deque(maxlen=4000)
        limit = max(0, int(getattr(config, "max_inflight", 0) or 0))
        self._gates: dict[str, threading.Semaphore] = {
            a.name: threading.Semaphore(limit) for a in self.accounts
        } if limit else {}
        self._gate_limit = limit
        self._last_start: dict[str, float] = {}
        self._dirty = False
        self._last_save = 0.0
        # 状态落盘串行化：临时文件 + os.replace 必须同一时刻只有一个 writer
        self._save_lock = threading.Lock()
        # 会话粘性：指纹 → (到期时刻, 账号名)。只存摘要，不存 prompt 正文。
        self._sticky: dict[str, tuple[float, str]] = {}
        self.sticky_hits = 0
        self.sticky_misses = 0
        # Prometheus 计数：{(account, model, status): n} / {(account, reason): n}
        self._requests: dict[tuple[str, str, str], int] = {}
        self._failovers: dict[tuple[str, str], int] = {}
        self._load_state()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    # —— provider 视图 ——
    def provider(self, account: Account) -> Provider:
        return self.cfg.providers[account.provider_id]

    # —— 持久化 ——
    def _load_state(self) -> None:
        path = self.cfg.state_file
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as exc:
            # 坏状态不该挡住启动，但要留一行线索，否则"状态为什么丢了"无从查起
            print(f"[pool] state load failed ({type(exc).__name__}): {exc}"[:160], flush=True)
            return
        saved = state.get("accounts") or {}
        for account in self.accounts:
            if account.name in saved:
                try:
                    account.load_state(saved[account.name])
                except Exception:  # noqa: BLE001 - 旧状态坏掉不该挡住启动
                    pass

    def _save_state(self, force: bool = False) -> None:
        """写状态文件：唯一临时文件 + 串行化 + 原子 replace。

        旧实现用固定的 `path + ".tmp"`：多线程同时 force 保存会互相踩同一个
        临时文件（读回半截 JSON），先 replace 的线程还会让后一个 ENOENT。
        现在每次 mkstemp 一个自己的临时文件，`_save_lock` 保证同一时刻只有
        一个 writer，replace 之后清理自己的临时文件。
        """
        path = self.cfg.state_file
        if not path:
            return
        with self._save_lock:
            if not force and (not self._dirty or time.time() - self._last_save < 5):
                return
            # 快照在 self.lock 内取：窗口 dict 可能正被采样线程改，直接 dump 会
            # RuntimeError: dictionary changed size during iteration
            with self.lock:
                payload = {
                    "version": 2,
                    "saved_at": time.time(),
                    "accounts": {a.name: a.to_state() for a in self.accounts},
                }
            tmp: str | None = None
            try:
                directory = os.path.dirname(os.path.abspath(path)) or "."
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pool-state-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp, path)
                tmp = None
                self._dirty = False
                self._last_save = time.time()
            except OSError as exc:
                print(f"[pool] state save failed: {exc}", flush=True)
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # —— 观测 ——
    def refresh(self, account: Account) -> None:
        # 凭据检查放在 supports_quota 早退之前：没有额度观测的适配器（vLLM 等）
        # 照样可能缺 key，必须留下 refresh_error，否则 /healthz 会误报“就绪”。
        key = self.keys.get(account.key_env) if account.key_env else ""
        if account.key_env and not key:
            account.refresh_error = f"{MISSING_KEY_PREFIX} {account.key_env}"
            return
        if not account.adapter.supports_quota:
            account.refresh_error = ""
            return
        if not key:                   # 不需要凭据的 quota 适配器：无法主动采样
            account.refresh_error = ""
            return
        try:
            snapshot: QuotaSnapshot | None = account.adapter.probe(
                account.effective(self.provider(account)), key, self.http)
        except Exception as exc:  # noqa: BLE001
            account.refresh_error = f"{type(exc).__name__}: {exc}"[:120]
            return
        if snapshot is None:
            return
        with self.lock:
            account.windows = snapshot.windows
            account.last_refresh = snapshot.observed_at or time.time()
            account.refresh_error = ""
            sample = {"t": account.last_refresh}
            for spec in account.adapter.windows:
                state = account.windows.get(spec.key)
                sample[spec.key] = state.percent if state else None
            account.history.append(sample)
            account.history = account.history[-HISTORY_MAX:]
            self._dirty = True
        self._save_state()

    def _refresh_loop(self) -> None:
        while True:
            for account in list(self.accounts):
                try:
                    self.refresh(account)
                except Exception as exc:  # noqa: BLE001
                    print(f"[pool] refresh {account.name} crashed: {exc}", flush=True)
            time.sleep(max(15, int(self.cfg.usage_refresh_seconds)))

    # —— 派生指标 ——
    def _rate(self, account: Account, window: str, lookback: float = 7200.0) -> float | None:
        cutoff = time.time() - lookback
        samples = [h for h in account.history if h.get("t", 0) >= cutoff and h.get(window) is not None]
        if len(samples) < 2:
            return None
        span = (samples[-1]["t"] - samples[0]["t"]) / 3600.0
        if span < 0.02:
            return None
        delta = (samples[-1][window] or 0) - (samples[0][window] or 0)
        return max(0.0, delta / span)

    def derive(self, account: Account) -> dict:
        percent: dict[str, float | None] = {}
        rate: dict[str, float | None] = {}
        eta: dict[str, float | None] = {}
        for spec in account.adapter.windows:
            state = account.windows.get(spec.key)
            percent[spec.key] = state.percent if state else None
            rate[spec.key] = self._rate(account, spec.key)
            used = percent[spec.key] or 0.0
            r = rate[spec.key]
            eta[spec.key] = max(0.0, (100.0 - used) / r * 3600.0) if r and r > 0 else None
        exhausted = [spec.key for spec in account.adapter.windows
                     if (account.windows.get(spec.key) or WindowState()).status not in ("ok", "unknown")]
        if exhausted:
            binding = exhausted[0]
        elif percent:
            binding = max(percent, key=lambda k: percent.get(k) or 0.0)
        else:
            binding = None
        return {"percent": percent, "rate_per_hour": rate, "eta_seconds": eta, "binding_window": binding}

    # —— 会话粘性 ——
    def sticky_ttl(self) -> float:
        return max(0.0, float(getattr(self.cfg, "sticky_ttl_seconds", 0) or 0))

    def conversation_key(self, model: str | None, body: bytes | None) -> str:
        """对话指纹：model + system + 首条 user → sha1 摘要。

        只留摘要、不留正文：映射表里躺的永远是 16 位十六进制，不是用户内容。
        没有对话形状（无 user 消息的 embeddings / completions）、或
        `sticky_ttl_seconds = 0` 时返回空串 —— 空串 = 不钉号、不计数，
        调用方按普通规则每轮选号。
        """
        if self.sticky_ttl() <= 0:
            if self._sticky:      # 关掉粘性后旧钉子没有任何用处，顺手清干净
                with self.lock:
                    self._sticky.clear()
            return ""
        seed = conversation_seed(body)
        if not seed:
            return ""
        return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:16]

    def _prune_sticky(self, now: float) -> None:
        """回收钉子：先清过期，再按容量上限做 LRU 淘汰。

        命中即续期，所以"到期时间"就是"最久未用"的等价物：TTL（默认 12h）只是
        安全上界，正常回收发生在 STICKY_MAX 溢出时 —— 按最早到期批量淘汰。
        调用方需持有 self.lock。
        """
        expired = [key for key, (expires_at, _) in self._sticky.items() if expires_at <= now]
        for key in expired:
            self._sticky.pop(key, None)
        overflow = len(self._sticky) - STICKY_MAX
        if overflow > 0:
            oldest = sorted(self._sticky.items(), key=lambda kv: kv[1][0])[:overflow]
            for key, _ in oldest:
                self._sticky.pop(key, None)

    def unpin(self, sticky_key: str, account_name: str | None = None) -> bool:
        """解除对话钉子（失败换号后用），让下一轮重新分布。

        `account_name` 给了就只解"当前确实钉在它上面"的钉子，避免误伤已经重选过的映射。
        """
        if not sticky_key:
            return False
        with self.lock:
            pinned = self._sticky.get(sticky_key)
            if pinned is None:
                return False
            if account_name and pinned[1] != account_name:
                return False
            del self._sticky[sticky_key]
            return True

    def _sticky_view(self) -> dict:
        """调用方需持有 self.lock。"""
        return {
            "ttl_seconds": self.sticky_ttl(),
            "pinned": len(self._sticky),
            "hits": self.sticky_hits,
            "misses": self.sticky_misses,
        }

    # —— 选号 ——
    def pick(self, model: str | None = None, exclude: set[str] | None = None,
             sticky_key: str = "", trace: dict | None = None) -> Account | None:
        """挑号优先级：会话钉子 > 显式声明了该模型的 provider > 通配 provider；同档按余量。

        没有 provider 这一层的话，一个"啥都能接"的上游会把别的上游专有的模型也抢走 ——
        实测过：本地 vLLM 的模型被 opencode 通了，回 4xx Model is unavailable。

        钉住的号只有在**不可用**（冷却 / 额度打满 / 低于水位线）或已试过时才让位；
        让位后按常规规则重选并更新钉子。TTL 到期或映射超量由 _prune_sticky 清理。
        """
        exclude = exclude or set()
        floor = float(getattr(self.cfg, "min_headroom", 0.0) or 0.0)
        now = time.time()

        if sticky_key:
            with self.lock:
                self._prune_sticky(now)
                pinned = self._sticky.get(sticky_key)
                if pinned and pinned[0] > now and pinned[1] not in exclude:
                    candidate = next((a for a in self.accounts if a.name == pinned[1]), None)
                    if (candidate is not None
                            and self.provider(candidate).serves(model)
                            and candidate.available(floor)):
                        # 命中：续期，同一账号的 prompt cache 继续有效
                        self._sticky[sticky_key] = (now + self.sticky_ttl(), candidate.name)
                        self.sticky_hits += 1
                        if trace is not None:
                            trace["sticky"] = "hit"
                        return candidate

        with self.lock:
            candidates = [a for a in self.accounts if a.name not in exclude]
            if model:
                candidates = [a for a in candidates if self.provider(a).serves(model)]
                explicit = [a for a in candidates
                            if model in self.provider(a).models and self.provider(a).models]
                if explicit:
                    candidates = explicit

        def sort_key(account: Account):
            return (-(account.headroom() or 0.0), account.last_used)

        refreshed = [a for a in candidates
                     if a.headroom() is not None and a.available(floor)]
        chosen = sorted(refreshed, key=sort_key)[0] if refreshed else None
        if chosen is None:
            unknown = [a for a in candidates if a.headroom() is None and a.available(floor)]
            if unknown:
                chosen = sorted(unknown, key=lambda a: a.last_used)[0]

        if chosen is not None and sticky_key:
            if trace is not None:
                trace["sticky"] = "miss"
            with self.lock:
                # 重选（新对话 / 钉子过期 / 原号不可用）→ 更新钉子
                self._sticky[sticky_key] = (now + self.sticky_ttl(), chosen.name)
                # 未命中只在"真的重新选了号"时记一次：全池冷却时客户端每 0.5s
                # 就会探一次 pick，那种等待不该灌进指标。
                self.sticky_misses += 1
                self._prune_sticky(now)
        return chosen

    # —— 并发闸门 ——
    def acquire(self, account: Account, timeout: float = 30.0) -> bool:
        """占一个并发位；超时返回 False（调用方应当换号或快速失败）。"""
        gate = self._gates.get(account.name)
        if gate is None:
            return True
        return gate.acquire(timeout=timeout)

    def release(self, account: Account) -> None:
        gate = self._gates.get(account.name)
        if gate is not None:
            try:
                gate.release()
            except ValueError:
                pass

    def wait_turn(self, account: Account, timeout: float = 90.0) -> bool:
        """按最小间隔排队：上游是速率限流，排队比撞 429 体面得多。"""
        interval = float(getattr(self.cfg, "min_interval_seconds", 0) or 0)
        if interval <= 0:
            return True
        deadline = time.time() + timeout
        while True:
            with self.lock:
                now = time.time()
                wait = self._last_start.get(account.name, 0.0) + interval - now
                if wait <= 0:
                    self._last_start[account.name] = now
                    return True
            if time.time() > deadline:
                return False
            time.sleep(min(wait, 0.2))

    # —— 记账 / 处置 ——
    def note_request(self, account: Account | None, model: str | None, status) -> None:
        """记一次经手（含换号尝试）：status 是上游返回码或本地处置码。

        model 截断到 64 字符：客户端能送任意模型名，不能让标签基数无限膨胀。
        """
        key = (account.name if account is not None else "-",
               (model or "-")[:64],
               str(status)[:32])
        with self.lock:
            self._requests[key] = self._requests.get(key, 0) + 1

    def record_request(self, record: dict) -> None:
        """写一行结构化请求日志；记账失败绝不能影响已经发完的响应。"""
        try:
            self.request_log.write(record)
        except Exception as exc:  # noqa: BLE001
            print(f"[reqlog] {type(exc).__name__}: {exc}", flush=True)

    def note_failover(self, account: Account, reason: str) -> None:
        key = (account.name, (reason or "?")[:64])
        with self.lock:
            self._failovers[key] = self._failovers.get(key, 0) + 1

    @staticmethod
    def _token_count(tokens: dict, key: str) -> float:
        """上游 tokens 字段容错取值：数字才收，字符串/缺字段都当 0。"""
        value = tokens.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def mark_ok(self, account: Account, usage: dict | None = None) -> None:
        with self.lock:
            account.requests += 1
            account.last_used = time.time()
            if usage:
                if usage.get("allowance"):
                    try:
                        account.allowance_used += float(usage["allowance"])
                    except (TypeError, ValueError):
                        pass
                tokens = usage.get("tokens")
                if isinstance(tokens, dict):
                    # 缓存命中量是"粘性有没有用"的唯一硬证据；input 是分母
                    account.cache_read_tokens += self._token_count(tokens, "cache_read")
                    account.input_tokens += self._token_count(tokens, "input")
            self.recent.append(time.time())
            self._dirty = True
        self._save_state()

    def apply(self, account: Account, decision: ErrorDecision) -> None:
        """执行适配器的处置决定。"""
        now = time.time()
        passthrough = False
        with self.lock:
            account.errors += 1
            if decision.action == "passthrough":
                self._dirty = True
                passthrough = True
            else:
                if decision.action == "failover":
                    # 换号计数与日志同源：一次 failover 处置 = 一次换号
                    key = (account.name, (decision.reason or "?")[:64])
                    self._failovers[key] = self._failovers.get(key, 0) + 1

                if decision.scope == "window" and decision.window:
                    state = account.windows.setdefault(decision.window, WindowState())
                    state.status = "rate-limited"
                    state.percent = 100.0
                    deadline = state.resets_at
                    if decision.until:
                        deadline = max(deadline or 0.0, decision.until) or None
                    if decision.cooldown_seconds and not deadline:
                        deadline = now + decision.cooldown_seconds
                    wait = max(60.0, (deadline - now)) if deadline else 3600.0
                else:
                    wait = max(1.0, decision.cooldown_seconds or 30.0)

                account.cooldown_until = max(account.cooldown_until, now + wait)
                account.cooldown_reason = decision.reason or "cooling"
                self._dirty = True
        # 落盘放在 self.lock 之外：锁序固定为 _save_lock → self.lock，避免死锁
        if passthrough:
            self._save_state(force=True)
            return
        reason = decision.reason or "?"
        print(f"[pool] {account.name} 冷却 {_human(wait)} 原因={reason}", flush=True)
        self._save_state(force=True)

    def clear_cool(self) -> list[dict]:
        cleared = []
        with self.lock:
            for account in self.accounts:
                if account.cooldown_until or account.cooldown_reason:
                    cleared.append({"name": account.name, "reason": account.cooldown_reason})
                    account.cooldown_until = 0.0
                    account.cooldown_reason = ""
            self._dirty = True
        self._save_state(force=True)
        return cleared

    # —— 就绪 / 指标 ——
    def readiness(self) -> tuple[bool, dict]:
        """就绪语义：200 = 至少一个账号可调度且额度采样新鲜。

        采样新鲜判据：年龄 < 2 × usage_refresh_seconds（不提供余量观测的适配器没有采样，
        只看冷却/暂停）。返回 (ok, body)；body 只含计数与原因，绝不含密钥。
        """
        now = time.time()
        refresh = max(15, int(getattr(self.cfg, "usage_refresh_seconds", 90) or 90))
        fresh_for = 2 * refresh
        floor = float(getattr(self.cfg, "min_headroom", 0.0) or 0.0)
        counts = {"total": 0, "ready": 0, "disabled": 0, "cooling": 0,
                  "exhausted": 0, "below_reserve": 0, "stale": 0,
                  "no_credentials": 0, "no_quota": 0}
        oldest_age: float | None = None
        never_sampled = 0
        with self.lock:
            accounts = list(self.accounts)

        for account in accounts:
            counts["total"] += 1
            quota = account.adapter.supports_quota and bool(account.adapter.windows)
            # 直接问密钥仓，不信 refresh_error：后者取决于后台线程有没有跑过一轮，
            # 会把"还没采到样"和"没配凭据"混在一起。key_env 为空 = 该适配器不需要凭据。
            missing_key = bool(account.key_env) and not self.keys.get(account.key_env)
            age: float | None = None
            if not account.enabled:
                counts["disabled"] += 1
                continue
            if missing_key:
                # 没凭据就采不到样：这是独立原因，不能伪装成"采样没跑/过期"。
                # 只计数，不回槽位名。
                counts["no_credentials"] += 1
                continue
            if quota:
                if account.last_refresh:
                    age = now - account.last_refresh
                    oldest_age = age if oldest_age is None else max(oldest_age, age)
                else:
                    never_sampled += 1
            if account.cooldown_until > now:
                counts["cooling"] += 1
                continue
            if account.blocked_window():
                counts["exhausted"] += 1
                continue
            headroom = account.headroom()
            if headroom is not None and headroom < max(0.0, floor):
                counts["below_reserve"] += 1
                continue
            if quota:
                if age is None or age >= fresh_for:
                    counts["stale"] += 1
                    continue
            else:
                counts["no_quota"] += 1
            counts["ready"] += 1

        ok = counts["ready"] > 0
        if ok:
            reason = "ready"
        elif counts["total"] == 0:
            reason = "no-accounts"
        elif counts["disabled"] == counts["total"]:
            reason = "all-disabled"
        else:
            blockers = [("missing-credentials", counts["no_credentials"]),
                        ("all-cooling", counts["cooling"]),
                        ("all-exhausted", counts["exhausted"]),
                        ("all-below-reserve", counts["below_reserve"]),
                        ("usage-stale", counts["stale"])]
            reason = max(blockers, key=lambda item: item[1])[0]
            if reason == "usage-stale" and never_sampled:
                reason = "usage-not-sampled"

        messages = {
            "ready": "至少一个账号可调度且额度采样新鲜",
            "no-accounts": "池子里没有配置任何账号",
            "all-disabled": "全部账号已暂停",
            "missing-credentials": "全部账号缺少凭据（读不到凭据，无法采样额度）",
            "all-cooling": "全部账号在冷却",
            "all-exhausted": "全部账号额度打满",
            "all-below-reserve": "全部账号余量低于水位线（不参与调度）",
            "usage-stale": f"额度采样过期（年龄 ≥ {fresh_for}s），不知道余量就不接单",
            "usage-not-sampled": "启动后尚未完成首轮额度采样",
        }
        body = {
            "ok": ok,
            "ready": ok,
            "reason": reason,
            "message": messages.get(reason, reason),
            "accounts": counts,
            "usage_refresh_seconds": refresh,
            "fresh_within_seconds": fresh_for,
            "oldest_sample_age_seconds": round(oldest_age, 1) if oldest_age is not None else None,
            "never_sampled": never_sampled,
            "upstreams": list(self.cfg.providers),
            "uptime_seconds": round(now - self.started_at, 1),
        }
        return ok, body

    @staticmethod
    def _escape_label(value) -> str:
        """Prometheus 文本格式的标签转义：\\ " 换行。"""
        return (str(value).replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace('"', '\\"'))

    @staticmethod
    def _num(value) -> str:
        """定点输出：Prometheus 认指数写法，但 9.95e+07 这种秒数人读起来费劲。"""
        text = f"{float(value):.3f}".rstrip("0").rstrip(".")
        return text or "0"

    def metrics_text(self) -> str:
        """Prometheus 文本格式（手写，够抓就行）。

        与 /v1/usage 同源：都从这一份账号状态算出来，两处对不上就是 bug。
        """
        now = time.time()
        out: list[str] = []
        esc = self._escape_label

        def family(name: str, kind: str, help_text: str) -> None:
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")

        with self.lock:
            accounts = list(self.accounts)
            requests = dict(self._requests)
            failovers = dict(self._failovers)
            sticky = self._sticky_view()

        family("pool_account_headroom", "gauge",
               "账号余量 = 100 - max(各窗口 percent)；-1 = 冷却中/额度打满/已暂停")
        for account in accounts:
            headroom = account.headroom()
            if headroom is None:      # 上游不提供余量观测 → 不出这条序列
                continue
            out.append(
                f'pool_account_headroom{{account="{esc(account.name)}",'
                f'provider="{esc(account.provider_id)}"}} {self._num(headroom)}')

        family("pool_account_window_percent", "gauge", "单档额度已用百分比（0-100）")
        for account in accounts:
            for spec in account.adapter.windows:
                state = account.windows.get(spec.key)
                if state is None or state.percent is None:
                    continue
                out.append(
                    f'pool_account_window_percent{{account="{esc(account.name)}",'
                    f'window="{esc(spec.key)}"}} {self._num(state.percent)}')

        family("pool_requests_total", "counter", "经手的请求数（含换号尝试）")
        for (name, model, status), count in sorted(requests.items()):
            out.append(
                f'pool_requests_total{{account="{esc(name)}",model="{esc(model)}",'
                f'status="{esc(status)}"}} {count}')

        family("pool_failovers_total", "counter", "换号次数（reason 与日志同源）")
        for (name, reason), count in sorted(failovers.items()):
            out.append(
                f'pool_failovers_total{{account="{esc(name)}",'
                f'reason="{esc(reason)}"}} {count}')

        family("pool_cooling_seconds", "gauge", "该账号还要冷却多少秒（0 = 没在冷却）")
        for account in accounts:
            out.append(
                f'pool_cooling_seconds{{account="{esc(account.name)}"}} '
                f'{self._num(max(0.0, account.cooldown_until - now))}')

        # prompt cache 观测：cache_read 是"粘性有没有保住缓存"的唯一硬证据。
        # 注意上游对短 prompt 不命中缓存（cache_read 恒为 0），别拿它当健康判据。
        family("pool_cache_read_tokens_total", "counter",
               "上游 prompt cache 命中的输入 token 累计（usage.opencode.tokens.cache_read）")
        for account in accounts:
            out.append(
                f'pool_cache_read_tokens_total{{account="{esc(account.name)}"}} '
                f'{self._num(account.cache_read_tokens)}')
        family("pool_input_tokens_total", "counter",
               "未命中缓存的输入 token 累计；命中率 = cache_read / (cache_read + input)")
        for account in accounts:
            out.append(
                f'pool_input_tokens_total{{account="{esc(account.name)}"}} '
                f'{self._num(account.input_tokens)}')

        family("pool_sticky_hits_total", "counter", "会话粘性命中（对话被钉回原账号）")
        out.append(f"pool_sticky_hits_total {sticky['hits']}")
        family("pool_sticky_misses_total", "counter",
               "会话粘性未命中（新对话 / 钉子过期 / 账号不可用重选）")
        out.append(f"pool_sticky_misses_total {sticky['misses']}")
        return "\n".join(out) + "\n"

    def inherit(self, other: "Pool") -> None:
        """重载配置后接管运行期观测：采样时间线、粘性钉子与计数器不能归零。"""
        self.recent = other.recent
        self.started_at = other.started_at
        with other.lock:
            self.sticky_hits = other.sticky_hits
            self.sticky_misses = other.sticky_misses
            self._sticky = dict(other._sticky)
            self._requests = dict(other._requests)
            self._failovers = dict(other._failovers)
            cache = {a.name: (a.cache_read_tokens, a.input_tokens) for a in other.accounts}
        for account in self.accounts:
            if account.name in cache:      # 重载面板配置不该把缓存观测归零
                account.cache_read_tokens, account.input_tokens = cache[account.name]

    # —— 快照 ——
    def snapshot(self) -> dict:
        now = time.time()
        while self.recent and now - self.recent[0] > 900:
            self.recent.popleft()
        with self.lock:
            accounts = []
            for account in self.accounts:
                derived = self.derive(account)
                accounts.append({
                    "name": account.name,
                    "provider": account.provider_id,
                    "adapter": account.adapter.id,
                    "enabled": account.enabled,
                    "headroom": account.headroom(),
                    "below_reserve": (account.headroom() is not None
                                      and 0 <= account.headroom() < float(getattr(self.cfg, "min_headroom", 0) or 0)),
                    "blocked_window": account.blocked_window(),
                    "cooldown_until": account.cooldown_until or None,
                    "cooldown_reason": account.cooldown_reason or None,
                    "windows": {k: v.to_dict() for k, v in account.windows.items()},
                    "requests": account.requests,
                    "errors": account.errors,
                    "allowance_used": round(account.allowance_used, 6),
                    "cache_read_tokens": round(account.cache_read_tokens, 3),
                    "input_tokens": round(account.input_tokens, 3),
                    # 上游 tokens.input 是"未命中缓存"的那部分，命中率分母要算上 cache_read
                    "cache_hit_ratio": (round(account.cache_read_tokens
                                              / (account.cache_read_tokens + account.input_tokens), 4)
                                        if (account.cache_read_tokens + account.input_tokens) else None),
                    "last_used": account.last_used or None,
                    "last_refresh": account.last_refresh or None,
                    "refresh_error": account.refresh_error or None,
                    "key": self.keys.describe(account.key_env),
                    "samples": len(account.history),
                    **derived,
                })
            worst: dict[str, dict] = {}
            for pid in self.cfg.providers:
                per_provider = [a for a in accounts if a["provider"] == pid]
                window_map = {}
                for spec in self.cfg.providers[pid].adapter.windows:
                    values = [(a["percent"].get(spec.key) or 0, a["name"])
                              for a in per_provider if a["percent"].get(spec.key) is not None]
                    if values:
                        pct, who = max(values)
                        window_map[spec.key] = {"percent": pct, "account": who}
                worst[pid] = window_map
            floor = float(getattr(self.cfg, "min_headroom", 0.0) or 0.0)
            healthy = [a for a in accounts
                       if (a["headroom"] if a["headroom"] is not None else 1) >= max(0.0, floor)]
            return {
                "upstreams": {
                    pid: {
                        "adapter": p.adapter.id,
                        "display_name": p.adapter.display_name,
                        "base_url": p.base_url or p.adapter.base_url,
                        "supports_quota": p.adapter.supports_quota,
                        "windows": [w.__dict__ for w in p.adapter.windows],
                    }
                    for pid, p in self.cfg.providers.items()
                },
                "healthy_accounts": len(healthy),
                "total_accounts": len(accounts),
                "started_at": self.started_at,
                "uptime_seconds": now - self.started_at,
                "requests_last_5m": sum(1 for t in self.recent if now - t <= 300),
                "requests_last_15m": len(self.recent),
                "pool_worst": worst,
                "sticky": self._sticky_view(),
                "accounts": accounts,
            }

    def schema(self) -> dict:
        return {
            "adapters": list(adapters.descriptors()),
            "providers": {
                pid: {
                    "id": pid,
                    "adapter": p.adapter.id,
                    "display_name": p.adapter.display_name,
                    "base_url": p.base_url or p.adapter.base_url,
                    "supports_quota": p.adapter.supports_quota,
                    "windows": [w.__dict__ for w in p.adapter.windows],
                    "models": p.models,
                }
                for pid, p in self.cfg.providers.items()
            },
            "warnings": self.cfg.warnings,
            "adapter_load_errors": adapters.load_errors(),
        }
