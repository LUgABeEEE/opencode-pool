"""设置页：客户端偏好 + 中转参数 + 诊断。

两类设置的边界很清楚（也是排障时最要紧的一点）：
  * 左边/上半是**客户端自己的**（项目目录、Python、托盘行为）→ `client.json`
  * 下半是**中转的**（水位线、并发、粘性、采样周期）→ `pool.config.json`
改后者等于改内核行为，所以保存后会问"要不要重载/重启"。
"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from tkinter import filedialog, ttk

from .. import autostart, localcfg, paths, relay_defaults, theme, util, widgets


class SettingsPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=theme.BG)
        self.app = app
        self._cfg_vars: dict[str, tk.StringVar] = {}
        self._relay_vars: dict[str, tk.StringVar] = {}

        scroller = widgets.ScrollFrame(self, bg=theme.BG)
        scroller.pack(fill="both", expand=True)
        body = scroller.body

        # ── 客户端 ──────────────────────────────────────
        client_card = widgets.Card(body, title="客户端（存在 %APPDATA%\\opencode-pool-client\\client.json）")
        client_card.pack(fill="x", pady=(0, 12))
        self._text_row(client_card.body, "项目目录", "project_dir",
                       "opencode-pool 仓库根（要能看到 opencode_pool/ 目录）", browse="dir")
        self._text_row(client_card.body, "中转配置", "config_path",
                       "pool.config.json 的路径；不存在会自动按 init 模板生成", browse="file")
        self._text_row(client_card.body, "Python", "python",
                       "留空 = 用当前解释器；必须装过 requests")

        row = tk.Frame(client_card.body, bg=theme.SURFACE)
        row.pack(fill="x", pady=(8, 0))
        self._check(row, "启动客户端即启动中转", "start_service_on_launch")
        self._check(row, "关闭窗口最小化到托盘", "close_to_tray")
        row2 = tk.Frame(client_card.body, bg=theme.SURFACE)
        row2.pack(fill="x", pady=(2, 0))
        self._check(row2, "退出客户端时停掉它自己拉起的中转", "stop_service_on_exit")
        self._check(row2, "危险操作要二次确认", "confirm_destructive")

        row3 = tk.Frame(client_card.body, bg=theme.SURFACE)
        row3.pack(fill="x", pady=(8, 0))
        tk.Label(row3, text="刷新间隔（秒）", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=(theme.FONT_FAMILY, 8)).pack(side="left")
        self.refresh_var = tk.StringVar()
        tk.Entry(row3, textvariable=self.refresh_var, width=6, bg=theme.SURFACE_2, fg=theme.TEXT,
                 bd=0, insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 9)
                 ).pack(side="left", padx=(6, 16), ipady=4)
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(row3, text="开机自启（当前用户 Run 键，--minimized 进托盘）",
                        variable=self.autostart_var,
                        command=self._toggle_autostart).pack(side="left")

        btns = tk.Frame(client_card.body, bg=theme.SURFACE)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="保存客户端设置", style="Accent.TButton",
                   command=self.save_client).pack(side="left")
        ttk.Button(btns, text="重新检测环境", style="Ghost.TButton",
                   command=self.check_env).pack(side="left", padx=(6, 0))
        self.env_label = tk.Label(btns, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                                  font=(theme.FONT_FAMILY, 8), anchor="w")
        self.env_label.pack(side="left", padx=(12, 0))

        # ── 中转参数 ────────────────────────────────────
        relay_card = widgets.Card(body, title="中转参数（写 pool.config.json，改完要重载）")
        relay_card.pack(fill="x", pady=(0, 12))
        listen_row = tk.Frame(relay_card.body, bg=theme.SURFACE)
        listen_row.pack(fill="x", pady=2)
        tk.Label(listen_row, text="监听地址", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=(theme.FONT_FAMILY, 8), width=16, anchor="w").pack(side="left")
        self._relay_vars["listen"] = tk.StringVar()
        tk.Entry(listen_row, textvariable=self._relay_vars["listen"], width=22,
                 bg=theme.SURFACE_2, fg=theme.TEXT, bd=0, insertbackground=theme.TEXT,
                 font=(theme.MONO_FAMILY, 9)).pack(side="left", ipady=4)
        tk.Label(listen_row, text="改端口要重启中转才生效", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 7)).pack(side="left", padx=(8, 0))

        for key, (label, help_text, low, high, step) in relay_defaults.FIELD_META.items():
            row = tk.Frame(relay_card.body, bg=theme.SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                     font=(theme.FONT_FAMILY, 8), width=16, anchor="w").pack(side="left")
            var = tk.StringVar()
            self._relay_vars[key] = var
            tk.Entry(row, textvariable=var, width=14, bg=theme.SURFACE_2, fg=theme.TEXT,
                     bd=0, insertbackground=theme.TEXT,
                     font=(theme.MONO_FAMILY, 9)).pack(side="left", ipady=4)
            tk.Label(row, text=f"{help_text}（{low}–{high}，步进 {step}）", bg=theme.SURFACE,
                     fg=theme.TEXT_MUTE, font=(theme.FONT_FAMILY, 7), anchor="w",
                     justify="left").pack(side="left", padx=(8, 0))

        relay_btns = tk.Frame(relay_card.body, bg=theme.SURFACE)
        relay_btns.pack(fill="x", pady=(12, 0))
        ttk.Button(relay_btns, text="保存到配置", style="Accent.TButton",
                   command=self.save_relay).pack(side="left")
        ttk.Button(relay_btns, text="保存并热重载", style="Ghost.TButton",
                   command=lambda: self.save_relay(reload=True)).pack(side="left", padx=(6, 0))
        ttk.Button(relay_btns, text="恢复上游默认值", style="Ghost.TButton",
                   command=self.reset_relay).pack(side="left", padx=(6, 0))

        # ── 诊断 ────────────────────────────────────────
        diag = widgets.Card(body, title="诊断")
        diag.pack(fill="x")
        diag_row = tk.Frame(diag.body, bg=theme.SURFACE)
        diag_row.pack(fill="x")
        ttk.Button(diag_row, text="校验中转配置（check）", style="Ghost.TButton",
                   command=self.run_check).pack(side="left")
        ttk.Button(diag_row, text="打开配置目录", style="Ghost.TButton",
                   command=lambda: self._open(paths.config_dir(self.app.settings.config_path()))
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(diag_row, text="打开客户端数据目录", style="Ghost.TButton",
                   command=lambda: self._open(paths.app_dir())).pack(side="left", padx=(6, 0))
        ttk.Button(diag_row, text="重启中转", style="Ghost.TButton",
                   command=self.app.restart_service).pack(side="left", padx=(6, 0))

        self.diag_text = tk.Text(diag.body, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                                 insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 8),
                                 height=9, wrap="none", padx=8, pady=6)
        self.diag_text.pack(fill="x", pady=(10, 0))
        self.diag_text.configure(state="disabled")

    # ── 小部件工厂 ──────────────────────────────────────
    def _text_row(self, parent, label: str, key: str, help_text: str, browse: str = "") -> None:
        row = tk.Frame(parent, bg=theme.SURFACE)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=(theme.FONT_FAMILY, 8), width=10, anchor="w").pack(side="left")
        var = tk.StringVar()
        self._cfg_vars[key] = var
        entry = tk.Entry(row, textvariable=var, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                         insertbackground=theme.TEXT, font=(theme.MONO_FAMILY, 8))
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        if browse:
            ttk.Button(row, text="浏览", style="Ghost.TButton", width=6,
                       command=lambda: self._pick(key, browse)).pack(side="left", padx=(6, 0))
        tk.Label(parent, text=help_text, bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                 font=(theme.FONT_FAMILY, 7), anchor="w").pack(fill="x", padx=(66, 0))

    def _check(self, parent, text: str, key: str) -> None:
        var = tk.BooleanVar(value=bool(self.app.settings[key]))
        setattr(self, f"_switch_{key}", var)

        def flip():
            self.app.settings[key] = bool(var.get())
            self.app.settings.save()

        ttk.Checkbutton(parent, text=text, variable=var, command=flip,
                        style="TCheckbutton").pack(side="left", padx=(0, 18))

    def _pick(self, key: str, kind: str) -> None:
        if kind == "dir":
            chosen = filedialog.askdirectory(parent=self, title="选择 opencode-pool 仓库根")
        else:
            chosen = filedialog.askopenfilename(parent=self, title="选择 pool.config.json",
                                               filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if chosen:
            self._cfg_vars[key].set(chosen)

    def _open(self, path: str) -> None:
        problem = util.open_path(path)
        if problem:
            self.app.toast(problem, "warn")

    # ── 载入 ────────────────────────────────────────────
    def on_show(self) -> None:
        self.app._page_subtitle("客户端偏好与中转参数分开：改哪个、什么时候生效都写在旁边")
        settings = self.app.settings
        self._cfg_vars["project_dir"].set(settings.project_dir())
        self._cfg_vars["config_path"].set(settings.config_path())
        self._cfg_vars["python"].set(settings["python"] or "")
        self.refresh_var.set(str(settings.refresh_seconds()))
        self.autostart_var.set(autostart.enabled())
        for key in ("start_service_on_launch", "close_to_tray", "stop_service_on_exit",
                    "confirm_destructive"):
            var = getattr(self, f"_switch_{key}", None)
            if var is not None:
                var.set(bool(settings[key]))
        self._load_relay_values()
        self.check_env()

    def _load_relay_values(self) -> None:
        try:
            snapshot = localcfg.options_snapshot(self.app.settings.config_path())
        except localcfg.LocalConfigError as exc:
            self._diag(f"读配置失败：{exc}")
            return
        for key, var in self._relay_vars.items():
            value = snapshot.get(key)
            if isinstance(value, float):
                var.set(f"{value:g}")
            else:
                var.set("" if value is None else str(value))

    def check_env(self) -> None:
        python = self.app.settings.python()
        ok, detail = paths.python_has_requests(python)
        project = self.app.settings.project_dir()
        pieces = [f"Python {os.path.basename(python)}",
                  ("requests " + detail) if ok else f"缺 requests：{detail}"]
        pieces.append(f"项目目录 {project or '（未找到）'}")
        self.env_label.configure(text=" · ".join(pieces),
                                 fg=theme.OK if (ok and project) else theme.BAD)

    # ── 保存 ────────────────────────────────────────────
    def save_client(self) -> None:
        settings = self.app.settings
        settings["project_dir"] = self._cfg_vars["project_dir"].get().strip()
        settings["config_path"] = self._cfg_vars["config_path"].get().strip()
        settings["python"] = self._cfg_vars["python"].get().strip()
        try:
            seconds = max(1, min(60, int(float(self.refresh_var.get() or 3))))
        except ValueError:
            self.app.toast("刷新间隔要是数字", "warn")
            return
        settings["refresh_seconds"] = seconds
        settings.save()

        # 配置路径变了 → 重建客户端与进程托管对象，别让它们还指着旧文件
        self.app.client = type(self.app.client)(settings.config_path())
        self.app.proc.settings = settings
        self.app.toast("客户端设置已保存", "ok")
        self.check_env()
        self._load_relay_values()
        self.app.refresh_now()

    def save_relay(self, reload: bool = False) -> None:
        values: dict[str, object] = {}
        for key, var in self._relay_vars.items():
            text = var.get().strip()
            if key == "listen":
                if not text:
                    self.app.toast("监听地址不能为空", "warn")
                    return
                values[key] = text
                continue
            meta = relay_defaults.FIELD_META.get(key)
            if meta is None:
                continue
            _label, _help, low, high, _step = meta
            try:
                number = float(text)
            except ValueError:
                self.app.toast(f"{meta[0]} 要填数字", "warn")
                return
            if not (low <= number <= high):
                self.app.toast(f"{meta[0]} 要在 {low}–{high} 之间", "warn")
                return
            values[key] = int(number) if float(number).is_integer() and key in (
                "max_inflight", "usage_refresh_seconds", "sticky_ttl_seconds",
                "upstream_timeout", "request_log_max_bytes", "request_log_keep") else number
        try:
            backup = localcfg.update_options(self.app.settings.config_path(), values)
        except localcfg.LocalConfigError as exc:
            self.app.toast(f"写配置失败：{exc}", "bad")
            return
        note = f"已写入配置{f'（备份 {os.path.basename(backup)}）' if backup else ''}"
        self._diag(note)
        if reload and self.app.healthy:
            def work():
                return self.app.client.reload()

            self.app.run_async("reload", work, self._after_reload)
        else:
            self.app.toast(note + ("；重启中转后生效" if not reload else ""), "ok")
            if reload:
                self.app.toast("中转没在跑，改完直接启动就是新参数", "warn")

    def _after_reload(self, result, error) -> None:
        if error:
            self.app.toast(f"热重载失败：{error}", "bad")
            return
        names = (result or {}).get("accounts") or []
        self.app.toast(f"已热重载：{len(names)} 个号", "ok")
        self.app.refresh_now()

    def reset_relay(self) -> None:
        for key, meta in relay_defaults.FIELD_META.items():
            default = relay_defaults.CFG_DEFAULTS.get(key, meta[2])
            var = self._relay_vars.get(key)
            if var is not None:
                var.set(f"{default:g}" if isinstance(default, float) else str(default))
        self._diag("已填回上游默认值（还没写盘，点「保存到配置」才生效）")

    # ── 诊断 ────────────────────────────────────────────
    def run_check(self) -> None:
        python = self.app.settings.python()
        project = self.app.settings.project_dir()
        config_path = self.app.settings.config_path()
        if not project:
            self._diag("没找到项目目录，去上面填一下")
            return
        self._diag("正在跑 opencode_pool check …")

        def work():
            return subprocess.run(
                [python, "-m", "opencode_pool", "check", "--config", config_path],
                cwd=project, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        self.app.run_async("check", work, self._after_check)

    def _after_check(self, result, error) -> None:
        if error:
            self._diag(f"check 跑不起来：{error}")
            return
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        self._diag(f"exit={result.returncode}\n{output or '(无输出)'}")

    def _diag(self, text: str) -> None:
        stamp = util.fmt_epoch_time()
        try:
            self.diag_text.configure(state="normal")
            self.diag_text.insert("end", f"[{stamp}] {text}\n")
            self.diag_text.see("end")
            self.diag_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _toggle_autostart(self) -> None:
        if self.autostart_var.get():
            ok, detail = autostart.enable()
            if ok:
                self._diag(f"已注册开机自启：{detail}")
            else:
                self.autostart_var.set(False)
                self._diag(f"注册失败：{detail}")
        else:
            ok, detail = autostart.disable()
            self._diag("已取消开机自启" if ok else f"取消失败：{detail}")
