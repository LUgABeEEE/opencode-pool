"""请求页：结构化请求流（requests.jsonl）+ 中转控制台日志（relay.log）。

两类日志刻意分开：
  * `requests.jsonl` 是给机器看的（一行一个事件、21 个字段），这里做筛选/聚合；
  * `relay.log` 是给人看的散文（启动横幅、退出原因、异常栈），原样滚动显示。
两栏都在，排障时不用切窗口。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
from tkinter import ttk

from .. import localcfg, logtail, paths, theme, util

OUTCOME_LABELS = {
    "all": "全部结果",
    "ok": "ok 成功",
    "failover": "failover 换号",
    "throttled": "throttled 限流",
    "client_error": "client_error 客户端错",
    "passthrough": "passthrough 透传",
    "no_account": "no_account 无可用号",
}

COLUMNS = (
    ("ts", "时间", 76, "w"),
    ("model", "模型", 168, "w"),
    ("account", "账号", 104, "w"),
    ("proto", "协议", 78, "w"),
    ("status", "状态", 56, "e"),
    ("total", "总耗时", 68, "e"),
    ("cache", "缓存读", 68, "e"),
    ("outcome", "结果", 90, "w"),
)


class RequestsPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=theme.BG)
        self.app = app
        self._records: list[dict] = []
        self._row_map: dict[str, dict] = {}
        self._last_load = 0.0
        self._log_loaded = False

        stats = tk.Frame(self, bg=theme.BG)
        stats.pack(fill="x")
        self.stat_labels: dict[str, tk.Label] = {}
        for key, title in (("total", "窗口内请求"), ("ok", "成功率"),
                           ("p50", "P50 耗时"), ("p95", "P95 耗时"),
                           ("cache", "缓存命中"), ("failover", "换号次数")):
            card = tk.Frame(stats, bg=theme.SURFACE)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            inner = tk.Frame(card, bg=theme.SURFACE)
            inner.pack(fill="both", expand=True, padx=12, pady=9)
            tk.Label(inner, text=title, bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                     font=(theme.FONT_FAMILY, 8), anchor="w").pack(fill="x")
            value = tk.Label(inner, text="—", bg=theme.SURFACE, fg=theme.TEXT,
                             font=(theme.FONT_FAMILY, 13, "bold"), anchor="w")
            value.pack(fill="x")
            self.stat_labels[key] = value

        bar = tk.Frame(self, bg=theme.BG)
        bar.pack(fill="x", pady=(10, 8))
        tk.Label(bar, text="账号", bg=theme.BG, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 8)).pack(side="left")
        self.account_filter = tk.StringVar(value="全部账号")
        self.account_box = ttk.Combobox(bar, textvariable=self.account_filter, width=16,
                                        state="readonly", values=["全部账号"])
        self.account_box.pack(side="left", padx=(4, 12))
        self.account_box.bind("<<ComboboxSelected>>", lambda _e: self._render())
        tk.Label(bar, text="结果", bg=theme.BG, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 8)).pack(side="left")
        self.outcome_filter = tk.StringVar(value=OUTCOME_LABELS["all"])
        self.outcome_box = ttk.Combobox(bar, textvariable=self.outcome_filter, width=18,
                                        state="readonly", values=list(OUTCOME_LABELS.values()))
        self.outcome_box.pack(side="left", padx=(4, 12))
        self.outcome_box.bind("<<ComboboxSelected>>", lambda _e: self._render())
        self.only_bad = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只看非 2xx", variable=self.only_bad,
                        command=self._render).pack(side="left")
        ttk.Button(bar, text="刷新", style="Ghost.TButton",
                   command=lambda: self._load(force=True)).pack(side="left", padx=(12, 0))
        self.path_label = tk.Label(bar, text="", bg=theme.BG, fg=theme.TEXT_MUTE,
                                   font=(theme.FONT_FAMILY, 7))
        self.path_label.pack(side="right")

        panes = ttk.Notebook(self)
        panes.pack(fill="both", expand=True)
        self.panes = panes

        # ── 请求流 ──────────────────────────────────────
        stream = tk.Frame(panes, bg=theme.SURFACE)
        panes.add(stream, text="请求流")
        table_wrap = tk.Frame(stream, bg=theme.SURFACE)
        table_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_wrap, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "model"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        self.tree.tag_configure("bad", foreground=theme.BAD)
        self.tree.tag_configure("warn", foreground=theme.WARN)
        self.tree.tag_configure("ok", foreground=theme.TEXT)

        detail_wrap = tk.Frame(stream, bg=theme.SURFACE_2)
        detail_wrap.pack(fill="x")
        self.detail = tk.Label(detail_wrap, text="选中一行看完整字段", bg=theme.SURFACE_2,
                               fg=theme.TEXT_DIM, font=(theme.MONO_FAMILY, 8),
                               anchor="w", justify="left", wraplength=1000)
        self.detail.pack(fill="x", padx=10, pady=6)

        # ── 控制台日志 ──────────────────────────────────
        log_frame = tk.Frame(panes, bg=theme.SURFACE)
        panes.add(log_frame, text="控制台日志")
        log_bar = tk.Frame(log_frame, bg=theme.SURFACE)
        log_bar.pack(fill="x")
        ttk.Button(log_bar, text="重读 relay.log", style="Ghost.TButton",
                   command=self._reload_log).pack(side="left", padx=6, pady=6)
        ttk.Button(log_bar, text="清屏", style="Ghost.TButton",
                   command=self._clear_log).pack(side="left")
        ttk.Button(log_bar, text="打开日志目录", style="Ghost.TButton",
                   command=self._open_log_dir).pack(side="left", padx=6)
        self.log_text = tk.Text(log_frame, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                                insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 8),
                                wrap="none", height=18, padx=8, pady=6)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")

    # ── 路径 ────────────────────────────────────────────
    def _request_log_path(self) -> str:
        config_path = self.app.settings.config_path()
        try:
            raw = localcfg.load_config(config_path)
        except localcfg.LocalConfigError:
            raw = {}
        value = str(raw.get("request_log_path") or "")
        if not value:
            return os.path.join(paths.config_dir(config_path), "requests.jsonl")
        try:
            return paths.resolve_relative(config_path, value, "requests.jsonl")
        except OSError:
            return value

    def _relay_log_path(self) -> str:
        return self.app.settings.relay_log()

    # ── 生命周期 ────────────────────────────────────────
    def on_show(self) -> None:
        self.app._page_subtitle("结构化请求流 + 控制台日志；Requests 里没有 prompt，只有元数据")
        self._load(force=True)
        if not self._log_loaded:
            self._reload_log()

    def on_snapshot(self, _snap: dict) -> None:
        self._sync_account_filter()

    def on_tick(self) -> None:
        if time.time() - self._last_load >= 2.0:
            self._load()

    def append_log(self, line: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            # 只留最近 1500 行，长时间挂着也不吃内存
            if int(self.log_text.index("end-1c").split(".")[0]) > 1500:
                self.log_text.delete("1.0", "200.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _reload_log(self) -> None:
        path = self._relay_log_path()
        lines = logtail.tail_text(path, lines=600)
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            if lines:
                self.log_text.insert("1.0", "\n".join(lines) + "\n")
            else:
                self.log_text.insert("1.0", f"（{path} 还是空的：启动中转后这里会有输出）\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        self._log_loaded = True

    def _clear_log(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _open_log_dir(self) -> None:
        problem = util.open_path(os.path.dirname(self._relay_log_path()))
        if problem:
            self.app.toast(problem, "warn")

    # ── 读与渲染 ────────────────────────────────────────
    def _load(self, force: bool = False) -> None:
        if not force and time.time() - self._last_load < 1.5:
            return
        self._last_load = time.time()
        path = self._request_log_path()
        records = logtail.read_records(path, limit=self.app.settings.request_rows())
        self._records = records
        self.path_label.configure(text=path)
        self._update_stats(records)
        self._sync_account_filter()
        self._render()

    def _update_stats(self, records: list[dict]) -> None:
        summary = logtail.summarize(records)
        total = summary.get("total", 0)
        self.stat_labels["total"].configure(text=str(total))
        ratio = summary.get("ok_ratio")
        self.stat_labels["ok"].configure(
            text="—" if ratio is None else f"{ratio * 100:.0f}%",
            fg=theme.TEXT if ratio is None else (theme.OK if ratio >= 0.95 else
                                                 (theme.WARN if ratio >= 0.8 else theme.BAD)))
        p50 = summary.get("p50_ms")
        p95 = summary.get("p95_ms")
        self.stat_labels["p50"].configure(text="—" if p50 is None else f"{p50:.0f}ms")
        self.stat_labels["p95"].configure(text="—" if p95 is None else f"{p95:.0f}ms")
        cache = summary.get("cache_ratio")
        self.stat_labels["cache"].configure(
            text="—" if cache is None else f"{cache * 100:.0f}%",
            fg=theme.TEXT if cache is None else (theme.OK if cache >= 0.4 else theme.TEXT_DIM))
        self.stat_labels["failover"].configure(text=str(summary.get("failovers", 0)))

    def _sync_account_filter(self) -> None:
        names = sorted({str(r.get("account")) for r in self._records if r.get("account")})
        values = ["全部账号"] + names
        if list(self.account_box["values"]) != values:
            self.account_box.configure(values=values)
            if self.account_filter.get() not in values:
                self.account_filter.set("全部账号")

    def _filtered(self) -> list[dict]:
        records = self._records
        account = self.account_filter.get()
        if account and account != "全部账号":
            records = [r for r in records if str(r.get("account")) == account]
        outcome = self.outcome_filter.get()
        if outcome and outcome != OUTCOME_LABELS["all"]:
            key = next((k for k, v in OUTCOME_LABELS.items() if v == outcome), "")
            if key:
                records = [r for r in records if str(r.get("outcome")) == key]
        if self.only_bad.get():
            def bad(record: dict) -> bool:
                status = record.get("status")
                return not (isinstance(status, int) and 200 <= status < 300)
            records = [r for r in records if bad(r)]
        return records

    def _render(self) -> None:
        selected = self.tree.selection()
        keep = selected[0] if selected else ""
        self.tree.delete(*self.tree.get_children())
        self._row_map.clear()
        for index, record in enumerate(self._filtered()):
            row = logtail.record_row(record)
            iid = f"r{index}"
            status = record.get("status")
            tag = "ok"
            if not (isinstance(status, int) and 200 <= status < 300):
                tag = "bad"
            elif record.get("outcome") in ("failover", "throttled"):
                tag = "warn"
            self.tree.insert("", "end", iid=iid, values=row, tags=(tag,))
            self._row_map[iid] = record
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
        self._show_detail()

    def _show_detail(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.detail.configure(text="选中一行看完整字段", fg=theme.TEXT_DIM)
            return
        record = self._row_map.get(selection[0]) or {}
        parts = []
        for key in ("ts", "req_id", "model", "account", "provider", "status", "upstream_status",
                    "outcome", "queue_ms", "ttfb_ms", "total_ms", "retries", "failovers",
                    "sticky", "error_reason"):
            value = record.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        tokens = record.get("tokens") or {}
        if isinstance(tokens, dict):
            interesting = {k: v for k, v in tokens.items() if v}
            if interesting:
                parts.append("tokens=" + json.dumps(interesting, ensure_ascii=False))
        self.detail.configure(text="  ".join(parts) or "（这条记录是空的）", fg=theme.TEXT)
