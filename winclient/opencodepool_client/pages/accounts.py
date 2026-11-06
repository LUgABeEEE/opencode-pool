"""账号页：加号 / 换 key / 暂停 / 删除 / 清冷却，一张表看全。

一条重要设计：**服务没起来时也能加号**。
中转的 `validate()` 在 accounts 为空时直接报错退出，于是"先起服务再从接口加
第一个号"死锁；这时改用离线写盘（`localcfg`），写完提示启动服务。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .. import localcfg, theme, util

COLUMNS = (
    ("name", "账号", 130, "w"),
    ("provider", "上游", 90, "w"),
    ("status", "状态", 84, "w"),
    ("headroom", "余量", 60, "e"),
    ("rolling", "5h", 52, "e"),
    ("weekly", "周", 52, "e"),
    ("monthly", "月", 52, "e"),
    ("requests", "请求", 60, "e"),
    ("errors", "错误", 52, "e"),
    ("cache", "缓存", 60, "e"),
    ("age", "采样", 64, "e"),
    ("key", "密钥", 90, "w"),
)


class Dialog(tk.Toplevel):
    """极简模态对话框：字段由调用方给，校验函数返回错误文本。"""

    def __init__(self, parent, title: str, fields: list[dict], submit_text: str = "确定",
                 validate=None, note: str = ""):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=theme.SURFACE)
        self.resizable(False, False)
        self.transient(parent)
        self.result: dict | None = None
        self._fields = fields
        self._validate = validate
        self._vars: dict[str, tk.Variable] = {}

        body = tk.Frame(self, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        if note:
            tk.Label(body, text=note, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                     font=(theme.FONT_FAMILY, 8), justify="left", wraplength=380,
                     anchor="w").pack(fill="x", pady=(0, 10))
        for index, field in enumerate(fields):
            row = tk.Frame(body, bg=theme.SURFACE)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=field["label"], bg=theme.SURFACE, fg=theme.TEXT_DIM,
                     font=(theme.FONT_FAMILY, 8), width=12, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(field.get("value", "")))
            self._vars[field["key"]] = var
            if field.get("options"):
                widget = ttk.Combobox(row, textvariable=var, values=field["options"],
                                      state="readonly", width=34)
                widget.pack(side="left", fill="x", expand=True)
            else:
                widget = tk.Entry(row, textvariable=var, bg=theme.SURFACE_2, fg=theme.TEXT,
                                  bd=0, insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 9),
                                  show="•" if field.get("secret") else "")
                widget.pack(side="left", fill="x", expand=True, ipady=5)
            if field.get("help"):
                tk.Label(body, text=field["help"], bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                         font=(theme.FONT_FAMILY, 7), anchor="w").pack(fill="x", padx=(96, 0))
            if index == 0:
                widget.focus_set()

        self.error = tk.Label(body, text="", bg=theme.SURFACE, fg=theme.BAD,
                              font=(theme.FONT_FAMILY, 8), anchor="w", wraplength=380,
                              justify="left")
        self.error.pack(fill="x", pady=(6, 0))

        foot = tk.Frame(self, bg=theme.SURFACE)
        foot.pack(fill="x", padx=18, pady=(0, 16))
        self._submit = ttk.Button(foot, text=submit_text, style="Accent.TButton",
                                  command=self._on_submit)
        self._submit.pack(side="right")
        ttk.Button(foot, text="取消", style="Ghost.TButton",
                   command=self._on_cancel).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda _e: self._on_submit())
        self.bind("<Escape>", lambda _e: self._on_cancel())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._center(parent)

    def _center(self, parent) -> None:
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        except tk.TclError:
            pass

    def _on_submit(self) -> None:
        values = {key: var.get().strip() for key, var in self._vars.items()}
        if self._validate:
            problem = self._validate(values)
            if problem:
                self.error.configure(text=problem)
                return
        self.result = values
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()

    def show(self) -> dict | None:
        self.grab_set()
        self.wait_window()
        return self.result


class AccountsPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=theme.BG)
        self.app = app
        self._rows: dict[str, dict] = {}

        bar = tk.Frame(self, bg=theme.BG)
        bar.pack(fill="x")
        ttk.Button(bar, text="＋ 加号", style="Accent.TButton",
                   command=self.add_account).pack(side="left")
        ttk.Button(bar, text="换 key", style="Ghost.TButton",
                   command=self.rotate_key).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="暂停 / 恢复", style="Ghost.TButton",
                   command=self.toggle).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="删除", style="Danger.TButton",
                   command=self.remove).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10, pady=4)
        ttk.Button(bar, text="查额度", style="Ghost.TButton",
                   command=self.app.refresh_quota).pack(side="left")
        ttk.Button(bar, text="清冷却", style="Ghost.TButton",
                   command=self.clear_cooldowns).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="热重载配置", style="Ghost.TButton",
                   command=self.reload).pack(side="left", padx=(6, 0))
        self.hint = tk.Label(bar, text="", bg=theme.BG, fg=theme.TEXT_MUTE,
                             font=(theme.FONT_FAMILY, 8))
        self.hint.pack(side="right")

        wrap = tk.Frame(self, bg=theme.BORDER)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        inner = tk.Frame(wrap, bg=theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.tree = ttk.Treeview(inner, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="browse")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self.rotate_key())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_hint())
        self.tree.tag_configure("ready", foreground=theme.TEXT)
        self.tree.tag_configure("warn", foreground=theme.WARN)
        self.tree.tag_configure("bad", foreground=theme.BAD)
        self.tree.tag_configure("mute", foreground=theme.TEXT_MUTE)

        foot = tk.Frame(self, bg=theme.BG)
        foot.pack(fill="x", pady=(8, 0))
        self.summary = tk.Label(foot, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                                font=(theme.FONT_FAMILY, 8), anchor="w", justify="left")
        self.summary.pack(side="left")
        ttk.Button(foot, text="打开配置", style="Ghost.TButton",
                   command=self.open_config).pack(side="right")

    # —— 回调 ——
    def on_show(self) -> None:
        self.app._page_subtitle("余量、冷却、密钥指纹都在一屏；服务没起来时也能改配置")
        self.on_snapshot(self.app.snap)

    def on_snapshot(self, snap: dict) -> None:
        snap = snap or {}
        accounts = list(snap.get("accounts") or [])
        selected = self._selected_name()
        live_names = set()

        if not accounts:
            accounts = self._offline_rows()

        for account in accounts:
            name = str(account.get("name") or "")
            live_names.add(name)
            self._rows[name] = account
            key_state, label, color = util.account_status(account, self.app.healthy)
            percent = account.get("percent") or {}
            headroom = account.get("headroom")
            tag = "ready"
            if key_state in ("exhausted", "no_credentials"):
                tag = "bad"
            elif key_state in ("cooling", "below_reserve", "stale"):
                tag = "warn"
            elif key_state in ("disabled", "no_quota", "offline"):
                tag = "mute"
            values = (
                name,
                str(account.get("provider") or "-"),
                label,
                "—" if headroom is None else f"{headroom:.0f}",
                _pct(percent.get("rolling")),
                _pct(percent.get("weekly")),
                _pct(percent.get("monthly")),
                str(account.get("requests") or 0),
                str(account.get("errors") or 0),
                _ratio(account.get("cache_hit_ratio")),
                util.fmt_age(account.get("last_refresh")) if account.get("last_refresh") else "—",
                (account.get("key") or {}).get("fingerprint") or "缺",
            )
            if self.tree.exists(name):
                self.tree.item(name, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=name, values=values, tags=(tag,))

        for name in list(self._rows):
            if name not in live_names:
                if self.tree.exists(name):
                    self.tree.delete(name)
                del self._rows[name]

        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        self._update_hint()
        self._update_summary(snap)

    def _offline_rows(self) -> list[dict]:
        """服务没跑时，从配置文件里直接拼出表格（没有额度，只有身份/开关/密钥指纹）。"""
        rows = []
        try:
            raw = localcfg.load_config(self.app.settings.config_path())
        except localcfg.LocalConfigError:
            return rows
        secrets_file = localcfg.secrets_path(self.app.settings.config_path(), raw)
        for account in localcfg.accounts(raw):
            slot = str(account.get("key_env") or "")
            info = localcfg.secret_describe(secrets_file, slot) if slot else {}
            rows.append({
                "name": account.get("name"),
                "provider": account.get("provider"),
                "enabled": account.get("enabled", True),
                "key": {"configured": info.get("configured"), "fingerprint": info.get("fingerprint")},
                "percent": {},
                "headroom": None,
            })
        return rows

    def _update_summary(self, snap: dict) -> None:
        total = len(self._rows)
        raw = None
        try:
            raw = localcfg.load_config(self.app.settings.config_path())
        except localcfg.LocalConfigError:
            pass
        floor = (raw or {}).get("min_headroom", 0)
        sticky = (raw or {}).get("sticky_ttl_seconds", 0)
        parts = [f"{total} 个号", f"水位线 {floor}", f"粘性 {util.fmt_seconds(sticky)}"]
        if snap.get("sticky"):
            try:
                parts.append(f"已钉对话 {len(snap['sticky'])} 条")
            except TypeError:
                pass
        self.summary.configure(text=" · ".join(parts))

    def on_tick(self) -> None:
        # 冷却倒计时每秒重算：只重画状态列，不动整张表
        for name, account in self._rows.items():
            if not self.tree.exists(name):
                continue
            _key, label, _color = util.account_status(account, self.app.healthy)
            note = util.status_note(account)
            if note and note != "—":
                label = f"{label} · {note}"[:40]
            values = list(self.tree.item(name, "values"))
            if len(values) > 2 and values[2] != label:
                values[2] = label
                self.tree.item(name, values=values)

    # —— 操作 ——
    def _selected_name(self) -> str:
        selection = self.tree.selection()
        return str(selection[0]) if selection else ""

    def _update_hint(self) -> None:
        name = self._selected_name()
        account = self._rows.get(name) or {}
        if not name:
            self.hint.configure(text="选中一行可以换 key / 暂停 / 删除（双击也能换 key）")
            return
        self.hint.configure(
            text=f"已选 {name} · 上游 {account.get('provider') or '-'} · "
                 f"绑定窗口 {account.get('binding_window') or '-'}")

    def _providers(self) -> list[str]:
        try:
            raw = localcfg.load_config(self.app.settings.config_path())
            return localcfg.provider_ids(raw)
        except localcfg.LocalConfigError:
            return [localcfg.DEFAULT_PROVIDER]

    def add_account(self) -> None:
        providers = self._providers()

        def validate(values: dict) -> str:
            if not localcfg.NAME_RE.match(values.get("name", "")):
                return "账号名只能由字母、数字、_ . - 组成（1–40 字）"
            if values.get("name") in self._rows:
                return f"账号 {values['name']} 已存在"
            if not values.get("key"):
                return "密钥不能为空"
            return ""

        dialog = Dialog(
            self, "加号",
            fields=[
                {"key": "name", "label": "账号名", "value": "", "help": "字母/数字/_.-，例如 go-01"},
                {"key": "provider", "label": "上游", "value": providers[0], "options": providers},
                {"key": "key", "label": "API key", "value": "", "secret": True,
                 "help": "OpenCode Go 订阅的 key；只落在本机 secrets.json（0600），面板/客户端都不再回显"},
                {"key": "session_prefix", "label": "会话前缀", "value": "",
                 "help": "留空自动用 dsh-pool-<账号名>；它决定上游 prompt cache 的命名空间"},
            ],
            submit_text="加进池子",
            validate=validate,
            note="账号加完会立刻采一次额度；之后按 usage_refresh_seconds 自动刷新。",
        )
        result = dialog.show()
        if not result:
            return
        self._submit_account(result)

    def _submit_account(self, values: dict) -> None:
        name = values["name"]
        provider = values["provider"]
        key = values["key"]
        prefix = values.get("session_prefix", "")

        if self.app.healthy:
            def work():
                self.app.client.add_account(name, provider, key, prefix)
                return self.app.client.snapshot()

            self.app.run_async("add-account", work, lambda result, error: self._after_add(result, error, name))
        else:
            def work():
                localcfg.add_account(self.app.settings.config_path(), name, provider, key, prefix)
                return None

            self.app.run_async("add-account", work,
                               lambda result, error: self._after_add_offline(result, error, name))

    def _after_add(self, result, error, name: str) -> None:
        if error:
            self.app.toast(f"加号失败：{error}", "bad")
            return
        self.app.snap = result or self.app.snap
        self.app.toast(f"已加入 {name}，正在采额度", "ok")
        self.app.refresh_now()

    def _after_add_offline(self, result, error, name: str) -> None:
        if error:
            self.app.toast(f"加号失败：{error}", "bad")
            return
        self.app.toast(f"已写入配置：{name} — 现在启动中转就会生效", "ok")
        self.on_snapshot(self.app.snap)
        if self.app.svc_state in ("stopped", "error"):
            if self.app.settings["auto_start_service"]:
                self.app.start_service()

    def rotate_key(self) -> None:
        name = self._selected_name()
        if not name:
            self.app.toast("先选一个账号", "warn")
            return
        dialog = Dialog(
            self, f"换 key · {name}",
            fields=[{"key": "key", "label": "新 API key", "value": "", "secret": True}],
            submit_text="替换",
            validate=lambda v: "" if v.get("key") else "密钥不能为空",
            note="替换后这个号的冷却与错误计数会被清零，并立刻重采一次额度。",
        )
        result = dialog.show()
        if not result:
            return
        key = result["key"]
        if self.app.healthy:
            def work():
                self.app.client.rotate_key(name, key)
                return self.app.client.snapshot()

            self.app.run_async("rotate", work, lambda r, e: self._after_simple(r, e, f"{name} 的 key 已替换"))
        else:
            def work():
                localcfg.rotate_key(self.app.settings.config_path(), name, key)

            self.app.run_async("rotate", work,
                               lambda r, e: self._after_simple(r, e, f"{name} 的 key 已写入配置（重启中转后生效）"))

    def toggle(self) -> None:
        name = self._selected_name()
        if not name:
            self.app.toast("先选一个账号", "warn")
            return
        account = self._rows.get(name) or {}
        enabled = account.get("enabled") is not False
        if self.app.healthy:
            def work():
                self.app.client.toggle_account(name, not enabled)
                return self.app.client.snapshot()

            self.app.run_async("toggle", work,
                               lambda r, e: self._after_simple(r, e, f"{name} 已{'暂停' if enabled else '恢复'}"))
        else:
            def work():
                localcfg.set_enabled(self.app.settings.config_path(), name, not enabled)

            self.app.run_async("toggle", work,
                               lambda r, e: self._after_simple(r, e, f"{name} 已写入配置（重启中转后生效）"))

    def remove(self) -> None:
        name = self._selected_name()
        if not name:
            self.app.toast("先选一个账号", "warn")
            return
        if not self.app.confirm("删除账号", f"从池子里删掉 {name}？\n它的密钥（如果没有别的号在用同一槽位）也会一起删掉。"):
            return
        if self.app.healthy:
            def work():
                self.app.client.remove_account(name)
                return self.app.client.snapshot()

            self.app.run_async("remove", work, lambda r, e: self._after_remove(r, e, name))
        else:
            def work():
                localcfg.remove_account(self.app.settings.config_path(), name)
                return None

            self.app.run_async("remove", work, lambda r, e: self._after_remove(r, e, name))

    def _after_remove(self, result, error, name: str) -> None:
        if error:
            self.app.toast(f"删除失败：{error}", "bad")
            return
        if isinstance(result, dict) and result.get("accounts") is not None:
            self.app.snap = result
        self.app.toast(f"已删除 {name}", "ok")
        self.app.refresh_now()
        if not self.app.healthy:
            self.on_snapshot(self.app.snap)

    def _after_simple(self, result, error, message: str) -> None:
        if error:
            self.app.toast(f"操作失败：{error}", "bad")
            return
        if isinstance(result, dict) and result.get("accounts") is not None:
            self.app.snap = result
        self.app.toast(message, "ok")
        self.app.refresh_now()
        if not self.app.healthy:
            self.on_snapshot(self.app.snap)

    def clear_cooldowns(self) -> None:
        if not self.app.healthy:
            self.app.toast("中转没在跑", "warn")
            return

        def work():
            result = self.app.client.clear_cooldowns()
            return {"cleared": result.get("cleared"), "snap": self.app.client.snapshot()}

        self.app.run_async("clear-cool", work, self._after_clear)

    def _after_clear(self, result, error) -> None:
        if error:
            self.app.toast(f"清冷却失败：{error}", "bad")
            return
        self.app.snap = (result or {}).get("snap") or self.app.snap
        self.app.toast(f"已清掉 {(result or {}).get('cleared', 0)} 个冷却", "ok")
        self.app._broadcast()

    def reload(self) -> None:
        if not self.app.healthy:
            self.app.toast("中转没在跑；改配置后直接启动就行", "warn")
            return

        def work():
            return self.app.client.reload()

        self.app.run_async("reload", work, self._after_reload)

    def _after_reload(self, result, error) -> None:
        if error:
            self.app.toast(f"热重载失败：{error}", "bad")
            return
        names = (result or {}).get("accounts") or []
        self.app.toast(f"已重载配置：{len(names)} 个号", "ok")
        self.app.refresh_now()

    def open_config(self) -> None:
        path = self.app.settings.config_path()
        problem = util.open_path(path)
        if problem:
            messagebox.showinfo("打开配置", problem, parent=self)


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "—"


def _ratio(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"
