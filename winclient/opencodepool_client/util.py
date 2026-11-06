"""小工具：时间/字节格式化、打开文件与文件夹、状态推导。

状态推导（`account_status`）是账号页和仪表盘共用的判断，放在一处，
免得两个页面对"什么叫冷却中"各有一套理解。
"""
from __future__ import annotations

import os
import subprocess
import time


def fmt_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def fmt_countdown(target: float | None, now: float | None = None) -> str:
    """冷却到什么时候 → 还能读的倒计时（`2小时13分`）。"""
    if not target:
        return ""
    now = now or time.time()
    left = target - now
    if left <= 0:
        return "已到"
    if left < 60:
        return f"{left:.0f}s"
    if left < 3600:
        return f"{left // 60:.0f}m"
    if left < 86400:
        hours = int(left // 3600)
        minutes = int((left % 3600) // 60)
        return f"{hours}h{minutes:02d}m"
    days = int(left // 86400)
    hours = int((left % 86400) // 3600)
    return f"{days}d{hours}h"


def fmt_epoch(value: float | None) -> str:
    if not value:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(value))


def fmt_epoch_time() -> str:
    """只要时分秒，给诊断/日志行打时间戳用。"""
    return time.strftime("%H:%M:%S")


def fmt_age(value: float | None, now: float | None = None) -> str:
    if not value:
        return "从未"
    now = now or time.time()
    delta = max(0.0, now - value)
    if delta < 60:
        return f"{delta:.0f}s 前"
    if delta < 3600:
        return f"{delta / 60:.0f}m 前"
    return f"{delta / 3600:.1f}h 前"


def fmt_bytes(value: float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def fmt_number(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    if digits:
        return f"{value:,.{digits}f}"
    return f"{value:,.0f}"


def open_path(path: str) -> str:
    """用系统默认手段打开文件/文件夹；返回空串表示成功，否则错误描述。"""
    if not path:
        return "路径为空"
    if not os.path.exists(path):
        return f"不存在：{path}"
    try:
        os.startfile(path)  # type: ignore[attr-defined]
        return ""
    except (OSError, AttributeError) as exc:
        try:
            subprocess.Popen(["explorer", "/select,", path])
            return ""
        except OSError:
            return f"{type(exc).__name__}: {exc}"


def account_status(account: dict, healthy: bool) -> tuple[str, str, str]:
    """把一条 snapshot 记录归纳成 (key, 中文, 颜色)。

    判据顺序与中转 `readiness()` 一致：暂停 → 缺密钥 → 冷却 → 打满 → 低于水位
    → 采样过期，最后才是可用。页面直接用这个 key 上色，不各自 if-else。
    """
    from . import theme

    if not healthy:
        return ("offline",) + theme.STATUS_STYLE["offline"]
    key_info = account.get("key") or {}
    if account.get("enabled") is False:
        return ("disabled",) + theme.STATUS_STYLE["disabled"]
    if not key_info.get("configured"):
        return ("no_credentials",) + theme.STATUS_STYLE["no_credentials"]
    now = time.time()
    cooldown_until = account.get("cooldown_until")
    if cooldown_until and float(cooldown_until) > now:
        return ("cooling",) + theme.STATUS_STYLE["cooling"]
    if account.get("blocked_window"):
        return ("exhausted",) + theme.STATUS_STYLE["exhausted"]
    if account.get("below_reserve"):
        return ("below_reserve",) + theme.STATUS_STYLE["below_reserve"]
    if account.get("refresh_error"):
        return ("stale",) + theme.STATUS_STYLE["stale"]
    return ("ready",) + theme.STATUS_STYLE["ready"]


def status_note(account: dict) -> str:
    """账号右侧那行小字：优先说"为什么不能接单"，否则说最紧的窗口。"""
    now = time.time()
    cooldown_until = account.get("cooldown_until")
    if cooldown_until and float(cooldown_until) > now:
        reason = account.get("cooldown_reason") or "冷却"
        return f"{reason} · 剩 {fmt_countdown(cooldown_until, now)}"
    blocked = account.get("blocked_window")
    if blocked:
        windows = account.get("windows") or {}
        resets = (windows.get(blocked) or {}).get("resets_at")
        tail = f" · {fmt_countdown(resets, now)} 后回血" if resets else ""
        return f"{blocked} 窗口已打满{tail}"
    if account.get("refresh_error"):
        return str(account["refresh_error"])[:80]
    binding = account.get("binding_window")
    if binding:
        pct = (account.get("percent") or {}).get(binding)
        if pct is not None:
            return f"最紧：{binding} {pct:.0f}%"
    return "—"
