"""把中转接进别的客户端：一键写入、一键还原。

这是桌面客户端相对 Web 面板唯一不可替代的增量：面板只能"告诉你填什么"，
客户端可以**真的把 base_url / token 写进 Codex 的 config.toml 和用户环境变量**，
并且在写之前留好还原点。

两条铁律（抄自 cc-switch / codex-tools 的做法）：
  * 只动自己那一段，别人的配置不碰；
  * 首次写入前存一份原始快照，还原就是回到那一刻。

TOML 的两条硬约束（这里踩过坑）：
  * 顶层键必须落在**第一个表头之前**——追加到文件末尾会被解析成最后一张表的键；
  * 顶层 `model_provider` / `model` 每种只能出现一次，所以写入是"替换"而不是"追加"。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime

from . import paths

try:                                    # Python ≥3.11；3.10 只做结构写入，跳过解析校验
    import tomllib as _toml
except ImportError:
    _toml = None

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
def _toml_string(value: str) -> str:
    """TOML basic string：反斜杠与双引号必须转义，否则写出来是坏文件。"""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _root_kv_lines(base_url: str, model: str = "") -> list[str]:
    lines = [f"model_provider = {_toml_string(CODEX_PROVIDER_ID)}"]
    if model:
        lines.append(f"model = {_toml_string(model)}")
    return lines


def _provider_block_lines(base_url: str) -> list[str]:
    return [
        f"[model_providers.{CODEX_PROVIDER_ID}]",
        'name = "OpenCode 号池"',
        f"base_url = {_toml_string(_clean_base_url(base_url) + '/v1')}",
        'wire_api = "chat"',
        "requires_openai_auth = false",
        'env_key = "OPENCODE_POOL_TOKEN"',
    ]


def codex_snippet(base_url: str, model: str = "") -> str:
    """展示用的完整片段（顶层键在前、表在后——顺序本身就是正确写法）。"""
    return "\n".join(_root_kv_lines(base_url, model) + [""] + _provider_block_lines(base_url)) + "\n"


def _find_root_end(lines: list[str]) -> int:
    """第一个表头的位置；多行字符串里的 `[` 不算（粗略扫描，够用）。"""
    delim = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if delim:
            if stripped.count(delim) % 2 == 1:
                delim = ""
            continue
        for candidate in ('"""', "'''"):
            if stripped.count(candidate) % 2 == 1:
                delim = candidate
                break
        if delim:
            continue
        if stripped.startswith("["):
            return index
    return len(lines)


def _drop_provider_block(lines: list[str]) -> list[str]:
    """删掉 `[model_providers.opencode-pool]` 这一整段（只认精确表名，`-alt` 之类不误伤）。"""
    header = re.compile(r"^\[\s*model_providers\.%s\s*\]\s*(#.*)?$" % re.escape(CODEX_PROVIDER_ID))
    out: list[str] = []
    skipping = False
    delim = ""
    for line in lines:
        stripped = line.strip()
        if delim:
            if stripped.count(delim) % 2 == 1:
                delim = ""
            if not skipping:
                out.append(line)
            continue
        if stripped.startswith("["):
            skipping = bool(header.match(stripped))
            if skipping:
                continue
        for candidate in ('"""', "'''"):
            if stripped.count(candidate) % 2 == 1:
                delim = candidate
                break
        if skipping:
            continue
        out.append(line)
    return out


def _merge_toml(current: str, base_url: str, model: str = "") -> str:
    """把"我们那段"合并进现有 TOML：顶层键替换、表段整段替换，其余原样保留。"""
    lines = _drop_provider_block(current.splitlines(keepends=True))
    root_end = _find_root_end(lines)
    root = lines[:root_end]
    rest = lines[root_end:]

    kept_root: list[str] = []
    for line in root:
        match = re.match(r"^\s*(model_provider|model)\s*=", line)
        # model 只在需要设置时才替换；用户自己配的 model 不动
        if match and (match.group(1) == "model_provider" or model):
            continue
        kept_root.append(line)

    while kept_root and not kept_root[-1].strip():
        kept_root.pop()
    block = kept_root + ([""] if kept_root else []) + [line + "\n" for line in _root_kv_lines(base_url, model)]

    merged = block
    if rest:
        while merged and not merged[-1].strip():
            merged.pop()
        merged = merged + (["\n"] if merged else []) + rest
    while merged and not merged[-1].strip():
        merged.pop()
    merged = merged + ["\n", "\n"] + [line + "\n" for line in _provider_block_lines(base_url)]
    return _validate_toml("".join(merged))


