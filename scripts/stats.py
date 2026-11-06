#!/usr/bin/env python3
"""分析 `requests.jsonl`（结构化请求日志），零依赖、只用标准库。

用法：
    python3 scripts/stats.py [--file PATH] [--since 24h]

输出：总请求 / 成功率 / outcome 分布 / 按账号（请求、失败、平均与中位 TTFB）/
缓存命中率 / 粘性命中率 / 限流与换号次数 / p50·p95 延迟。

--since 支持 Ns / Nm / Nh / Nd（例如 90m、24h、7d）；缺省统计整个文件。
坏行（半截 JSON / 手工改坏）会被跳过并计数，不让脚本崩。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DEFAULT_PATH = "~/.dsh/opencode-pool/requests.jsonl"
SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(text: str | None) -> float | None:
    """'24h' → 86400 秒；None/空串 → 不过滤。"""
    if not text:
        return None
    unit = text[-1:]
    if unit not in SINCE_UNITS:
        raise SystemExit(f"--since 只支持 Ns/Nm/Nh/Nd，收到 {text!r}")
    try:
        amount = float(text[:-1])
    except ValueError:
        raise SystemExit(f"--since 只支持 Ns/Nm/Nh/Nd，收到 {text!r}")
    if amount < 0:
        raise SystemExit("--since 不能为负数")
    return amount * SINCE_UNITS[unit]


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def percentile(values: list[float], fraction: float) -> float | None:
    """线性插值分位数（与 numpy 默认口径一致，够用且零依赖）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def load_rows(path: str, since_seconds: float | None):
    cutoff = None
    if since_seconds is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    rows, bad, skipped_old = [], 0, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(row, dict):
                bad += 1
                continue
            if cutoff is not None:
                stamp = parse_ts(row.get("ts"))
                if stamp is not None and stamp < cutoff:
                    skipped_old += 1
                    continue
            rows.append(row)
    return rows, bad, skipped_old


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize(rows: list[dict]) -> dict:
    total = len(rows)

    def is_client_abort(row: dict) -> bool:
        return row.get("outcome") == "client_error" or row.get("error_reason") == "client_abort"

    client_errors = sum(1 for row in rows if is_client_abort(row))
    completed = total - client_errors          # 被取消的请求不算"完成"
    # 成功 = 完成里非取消且 2xx；取消既不算成功也不算完成（单列 client_error）
    ok = sum(1 for row in rows
             if not is_client_abort(row)
             and isinstance(row.get("status"), int) and row["status"] < 400)

    outcomes = Counter(row.get("outcome") or "unknown" for row in rows)

    accounts: dict[str, dict] = defaultdict(lambda: {"requests": 0, "failures": 0, "ttfb": []})
    for row in rows:
        name = row.get("account") or "-"
        bucket = accounts[name]
        bucket["requests"] += 1
        status = row.get("status")
        if is_client_abort(row) or (isinstance(status, int) and status >= 400):
            bucket["failures"] += 1
        ttfb = _num(row.get("ttfb_ms"))
        if ttfb is not None:
            bucket["ttfb"].append(ttfb)

    cache_read = cache_input = 0.0
    have_cache = False
    for row in rows:
        tokens = row.get("tokens") or {}
        cr, inp = _num(tokens.get("cache_read")), _num(tokens.get("input"))
        if cr is not None and inp is not None:
            cache_read += cr
            cache_input += inp
            have_cache = True
    cache_ratio = (cache_read / (cache_read + cache_input)) if (have_cache and cache_read + cache_input) else None

    sticky = Counter(row.get("sticky") for row in rows if row.get("sticky") in ("hit", "miss"))
    sticky_total = sticky["hit"] + sticky["miss"]
    sticky_ratio = (sticky["hit"] / sticky_total) if sticky_total else None

    throttled = sum(1 for row in rows
                    if row.get("outcome") == "throttled"
                    or "throttl" in str(row.get("error_reason") or ""))
    failovers = sum(int(row.get("failovers") or 0) for row in rows)

    totals = [value for value in (_num(row.get("total_ms")) for row in rows) if value is not None]

    return {
        "total": total,
        "ok": ok,
        "completed": completed,
        "client_errors": client_errors,
        "outcomes": outcomes,
        "accounts": accounts,
        "cache_read": cache_read,
        "cache_input": cache_input,
        "cache_ratio": cache_ratio,
        "sticky": sticky,
        "sticky_ratio": sticky_ratio,
        "throttled": throttled,
        "failovers": failovers,
        "p50": percentile(totals, 0.50),
        "p95": percentile(totals, 0.95),
    }


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def render(path: str, summary: dict, bad: int, skipped_old: int) -> str:
    lines = [f"requests.jsonl · {path}"]
    total = summary["total"]
    completed = summary["completed"]
    rate = (summary["ok"] / completed) if completed else None
    lines.append(f"总请求: {total}  成功率: {_fmt_pct(rate)} "
                 f"({summary['ok']}/{completed} 完成请求)")
    client_share = (summary["client_errors"] / total) if total else 0.0
    lines.append(f"client_error: {summary['client_errors']} ({client_share * 100:.1f}% of all)")
    if bad:
        lines.append(f"跳过坏行: {bad}")
    if skipped_old:
        lines.append(f"按 --since 跳过: {skipped_old}")

    outcomes = " ".join(f"{name}={count}" for name, count in summary["outcomes"].most_common())
    lines.append(f"outcome: {outcomes or '-'}")
    p50, p95 = summary["p50"], summary["p95"]
    p50_text = "n/a" if p50 is None else f"{p50:.1f}"
    p95_text = "n/a" if p95 is None else f"{p95:.1f}"
    lines.append(f"延迟(ms): p50={p50_text} p95={p95_text}")
    lines.append(f"缓存命中率: {_fmt_pct(summary['cache_ratio'])} "
                 f"(cache_read={summary['cache_read']:.0f}, input={summary['cache_input']:.0f})")
    lines.append(f"粘性命中率: {_fmt_pct(summary['sticky_ratio'])} "
                 f"(hit={summary['sticky']['hit']} miss={summary['sticky']['miss']})")
    lines.append(f"限流请求: {summary['throttled']}  换号次数: {summary['failovers']}")

    lines.append("按账号:")
    for name in sorted(summary["accounts"]):
        bucket = summary["accounts"][name]
        ttfb = bucket["ttfb"]
        avg = (sum(ttfb) / len(ttfb)) if ttfb else None
        median = percentile(ttfb, 0.5)
        lines.append(f"  {name:<10} 请求 {bucket['requests']:<5} 失败 {bucket['failures']:<5} "
                     f"平均TTFB {_fmt_ms(avg)}  中位 {_fmt_ms(median)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分析 opencode-pool 的 requests.jsonl")
    parser.add_argument("--file", default=os.path.expanduser(DEFAULT_PATH),
                        help=f"requests.jsonl 路径（默认 {DEFAULT_PATH}）")
    parser.add_argument("--since", default=None,
                        help="只看最近一段时间，例如 90m / 24h / 7d")
    args = parser.parse_args(argv)

    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        print(f"找不到 {path}（请求日志可能未开启：request_log_path）", file=sys.stderr)
        return 1
    since = parse_since(args.since)
    try:
        rows, bad, skipped_old = load_rows(path, since)
    except OSError as exc:
        print(f"读取 {path} 失败: {exc}", file=sys.stderr)
        return 1

    print(render(path, summarize(rows), bad, skipped_old))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
