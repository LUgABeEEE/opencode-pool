"""把中转接进别的客户端：一键写入、一键还原。

这是桌面客户端相对 Web 面板唯一不可替代的增量：面板只能"告诉你填什么"，
客户端可以**真的把 base_url / token 写进 Codex 的 config.toml 和用户环境变量**，
并且在写之前留好还原点。

两条铁律（抄自 cc-switch / codex-tools 的做法）：
  * 只动自己那一段，别人的配置不碰；
  * 首次写入前存一份原始快照，还原就是回到那一刻。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from . import paths

CODEX_PROVIDER_ID = "opencode-pool"
CODEX_BACKUP_SUFFIX = ".opencode-pool-backup"
ENV_BACKUP_NAME = "env-backup.json"

# 会被"一键写入"的环境变量：名字 → 说明。默认只勾前两个。
ENV_TARGETS = {
    "OPENAI_BASE_URL": "OpenAI SDK / 兼容客户端的默认端点",
    "OPENCODE_POOL_TOKEN": "我们自己的 token 槽位（推荐填它，不动别人的 OPENAI_API_KEY）",
    "OPENAI_API_KEY": "会顶掉你原有的 OpenAI key，谨慎",
    "ANTHROPIC_BASE_URL": "Claude Code 走的 Anthropic 端点",
    "ANTHROPIC_AUTH_TOKEN": "Claude Code 的鉴权 token",
}
DEFAULT_ENV_TARGETS = ("OPENAI_BASE_URL", "OPENCODE_POOL_TOKEN")


class ClientConfigError(RuntimeError):
    pass


# ── 路径 ─────────────────────────────────────────────────
def codex_config_path() -> str:
    return os.path.join(paths.home(), ".codex", "config.toml")


def env_backup_path() -> str:
    return os.path.join(paths.app_dir(), ENV_BACKUP_NAME)


# ── Codex config.toml ───────────────────────────────────
def codex_snippet(base_url: str, model: str = "") -> str:
    model_line = f'model = "{model}"\n' if model else ""
    return (
        f'model_provider = "{CODEX_PROVIDER_ID}"\n'
        f"{model_line}\n"
        f"[model_providers.{CODEX_PROVIDER_ID}]\n"
        f'name = "OpenCode 号池"\n'
        f'base_url = "{base_url}/v1"\n'
        f'wire_api = "chat"\n'
        f"requires_openai_auth = false\n"
        f'env_key = "OPENCODE_POOL_TOKEN"\n'
    )


def _strip_our_block(text: str) -> str:
    """删掉 `model_provider = "opencode-pool"` 与 `[model_providers.opencode-pool]` 段。"""
    out: list[str] = []
    skipping = False
    header = f"[model_providers.{CODEX_PROVIDER_ID}"
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped.startswith(header)
            if skipping:
                continue
        if skipping:
            continue
        if re.match(r'^\s*model_provider\s*=\s*"[^"]*"', line) and CODEX_PROVIDER_ID in line:
            continue
        out.append(line)
    cleaned = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def preview_codex(base_url: str, model: str = "") -> tuple[str, str]:
    """返回 (写入后的完整内容, 会不会新建文件)。不写盘，纯预览。"""
    path = codex_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
        exists = True
    except OSError:
        current = ""
        exists = False
    head = _strip_our_block(current).rstrip("\n")
    body = codex_snippet(base_url, model).rstrip("\n")
    merged = (head + "\n\n" if head else "") + body + "\n"
    return merged, not exists


def apply_codex(base_url: str, model: str = "") -> dict:
    """写入 config.toml；返回 {path, backup, created, content}。"""
    path = codex_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = ""
    if os.path.isfile(path):
        snapshot = path + CODEX_BACKUP_SUFFIX
        if not os.path.isfile(snapshot):          # 只留"第一次接入前"的那份原貌
            try:
                with open(path, "rb") as src, open(snapshot, "wb") as dst:
                    dst.write(src.read())
                backup = snapshot
            except OSError as exc:
                raise ClientConfigError(f"存还原点失败：{exc}") from exc
        else:
            backup = snapshot
        # 每次写入都另存一份带时间戳的，方便对照
        try:
            with open(path, "rb") as src, open(f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-client", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    merged, created = preview_codex(base_url, model)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(merged)
    except OSError as exc:
        raise ClientConfigError(f"写 {path} 失败：{exc}") from exc
    return {"path": path, "backup": backup, "created": created, "content": merged}


def codex_state(base_url: str) -> dict:
    path = codex_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {"exists": False, "wired": False, "path": path}
    wired = (f"[model_providers.{CODEX_PROVIDER_ID}]" in text) and (base_url.split("//")[-1] in text)
    return {
        "exists": True,
        "wired": wired,
        "has_restore_point": os.path.isfile(path + CODEX_BACKUP_SUFFIX),
        "provider_selected": f'model_provider = "{CODEX_PROVIDER_ID}"' in text,
        "path": path,
    }


def restore_codex() -> dict:
    """把 config.toml 恢复成首次接入前的样子（若没有还原点则只删我们那段）。"""
    path = codex_config_path()
    snapshot = path + CODEX_BACKUP_SUFFIX
    if os.path.isfile(snapshot):
        try:
            with open(snapshot, "rb") as src, open(path, "wb") as dst:
                dst.write(src.read())
        except OSError as exc:
            raise ClientConfigError(f"还原失败：{exc}") from exc
        return {"restored": True, "from": snapshot, "path": path}
    if not os.path.isfile(path):
        return {"restored": False, "reason": "config.toml 不存在，没什么要还原的"}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise ClientConfigError(f"读 {path} 失败：{exc}") from exc
    cleaned = _strip_our_block(text)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(cleaned)
    except OSError as exc:
        raise ClientConfigError(f"写 {path} 失败：{exc}") from exc
    return {"restored": True, "from": "inline-strip", "path": path}


# ── 用户环境变量 ────────────────────────────────────────
def _open_env_key(write: bool = False):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, access)


def read_env(name: str) -> str:
    try:
        import winreg

        with _open_env_key() as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except (ImportError, FileNotFoundError, OSError):
        return ""


def env_preview(base_url: str, token: str, selected: tuple[str, ...]) -> dict[str, str]:
    values = {
        "OPENAI_BASE_URL": f"{base_url}/v1",
        "OPENCODE_POOL_TOKEN": token,
        "OPENAI_API_KEY": token,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
    }
    return {name: values.get(name, "") for name in selected if name in values}


def env_state() -> dict:
    return {name: read_env(name) for name in ENV_TARGETS}


def apply_env(values: dict[str, str]) -> dict:
    """写用户环境变量；首次写入前把**原值**存成 json，供还原。"""
    try:
        import winreg
    except ImportError as exc:
        raise ClientConfigError("这台机器上没有 winreg，改不了环境变量") from exc

    path = env_backup_path()
    backup: dict[str, str | None] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                backup = json.load(f)
        except (OSError, ValueError):
            backup = {}
    changed = []
    for name, value in values.items():
        if name not in ENV_TARGETS:
            continue
        if name not in backup:                    # 只记录"第一次看到"的原值
            backup[name] = read_env(name) or None
        try:
            with _open_env_key(write=True) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                changed.append(name)
        except OSError as exc:
            raise ClientConfigError(f"写环境变量 {name} 失败：{exc}") from exc
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _broadcast_env_change()
    return {"changed": changed, "backup": path}


def restore_env() -> dict:
    """按备份把环境变量恢复（原来没有的就删掉）。"""
    try:
        import winreg
    except ImportError as exc:
        raise ClientConfigError("这台机器上没有 winreg") from exc
    path = env_backup_path()
    if not os.path.isfile(path):
        return {"restored": [], "reason": "没有备份，说明没被客户端改过"}
    try:
        with open(path, encoding="utf-8") as f:
            backup = json.load(f)
    except (OSError, ValueError) as exc:
        raise ClientConfigError(f"读备份失败：{exc}") from exc
    restored = []
    for name, value in backup.items():
        try:
            with _open_env_key(write=True) as key:
                if value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
                else:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
            restored.append(name)
        except OSError:
            continue
    try:
        os.unlink(path)
    except OSError:
        pass
    _broadcast_env_change()
    return {"restored": restored}


def _broadcast_env_change() -> None:
    """通知资源管理器环境变量变了，否则新开的终端还是旧值。"""
    try:
        import ctypes

        result = ctypes.c_ulong(0)
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, ctypes.byref(result))
    except (AttributeError, OSError):
        pass


# ── 各种客户端的接入片段 ─────────────────────────────────
def snippets(base_url: str, token: str, model: str = "") -> dict[str, str]:
    model = model or "deepseek-v4.1-flash"
    return {
        "OpenAI SDK (Python)": (
            "from openai import OpenAI\n\n"
            f'client = OpenAI(base_url="{base_url}/v1", api_key="{token}")\n'
            f'print(client.chat.completions.create(model="{model}",\n'
            '      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)\n'
        ),
        "环境变量": (
            f'set OPENAI_BASE_URL={base_url}/v1\n'
            f"set OPENCODE_POOL_TOKEN={token}\n"
            "\n"
            "# PowerShell\n"
            f'$env:OPENAI_BASE_URL="{base_url}/v1"\n'
            f'$env:OPENCODE_POOL_TOKEN="{token}"\n'
        ),
        "Codex CLI": (
            "~/.codex/config.toml\n\n" + codex_snippet(base_url, model)
        ),
        "Claude Code": (
            "# /v1/messages 走 Anthropic 协议，中转直接支持\n"
            f'set ANTHROPIC_BASE_URL={base_url}\n'
            f"set ANTHROPIC_AUTH_TOKEN={token}\n"
        ),
        "curl 自检": (
            f'curl "{base_url}/v1/chat/completions" -H "Authorization: Bearer {token}" '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"model":"{model}","messages":[{{"role":"user","content":"hi"}}]}}\'\n'
        ),
    }


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
