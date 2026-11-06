"""读中转留下的两份文本：`relay.log`（人看的散文）和 `requests.jsonl`（结构化事件）。

都按"尾部读"实现：请求日志单个文件最大 20MB，把整份读进内存只为显示最后 200 行
太浪费，而且客户端每 3 秒刷一次。所以只 seek 到文件尾部若干 KB 再按行切。
"""
from __future__ import annotations

import json
import os
from collections import Counter

TAIL_BYTES = 512 * 1024


def tail_text(path: str, lines: int = 200, max_bytes: int = TAIL_BYTES) -> list[str]:
    """返回文件最后 `lines` 行；文件不存在返回空列表。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= 0:
        return []
    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", "replace")
    rows = text.splitlines()
    if start > 0 and rows:
        rows = rows[1:]          # 第一行多半被截断了
    return rows[-lines:]


def read_records(path: str, limit: int = 200, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """从尾部反向收集最多 `limit` 条合法 JSON 记录（新的在前）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= 0:
        return []
    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []
    rows = chunk.decode("utf-8", "replace").splitlines()
    if start > 0 and rows:
        rows = rows[1:]
    records: list[dict] = []
    for line in reversed(rows):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
        if len(records) >= limit:
            break
    return records


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def summarize(records: list[dict]) -> dict:
    """从一批请求记录里算概览指标（新→旧无所谓，统计与顺序无关）。"""
    total = len(records)
    if not total:
        return {"total": 0}
    statuses = Counter()
    outcomes = Counter()
    accounts = Counter()
    models = Counter()
    total_ms: list[float] = []
    ttfb_ms: list[float] = []
    cache_read = 0.0
    input_tokens = 0.0
    ok = 0
    failovers = 0
    for item in records:
        status = item.get("status")
        if isinstance(status, int):
            statuses[status] += 1
            if 200 <= status < 300:
                ok += 1
        outcome = item.get("outcome") or "-"
        outcomes[str(outcome)] += 1
        if item.get("account"):
            accounts[str(item["account"])] += 1
        if item.get("model"):
            models[str(item["model"])] += 1
        for key, bucket in (("total_ms", total_ms), ("ttfb_ms", ttfb_ms)):
            value = _number(item.get(key))
            if value is not None:
                bucket.append(float(value))
        tokens = item.get("tokens") or {}
        if isinstance(tokens, dict):
            cache_read += _number(tokens.get("cache_read")) or 0.0
            input_tokens += _number(tokens.get("input")) or 0.0
        failovers += int(_number(item.get("failovers")) or 0)
    cache_ratio = (cache_read / (cache_read + input_tokens)) if (cache_read + input_tokens) else None
    return {
        "total": total,
        "ok": ok,
        "ok_ratio": ok / total,
        "statuses": dict(statuses),
        "outcomes": dict(outcomes),
        "accounts": dict(accounts),
        "models": dict(models),
        "p50_ms": _percentile(total_ms, 0.5),
        "p95_ms": _percentile(total_ms, 0.95),
        "ttfb_p50_ms": _percentile(ttfb_ms, 0.5),
        "cache_ratio": cache_ratio,
        "failovers": failovers,
    }


def record_row(item: dict) -> tuple:
    """把一条记录摊成表格一行的 8 个格子（时间/模型/账号/协议/状态/耗时/缓存/结果）。"""
    ts = str(item.get("ts") or "")
    clock = ts[11:19] if len(ts) >= 19 else ts
    model = str(item.get("model") or "-")
    account = str(item.get("account") or "-")
    path = str(item.get("path") or "")
    proto = {"chat/completions": "openai", "messages": "anthropic", "responses": "responses"}.get(
        path.rsplit("/", 1)[-1], path.rsplit("/", 1)[-1] or "-")
    status = item.get("status")
    total = _number(item.get("total_ms"))
    tokens = item.get("tokens") or {}
    cache = _number(tokens.get("cache_read")) if isinstance(tokens, dict) else None
    outcome = str(item.get("outcome") or "-")
    return (
        clock,
        model,
        account,
        proto,
        "-" if status is None else str(status),
        "-" if total is None else f"{total:.0f}ms",
        "-" if not cache else f"{cache / 1000:.1f}k",
        outcome,
    )
