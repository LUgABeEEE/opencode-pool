"""HTTP 层：一个 OpenAI 兼容入口 + 池管理 API + 控制台。

负载路径（/v1/...）完全由适配器决定 URL 与请求头；这里只管选号、重试、透传。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from .adapters.base import ErrorDecision
from .pool import Pool
from .reqlog import (allowance_field, iso_now, new_request_record,
                     token_fields)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_PATH = os.path.join(PKG_DIR, "panel", "panel.html")

PASS_RESPONSE_HEADERS = ("content-type", "openai-processing-ms", "x-request-id", "x-opencode-log-id")

PROXY_PATHS = (
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/models",
)


NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def _cooldown(reason: str, seconds: float) -> ErrorDecision:
    return ErrorDecision("failover", cooldown_seconds=seconds, scope="account", reason=reason)


def config_path(pool: "Pool | None" = None) -> str:
    """写回必须落在「这份池子自己读的那份配置」上。

    曾经写死成包目录下的 pool.config.json，用临时配置起的实例一改配置就动到了生产文件 ——
    所以路径跟着 Config 走，没有才退回默认（包目录旁的那份）。
    """
    if pool is not None and getattr(pool.cfg, "source_path", ""):
        return pool.cfg.source_path
    return os.path.join(PKG_DIR, os.pardir, "pool.config.json")


def write_config(mutator, pool: "Pool | None" = None) -> str:
    """改配置：先备份、再原子写。返回备份路径，失败时抛异常。"""
    from .config import Config

    path = config_path(pool)
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    mutator(raw)
    # 写之前先验证一遍：验证不过就不落地，避免把池子写坏
    problems = Config.from_dict(raw).validate()
    if problems:
        raise ValueError("; ".join(problems))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}-panel"
    shutil.copy2(path, backup)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return backup


def rebuild_pool(pool: Pool) -> Pool:
    """重读配置重建号池，能对上的账号状态（额度采样、累计、冷却）原样搬过去。"""
    from .config import Config

    fresh = Config.load(config_path(pool))
    problems = fresh.validate()
    if problems:
        raise ValueError("; ".join(problems))
    state = {a.name: a.to_state() for a in pool.accounts}
    rebuilt = Pool(fresh)
    for account in rebuilt.accounts:
        if account.name in state:
            account.load_state(state[account.name])
    rebuilt.inherit(pool)     # 采样时间线 / 粘性钉子 / 计数器不因重载归零
    return rebuilt


class RelayServer(ThreadingHTTPServer):
    """HTTP 服务器：把"客户端断开"降噪成一行摘要。

    socketserver 默认的 handle_error 会把整段 traceback 打进 relay.log ——
    一次被取消的流式请求就能刷 6~10 行，把真故障淹掉。这里只对
    连接被重置 / 管道破裂这类**客户端主动断开**降噪；
    其他异常（真正的 bug）仍走默认 traceback，不能被吞掉。
    """

    daemon_threads = True
    disconnect_errors = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_local = threading.local()

    def note_request(self, line: str) -> None:
        """按连接线程记录当前请求行：并发下不会把 path 记到别的请求头上。"""
        self._request_local.request = line
        self._request_local.disconnect_noted = False

    def note_disconnect(self, request_line: str, exc: BaseException) -> None:
        """一次断连只打一行摘要（写回路径与 handle_error 共用）。"""
        self._request_local.disconnect_noted = True
        print(f"[http] 客户端断开 {request_line} ({type(exc).__name__})", flush=True)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, self.disconnect_errors):
            if getattr(self._request_local, "disconnect_noted", False):
                return            # 同一次取消已由写回路径记过，别重复打
            where = getattr(self._request_local, "request", "") or "-"
            self.note_disconnect(where, exc)
            return
        super().handle_error(request, client_address)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opencode-pool/2.0"

    pool: Pool = None  # type: ignore[assignment]

    # ── 基础 ──────────────────────────────────────────────
    def log_message(self, fmt, *args):  # 静音 access log
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._raw(status, body, "application/json; charset=utf-8")

    def _text(self, status: int, text: str, content_type: str) -> None:
        self._raw(status, text.encode("utf-8", "replace"), content_type)

    def _raw(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # 客户端已经走了：不是故障；记一行摘要 + 本请求按 client_error 归档
            self._note_disconnect(exc)
            self._mark_client_bytes(0)

    def _authorized(self) -> bool:
        token = self.pool.cfg.relay_token or ""
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth.removeprefix("Bearer ").strip() == token

    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _metrics_authorized(self) -> bool:
        """指标带账号/模型标签但不含密钥：本机抓取免 token，远程仍要 Bearer。

        Prometheus 通常和转置同机或经内网抓；不要求 token 是为了让
        `curl 127.0.0.1:8790/metrics` 和 sidecar 配置保持简单。
        """
        if self._loopback():
            return True
        return self._authorized()

    def _read_body(self) -> bytes:
        size = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(size) if size else b""

    def _path(self) -> str:
        return self.path.split("?")[0].rstrip("/")

    def _note_disconnect(self, exc: BaseException) -> None:
        """写回路径捕获断连时调用：一行摘要 + 本次请求记为 client_error。

        摘要 path 取**当前请求**（self.command/self.path），不是上一次
        keep-alive 的；同一请求最多打一次（_disconnect_noted）。
        """
        if getattr(self, "_disconnect_noted", False):
            return
        self._disconnect_noted = True
        request_line = f"{getattr(self, 'command', None) or '-'} " \
                       f"{getattr(self, 'path', None) or '-'}"
        server = getattr(self, "server", None)
        reporter = getattr(server, "note_disconnect", None)
        if reporter is not None:
            reporter(request_line, exc)
        else:
            print(f"[http] 客户端断开 {request_line} ({type(exc).__name__})", flush=True)
        record = getattr(self, "_request_record", None)
        if record is not None:
            record["outcome"] = "client_error"
            record["error_reason"] = "client_abort"
            record["status"] = 499            # 客户端已关，没拿到完整响应

    def _mark_client_bytes(self, written: int) -> None:
        """bytes 记**实际写出量**（客户端 0 字节收到时不能记整个响应体）。"""
        record = getattr(self, "_request_record", None)
        if record is not None:
            record["bytes"] = written

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok:
            self._disconnect_noted = False      # 每个请求各自计数，避免串到上一个请求
            note = getattr(self.server, "note_request", None)
            if note is not None:
                note(f"{self.command} {self.path}")
        return ok

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        path = self._path()
        if path in ("", "/", "/panel", "/panel.html"):
            return self._serve_panel()
        if path == "/healthz":
            ok, body = self.pool.readiness()
            return self._json(200 if ok else 503, body)
        if path == "/metrics":
            if not self._metrics_authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._text(200, self.pool.metrics_text(),
                              "text/plain; version=0.0.4; charset=utf-8")
        if path == "/v1/pool/schema":
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, self.pool.schema())
        if path == "/v1/usage":
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, self.pool.snapshot())
        if path in ("/v1/models",):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._proxy("GET", path, b"", stream=False)
        return self._json(404, {"error": f"no route {path}"})

    # ── POST ──────────────────────────────────────────────
    def do_POST(self):  # noqa: N802
        path = self._path()
        if not self._authorized():
            return self._json(401, {"type": "error", "error": {"type": "unauthorized",
                                                               "message": "bad relay token"}})
        body = self._read_body()

        if path == "/v1/pool/refresh":
            for account in self.pool.accounts:
                self.pool.refresh(account)
            return self._json(200, self.pool.snapshot())
        if path == "/v1/pool/clear-cooldowns":
            return self._json(200, {"cleared": self.pool.clear_cool()})
        if path == "/v1/pool/account":
            return self._toggle_account(body)
        if path == "/v1/pool/accounts":
            return self._create_account(body)
        if path == "/v1/pool/account/key":
            return self._rotate_key(body)
        if path == "/v1/pool/account/remove":
            return self._remove_account(body)
        if path == "/v1/pool/reload":
            return self._reload()

        if path in PROXY_PATHS:
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = {}
            model = payload.get("model") if isinstance(payload, dict) else None
            return self._proxy("POST", path, body, stream=bool(payload.get("stream")), model=model)
        return self._json(404, {"error": f"no route {path}"})

    # ── 管理动作 ───────────────────────────────────────────
    def _toggle_account(self, body: bytes):
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        name = payload.get("name")
        with self.pool.lock:
            account = next((a for a in self.pool.accounts if a.name == name), None)
            if account is None:
                return self._json(404, {"error": f"unknown account {name}"})
            if payload.get("enabled") is not None:
                account.enabled = bool(payload["enabled"])
                if account.enabled:
                    account.cooldown_reason = ""
            self.pool._dirty = True
        self.pool._save_state(force=True)
        return self._json(200, self.pool.snapshot())

    def _reload(self):
        try:
            rebuilt = rebuild_pool(self.pool)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": str(exc)})
        self.__class__.pool = rebuilt
        return self._json(200, {"ok": True, "accounts": [a.name for a in rebuilt.accounts],
                                "warnings": rebuilt.cfg.warnings})

    def _create_account(self, body: bytes):
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        name = str(payload.get("name") or "").strip()
        provider_id = str(payload.get("provider") or "").strip()
        key = str(payload.get("key") or "").strip()
        slot = str(payload.get("key_env") or "").strip()

        if not NAME_RE.match(name):
            return self._json(400, {"error": "账号名只能由字母、数字、_ . - 组成（1–40 字）"})
        if provider_id not in self.pool.cfg.providers:
            return self._json(400, {"error": f"没有这个上游：{provider_id}"})
        if not key:
            return self._json(400, {"error": "密钥不能为空"})
        if any(a.name == name for a in self.pool.accounts):
            return self._json(409, {"error": f"账号 {name} 已存在"})

        slot = slot or f"POOL_KEY_{re.sub(r'[^A-Za-z0-9]+', '_', name).upper()}"
        account = {"name": name, "provider": provider_id, "key_env": slot}
        if payload.get("session_prefix"):
            account["session_prefix"] = str(payload["session_prefix"])
        elif self.pool.cfg.providers[provider_id].adapter_id == "opencode-go":
            account["session_prefix"] = f"dsh-pool-{name}"

        try:
            backup = write_config(lambda raw: raw.setdefault("accounts", []).append(account), self.pool)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": f"写配置失败：{exc}"})
        self.pool.keys.set(slot, key)
        try:
            rebuilt = rebuild_pool(self.pool)
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"配置已写但重载失败：{exc}", "backup": backup})
        self.__class__.pool = rebuilt
        for account_obj in rebuilt.accounts:
            if account_obj.name == name:
                rebuilt.refresh(account_obj)
        return self._json(200, {"ok": True, "backup": backup, "slot": slot,
                                "snapshot": rebuilt.snapshot()})

    def _rotate_key(self, body: bytes):
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        name = str(payload.get("name") or "").strip()
        key = str(payload.get("key") or "").strip()
        if not key:
            return self._json(400, {"error": "密钥不能为空"})
        account = next((a for a in self.pool.accounts if a.name == name), None)
        if account is None:
            return self._json(404, {"error": f"没有账号 {name}"})
        slot = str(payload.get("key_env") or account.key_env or "").strip()
        if not slot:
            return self._json(400, {"error": "该账号没有凭据槽位，请先配置 key_env"})
        source = self.pool.keys.source(slot)
        if source == "env":
            return self._json(409, {"error": f"{slot} 来自环境变量，面板改不动；请改名或改环境变量"})
        self.pool.keys.set(slot, key)
        with self.pool.lock:
            account.cooldown_until = 0.0
            account.cooldown_reason = ""
            account.errors = 0
            account.refresh_error = ""
            self.pool._dirty = True
        self.pool._save_state(force=True)
        self.pool.refresh(account)
        return self._json(200, {"ok": True, "slot": slot,
                                "key": self.pool.keys.describe(slot),
                                "snapshot": self.pool.snapshot()})

    def _remove_account(self, body: bytes):
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        name = str(payload.get("name") or "").strip()
        account = next((a for a in self.pool.accounts if a.name == name), None)
        if account is None:
            return self._json(404, {"error": f"没有账号 {name}"})
        slot = account.key_env

        def drop(raw):
            raw["accounts"] = [a for a in raw.get("accounts", []) if a.get("name") != name]

        try:
            backup = write_config(drop, self.pool)
        except Exception as exc:  # noqa: BLE001
            return self._json(400, {"error": f"写配置失败：{exc}"})

        # 只有当没有别的账号再用这个槽位时，才把密钥一起回收
        still_used = any(a.key_env == slot and a.name != name for a in self.pool.accounts)
        secret_dropped = False
        if slot and not still_used:
            secret_dropped = self.pool.keys.delete(slot)

        try:
            rebuilt = rebuild_pool(self.pool)
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"配置已写但重载失败：{exc}", "backup": backup})
        self.__class__.pool = rebuilt
        return self._json(200, {"ok": True, "backup": backup,
                                "secret_dropped": secret_dropped,
                                "snapshot": rebuilt.snapshot()})

    # ── 控制台 ─────────────────────────────────────────────
    def _serve_panel(self) -> None:
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return self._json(403, {"error": "panel is loopback-only"})
        page = PANEL_PATH
        try:
            with open(page, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self._json(500, {"error": "panel.html 不在旁边"})
        html = html.replace("__POOL_TOKEN__", json.dumps(self.pool.cfg.relay_token or ""))
        body = html.encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._note_disconnect(exc)

    # ── 代理 ───────────────────────────────────────────────
    def _proxy(self, method: str, path: str, body: bytes, stream: bool, model: str | None = None):
        record = new_request_record(method, path, model, stream)
        record["_started"] = time.time()
        record["_upstream_s"] = 0.0
        self._request_record = record
        self._disconnect_noted = False
        try:
            self._run_proxy(method, path, body, stream, model, record)
        finally:
            self._finish_request(record)

    @staticmethod
    def _cache_text(record: dict) -> str:
        tokens = record.get("tokens") or {}
        cache_read, prompt_input = tokens.get("cache_read"), tokens.get("input")
        if cache_read is None or prompt_input is None:
            return "cache=-/-"
        return f"cache={int(cache_read)}/{int(cache_read + prompt_input)}"

    def _finish_request(self, record: dict) -> None:
        """一个客户端请求只写一行 JSONL；内部计时键在写盘前剔除。"""
        now = time.time()
        total_s = max(0.0, now - record.pop("_started", now))
        upstream_s = record.pop("_upstream_s", 0.0)
        record["ts"] = iso_now()
        record["total_ms"] = round(total_s * 1000, 1)
        if record.get("ttfb_ms") is None:
            record["ttfb_ms"] = record["total_ms"]
        record["queue_ms"] = max(0.0, round((total_s - upstream_s) * 1000, 1))
        if record.get("outcome") is None:
            status = record.get("status")
            if status is None:
                record["status"] = status = 502
            record["outcome"] = "ok" if status < 400 else "failover"
        self.pool.record_request(record)
        self._request_record = None

    def _run_proxy(self, method, path, body, stream, model, record) -> None:
        started_at = record["_started"]
        inbound = {k.lower(): v for k, v in self.headers.items()}
        tried: set[str] = set()
        attempts: dict[str, int] = {}
        retry_budget = 3          # 同一账号因瞬时限流最多重试几次
        detail = ""
        account = None
        waiting_since = None
        # 对话指纹：把同一对话钉在同一账号上，保住上游 prompt cache。
        # 失败换号后清空本轮的 key —— 让"下一轮"重新分布，而不是把应急换到的号钉死。
        sticky_key = self.pool.conversation_key(model, body)
        record["sticky"] = "off" if not sticky_key else "miss"
        req_tag = f" req={record['req_id']}"

        def drop_pin(account_obj) -> None:
            """上游判定账号不可用（冷却/额度/凭据/网络）才解钉，让下一轮重新分布。"""
            nonlocal sticky_key
            if sticky_key and self.pool.unpin(sticky_key, account_obj.name):
                print(f"[sticky] {account_obj.name} 不可用 → 解除本对话钉子{req_tag}",
                      flush=True)
            sticky_key = ""

        def switch_only(account_obj, why: str) -> None:
            """本地容量等待（并发位/排队）只换本次请求的号，钉子保留。

            账号没坏，只是本地闸门在排队：解钉会把下一轮赶去另一个账号，
            白丢一份还有效的上游 prompt cache。
            """
            nonlocal sticky_key
            sticky_key = ""
            print(f"[req] {account_obj.name} {why} → 本次请求换号（会话钉子保留）{req_tag}",
                  flush=True)

        while True:
            if account is None:
                trace: dict = {}
                account = self.pool.pick(model, tried, sticky_key=sticky_key, trace=trace)
                if account is None:
                    # 一个都没试过 = 全池在冷却；陪客户端等窗口滑走，别立刻 503
                    if tried:
                        break
                    if waiting_since is None:
                        waiting_since = time.time()
                        print(f"[req] {model or '-'} 全池冷却中，排队等待{req_tag}", flush=True)
                    budget = float(getattr(self.pool.cfg, "queue_wait_seconds", 0) or 0)
                    if time.time() - waiting_since > budget:
                        detail = "排队超时（全池仍在冷却）"
                        record["error_reason"] = "queue-timeout"
                        break
                    time.sleep(0.5)
                    continue
                if trace.get("sticky") == "hit":
                    record["sticky"] = "hit"
                tried.add(account.name)
                attempts.setdefault(account.name, 0)

            provider = self.pool.provider(account)
            adapter = account.adapter
            effective = account.effective(provider)
            key = self.pool.keys.get(account.key_env)
            if not key:
                self.pool.apply(account, ErrorDecision("failover", 600, reason="missing-key"))
                self.pool.note_request(account, model, "missing-key")
                detail = f"{account.name}: 缺凭据"
                record["error_reason"] = "missing-key"
                drop_pin(account)
                record["failovers"] += 1
                account = None
                continue

            # 并发位要一直占到响应（含流）跑完，否则上游看到的并发并没有被真正限制住
            if not self.pool.acquire(account):
                self.pool.note_request(account, model, "concurrency-timeout")
                detail = f"{account.name}: 并发排队超时"
                record["error_reason"] = "concurrency-timeout"
                switch_only(account, "并发排队超时")
                record["failovers"] += 1
                account = None
                continue

            if not self.pool.wait_turn(account):
                self.pool.release(account)
                self.pool.note_request(account, model, "queue-timeout")
                detail = f"{account.name}: 排队超时"
                record["error_reason"] = "queue-timeout"
                switch_only(account, "排队超时")
                record["failovers"] += 1
                account = None
                continue

            gated = account
            record["account"] = gated.name
            record["provider"] = gated.provider_id
            try:
                outcome = self._attempt(method, path, body, stream, model, gated,
                                       adapter, effective, key, inbound, started_at,
                                       attempts, retry_budget, record)
            finally:
                self.pool.release(gated)

            action, detail_out = outcome
            if action == "done":
                return
            if action == "retry":
                continue                      # 同号退避重试：account 保持不变
            drop_pin(gated)                    # 失败换号 → 解除钉子
            record["failovers"] += 1
            account = None                    # 换号
            if detail_out:
                detail = detail_out

        self.pool.note_request(None, model, "no-account")
        payload = {"type": "error", "error": {
            "type": "NoAvailableAccount",
            "message": f"号池里没有可用账号（{detail or '全部冷却 / 额度耗尽'}）",
        }}
        record["status"] = 503
        record["bytes"] = len(json.dumps(payload, ensure_ascii=False).encode())
        if not tried:
            record["outcome"] = "no_account"
        elif record.get("error_reason") == "throttled-persistent":
            record["outcome"] = "throttled"
        else:
            record["outcome"] = "failover"
        self._json(503, payload)

    def _attempt(self, method, path, body, stream, model, account, adapter, effective,
                 key, inbound, started_at, attempts, retry_budget, record=None):
        """跑一次上游请求。返回 ("done"|"retry"|"next", detail)。

        调用方已持有该账号的并发位，本方法不负责释放。
        """
        url = adapter.endpoint(effective) + adapter.map_path(path)
        headers = adapter.build_headers(effective, key, inbound, body)
        req_tag = f" req={record['req_id']}" if record is not None else ""
        t0 = time.time()
        try:
            response = self.pool.http.request(
                method, url, data=body or None, headers=headers,
                stream=stream, timeout=float(self.pool.cfg.upstream_timeout),
            )
        except requests.RequestException as exc:
            if record is not None:
                record["_upstream_s"] += time.time() - t0
                record["error_reason"] = f"net:{type(exc).__name__}"
            self.pool.note_request(account, model, f"net:{type(exc).__name__}")
            self.pool.apply(account, ErrorDecision("failover", 20, reason=f"net:{type(exc).__name__}"))
            return "next", f"{account.name}: {type(exc).__name__}"
        if record is not None:
            record["_upstream_s"] += time.time() - t0
            record["upstream_status"] = response.status_code

        self.pool.note_request(account, model, response.status_code)
        if response.status_code < 400:
            self._relay(account, adapter, response, stream=stream, model=model,
                        started=started_at, record=record)
            return "done", None

        decision = adapter.classify(
            response.status_code, response.content, dict(response.headers), effective)

        if decision.action == "passthrough":
            print(f"[req] {account.name} {model or '-'} {response.status_code} 透传{req_tag}",
                  flush=True)
            if record is not None:
                record["outcome"] = "passthrough"
                record["error_reason"] = decision.detail or f"client:{response.status_code}"
            self._relay(account, adapter, response, stream=False, model=model,
                        started=started_at, record=record)
            return "done", None

        if decision.action == "retry":
            used = attempts.get(account.name, 0)
            response.close()
            if used < retry_budget:
                attempts[account.name] = used + 1
                # 指数退避：2s → 4s → 8s（上游的瞬时限流通常几秒内自解）
                wait = min(8.0, max(2.0, (decision.cooldown_seconds or 1.5) * (2 ** used)))
                print(f"[req] {account.name} {model or '-'} {response.status_code} "
                      f"瞬时限流 → {wait:.1f}s 后同号重试 ({used + 1}/{retry_budget}){req_tag}",
                      flush=True)
                if record is not None:
                    record["retries"] += 1
                    record["error_reason"] = "throttled"
                time.sleep(wait)
                return "retry", None
            print(f"[req] {account.name} {model or '-'} {response.status_code} "
                  f"限流持续 → 冷却 15s 并换号{req_tag}", flush=True)
            if record is not None:
                record["error_reason"] = "throttled-persistent"
            self.pool.apply(account, _cooldown("throttled-persistent", 15.0))
            return "next", f"{account.name}: 限流持续"

        print(f"[req] {account.name} {model or '-'} {response.status_code} "
              f"{decision.reason or 'failover'} → 换号{req_tag}", flush=True)
        if record is not None:
            record["error_reason"] = decision.reason or f"upstream:{response.status_code}"
        response.close()
        self.pool.apply(account, decision)
        return "next", f"{account.name}: {decision.reason or response.status_code}"

    def _relay(self, account, adapter, response, stream: bool,
               model: str | None = None, started: float | None = None,
               record: dict | None = None) -> None:
        status = response.status_code
        relay_started = time.time()
        content_type = response.headers.get("Content-Type", "application/json")
        usage = None

        if stream and status == 200:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type or "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                for name in PASS_RESPONSE_HEADERS:
                    if name.lower() == "content-type":
                        continue
                    value = response.headers.get(name)
                    if value:
                        self.send_header(name, value)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError) as exc:
                response.close()      # 客户端连响应头都不收了：上游资源要还回去
                self._note_disconnect(exc)
                self._mark_client_bytes(0)
                if record is not None:
                    record["_upstream_s"] += time.time() - relay_started
                return
            # ttfb/字节数：以前这两个变量没初始化，流跑完才在日志行上抛 NameError ——
            # 响应已经发完，客户端看不见，只有 relay.log 里堆 traceback。
            first_at = None
            total_bytes = 0
            written = 0
            # 流式记账：只留"最后一条含 usage 的 data: 行"（≤4KB），不缓存整个流。
            # 逐块转发的同时扫描，跨块的行用 pending 拼回来。
            pending = b""
            last_usage_line: bytes | None = None

            def remember_usage_line(line: bytes) -> None:
                nonlocal last_usage_line
                line = line.strip()
                if not line.startswith(b"data:") or b'"usage"' not in line:
                    return
                if len(line) > 4096:      # 异常大的帧不记，避免内存与解析风险
                    return
                last_usage_line = line[5:].strip()

            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    if first_at is None:
                        first_at = time.time()
                    total_bytes += len(chunk)
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    written += len(chunk)
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    pending = pending[-8192:]
                    for line in lines:
                        remember_usage_line(line)
                remember_usage_line(pending)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._note_disconnect(exc)
                self._mark_client_bytes(written)
            finally:
                response.close()
            # 解析失败一律静默：响应已经发完，记账异常不能把请求搞崩
            usage = None
            if last_usage_line:
                try:
                    payload = json.loads(last_usage_line)
                    if isinstance(payload, dict):
                        usage = adapter.extract_usage(payload)
                except (ValueError, TypeError, AttributeError):
                    usage = None
            self.pool.mark_ok(account, usage)
            now = time.time()
            if record is not None:
                if record.get("outcome") != "client_error":
                    record["status"] = status      # 取消过就别用 200 覆盖 499
                record["bytes"] = written
                record["tokens"] = token_fields(usage)
                record["allowance"] = allowance_field(usage)
                record["ttfb_ms"] = round(
                    ((first_at - started) * 1000) if first_at and started else 0.0, 1)
                record["_upstream_s"] += time.time() - relay_started
                if status < 400 and record.get("outcome") is None:
                    record["outcome"] = "ok"
            extra = f" {self._cache_text(record)} sticky={record['sticky']}" if record else ""
            shown_status, shown_bytes = status, total_bytes
            if record is not None and record.get("outcome") == "client_error":
                shown_status = record.get("status") or 499      # 别用 200 描述已取消的请求
                shown_bytes = written                            # 只报实际写出的量
            print(f"[req] {account.name} {model or '-'} {shown_status} stream "
                  f"ttfb={((first_at - started) if first_at and started else 0):.2f}s "
                  f"total={(now - started) if started else 0:.2f}s bytes={shown_bytes}{extra}",
                  flush=True)
            return

        raw = response.content
        response.close()
        # 先记账再回包：否则客户端可能在 mark_ok 之前就收到响应（测试里真的撞到过）
        usage = None
        if status < 400:
            try:
                usage = adapter.extract_usage(json.loads(raw))
            except (ValueError, TypeError, AttributeError):
                usage = None
        self.pool.mark_ok(account, usage)
        if record is not None:
            record["status"] = status
            record["bytes"] = len(raw)
            record["tokens"] = token_fields(usage)
            record["allowance"] = allowance_field(usage)
            record["ttfb_ms"] = round((time.time() - started) * 1000, 1) if started else None
            record["_upstream_s"] += time.time() - relay_started
            if status < 400 and record.get("outcome") is None:
                record["outcome"] = "ok"
        extra = f" {self._cache_text(record)} sticky={record['sticky']}" if record else ""
        print(f"[req] {account.name} {model or '-'} {status} total="
              f"{(time.time() - started) if started else 0:.2f}s bytes={len(raw)}{extra}",
              flush=True)

        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name in PASS_RESPONSE_HEADERS:
                if name.lower() == "content-type":
                    continue
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._note_disconnect(exc)
            self._mark_client_bytes(0)


def serve(pool: Pool) -> None:
    RelayHandler.pool = pool
    host, _, port = (pool.cfg.listen or "127.0.0.1:8790").partition(":")
    httpd = RelayServer((host or "127.0.0.1", int(port or 8790)), RelayHandler)
    print(f"[pool] listening http://{host}:{port}  "
          f"providers={list(pool.cfg.providers)}  accounts={[a.name for a in pool.accounts]}", flush=True)
    for account in pool.accounts:
        pool.refresh(account)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pool._save_state(force=True)