def _validate_toml(text: str) -> str:
    """写盘前自校验：解析不过就拒绝写入，别把用户的 config.toml 弄坏。"""
    if _toml is None:
        return text
    try:
        parsed = _toml.loads(text)
    except Exception as exc:  # noqa: BLE001 - tomllib 的异常类型随版本不同
        raise ClientConfigError(f"生成的 config.toml 不是合法 TOML，已放弃写入：{exc}") from exc
    provider = (parsed.get("model_providers") or {}).get(CODEX_PROVIDER_ID)
    if not isinstance(provider, dict):
        raise ClientConfigError("生成的 config.toml 里读不到 [model_providers.opencode-pool]，已放弃写入")
    return text


def _read_text(path: str) -> tuple[str, str, bool]:
    """读全文，返回 (文本, 换行风格, 是否存在)；编码不对时给可读错误而不是抛 UnicodeDecodeError。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "", "\n", False
    except OSError as exc:
        raise ClientConfigError(f"读 {path} 失败：{exc}") from exc
    newline = "\r\n" if b"\r\n" in raw else "\n"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ClientConfigError(f"{path} 不是 UTF-8 编码，为安全起见没有改动它：{exc}") from exc
    return text, newline, True


def _atomic_write_text(path: str, text: str, newline: str = "\n") -> None:
    """同目录临时文件 + 原子替换；保留原换行风格（CRLF 不会被整份改成 LF）。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = text if newline == "\n" else text.replace("\n", newline)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        raise ClientConfigError(f"写 {path} 失败：{exc}") from exc
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _strip_our_block(text: str) -> str:
    """删掉 `model_provider = "opencode-pool"` 与 `[model_providers.opencode-pool]` 段。"""
    out: list[str] = []
    for line in _drop_provider_block(text.splitlines(keepends=True)):
        if re.search(r'^\s*model_provider\s*=\s*"%s"' % re.escape(CODEX_PROVIDER_ID), line):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def preview_codex(base_url: str, model: str = "", path: str = "") -> tuple[str, bool]:
    """返回 (写入后的完整内容, 会不会新建文件)。不写盘，纯预览。"""
    path = paths.expand(path) or codex_config_path()
    current, _newline, exists = _read_text(path)
    return _merge_toml(current, base_url, model), not exists


def apply_codex(base_url: str, model: str = "", path: str = "") -> dict:
    """写入 config.toml；返回 {path, backup, created, content}。"""
    path = paths.expand(path) or codex_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current, newline, exists = _read_text(path)
    merged = _merge_toml(current, base_url, model)

    backup = ""
    if exists:
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

    _atomic_write_text(path, merged, newline)
    return {"path": path, "backup": backup, "created": not exists, "content": merged}


def codex_state(base_url: str, path: str = "") -> dict:
    path = paths.expand(path) or codex_config_path()
    try:
        text, _newline, exists = _read_text(path)
    except ClientConfigError as exc:
        return {"exists": True, "wired": False, "path": path, "error": str(exc)}
    if not exists:
        return {"exists": False, "wired": False, "path": path}

    if _toml is not None:
        try:
            parsed = _toml.loads(text)
        except Exception:  # noqa: BLE001 - 用户手写的文件可能本来就不合法，不影响展示
            parsed = {}
        provider_selected = parsed.get("model_provider") == CODEX_PROVIDER_ID
        provider = (parsed.get("model_providers") or {}).get(CODEX_PROVIDER_ID) or {}
        wired = str(provider.get("base_url") or "").rstrip("/") == _clean_base_url(base_url) + "/v1"
    else:
        provider_selected = f'model_provider = "{CODEX_PROVIDER_ID}"' in text
        wired = (f"[model_providers.{CODEX_PROVIDER_ID}]" in text
                 and base_url.split("//")[-1] in text)
    return {
        "exists": True,
        "wired": wired,
        "has_restore_point": os.path.isfile(path + CODEX_BACKUP_SUFFIX),
        "provider_selected": provider_selected,
        "path": path,
    }


def restore_codex(path: str = "") -> dict:
    """把 config.toml 恢复成首次接入前的样子（若没有还原点则只删我们那段）。"""
    path = paths.expand(path) or codex_config_path()
    snapshot = path + CODEX_BACKUP_SUFFIX
    if os.path.isfile(snapshot):
        try:
            with open(snapshot, "rb") as src, open(path, "wb") as dst:
                dst.write(src.read())
        except OSError as exc:
            raise ClientConfigError(f"还原失败：{exc}") from exc
        return {"restored": True, "from": snapshot, "path": path}
    text, newline, exists = _read_text(path)
    if not exists:
        return {"restored": False, "reason": "config.toml 不存在，没什么要还原的"}
    _atomic_write_text(path, _strip_our_block(text), newline)
    return {"restored": True, "from": "inline-strip", "path": path}


# ── 用户环境变量 ────────────────────────────────────────
def _open_env_key(write: bool = False):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, access)


def _read_env_entry(name: str) -> tuple[str | None, int | None]:
    """读原值**和类型**：类型不对就是坏的（REG_EXPAND_SZ 写成 REG_SZ 后 %VAR% 不展开）。"""
    try:
        import winreg

        with _open_env_key() as key:
            value, kind = winreg.QueryValueEx(key, name)
            return (str(value) if value is not None else None), int(kind)
    except (ImportError, FileNotFoundError, OSError):
        return None, None


