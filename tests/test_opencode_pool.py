"""号池中转的回归测试（stdlib unittest，不引依赖）。

跑法：
    cd opencode-pool && python3 -m unittest discover -s tests -v

覆盖：配置迁移 / 适配器判定 / 调度选号 / 速率与耗尽推算 / 端到端换号与流式。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = ROOTHERE = ROOT


def _ts_ago(*, hours=0.0):
    """夹具用的**相对**时间戳。

    stats.py 的 --since 是按「现在」算的；夹具里写死日期，写完 24 小时之后整批行就会
    被 --since 24h 筛掉，测试变成定时炸弹（2026-11-08 就是这么挂的）。
    """
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.isoformat(timespec="milliseconds")
sys.path.insert(0, ROOT)

from opencode_pool import adapters  # noqa: E402
from opencode_pool.adapters.base import ErrorDecision, WindowState  # noqa: E402
from opencode_pool.config import Config  # noqa: E402
from opencode_pool.pool import STICKY_MAX, KeyStore, Pool  # noqa: E402
from opencode_pool.secrets import SecretStore, fingerprint  # noqa: E402

adapters.load_all()


class FakeKeyStore:
    """单测用假凭据仓：只认内存映射，**不读 os.environ / ~/.dsh**（B1）。

    注入 `Pool(cfg, keys=...)` 后，后台采样线程与请求路径都用它，
    测试类之间再也不会因为环境变量泄漏而抖动。
    """

    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    def get(self, env_name: str) -> str | None:
        return self.values.get(env_name) if env_name else None

    def source(self, env_name: str) -> str:
        return "pool" if self.get(env_name) else ""

    def describe(self, env_name: str) -> dict:
        value = self.get(env_name) or ""
        return {"slot": env_name, "configured": bool(value), "source": self.source(env_name),
                "fingerprint": fingerprint(value) if value else "", "editable": True}

    def set(self, env_name: str, value: str) -> None:
        self.values[env_name] = value

    def delete(self, env_name: str) -> bool:
        return self.values.pop(env_name, None) is not None


def use_env(testcase, **values):
    """真实 KeyStore 的用例用它设环境变量，class 结束自动恢复。"""
    patcher = mock.patch.dict(os.environ, values)
    patcher.start()
    testcase.addClassCleanup(patcher.stop)
    return patcher


def set_windows(pool, name, **percents):
    """给测试账号铺一组额度采样（默认都还很宽裕）。"""
    account_ = next(a for a in pool.accounts if a.name == name)
    for key, pct in percents.items():
        account_.windows[key] = WindowState(percent=pct, status="ok",
                                            resets_at=time.time() + 3600)
    return account_


def conversation(model: str = "deepseek-v4.1-flash", user: str = "同一个问题") -> bytes:
    return json.dumps({"model": model, "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": user}]}).encode()


def make_config(pool: list[dict], providers: dict | None = None) -> Config:
    return Config.from_dict({
        "version": 2,
        "relay_token": "",
        "credentials_yaml": "/nonexistent",
        "state_file": "",
        "usage_refresh_seconds": 3600,
        "request_log_path": "",          # 单测默认不写 requests.jsonl（要测的用例显式给临时路径）
        "providers": providers or {"opencode-go": {"adapter": "opencode-go"}},
        "accounts": pool,
    })


def account(name: str, key_env: str = "K", provider: str = "opencode-go", **extra) -> dict:
    return {"name": name, "provider": provider, "key_env": key_env, **extra}


class RegistryTest(unittest.TestCase):
    def test_builtin_adapters_registered(self):
        ids = {a.id for a in adapters.all()}
        self.assertIn("opencode-go", ids)
        self.assertIn("openai-compatible", ids)

    def test_descriptor_is_panel_ready(self):
        descriptor = adapters.get("opencode-go").descriptor()
        self.assertTrue(descriptor["supports_quota"])
        self.assertEqual([w["key"] for w in descriptor["windows"]], ["rolling", "weekly", "monthly"])
        secret = [f for f in descriptor["fields"] if f["key"] == "key_env"][0]
        self.assertTrue(secret["sensitive"])


class ConfigTest(unittest.TestCase):
    def test_v1_config_migrates(self):
        cfg = Config.from_dict({
            "listen": "127.0.0.1:1",
            "accounts": [
                {"name": "a", "key_env": "K1", "session_prefix": "p1"},
                {"name": "b", "key_env": "K2"},
            ],
        })
        self.assertEqual(cfg.version, 1)
        self.assertIn("opencode-go", cfg.providers)
        self.assertEqual(cfg.accounts[0]["provider"], "opencode-go")
        self.assertTrue(cfg.warnings)

    def test_unknown_adapter_is_reported(self):
        cfg = make_config([account("a")], providers={"p": {"adapter": "nope"}})
        problems = cfg.validate()
        self.assertTrue(any("未知适配器" in p for p in problems))

    def test_unknown_provider_is_reported(self):
        cfg = make_config([account("a", provider="ghost")])
        self.assertTrue(any("provider ghost" in p for p in cfg.validate()))


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = adapters.get("opencode-go")

    def test_session_header_required_shape(self):
        headers = self.adapter.build_headers(
            {"name": "go02", "session_prefix": "dsh-pool-02"}, "key-123", {"user-agent": "x"})
        self.assertEqual(headers["Authorization"], "Bearer key-123")
        self.assertTrue(headers["x-opencode-session"].startswith("dsh-pool-02-"))
        # 同一会话稳定 → prompt cache 亲和；不同会话分开
        other = self.adapter.build_headers({"name": "go02", "session_prefix": "dsh-pool-02"},
                                          "key-123", {"x-opencode-session": "abc"})
        again = self.adapter.build_headers({"name": "go02", "session_prefix": "dsh-pool-02"},
                                          "key-123", {"x-opencode-session": "abc"})
        self.assertEqual(other["x-opencode-session"], again["x-opencode-session"])

    def test_session_id_is_conversation_scoped(self):
        """会话头必须按对话派生：固定头会让多会话共用缓存域，互相冲掉前缀。"""
        import json as _json

        def body(user):
            return _json.dumps({"model": "m", "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": user}]}).encode()

        acct = {"name": "go02", "session_prefix": "dsh-pool-02"}
        a = self.adapter.session_id(acct, "dsh-pool", body("问题甲"))
        b = self.adapter.session_id(acct, "dsh-pool", body("问题甲"))
        c = self.adapter.session_id(acct, "dsh-pool", body("问题乙"))
        d = self.adapter.session_id({"name": "go01", "session_prefix": "dsh-pool-01"},
                                    "dsh-pool", body("问题甲"))
        self.assertEqual(a, b, "同一对话的多轮必须稳定，否则 prompt cache 不命中")
        self.assertNotEqual(a, c, "不同对话必须分开，否则缓存前缀互相冲掉")
        self.assertNotEqual(a, d, "不同账号必须分开")
        self.assertTrue(a.startswith("dsh-pool-02-"))

    def test_quota_429_is_window_scoped(self):
        body = json.dumps({"error": {"type": "GoUsageLimitError"},
                           "metadata": {"limitName": "weekly"}}).encode()
        decision = self.adapter.classify(429, body, {"Retry-After": "3600"}, {})
        self.assertEqual(decision.action, "failover")
        self.assertEqual(decision.scope, "window")
        self.assertEqual(decision.window, "weekly")

    def test_bare_429_is_transient_throttle_not_account_death(self):
        """实测：额度还有（周 46%/月 23%）时上游照样回裸 429 —— 那是并发限流。
        冷掉唯一账号会让整池 503，所以必须同号退避重试。"""
        decision = self.adapter.classify(429, b'{"error":{"type":"rate_limited"}}', {}, {})
        self.assertEqual(decision.action, "retry")
        self.assertLessEqual(decision.cooldown_seconds, 5)

    def test_region_403_passes_through(self):
        decision = self.adapter.classify(403, b"{}", {}, {})
        self.assertEqual(decision.action, "passthrough")

    def test_401_cools_account(self):
        decision = self.adapter.classify(401, b"{}", {}, {})
        self.assertEqual(decision.action, "failover")
        self.assertEqual(decision.scope, "account")
        self.assertGreaterEqual(decision.cooldown_seconds, 600)


class SchedulerTest(unittest.TestCase):
    def pool_with(self, specs):
        pool = Pool(make_config(specs))
        pool.cfg.usage_refresh_seconds = 36000
        return pool

    def set_windows(self, pool, name, **percents):
        account_ = next(a for a in pool.accounts if a.name == name)
        for key, pct in percents.items():
            account_.windows[key] = WindowState(percent=pct, status="ok", resets_at=time.time() + 3600)

    def test_picks_highest_headroom(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        self.set_windows(pool, "a", rolling=50, weekly=50, monthly=50)
        self.set_windows(pool, "b", rolling=5, weekly=5, monthly=5)
        self.assertEqual(pool.pick().name, "b")

    def test_blocked_window_is_skipped(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        self.set_windows(pool, "a", rolling=1, weekly=100, monthly=10)
        self.set_windows(pool, "b", rolling=80, weekly=80, monthly=80)
        picked = pool.pick()
        self.assertEqual(picked.name, "b")

    def test_headroom_floor_skips_nearly_dead_accounts(self):
        """贴着死线的号不接单：它在需求"先接流再不出字"，比 503 更难排。"""
        cfg = make_config([account("a", "K1"), account("b", "K2")])
        cfg.min_headroom = 10.0
        pool = Pool(cfg)
        pool.cfg.usage_refresh_seconds = 36000
        self.set_windows(pool, "a", rolling=99, weekly=99, monthly=99)   # 余量 1
        self.set_windows(pool, "b", rolling=92, weekly=92, monthly=92)   # 余量 8，仍低于 10
        self.assertIsNone(pool.pick(), "都在水位线以下时应当干脆不接单")
        self.set_windows(pool, "b", rolling=50, weekly=50, monthly=50)   # 余量 50
        self.assertEqual(pool.pick().name, "b")

    def test_unknown_quota_provider_falls_back_to_round_robin(self):
        cfg = make_config(
            [account("g1", "K1", provider="generic"), account("g2", "K2", provider="generic")],
            providers={"generic": {"adapter": "openai-compatible", "base_url": "http://127.0.0.1:1"}})
        pool = Pool(cfg)
        self.assertIsNone(pool.accounts[0].headroom())      # 不提供余量观测
        first = pool.pick()
        pool.mark_ok(first)
        self.assertIsNotNone(pool.pick())

    def test_quota_decision_cools_until_reset(self):
        pool = self.pool_with([account("a", "K1")])
        account_ = pool.accounts[0]
        account_.windows["weekly"] = WindowState(percent=90, status="ok", resets_at=time.time() + 7200)
        pool.apply(account_, ErrorDecision("failover", scope="window", window="weekly", reason="quota:weekly"))
        self.assertEqual(account_.windows["weekly"].status, "rate-limited")
        self.assertAlmostEqual(account_.cooldown_until - time.time(), 7200, delta=60)
        self.assertLess(account_.headroom(), 0)

    def test_rate_and_eta(self):
        pool = self.pool_with([account("a", "K1")])
        account_ = pool.accounts[0]
        now = time.time()
        account_.history = [
            {"t": now - 3600, "monthly": 80.0},
            {"t": now - 1800, "monthly": 82.0},
            {"t": now, "monthly": 84.0},
        ]
        account_.windows["monthly"] = WindowState(percent=84, status="ok")
        derived = pool.derive(account_)
        self.assertAlmostEqual(derived["rate_per_hour"]["monthly"], 4.0, places=3)
        self.assertAlmostEqual(derived["eta_seconds"]["monthly"] / 3600, 4.0, places=2)
        self.assertEqual(derived["binding_window"], "monthly")


# ── 端到端：假上游 ──────────────────────────────────────────────────────
class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls: list[str] = []

    def log_message(self, *args):
        return

    def _send(self, code, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/usage"):
            return self._send(200, {"usage": {
                "rolling": {"percent": 0, "status": "ok", "resetsAt": "2030-01-01T00:00:00.000Z"},
                "weekly": {"percent": 0, "status": "ok", "resetsAt": "2030-01-01T00:00:00.000Z"},
                "monthly": {"percent": 0, "status": "ok", "resetsAt": "2030-01-01T00:00:00.000Z"},
            }})
        return self._send(404, {})

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(size)
        key = self.headers.get("Authorization", "").removeprefix("Bearer ")
        FakeUpstream.calls.append(key)
        if key == "k-dead":
            return self._send(429,
                              {"error": {"type": "GoUsageLimitError"},
                               "metadata": {"limitName": "weekly"}},
                              {"Retry-After": "600"})
        if key == "k-bad":
            return self._send(401, {"error": {"type": "auth"}})
        body = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"opencode": {
                "allowance": 0.5, "billing_source": "go",
                # 合成数据：真实上游对短 prompt 的 cache_read 恒为 0（带 tools 的长
                # prompt 才可能非零），这里刻意给非零值来验证累计逻辑本身。
                "tokens": {"input": 120, "output": 10, "reasoning": 4,
                           "cache_read": 80, "cache_write_5m": 40, "cache_write_1h": 0},
            }},
        }
        return self._send(200, body)


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"

        cfg = make_config(
            [account("dead", "K_DEAD"), account("live", "K_LIVE")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": cls.upstream_url}})
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_DEAD": "k-dead", "K_LIVE": "k-live"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)

        from opencode_pool.server import RelayHandler
        cls.handler = RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.relay_url = f"http://127.0.0.1:{cls.relay.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def test_failover_on_quota_429(self):
        import requests

        FakeUpstream.calls.clear()
        dead = next(a for a in self.pool.accounts if a.name == "dead")
        resp = requests.post(f"{self.relay_url}/v1/chat/completions",
                             json={"model": "deepseek-v4.1-flash",
                                   "messages": [{"role": "user", "content": "hi"}]}, timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["choices"][0]["message"]["content"], "ok")
        self.assertEqual(FakeUpstream.calls, ["k-dead", "k-live"])
        self.assertEqual(dead.windows["weekly"].status, "rate-limited")
        self.assertGreater(dead.cooldown_until, time.time())
        self.assertAlmostEqual(next(a for a in self.pool.accounts if a.name == "live").allowance_used,
                               0.5, places=3)

    def test_usage_endpoint_shape(self):
        import requests

        payload = requests.get(f"{self.relay_url}/v1/usage", timeout=10).json()
        self.assertIn("opencode-go", payload["upstreams"])
        self.assertEqual(payload["total_accounts"], 2)
        account_ = payload["accounts"][0]
        for key in ("headroom", "percent", "rate_per_hour", "eta_seconds", "binding_window"):
            self.assertIn(key, account_)

    def test_schema_endpoint_lists_adapter_windows(self):
        import requests

        payload = requests.get(f"{self.relay_url}/v1/pool/schema", timeout=10).json()
        provider = payload["providers"]["opencode-go"]
        self.assertEqual([w["key"] for w in provider["windows"]], ["rolling", "weekly", "monthly"])

    def test_missing_token_is_401(self):
        import requests

        self.pool.cfg.relay_token = "secret"
        try:
            resp = requests.get(f"{self.relay_url}/v1/usage", timeout=10)
            self.assertEqual(resp.status_code, 401)
        finally:
            self.pool.cfg.relay_token = ""


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CliTest(unittest.TestCase):
    """命令行必须容忍 `serve --config X`（插件与 start.sh 就是这种写法）。"""

    def test_config_accepted_on_both_sides(self):
        from opencode_pool.cli import build_parser

        parser = build_parser()
        first = parser.parse_args(["--config", "/tmp/a.json", "status"])
        second = parser.parse_args(["status", "--config", "/tmp/b.json"])
        self.assertEqual(first.config, "/tmp/a.json")
        self.assertEqual(second.config, "/tmp/b.json")

    def test_config_default_kept_when_absent(self):
        from opencode_pool.cli import DEFAULT_CONFIG, build_parser

        args = build_parser().parse_args(["check"])
        self.assertEqual(args.config, DEFAULT_CONFIG)


# ── 面板改配置：加号 / 换 key / 删号 ────────────────────────────────
class AccountLifecycleTest(unittest.TestCase):
    """面板能改配置的前提：写回对的那份文件、密钥不回显、越权要拒绝。"""

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = tempfile.mkdtemp(prefix="pool-test-")
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"

        cls.config_file = os.path.join(cls.tmp, "pool.config.json")
        cls.secrets_file = os.path.join(cls.tmp, "secrets.json")
        use_env(cls, K_BASE="k-live")     # 真实 KeyStore 要验证 env 槽位，用 fixture 管
        with open(cls.config_file, "w", encoding="utf-8") as f:
            json.dump({
                "version": 2,
                "listen": "127.0.0.1:0",
                "relay_token": "t0k",
                "credentials_yaml": "/nonexistent",
                "secrets_file": cls.secrets_file,
                "state_file": "",
                "request_log_path": "",
                "usage_refresh_seconds": 36000,
                "providers": {"opencode-go": {"adapter": "opencode-go", "base_url": upstream_url}},
                "accounts": [{"name": "base", "provider": "opencode-go", "key_env": "K_BASE"}],
            }, f)

        cfg = Config.load(cls.config_file)
        cls.pool = Pool(cfg)
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)

        from opencode_pool import server as server_mod

        cls.server_mod = server_mod
        cls.handler = server_mod.RelayHandler
        server_mod.RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.relay.server_port}"
        cls.headers = {"Authorization": "Bearer t0k"}

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def post(self, path, payload):
        import requests

        return requests.post(f"{self.base_url}{path}", json=payload, headers=self.headers, timeout=10)

    def live_pool(self):
        """改配置会重建号池，测试要跟着用当前那一份。"""
        return self.server_mod.RelayHandler.pool

    def account(self, name):
        snapshot = self.server_mod.RelayHandler.pool.snapshot()
        return next((a for a in snapshot["accounts"] if a["name"] == name), None)

    def test_01_add_account_writes_own_config_and_secret(self):
        before = json.load(open(self.config_file))
        production = os.path.expanduser("~/.dsh/opencode-pool/pool.config.json")
        prod_before = json.load(open(production)) if os.path.exists(production) else None

        resp = self.post("/v1/pool/accounts", {"name": "go03", "provider": "opencode-go", "key": "k-new"})
        self.assertEqual(resp.status_code, 200, resp.text)
        slot = resp.json()["slot"]
        self.assertEqual(slot, "POOL_KEY_GO03")

        after = json.load(open(self.config_file))
        self.assertIn("go03", [a["name"] for a in after["accounts"]])
        self.assertNotIn("go03", [a["name"] for a in before["accounts"]])

        # 回归：写回必须落在本实例的配置上，绝不能碰生产配置
        if prod_before is not None:
            self.assertEqual(json.load(open(production)), prod_before)

        # 密钥仓：0600，且密钥能被解析出来
        mode = oct(os.stat(self.secrets_file).st_mode)[-3:]
        self.assertEqual(mode, "600")
        self.assertEqual(self.server_mod.RelayHandler.pool.keys.get(slot), "k-new")

        account_ = self.account("go03")
        self.assertTrue(account_["key"]["configured"])
        self.assertEqual(account_["key"]["source"], "pool")
        self.assertNotIn("k-new", json.dumps(account_))

    def test_02_rotate_key_changes_fingerprint(self):
        self.post("/v1/pool/accounts", {"name": "go04", "provider": "opencode-go", "key": "k-old"})
        first = self.account("go04")["key"]["fingerprint"]
        resp = self.post("/v1/pool/account/key", {"name": "go04", "key": "k-rotated"})
        self.assertEqual(resp.status_code, 200, resp.text)
        second = self.account("go04")["key"]["fingerprint"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 8)
        self.assertNotIn("k-rotated", json.dumps(self.account("go04")))

    def test_03_env_slot_cannot_be_overridden(self):
        resp = self.post("/v1/pool/account/key", {"name": "base", "key": "whatever"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("环境变量", resp.json()["error"])

    def test_04_remove_account_recycles_secret(self):
        self.post("/v1/pool/accounts", {"name": "go05", "provider": "opencode-go", "key": "k-gone"})
        slot = "POOL_KEY_GO05"
        self.assertIsNotNone(self.live_pool().keys.get(slot))
        resp = self.post("/v1/pool/account/remove", {"name": "go05"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["secret_dropped"])
        self.assertIsNone(self.live_pool().keys.get(slot))
        self.assertIsNone(self.account("go05"))

    def test_05_validation_errors(self):
        dup = self.post("/v1/pool/accounts", {"name": "base", "provider": "opencode-go", "key": "x"})
        self.assertEqual(dup.status_code, 409)
        empty = self.post("/v1/pool/accounts", {"name": "zz", "provider": "opencode-go", "key": ""})
        self.assertEqual(empty.status_code, 400)
        bad_provider = self.post("/v1/pool/accounts", {"name": "zz2", "provider": "nope", "key": "x"})
        self.assertEqual(bad_provider.status_code, 400)
        bad_name = self.post("/v1/pool/accounts", {"name": "has space", "provider": "opencode-go", "key": "x"})
        self.assertEqual(bad_name.status_code, 400)


# ── 会话粘性（sticky）──────────────────────────────────────────────────
class StickyRoutingTest(unittest.TestCase):
    """同一对话钉同一账号，保住上游 prompt cache；号坏了才让位。"""

    def pool_with(self, specs, **overrides):
        cfg = make_config(specs)
        cfg.usage_refresh_seconds = 36000
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        for key, value in overrides.items():
            setattr(cfg, key, value)
        pool = Pool(cfg)
        for account_ in pool.accounts:
            set_windows(pool, account_.name, rolling=0, weekly=0, monthly=0)
        return pool

    def test_default_ttl_is_12_hours_and_configurable(self):
        self.assertEqual(make_config([]).sticky_ttl_seconds, 43200,
                         "默认 12 小时：DeepSeek 上游缓存保留小时级，TTL 只是安全上界")
        self.assertEqual(Config.from_dict({"version": 2, "sticky_ttl_seconds": 0,
                                           "accounts": []}).sticky_ttl_seconds, 0)
        # 写回配置不能把粘性设置弄丢（面板改一次号就关掉粘性会很隐蔽）
        self.assertEqual(make_config([]).to_raw()["sticky_ttl_seconds"], 43200)
        self.assertEqual(make_config([]).to_dict()["sticky_ttl_seconds"], 43200)

    def test_conversation_key_is_stable_opaque_and_content_free(self):
        pool = self.pool_with([account("a", "K1")])
        first = pool.conversation_key("m", conversation(user="秘密问题甲"))
        again = pool.conversation_key("m", conversation(user="秘密问题甲"))
        other = pool.conversation_key("m", conversation(user="秘密问题乙"))
        other_model = pool.conversation_key("m2", conversation(model="m2", user="秘密问题甲"))
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, other_model)
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertNotIn("秘密", first, "映射表里只能有摘要，不能有 prompt 正文")
        self.assertEqual(pool.conversation_key("m", b""), "")

    def test_sticky_can_be_disabled(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")], sticky_ttl_seconds=0)
        self.assertEqual(pool.conversation_key("m", conversation()), "")
        # key 为空 → 每轮独立选号：a 用过一轮后轮到 b
        self.assertEqual(pool.pick("m").name, "a")
        pool.mark_ok(next(a for a in pool.accounts if a.name == "a"))
        self.assertEqual(pool.pick("m").name, "b")

    def test_first_pick_pins_and_later_picks_hit(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)      # 余量 100 → 首轮选中 a
        set_windows(pool, "b", rolling=10, weekly=10, monthly=10)   # 余量 90
        key = pool.conversation_key("m", conversation())

        first = pool.pick("m", sticky_key=key)
        self.assertEqual(first.name, "a")
        self.assertEqual(pool.sticky_misses, 1)
        pool.mark_ok(first)                 # 没有粘性的话，下一轮会轮到 b

        for _ in range(2):
            hit = pool.pick("m", sticky_key=key)
            self.assertEqual(hit.name, "a", "同一对话的后续轮次必须还在同一账号")
            pool.mark_ok(hit)

        self.assertEqual(pool.sticky_hits, 2)
        self.assertEqual(pool.sticky_misses, 1)
        self.assertEqual(pool.snapshot()["sticky"],
                         {"ttl_seconds": 43200.0, "pinned": 1, "hits": 2, "misses": 1})

    def test_different_conversations_are_pinned_separately(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        # 两号余量相同：没有粘性时按 last_used 轮转，才能看出"对话各自钉住"
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)
        set_windows(pool, "b", rolling=0, weekly=0, monthly=0)
        key1 = pool.conversation_key("m", conversation(user="会话一"))
        key2 = pool.conversation_key("m", conversation(user="会话二"))

        first = pool.pick("m", sticky_key=key1)
        pool.mark_ok(first)
        second = pool.pick("m", sticky_key=key2)
        pool.mark_ok(second)

        self.assertNotEqual(first.name, second.name)
        self.assertEqual(pool._sticky[key1][1], first.name)
        self.assertEqual(pool._sticky[key2][1], second.name)
        self.assertEqual(pool.pick("m", sticky_key=key1).name, first.name)
        self.assertEqual(pool.pick("m", sticky_key=key2).name, second.name)

    def test_unavailable_account_is_replaced(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)
        set_windows(pool, "b", rolling=10, weekly=10, monthly=10)
        key = pool.conversation_key("m", conversation())
        pinned = pool.pick("m", sticky_key=key)
        self.assertEqual(pinned.name, "a")

        pool.apply(pinned, ErrorDecision("failover", 120, reason="upstream:500"))
        self.assertLess(pinned.headroom(), 0)

        replacement = pool.pick("m", sticky_key=key)
        self.assertEqual(replacement.name, "b", "钉住的号不可用时必须重选")
        self.assertEqual(pool._sticky[key][1], "b", "钉子要跟着更新")
        self.assertEqual(pool.sticky_hits, 0)
        self.assertGreaterEqual(pool.sticky_misses, 2)

    def test_below_reserve_pin_is_replaced(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")], min_headroom=10.0)
        set_windows(pool, "a", rolling=90, weekly=0, monthly=0)     # 余量 10，刚够
        set_windows(pool, "b", rolling=10, weekly=10, monthly=10)   # 余量 90
        key = pool.conversation_key("m", conversation())
        # 首轮选中 b（余量高）；把它压到水位线以下后必须换号
        pinned = pool.pick("m", sticky_key=key)
        self.assertEqual(pinned.name, "b")
        set_windows(pool, "b", rolling=99, weekly=99, monthly=99)
        replacement = pool.pick("m", sticky_key=key)
        self.assertEqual(replacement.name, "a")

    def test_cooling_wait_does_not_inflate_misses(self):
        pool = self.pool_with([account("a", "K1")])
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)
        key = pool.conversation_key("m", conversation())
        self.assertEqual(pool.pick("m", sticky_key=key).name, "a")
        with pool.lock:
            pool.accounts[0].cooldown_until = time.time() + 600

        misses = pool.sticky_misses
        for _ in range(5):        # _proxy 等冷却时每 0.5s 探一次 pick
            self.assertIsNone(pool.pick("m", sticky_key=key))
        self.assertEqual(pool.sticky_misses, misses,
                         "全池冷却期间的轮询不该灌进 sticky 指标")

    def test_unpin_lets_next_turn_redistribute(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)
        set_windows(pool, "b", rolling=10, weekly=10, monthly=10)
        key = pool.conversation_key("m", conversation())
        pool.pick("m", sticky_key=key)

        self.assertFalse(pool.unpin(key, "b"), "钉子钉的是 a，不该被别的账号名误删")
        self.assertTrue(pool.unpin(key, "a"))
        self.assertFalse(pool.unpin(key, "a"), "已经解除了，再解一次要返回 False")
        self.assertFalse(pool.unpin("", None))

        hits_before = pool.sticky_hits
        pool.pick("m", sticky_key=key)
        self.assertEqual(pool.sticky_hits, hits_before, "解除后下一轮必须是新选号")

    def test_expired_pin_is_reselected(self):
        pool = self.pool_with([account("a", "K1"), account("b", "K2")])
        set_windows(pool, "a", rolling=0, weekly=0, monthly=0)      # 余量 100
        set_windows(pool, "b", rolling=10, weekly=10, monthly=10)   # 余量 90
        key = pool.conversation_key("m", conversation())
        pool._sticky[key] = (time.time() - 1, "b")                  # 过期钉子指向 b

        picked = pool.pick("m", sticky_key=key)
        self.assertEqual(picked.name, "a")
        self.assertGreater(pool._sticky[key][0], time.time(), "重选后钉子要续期")

    def test_sticky_map_is_bounded(self):
        pool = self.pool_with([account("a", "K1")])
        now = time.time()
        for i in range(STICKY_MAX + 10):
            pool._sticky[f"live{i:05d}"] = (now + 1000 + i, "a")
        pool.pick("m", sticky_key="live00000")
        self.assertLessEqual(len(pool._sticky), STICKY_MAX)

        pool._sticky.clear()
        for i in range(STICKY_MAX + 10):
            pool._sticky[f"dead{i:05d}"] = (now - 1, "a")
        pool.pick("m", sticky_key="dead00000")
        self.assertEqual(len(pool._sticky), 1, "过期钉子全清，只剩刚钉上的一个")

    def test_inherit_carries_pins_and_counters(self):
        """面板"重载配置"会重建号池：粘性钉子和指标不能因此归零。"""
        old = self.pool_with([account("a", "K1"), account("b", "K2")])
        key = old.conversation_key("m", conversation())
        old.pick("m", sticky_key=key)
        old.note_request(old.accounts[0], "m", 200)

        fresh = self.pool_with([account("a", "K1"), account("b", "K2")])
        fresh.inherit(old)
        self.assertIn(key, fresh._sticky)
        self.assertEqual(fresh.sticky_misses, old.sticky_misses)
        self.assertEqual(fresh._requests, old._requests)
        self.assertEqual(fresh.pick("m", sticky_key=key).name, old.accounts[0].name)

    def test_inherit_keeps_cache_counters(self):
        """B5：面板重载重建 Pool 后，缓存累计不能归零。"""
        old = self.pool_with([account("a", "K1")])
        old.mark_ok(old.accounts[0], {"tokens": {"input": 30, "cache_read": 70}})

        fresh = self.pool_with([account("a", "K1")])
        self.assertEqual(fresh.accounts[0].cache_read_tokens, 0.0)
        fresh.inherit(old)
        self.assertEqual(fresh.accounts[0].cache_read_tokens, 70.0)
        self.assertEqual(fresh.accounts[0].input_tokens, 30.0)
        snap = fresh.snapshot()["accounts"][0]
        self.assertAlmostEqual(snap["cache_hit_ratio"], round(70 / 100, 4), places=4)

    def test_prune_keeps_entries_that_are_still_alive(self):
        pool = self.pool_with([account("a", "K1")])
        now = time.time()
        pool._sticky["alive"] = (now + 600, "a")
        pool._sticky["dead"] = (now - 1, "a")
        with pool.lock:
            pool._prune_sticky(now)
        self.assertIn("alive", pool._sticky)
        self.assertNotIn("dead", pool._sticky)


# ── /metrics ───────────────────────────────────────────────────────────
SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (-?[0-9.eE+]+)$")
REQUIRED_FAMILIES = (
    "pool_account_headroom",
    "pool_account_window_percent",
    "pool_requests_total",
    "pool_failovers_total",
    "pool_cooling_seconds",
    "pool_cache_read_tokens_total",
    "pool_input_tokens_total",
    "pool_sticky_hits_total",
    "pool_sticky_misses_total",
)


class MetricsTest(unittest.TestCase):
    def pool(self):
        cfg = make_config([account("go01", "K1"), account("go02", "K2")])
        cfg.usage_refresh_seconds = 36000
        pool = Pool(cfg)
        set_windows(pool, "go01", rolling=10, weekly=40, monthly=60)
        set_windows(pool, "go02", rolling=99, weekly=99, monthly=99)
        return pool

    def assert_prometheus(self, text):
        """每一行样本都得能被 Prometheus 文本格式吃掉，且同一标签集合只出现一次。"""
        families = set()
        seen = set()
        for line in text.splitlines():
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# TYPE "):
                    families.add(line.split()[2])
                continue
            match = SAMPLE_RE.match(line)
            self.assertIsNotNone(match, f"不是合法的 Prometheus 样本行：{line!r}")
            metric, labels, value = match.groups()
            key = (metric, labels or "")
            self.assertNotIn(key, seen, f"同一指标重复输出标签集合：{line!r}")
            seen.add(key)
            float(value)
        return families

    def test_metrics_text_is_valid_and_complete(self):
        pool = self.pool()
        pool.note_request(pool.accounts[0], "deepseek-v4.1-flash", 200)
        pool.note_request(pool.accounts[0], "deepseek-v4.1-flash", 200)
        pool.note_failover(pool.accounts[1], "quota:weekly")
        pool.apply(pool.accounts[1], ErrorDecision("failover", 120, reason="upstream:503"))

        text = pool.metrics_text()
        families = self.assert_prometheus(text)
        for family in REQUIRED_FAMILIES:
            self.assertIn(family, families, f"缺少指标族 {family}")

        self.assertIn('pool_account_headroom{account="go01",provider="opencode-go"} 40', text)
        self.assertIn('pool_account_window_percent{account="go01",window="weekly"} 40', text)
        self.assertIn('pool_requests_total{account="go01",model="deepseek-v4.1-flash",'
                      'status="200"} 2', text)
        self.assertIn('pool_failovers_total{account="go02",reason="quota:weekly"} 1', text)
        self.assertIn('pool_failovers_total{account="go02",reason="upstream:503"} 1', text)
        self.assertIn('pool_cooling_seconds{account="go02"} ', text)
        self.assertIn("pool_sticky_hits_total 0", text)
        self.assertIn("pool_sticky_misses_total 0", text)

    def test_headroom_zero_is_not_mistaken_for_dead(self):
        pool = self.pool()
        set_windows(pool, "go01", rolling=100, weekly=100, monthly=100)  # 打满 → -1
        text = pool.metrics_text()
        self.assertIn('pool_account_headroom{account="go01",provider="opencode-go"} -1', text)
        self.assert_prometheus(text)

    def test_label_values_are_escaped(self):
        cfg = make_config([account('go"01', "K1")])
        cfg.usage_refresh_seconds = 36000
        pool = Pool(cfg)
        set_windows(pool, 'go"01', rolling=1, weekly=1, monthly=1)
        pool.note_request(pool.accounts[0], 'weird"model\\x', 200)

        text = pool.metrics_text()
        self.assertIn(r'account="go\"01"', text)
        self.assertIn(r'model="weird\"model\\x"', text)
        self.assert_prometheus(text)

    def test_headroom_series_omitted_for_unknown_quota(self):
        cfg = make_config(
            [account("vllm", "K1", provider="generic")],
            providers={"generic": {"adapter": "openai-compatible",
                                   "base_url": "http://127.0.0.1:1"}})
        pool = Pool(cfg)
        text = pool.metrics_text()
        self.assertNotIn('pool_account_headroom{account="vllm"', text,
                         "不提供余量观测的适配器不该出 headroom 序列")
        self.assertIn('pool_cooling_seconds{account="vllm"} 0', text)
        self.assert_prometheus(text)

    def test_cache_tokens_are_counted_and_exported(self):
        pool = self.pool()
        account_ = pool.accounts[0]
        pool.mark_ok(account_, {"allowance": 0.1, "tokens": {"input": 120, "cache_read": 80}})
        pool.mark_ok(account_, {"tokens": {"input": 30, "cache_read": 0}})
        pool.mark_ok(account_, {"tokens": {"input": "bad", "cache_read": None}})  # 脏数据不炸

        snap = [a for a in pool.snapshot()["accounts"] if a["name"] == "go01"][0]
        self.assertEqual(snap["cache_read_tokens"], 80)
        self.assertEqual(snap["input_tokens"], 150)
        # B6：上游 input 是“未命中缓存”的部分，分母要算上 cache_read
        self.assertAlmostEqual(snap["cache_hit_ratio"], round(80 / (80 + 150), 4), places=4)

        text = pool.metrics_text()
        self.assertIn('pool_cache_read_tokens_total{account="go01"} 80', text)
        self.assertIn('pool_input_tokens_total{account="go01"} 150', text)
        self.assert_prometheus(text)

    def test_cache_hit_ratio_is_bounded_when_cache_read_exceeds_input(self):
        """旧口径 cache_read/input 会算出 >1；新口径必须落在 [0,1]。"""
        pool = self.pool()
        account_ = pool.accounts[0]
        pool.mark_ok(account_, {"tokens": {"input": 100, "cache_read": 300}})
        snap = [a for a in pool.snapshot()["accounts"] if a["name"] == "go01"][0]
        self.assertAlmostEqual(snap["cache_hit_ratio"], round(300 / (300 + 100), 4), places=4)
        self.assertLessEqual(snap["cache_hit_ratio"], 1.0)

    def test_sticky_counters_are_exported(self):
        pool = self.pool()
        key = pool.conversation_key("m", conversation())
        first = pool.pick("m", sticky_key=key)
        pool.mark_ok(first)
        pool.pick("m", sticky_key=key)
        text = pool.metrics_text()
        self.assertIn("pool_sticky_hits_total 1", text)
        self.assertIn("pool_sticky_misses_total 1", text)


# ── /healthz 就绪语义 ──────────────────────────────────────────────────
class ReadinessTest(unittest.TestCase):
    """每个用例自己造状态：注入假 KeyStore + 假上游，完全不碰 os.environ；
    建池后等后台首轮采样落定，再改状态，避免和后台线程抢时序（B1）。"""

    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"
        cls.addClassCleanup(cls.upstream.shutdown)

    def pool(self, specs=None, keys=None, **overrides):
        cfg = make_config(
            specs or [account("a", "K_READY")],
            providers={"opencode-go": {"adapter": "opencode-go",
                                       "base_url": self.upstream_url}})
        cfg.usage_refresh_seconds = 90
        cfg.min_headroom = 2.0
        for key, value in overrides.items():
            setattr(cfg, key, value)
        pool = Pool(cfg, keys=keys if keys is not None else FakeKeyStore({"K_READY": "k-ready"}))
        for account_ in pool.accounts:          # 同步采一轮，状态先钉住
            pool.refresh(account_)
        return pool

    def no_quota_pool(self, keys, key_env="K_VLLM"):
        cfg = make_config(
            [account("vllm", key_env, provider="generic")],
            providers={"generic": {"adapter": "openai-compatible",
                                   "base_url": self.upstream_url}})
        cfg.usage_refresh_seconds = 90
        pool = Pool(cfg, keys=keys)
        for account_ in pool.accounts:
            pool.refresh(account_)
        return pool

    @staticmethod
    def settle(pool, samples=2, timeout=3.0):
        """等后台线程的首轮采样也结束；之后 ≥90s 才会再采，用例期间稳定。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(len(a.history) >= samples for a in pool.accounts):
                return
            time.sleep(0.01)
        raise AssertionError("后台首轮采样没有在 3s 内完成")

    def test_ready_when_fresh(self):
        pool = self.pool()
        self.settle(pool)
        ok, body = pool.readiness()
        self.assertTrue(ok)
        self.assertEqual(body["reason"], "ready")
        self.assertEqual(body["accounts"]["ready"], 1)
        self.assertEqual(body["accounts"]["total"], 1)
        self.assertEqual(body["fresh_within_seconds"], 180, "2 × usage_refresh_seconds")
        self.assertEqual(body["accounts"]["no_credentials"], 0)
        self.assertLess(body["oldest_sample_age_seconds"], 5)

    def test_no_accounts_is_not_ready(self):
        pool = Pool(make_config([]), keys=FakeKeyStore())
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "no-accounts")
        self.assertEqual(body["accounts"]["total"], 0)

    def test_stale_sample_is_not_ready(self):
        """凭据存在（假 KeyStore 有 key）时把采样推老 → 必须归因 usage-stale。"""
        pool = self.pool()
        self.settle(pool)
        pool.accounts[0].last_refresh = time.time() - 1000
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "usage-stale")
        self.assertEqual(body["accounts"]["stale"], 1)
        self.assertEqual(body["accounts"]["no_credentials"], 0)
        self.assertGreaterEqual(body["oldest_sample_age_seconds"], 1000)

    def test_never_sampled_is_not_ready(self):
        pool = self.pool()
        self.settle(pool)
        pool.accounts[0].last_refresh = 0.0        # 模拟“启动后还没采到样”
        pool.accounts[0].refresh_error = ""
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "usage-not-sampled")
        self.assertEqual(body["never_sampled"], 1)
        self.assertEqual(body["accounts"]["stale"], 1)

    def test_all_cooling_is_not_ready(self):
        pool = self.pool()
        self.settle(pool)
        pool.accounts[0].cooldown_until = time.time() + 600
        pool.accounts[0].cooldown_reason = "upstream:503"
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "all-cooling")
        self.assertEqual(body["accounts"]["cooling"], 1)

    def test_missing_credentials_has_its_own_reason(self):
        """没配凭据不是“采样没跑”：独立 reason，只报计数、不回槽位名。"""
        pool = self.pool([account("nokey", "K_ABSENT_FOR_T6_TEST")], keys=FakeKeyStore())
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "missing-credentials")
        self.assertEqual(body["accounts"]["no_credentials"], 1)
        self.assertEqual(body["accounts"]["stale"], 0, "缺凭据不该被算成采样过期")
        self.assertEqual(body["never_sampled"], 0)
        self.assertNotIn("K_ABSENT_FOR_T6_TEST", json.dumps(body, ensure_ascii=False))

    def test_no_quota_adapter_without_credentials_is_not_ready(self):
        """B4：没有额度观测的适配器缺凭据也必须 503，不能报 ready 然后请求 503。"""
        pool = self.no_quota_pool(FakeKeyStore({}))
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "missing-credentials")
        self.assertEqual(body["accounts"]["no_credentials"], 1)

    def test_no_quota_adapter_with_credentials_is_ready(self):
        pool = self.no_quota_pool(FakeKeyStore({"K_VLLM": "vllm-key"}))
        ok, body = pool.readiness()
        self.assertTrue(ok)
        self.assertEqual(body["reason"], "ready")
        self.assertEqual(body["accounts"]["no_quota"], 1)
        self.assertEqual(body["accounts"]["no_credentials"], 0)

    def test_no_quota_adapter_without_key_env_needs_no_credentials(self):
        pool = self.no_quota_pool(FakeKeyStore({}), key_env="")
        ok, body = pool.readiness()
        self.assertTrue(ok, "key_env 为空 = 该适配器不需要凭据")
        self.assertEqual(body["accounts"]["no_credentials"], 0)

    def test_all_below_reserve_is_not_ready(self):
        pool = self.pool()
        self.settle(pool)
        set_windows(pool, "a", rolling=99, weekly=0, monthly=0)   # 余量 1 < 2
        ok, body = pool.readiness()
        self.assertFalse(ok)
        self.assertEqual(body["reason"], "all-below-reserve")
        self.assertEqual(body["accounts"]["below_reserve"], 1)

    def test_body_has_no_secret_material(self):
        pool = self.pool()
        self.settle(pool)
        ok, body = pool.readiness()
        self.assertTrue(ok)
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("K_READY", text, "只报计数，不回凭据槽位")
        self.assertNotIn("k-ready", text, "响应体不能出现凭据值")
        self.assertNotIn("key", text.lower().replace("monkey", ""),
                         "文案里不许出现 key 字样")


