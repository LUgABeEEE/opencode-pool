"""主窗口：侧栏导航 + 顶栏状态 + 五个页面 + 托盘。

线程模型只有一条规矩：**tkinter 只许主线程碰**。
所以后台线程（拉额度、起停服务、写配置）一律把结果塞进 `self.q`，
主线程用 `after()` 轮询队列再更新界面。看起来绕，但这是 tk 唯一可靠的做法。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import icon, localcfg, paths, single_instance, theme, tray, util, widgets
from .relay import RelayClient, RelayError, RelayProcess
from .settings import Settings

APP_TITLE = "OpenCode 号池 · Windows 客户端"


class App(tk.Tk):
    PAGES = (
        ("dashboard", "仪表盘"),
        ("accounts", "账号"),
        ("connect", "接驳"),
    )

    def __init__(self, minimized: bool = False, config_override: str = ""):
        super().__init__()
        self.settings = Settings.load()
        if config_override:
            self.settings["config_path"] = config_override

        self.title(APP_TITLE)
        self.minsize(1060, 660)
        self._restore_geometry()
        self.theme = theme.Theme(self)
        self.theme.apply(self)
        self._apply_icon()

        self.client = RelayClient(self.settings.config_path())
        self.q: queue.Queue = queue.Queue()
        self.proc = RelayProcess(
            self.settings,
            on_line=lambda line: self.q.put(("log", line)),
            on_exit=lambda code: self.q.put(("proc_exit", code)),
        )
        self._polling = False

        self.snap: dict = {}
        self.schema: dict = {}
        self.snap_at: float = 0.0
        self.healthy: bool = False
        self.svc_state: str = "stopped"      # stopped | starting | running | attached | error
        self.svc_text: str = "中转未启动"
        self._pages: dict[str, object] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}
        self.current_page: str = self.settings["last_page"] or "dashboard"
        self._busy: set[str] = set()
        self._closing = False
        self._toast_after = None
        self._last_poll_error = ""

        self._build()
        self._start_tray()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-r>", lambda _e: self.refresh_now())
        self.bind("<F5>", lambda _e: self.refresh_now())

        self.after(400, self._boot)
        self.after(120, self._drain)
        self.after(500, self._tick)
        if minimized:
            self.withdraw()
        else:
            self.deiconify()

    # ── 构图 ────────────────────────────────────────────
    def _build(self) -> None:
        root = tk.Frame(self, bg=theme.BG)
        root.pack(fill="both", expand=True)
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_sidebar(root)
        main = tk.Frame(root, bg=theme.BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self._build_topbar(main)
        self.content = tk.Frame(main, bg=theme.BG)
        self.content.grid(row=1, column=0, sticky="nsew", padx=(2, 0), pady=(0, 0))
        self._build_statusbar(main)

        self.show_page(self.current_page, first=True)

    def _build_sidebar(self, parent) -> None:
        bar = tk.Frame(parent, bg=theme.SURFACE, width=196)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.pack_propagate(False)

        brand = tk.Frame(bar, bg=theme.SURFACE)
        brand.pack(fill="x", padx=16, pady=(18, 12))
        tk.Label(brand, text="OPENCODE", bg=theme.SURFACE, fg=theme.ACCENT,
                 font=(theme.FONT_FAMILY, 8, "bold"), anchor="w").pack(fill="x")
        tk.Label(brand, text="号池客户端", bg=theme.SURFACE, fg=theme.TEXT,
                 font=(theme.FONT_FAMILY, 13, "bold"), anchor="w").pack(fill="x")

        nav = tk.Frame(bar, bg=theme.SURFACE)
        nav.pack(fill="x", pady=(4, 0))
        for key, label in self.PAGES:
            btn = ttk.Button(nav, text=f"  {label}", style="Nav.TButton",
                             command=lambda k=key: self.show_page(k))
            btn.pack(fill="x", padx=8, pady=1)
            self._nav_buttons[key] = btn

        spacer = tk.Frame(bar, bg=theme.SURFACE)
        spacer.pack(fill="both", expand=True)

        panel = tk.Frame(bar, bg=theme.SURFACE_2)
        panel.pack(fill="x", padx=10, pady=10)
        inner = tk.Frame(panel, bg=theme.SURFACE_2)
        inner.pack(fill="x", padx=12, pady=12)

        row = tk.Frame(inner, bg=theme.SURFACE_2)
        row.pack(fill="x")
        self.svc_dot = tk.Canvas(row, width=10, height=10, bg=theme.SURFACE_2,
                                 highlightthickness=0, bd=0)
        self.svc_dot.pack(side="left", pady=2)
        self.svc_dot_id = self.svc_dot.create_oval(1, 1, 9, 9, fill=theme.TEXT_MUTE, outline="")
        tk.Label(row, text=" 中转", bg=theme.SURFACE_2, fg=theme.TEXT_DIM,
                 font=(theme.FONT_FAMILY, 8)).pack(side="left")
        self.svc_label = tk.Label(inner, text="已停止", bg=theme.SURFACE_2, fg=theme.TEXT_MUTE,
                                  font=(theme.FONT_FAMILY, 9, "bold"), anchor="w")
        self.svc_label.pack(fill="x", pady=(4, 8))

        self.btn_service = ttk.Button(inner, text="启动中转", style="Accent.TButton",
                                      command=self.toggle_service)
        self.btn_service.pack(fill="x")
        ttk.Button(inner, text="立即查额度", style="Ghost.TButton",
                   command=self.refresh_quota).pack(fill="x", pady=(6, 0))
        ttk.Button(inner, text="打开控制台", style="Ghost.TButton",
                   command=self.open_panel).pack(fill="x", pady=(4, 0))

    def _build_topbar(self, parent) -> None:
        bar = tk.Frame(parent, bg=theme.BG, height=58)
        bar.grid(row=0, column=0, sticky="ew", padx=(20, 20), pady=(14, 0))
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=theme.BG)
        left.pack(side="left", fill="y")
        self.page_title = tk.Label(left, text="仪表盘", bg=theme.BG, fg=theme.TEXT,
                                   font=(theme.FONT_FAMILY, 15, "bold"), anchor="w")
        self.page_title.pack(anchor="w")
        self.page_sub = tk.Label(left, text="", bg=theme.BG, fg=theme.TEXT_MUTE,
                                 font=(theme.FONT_FAMILY, 8), anchor="w")
        self.page_sub.pack(anchor="w", pady=(1, 0))

        right = tk.Frame(bar, bg=theme.BG)
        right.pack(side="right", fill="y")
        self.top_pill = widgets.Pill(right, "中转未启动", theme.TEXT_MUTE, theme.SURFACE_2)
        self.top_pill.pack(side="left", padx=(0, 10))
        self.top_stats = tk.Label(right, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                                  font=(theme.MONO_FAMILY, 9))
        self.top_stats.pack(side="left", padx=(0, 12))
        ttk.Button(right, text="刷新", style="Ghost.TButton",
                   command=self.refresh_now).pack(side="left")

    def _build_statusbar(self, parent) -> None:
        bar = tk.Frame(parent, bg=theme.SURFACE, height=28)
        bar.grid(row=2, column=0, sticky="ew")
        bar.pack_propagate(False)
        self.status_left = tk.Label(bar, text="", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                                    font=(theme.MONO_FAMILY, 8), anchor="w")
        self.status_left.pack(side="left", padx=14)
        self.status_right = tk.Label(bar, text="", bg=theme.SURFACE, fg=theme.TEXT_MUTE,
                                     font=(theme.FONT_FAMILY, 8), anchor="e")
        self.status_right.pack(side="right", padx=14)

    def _apply_icon(self) -> None:
        try:
            ico = icon.ensure_icon(paths.client_icon_path())
            if ico:
                self.iconbitmap(default=ico)
                self._icon_path = ico
        except (tk.TclError, OSError):
            self._icon_path = ""

    def _restore_geometry(self) -> None:
        geometry = str(self.settings["geometry"] or "")
        try:
            if geometry:
                self.geometry(geometry)
            else:
                self.geometry("1240x800")
        except tk.TclError:
            self.geometry("1240x800")

    # ── 页面切换 ────────────────────────────────────────
    def show_page(self, key: str, first: bool = False) -> None:
        if key not in dict(self.PAGES):
            key = "dashboard"
        self.current_page = key
        for name, btn in self._nav_buttons.items():
            btn.configure(style="NavOn.TButton" if name == key else "Nav.TButton")
        self.page_title.configure(text=dict(self.PAGES)[key])
        for page in self._pages.values():
            page.pack_forget()

        page = self._pages.get(key)
        if page is None:
            page = self._create_page(key)
            self._pages[key] = page
        page.pack(fill="both", expand=True, padx=(18, 20), pady=(6, 12))
        try:
            page.on_show()
        except AttributeError:
            pass
        if not first:
            self._push_state_to(page)
        self.settings["last_page"] = key

    def _create_page(self, key: str):
        from .pages import accounts, connect, dashboard

        mapping = {
            "dashboard": dashboard.DashboardPage,
            "accounts": accounts.AccountsPage,
            "connect": connect.ConnectPage,
        }
        return mapping[key](self.content, self)

    def _push_state_to(self, page) -> None:
        try:
            page.on_status(self.healthy, self.svc_text)
        except AttributeError:
            pass
        try:
            page.on_snapshot(self.snap)
        except AttributeError:
            pass

    # ── 启动/退出 ───────────────────────────────────────
    def _boot(self) -> None:
        try:
            config_path = self.settings.config_path()
            created = not os.path.isfile(config_path)
            raw, _ = localcfg.ensure_config(config_path)
            if created:
                self.toast(f"已生成配置：{config_path}")
        except localcfg.LocalConfigError as exc:
            self.toast(f"配置初始化失败：{exc}", "bad")
            return

        # 空池子起不了服务（上游 validate 会直接退出），别让首屏变成一条报错：
        # 直接把人送到"加第一个号"的地方。
        if not localcfg.accounts(raw):
            if self._first_run_seen():
                self.toast("池子里还没有号：先在「账号」页加一个 key", "warn")
            self.show_page("accounts")
            self.refresh_now()
            return
        if self.settings["start_service_on_launch"]:
            self.start_service()
        else:
            self.refresh_now()

    def _first_run_seen(self) -> bool:
        """空池子提示只在第一次说，之后每次启动都提醒就成噪声了。"""
        if self.settings["last_page"]:
            return True
        return False

    def on_close(self) -> None:
        if self.settings["close_to_tray"] and getattr(self, "tray_ok", False) and not self._closing:
            self.withdraw()
            self.toast("已最小化到托盘")
            return
        self.do_exit()

    def do_exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.settings["geometry"] = self.geometry()
            if self.settings["stop_service_on_exit"]:
                self.proc.stop(timeout=4.0)
            self.settings.save()
        except Exception:  # noqa: BLE001 - 退出路径不许抛
            pass
        try:
            if getattr(self, "tray", None):
                self.tray.stop()
        except Exception:  # noqa: BLE001
            pass
        self.after(60, self.destroy)

    # ── 托盘 ────────────────────────────────────────────
    def _start_tray(self) -> None:
        ico = getattr(self, "_icon_path", "") or paths.client_icon_path()
        items = [
            (1001, "显示主窗口", "show"),
            (1002, "-"),
            (1003, "启动中转", "start"),
            (1004, "停止中转", "stop"),
            (1005, "-"),
            (1006, "立即查额度", "refresh"),
            (1007, "打开控制台", "panel"),
            (1008, "-"),
            (1009, "退出", "quit"),
        ]
        self.tray = tray.Tray(
            tooltip="OpenCode 号池", icon_path=ico,
            on_action=lambda _a: None, menu_items=items,
            dynamic=self._tray_state,
        )
        self.tray_ok = self.tray.start()
        if not self.tray_ok:
            self.settings["close_to_tray"] = False
        self.after(250, self._poll_tray)

    def _tray_state(self) -> dict:
        running = self.svc_state in ("running", "attached")
        counts = self._counts()
        return {
            "start": {"enabled": not running},
            "stop": {"enabled": running},
            "show": {"text": f"显示主窗口（{counts.get('ready', 0)}/{counts.get('total', 0)} 可用）"},
        }

    def _poll_tray(self) -> None:
        if self._closing:
            return
        try:
            for action in self.tray.poll():
                self._tray_action(action)
        except Exception:  # noqa: BLE001
            pass
        self.after(250, self._poll_tray)

    def _tray_action(self, action: str) -> None:
        if action == "show":
            self.deiconify()
            self.lift()
            self.focus_force()
        elif action == "start":
            self.start_service()
        elif action == "stop":
            self.stop_service()
        elif action == "refresh":
            self.refresh_quota()
        elif action == "panel":
            self.open_panel()
        elif action == "quit":
            self.do_exit()

    # ── 服务控制 ────────────────────────────────────────
    def toggle_service(self) -> None:
        if self.svc_state in ("running", "attached", "starting"):
            self.stop_service()
        else:
            self.start_service()

    def start_service(self) -> None:
        if self.svc_state == "starting":
            return
        self.svc_state = "starting"
        self._set_service_ui()
        self.toast("正在启动中转…")

        def work():
            ok, message = self.proc.start()
            return ok, message

        self.run_async("start", work, self._after_start)

    def _after_start(self, result, error) -> None:
        if error:
            self.svc_state = "error"
            self.svc_text = f"启动失败：{error}"
            self._set_service_ui()
            self.toast(self.svc_text, "bad")
            return
        ok, message = result
        if ok:
            self.svc_state = "attached" if self.proc.attached else "running"
            self.svc_text = message
            self.toast(message, "ok")
            self._set_service_ui()
            self.refresh_now()
        else:
            self.svc_state = "error"
            self.svc_text = message
            self._set_service_ui()
            self.toast(message.splitlines()[0] if message else "启动失败", "bad")

    def stop_service(self) -> None:
        self.toast("正在停止中转…")
        self.svc_state = "stopping"
        self._set_service_ui()

        def work():
            return self.proc.stop()

        self.run_async("stop", work, lambda result, error: self._after_stop(result, error))

    def _after_stop(self, result, error) -> None:
        if error:
            self.svc_state = "error"
            self.svc_text = f"停止失败：{error}"
        else:
            self.svc_state = "stopped"
            self.svc_text = result[1] if result else "已停止"
        self.healthy = False
        self.snap = {}
        self._set_service_ui()
        self._broadcast()
        self.toast(self.svc_text, "ok" if not error else "bad")

    def restart_service(self) -> None:
        self.toast("正在重启中转…")

        def work():
            return self.proc.restart()

        self.svc_state = "starting"
        self._set_service_ui()
        self.run_async("restart", work, self._after_start)

    def _set_service_ui(self) -> None:
        colors = {
            "stopped": theme.TEXT_MUTE,
            "starting": theme.WARN,
            "stopping": theme.WARN,
            "running": theme.OK,
            "attached": theme.CYAN,
            "error": theme.BAD,
        }
        labels = {
            "stopped": "已停止",
            "starting": "启动中…",
            "stopping": "停止中…",
            "running": "运行中",
            "attached": "外部实例",
            "error": "出错",
        }
        color = colors.get(self.svc_state, theme.TEXT_MUTE)
        try:
            self.svc_dot.itemconfigure(self.svc_dot_id, fill=color)
            self.svc_label.configure(text=labels.get(self.svc_state, self.svc_state), fg=color)
            self.btn_service.configure(
                text="停止中转" if self.svc_state in ("running", "attached", "starting") else "启动中转")
            self.top_pill.set(
                {True: "中转在线", False: "中转离线"}[self.svc_state in ("running", "attached")],
                {True: theme.OK, False: theme.TEXT_MUTE}[self.svc_state in ("running", "attached")])
        except tk.TclError:
            pass

    # ── 数据轮询 ────────────────────────────────────────
    def _counts(self) -> dict:
        snap = self.snap or {}
        return {
            "total": snap.get("total_accounts", 0),
            "ready": snap.get("healthy_accounts", 0),
            "recent": snap.get("requests_last_5m", 0),
        }

    def refresh_now(self) -> None:
        # 上一次还没回来就跳过：中转慢或卡住时不该堆出一串线程
        if self._polling:
            return
        self._polling = True

        def work():
            healthy = self.client.healthy()
            if not healthy:
                return {"healthy": False}
            snap = self.client.snapshot()
            schema = {}
            try:
                schema = self.client.schema()
            except RelayError:
                pass
            return {"healthy": True, "snapshot": snap, "schema": schema}

        def done(result, error):
            self._polling = False
            self._after_poll(result, error)

        self.run_async("poll", work, done, quiet=True)

    def _after_poll(self, result, error) -> None:
        if error:
            self.healthy = False
            self.svc_text = f"读不到中转：{error}"
            self._last_poll_error = str(error)
            if self.svc_state == "running":
                self.svc_state = "error"
            self._set_service_ui()
            self._broadcast()
            return
        self._last_poll_error = ""
        if not result.get("healthy"):
            self.healthy = False
            if self.svc_state in ("running", "attached"):
                self.svc_state = "stopped"
            self.svc_text = "中转未启动"
            self._set_service_ui()
            self._broadcast()
            return
        self.healthy = True
        self.snap = result.get("snapshot") or {}
        self.snap_at = time.time()
        if result.get("schema"):
            self.schema = result["schema"]
        if self.svc_state in ("stopped", "error", "starting"):
            self.svc_state = "attached" if self.proc.attached else "running"
            self.svc_text = f"中转在线 · {self.client.base_url()}"
        self._set_service_ui()
        self._broadcast()

    def refresh_quota(self) -> None:
        if not self.healthy:
            self.toast("中转没在跑，先启动它", "warn")
            return
        self.toast("正在向上游拉三档额度…")

        def work():
            return self.client.refresh()

        self.run_async("refresh", work, self._after_quota)

    def _after_quota(self, result, error) -> None:
        if error:
            self.toast(f"刷新失败：{error}", "bad")
            return
        self.snap = result if isinstance(result, dict) else self.snap
        self.snap_at = time.time()
        self._broadcast()
        self.toast("额度已刷新", "ok")

    def open_panel(self) -> None:
        try:
            url = self.client.base_url() + "/"
        except Exception:  # noqa: BLE001
            url = "http://127.0.0.1:8790/"
        webbrowser.open(url)

    # ── 广播到页面 ──────────────────────────────────────
    def _broadcast(self) -> None:
        counts = self._counts()
        try:
            self.top_stats.configure(
                text=f"在线 {counts['ready']}/{counts['total']}   5min {counts['recent']} 次")
        except tk.TclError:
            pass
        page = self._pages.get(self.current_page)
        if page is not None:
            self._push_state_to(page)
        self._update_statusbar()
        self._update_tray_tip()

    def _update_statusbar(self) -> None:
        try:
            self.status_left.configure(text=f"{self.client.base_url()}/v1")
            self.status_right.configure(
                text=f"配置 {self.settings.config_path()} · 队列/采样 {util.fmt_age(self.snap_at) if self.snap_at else '未采样'}")
        except tk.TclError:
            pass

    def _update_tray_tip(self) -> None:
        if not getattr(self, "tray_ok", False):
            return
        counts = self._counts()
        state = "在线" if self.healthy else "离线"
        try:
            self.tray.set_tooltip(f"OpenCode 号池 · {state} · {counts['ready']}/{counts['total']} 可用")
        except Exception:  # noqa: BLE001
            pass

    def _page_subtitle(self, text: str) -> None:
        try:
            self.page_sub.configure(text=text)
        except tk.TclError:
            pass

    # ── 异步任务与消息泵 ────────────────────────────────
    def run_async(self, label: str, fn, on_done=None, quiet: bool = False) -> None:
        if not quiet:
            self._busy.add(label)

        def worker():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
                self.q.put(("done", label, None, exc, on_done))
                return
            self.q.put(("done", label, result, None, on_done))

        threading.Thread(target=worker, daemon=True, name=f"task-{label}").start()

    def _drain(self) -> None:
        if self._closing:
            return
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "done":
                    _, label, result, error, on_done = item
                    self._busy.discard(label)
                    if on_done is not None:
                        try:
                            on_done(result, error)
                        except Exception as exc:  # noqa: BLE001
                            self.toast(f"界面更新出错：{type(exc).__name__}: {exc}", "bad")
                elif kind == "log":
                    page = self._pages.get("requests")
                    if page is not None and hasattr(page, "append_log"):
                        page.append_log(item[1])
                elif kind == "proc_exit":
                    if not self._closing and self.svc_state in ("running", "starting"):
                        self.svc_state = "error"
                        self.svc_text = f"中转进程退出了（code={item[1]}）"
                        self._set_service_ui()
                        self.toast(self.svc_text, "bad")
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _tick(self) -> None:
        """每秒一次：轮询是否到点 + 让需要"倒计时"的页面重绘。"""
        if self._closing:
            return
        now = time.time()
        if now - self.snap_at >= self.settings.refresh_seconds() and not self._busy:
            self.refresh_now()
        page = self._pages.get(self.current_page)
        if page is not None and hasattr(page, "on_tick"):
            try:
                page.on_tick()
            except Exception:  # noqa: BLE001
                pass
        self.after(1000, self._tick)

    # ── 提示 ────────────────────────────────────────────
    def toast(self, message: str, kind: str = "info") -> None:
        colors = {"info": theme.TEXT_DIM, "ok": theme.OK, "warn": theme.WARN, "bad": theme.BAD}
        try:
            self.status_left.configure(text=message[:160], fg=colors.get(kind, theme.TEXT_DIM))
        except tk.TclError:
            return
        if self._toast_after:
            try:
                self.after_cancel(self._toast_after)
            except (tk.TclError, ValueError):
                pass
        self._toast_after = self.after(6000, self._update_statusbar)

    def confirm(self, title: str, message: str) -> bool:
        if not self.settings["confirm_destructive"]:
            return True
        return bool(messagebox.askyesno(title, message, parent=self))

    def notify(self, title: str, message: str, kind: str = "info") -> None:
        self.toast(message, kind)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="opencodepool-client", description="OpenCode 号池 Windows 客户端")
    parser.add_argument("--minimized", action="store_true", help="启动后直接进托盘")
    parser.add_argument("--config", default="", help="指定 pool.config.json 路径")
    args = parser.parse_args(argv)

    # 单实例：第二次双击 = 把已有窗口叫回来，而不是再起一个内核抢端口
    guard = single_instance.SingleInstance()
    if guard.already_running:
        if single_instance.focus_existing(APP_TITLE) or single_instance.focus_existing(APP_TITLE, tries=24):
            return 0
        # 认不出窗口（上一实例可能正在退），继续起一份；端口冲突会在界面上讲清楚
    try:
        app = App(minimized=args.minimized, config_override=args.config)
    except tk.TclError as exc:
        print(f"界面起不来（{exc}）—— 这台机器没有可用的图形会话？")
        guard.release()
        return 1
    try:
        app.mainloop()
    finally:
        guard.release()
    return 0