def read_env(name: str) -> str:
    value, _kind = _read_env_entry(name)
    return value or ""


def env_preview(base_url: str, token: str, selected: tuple[str, ...]) -> dict[str, str]:
    base = _clean_base_url(base_url)
    values = {
        "OPENAI_BASE_URL": f"{base}/v1",
        "OPENCODE_POOL_TOKEN": token,
        "OPENAI_API_KEY": token,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": token,
    }
    return {name: values.get(name, "") for name in selected if name in values}


def env_state() -> dict:
    return {name: read_env(name) for name in ENV_TARGETS}


def _load_env_backup(path: str) -> dict[str, dict]:
    """备份 → {name: {"value": str|None, "type": int|None}}；兼容旧的纯字符串格式。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for name, entry in raw.items():
        if isinstance(entry, dict):
            value = entry.get("value")
            kind = entry.get("type")
            out[str(name)] = {"value": None if value is None else str(value),
                              "type": kind if isinstance(kind, int) else None}
        else:
            out[str(name)] = {"value": None if entry is None else str(entry), "type": None}
    return out


def _save_env_backup(path: str, data: dict[str, dict]) -> None:
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".env-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        raise ClientConfigError(f"存环境变量还原点失败（没有改动任何变量）：{exc}") from exc


def _write_env(name: str, value: str | None, kind: int | None) -> None:
    import winreg

    with _open_env_key(write=True) as key:
        if value is None or value == "":
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, name, 0, kind or winreg.REG_SZ, value)


def apply_env(values: dict[str, str]) -> dict:
    """写用户环境变量：**先落还原点再动注册表**，中途失败按原值/原类型回滚。"""
    try:
        import winreg  # noqa: F401 - 只为确认平台能力
    except ImportError as exc:
        raise ClientConfigError("这台机器上没有 winreg，改不了环境变量") from exc

    path = env_backup_path()
    backup = _load_env_backup(path)
    updated = dict(backup)
    for name in values:
        if name in ENV_TARGETS and name not in updated:   # 只记录"第一次看到"的原值
            value, kind = _read_env_entry(name)
            updated[name] = {"value": value, "type": kind}
    _save_env_backup(path, updated)                       # 失败即中止，注册表没动

    changed: list[str] = []
    try:
        for name, value in values.items():
            if name not in ENV_TARGETS:
                continue
            _write_env(name, value, None)
            changed.append(name)
    except OSError as exc:
        for name in changed:                              # 回滚已写的
            original = updated.get(name) or {"value": None, "type": None}
            try:
                _write_env(name, original.get("value"), original.get("type"))
            except OSError:
                pass
        raise ClientConfigError(f"写环境变量失败，已回滚：{exc}") from exc
    _broadcast_env_change()
    return {"changed": changed, "backup": path}


def restore_env() -> dict:
    """按备份把环境变量恢复（原来没有的就删掉，类型按原样写回）。"""
    try:
        import winreg  # noqa: F401
    except ImportError as exc:
        raise ClientConfigError("这台机器上没有 winreg") from exc
    path = env_backup_path()
    if not os.path.isfile(path):
        return {"restored": [], "reason": "没有备份，说明没被客户端改过"}
    backup = _load_env_backup(path)
    if not backup:
        raise ClientConfigError(f"读备份失败或备份是空的：{path}")
    restored: list[str] = []
    for name, entry in backup.items():
        try:
            _write_env(name, entry.get("value"), entry.get("type"))
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
    base = _clean_base_url(base_url)
    return {
        "OpenAI SDK (Python)": (
            "from openai import OpenAI\n\n"
            f'client = OpenAI(base_url="{base}/v1", api_key="{token}")\n'
            f'print(client.chat.completions.create(model="{model}",\n'
            '      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)\n'
        ),
        "环境变量": (
            f'set OPENAI_BASE_URL={base}/v1\n'
            f"set OPENCODE_POOL_TOKEN={token}\n"
            "\n"
            "# PowerShell\n"
            f'$env:OPENAI_BASE_URL="{base}/v1"\n'
            f'$env:OPENCODE_POOL_TOKEN="{token}"\n'
        ),
        "Codex CLI": (
            "~/.codex/config.toml\n\n" + codex_snippet(base, model)
        ),
        "Claude Code": (
            "# /v1/messages 走 Anthropic 协议，中转直接支持\n"
            f'set ANTHROPIC_BASE_URL={base}\n'
            f"set ANTHROPIC_AUTH_TOKEN={token}\n"
        ),
        "curl 自检": (
            f'curl "{base}/v1/chat/completions" -H "Authorization: Bearer {token}" '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"model":"{model}","messages":[{{"role":"user","content":"hi"}}]}}\'\n'
        ),
    }


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