# ── 流式响应：ttfb/bytes + usage 记账（回归：NameError 只在日志里可见）──
class StreamRelayTest(unittest.TestCase):
    USAGE_FRAME = (b'data: {"choices":[{"delta":{"content":"ok"}}],'
                   b'"usage":{"opencode":{"allowance":0.25,'
                   b'"tokens":{"input":40,"output":5,"cache_read":80}}}}\n\n')

    def run_stream(self, pool, chunks):
        """直接驱动 _relay 的流式分支：返回 (handler, 日志, 假响应)。"""
        from opencode_pool.server import RelayHandler

        class FakeStreamResponse:
            status_code = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.closed = False

            def iter_content(self, chunk_size=8192):
                for chunk in chunks:
                    yield chunk

            def close(self):
                self.closed = True

        handler = RelayHandler.__new__(RelayHandler)
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.requestline = "POST /v1/chat/completions HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 0)
        handler.pool = pool

        response = FakeStreamResponse()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handler._relay(pool.accounts[0], adapters.get("opencode-go"), response,
                           stream=True, model="m", started=time.time())
        return handler, output.getvalue(), response

    def pool(self):
        return Pool(make_config([account("a", "K1")]), keys=FakeKeyStore({"K1": "k"}))

    def test_stream_branch_logs_ttfb_and_bytes(self):
        pool = self.pool()
        handler, log, response = self.run_stream(
            pool, [b'data: {"delta":"hi"}\n\n', b"data: [DONE]\n\n"])

        raw = handler.wfile.getvalue()
        self.assertTrue(raw.endswith(b"0\r\n\r\n"), "chunked 结束标记必须写出")
        self.assertIn(b"data: [DONE]", raw)
        self.assertIn("stream ttfb=", log)
        self.assertIn("bytes=", log)
        self.assertNotIn("NameError", log)
        self.assertTrue(response.closed)
        self.assertEqual(pool.accounts[0].requests, 1)

    def test_stream_usage_frame_is_accounted(self):
        """B3：流式末帧的 usage 必须计入缓存/额度指标（否则主流量指标恒为 0）。"""
        pool = self.pool()
        account_ = pool.accounts[0]
        before = (account_.cache_read_tokens, account_.input_tokens, account_.allowance_used)

        handler, _log, _resp = self.run_stream(
            pool, [b'data: {"delta":"hi"}\n\n', self.USAGE_FRAME, b"data: [DONE]\n\n"])

        self.assertIn(b'"usage"', handler.wfile.getvalue(), "原始 SSE 仍要原样转发")
        after = (account_.cache_read_tokens, account_.input_tokens, account_.allowance_used)
        self.assertEqual(after[0] - before[0], 80, "cache_read 增量必须等于 SSE 帧里的值")
        self.assertEqual(after[1] - before[1], 40, "input 增量必须等于 SSE 帧里的值")
        self.assertAlmostEqual(after[2] - before[2], 0.25, places=4)
        snap = pool.snapshot()["accounts"][0]
        self.assertAlmostEqual(snap["cache_hit_ratio"], round(80 / 120, 4), places=4)

    def test_stream_usage_frame_split_across_chunks(self):
        """跨块的行也要能拼回来（SSE 帧不保证落在同一个 TCP chunk 里）。"""
        pool = self.pool()
        frame = self.USAGE_FRAME
        chunks = [b'data: {"delta":"hi"}\n\n', frame[:17], frame[17:53], frame[53:],
                  b"data: [DONE]\n\n"]
        self.run_stream(pool, chunks)
        account_ = pool.accounts[0]
        self.assertEqual(account_.cache_read_tokens, 80)
        self.assertEqual(account_.input_tokens, 40)

    def test_stream_usage_parse_failure_is_silent(self):
        """记账解析失败必须静默：响应已发完，不能抛异常。"""
        pool = self.pool()
        handler, log, _resp = self.run_stream(
            pool,
            [b'data: {"usage": not-json}\n\n', b"data: [DONE]\n\n"])
        self.assertTrue(handler.wfile.getvalue().endswith(b"0\r\n\r\n"))
        self.assertNotIn("Traceback", log)
        account_ = pool.accounts[0]
        self.assertEqual(account_.cache_read_tokens, 0)
        self.assertEqual(account_.input_tokens, 0)
        self.assertEqual(account_.requests, 1)


