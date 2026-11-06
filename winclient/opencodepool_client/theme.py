"""深色主题：配色、字体、圆角/描边绘制。

做成一个模块而不是散在页面里，是因为 tkinter 的默认控件外观太旧；
所有页面共用同一套色板和控件工厂，才能看起来像一个客户端而不是七个脚本。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# —— 色板（深色，靠灰阶层级而不是描边堆层次） ——
BG = "#0d1117"          # 窗口底
SURFACE = "#151b23"     # 卡片
SURFACE_2 = "#1b232e"   # 卡片内分区 / 表头
SURFACE_3 = "#222c39"   # hover / 选中
BORDER = "#26303c"
BORDER_SOFT = "#1e2732"
TEXT = "#e6edf3"
TEXT_DIM = "#9aa7b4"
TEXT_MUTE = "#6b7885"

ACCENT = "#4aa8ff"
ACCENT_DIM = "#2b6fb0"
OK = "#3fd68a"
WARN = "#f2c14e"
BAD = "#ff6b6b"
COOL = "#8b7cf6"
CYAN = "#35d0d8"

# 三档额度窗口固定配色：rolling / weekly / monthly
WINDOW_HUE = {
    "rolling": "#35d0d8",
    "weekly": "#4aa8ff",
    "monthly": "#8b7cf6",
}
WINDOW_FALLBACK = ("#35d0d8", "#4aa8ff", "#8b7cf6")

# 账号状态 → (中文, 色)
STATUS_STYLE = {
    "ready": ("可用", OK),
    "cooling": ("冷却中", COOL),
    "exhausted": ("额度打满", BAD),
    "below_reserve": ("低于水位", WARN),
    "disabled": ("已暂停", TEXT_MUTE),
    "no_credentials": ("缺密钥", BAD),
    "stale": ("采样过期", WARN),
    "no_quota": ("无额度观测", TEXT_DIM),
    "offline": ("中转未启动", TEXT_MUTE),
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


class Theme:
    """一次算好所有字体对象（tkinter 每次 new font 都要建 Tcl 对象，很贵）。"""

    def __init__(self, root: tk.Misc):
        self.base = FONT_FAMILY
        self.mono = MONO_FAMILY
        self.f_title = tkfont.Font(root=root, family=self.base, size=15, weight="bold")
        self.f_h2 = tkfont.Font(root=root, family=self.base, size=11, weight="bold")
        self.f_body = tkfont.Font(root=root, family=self.base, size=9)
        self.f_small = tkfont.Font(root=root, family=self.base, size=8)
        self.f_tiny = tkfont.Font(root=root, family=self.base, size=7)
        self.f_num = tkfont.Font(root=root, family=self.base, size=20, weight="bold")
        self.f_num_sm = tkfont.Font(root=root, family=self.base, size=12, weight="bold")
        self.f_mono = tkfont.Font(root=root, family=self.mono, size=9)
        self.f_mono_sm = tkfont.Font(root=root, family=self.mono, size=8)

    # —— ttk 全局样式 ——
    def apply(self, root: tk.Misc) -> None:
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        root.configure(bg=BG)

        style.configure("TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Surface2.TFrame", background=SURFACE_2)

        style.configure("TLabel", background=BG, foreground=TEXT, font=self.f_body)
        style.configure("Dim.TLabel", background=BG, foreground=TEXT_DIM, font=self.f_small)
        style.configure("Mute.TLabel", background=BG, foreground=TEXT_MUTE, font=self.f_tiny)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=self.f_title)
        style.configure("H2.TLabel", background=BG, foreground=TEXT, font=self.f_h2)
        style.configure("Surface.TLabel", background=SURFACE, foreground=TEXT, font=self.f_body)
        style.configure("SurfaceDim.TLabel", background=SURFACE, foreground=TEXT_DIM, font=self.f_small)
        style.configure("Mono.TLabel", background=SURFACE, foreground=TEXT, font=self.f_mono)

        # 按钮：扁平 + 悬停提亮，靠 padding 撑出点击区域
        style.configure("TButton", background=SURFACE_3, foreground=TEXT, borderwidth=0,
                        focusthickness=0, font=self.f_small, padding=(12, 6))
        style.map("TButton",
                  background=[("pressed", BORDER), ("active", "#2b3745")],
                  foreground=[("disabled", TEXT_MUTE)])
        style.configure("Accent.TButton", background=ACCENT_DIM, foreground="#ffffff",
                        borderwidth=0, font=self.f_small, padding=(12, 6))
        style.map("Accent.TButton",
                  background=[("pressed", "#20527f"), ("active", ACCENT)])
        style.configure("Ghost.TButton", background=SURFACE, foreground=TEXT_DIM,
                        borderwidth=0, font=self.f_small, padding=(10, 5))
        style.map("Ghost.TButton",
                  background=[("active", SURFACE_3)],
                  foreground=[("active", TEXT)])
        style.configure("Danger.TButton", background="#3a2027", foreground=BAD,
                        borderwidth=0, font=self.f_small, padding=(10, 5))
        style.map("Danger.TButton", background=[("active", "#4d2a33")])

        # 导航
        style.configure("Nav.TButton", background=BG, foreground=TEXT_DIM, borderwidth=0,
                        anchor="w", font=self.f_body, padding=(16, 9))
        style.map("Nav.TButton", background=[("active", SURFACE_2)], foreground=[("active", TEXT)])
        style.configure("NavOn.TButton", background=SURFACE_2, foreground=ACCENT, borderwidth=0,
                        anchor="w", font=self.f_body, padding=(16, 9))
        style.map("NavOn.TButton", background=[("active", SURFACE_2)], foreground=[("active", ACCENT)])

        # 表格
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                        foreground=TEXT, borderwidth=0, rowheight=26, font=self.f_small)
        style.configure("Treeview.Heading", background=SURFACE_2, foreground=TEXT_DIM,
                        borderwidth=0, font=self.f_small, relief="flat", padding=(6, 5))
        style.map("Treeview", background=[("selected", "#2a3a4d")],
                  foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", SURFACE_3)])

        # 输入
        style.configure("TEntry", fieldbackground=SURFACE_2, background=SURFACE_2,
                        foreground=TEXT, insertcolor=TEXT, borderwidth=0, padding=6)
        style.map("TEntry", fieldbackground=[("disabled", SURFACE)])
        style.configure("TCombobox", fieldbackground=SURFACE_2, background=SURFACE_3,
                        foreground=TEXT, arrowcolor=TEXT_DIM, borderwidth=0, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE_2)])
        root.option_add("*TCombobox*Listbox.background", SURFACE_2)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)

        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT, font=self.f_small)
        style.map("TCheckbutton", background=[("active", SURFACE)],
                  indicatorcolor=[("selected", ACCENT)])
        style.configure("TSeparator", background=BORDER_SOFT)
        style.configure("TScrollbar", background=SURFACE_2, troughcolor=BG, borderwidth=0,
                        arrowcolor=TEXT_MUTE)
        style.map("TScrollbar", background=[("active", SURFACE_3)])

        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=TEXT_DIM,
                        padding=(14, 7), font=self.f_small, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", SURFACE)],
                  foreground=[("selected", TEXT)])
        style.configure("TProgressbar", background=ACCENT, troughcolor=SURFACE_2,
                        borderwidth=0, thickness=6)


# —— 直观的颜色工具 ——
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def mix(color_a: str, color_b: str, ratio: float) -> str:
    """两色按比例插值（ratio=0 → a，1 → b）。用于条/环的渐变感。"""
    ratio = clamp(ratio)
    a = _rgb(color_a)
    b = _rgb(color_b)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    try:
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        return 255, 255, 255


def usage_color(percent: float | None) -> str:
    """按"用得越多越危险"上色：<60 绿，60–85 黄，>85 红，未知灰。"""
    if percent is None:
        return TEXT_MUTE
    if percent >= 85:
        return BAD
    if percent >= 60:
        return WARN
    return OK


def headroom_color(headroom: float | None) -> str:
    if headroom is None:
        return TEXT_MUTE
    if headroom <= 10:
        return BAD
    if headroom <= 30:
        return WARN
    return OK


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius: float, **kwargs):
    """Canvas 没有圆角矩形，用多边形 + smooth 近似；半径夹在短边一半以内。"""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)
