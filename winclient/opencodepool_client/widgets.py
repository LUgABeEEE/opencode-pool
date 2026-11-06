"""复用小控件：额度环、余量条、状态胶囊、卡片、滚动容器、可复制字段。

全部基于标准 tkinter（没有 ttkbootstrap / customtkinter），因为客户端必须
"装个 Python 双击就跑"；外观统一走 theme.py 的色板。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


class Ring(tk.Canvas):
    """三档额度环：一圈底色 + 按 percent 画出的弧 + 中心数字。

    用作"用量"展示，所以颜色是"用得越多越危险"（绿→黄→红），不是主题色。
    """

    def __init__(self, master, size: int = 84, width: int = 8, label: str = "",
                 bg: str = theme.SURFACE, family: str = theme.FONT_FAMILY):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self.size = size
        self.width = width
        self.label = label
        self.bg = bg
        self._family = family
        self._arc = None
        self.value: float | None = None

    def set(self, percent: float | None, sub: str = "") -> None:
        self.delete("all")
        pad = self.width / 2 + 2
        box = (pad, pad, self.size - pad, self.size - pad)
        # 底环
        self.create_oval(*box, outline=theme.BORDER, width=self.width, tags="track")
        value = None if percent is None else max(0.0, min(100.0, float(percent)))
        self.value = value
        if value is not None and value > 0.2:
            color = theme.usage_color(value)
            extent = -359.9 * (value / 100.0)
            self.create_arc(*box, start=90, extent=extent, style="arc",
                            outline=color, width=self.width, tags="arc")
        text = "—" if value is None else f"{value:.0f}%"
        color = theme.TEXT_DIM if value is None else theme.TEXT
        self.create_text(self.size / 2, self.size / 2 - (4 if sub else 0), text=text,
                         fill=color, font=(self._family, max(8, int(self.size / 7)), "bold"))
        if sub:
            self.create_text(self.size / 2, self.size / 2 + self.size / 7, text=sub,
                             fill=theme.TEXT_MUTE, font=(self._family, 7))
        if self.label:
            self.create_text(self.size / 2, self.size - 6, text=self.label,
                             fill=theme.TEXT_DIM, font=(self._family, 7))


class Meter(tk.Canvas):
    """横向余量条：左绿右红，值 = 余量（100−占用）。"""

    def __init__(self, master, width: int = 180, height: int = 6, bg: str = theme.SURFACE):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        # 注意：不能叫 self._w —— 那是 tkinter.Misc 存 widget 路径名的内部属性，
        # 覆盖掉之后所有 tk 调用都会报 `invalid command name "180"`。
        self._bar_width = width
        self._bar_height = height
        self._bg = bg
        self.value: float | None = None

    def set(self, headroom: float | None) -> None:
        self.delete("all")
        theme.rounded_rect(self, 0, 0, self._bar_width, self._bar_height, self._bar_height / 2,
                           fill=theme.BORDER_SOFT, outline="")
        if headroom is None:
            return
        ratio = max(0.0, min(1.0, headroom / 100.0))
        if ratio <= 0:
            return
        color = theme.headroom_color(headroom)
        theme.rounded_rect(self, 0, 0, max(self._bar_height, self._bar_width * ratio),
                           self._bar_height, self._bar_height / 2, fill=color, outline="")
        self.value = headroom


class Pill(tk.Label):
    """状态胶囊：一个带底色的小标签。"""

    def __init__(self, master, text: str = "", fg: str = theme.TEXT_DIM,
                 bg: str = theme.SURFACE_2, font=None):
        super().__init__(master, text=text, fg=fg, bg=bg, padx=7, pady=2,
                         font=font or (theme.FONT_FAMILY, 8), bd=0)

    def set(self, text: str, fg: str, bg: str | None = None) -> None:
        self.configure(text=text, fg=fg)
        if bg is not None:
            self.configure(bg=bg)


class Card(tk.Frame):
    """带细边和标题的卡片容器（内容塞进 self.body）。"""

    def __init__(self, master, title: str = "", bg: str = theme.SURFACE,
                 border: str = theme.BORDER, pad: int = 14):
        super().__init__(master, bg=border, bd=0, highlightthickness=0)
        self.inner = tk.Frame(self, bg=bg, bd=0, highlightthickness=0)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(self.inner, bg=bg)
        self.body.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.title_label = None
        if title:
            self.title_label = tk.Label(self.body, text=title, bg=bg, fg=theme.TEXT,
                                        font=(theme.FONT_FAMILY, 10, "bold"), anchor="w")
            self.title_label.pack(fill="x", pady=(0, 8))


class ScrollFrame(tk.Frame):
    """纵向滚动容器：内部 `body` 用来堆控件，宽度自动跟随。"""

    def __init__(self, master, bg: str = theme.BG):
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_body(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_wheel(self, event) -> None:
        # 只在指针真的在这个滚动区里时滚它，避免多个滚动区一起动
        widget = self.winfo_containing(event.x_root, event.y_root)
        parent = widget
        while parent is not None:
            if parent is self:
                try:
                    self.canvas.yview_scroll(int(-event.delta / 120), "units")
                except tk.TclError:
                    pass
                return
            parent = getattr(parent, "master", None)

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()


class Field(tk.Frame):
    """一行"标签 + 值 + 复制按钮"，用于接驳页展示 base_url / token。"""

    def __init__(self, master, label: str, value: str = "", mono: bool = True,
                 bg: str = theme.SURFACE, on_copy=None, secret: bool = False):
        super().__init__(master, bg=bg)
        tk.Label(self, text=label, bg=bg, fg=theme.TEXT_DIM,
                 font=(theme.FONT_FAMILY, 8), anchor="w").pack(fill="x")
        row = tk.Frame(self, bg=bg)
        row.pack(fill="x", pady=(2, 0))
        font = (theme.MONO_FAMILY, 9) if mono else (theme.FONT_FAMILY, 9)
        self.entry = tk.Entry(row, bg=theme.SURFACE_2, fg=theme.TEXT, bd=0,
                              insertbackground=theme.TEXT, font=font,
                              highlightthickness=0, readonlybackground=theme.SURFACE_2)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        self.raw = value
        self.secret = secret
        self._visible = not secret
        self._render()
        self.entry.configure(state="readonly")
        self.copy_btn = ttk.Button(row, text="复制", style="Ghost.TButton", width=6,
                                   command=lambda: self._copy(on_copy))
        self.copy_btn.pack(side="left")
        if secret:
            self.eye = ttk.Button(row, text="显示", style="Ghost.TButton", width=6,
                                  command=self._toggle)
            self.eye.pack(side="left", padx=(4, 0))

    def _render(self) -> None:
        shown = self.raw if self._visible else mask(self.raw)
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        self.entry.insert(0, shown)
        self.entry.configure(state="readonly")

    def _toggle(self) -> None:
        self._visible = not self._visible
        self._render()
        if self.secret:
            self.eye.configure(text="隐藏" if self._visible else "显示")

    def _copy(self, on_copy) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.raw)
        except tk.TclError:
            pass
        if on_copy:
            on_copy(self.raw)

    def set_value(self, value: str) -> None:
        self.raw = value
        self._render()


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return value[:6] + "•" * 12 + value[-4:]


def section(parent, text: str, bg: str = theme.BG) -> tk.Label:
    label = tk.Label(parent, text=text, bg=bg, fg=theme.TEXT_DIM,
                     font=(theme.FONT_FAMILY, 8, "bold"), anchor="w")
    return label


def kv_row(parent, key: str, value: str = "", bg: str = theme.SURFACE,
           value_fg: str = theme.TEXT, mono: bool = False) -> tk.Label:
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=1)
    tk.Label(row, text=key, bg=bg, fg=theme.TEXT_MUTE,
             font=(theme.FONT_FAMILY, 8), anchor="w").pack(side="left")
    font = (theme.MONO_FAMILY, 8) if mono else (theme.FONT_FAMILY, 8)
    out = tk.Label(row, text=value, bg=bg, fg=value_fg, font=font, anchor="e")
    out.pack(side="right")
    return out
