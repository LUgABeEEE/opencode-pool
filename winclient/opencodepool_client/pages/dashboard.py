"""仪表盘：一眼看清"哪个号快死了、什么时候回血"。

这一页照抄了中转面板的信息层次（三档窗口 → 池子最坏点 → 每号明细），
但把它做成桌面客户端能一直开着的形态：数字每秒重算，倒计时自己走。
"""
from __future__ import annotations

import tkinter as tk

from .. import theme, util, widgets


class AccountCard(tk.Frame):
    """一张账号卡：左边身份+状态，中间三档环，右边余量条与用量。"""

    def __init__(self, master, app, windows: list[dict]):
        super().__init__(master, bg=theme.BORDER, bd=0, highlightthickness=0)
        self.app = app
        self.windows = windows
        self.inner = tk.Frame(self, bg=theme.SURFACE)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        body = tk.Frame(self.inner, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        head = tk.Frame(body, bg=theme.SURFACE)
        head.pack(fill="x")
        self.name = tk.Label(head, text="", bg=theme.SURFACE, fg=theme.TEXT,
                             font=(theme.FONT_FAMILY, 10, "bold"), anchor="w")
        self.name.pack(side="left")
        self.pill = widgets.Pill(head, "", theme.TEXT_DIM, theme.SURFACE_2)
        self.pill.pack(side="left", padx=(8, 0))
        self.fp = tk.Label(head, text="", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                           font=(theme.MONO_FAMILY, 7))
        self.fp.pack(side="right")

        rings = tk.Frame(body, bg=theme.SURFACE)
        rings.pack(fill="x", pady=(10, 2))
        self.rings: dict[str, widgets.Ring] = {}
        for spec in windows:
            cell = tk.Frame(rings, bg=theme.SURFACE)
            cell.pack(side="left", padx=(0, 10))
            ring = widgets.Ring(cell, size=74, width=7, label=spec.get("label") or spec.get("key", ""))
            ring.pack()
            self.rings[str(spec.get("key"))] = ring

        self.meter = widgets.Meter(body, width=260, height=6)
        self.meter.pack(fill="x", pady=(8, 0))
        foot = tk.Frame(body, bg=theme.SURFACE)
        foot.pack(fill="x", pady=(6, 0))
        self.headroom = tk.Label(foot, text="", bg=theme.SURFACE, fg=theme.TEXT,
                                 font=(theme.FONT_FAMILY, 9, "bold"), anchor="w")
        self.headroom.pack(side="left")
        self.note = tk.Label(foot, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                             font=(theme.FONT_FAMILY, 8), anchor="e")
        self.note.pack(side="right")

        stats = tk.Frame(body, bg=theme.SURFACE)
        stats.pack(fill="x", pady=(4, 0))
        self.stats = tk.Label(stats, text="", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                              font=(theme.FONT_FAMILY, 8), anchor="w")
        self.stats.pack(side="left")

        self.account_name = ""

    def set_data(self, account: dict, healthy: bool) -> None:
        self.account_name = str(account.get("name") or "")
        self.name.configure(text=self.account_name or "—")
        key_state, label, color = util.account_status(account, healthy)
        self.pill.set(label, color, theme.SURFACE_2)
        fingerprint = (account.get("key") or {}).get("fingerprint") or "无密钥"
        self.fp.configure(text=f"key {fingerprint}" if fingerprint != "无密钥" else fingerprint)

        percent = account.get("percent") or {}
        for key, ring in self.rings.items():
            ring.set(percent.get(key))
        headroom = account.get("headroom")
        self.meter.set(headroom)
        self.headroom.configure(
            text="余量 —" if headroom is None else f"余量 {headroom:.0f}",
            fg=theme.headroom_color(headroom))
        self.note.configure(text=util.status_note(account))

        parts = [f"请求 {account.get('requests', 0)}"]
        if account.get("errors"):
            parts.append(f"错误 {account['errors']}")
        ratio = account.get("cache_hit_ratio")
        if ratio is not None:
            parts.append(f"缓存命中 {ratio * 100:.0f}%")
        binding = account.get("binding_window")
        if binding:
            parts.append(f"绑定 {binding}")
        refreshed = account.get("last_refresh")
        if refreshed:
            parts.append(f"采样 {util.fmt_age(refreshed)}")
        if account.get("enabled") is False:
            parts.append("已暂停")
        self.stats.configure(text=" · ".join(parts))


class DashboardPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=theme.BG)
        self.app = app
        self.windows: list[dict] = []
        self._cards: dict[str, AccountCard] = {}
        self._order: list[str] = []

        top = tk.Frame(self, bg=theme.BG)
        top.pack(fill="x")
        self.stat_labels: dict[str, tk.Label] = {}
        for key, title in (("ready", "可用 / 总数"), ("recent", "近 5 分钟请求"),
                           ("avg", "池子平均余量"), ("cache", "缓存命中率")):
            card = tk.Frame(top, bg=theme.SURFACE)
            card.pack(side="left", fill="both", expand=True, padx=(0, 10))
            inner = tk.Frame(card, bg=theme.SURFACE)
            inner.pack(fill="both", expand=True, padx=14, pady=12)
            tk.Label(inner, text=title, bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                     font=(theme.FONT_FAMILY, 8), anchor="w").pack(fill="x")
            value = tk.Label(inner, text="—", bg=theme.SURFACE, fg=theme.TEXT,
                             font=(theme.FONT_FAMILY, 17, "bold"), anchor="w")
            value.pack(fill="x", pady=(2, 0))
            self.stat_labels[key] = value

        windows_box = widgets.Card(self, title="三档额度窗口（池内最坏点）")
        windows_box.pack(fill="x", pady=(12, 0))
        self.window_row = tk.Frame(windows_box.body, bg=theme.SURFACE)
        self.window_row.pack(fill="x")
        self._window_rings: dict[str, widgets.Ring] = {}
        self._window_caption: dict[str, tk.Label] = {}

        head = tk.Frame(self, bg=theme.BG)
        head.pack(fill="x", pady=(14, 6))
        tk.Label(head, text="账号", bg=theme.BG, fg=theme.TEXT,
                 font=(theme.FONT_FAMILY, 10, "bold")).pack(side="left")
        self.sort_pill = widgets.Pill(head, "按余量排序", theme.TEXT_MUTE, theme.BG)
        self.sort_pill.pack(side="left", padx=(8, 0))

        self.scroller = widgets.ScrollFrame(self, bg=theme.BG)
        self.scroller.pack(fill="both", expand=True)
        self.empty = tk.Label(self.scroller.body, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                              font=(theme.FONT_FAMILY, 9), justify="left", anchor="w")
        self.empty.pack(fill="x", pady=20, padx=4)

    # —— 页面回调 ——
    def on_show(self) -> None:
        self.app._page_subtitle("哪个号现在能用、还能用多久、什么时候回血")
        self.on_snapshot(self.app.snap)
        self.on_status(self.app.healthy, self.app.svc_text)

    def on_status(self, healthy: bool, text: str) -> None:
        if not healthy:
            self.sort_pill.set("中转不在线 · 数据为最后已知状态", theme.WARN)
        else:
            self.sort_pill.set("按余量排序", theme.TEXT_MUTE)

    def on_snapshot(self, snap: dict) -> None:
        snap = snap or {}
        self._update_stats(snap)
        self._update_windows(snap)
        self._update_cards(snap)

    # —— 各块 ——
    def _update_stats(self, snap: dict) -> None:
        accounts = snap.get("accounts") or []
        total = snap.get("total_accounts", len(accounts))
        ready = snap.get("healthy_accounts", 0)
        self.stat_labels["ready"].configure(
            text=f"{ready} / {total}",
            fg=theme.OK if ready and ready == total else (theme.WARN if ready else theme.BAD))
        self.stat_labels["recent"].configure(text=str(snap.get("requests_last_5m", 0)))
        headrooms = [a.get("headroom") for a in accounts if a.get("headroom") is not None]
        avg = sum(headrooms) / len(headrooms) if headrooms else None
        self.stat_labels["avg"].configure(
            text="—" if avg is None else f"{avg:.0f}",
            fg=theme.headroom_color(avg))
        ratios = [a.get("cache_hit_ratio") for a in accounts if a.get("cache_hit_ratio") is not None]
        cache = sum(ratios) / len(ratios) if ratios else None
        self.stat_labels["cache"].configure(
            text="—" if cache is None else f"{cache * 100:.0f}%",
            fg=theme.TEXT if cache is None else (theme.OK if cache >= 0.4 else theme.TEXT_DIM))

    def _window_specs(self) -> list[dict]:
        upstreams = (self.app.snap or {}).get("upstreams") or {}
        for _pid, info in upstreams.items():
            windows = info.get("windows") or []
            if windows:
                return windows
        return [
            {"key": "rolling", "label": "5 小时"},
            {"key": "weekly", "label": "周"},
            {"key": "monthly", "label": "月"},
        ]

    def _update_windows(self, snap: dict) -> None:
        specs = self._window_specs()
        worst = snap.get("pool_worst") or {}
        merged: dict[str, tuple[float, str]] = {}
        for _pid, windows in worst.items():
            for key, info in (windows or {}).items():
                pct = info.get("percent")
                if pct is None:
                    continue
                current = merged.get(key)
                if current is None or float(pct) > current[0]:
                    merged[key] = (float(pct), str(info.get("account") or ""))

        keys = [str(spec.get("key")) for spec in specs]
        if set(keys) != set(self._window_rings):
            for child in self.window_row.winfo_children():
                child.destroy()
            self._window_rings.clear()
            self._window_caption.clear()
            for spec in specs:
                cell = tk.Frame(self.window_row, bg=theme.SURFACE)
                cell.pack(side="left", padx=(0, 24))
                ring = widgets.Ring(cell, size=92, width=9,
                                    label=str(spec.get("label") or spec.get("key")))
                ring.pack()
                caption = tk.Label(cell, text="—", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                                   font=(theme.FONT_FAMILY, 8))
                caption.pack(pady=(2, 0))
                self._window_rings[str(spec.get("key"))] = ring
                self._window_caption[str(spec.get("key"))] = caption
        for key, ring in self._window_rings.items():
            entry = merged.get(key)
            ring.set(None if entry is None else entry[0])
            self._window_caption[key].configure(
                text="—" if entry is None or not entry[1] else f"最紧：{entry[1]}")

    def _sorted_accounts(self, snap: dict) -> list[dict]:
        accounts = list(snap.get("accounts") or [])

        def key(account: dict):
            if account.get("enabled") is False:
                return (3, 0.0, account.get("name") or "")
            headroom = account.get("headroom")
            ready = 0 if (headroom is not None and headroom > 0) else 1
            return (ready, -(headroom if headroom is not None else -1), account.get("name") or "")

        return sorted(accounts, key=key)

    def _update_cards(self, snap: dict) -> None:
        accounts = self._sorted_accounts(snap)
        specs = self._window_specs()
        seen = set()
        for account in accounts:
            name = str(account.get("name") or "")
            seen.add(name)
            card = self._cards.get(name)
            if card is None or [s.get("key") for s in card.windows] != [s.get("key") for s in specs]:
                if card is not None:
                    card.destroy()
                card = AccountCard(self.scroller.body, self.app, specs)
                self._cards[name] = card
            card.set_data(account, self.app.healthy)
        for name in list(self._cards):
            if name not in seen:
                self._cards[name].destroy()
                del self._cards[name]

        order = [str(a.get("name") or "") for a in accounts]
        if order != self._order:
            for name in order:
                self._cards[name].pack(fill="x", pady=(0, 10))
            self._order = order
        for name in order:
            self._cards[name].set_data(next(a for a in accounts if a.get("name") == name),
                                       self.app.healthy)

        if accounts:
            self.empty.configure(text="")
        else:
            if self.app.healthy:
                text = ("池子里还没有号。\n\n去「账号」页点「加号」，填上 OpenCode Go 的 API key；\n"
                        "加完这里会立刻出现三档额度环。")
            else:
                text = ("中转没在跑。点左下角「启动中转」，\n"
                        "或者去「设置」里检查项目目录与 Python 路径。")
            self.empty.configure(text=text)
