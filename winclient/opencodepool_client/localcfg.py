"""离线改中转配置：客户端在"服务没起来"的时候也能加号。

为什么需要这条路径：中转在 `accounts` 为空时 `validate()` 就报错退出，
所以"先起服务再从 HTTP 加第一个号"是死循环。客户端因此自己实现了与
`server.py::_create_account` 等价的写盘逻辑 —— 格式必须与上游一致：

  pool.config.json  : {"version":2, "listen":..., "providers":{...}, "accounts":[...]}
  secrets.json      : {"version":1, "secrets": {slot: {"value":..., "updated_at":...}}}

写盘一律"先备份、再原子替换"，和面板的 `*.bak-<时间戳>-panel` 一个习惯。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import tempfile
import time

from . import paths

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
DEFAULT_PROVIDER = "opencode-go"


class LocalConfigError(RuntimeError):
    pass


# ── 通用读写 ─────────────────────────────────────────────
def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise LocalConfigError(f"文件不存在：{path}") from exc
    except (OSError, ValueError) as exc:
        raise LocalConfigError(f"读不了 {path}：{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocalConfigError(f"{path} 不是一个 JSON 对象")
    return data


def backup(path: str) -> str:
    """复制一份 `.bak-<时间戳>-client`；文件不存在则返回空串。"""
    if not os.path.isfile(path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{path}.bak-{stamp}-client"
    try:
        with open(path, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        return target
    except OSError:
        return ""


def _atomic_write(path: str, data: dict, mode: int = 0o600) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cfg-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── 配置 ─────────────────────────────────────────────────
def load_config(config_path: str) -> dict:
    return read_json(paths.expand(config_path))


def save_config(config_path: str, raw: dict) -> str:
    """写配置并返回备份路径（备份失败不阻塞写入）。"""
    config_path = paths.expand(config_path)
    made = backup(config_path)
    _atomic_write(config_path, raw)
    return made


def ensure_config(config_path: str) -> tuple[dict, bool]:
    """读配置；不存在就按 CLI 的 `init` 模板生成一份（含随机 relay_token）。"""
    config_path = paths.expand(config_path)
    if os.path.isfile(config_path):
        raw = load_config(config_path)
        if not raw.get("relay_token"):
            raw["relay_token"] = "pool-" + _secrets.token_urlsafe(24)
            save_config(config_path, raw)
        return raw, False
    raw = default_config(config_path)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    _atomic_write(config_path, raw)
    return raw, True


def default_config(config_path: str) -> dict:
    """与 `opencode_pool/cli.py::TEMPLATE` 对齐；只补默认值，不改上游语义。"""
    home = paths.home()
    directory = os.path.dirname(paths.expand(config_path)) or os.path.join(home, ".dsh", "opencode-pool")
    return {
        "version": 2,
        "listen": "127.0.0.1:8790",
        "relay_token": "pool-" + _secrets.token_urlsafe(24),
        "credentials_yaml": os.path.join(home, ".dsh", ".credentials.yaml"),
        "secrets_file": os.path.join(directory, "secrets.json"),
        "state_file": "pool_state.json",
        "pid_file": "relay.pid",
        "usage_refresh_seconds": 90,
        "upstream_timeout": 900,
        "providers": {
            DEFAULT_PROVIDER: {
                "adapter": "opencode-go",
                "base_url": "https://opencode.ai/inference/go/v1",
            }
        },
        "accounts": [],
    }


def provider_ids(raw: dict) -> list[str]:
    providers = raw.get("providers")
    if isinstance(providers, dict) and providers:
        return list(providers)
    return [DEFAULT_PROVIDER]


def provider_adapter(raw: dict, provider_id: str) -> str:
    providers = raw.get("providers") or {}
    entry = providers.get(provider_id) or {}
    return str(entry.get("adapter") or provider_id or DEFAULT_PROVIDER)


def listen(raw: dict) -> tuple[str, int]:
    value = str(raw.get("listen") or "127.0.0.1:8790")
    host, _, port = value.rpartition(":")
    if not host:
        host, port = "127.0.0.1", value
    try:
        return host or "127.0.0.1", int(port)
    except ValueError:
        return "127.0.0.1", 8790


def loopback_base(raw: dict) -> str:
    """中转只可能在本机：`0.0.0.0` / `::` 这类通配地址换成回环再访问。"""
    host, port = listen(raw)
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def accounts(raw: dict) -> list[dict]:
    items = raw.get("accounts")
    return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []


def find_account(raw: dict, name: str) -> dict | None:
    return next((a for a in accounts(raw) if a.get("name") == name), None)


# ── 密钥仓 ───────────────────────────────────────────────
def secrets_path(config_path: str, raw: dict | None = None) -> str:
    if raw is None:
        try:
            raw = load_config(config_path)
        except LocalConfigError:
            raw = {}
    value = str((raw or {}).get("secrets_file") or "")
    if not value:
        return os.path.join(paths.config_dir(config_path), "secrets.json")
    return paths.resolve_relative(config_path, value, "secrets.json")


def read_secrets(path: str) -> dict:
    try:
        raw = read_json(path)
    except LocalConfigError:
        return {}
    items = raw.get("secrets")
    if not isinstance(items, dict):
        return {}
    return {k: dict(v) for k, v in items.items() if isinstance(v, dict)}


def write_secrets(path: str, data: dict) -> None:
    _atomic_write(path, {"version": 1, "secrets": {k: dict(v) for k, v in data.items()}})


def secret_set(path: str, slot: str, value: str) -> None:
    if not slot or not value:
        raise LocalConfigError("密钥槽位与值都不能为空")
    data = read_secrets(path)
    data[slot] = {"value": value.strip(), "updated_at": time.time()}
    write_secrets(path, data)


def secret_delete(path: str, slot: str) -> bool:
    data = read_secrets(path)
    if slot in data:
        del data[slot]
        write_secrets(path, data)
        return True
    return False


def fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def secret_describe(path: str, slot: str) -> dict:
    entry = read_secrets(path).get(slot) or {}
    value = str(entry.get("value") or "")
    return {
        "configured": bool(value),
        "fingerprint": fingerprint(value),
        "updated_at": entry.get("updated_at"),
    }


def slot_for(name: str) -> str:
    return "POOL_KEY_" + re.sub(r"[^A-Za-z0-9]+", "_", name).upper()


# ── 账号动作（离线，等价于面板的 HTTP 动作） ──────────────
def add_account(config_path: str, name: str, provider: str, key: str,
                session_prefix: str = "") -> dict:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise LocalConfigError("账号名只能由字母、数字、_ . - 组成（1–40 字）")
    key = (key or "").strip()
    if not key:
        raise LocalConfigError("密钥不能为空")
    raw = load_config(config_path)
    if provider not in raw.get("providers", {}):
        raise LocalConfigError(f"没有这个上游：{provider}")
    if find_account(raw, name):
        raise LocalConfigError(f"账号 {name} 已存在")

    slot = slot_for(name)
    account = {"name": name, "provider": provider, "key_env": slot}
    if session_prefix:
        account["session_prefix"] = session_prefix
    elif provider_adapter(raw, provider) == "opencode-go":
        account["session_prefix"] = f"dsh-pool-{name}"
    raw.setdefault("accounts", []).append(account)
    made = save_config(config_path, raw)
    secret_set(secrets_path(config_path, raw), slot, key)
    return {"slot": slot, "backup": made}


def rotate_key(config_path: str, name: str, key: str) -> dict:
    key = (key or "").strip()
    if not key:
        raise LocalConfigError("密钥不能为空")
    raw = load_config(config_path)
    account = find_account(raw, name)
    if not account:
        raise LocalConfigError(f"没有账号 {name}")
    slot = str(account.get("key_env") or "").strip()
    if not slot:
        raise LocalConfigError("该账号没有凭据槽位；请先给它配 key_env")
    secret_set(secrets_path(config_path, raw), slot, key)
    return {"slot": slot}


def remove_account(config_path: str, name: str) -> dict:
    raw = load_config(config_path)
    account = find_account(raw, name)
    if not account:
        raise LocalConfigError(f"没有账号 {name}")
    slot = str(account.get("key_env") or "").strip()
    raw["accounts"] = [a for a in accounts(raw) if a.get("name") != name]
    made = save_config(config_path, raw)
    dropped = False
    if slot and not any(a.get("key_env") == slot for a in accounts(raw)):
        dropped = secret_delete(secrets_path(config_path, raw), slot)
    return {"backup": made, "secret_dropped": dropped}


def set_enabled(config_path: str, name: str, enabled: bool) -> None:
    raw = load_config(config_path)
    account = find_account(raw, name)
    if not account:
        raise LocalConfigError(f"没有账号 {name}")
    account["enabled"] = bool(enabled)
    save_config(config_path, raw)


def options_snapshot(config_path: str) -> dict:
    """设置页要展示的中转参数（缺失时给上游默认值）。"""
    raw = load_config(config_path)
    from .relay_defaults import CFG_DEFAULTS

    values = {}
    for key, default in CFG_DEFAULTS.items():
        values[key] = raw.get(key, default)
    values["listen"] = raw.get("listen", "127.0.0.1:8790")
    values["relay_token"] = raw.get("relay_token", "")
    return values


def update_options(config_path: str, values: dict) -> str:
    allowed = set(__import__("opencodepool_client.relay_defaults", fromlist=["CFG_DEFAULTS"]).CFG_DEFAULTS)
    allowed.add("listen")
    raw = load_config(config_path)
    for key, value in values.items():
        if key in allowed and value is not None:
            raw[key] = value
    return save_config(config_path, raw)
