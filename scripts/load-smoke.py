"""端到端压测：在本地假上游上跑真 relay，验证过载下的排队行为。

场景（都用 max_inflight=1 / 每请求 1.5s 上游耗时）：
  A. 8 并发 / 2 号 / 预算 10s   → 全部 200，不该有 503（容量够）
  B. 20 并发 / 2 号 / 预算 3s    → 允许 503，但每个 503 的排队必须 ≤ 预算（约 3s），
                                 不能再出现 60s/90s 串行等待
  C. 客户端排队中途断开           → 上游一次都不该被调到（不为死连接打上游）
"""
import concurrent.futures as cf
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_DELAY = 1.5                 # 假上游每个请求耗时（秒）


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = 0
    seen: list[str] = []
    lock = threading.Lock()

    def log_message(self, *args):
        return

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/usage"):
            return self._send(200, {"usage": {
                k: {"percent": 0, "status": "ok", "resetsAt": "2030-01-01T00:00:00.000Z"}
                for k in ("rolling", "weekly", "monthly")}})
        return self._send(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with FakeUpstream.lock:
            FakeUpstream.calls += 1
            try:
                FakeUpstream.seen.append(json.loads(raw)["messages"][0]["content"])
            except Exception:  # noqa: BLE001 - 冒烟脚本，解析不动就记空
                FakeUpstream.seen.append("?")
        time.sleep(UPSTREAM_DELAY)
        return self._send(200, {"id": "x", "choices": [
            {"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"opencode": {"allowance": 0.01, "tokens": {"input": 10, "cache_read": 90}}}})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build(budget):
    up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{up.server_port}"
    port = free_port()
    tmpdir = tempfile.mkdtemp(prefix="pool-load-smoke-")
    cfg_path = os.path.join(tmpdir, f"cfg-{port}.json")
    cfg = {
        "version": 2,
        "listen": f"127.0.0.1:{port}",
        "relay_token": "smoke",
        "credentials_yaml": "/nonexistent",
        "secrets_file": os.path.join(tmpdir, f"secrets-{port}.json"),
        "state_file": os.path.join(tmpdir, f"state-{port}.json"),
        "pid_file": os.path.join(tmpdir, f"pid-{port}"),
        "usage_refresh_seconds": 15,
        "max_inflight": 1,
        "min_interval_seconds": 0.0,
        "queue_wait_seconds": budget,
        "request_log_path": "",
        "providers": {"opencode-go": {"adapter": "opencode-go", "base_url": url}},
        "accounts": [
            {"name": "a", "provider": "opencode-go", "key_env": "SMOKE_A"},
            {"name": "b", "provider": "opencode-go", "key_env": "SMOKE_B"},
        ],
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    env = dict(os.environ, SMOKE_A="k-a", SMOKE_B="k-b")
    proc = subprocess.Popen([sys.executable, "-m", "opencode_pool", "serve", "--config", cfg_path],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.1)
    return up, proc, base, cfg_path, port, tmpdir


def body(tag):
    return {"model": "deepseek-v4.1-flash", "stream": False,
            "messages": [{"role": "user", "content": f"负载测试 {tag}-{time.time()}"}]}


def hit(base, tag):
    started = time.time()
    try:
        r = requests.post(f"{base}/v1/chat/completions", json=body(tag), timeout=60,
                          headers={"Authorization": "Bearer smoke"})
        return r.status_code, round(time.time() - started, 2)
    except requests.RequestException as exc:
        return f"EXC:{type(exc).__name__}", round(time.time() - started, 2)


def run_case(label, n, budget):
    up, proc, base, cfg_path, port, tmpdir = build(budget)
    FakeUpstream.calls = 0
    try:
        with cf.ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(lambda i: hit(base, f"{label}-{i}"), range(n)))
        ok = sum(1 for s, _ in results if s == 200)
        fail = [(s, d) for s, d in results if s != 200]
        worst = max(d for _, d in results)
        print(f"[{label}] n={n} budget={budget}s ok={ok} fail={len(fail)} worst={worst}s")
        if fail:
            print("        失败样本:", fail[:6])
        print(f"        上游被调用 {FakeUpstream.calls} 次")
        return results
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        up.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"        relay 日志尾部: {out.strip().splitlines()[-3:] if out.strip() else '(空)'}")


def case_client_abort():
    """排队中途断开：上游不该因为这条死连接被调用。

    两个号都被 3s 的长请求占着，客户端 0.15s 就断开 —— 它一次都不该打到上游。
    """
    global UPSTREAM_DELAY
    UPSTREAM_DELAY = 3.0
    up, proc, base, cfg_path, port, tmpdir = build(10)
    FakeUpstream.calls = 0
    FakeUpstream.seen = []
    try:
        blockers = []
        for tag in ("hold-1", "hold-2"):       # 先占满两个并发位
            t = threading.Thread(target=hit, args=(base, tag), daemon=True)
            t.start()
            blockers.append(t)
        for _ in range(100):                   # 等两个占位请求真的打到上游
            with FakeUpstream.lock:
                if len(FakeUpstream.seen) >= 2:
                    break
            time.sleep(0.05)
        try:
            requests.post(f"{base}/v1/chat/completions", json=body("abort"),
                          timeout=0.15, headers={"Authorization": "Bearer smoke"})
        except requests.RequestException:
            pass                                    # 客户端主动超时走人
        for t in blockers:
            t.join(timeout=30)
        time.sleep(0.2)
        with FakeUpstream.lock:
            leaked = [c for c in FakeUpstream.seen if c.startswith("负载测试 abort")]
        print(f"[client-abort] 上游收到的请求 {len(FakeUpstream.seen)} 条，"
              f"其中来自已断开客户端 {len(leaked)} 条（应为 0）")
        return not leaked
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        up.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    a = run_case("A-容量够", 8, 10)
    b = run_case("B-容量不够", 20, 3)
    c = case_client_abort()

    problems = []
    if any(s != 200 for s, _ in a):
        problems.append("A 不该失败")
    slow_503 = [d for s, d in b if s != 200 and d > 3 + 3]
    if slow_503:
        problems.append(f"B 出现超预算的失败等待: {slow_503}")
    if not c:
        problems.append("C 为死连接打了上游")
    print("\n结论:", "PASS" if not problems else f"FAIL {problems}")
