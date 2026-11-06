"""接驳页：把中转接到别的客户端，并且能一键还原。

分三块：本机入口（base_url / token）、五种客户端片段、一键写入（Codex 配置 /
用户环境变量）。第三块是这个客户端相对 Web 面板唯一的硬增量，所以带
"预览 → 写入 → 还原"三步，写之前一定先存还原点。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import clientcfg, theme, widgets


class ConnectPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=theme.BG)
        self.app = app
        self._snippet_tabs: dict[str, tk.Text] = {}
        self._env_vars: dict[str, tk.BooleanVar] = {}
        self._model = tk.StringVar(value="deepseek-v4.1-flash")

        scroller = widgets.ScrollFrame(self, bg=theme.BG)
        scroller.pack(fill="both", expand=True)
        body = scroller.body

        # ── 本机入口 ─────────────────────────────────────
        entry_card = widgets.Card(body, title="本机入口")
        entry_card.pack(fill="x", pady=(0, 12))
        self.base_field = widgets.Field(entry_card.body, "Base URL（客户端填这个）",
                                        self._base_url(), on_copy=lambda _v: self.app.toast("已复制 Base URL", "ok"))
        self.base_field.pack(fill="x")
        self.token_field = widgets.Field(entry_card.body, "API Key（= 中转的 relay_token）",
                                         self._token(), secret=True,
                                         on_copy=lambda _v: self.app.toast("已复制 token", "ok"))
        self.token_field.pack(fill="x", pady=(10, 0))

        actions = tk.Frame(entry_card.body, bg=theme.SURFACE)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="复制全部", style="Ghost.TButton",
                   command=self.copy_all).pack(side="left")
        ttk.Button(actions, text="打开控制台", style="Ghost.TButton",
                   command=self.app.open_panel).pack(side="left", padx=(6, 0))
        tk.Label(actions, text="模型", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 8)).pack(side="left", padx=(16, 6))
        tk.Entry(actions, textvariable=self._model, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                 insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 8), width=26
                 ).pack(side="left", ipady=4)
        ttk.Button(actions, text="真发一次 ping", style="Accent.TButton",
                   command=self.ping).pack(side="left", padx=(8, 0))
        self.ping_result = tk.Label(entry_card.body, text="", bg=theme.SURFACE,
                                    fg=theme.TEXT_DIM, font=(theme.FONT_FAMILY, 8),
                                    anchor="w", justify="left", wraplength=760)
        self.ping_result.pack(fill="x", pady=(8, 0))

        # ── 片段 ────────────────────────────────────────
        snippet_card = widgets.Card(body, title="接入片段（只读，点复制拿去用）")
        snippet_card.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(snippet_card.body)
        self.notebook.pack(fill="both", expand=True)
        self._build_snippets()
        copy_row = tk.Frame(snippet_card.body, bg=theme.SURFACE)
        copy_row.pack(fill="x", pady=(8, 0))
        ttk.Button(copy_row, text="复制当前片段", style="Ghost.TButton",
                   command=self.copy_current).pack(side="left")
        tk.Label(copy_row, text="token 会出现在片段里——别把这段贴到公开的地方",
                 bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 7)).pack(side="left", padx=(10, 0))

        # ── 一键写入 ────────────────────────────────────
        write_card = widgets.Card(body, title="一键写入（先预览，写前自动留还原点）")
        write_card.pack(fill="x", pady=(12, 0))

        codex_box = tk.Frame(write_card.body, bg=theme.SURFACE)
        codex_box.pack(fill="x")
        tk.Label(codex_box, text="Codex CLI · ~/.codex/config.toml", bg=theme.SURFACE,
                 fg=theme.TEXT, font=(theme.FONT_FAMILY, 9, "bold"), anchor="w").pack(fill="x")
        self.codex_state = tk.Label(codex_box, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                                    font=(theme.FONT_FAMILY, 8), anchor="w", justify="left")
        self.codex_state.pack(fill="x", pady=(2, 6))
        codex_actions = tk.Frame(codex_box, bg=theme.SURFACE)
        codex_actions.pack(fill="x")
        ttk.Button(codex_actions, text="预览", style="Ghost.TButton",
                   command=self.preview_codex).pack(side="left")
        ttk.Button(codex_actions, text="写入", style="Accent.TButton",
                   command=self.apply_codex).pack(side="left", padx=(6, 0))
        ttk.Button(codex_actions, text="还原", style="Ghost.TButton",
                   command=self.restore_codex).pack(side="left", padx=(6, 0))

        ttk.Separator(write_card.body, orient="horizontal").pack(fill="x", pady=12)

        env_box = tk.Frame(write_card.body, bg=theme.SURFACE)
        env_box.pack(fill="x")
        tk.Label(env_box, text="用户环境变量（当前用户，不需要管理员）", bg=theme.SURFACE,
                 fg=theme.TEXT, font=(theme.FONT_FAMILY, 9, "bold"), anchor="w").pack(fill="x")
        tk.Label(env_box, text="勾选才会写；OPENAI_API_KEY 会顶掉你原有的 OpenAI key。",
                 bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 7), anchor="w").pack(fill="x", pady=(0, 6))
        for name, hint in clientcfg.ENV_TARGETS.items():
            row = tk.Frame(env_box, bg=theme.SURFACE)
            row.pack(fill="x", pady=1)
            var = tk.BooleanVar(value=name in clientcfg.DEFAULT_ENV_TARGETS)
            self._env_vars[name] = var
            ttk.Checkbutton(row, text=name, variable=var,
                            style="TCheckbutton").pack(side="left")
            tk.Label(row, text=hint, bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                     font=(theme.FONT_FAMILY, 7)).pack(side="left", padx=(8, 0))
        env_actions = tk.Frame(env_box, bg=theme.SURFACE)
        env_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(env_actions, text="写入选中项", style="Accent.TButton",
                   command=self.apply_env).pack(side="left")
        ttk.Button(env_actions, text="还原环境变量", style="Ghost.TButton",
                   command=self.restore_env).pack(side="left", padx=(6, 0))
        self.env_state = tk.Label(env_box, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                                  font=(theme.MONO_FAMILY, 7), anchor="w", justify="left")
        self.env_state.pack(fill="x", pady=(8, 0))

    # ── 数据 ────────────────────────────────────────────
    def _base_url(self) -> str:
        try:
            return self.app.client.base_url()
        except Exception:  # noqa: BLE001
            return "http://127.0.0.1:8790"

    def _token(self) -> str:
        try:
            return self.app.client.token()
        except Exception:  # noqa: BLE001
            return ""

    def on_show(self) -> None:
        self.app._page_subtitle("base_url + token 拿走即用；Codex / 环境变量可以一键写，也能一键还原")
        self.base_field.set_value(self._base_url())
        self.token_field.set_value(self._token())
        self._refresh_codex_state()
        self._refresh_env_state()

    def on_snapshot(self, _snap: dict) -> None:
        base = self._base_url()
        if self.base_field.raw != base:
            self.base_field.set_value(base)
            self.token_field.set_value(self._token())
            self._rebuild_snippets()

    # ── 片段 ────────────────────────────────────────────
    def _build_snippets(self) -> None:
        data = clientcfg.snippets(self._base_url(), self._token(), self._model.get())
        for name, text in data.items():
            frame = tk.Frame(self.notebook, bg=theme.SURFACE_2)
            widget = tk.Text(frame, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                             insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 9),
                             wrap="none", height=13, padx=10, pady=8)
            widget.insert("1.0", text)
            widget.configure(state="disabled")
            widget.pack(fill="both", expand=True)
            self.notebook.add(frame, text=name)
            self._snippet_tabs[name] = widget

    def _rebuild_snippets(self) -> None:
        data = clientcfg.snippets(self._base_url(), self._token(), self._model.get())
        for name, widget in self._snippet_tabs.items():
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", data.get(name, ""))
            widget.configure(state="disabled")

    def _current_text(self) -> str:
        try:
            index = self.notebook.index("current")
        except tk.TclError:
            return ""
        names = list(self._snippet_tabs)
        if index < len(names):
            widget = self._snippet_tabs[names[index]]
            return widget.get("1.0", "end").rstrip("\n")
        return ""

    def copy_current(self) -> None:
        text = self._current_text()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("已复制当前片段", "ok")

    def copy_all(self) -> None:
        text = f"base_url: {self._base_url()}/v1\ntoken:    {self._token()}\n"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("已复制 base_url 与 token", "ok")

    def ping(self) -> None:
        if not self.app.healthy:
            self.app.toast("中转没在跑，先启动它", "warn")
            return
        model = self._model.get().strip()
        self.ping_result.configure(text=f"正在请求 {model} …", fg=theme.TEXT_DIM)

        def work():
            return self.app.client.probe_chat(model)

        self.app.run_async("ping", work, self._after_ping)

    def _after_ping(self, result, error) -> None:
        if error:
            self.ping_result.configure(text=f"自检失败：{error}", fg=theme.BAD)
            return
        if result.get("ok"):
            text = (f"链路 OK · HTTP {result['status']} · {result['seconds']:.1f}s"
                    f" · 回包：{(result.get('text') or '(空)')[:60]}")
            self.ping_result.configure(text=text, fg=theme.OK)
        else:
            body = result.get("body") or {}
            message = body.get("error") if isinstance(body, dict) else body
            self.ping_result.configure(
                text=f"HTTP {result['status']} · {str(message)[:220]}", fg=theme.BAD)

    # ── Codex ───────────────────────────────────────────
    def _refresh_codex_state(self) -> None:
        state = clientcfg.codex_state(self._base_url())
        path = state.get("path", "")
        if not state.get("exists"):
            text = f"未发现 {path}（写入时会新建）"
            color = theme.TEXT_DIM
        elif state.get("wired"):
            text = f"已接到本机中转 · {path}"
            color = theme.OK
        else:
            text = f"存在但没接上 · {path}"
            color = theme.WARN
        if state.get("has_restore_point"):
            text += " · 有还原点"
        self.codex_state.configure(text=text, fg=color)

    def preview_codex(self) -> None:
        try:
            content, created = clientcfg.preview_codex(self._base_url(), self._model.get().strip())
        except clientcfg.ClientConfigError as exc:
            self.app.toast(str(exc), "bad")
            return
        PreviewWindow(self, "Codex config.toml 预览（写入后长这样）", content,
                      note="只会改动 model_provider 与 [model_providers.opencode-pool] 段，其它段落原样保留。"
                           + ("（文件不存在，会新建）" if created else ""))

    def apply_codex(self) -> None:
        if not self.app.confirm("写入 Codex 配置",
                                "会重写 ~/.codex/config.toml 里的接入段。\n首次写入前会存一份原貌还原点。"):
            return
        try:
            result = clientcfg.apply_codex(self._base_url(), self._model.get().strip())
        except clientcfg.ClientConfigError as exc:
            self.app.toast(str(exc), "bad")
            return
        backup = result.get("backup") or ""
        self.app.toast(f"已写入 {result['path']}" + (f"（还原点 {backup}）" if backup else ""), "ok")
        self._refresh_codex_state()

    def restore_codex(self) -> None:
        if not self.app.confirm("还原 Codex 配置", "会把这台机器上的 codex/config.toml 恢复成接入前的样子。"):
            return
        try:
            result = clientcfg.restore_codex()
        except clientcfg.ClientConfigError as exc:
            self.app.toast(str(exc), "bad")
            return
        self.app.toast(f"已还原：{result.get('from') or result.get('reason') or result.get('path')}", "ok")
        self._refresh_codex_state()

    # ── 环境变量 ────────────────────────────────────────
    def _selected_env(self) -> tuple[str, ...]:
        return tuple(name for name, var in self._env_vars.items() if var.get())

    def _refresh_env_state(self) -> None:
        current = clientcfg.env_state()
        lines = [f"{name} = {value or '（未设置）'}" for name, value in current.items()]
        self.env_state.configure(text="\n".join(lines))

    def apply_env(self) -> None:
        selected = self._selected_env()
        if not selected:
            self.app.toast("先勾选要写的变量", "warn")
            return
        values = clientcfg.env_preview(self._base_url(), self._token(), selected)
        preview = "\n".join(f"{k} = {v}" for k, v in values.items())
        if not self.app.confirm("写入用户环境变量",
                               f"将写入：\n\n{preview}\n\n原值会存成备份，之后可以还原。"):
            return
        try:
            result = clientcfg.apply_env(values)
        except clientcfg.ClientConfigError as exc:
            self.app.toast(str(exc), "bad")
            return
        self.app.toast(f"已写入 {', '.join(result.get('changed') or [])}；新终端才生效", "ok")
        self._refresh_env_state()

    def restore_env(self) -> None:
        try:
            result = clientcfg.restore_env()
        except clientcfg.ClientConfigError as exc:
            self.app.toast(str(exc), "bad")
            return
        names = result.get("restored") or []
        self.app.toast(f"已还原 {len(names)} 个变量" if names else (result.get("reason") or "无需还原"), "ok")
        self._refresh_env_state()


class PreviewWindow(tk.Toplevel):
    """只读预览窗口：写盘前先看结果（cc-switch 的"切换前预览"同款做法）。"""

    def __init__(self, parent, title: str, content: str, note: str = ""):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=theme.SURFACE)
        self.geometry("760x520")
        self.transient(parent)
        body = tk.Frame(self, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=14)
        if note:
            tk.Label(body, text=note, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                     font=(theme.FONT_FAMILY, 8), anchor="w", justify="left",
                     wraplength=720).pack(fill="x", pady=(0, 8))
        wrap = tk.Frame(body, bg=theme.BORDER)
        wrap.pack(fill="both", expand=True)
        text = tk.Text(wrap, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0, wrap="none",
                       font=(theme.MONO_FAMILY, 9), padx=10, pady=8)
        text.insert("1.0", content)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        foot = tk.Frame(self, bg=theme.SURFACE)
        foot.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(foot, text="关掉", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")
