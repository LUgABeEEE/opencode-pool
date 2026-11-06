"""系统托盘（纯 ctypes，不依赖 pystray / Pillow）。

实现方式：注册一个隐藏窗口类 → 建隐藏窗口 → `Shell_NotifyIconW` 挂图标 →
在自己的线程里跑 `GetMessageW` 循环。菜单项点击后把动作字符串塞进队列，
由主线程的 `poll()` 取走执行 —— tkinter 不是线程安全的，跨线程只能传数据。

托盘线程死掉不影响主窗口（全部做了异常兜底），最坏情况是没有托盘图标。
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_GRAYED = 0x00000001
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTALIGN = 0x0000

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512

CS_VREDRAW = 0x0001
CS_HREDRAW = 0x0002
WS_OVERLAPPED = 0x00000000

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.restype = ctypes.c_void_p
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadImageW.restype = wintypes.HANDLE


class Tray:
    """托盘图标 + 右键菜单。

    菜单项在构造时定好：(id, 文案, 动作名, 是否分隔线)。可用性/勾选状态通过
    `set_state()` 在 add 之后动态改（每次右键菜单弹出前都会调用 `_build_menu`）。
    """

    def __init__(self, tooltip: str, icon_path: str, on_action,
                 menu_items: list[tuple[int, str, str]], dynamic=None):
        self.actions: queue.Queue[str] = queue.Queue()
        self._on_action = on_action
        self._tooltip = tooltip[:127]
        self._icon_path = icon_path
        self._menu_items = menu_items
        self._dynamic = dynamic or (lambda: {})   # 返回 {action: {"checked":bool,"enabled":bool,"text":str}}
        self._hwnd = None
        self._hicon = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.ok = False
        self.error = ""
        self.menu_ids = [item[0] for item in menu_items if len(item) >= 3]

    # —— 生命周期 ——
    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()
        self._ready.wait(2.5)
        return self.ok

    def stop(self) -> None:
        try:
            if self._hwnd:
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except (AttributeError, OSError):
            pass

    def set_tooltip(self, text: str) -> None:
        self._tooltip = text[:127]
        if self._hwnd:
            self._notify(NIM_MODIFY)

    def set_icon(self, icon_path: str) -> None:
        self._icon_path = icon_path
        if self._hwnd:
            self._load_icon()
            self._notify(NIM_MODIFY)

    def poll(self) -> list[str]:
        """主线程取动作（回调里只放行，不直接操作 tk）。"""
        out = []
        while True:
            try:
                out.append(self.actions.get_nowait())
            except queue.Empty:
                break
        return out

    # —— 内部 ——
    def _run(self) -> None:
        try:
            self._create_window()
        except Exception as exc:  # noqa: BLE001 - 托盘失败不该拖死客户端
            self.error = f"{type(exc).__name__}: {exc}"
            self.ok = False
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if self._hwnd:
                self._notify(NIM_DELETE)

    def _create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        self._load_icon()
        self._proc = WNDPROC(self._wndproc)          # 必须持引用，否则被 GC 回调就崩

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hIcon = self._hicon
        wc.lpszClassName = "OpenCodePoolClientTray"
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err not in (0, 1410):                 # 1410 = 类已注册
                raise OSError(f"RegisterClassExW failed: {err}")

        self._hwnd = user32.CreateWindowExW(
            0, "OpenCodePoolClientTray", "opencode-pool tray", WS_OVERLAPPED,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not self._hwnd:
            raise OSError("CreateWindowExW failed")
        self.ok = self._notify(NIM_ADD)

    def _load_icon(self) -> None:
        handle = None
        if self._icon_path and os.path.isfile(self._icon_path):
            handle = user32.LoadImageW(None, self._icon_path, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
        self._hicon = handle or user32.LoadIconW(None, IDI_APPLICATION)

    def _notify(self, action: int) -> bool:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = self._hicon
        data.szTip = self._tooltip
        try:
            return bool(shell32.Shell_NotifyIconW(action, ctypes.byref(data)))
        except OSError:
            return False

    def _build_menu(self):
        menu = user32.CreatePopupMenu()
        state = {}
        try:
            state = self._dynamic() or {}
        except Exception:  # noqa: BLE001
            state = {}
        for item in self._menu_items:
            if len(item) == 2 and item[1] == "-":
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                continue
            menu_id, text, action = item[0], item[1], item[2]
            info = state.get(action) or {}
            label = info.get("text") or text
            flags = MF_STRING
            if info.get("checked"):
                flags |= MF_CHECKED
            if info.get("enabled") is False:
                flags |= MF_GRAYED
            user32.AppendMenuW(menu, flags, menu_id, label)
        return menu

    def _show_menu(self) -> None:
        menu = self._build_menu()
        try:
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                                  point.x, point.y, 0, self._hwnd, None)
        finally:
            user32.DestroyMenu(menu)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                low = int(lparam) & 0xFFFF
                if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.actions.put("show")
                    return 0
                if low == WM_RBUTTONUP:
                    self._show_menu()
                    return 0
            elif msg == WM_COMMAND:
                menu_id = int(wparam) & 0xFFFF
                action = self._action_for(menu_id)
                if action:
                    self.actions.put(action)
                return 0
            elif msg == WM_CLOSE:
                self._notify(NIM_DELETE)
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - 回调里绝不能让异常穿回去
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _action_for(self, menu_id: int) -> str:
        for item in self._menu_items:
            if len(item) >= 3 and item[0] == menu_id:
                return item[2]
        return ""
