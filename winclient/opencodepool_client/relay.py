"""中转的 HTTP 面 + 进程生命周期。

两件事分开写：
  * `RelayClient` 只讲 HTTP —— 服务是别人拉起来的（DSH 插件 / start.sh / 客户端）
    都一样能用；
  * `RelayProcess` 只管"我拉起来的这个子进程" —— 不是自己起的（attach 模式）
    就不会去杀，避免把用户别处正在跑的中转踢掉。

token 每次都从配置里现读，不做缓存：配置文件 1KB，读一次可以忽略不计，
而任何缓存都会踩"面板刚改了 token，客户端还拿旧的"这类难查的问题。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from . import localcfg, paths


class RelayError(RuntimeError):
    """中转连不上 / 返回了非 2xx —— message 直接可以给用户看。"""


# ── HTTP 客户端 ──────────────────────────────────────────
class RelayClient:
    def __init__(self, config_path: str):
        self.config_path = paths.expand(config_path)

    # —— 基础 ——
    def _raw_config(self) -> dict:
        try:
            return localcfg.load_config(self.config_path)
        except localcfg.LocalConfigError:
            return {}

    def base_url(self) -> str:
        return localcfg.loopback_base(self._raw_config())

    def token(self) -> str:
        return str(self._raw_config().get("relay_token") or "")

    def configured(self) -> bool:
        return os.path.isfile(self.config_path)

    def request(self, method: str, path: str, payload: dict | None = None,
                timeout: float = 20.0, auth: bool = True) -> tuple[int, dict]:
        url = self.base_url() + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, _decode(body)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except OSError:
                pass
            return exc.code, _decode(body) or {"error": f"HTTP {exc.code}"}
        except urllib.error.URLError as exc:
            raise RelayError(f"连不上中转（{url}）：{_reason(exc)}") from exc
        except (TimeoutError, OSError) as exc:
            raise RelayError(f"中转没响应（{url}）：{type(exc).__name__}: {exc}") from exc

    def _ok(self, method: str, path: str, payload: dict | None = None,
            timeout: float = 25.0) -> dict:
        status, body = self.request(method, path, payload, timeout)
        if status >= 400 or (isinstance(body, dict) and body.get("error") and status != 200):
            message = ""
            if isinstance(body, dict):
                error = body.get("error")
                message = error.get("message") if isinstance(error, dict) else str(error or "")
            raise RelayError(f"HTTP {status}：{message or '未知错误'}")
        return body if isinstance(body, dict) else {}

    # —— 只读 ——
    def healthy(self, timeout: float = 1.5) -> bool:
        try:
            status, _ = self.request("GET", "/healthz", timeout=timeout, auth=False)
        except RelayError:
            return False
        return status in (200, 503)   # 503 = 进程活着但没可用账号，也算在跑

    def readiness(self) -> tuple[bool, dict]:
        try:
            status, body = self.request("GET", "/healthz", timeout=2.5, auth=False)
        except RelayError as exc:
            return False, {"error": str(exc)}
        return status == 200, body

    def snapshot(self) -> dict:
        return self._ok("GET", "/v1/usage")

    def schema(self) -> dict:
        return self._ok("GET", "/v1/pool/schema")

    def models(self) -> dict:
        return self._ok("GET", "/v1/models", timeout=30)

    def metrics_text(self) -> str:
        status, body = self.request("GET", "/metrics", timeout=6)
        if status >= 400:
            raise RelayError(f"HTTP {status}")
        return json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else str(body)

    # —— 动作 ——
    def refresh(self) -> dict:
        return self._ok("POST", "/v1/pool/refresh", {}, timeout=120)

    def clear_cooldowns(self) -> dict:
        return self._ok("POST", "/v1/pool/clear-cooldowns", {})

    def reload(self) -> dict:
        return self._ok("POST", "/v1/pool/reload", {})

    def add_account(self, name: str, provider: str, key: str, session_prefix: str = "") -> dict:
        payload = {"name": name, "provider": provider, "key": key}
        if session_prefix:
            payload["session_prefix"] = session_prefix
        return self._ok("POST", "/v1/pool/accounts", payload, timeout=60)

    def rotate_key(self, name: str, key: str) -> dict:
        return self._ok("POST", "/v1/pool/account/key", {"name": name, "key": key}, timeout=60)

    def remove_account(self, name: str) -> dict:
        return self._ok("POST", "/v1/pool/account/remove", {"name": name})

    def toggle_account(self, name: str, enabled: bool) -> dict:
        return self._ok("POST", "/v1/pool/account", {"name": name, "enabled": enabled})

    # —— 自检：真的发一次对话，验证"链路通不通" ——
    def probe_chat(self, model: str = "", timeout: float = 120.0) -> dict:
        model = model or "deepseek-v4.1-flash"
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 16, "stream": False}
        started = time.time()
        status, body = self.request("POST", "/v1/chat/completions", payload, timeout=timeout)
        elapsed = time.time() - started
        text = ""
        if isinstance(body, dict):
            choices = body.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                text = str(message.get("content") or "")[:200]
        return {"status": status, "body": body, "seconds": elapsed, "text": text,
                "ok": 200 <= status < 300}


def _decode(body: bytes):
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {"raw": body.decode("utf-8", "replace")[:2000]}


def _reason(exc: urllib.error.URLError) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ConnectionRefusedError):
        return "中转没在跑（连接被拒绝）"
    return f"{type(reason).__name__}: {reason}" if reason else str(exc)


# ── 进程托管 ─────────────────────────────────────────────
class RelayProcess:
    """拉起/收掉 `python -m opencode_pool serve`。

    日志：子进程 stdout/stderr 合并 → 追加写入 `relay.log`，同时留最近 600 行
    给界面实时看。用管道而不是直接重定向文件，是为了"客户端开着的时候能立刻看到
    启动了没有"，关掉客户端后日志文件仍在，两边都不丢。
    """

    MAX_LINES = 600

    def __init__(self, settings, on_line=None, on_exit=None):
        self.settings = settings
        self.on_line = on_line or (lambda line: None)
        self.on_exit = on_exit or (lambda code: None)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self.lines: deque[tuple[float, str]] = deque(maxlen=self.MAX_LINES)
        self.attached = False          # True = 用的是别处已经在跑的实例
        self.started_at = 0.0
        self.last_error = ""

    # —— 状态 ——
    @property
    def alive(self) -> bool:
        if self.attached:
            return RelayClient(self.settings.config_path()).healthy()
        return bool(self._proc and self._proc.poll() is None)

    def client(self) -> RelayClient:
        return RelayClient(self.settings.config_path())

    def log_path(self) -> str:
        return self.settings.relay_log()

    def tail(self, count: int = 200) -> list[str]:
        return [line for _, line in list(self.lines)[-count:]]

    # —— 启动 ——
    def start(self, wait_seconds: float = 20.0) -> tuple[bool, str]:
        client = self.client()
        if client.healthy():
            self.attached = True
            self.last_error = ""
            return True, "已连上正在运行的中转（不是本客户端拉起的，停止按钮不会杀掉它）"

        project = self.settings.project_dir()
        if not project:
            self.last_error = "找不到 opencode-pool 项目目录，去「设置」里指一下"
            return False, self.last_error
        entry = os.path.join(project, "opencode_pool", "__main__.py")
        if not os.path.isfile(entry):
            self.last_error = f"{entry} 不存在 —— 项目目录指错了"
            return False, self.last_error

        python = self.settings.python()
        ok, detail = paths.python_has_requests(python)
        if not ok:
            self.last_error = (f"这个 Python 没有 requests：{detail}；"
                               f"装一下：\"{python}\" -m pip install requests")
            return False, self.last_error

        # 配置不存在就现生成一份（等价 `opencode_pool init`）
        config_path = self.settings.config_path()
        try:
            _, created = localcfg.ensure_config(config_path)
        except localcfg.LocalConfigError as exc:
            self.last_error = str(exc)
            return False, self.last_error
        if created:
            self._push(f"[client] 已生成配置 {config_path}")

        log_path = self.log_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PYTHONUTF8", "1")

        args = [python, "-m", "opencode_pool", "serve", "--config", config_path]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                args, cwd=project, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            self.last_error = f"拉不起来：{type(exc).__name__}: {exc}"
            return False, self.last_error

        self.attached = False
        self.started_at = time.time()
        self._stop_flag.clear()
        self._push(f"[client] $ {' '.join(args)}")
        self._reader = threading.Thread(target=self._pump, args=(log_path,), daemon=True,
                                        name="relay-log-pump")
        self._reader.start()

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                self.last_error = (f"中转启动即退出（code={code}）。日志最后几行：\n"
                                   + "\n".join(self.tail(6)))
                return False, self.last_error
            if client.healthy(timeout=1.0):
                self.last_error = ""
                return True, f"中转已就绪（{client.base_url()}）"
            time.sleep(0.25)
        self.last_error = "20 秒内没起来，看看日志页/relay.log"
        return False, self.last_error

    def _pump(self, log_path: str) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            handle = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            handle = None
        try:
            for line in proc.stdout:
                if self._stop_flag.is_set():
                    break
                text = line.rstrip("\r\n")
                self._push(text)
                if handle:
                    try:
                        handle.write(text + "\n")
                        handle.flush()
                    except OSError:
                        pass
        except (ValueError, OSError):
            pass
        finally:
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass
            code = proc.poll()
            if not self._stop_flag.is_set():
                self._push(f"[client] 中转进程结束（code={code}）")
            try:
                self.on_exit(code)
            except Exception:  # noqa: BLE001 - UI 回调不该拖垮读线程
                pass

    def _push(self, line: str) -> None:
        stamp = time.time()
        self.lines.append((stamp, line))
        try:
            self.on_line(line)
        except Exception:  # noqa: BLE001
            pass

    # —— 停止 ——
    def stop(self, timeout: float = 8.0) -> tuple[bool, str]:
        if self.attached:
            self.attached = False
            return True, "这是别处启动的中转，客户端没有动它（去那边停）"
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            return True, "中转本来就没在跑"
        self._stop_flag.set()
        try:
            proc.terminate()
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.15)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            time.sleep(0.3)
        code = proc.poll()
        self._proc = None
        self._push(f"[client] 已停止中转（code={code}）")
        return True, "中转已停止"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        time.sleep(0.4)
        return self.start()