# ── 观测端点（HTTP 层）────────────────────────────────────────────────
class ObservabilityHttpTest(unittest.TestCase):
    """健康时 200、不可用时 503，且两个端点都不许漏密钥。"""

    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"

        cfg = make_config([account("obs", "K_OBS")],
                          providers={"opencode-go": {"adapter": "opencode-go",
                                                     "base_url": upstream_url}})
        cfg.relay_token = "t0k"
        cfg.usage_refresh_seconds = 90
        cfg.min_headroom = 2.0
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_OBS": "k-secret-value"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)

        from opencode_pool.server import RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.relay.server_port}"
        cls.headers = {"Authorization": "Bearer t0k"}

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def reset(self):
        account_ = self.pool.accounts[0]
        with self.pool.lock:
            account_.cooldown_until = 0.0
            account_.cooldown_reason = ""
        set_windows(self.pool, "obs", rolling=0, weekly=0, monthly=0)
        account_.last_refresh = time.time()

    def get(self, path, **kwargs):
        import requests

        return requests.get(f"{self.base}{path}", timeout=10, **kwargs)

    def test_healthz_200_when_ready(self):
        self.reset()
        resp = self.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reason"], "ready")
        self.assertIn("accounts", body)
        self.assertNotIn("k-secret-value", resp.text)
        self.assertNotIn("t0k", resp.text)

    def test_healthz_503_when_sample_is_stale(self):
        self.reset()
        self.pool.accounts[0].last_refresh = time.time() - 1000
        resp = self.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "usage-stale")
        self.assertEqual(body["accounts"]["stale"], 1)
        self.assertNotIn("k-secret-value", resp.text)

    def test_healthz_503_when_cooling(self):
        self.reset()
        with self.pool.lock:
            self.pool.accounts[0].cooldown_until = time.time() + 600
            self.pool.accounts[0].cooldown_reason = "quota:weekly"
        resp = self.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["reason"], "all-cooling")

    def test_healthz_503_when_below_reserve(self):
        self.reset()
        set_windows(self.pool, "obs", rolling=99, weekly=99, monthly=99)   # 余量 1
        resp = self.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["reason"], "all-below-reserve")
        self.assertEqual(body["accounts"]["below_reserve"], 1)

    def test_cache_token_accounting(self):
        self.reset()
        import requests

        before = self.pool.snapshot()["accounts"][0]
        resp = requests.post(f"{self.base}/v1/chat/completions", headers=self.headers,
                             json={"model": "m", "messages": [
                                 {"role": "user", "content": "缓存记账"}]}, timeout=10)
        self.assertEqual(resp.status_code, 200)
        after = self.pool.snapshot()["accounts"][0]
        self.assertEqual(after["cache_read_tokens"] - before["cache_read_tokens"], 80)
        self.assertEqual(after["input_tokens"] - before["input_tokens"], 120)
        self.assertAlmostEqual(
            after["cache_hit_ratio"],
            round(after["cache_read_tokens"]
                  / (after["cache_read_tokens"] + after["input_tokens"]), 4),
            places=4)
        metrics = self.get("/metrics").text
        self.assertIn('pool_cache_read_tokens_total{account="obs"}', metrics)
        self.assertIn('pool_input_tokens_total{account="obs"}', metrics)

    def test_metrics_endpoint_serves_prometheus_text(self):
        self.reset()
        resp = self.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.headers.get("Content-Type", ""))
        text = resp.text
        for family in REQUIRED_FAMILIES:
            self.assertIn(f"# TYPE {family}", text)
        self.assertIn('pool_account_headroom{account="obs",provider="opencode-go"}', text)
        self.assertNotIn("k-secret-value", text)
        self.assertNotIn("t0k", text)

    def test_metrics_accepts_bearer_token_too(self):
        self.reset()
        resp = self.get("/metrics", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pool_sticky_hits_total", resp.text)

    def test_usage_reports_below_reserve_and_sticky(self):
        self.reset()
        set_windows(self.pool, "obs", rolling=99, weekly=99, monthly=99)
        payload = self.get("/v1/usage", headers=self.headers).json()
        account_ = payload["accounts"][0]
        self.assertTrue(account_["below_reserve"])
        self.assertEqual(payload["sticky"]["ttl_seconds"], 43200)
        for key in ("pinned", "hits", "misses"):
            self.assertIn(key, payload["sticky"])


# ── 端到端：同一对话三轮钉同一账号 ────────────────────────────────────
class StickyEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_port}"

        cfg = make_config(
            [account("a", "K_STICK_A"), account("b", "K_STICK_B")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": upstream_url}})
        cfg.sticky_ttl_seconds = 43200
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_STICK_A": "k-a", "K_STICK_B": "k-b"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)

        from opencode_pool.server import RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.relay.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def post(self, payload):
        import requests

        return requests.post(f"{self.base}/v1/chat/completions", json=payload, timeout=10)

    def test_three_turns_stick_to_one_account(self):
        FakeUpstream.calls.clear()
        payload = json.loads(conversation())
        for _ in range(3):
            resp = self.post(payload)
            self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(FakeUpstream.calls, ["k-a", "k-a", "k-a"],
                         "同一对话连续三轮必须落在同一账号（否则 prompt cache 每轮失效）")

        snapshot = self.pool.snapshot()
        self.assertGreaterEqual(snapshot["sticky"]["hits"], 2)
        self.assertEqual(snapshot["sticky"]["pinned"], 1)
        self.assertEqual(snapshot["sticky"]["ttl_seconds"], 43200,
                         "默认 12 小时；活跃对话靠命中续期，回收交给 STICKY_MAX 的 LRU")

        # 换一个对话 = 新指纹 → 常规选号（a 刚用过，轮到 b）
        FakeUpstream.calls.clear()
        resp = self.post(json.loads(conversation(user="另一个问题")))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(FakeUpstream.calls, ["k-b"])
        self.assertEqual(self.pool.snapshot()["sticky"]["pinned"], 2)


# ── 指纹：对「对话增长」不敏感（review 用真 relay 复现过：anthropic 形状第 2 轮就断）──
class StickyFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.adapter = adapters.get("opencode-go")
        self.pool = Pool(make_config([account("a", "K1")]))
        self.acct = {"name": "a", "session_prefix": "p"}

    @staticmethod
    def growing(n: int, *, anthropic: bool) -> bytes:
        """构造 n 条消息的同一对话：首条 user 固定，后面 assistant/user 交替增长。"""
        messages = [{"role": "user", "content": "同一个问题"}]
        for i in range(1, n):
            messages.append({"role": "assistant" if i % 2 else "user", "content": f"第 {i} 条"})
        payload = {"model": "claude-x" if anthropic else "deepseek-v4.1-flash"}
        if anthropic:
            payload["system"] = "你是助手"          # anthropic：system 在顶层
        else:
            messages = [{"role": "system", "content": "你是助手"}] + messages  # openai 形状
        payload["messages"] = messages
        return json.dumps(payload).encode()

    def keys(self, bodies):
        return {self.pool.conversation_key(None, body) for body in bodies}

    def sessions(self, bodies):
        return {self.adapter.session_id(self.acct, "dsh-pool", body) for body in bodies}

    def test_openai_shape_is_stable_at_1_3_5_7_9_messages(self):
        bodies = [self.growing(n, anthropic=False) for n in (1, 3, 5, 7, 9)]
        self.assertEqual(len(self.keys(bodies)), 1, "同一对话增长后指纹不能变")
        self.assertEqual(len(self.sessions(bodies)), 1, "x-opencode-session 与钉号同源")

    def test_anthropic_shape_is_stable_at_1_3_5_7_9_messages(self):
        bodies = [self.growing(n, anthropic=True) for n in (1, 3, 5, 7, 9)]
        self.assertEqual(len(self.keys(bodies)), 1, "顶层 system 形状同样不能随轮次漂移")
        self.assertEqual(len(self.sessions(bodies)), 1)

    def test_session_id_survives_process_restart(self):
        """种子只依赖请求内容：重建 Pool / 重新取适配器（等价进程重启）结果必须一致。"""
        body = self.growing(5, anthropic=True)
        first_key = self.pool.conversation_key(None, body)
        first_sid = self.adapter.session_id(self.acct, "dsh-pool", body)

        fresh_pool = Pool(make_config([account("a", "K1")]))
        fresh_adapter = adapters.get("opencode-go")
        self.assertEqual(fresh_pool.conversation_key(None, body), first_key)
        self.assertEqual(fresh_adapter.session_id(self.acct, "dsh-pool", body), first_sid,
                         "重启换了 session id 就等于白丢上游 cache")

    def test_seed_version_is_part_of_the_fingerprint(self):
        import opencode_pool.adapters.base as base_mod

        body = self.growing(3, anthropic=False)
        before = base_mod.conversation_seed(body)
        old = base_mod.SEED_VERSION
        try:
            base_mod.SEED_VERSION = old + 1
            after = base_mod.conversation_seed(body)
        finally:
            base_mod.SEED_VERSION = old
        self.assertNotEqual(before, after, "升 SEED_VERSION 必须等于作废上游缓存（公式换域）")

    def test_assistant_and_tool_messages_do_not_affect_fingerprint(self):
        base = [{"role": "user", "content": "同一个问题"}]
        variants = [
            base,
            base + [{"role": "assistant", "content": "答甲"}],
            base + [{"role": "assistant", "content": "完全不同的答乙"},
                    {"role": "tool", "content": "工具结果"}],
            base + [{"role": "assistant", "content": "答丙"},
                    {"role": "tool", "content": "工具结果"},
                    {"role": "assistant", "content": "答丁"}],
        ]
        bodies = [json.dumps({"model": "m", "messages": m}).encode() for m in variants]
        self.assertEqual(len(self.keys(bodies)), 1)

    def test_system_and_model_take_part_in_fingerprint(self):
        one = json.dumps({"model": "m", "messages": [
            {"role": "system", "content": "系统甲"},
            {"role": "user", "content": "问"}]}).encode()
        other_system = json.dumps({"model": "m", "messages": [
            {"role": "system", "content": "系统乙"},
            {"role": "user", "content": "问"}]}).encode()
        other_model = json.dumps({"model": "m2", "messages": [
            {"role": "system", "content": "系统甲"},
            {"role": "user", "content": "问"}]}).encode()
        self.assertEqual(len(self.keys([one, one])), 1)
        self.assertNotEqual(self.pool.conversation_key(None, one),
                            self.pool.conversation_key(None, other_system))
        self.assertNotEqual(self.pool.conversation_key(None, one),
                            self.pool.conversation_key(None, other_model))

    def test_no_user_message_returns_empty_and_never_pins(self):
        bodies = [
            {"model": "m", "messages": [{"role": "assistant", "content": "只有回答"}]},
            {"model": "m", "system": "s", "messages": []},
            {"model": "text-embedding-x", "input": "一段文本"},
            {"model": "text-embedding-x", "input": ["甲", "乙"]},
            {"model": "m", "prompt": "补全一下"},
            {"model": "m", "messages": [{"role": "user",
                                         "content": [{"type": "image", "url": "x"}]}]},
        ]
        for payload in bodies:
            self.assertEqual(self.pool.conversation_key(None, json.dumps(payload).encode()), "",
                             f"没有 user 消息不能退化成 model 指纹：{payload}")

        # 空 key = 不钉号、不计数：选号照常进行，但 sticky 统计必须保持 0
        self.assertIsNotNone(self.pool.pick("m", sticky_key=""))
        self.assertEqual(self.pool.sticky_hits + self.pool.sticky_misses, 0)
        self.assertEqual(self.pool._sticky, {})


# ── 并发：同 key 压测 + 同对话并发换号 ─────────────────────────────────
class StickyConcurrencyTest(unittest.TestCase):
    def test_parallel_pick_with_unpin_and_cooldowns(self):
        cfg = make_config([account("a", "K1"), account("b", "K2"), account("c", "K3")])
        cfg.usage_refresh_seconds = 36000
        cfg.min_interval_seconds = 0.0
        pool = Pool(cfg)
        for name in ("a", "b", "c"):
            set_windows(pool, name, rolling=0, weekly=0, monthly=0)
        key = pool.conversation_key("m", conversation())
        sacrifice = next(a for a in pool.accounts if a.name == "c")

        successes: list[str] = []
        errors: list[BaseException] = []
        sizes: list[int] = []              # 每次 pick 后的映射表长度，取峰值

        def worker(idx):
            try:
                for i in range(40):
                    picked = pool.pick("m", sticky_key=key)
                    sizes.append(len(pool._sticky))
                    if picked is None:
                        continue
                    successes.append(picked.name)
                    if idx % 3 == 0 and i % 11 == 0:
                        pool.unpin(key)
                    if idx % 4 == 0 and i % 13 == 0:
                        pool.apply(sacrifice, ErrorDecision("failover", 1, reason="test:cool"))
            except BaseException as exc:  # noqa: BLE001 - 线程异常要带回主线程断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse([t for t in threads if t.is_alive()], "压测线程必须全部结束")
        self.assertEqual(errors, [])
        self.assertLessEqual(max(sizes, default=0), STICKY_MAX, "映射表峰值必须有界")
        self.assertEqual(pool.sticky_hits + pool.sticky_misses, len(successes),
                         "每次成功 pick 恰好记一次命中或未命中")

        time.sleep(1.2)              # 等 test:cool 的 1s 冷却过期
        pool.clear_cool()
        first = pool.pick("m", sticky_key=key)
        second = pool.pick("m", sticky_key=key)
        self.assertIsNotNone(first)
        self.assertEqual(second.name, first.name, "压测后钉子应收敛到唯一账号")
        self.assertEqual(pool._sticky[key][1], first.name)


class FailoverBarrierUpstream(BaseHTTPRequestHandler):
    """k-a 的前两个请求在 barrier 上会合，然后一个 200、一个额度 429。

    这样"两个并发请求先都命中同一个钉子"是确定的，而不是靠调度运气。
    """

    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    calls: list[str] = []
    barrier: "threading.Barrier | None" = None
    fail_budget = 0

    def log_message(self, *args):  # noqa: D102
        return

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/usage"):
            return self._send(200, {"usage": {
                key: {"percent": 0, "status": "ok", "resetsAt": "2030-01-01T00:00:00.000Z"}
                for key in ("rolling", "weekly", "monthly")}})
        return self._send(404, {})

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        key = self.headers.get("Authorization", "").removeprefix("Bearer ")
        with FailoverBarrierUpstream.lock:
            FailoverBarrierUpstream.calls.append(key)
            a_index = FailoverBarrierUpstream.calls.count("k-a")
            barrier = FailoverBarrierUpstream.barrier
        if key == "k-a" and barrier is not None and a_index <= 2:
            try:                     # 等第二个并发请求也命中 a，再做失败判定
                barrier.wait(timeout=5)
            except Exception:        # noqa: BLE001 - 只有一个请求时别把测试挂死
                pass
            with FailoverBarrierUpstream.lock:
                fail = FailoverBarrierUpstream.fail_budget > 0
                if fail:
                    FailoverBarrierUpstream.fail_budget -= 1
            if fail:
                return self._send(429, {"error": {"type": "GoUsageLimitError"},
                                        "metadata": {"limitName": "weekly"}})
        return self._send(200, {"id": "x", "choices": [
            {"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"opencode": {"allowance": 0.01}}})


class ConcurrentFailoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FailoverBarrierUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.upstream.server_port}"
        cfg = make_config(
            [account("a", "K_CC_A"), account("b", "K_CC_B")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": url}})
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_CC_A": "k-a", "K_CC_B": "k-b"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)

        from opencode_pool.server import RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.relay.server_port}"
        cls.body = json.loads(conversation(model="m", user="并发同一个对话"))

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def chat(self):
        import requests

        return requests.post(f"{self.base}/v1/chat/completions", json=self.body, timeout=30)

    def test_two_concurrent_same_conversation_one_fails_over(self):
        FailoverBarrierUpstream.calls.clear()
        FailoverBarrierUpstream.fail_budget = 1
        FailoverBarrierUpstream.barrier = threading.Barrier(2, timeout=5)
        key = self.pool.conversation_key("m", json.dumps(self.body).encode())
        self.assertEqual(self.pool.pick("m", sticky_key=key).name, "a")

        results = {}

        def call(idx):
            results[idx] = self.chat()

        threads = [threading.Thread(target=call, args=(i,)) for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        FailoverBarrierUpstream.barrier = None

        self.assertEqual([r.status_code for r in results.values()], [200, 200])
        self.assertGreaterEqual(self.pool.sticky_hits, 2,
                                "两个并发请求都应命中同一个钉子（hits>=1）")
        self.assertEqual(FailoverBarrierUpstream.calls.count("k-a"), 2,
                         "两个并发请求都应先命中同一个账号 a")
        self.assertEqual(FailoverBarrierUpstream.calls.count("k-b"), 1,
                         "被额度 429 的那个换到 b")
        self.assertIsNone(self.pool._sticky.get(key),
                          "只有失败方解钉；成功方不应补钉")

        # 下一轮：a 已冷却 → 换到 b，并把钉子钉到 b
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.pool._sticky[key][1], "b")
        # 再下一轮：命中换到的 b，而不是重新选号
        hits = self.pool.sticky_hits
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.pool.sticky_hits, hits + 1)


# ── 本地容量等待：只换本次请求的号，不解钉 ─────────────────────────────
class CapacityWaitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.upstream.server_port}"
        cfg = make_config(
            [account("a", "K_CAP_A"), account("b", "K_CAP_B")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": url}})
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_CAP_A": "k-a", "K_CAP_B": "k-b"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)
        from opencode_pool.server import RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.relay.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def pin_to_a(self, user):
        """让 a 的余量最高，把对话先钉到 a。"""
        set_windows(self.pool, "a", rolling=0, weekly=0, monthly=0)
        set_windows(self.pool, "b", rolling=20, weekly=20, monthly=20)
        key = self.pool.conversation_key("m", conversation(user=user))
        self.assertEqual(self.pool.pick("m", sticky_key=key).name, "a")
        return key

    def post(self, user):
        import requests

        return requests.post(f"{self.base}/v1/chat/completions",
                             json=json.loads(conversation(user=user)), timeout=15)

    def run_with(self, patched_name, deny):
        import requests  # noqa: F401 - 保持与 post 一致的可读性

        key = self.pin_to_a(f"容量等待 {patched_name}")
        original = getattr(self.pool, patched_name)
        setattr(self.pool, patched_name, deny(original))
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                resp = self.post(f"容量等待 {patched_name}")
        finally:
            setattr(self.pool, patched_name, original)
        log = output.getvalue()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.pool._sticky[key][1], "a",
                         f"{patched_name} 是本地容量等待，不该解钉")
        self.assertIn("会话钉子保留", log)
        self.assertNotIn("不可用", log, "日志不能把本地容量等待写成账号不可用")

    def test_concurrency_timeout_switches_without_unpinning(self):
        def deny(original):
            def acquire(account, timeout=30.0):
                if account.name == "a":
                    return False
                return original(account, timeout)
            return acquire

        self.run_with("acquire", deny)

    def test_queue_timeout_switches_without_unpinning(self):
        def deny(original):
            def wait_turn(account, timeout=90.0):
                if account.name == "a":
                    return False
                return original(account, timeout)
            return wait_turn

        self.run_with("wait_turn", deny)


# ── embeddings / completions：没有对话形状 → 走普通选号 ────────────────
class NonConversationRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.upstream.server_port}"
        cfg = make_config(
            [account("a", "K_NC_A"), account("b", "K_NC_B")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": url}})
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        cls.pool = Pool(cfg, keys=FakeKeyStore({"K_NC_A": "k-a", "K_NC_B": "k-b"}))
        for account_ in cls.pool.accounts:
            cls.pool.refresh(account_)
        from opencode_pool.server import RelayHandler
        RelayHandler.pool = cls.pool
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        cls.relay.daemon_threads = True
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.relay.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.upstream.shutdown()

    def test_embeddings_spread_across_accounts_and_never_pinned(self):
        import requests

        FakeUpstream.calls.clear()
        before = self.pool.snapshot()["sticky"]["pinned"]
        for i in range(6):
            resp = requests.post(f"{self.base}/v1/embeddings",
                                 json={"model": "text-embedding-x", "input": f"不同输入 {i}"},
                                 timeout=10)
            self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(FakeUpstream.calls.count("k-a"), 3,
                         "6 个不同 input 必须分摊到多个账号，不能全钉一个")
        self.assertEqual(FakeUpstream.calls.count("k-b"), 3)
        self.assertEqual(self.pool.snapshot()["sticky"]["pinned"], before,
                         "embeddings 不能产生会话钉子")
        self.assertEqual(self.pool.sticky_misses, 0)

    def test_completions_has_no_conversation_key(self):
        import requests

        body = {"model": "m", "prompt": "补全"}
        self.assertEqual(self.pool.conversation_key("m", json.dumps(body).encode()), "")
        FakeUpstream.calls.clear()
        resp = requests.post(f"{self.base}/v1/completions", json=body, timeout=10)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(FakeUpstream.calls), 1)
        self.assertEqual(self.pool.snapshot()["sticky"]["pinned"], 0)


# ── 日志噪声：客户端断开只留一行摘要，正常错误日志不变 ────────────────
class DisconnectNoiseTest(unittest.TestCase):
    def test_disconnect_is_one_line_with_path_and_type(self):
        from opencode_pool.server import RelayHandler, RelayServer

        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        server.note_request("POST /v1/chat/completions")
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    raise ConnectionResetError(104, "Connection reset by peer")
                except ConnectionResetError:
                    server.handle_error(object(), ("127.0.0.1", 51234))
        finally:
            server.server_close()

        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"客户端断开应只有一行摘要：{lines}")
        self.assertIn("POST /v1/chat/completions", lines[0])
        self.assertIn("ConnectionResetError", lines[0])
        self.assertNotIn("Traceback", output.getvalue())

    def test_broken_pipe_is_also_one_line(self):
        from opencode_pool.server import RelayHandler, RelayServer

        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        server.note_request("POST /v1/messages")
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    raise BrokenPipeError(32, "Broken pipe")
                except BrokenPipeError:
                    server.handle_error(object(), ("127.0.0.1", 51235))
        finally:
            server.server_close()

        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("POST /v1/messages", lines[0])
        self.assertIn("BrokenPipeError", lines[0])
        self.assertNotIn("Traceback", output.getvalue())

    def test_real_errors_still_get_traceback(self):
        """真异常不能降噪吞掉：仍要完整 traceback 供排障。"""
        from opencode_pool.server import RelayHandler, RelayServer

        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        output = io.StringIO()
        try:
            with contextlib.redirect_stderr(output):
                try:
                    raise ValueError("真正的 bug")
                except ValueError:
                    server.handle_error(object(), ("127.0.0.1", 51236))
        finally:
            server.server_close()

        self.assertIn("Traceback", output.getvalue())
        self.assertIn("ValueError", output.getvalue())

    def test_half_request_reset_is_single_line(self):
        """真 server：请求没发完就 RST，也不该刷多行 traceback。"""
        from opencode_pool.server import RelayHandler, RelayServer

        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                sock.sendall(b"GET /v1/usage HTTP/1.1\r\nHost: x\r\n")   # 请求头没发完
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
                sock.close()                                               # 触发 RST
                time.sleep(0.4)
        finally:
            server.shutdown()
            server.server_close()

        text = output.getvalue()
        self.assertNotIn("Traceback", text, f"客户端断开不该刷 traceback：{text}")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 1)
        if lines:
            self.assertIn("ConnectionResetError", lines[0])

    def test_keepalive_reset_reports_last_path(self):
        """真 server：keep-alive 连接被 RST 时，摘要里能带出上一次请求的 path。"""
        from opencode_pool.server import RelayHandler, RelayServer

        RelayHandler.pool = Pool(make_config([]), keys=FakeKeyStore())
        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                sock.sendall(b"GET /v1/usage HTTP/1.1\r\nHost: x\r\n\r\n")
                time.sleep(0.2)          # 让服务端完成响应并回到 readline
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
                sock.close()             # RST → 服务端下一次 readline 抛 ConnectionResetError
                time.sleep(0.4)
        finally:
            server.shutdown()
            server.server_close()

        text = output.getvalue()
        self.assertNotIn("Traceback", text, f"客户端断开不该刷 traceback：{text}")
        summary = [line for line in text.splitlines() if "客户端断开" in line]
        self.assertEqual(len(summary), 1, f"应有一行摘要：{text}")
        self.assertIn("GET /v1/usage", summary[0], "摘要要带 path")
        self.assertIn("ConnectionResetError", summary[0], "摘要要带异常类型")

    def test_normal_error_logs_are_unchanged(self):
        """429/401 的排障日志形态不能因为降噪而变。"""
        import requests

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        upstream.daemon_threads = True
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{upstream.server_port}"
        from opencode_pool.server import RelayHandler, RelayServer

        cfg = make_config(
            [account("dead", "K_DEAD"), account("bad", "K_BAD"), account("live", "K_LIVE")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": url}})
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        pool = Pool(cfg, keys=FakeKeyStore({"K_DEAD": "k-dead", "K_BAD": "k-bad",
                                            "K_LIVE": "k-live"}))
        for account_ in pool.accounts:
            pool.refresh(account_)
        RelayHandler.pool = pool
        server = RelayServer(("127.0.0.1", 0), RelayHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                resp = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                                     json={"model": "m", "messages": [
                                         {"role": "user", "content": "hi"}]}, timeout=15)
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()

        self.assertEqual(resp.status_code, 200)
        log = output.getvalue()
        self.assertIn("[req] dead m 429", log)
        self.assertIn("→ 换号", log)
        self.assertIn("[req] bad m 401", log)
        self.assertIn("[req] live m 200", log)
        self.assertNotIn("Traceback", log)
        self.assertNotIn("客户端断开", log)


# ── 状态落盘并发安全（R6）────────────────────────────────────────────
class StatePersistenceTest(unittest.TestCase):
    def make_pool(self):
        tmp = tempfile.mkdtemp(prefix="pool-state-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        cfg = make_config([account("a", "K1"), account("b", "K2")])
        cfg.state_file = os.path.join(tmp, "state.json")
        return Pool(cfg, keys=FakeKeyStore()), tmp, cfg.state_file, cfg

    def test_parallel_force_saves_never_expose_half_file(self):
        """8 线程 × 100 次 force 保存，同时读线程反复 json.loads。"""
        pool, tmp, path, _cfg = self.make_pool()
        stop = threading.Event()
        reads = {"ok": 0, "json": 0, "enoent": 0, "other": []}

        def reader():
            while not stop.is_set():
                if not os.path.exists(path):
                    time.sleep(0.001)
                    continue
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if "saved_at" not in data or set(data.get("accounts") or {}) != {"a", "b"}:
                        reads["other"].append("payload incomplete")
                    else:
                        reads["ok"] += 1
                except json.JSONDecodeError:
                    reads["json"] += 1
                except FileNotFoundError:
                    reads["enoent"] += 1
                except Exception as exc:  # noqa: BLE001 - 统计而不是吞掉
                    reads["other"].append(type(exc).__name__)

        def writer():
            for _ in range(100):
                pool._save_state(force=True)

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        writers = [threading.Thread(target=writer) for _ in range(8)]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join()
        stop.set()
        reader_thread.join(10)

        self.assertEqual(reads["json"], 0, "读到了半截 JSON（旧实现的典型症状）")
        self.assertEqual(reads["enoent"], 0, "replace 期间状态文件不该消失")
        self.assertEqual(reads["other"], [])
        self.assertGreater(reads["ok"], 0, "读线程至少要成功读回一次")

        with open(path, encoding="utf-8") as f:
            final = json.load(f)
        self.assertEqual(sorted(final["accounts"]), ["a", "b"])
        leftovers = [name for name in os.listdir(tmp) if name.startswith(".pool-state-")]
        self.assertEqual(leftovers, [], "临时文件必须被清理")

    def test_corrupt_state_file_is_reported_not_fatal(self):
        _pool, _tmp, path, cfg = self.make_pool()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Pool(cfg, keys=FakeKeyStore())          # 坏状态不该挡住启动
        self.assertIn("state load failed", output.getvalue())


# ── SecretStore：锁 + 外部写入可见 + 缓存自愈（L3）────────────────────
class SecretStoreSelfHealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-secrets-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "secrets.json")

    def test_external_write_is_seen(self):
        store = SecretStore(self.path)
        store.set("K", "v1")
        # 模拟另一个进程/实例改写文件
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "secrets": {"K": {"value": "v2"}}}, f)
        os.utime(self.path, None)
        self.assertEqual(store.get("K"), "v2")

    def test_poisoned_empty_cache_self_heals(self):
        """复现 L3：缓存被清空但 mtime 与磁盘一致（一次读失败的残留）。"""
        store = SecretStore(self.path)
        store.set("K", "v1")
        store._data = {}
        store._mtime = os.stat(self.path).st_mtime
        self.assertEqual(store.get("K"), "v1", "缓存为空但文件有内容时必须重读自愈")

    def test_delete_after_cache_reset(self):
        store = SecretStore(self.path)
        store.set("K", "v1")
        store._data = None
        store._mtime = None
        self.assertTrue(store.delete("K"))
        self.assertIsNone(store.get("K"))

    def test_concurrent_set_get_is_consistent(self):
        store = SecretStore(self.path)
        errors = []

        def worker(idx):
            for j in range(30):
                name = f"K{idx}_{j}"
                store.set(name, f"v{idx}")
                if store.get(name) != f"v{idx}":
                    errors.append(name)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["secrets"]), 8 * 30)


# ── 真 KeyStore 的三级优先级（L2）──────────────────────────────────
class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-keys-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.yaml = os.path.join(self.tmp, ".credentials.yaml")
        self.secrets = os.path.join(self.tmp, "secrets.json")
        with open(self.yaml, "w", encoding="utf-8") as f:
            f.write("refs:\n  K_DSH_ONLY: dsh-only\n  K_POOL_AND_DSH: dsh-value\n"
                    "  K_ENV_BEATS_ALL: dsh-value\n")
        store = SecretStore(self.secrets)
        store.set("K_POOL_ONLY", "pool-only")
        store.set("K_POOL_AND_DSH", "pool-value")
        store.set("K_ENV_BEATS_ALL", "pool-value")
        self.keys = KeyStore(self.yaml, self.secrets)
        patcher = mock.patch.dict(os.environ, {"K_ENV_ONLY": "env-only",
                                               "K_ENV_BEATS_ALL": "env-value"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_level_resolves_with_its_source(self):
        self.assertEqual(self.keys.get("K_ENV_ONLY"), "env-only")
        self.assertEqual(self.keys.source("K_ENV_ONLY"), "env")
        self.assertEqual(self.keys.get("K_POOL_ONLY"), "pool-only")
        self.assertEqual(self.keys.source("K_POOL_ONLY"), "pool")
        self.assertEqual(self.keys.get("K_DSH_ONLY"), "dsh-only")
        self.assertEqual(self.keys.source("K_DSH_ONLY"), "dsh")

    def test_missing_slot_returns_none(self):
        self.assertIsNone(self.keys.get("K_NOWHERE"))
        self.assertEqual(self.keys.source("K_NOWHERE"), "")
        self.assertIsNone(self.keys.get(""))

    def test_precedence_env_over_pool_over_dsh(self):
        self.assertEqual(self.keys.get("K_POOL_AND_DSH"), "pool-value",
                         "面板密钥仓要压过 DSH refs")
        self.assertEqual(self.keys.source("K_POOL_AND_DSH"), "pool")
        self.assertEqual(self.keys.get("K_ENV_BEATS_ALL"), "env-value",
                         "环境变量要压过面板密钥仓")
        self.assertEqual(self.keys.source("K_ENV_BEATS_ALL"), "env")

    def test_empty_env_does_not_shadow(self):
        with mock.patch.dict(os.environ, {"K_DSH_ONLY": ""}):
            self.assertEqual(self.keys.get("K_DSH_ONLY"), "dsh-only")


# ── 结构化请求日志 requests.jsonl ─────────────────────────────────────
SPEC_KEYS = {
    "ts", "req_id", "method", "path", "model", "stream", "account", "provider",
    "status", "upstream_status", "outcome", "queue_ms", "ttfb_ms", "total_ms",
    "bytes", "retries", "failovers", "sticky", "tokens", "allowance", "error_reason",
}
TOKEN_KEYS = {"input", "cache_read", "output", "reasoning",
              "cache_write_5m", "cache_write_1h"}


class RequestLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-reqlog-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = os.path.join(self.tmp, "requests.jsonl")

    def test_disabled_logger_writes_nothing(self):
        from opencode_pool.reqlog import RequestLogger, new_request_record

        logger = RequestLogger("")
        logger.write(new_request_record("POST", "/v1/chat/completions", "m", False))
        self.assertFalse(logger.enabled)

    def test_record_has_every_spec_key_even_when_missing(self):
        from opencode_pool.reqlog import RequestLogger, new_request_record

        logger = RequestLogger(self.path)
        logger.write(new_request_record("POST", "/v1/chat/completions", "m", True))
        with open(self.path, encoding="utf-8") as f:
            row = json.loads(f.readline())
        self.assertEqual(set(row), SPEC_KEYS, "字段不能省略，缺失写 null")
        self.assertEqual(set(row["tokens"]), TOKEN_KEYS)
        self.assertIsNone(row["account"])
        self.assertIsNone(row["status"])
        self.assertIsNone(row["error_reason"])
        for value in row["tokens"].values():
            self.assertIsNone(value, "缺失 token 必须是 null，不能用 0 顶替")
        self.assertRegex(row["req_id"], r"^[0-9a-f]{8}$")
        self.assertIsNotNone(parse_iso_stamp(row["ts"]))

    def test_rotation_keeps_configured_number_of_files(self):
        from opencode_pool.reqlog import RequestLogger, new_request_record

        logger = RequestLogger(self.path, max_bytes=300, keep=2)
        for i in range(40):
            record = new_request_record("POST", "/v1/chat/completions", "m", False)
            record["bytes"] = i
            record["account"] = "a" * 40          # 让每行足够大，尽快触发轮转
            logger.write(record)

        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertTrue(os.path.exists(self.path + ".2"))
        self.assertFalse(os.path.exists(self.path + ".3"), "超出 keep 的旧文件要删掉")

        rows = 0
        for name in (self.path, self.path + ".1", self.path + ".2"):
            with open(name, encoding="utf-8") as f:
                for line in f:
                    self.assertIsInstance(json.loads(line), dict)
                    rows += 1
        self.assertGreater(rows, 0, "轮转后的文件必须仍是可解析的 JSONL")
        self.assertLessEqual(rows, 40)


def parse_iso_stamp(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RequestLogEndToEndTest(unittest.TestCase):
    """真 relay：每次转发（成功/换号/无号）写一行，且与 relay.log 可对账。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-reqlog-e2e-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.log_path = os.path.join(self.tmp, "requests.jsonl")
        FakeUpstream.calls.clear()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.upstream_url = f"http://127.0.0.1:{self.upstream.server_port}"

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()

    def make_relay(self, specs, keys, **cfg_extra):
        from opencode_pool.server import RelayHandler

        cfg = make_config(
            specs, providers={"opencode-go": {"adapter": "opencode-go",
                                              "base_url": self.upstream_url}})
        cfg.request_log_path = self.log_path
        cfg.relay_token = "t0k-reqlog"
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        for key, value in cfg_extra.items():
            setattr(cfg, key, value)
        pool = Pool(cfg, keys=FakeKeyStore(keys))
        for account_ in pool.accounts:
            pool.refresh(account_)
        RelayHandler.pool = pool
        self.pool = pool
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        self.relay.daemon_threads = True
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.relay.server_port}"
        self.headers = {"Authorization": "Bearer t0k-reqlog"}
        self.addCleanup(self.relay.server_close)
        self.addCleanup(self.relay.shutdown)
        return pool

    def post(self, path="/v1/chat/completions", stream=False, user="同一个问题"):
        import requests

        return requests.post(f"{self.base}{path}", headers=self.headers,
                             json={"model": "m", "stream": stream,
                                   "messages": [{"role": "user", "content": user}]},
                             timeout=20)

    def rows(self, expect=1, timeout=3.0):
        """响应发完之后 _proxy.finally 才落盘，等文件里凑够 expect 行再断言。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.log_path):
                with open(self.log_path, encoding="utf-8") as f:
                    found = [json.loads(line) for line in f if line.strip()]
                if len(found) >= expect:
                    return found
            time.sleep(0.01)
        self.fail(f"requests.jsonl 在 {timeout}s 内没有写够 {expect} 行")

    def test_success_row_has_spec_fields_and_real_tokens(self):
        self.make_relay([account("live", "K_LIVE")], {"K_LIVE": "k-live"})
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1, "一个客户端请求只写一行")
        row = rows[0]
        self.assertEqual(set(row), SPEC_KEYS)
        self.assertEqual((row["method"], row["path"], row["model"], row["stream"]),
                         ("POST", "/v1/chat/completions", "m", False))
        self.assertEqual(row["account"], "live")
        self.assertEqual(row["provider"], "opencode-go")
        self.assertEqual((row["status"], row["upstream_status"], row["outcome"]),
                         (200, 200, "ok"))
        self.assertEqual((row["retries"], row["failovers"]), (0, 0))
        self.assertEqual(row["sticky"], "miss")
        self.assertIsNotNone(row["queue_ms"])
        self.assertIsNotNone(row["ttfb_ms"])
        self.assertGreaterEqual(row["total_ms"], row["ttfb_ms"])
        self.assertGreater(row["bytes"], 0)
        # 上游真实 usage（FakeUpstream 的合成值），不允许估算/写 0 顶替缺失
        self.assertEqual(row["tokens"]["input"], 120)
        self.assertEqual(row["tokens"]["cache_read"], 80)
        self.assertEqual(row["tokens"]["output"], 10)
        self.assertEqual(row["tokens"]["reasoning"], 4)
        self.assertEqual(row["tokens"]["cache_write_5m"], 40)
        self.assertEqual(row["tokens"]["cache_write_1h"], 0)
        self.assertAlmostEqual(row["allowance"], 0.5, places=6)

        with open(self.log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("t0k-reqlog", text, "relay token 不能进结构化日志")
        self.assertNotIn("k-live", text, "账号 key 不能进结构化日志")
        self.assertNotIn("同一个问题", text, "prompt 正文不能进结构化日志")

    def test_stream_row_marks_stream_and_ttfb(self):
        self.make_relay([account("live", "K_LIVE")], {"K_LIVE": "k-live"})
        resp = self.post(stream=True)
        self.assertEqual(resp.status_code, 200)
        row = self.rows()[0]
        self.assertTrue(row["stream"])
        self.assertIsNotNone(row["ttfb_ms"])
        self.assertLessEqual(row["ttfb_ms"], row["total_ms"] + 1)
        self.assertEqual(row["outcome"], "ok")

    def test_failover_row_keeps_req_id_across_stages(self):
        self.make_relay(
            [account("dead", "K_DEAD"), account("live", "K_LIVE")],
            {"K_DEAD": "k-dead", "K_LIVE": "k-live"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            resp = self.post(user="换号请求")
        self.assertEqual(resp.status_code, 200)
        log = output.getvalue()
        self.assertIn("→ 换号", log)
        match = re.search(r"req=([0-9a-f]{8})", log)
        self.assertIsNotNone(match, f"失败/换号日志必须带 req=<id>：{log}")
        row = self.rows()[0]
        self.assertEqual(row["req_id"], match.group(1), "relay.log 与 jsonl 要能按 req_id 对账")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["upstream_status"], 200, "upstream_status 记最后一次上游码（换号后成功）")
        self.assertGreaterEqual(row["failovers"], 1)
        self.assertEqual(row["error_reason"], "quota:weekly", "失败原因要保留下来")
        self.assertEqual(row["account"], "live")

    def test_no_account_row_uses_null_tokens(self):
        pool = self.make_relay([account("only", "K_ONLY")], {"K_ONLY": "k-only"},
                               queue_wait_seconds=0.05)
        with pool.lock:
            pool.accounts[0].cooldown_until = time.time() + 600
            pool.accounts[0].cooldown_reason = "quota:weekly"
        resp = self.post(user="无号请求")
        self.assertEqual(resp.status_code, 503)
        row = self.rows()[0]
        self.assertEqual(row["outcome"], "no_account")
        self.assertEqual(row["status"], 503)
        self.assertIsNone(row["account"])
        self.assertIsNone(row["upstream_status"])
        self.assertEqual(row["error_reason"], "queue-timeout")
        for value in row["tokens"].values():
            self.assertIsNone(value, "没有上游 usage 时必须写 null")

    def test_log_can_be_disabled(self):
        self.make_relay([account("live", "K_LIVE")], {"K_LIVE": "k-live"},
                        request_log_path="")
        self.assertEqual(self.post().status_code, 200)
        self.assertFalse(os.path.exists(self.log_path))


class StatsClientErrorTest(unittest.TestCase):
    """被取消的请求不算成功：成功率分母是"完成请求"，client_error 单列。"""

    def test_client_error_is_not_counted_as_success(self):
        tmp = tempfile.mkdtemp(prefix="pool-stats-abort-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "requests.jsonl")
        rows = [
            {"ts": _ts_ago(hours=5), "req_id": "11111111", "status": 200,
             "outcome": "ok", "account": "go02", "ttfb_ms": 100.0, "total_ms": 100.0,
             "sticky": "hit", "failovers": 0, "tokens": {"input": 1, "cache_read": 9}},
            {"ts": _ts_ago(hours=4), "req_id": "22222222", "status": 200,
             "outcome": "ok", "account": "go02", "ttfb_ms": 100.0, "total_ms": 100.0,
             "sticky": "miss", "failovers": 0, "tokens": {"input": 1, "cache_read": 9}},
            {"ts": _ts_ago(hours=3), "req_id": "33333333", "status": 499,
             "outcome": "client_error", "error_reason": "client_abort", "account": "go02",
             "ttfb_ms": 10.0, "total_ms": 20.0, "bytes": 0, "sticky": "hit",
             "failovers": 0, "tokens": {"input": None, "cache_read": None}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

        script = os.path.join(HERE, "scripts", "stats.py")
        result = subprocess.run([sys.executable, script, "--file", path],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("总请求: 3", out)
        self.assertIn("成功率: 100.0% (2/2 完成请求)", out, "取消不能拉低成功率的分母")
        self.assertIn("client_error: 1 (33.3% of all)", out)
        self.assertIn("outcome: ok=2 client_error=1", out)
        self.assertIn("失败 1", out, "按账号的失败数要 +1（与 JSONL 对账）")

class StatsScriptTest(unittest.TestCase):
    def test_stats_reads_jsonl_and_reports_numbers(self):
        tmp = tempfile.mkdtemp(prefix="pool-stats-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "requests.jsonl")
        rows = [
            {"ts": _ts_ago(hours=5), "req_id": "aaaaaaaa", "status": 200,
             "outcome": "ok", "account": "go02", "ttfb_ms": 100.0, "total_ms": 200.0,
             "sticky": "hit", "failovers": 0, "tokens": {"input": 10, "cache_read": 90}},
            {"ts": _ts_ago(hours=4), "req_id": "bbbbbbbb", "status": 200,
             "outcome": "ok", "account": "go02", "ttfb_ms": 300.0, "total_ms": 400.0,
             "sticky": "hit", "failovers": 0, "tokens": {"input": 0, "cache_read": 100}},
            {"ts": _ts_ago(hours=3), "req_id": "cccccccc", "status": 503,
             "outcome": "throttled", "account": "go01", "ttfb_ms": 50.0, "total_ms": 50.0,
             "sticky": "miss", "failovers": 1, "error_reason": "throttled-persistent",
             "tokens": {"input": None, "cache_read": None}},
            {"ts": _ts_ago(hours=2), "req_id": "dddddddd", "status": 503,
             "outcome": "no_account", "account": None, "ttfb_ms": 10.0, "total_ms": 10.0,
             "sticky": "off", "failovers": 0, "tokens": {}},
            {"ts": "2020-01-01T00:00:00.000+08:00", "req_id": "eeeeeeee", "status": 200,
             "outcome": "ok", "account": "go02", "ttfb_ms": 1.0, "total_ms": 5.0,
             "sticky": "hit", "failovers": 0, "tokens": {"input": 1, "cache_read": 1}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.write("{broken json\n")

        script = os.path.join(HERE, "scripts", "stats.py")
        result = subprocess.run([sys.executable, script, "--file", path, "--since", "24h"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("总请求: 4", out)
        self.assertIn("成功率: 50.0% (2/4 完成请求)", out)
        self.assertIn("client_error: 0 (0.0% of all)", out)
        self.assertIn("缓存命中率: 95.0%", out)
        self.assertIn("粘性命中率: 66.7%", out)
        self.assertIn("限流请求: 1  换号次数: 1", out)
        self.assertIn("p50=125.0", out)
        self.assertIn("p95=370.0", out)
        self.assertIn("按 --since 跳过: 1", out)
        self.assertIn("跳过坏行: 1", out)
        self.assertIn("go02", out)


# ── 客户端取消流式响应：摘要一次 + JSONL 记 client_error（V1）──────────
class ClientAbortWriteTest(unittest.TestCase):
    """写回时客户端断开：三处痕迹（relay.log / requests.jsonl / stats）要一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pool-abort-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.log_path = os.path.join(self.tmp, "requests.jsonl")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.shutdown)
        url = f"http://127.0.0.1:{self.upstream.server_port}"
        cfg = make_config(
            [account("live", "K_LIVE")],
            providers={"opencode-go": {"adapter": "opencode-go", "base_url": url}})
        cfg.request_log_path = self.log_path
        cfg.relay_token = "t0k-abort"
        cfg.min_interval_seconds = 0.0
        cfg.max_inflight = 0
        self.pool = Pool(cfg, keys=FakeKeyStore({"K_LIVE": "k-live"}))
        for account_ in self.pool.accounts:
            self.pool.refresh(account_)
        from opencode_pool.server import RelayHandler
        RelayHandler.pool = self.pool
        self.handler_cls = RelayHandler

    class _FailingWFile:
        """按调用序号失败：1=响应头 2=首个数据块 3=终止块。"""

        def __init__(self, fail_call):
            self.fail_call = fail_call
            self.calls = 0
            self.written_payload = 0     # 实际写出的 **payload** 字节（不含 chunk 框架）
            self.raw = bytearray()

        def write(self, payload):
            self.calls += 1
            if self.calls == self.fail_call:
                raise BrokenPipeError(32, "Broken pipe")
            if payload.startswith(b"0\r\n\r\n"):
                pass
            else:
                head, _, _ = payload.partition(b"\r\n")
                try:
                    self.written_payload += int(head, 16)   # chunked: "<hex>\r\n<data>\r\n"
                except ValueError:
                    pass                                    # 响应头（HTTP/1.1 ...）
            self.raw.extend(payload)

        def flush(self):
            pass

    def make_handler(self, fail_call, path):
        handler = self.handler_cls.__new__(self.handler_cls)
        handler.wfile = self._FailingWFile(fail_call)
        handler._headers_buffer = []
        handler.requestline = f"POST {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "POST"
        handler.path = path
        handler.client_address = ("127.0.0.1", 0)
        handler.headers = {"Authorization": "Bearer t0k-abort"}
        handler.server = None
        return handler

    def run_stream(self, fail_call, path="/v1/chat/completions"):
        handler = self.make_handler(fail_call, path)
        body = json.dumps({"model": "m", "stream": True,
                           "messages": [{"role": "user", "content": "cancel me"}]}).encode()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handler._proxy("POST", path, body, stream=True, model="m")
        deadline = time.time() + 2
        while time.time() < deadline and not os.path.exists(self.log_path):
            time.sleep(0.01)
        with open(self.log_path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return output.getvalue(), rows, handler

    def assert_summary_once(self, log, path):
        lines = [line for line in log.splitlines() if "客户端断开" in line]
        self.assertEqual(len(lines), 1, f"每次取消只允许一行摘要：{lines}")
        self.assertIn(f"POST {path}", lines[0], "摘要 path 必须是本次请求")
        self.assertIn("BrokenPipeError", lines[0])
        self.assertNotIn("Traceback", log)

    def assert_client_error_row(self, row, expected_bytes):
        self.assertEqual(row["outcome"], "client_error")
        self.assertEqual(row["error_reason"], "client_abort")
        self.assertEqual(row["bytes"], expected_bytes, "bytes 必须是实际写出量")
        self.assertNotEqual(row["status"], 200)

    def test_abort_before_first_byte(self):
        log, rows, handler = self.run_stream(fail_call=1)
        self.assertEqual(len(rows), 1)
        self.assert_client_error_row(rows[0], handler.wfile.written_payload)
        self.assertEqual(rows[0]["bytes"], 0)
        self.assert_summary_once(log, "/v1/chat/completions")

    def test_abort_mid_stream_uses_current_path(self):
        """path 必须是本次请求（/v1/messages），不是上一个 keep-alive 请求。"""
        log, rows, handler = self.run_stream(fail_call=2, path="/v1/messages")
        self.assert_client_error_row(rows[0], handler.wfile.written_payload)
        self.assertEqual(rows[0]["bytes"], 0)
        self.assert_summary_once(log, "/v1/messages")

    def test_abort_at_tail_counts_written_bytes(self):
        log, rows, handler = self.run_stream(fail_call=3)
        self.assert_client_error_row(rows[0], handler.wfile.written_payload)
        self.assertGreater(rows[0]["bytes"], 0, "已经写出的 payload 字节要如实记录")
        self.assert_summary_once(log, "/v1/chat/completions")

    def test_non_stream_body_abort_is_also_client_error(self):
        """非流式：响应头写失败同样记 client_error（不能伪装成 ok）。"""
        handler = self.make_handler(fail_call=1, path="/v1/chat/completions")
        body = json.dumps({"model": "m", "stream": False,
                           "messages": [{"role": "user", "content": "cancel"}]}).encode()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            handler._proxy("POST", "/v1/chat/completions", body, stream=False, model="m")
        with open(self.log_path, encoding="utf-8") as f:
            row = json.loads(f.readline())
        self.assertEqual(row["outcome"], "client_error")
        self.assertEqual(row["error_reason"], "client_abort")
        self.assertEqual(row["bytes"], 0)
        self.assert_summary_once(output.getvalue(), "/v1/chat/completions")
