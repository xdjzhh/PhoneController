"""
Card Window - Mac-style card window decoration for scrcpy windows.

Windows implementation uses a dedicated UI thread for overlay window creation/message pump,
plus a global WH_MOUSE_LL hook thread for drag/resize/hotzone detection.

Also provides apply_mac_card_style() for Windows DWM rounded corners + drop shadow.

Key design:
- UI thread: registers window class, creates/destroys overlays, runs GetMessage/DispatchMessage pump
- Hook thread: runs WH_MOUSE_LL global hook, intercepts mouse events for drag/resize/traffic lights
- All cross-thread operations use thread-safe queue + PostMessage to the UI thread
"""

import os
import sys
import threading
import time
import ctypes
from ctypes import wintypes


# ═══════════════════ DEBUG 开关 ═══════════════════
# 设为 True 开启拖拽调试日志，问题定位后改回 False
DEBUG_CARD = True


def _dlog(*args, **kwargs):
    """带时间戳和线程ID的调试日志"""
    if not DEBUG_CARD:
        return
    tid = threading.current_thread().name or str(threading.get_ident())
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
    print(f"[CARD {ts} {tid}]", *args, flush=True, **kwargs)


# ──────────────────────────── DPI Awareness ────────────────────────────
# Must be set BEFORE any window creation to ensure GetWindowRect and mouse
# hook coordinates use the same (physical pixel) coordinate system.

if os.name == 'nt':
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


# ──────────────────────────── Mac Card Style Defaults ────────────────────────────

CARD_CORNER_RADIUS = 20
CARD_BACKGROUND_COLOR = '#1a1a2e'


# ──────────────────────────── Windows Constants ────────────────────────────

if os.name == 'nt':
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    dwmapi = ctypes.windll.dwmapi
    kernel32 = ctypes.windll.kernel32

    WS_POPUP = 0x80000000
    WS_CLIPSIBLINGS = 0x04000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOPARENTNOTIFY = 0x00000004

    CS_HREDRAW = 0x0002
    CS_VREDRAW = 0x0001

    WM_DESTROY = 0x0002
    WM_TIMER = 0x0113
    WM_USER = 0x0400
    WM_TUI_CREATEOVERLAY = WM_USER + 1
    WM_TUI_DESTROYOVERLAY = WM_USER + 2
    WM_TUI_SHOWHIDE = WM_USER + 3
    WM_TUI_REDRAW = WM_USER + 4
    WM_TUI_MOVEOVERLAY = WM_USER + 5
    WM_TUI_MOVEWIN = WM_USER + 6
    WM_TUI_NCDRAG = WM_USER + 7   # 线程安全触发原生NC拖拽
    WM_TUI_QUIT = WM_USER + 99

    WH_MOUSE_LL = 14
    HC_ACTION = 0

    WM_PAINT = 0x000F
    WM_SHOWWINDOW = 0x0018
    WM_SETCURSOR = 0x0020
    WM_NCHITTEST = 0x0084

    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205

    WM_NCLBUTTONDOWN = 0x00A1
    HTCAPTION = 2
    HTLEFT = 10
    HTRIGHT = 11
    HTTOP = 12
    HTTOPLEFT = 13
    HTTOPRIGHT = 14
    HTBOTTOM = 15
    HTBOTTOMLEFT = 16
    HTBOTTOMRIGHT = 17
    SC_DRAGMOVE = 0xF012
    WM_SYSCOMMAND = 0x0112

    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SW_SHOW = 5

    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOREDRAW = 0x0008
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    SWP_NOOWNERZORDER = 0x0200

    NULL_BRUSH = 5
    BI_RGB = 0
    DIB_RGB_COLORS = 0
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01

    IDC_ARROW = 32512
    IDC_HAND = 32649
    IDC_SIZEALL = 32646
    IDC_SIZENS = 32645
    IDC_SIZEWE = 32644
    IDC_SIZENWSE = 32642
    IDC_SIZENESW = 32643

    GA_ROOT = 2
    GWL_EXSTYLE = -20

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", POINT)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND,
                                               wintypes.UINT, wintypes.WPARAM,
                                               wintypes.LPARAM)),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HMODULE), ("hIcon", wintypes.HWND),
            ("hCursor", wintypes.HWND), ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HWND)
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                    ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    LRESULT = ctypes.c_long
    LOWLEVELMOUSEPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                           wintypes.WPARAM,
                                           ctypes.POINTER(MSLLHOOKSTRUCT))


# ──────────────────────── Mac Traffic Light Colors ────────────────────────

class TrafficLightColors:
    CLOSE = (0xFF, 0x5F, 0x57)
    CLOSE_HOVER = (0xE0, 0x4A, 0x43)
    MIN = (0xFE, 0xBC, 0x2E)
    MIN_HOVER = (0xDE, 0xA1, 0x23)
    PIN = (0x28, 0xC8, 0x40)
    PIN_HOVER = (0x1F, 0xAB, 0x2F)
    GLYPH = (0xFF, 0xFF, 0xFF)


class HitZone:
    NONE = 0
    TRAFFIC_CLOSE = 1
    TRAFFIC_MIN = 2
    TRAFFIC_PIN = 3
    TITLE_BAR = 4
    EDGE_TOP = 5
    EDGE_BOTTOM = 6
    EDGE_LEFT = 7
    EDGE_RIGHT = 8
    CORNER_TL = 9
    CORNER_TR = 10
    CORNER_BL = 11
    CORNER_BR = 12


class CardState:
    def __init__(self, scrcpy_hwnd, on_close=None, on_geometry_changed=None, on_minimize=None):
        self.scrcpy_hwnd = scrcpy_hwnd
        self.overlay_hwnd = None
        # ══════════════════════════════════════════════════════════════════
        # 幽灵拖拽预览窗口：拖拽过程中 scrcpy 和 overlay 真实窗口完全不动
        # （零刷新、零内部状态变化），只有这一个半透明幽灵窗口跟着鼠标走，
        # 彻底消除 scrcpy 内部视频渲染 / SDL 回调 / DWM 合成带来的抖动源。
        # LUP 鼠标释放时才一次性把两个真实窗口移到最终位置。
        # ══════════════════════════════════════════════════════════════════
        self.drag_ghost_hwnd = None
        self.on_close = on_close
        self.on_geometry_changed = on_geometry_changed
        self.on_minimize = on_minimize

        self.btn_size = 12
        self.btn_spacing = 8
        self.btn_offset_x = 12
        self.btn_offset_y = 10
        # overlay 窗口大小 = scrcpy 窗口大小（全窗口覆盖），在创建/同步 overlay 时动态赋值
        self.overlay_width = 0
        self.overlay_height = 0

        self.title_bar_height = 32
        self.edge_size = 6
        self.corner_size = 12

        self.editing = False
        self.hover_button = -1
        # 旧的自实现拖拽标记（保留字段兼容，但不再使用核心路径）
        self.dragging = False
        self.resizing = False
        self.resize_zone = HitZone.NONE
        self.drag_start_x = 0
        self.drag_start_y = 0
        self._drag_start_injected = False
        self.win_start_x = 0
        self.win_start_y = 0
        self.win_start_w = 0
        self.win_start_h = 0
        self._move_pending = False
        self._target_x = 0
        self._target_y = 0
        self._target_w = 0
        self._target_h = 0
        # ═══ 原生 NC 拖拽（新核心路径） ═══
        self._nc_drag_active = False   # 原生模态拖拽/缩放是否进行中
        self._nc_drag_zone = HitZone.NONE  # HTCAPTION / HTxxx

    def get_traffic_button_rect(self, index):
        x = self.btn_offset_x + index * (self.btn_size + self.btn_spacing)
        y = self.btn_offset_y
        return x, y, self.btn_size, self.btn_size

    def hit_test(self, x, y, win_w, win_h):
        if self.editing:
            for i in range(3):
                bx, by, bw, bh = self.get_traffic_button_rect(i)
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    return [HitZone.TRAFFIC_CLOSE, HitZone.TRAFFIC_MIN, HitZone.TRAFFIC_PIN][i]
            if y < self.title_bar_height:
                return HitZone.TITLE_BAR
            es = self.edge_size
            cs = self.corner_size
            if x < cs and y < cs:
                return HitZone.CORNER_TL
            if x > win_w - cs and y < cs:
                return HitZone.CORNER_TR
            if x < cs and y > win_h - cs:
                return HitZone.CORNER_BL
            if x > win_w - cs and y > win_h - cs:
                return HitZone.CORNER_BR
            if y < es:
                return HitZone.EDGE_TOP
            if y > win_h - es:
                return HitZone.EDGE_BOTTOM
            if x < es:
                return HitZone.EDGE_LEFT
            if x > win_w - es:
                return HitZone.EDGE_RIGHT
        return HitZone.NONE


# ──────────────────────── DWM Card Style (rounded corners + shadow) ────────────────────────

def apply_mac_card_style(hwnd, window_title=""):
    """Apply Mac-style card look on Windows: DWM rounded corners + drop shadow.
    Uses Windows 11 DWM API natively with SetWindowRgn fallback for older versions.
    """
    if os.name != 'nt':
        return

    # --- 1. DWM rounded corners (Windows 11) ---
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2
    try:
        corner_pref = ctypes.c_int(DWMWCP_ROUND)
        hr = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(corner_pref),
            ctypes.sizeof(corner_pref)
        )
        if hr == 0:
            print(f"[{window_title}] DWM rounded corners applied (DWMWCP_ROUND)", flush=True)
        else:
            _apply_rounded_region(hwnd, window_title)
    except Exception as e:
        print(f"[{window_title}] DWM rounded corners not available: {e}", flush=True)
        _apply_rounded_region(hwnd, window_title)

    # --- 2. Drop shadow via DwmExtendFrameIntoClientArea ---
    try:
        class MARGINS(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int),
                        ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int),
                        ("cyBottomHeight", ctypes.c_int)]
        margins = MARGINS(1, 1, 1, 1)
        dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
        print(f"[{window_title}] Drop shadow enabled via DwmExtendFrameIntoClientArea", flush=True)
    except Exception as e:
        print(f"[{window_title}] Shadow setup error: {e}", flush=True)

    # --- 3. CS_DROPSHADOW class style for Win10 ---
    try:
        GCL_STYLE = -26
        CS_DROPSHADOW = 0x00020000
        class_style = user32.GetClassLongW(wintypes.HWND(hwnd), GCL_STYLE)
        user32.SetClassLongW(wintypes.HWND(hwnd), GCL_STYLE, class_style | CS_DROPSHADOW)
    except Exception:
        pass

    # --- 4. Force repaint ---
    try:
        user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0004 | 0x0020)
    except Exception:
        pass


def _apply_rounded_region(hwnd, window_title=""):
    """Fallback: use SetWindowRgn to create rounded corners on older Windows."""
    radius = CARD_CORNER_RADIUS
    rect = RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
    if rgn:
        user32.SetWindowRgn(wintypes.HWND(hwnd), rgn, True)
        print(f"[{window_title}] Rounded region applied (radius={radius}) via SetWindowRgn", flush=True)


# ──────────────────────── Windows Card Decorator ────────────────────────

class WindowsCardDecorator:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._cards = {}
        self._card_ptrs = {}
        self._ui_hwnd = None
        self._ui_thread = None
        self._ui_thread_id = None
        self._hook_thread = None
        self._hook_handle = None
        self._hook_proc = None
        self._wnd_proc_ref = None
        self._msg_hwnd = None
        self._running = True
        self._active_card = None
        self._start_event = threading.Event()
        self._ui_ready = False

        self._ui_thread = threading.Thread(target=self._ui_thread_main, daemon=True)
        self._ui_thread.start()
        self._start_event.wait(timeout=5)

        self._start_hook()

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = WindowsCardDecorator()
            return cls._instance

    # ──────────── UI Thread ────────────

    def _ui_thread_main(self):
        self._ui_thread_id = kernel32.GetCurrentThreadId()
        inst = kernel32.GetModuleHandleW(None)

        wnd_proc = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)(
            self._overlay_wnd_proc
        )
        self._wnd_proc_ref = wnd_proc

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = wnd_proc
        wc.hInstance = inst
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        wc.hbrBackground = gdi32.GetStockObject(NULL_BRUSH)
        wc.lpszClassName = "ScrcpyCardOverlay"
        wc.hIcon = None
        wc.hIconSm = None
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != 1410:
                print(f"[UI] RegisterClassEx failed: {err}", flush=True)

        wc_msg = WNDCLASSEXW()
        wc_msg.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc_msg.lpfnWndProc = wnd_proc
        wc_msg.hInstance = inst
        wc_msg.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        wc_msg.hbrBackground = gdi32.GetStockObject(NULL_BRUSH)
        wc_msg.lpszClassName = "ScrcpyCardMsgWnd"
        user32.RegisterClassExW(ctypes.byref(wc_msg))

        self._msg_hwnd = user32.CreateWindowExW(
            0, "ScrcpyCardMsgWnd", None, 0, 0, 0, 0, 0,
            wintypes.HWND(-3), None, inst, None
        )

        self._ui_ready = True
        self._start_event.set()
        print(f"[UI] UI thread ready (tid={self._ui_thread_id}, msg_hwnd={self._msg_hwnd})", flush=True)

        msg = MSG()
        while True:
            bRet = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if bRet > 0:
                if msg.message == WM_TUI_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            elif bRet == 0:
                break
            else:
                break

        for hwnd in list(self._cards.keys()):
            self._destroy_overlay(self._cards[hwnd])
        if self._msg_hwnd:
            user32.DestroyWindow(self._msg_hwnd)
        print("[UI] UI thread exited", flush=True)

    def _post_ui(self, msg, wparam=0, card=None):
        if self._msg_hwnd:
            lparam = id(card) if card is not None else 0
            _msg_names = {
                WM_TUI_CREATEOVERLAY: "CREATE",
                WM_TUI_DESTROYOVERLAY: "DESTROY",
                WM_TUI_SHOWHIDE: f"SHOWHIDE({bool(wparam)})",
                WM_TUI_REDRAW: "REDRAW",
                WM_TUI_MOVEOVERLAY: "MOVEOVERLAY",
                WM_TUI_MOVEWIN: "MOVEWIN",
                WM_TUI_QUIT: "QUIT",
            }
            _mn = _msg_names.get(int(msg), f"0x{int(msg):04X}")
            ret = user32.PostMessageW(wintypes.HWND(self._msg_hwnd), wintypes.UINT(msg),
                                wintypes.WPARAM(wparam), wintypes.LPARAM(lparam))
            if msg in (WM_TUI_MOVEWIN, WM_TUI_MOVEOVERLAY):
                _dlog(f"PostMessage {_mn} hwnd={self._msg_hwnd} ret={ret} err={ctypes.get_last_error() if not ret else 0} lparam_card={card is not None}")

    def _card_from_lparam(self, lparam):
        return self._card_ptrs.get(int(lparam))

    def _find_card_by_overlay_hwnd(self, hwnd):
        """根据 overlay 窗口句柄反向查找对应的 CardState"""
        ih = int(hwnd)
        for card in self._cards.values():
            if card.overlay_hwnd and int(card.overlay_hwnd) == ih:
                return card
        return None

    # ──────────── 幽灵拖拽预览窗口（抖动根除方案） ────────────

    def _create_drag_ghost(self, card, x_screen, y_screen, w, h):
        """
        创建『幽灵拖拽预览窗口』：
        一个和 scrcpy 同样大小的半透明（alpha~180）layered 窗口，
        带 1px 边框。拖拽过程中我们**完全不动 scrcpy 和 overlay 真实窗口**，
        只移动这一个幽灵窗口，彻底消除 scrcpy 内部 SDL/DWM 合成抖动源。
        鼠标释放后销毁幽灵窗口 + 一次到位移真实窗口。
        """
        inst = kernel32.GetModuleHandleW(None)
        # WS_EX_LAYERED + WS_EX_TOOLWINDOW（任务栏不显示、不在 Alt+Tab 里）
        ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_NOPARENTNOTIFY
        style = WS_POPUP
        # 注意：复用已注册的 "ScrcpyCardOverlay" 窗口类（纯分层窗口类），
        # 不需要再单独注册一个 Ghost 类名，保证 CreateWindowExW 一定成功。
        hwnd = user32.CreateWindowExW(
            ex_style, "ScrcpyCardOverlay", None, style,
            x_screen, y_screen, w, h,
            None, None, inst, None
        )
        if not hwnd:
            return None
        card.drag_ghost_hwnd = hwnd
        # 画一张半透明底色 + 外边框的 DIB 位图
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        ppv_bits = ctypes.c_void_p()
        hdc_screen = user32.GetDC(0)
        hbm = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi),
                                     DIB_RGB_COLORS, ctypes.byref(ppv_bits), None, 0)
        user32.ReleaseDC(0, hdc_screen)
        if not hbm or not ppv_bits:
            return None
        try:
            buf_size = w * h * 4
            ptr = (ctypes.c_ubyte * buf_size).from_address(ppv_bits.value)
            # 半透明淡紫色（BGR）底色，视觉上很轻但清晰可辨：#E8E4F0 → B=0xF0 G=0xE4 R=0xE8
            # alpha = 180（~70% 透明）
            B, G, R, A = 0xF0, 0xE4, 0xE8, 180
            for i in range(0, buf_size, 4):
                ptr[i]     = B
                ptr[i + 1] = G
                ptr[i + 2] = R
                ptr[i + 3] = A
            # 外边框（1px，深蓝色）：#2D54C5 → B=0xC5 G=0x54 R=0x2D, alpha=255
            BB, BG, BR, BA = 0xC5, 0x54, 0x2D, 255
            # 上边和下边
            for x_col in range(w):
                # 上边框 (y=0)
                o = x_col * 4
                ptr[o] = BB; ptr[o + 1] = BG; ptr[o + 2] = BR; ptr[o + 3] = BA
                # 下边框 (y=h-1)
                o = ((h - 1) * w + x_col) * 4
                ptr[o] = BB; ptr[o + 1] = BG; ptr[o + 2] = BR; ptr[o + 3] = BA
            # 左边和右边
            for y_row in range(h):
                # 左边框 (x=0)
                o = (y_row * w + 0) * 4
                ptr[o] = BB; ptr[o + 1] = BG; ptr[o + 2] = BR; ptr[o + 3] = BA
                # 右边框 (x=w-1)
                o = (y_row * w + (w - 1)) * 4
                ptr[o] = BB; ptr[o + 1] = BG; ptr[o + 2] = BR; ptr[o + 3] = BA
            # UpdateLayeredWindow 贴到幽灵窗口
            try:
                hdc_screen = user32.GetDC(0)
                hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
                old_bmp = gdi32.SelectObject(hdc_mem, hbm)
                pt_dst = POINT(x_screen, y_screen)
                size = POINT(w, h)
                pt_src = POINT(0, 0)
                blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
                user32.UpdateLayeredWindow(
                    wintypes.HWND(hwnd), hdc_screen,
                    ctypes.byref(pt_dst), ctypes.byref(size),
                    hdc_mem, ctypes.byref(pt_src),
                    0, ctypes.byref(blend), ULW_ALPHA
                )
                gdi32.SelectObject(hdc_mem, old_bmp)
                gdi32.DeleteDC(hdc_mem)
                user32.ReleaseDC(0, hdc_screen)
            finally:
                gdi32.DeleteObject(hbm)
        except Exception:
            import traceback
            traceback.print_exc()
        # 显示并置顶
        user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
        user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return hwnd

    def _move_drag_ghost(self, card, nx, ny, nw, nh):
        """移动幽灵预览窗口（极轻量，只是改位置，和 scrcpy 视频渲染完全无关）"""
        if not card.drag_ghost_hwnd:
            return
        user32.SetWindowPos(wintypes.HWND(card.drag_ghost_hwnd), wintypes.HWND(0),
                            nx, ny, nw, nh,
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW)

    def _destroy_drag_ghost(self, card):
        """销毁幽灵预览窗口（拖拽结束）"""
        if card.drag_ghost_hwnd:
            user32.DestroyWindow(wintypes.HWND(card.drag_ghost_hwnd))
            card.drag_ghost_hwnd = None

    # ──────────── Win32 API 声明补充 ────────────

    # 补充 DeferWindowPos 相关常量和函数（用于一次原子移动多个窗口，消除抖动）
    HDWP = ctypes.c_void_p
    try:
        user32.BeginDeferWindowPos.restype = HDWP
        user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
        user32.DeferWindowPos.restype = HDWP
        user32.DeferWindowPos.argtypes = [HDWP, wintypes.HWND, wintypes.HWND,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.EndDeferWindowPos.restype = wintypes.BOOL
        user32.EndDeferWindowPos.argtypes = [HDWP]
    except Exception:
        pass  # 某些 ctypes 版本不会抛错，提前声明防意外

    # ──────────── 辅助：原子性同时移动 scrcpy + overlay（消除一帧错位抖动） ────
    def _atomic_move_two_windows(self,
                                 hwnd1: int, x1: int, y1: int, w1: int, h1: int,
                                 hwnd2: int, x2: int, y2: int, w2: int, h2: int,
                                 repaint=False):
        """
        用 BeginDeferWindowPos/DeferWindowPos/EndDeferWindowPos 一次提交两个窗口的
        位置/大小变化。Windows 在同一个重绘周期内完成两个窗口的更新，不会出现
        "第一个窗口已经移动到位、第二个窗口下一帧才跟上"的一帧错位抖动。
        这是拖拽时 scrcpy 视频画面 + overlay 相对位置"晃动感"的根因修复。
        """
        SWP_FLAGS = SWP_NOZORDER | SWP_NOACTIVATE
        if not repaint:
            SWP_FLAGS |= SWP_NOREDRAW
        # 申请一个能放 2 个窗口变化的 HDWP
        hdwp = user32.BeginDeferWindowPos(2)
        if not hdwp:
            # 退化为普通 MoveWindow（极少数内存不足场景）
            user32.MoveWindow(wintypes.HWND(hwnd1), x1, y1, w1, h1, repaint)
            user32.MoveWindow(wintypes.HWND(hwnd2), x2, y2, w2, h2, repaint)
            return
        # 窗口 1：scrcpy
        hdwp = user32.DeferWindowPos(hdwp, wintypes.HWND(hwnd1), wintypes.HWND(0),
                                     x1, y1, w1, h1, SWP_FLAGS)
        # 窗口 2：overlay（只有 hdwp 没失败才继续）
        if hdwp:
            hdwp = user32.DeferWindowPos(hdwp, wintypes.HWND(hwnd2), wintypes.HWND(0),
                                         x2, y2, w2, h2, SWP_FLAGS)
        if hdwp:
            # 一次性原子提交两个窗口的位置变化（同帧刷新）
            user32.EndDeferWindowPos(hdwp)
        else:
            # 中间任何一步失败，退化到普通 MoveWindow
            user32.MoveWindow(wintypes.HWND(hwnd1), x1, y1, w1, h1, repaint)
            user32.MoveWindow(wintypes.HWND(hwnd2), x2, y2, w2, h2, repaint)

    # ──────────── Overlay window creation ────────────

    def _ensure_overlay_above_scrcpy(self, card):
        """确保 overlay 永远盖在 scrcpy 最上方（分层窗口命中测试正确的前提）"""
        if not card.overlay_hwnd or not card.scrcpy_hwnd:
            return
        # Step 1: scrcpy → TOPMOST
        user32.SetWindowPos(wintypes.HWND(card.scrcpy_hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # Step 2: overlay → TOPMOST（后调用的 TOPMOST 在前一个 TOPMOST 窗口之上）
        user32.SetWindowPos(wintypes.HWND(card.overlay_hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # Step 3: 显式把 overlay 插入 scrcpy 正上方（最关键，z-order 精确控制，防止 scrcpy 内部重新置顶）
        user32.SetWindowPos(wintypes.HWND(card.overlay_hwnd), wintypes.HWND(card.scrcpy_hwnd),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # Step 4: 再保险 —— 重新 TOPMOST
        user32.SetWindowPos(wintypes.HWND(card.overlay_hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _create_overlay_for_card(self, card):
        inst = kernel32.GetModuleHandleW(None)
        sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)

        # ═══════ overlay 覆盖整个 scrcpy 窗口（不再只是按钮区！） ═══════
        card.overlay_width = sw
        card.overlay_height = sh

        ex_style = (WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE |
                    WS_EX_TOPMOST | WS_EX_NOPARENTNOTIFY | WS_EX_TRANSPARENT)
        style = WS_POPUP | WS_CLIPSIBLINGS

        hwnd = user32.CreateWindowExW(
            ex_style, "ScrcpyCardOverlay", None, style,
            sx, sy, card.overlay_width, card.overlay_height,
            wintypes.HWND(card.scrcpy_hwnd), None, inst, None
        )
        if not hwnd:
            err = ctypes.get_last_error()
            print(f"[UI] CreateWindowEx failed for overlay: {err}", flush=True)
            return None
        card.overlay_hwnd = hwnd
        print(f"[UI] Overlay created: hwnd={hwnd} for scrcpy={card.scrcpy_hwnd} size=({sw}x{sh})", flush=True)

        self._redraw_overlay_on_ui(card)

        # ═══════ z-order 铁律：先 scrcpy TOPMOST，再 overlay TOPMOST，永远确保 overlay 盖在 scrcpy 之上 ═══════
        self._ensure_overlay_above_scrcpy(card)

        if card.editing:
            self._show_overlay_on_ui(card)
        return hwnd

    def _destroy_overlay(self, card):
        if card.overlay_hwnd:
            user32.DestroyWindow(wintypes.HWND(card.overlay_hwnd))
            card.overlay_hwnd = None

    def _show_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        # 编辑模式：去掉 WS_EX_TRANSPARENT，overlay 自己接收所有鼠标消息（全窗口覆盖）
        ex = user32.GetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE)
        ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE, ex)
        user32.ShowWindow(wintypes.HWND(card.overlay_hwnd), SW_SHOWNOACTIVATE)
        # 确保 overlay 在 scrcpy 之上拦截点击
        self._ensure_overlay_above_scrcpy(card)

    def _hide_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        user32.ShowWindow(wintypes.HWND(card.overlay_hwnd), SW_HIDE)
        # 非编辑模式：恢复 WS_EX_TRANSPARENT，鼠标穿透给 scrcpy 直接操作 Android
        ex = user32.GetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE)
        ex |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE, ex)

    def _sync_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
        card.overlay_width = sw
        card.overlay_height = sh
        # overlay 永远和 scrcpy 同样大小，保持覆盖对齐
        user32.MoveWindow(wintypes.HWND(card.overlay_hwnd), sx, sy, sw, sh, True)
        self._ensure_overlay_above_scrcpy(card)

    def _move_window_on_ui(self, card):
        card._move_pending = False
        tx, ty, tw, th = card._target_x, card._target_y, card._target_w, card._target_h
        user32.MoveWindow(wintypes.HWND(card.scrcpy_hwnd), tx, ty, tw, th, True)
        if card.overlay_hwnd:
            sx2, sy2, sw2, sh2 = self._window_rect(card.scrcpy_hwnd)
            card.overlay_width = sw2
            card.overlay_height = sh2
            user32.MoveWindow(wintypes.HWND(card.overlay_hwnd), sx2, sy2, sw2, sh2, True)
            self._ensure_overlay_above_scrcpy(card)

    def _sync_overlay_now(self, card):
        if not card.overlay_hwnd:
            return
        sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
        card.overlay_width = sw
        card.overlay_height = sh
        user32.MoveWindow(wintypes.HWND(card.overlay_hwnd), sx, sy, sw, sh, True)
        self._ensure_overlay_above_scrcpy(card)

    def _hit_zone_to_htcode(self, zone):
        """把我们的 HitZone 枚举映射为 Win32 HTxxx 常量"""
        return {
            HitZone.TITLE_BAR: HTCAPTION,
            HitZone.EDGE_TOP: HTTOP,
            HitZone.EDGE_BOTTOM: HTBOTTOM,
            HitZone.EDGE_LEFT: HTLEFT,
            HitZone.EDGE_RIGHT: HTRIGHT,
            HitZone.CORNER_TL: HTTOPLEFT,
            HitZone.CORNER_TR: HTTOPRIGHT,
            HitZone.CORNER_BL: HTBOTTOMLEFT,
            HitZone.CORNER_BR: HTBOTTOMRIGHT,
        }.get(int(zone), 0)  # 0=HTNOWHERE

    def _start_nc_drag(self, card):
        """UI 线程调用：通过模拟 WM_NCLBUTTONDOWN 触发 Windows 原生标题栏拖拽/边缘缩放模态循环"""
        if not card.scrcpy_hwnd:
            _dlog(f"[_start_nc_drag] ABORT scrcpy_hwnd is None")
            return
        ht = self._hit_zone_to_htcode(card._nc_drag_zone)
        if ht == 0:
            _dlog(f"[_start_nc_drag] ABORT zone={card._nc_drag_zone} maps to HTNOWHERE")
            return
        # 当前鼠标屏幕坐标 -> scrcpy 客户区坐标
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mx, my = pt.x, pt.y
        cx, cy = mx, my
        sx, sy, _, _ = self._window_rect(card.scrcpy_hwnd)
        cx, cy = mx - sx, my - sy
        # MAKELPARAM: (y << 16) | (x & 0xFFFF)
        lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        # 释放鼠标捕获（某些子控件可能持有捕获），这是触发模态 NC 拖拽的必要前置
        user32.ReleaseCapture()
        # 置顶（原自实现逻辑也会做）
        user32.SetWindowPos(wintypes.HWND(card.scrcpy_hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # ══════ 关键：给 scrcpy 窗口发送非客户区左键按下 ══════
        # 用 PostMessage 不阻塞当前 UI 线程；系统收到后进入原生模态拖拽循环，
        # 内部自动处理所有 WM_MOUSEMOVE + WM_LBUTTONUP，光标正常更新，无任何震颤
        _dlog(f"[_start_nc_drag] Send WM_NCLBUTTONDOWN scrcpy={card.scrcpy_hwnd} ht={ht} pt_client=({cx},{cy}) screen=({mx},{my})")
        user32.PostMessageW(wintypes.HWND(card.scrcpy_hwnd),
                            wintypes.UINT(WM_NCLBUTTONDOWN),
                            wintypes.WPARAM(ht),
                            wintypes.LPARAM(lp))

    def _redraw_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        w, h = card.overlay_width, card.overlay_height
        if w <= 0 or h <= 0:
            return
        bmp = self._draw_traffic_bitmap(card, w, h)
        if not bmp:
            return
        try:
            hdc_screen = user32.GetDC(0)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            old_bmp = gdi32.SelectObject(hdc_mem, bmp)

            sxx, syy, _, _ = self._window_rect(card.scrcpy_hwnd)
            pt_dst = POINT(sxx, syy)
            size = POINT(w, h)
            pt_src = POINT(0, 0)
            alpha = 255 if card.editing else 0
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)

            user32.UpdateLayeredWindow(
                wintypes.HWND(card.overlay_hwnd), hdc_screen,
                ctypes.byref(pt_dst), ctypes.byref(size),
                hdc_mem, ctypes.byref(pt_src),
                0, ctypes.byref(blend), ULW_ALPHA
            )
            gdi32.SelectObject(hdc_mem, old_bmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(0, hdc_screen)
        finally:
            gdi32.DeleteObject(bmp)

    def _draw_traffic_bitmap(self, card, w, h):
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        ppv_bits = ctypes.c_void_p()
        hdc_screen = user32.GetDC(0)
        hbm = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi),
                                     DIB_RGB_COLORS, ctypes.byref(ppv_bits), None, 0)
        user32.ReleaseDC(0, hdc_screen)
        if not hbm or not ppv_bits:
            return None

        buf_size = w * h * 4
        ptr = (ctypes.c_ubyte * buf_size).from_address(ppv_bits.value)

        # ═══════════════════════════════════════════════════════════════════
        # 核心修复：WS_EX_LAYERED + ULW_ALPHA 分层窗口命中测试规则
        #   每个像素 alpha=0 → 完全透明 → WindowFromPoint 自动跳过此窗口穿透命中下层
        #   每个像素 alpha≥1 → 视觉几乎全透 → WindowFromPoint 仍会命中此窗口
        # 之前 memset(0) 让整张图 alpha=0 → 99% 区域永远命中 scrcpy，这是
        # 物理鼠标点击永远打到 scrcpy 上的真正根源（软件注入事件跳过真实命中测试链路，所以UU远程没事）。
        # 现在改为整张背景 alpha=1（BGR 全 0 + alpha=1 → 肉眼几乎看不见的全透黑色），
        # 命中测试 100% 会返回 overlay 窗口而不是穿透。
        # ═══════════════════════════════════════════════════════════════════
        ctypes.memset(ppv_bits, 0, buf_size)
        # 每隔 4 字节（alpha 通道位置）填 1：字节索引 3, 7, 11, 15, ...
        # 使用切片赋值（C 级循环）比 Python for 循环快 10~100 倍
        ptr[3::4] = b'\x01' * (w * h)

        alpha = 255 if card.editing else 0
        for i in range(3):
            bx, by, bw, bh = card.get_traffic_button_rect(i)
            cx = bx + bw // 2
            cy = by + bh // 2
            r = bw // 2 + 1
            if i == 0:
                base = TrafficLightColors.CLOSE
                hover_c = TrafficLightColors.CLOSE_HOVER
            elif i == 1:
                base = TrafficLightColors.MIN
                hover_c = TrafficLightColors.MIN_HOVER
            else:
                base = TrafficLightColors.PIN
                hover_c = TrafficLightColors.PIN_HOVER
            is_hover = (card.hover_button == i and card.editing)
            color = hover_c if is_hover else base
            self._draw_circle(ptr, w, h, cx, cy, r, color, alpha if card.editing else 0)
            if is_hover and card.editing:
                if i == 0:
                    self._draw_x(ptr, w, h, cx, cy, int(bw * 0.4))
                elif i == 1:
                    self._draw_dash(ptr, w, h, cx, cy, int(bw * 0.4))
                else:
                    self._draw_check(ptr, w, h, cx, cy, int(bw * 0.35))
        return hbm

    def _draw_circle(self, buf, w, h, cx, cy, r, color, alpha):
        if alpha == 0:
            return
        br, bg, bb = color
        for py in range(max(0, cy - r), min(h, cy + r + 1)):
            for px in range(max(0, cx - r), min(w, cx + r + 1)):
                dx = px - cx
                dy = py - cy
                if dx * dx + dy * dy <= r * r:
                    idx = (py * w + px) * 4
                    buf[idx] = bb
                    buf[idx + 1] = bg
                    buf[idx + 2] = br
                    buf[idx + 3] = alpha

    def _draw_x(self, buf, w, h, cx, cy, half):
        for i in range(-half, half + 1):
            for dx, dy in [(i, i), (i, -i)]:
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h:
                    idx = (y * w + x) * 4
                    buf[idx] = 0xFF; buf[idx + 1] = 0xFF; buf[idx + 2] = 0xFF; buf[idx + 3] = 255

    def _draw_dash(self, buf, w, h, cx, cy, half):
        for x in range(cx - half, cx + half + 1):
            if 0 <= x < w and 0 <= cy < h:
                idx = (cy * w + x) * 4
                buf[idx] = 0xFF; buf[idx + 1] = 0xFF; buf[idx + 2] = 0xFF; buf[idx + 3] = 255

    def _draw_check(self, buf, w, h, cx, cy, half):
        r = max(1, half // 2)
        for py in range(cy - r, cy + r + 1):
            for px in range(cx - r, cx + r + 1):
                if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                    if 0 <= px < w and 0 <= py < h:
                        idx = (py * w + px) * 4
                        buf[idx] = 0xFF; buf[idx + 1] = 0xFF; buf[idx + 2] = 0xFF; buf[idx + 3] = 255

    # ──────────── WndProc ────────────

    def _overlay_wnd_proc(self, hwnd, msg, wParam, lParam):
        hwnd_c = wintypes.HWND(hwnd)
        msg_val = int(msg)

        if self._msg_hwnd and int(hwnd) == int(self._msg_hwnd):
            if msg_val == WM_TUI_CREATEOVERLAY:
                card = self._card_from_lparam(lParam)
                if card:
                    self._create_overlay_for_card(card)
                return 0
            elif msg_val == WM_TUI_DESTROYOVERLAY:
                card = self._card_from_lparam(lParam)
                if card:
                    self._destroy_overlay(card)
                return 0
            elif msg_val == WM_TUI_SHOWHIDE:
                card = self._card_from_lparam(lParam)
                if card:
                    show = bool(wParam)
                    if show:
                        self._show_overlay_on_ui(card)
                    else:
                        self._hide_overlay_on_ui(card)
                    self._redraw_overlay_on_ui(card)
                return 0
            elif msg_val == WM_TUI_REDRAW:
                card = self._card_from_lparam(lParam)
                if card:
                    self._redraw_overlay_on_ui(card)
                return 0
            elif msg_val == WM_TUI_MOVEOVERLAY:
                card = self._card_from_lparam(lParam)
                _dlog(f"[UI WndProc] GOT WM_TUI_MOVEOVERLAY, card found={card is not None}")
                if card:
                    self._sync_overlay_on_ui(card)
                return 0
            elif msg_val == WM_TUI_MOVEWIN:
                card = self._card_from_lparam(lParam)
                _dlog(f"[UI WndProc] GOT WM_TUI_MOVEWIN, card found={card is not None}, pending={card._move_pending if card else 'N/A'}")
                if card:
                    self._move_window_on_ui(card)
                return 0
            elif msg_val == WM_TUI_NCDRAG:
                card = self._card_from_lparam(lParam)
                _dlog(f"[UI WndProc] GOT WM_TUI_NCDRAG, card found={card is not None}, zone={card._nc_drag_zone if card else 'N/A'}")
                if card:
                    self._start_nc_drag(card)
                return 0
            elif msg_val == WM_TUI_QUIT:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd_c, wintypes.UINT(msg_val),
                                        wintypes.WPARAM(wParam), wintypes.LPARAM(lParam))

        # ═══════════════════════════════════════════════════════════════════
        # Overlay 自身窗口消息处理（编辑模式下全窗口覆盖，接收所有鼠标消息）
        # 核心优势：同线程同窗口过程处理，**完全不依赖低层钩子吞噬硬件事件**，
        #          彻底避开 WH_MOUSE_LL return 1 吞噬 INJECTED=False 硬件 MOVE
        #          被 Windows 防窃取机制"钳制在起点附近"的问题！
        # ═══════════════════════════════════════════════════════════════════
        card = self._find_card_by_overlay_hwnd(hwnd)
        if card is not None:
            # 客户区坐标（lParam 格式：LOWORD=x, HIWORD=y，有符号短整型）
            x = int(wintypes.SHORT(lParam & 0xFFFF).value)
            y = int(wintypes.SHORT((lParam >> 16) & 0xFFFF).value)

            if msg_val == WM_SETCURSOR:
                # ══════════════════════════════════════════════════════════════
                # 真正根因：WM_SETCURSOR 的 lParam 格式是
                #     LOWORD(lParam)  = HTCODE（命中码，不是客户区x！）
                #     HIWORD(lParam)  = 鼠标消息ID（比如 WM_MOUSEMOVE=0x200）
                # 之前错误地直接把 lParam 解成 x=HTCODE 传给 hit_test，
                # 导致 HTCODE=HTCLIENT(1)/HTTOPLEFT(13) 等被当成 x，
                # 随机命中边缘缩放区 → 指针经常变"斜拽形"。
                # 修复：用 GetCursorPos() 取真实屏幕坐标再转客户区坐标。
                # ══════════════════════════════════════════════════════════════
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                cx, cy = pt.x, pt.y
                # 转客户区坐标（ScreenToClient）
                pt2 = wintypes.POINT(cx, cy)
                user32.ScreenToClient(hwnd_c, ctypes.byref(pt2))
                hit = card.hit_test(pt2.x, pt2.y, card.overlay_width, card.overlay_height)
                self._set_cursor_for_zone(hit)
                return 1  # 已处理，不再 DefWindowProc

            if not card.editing:
                # 非编辑模式下 overlay 是穿透的，不处理消息
                return user32.DefWindowProcW(hwnd_c, wintypes.UINT(msg_val),
                                            wintypes.WPARAM(wParam), wintypes.LPARAM(lParam))

            if msg_val == WM_LBUTTONDOWN:
                zone = card.hit_test(x, y, card.overlay_width, card.overlay_height)
                user32.SetCapture(hwnd_c)  # 关键：SetCapture 后续消息即使移出窗口也能收到

                if zone in (HitZone.TRAFFIC_CLOSE, HitZone.TRAFFIC_MIN, HitZone.TRAFFIC_PIN):
                    if zone == HitZone.TRAFFIC_CLOSE:
                        self._on_close_clicked(card)
                    elif zone == HitZone.TRAFFIC_MIN:
                        self._on_min_clicked(card)
                    elif zone == HitZone.TRAFFIC_PIN:
                        self._on_pin_clicked(card)
                    user32.ReleaseCapture()
                    return 0

                if zone == HitZone.TITLE_BAR or zone in (HitZone.EDGE_TOP, HitZone.EDGE_BOTTOM,
                        HitZone.EDGE_LEFT, HitZone.EDGE_RIGHT,
                        HitZone.CORNER_TL, HitZone.CORNER_TR, HitZone.CORNER_BL, HitZone.CORNER_BR):
                    card.dragging = (zone == HitZone.TITLE_BAR)
                    card.resizing = not card.dragging
                    card.resize_zone = zone if card.resizing else HitZone.NONE
                    card.drag_start_x, card.drag_start_y = x, y  # 客户区坐标
                    # 保存 scrcpy 起始位置/大小
                    sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
                    card.win_start_x, card.win_start_y = sx, sy
                    card.win_start_w, card.win_start_h = sw, sh
                    # ═══════════════════════════════════════════════════════════
                    # 幽灵拖拽方案：DRAG 时立即创建一个半透明幽灵窗口，
                    # 拖拽过程中 scrcpy + overlay 两个真实窗口完全不动，
                    # 只有这一个幽灵预览窗口跟着鼠标走——完全像一张图片。
                    # 彻底消除 scrcpy 视频帧 / SDL 内部回调 / DWM 合成的抖动源。
                    # RESIZE 保留之前的实时刷新（缩放过程必须视觉可见）。
                    # ═══════════════════════════════════════════════════════════
                    if card.dragging:
                        self._create_drag_ghost(card, sx, sy, sw, sh)
                    return 0
                user32.ReleaseCapture()
                return user32.DefWindowProcW(hwnd_c, wintypes.UINT(msg_val),
                                            wintypes.WPARAM(wParam), wintypes.LPARAM(lParam))

            if msg_val == WM_MOUSEMOVE:
                # hover 重绘 + 拖拽/缩放移动
                if card.dragging:
                    # ═══════════════════════════════════════════════════════════
                    # DRAG：只移动『幽灵预览窗口』这一个句柄（一张图片），
                    # scrcpy + overlay 两个真实窗口**完全原地不动**，
                    # 零刷新、零内部状态变化、零 SDL/DWM 合成干扰 → 完全不抖！
                    # LUP 释放时再销毁幽灵窗口，真实窗口一次到位。
                    # ═══════════════════════════════════════════════════════════
                    dx = x - card.drag_start_x
                    dy = y - card.drag_start_y
                    nx = card.win_start_x + dx
                    ny = card.win_start_y + dy
                    nw, nh = card.win_start_w, card.win_start_h
                    # 只有一次 SetWindowPos（纯 DWM 位图平移，不触发任何窗口内部逻辑）
                    self._move_drag_ghost(card, nx, ny, nw, nh)
                    return 0
                elif card.resizing:
                    # RESIZE 保留之前的实时刷新（缩放过程必须视觉可见）
                    dx = x - card.drag_start_x
                    dy = y - card.drag_start_y
                    nx, ny = card.win_start_x, card.win_start_y
                    nw, nh = card.win_start_w, card.win_start_h
                    z = card.resize_zone
                    if z in (HitZone.EDGE_RIGHT, HitZone.CORNER_TR, HitZone.CORNER_BR):
                        nw = max(200, card.win_start_w + dx)
                    if z in (HitZone.EDGE_BOTTOM, HitZone.CORNER_BL, HitZone.CORNER_BR):
                        nh = max(200, card.win_start_h + dy)
                    if z in (HitZone.EDGE_LEFT, HitZone.CORNER_TL, HitZone.CORNER_BL):
                        nx = card.win_start_x + dx
                        nw = max(200, card.win_start_w - dx)
                    if z in (HitZone.EDGE_TOP, HitZone.CORNER_TL, HitZone.CORNER_TR):
                        ny = card.win_start_y + dy
                        nh = max(200, card.win_start_h - dy)
                    self._atomic_move_two_windows(
                        int(card.scrcpy_hwnd), nx, ny, nw, nh,
                        int(card.overlay_hwnd), nx, ny, nw, nh,
                        repaint=False
                    )
                    card.overlay_width = nw
                    card.overlay_height = nh
                    self._ensure_overlay_above_scrcpy(card)
                    self._redraw_overlay_on_ui(card)
                    return 0
                else:
                    # Hover：检测是否进入按钮区更新重绘
                    zone = card.hit_test(x, y, card.overlay_width, card.overlay_height)
                    new_hover = -1
                    if zone == HitZone.TRAFFIC_CLOSE: new_hover = 0
                    elif zone == HitZone.TRAFFIC_MIN: new_hover = 1
                    elif zone == HitZone.TRAFFIC_PIN: new_hover = 2
                    if new_hover != card.hover_button:
                        card.hover_button = new_hover
                        self._redraw_overlay_on_ui(card)
                    return 0

            if msg_val == WM_LBUTTONUP:
                if card.dragging or card.resizing:
                    was_drag = card.dragging
                    dx = x - card.drag_start_x
                    dy = y - card.drag_start_y
                    if card.dragging:
                        nx = card.win_start_x + dx
                        ny = card.win_start_y + dy
                        nw, nh = card.win_start_w, card.win_start_h
                    else:
                        nx, ny = card.win_start_x, card.win_start_y
                        nw, nh = card.win_start_w, card.win_start_h
                        z = card.resize_zone
                        if z in (HitZone.EDGE_RIGHT, HitZone.CORNER_TR, HitZone.CORNER_BR):
                            nw = max(200, card.win_start_w + dx)
                        if z in (HitZone.EDGE_BOTTOM, HitZone.CORNER_BL, HitZone.CORNER_BR):
                            nh = max(200, card.win_start_h + dy)
                        if z in (HitZone.EDGE_LEFT, HitZone.CORNER_TL, HitZone.CORNER_BL):
                            nx = card.win_start_x + dx
                            nw = max(200, card.win_start_w - dx)
                        if z in (HitZone.EDGE_TOP, HitZone.CORNER_TL, HitZone.CORNER_TR):
                            ny = card.win_start_y + dy
                            nh = max(200, card.win_start_h - dy)
                    card.dragging = False
                    card.resizing = False
                    user32.ReleaseCapture()
                    # ═══════════════════════════════════════════════════════════
                    # DRAG 结束：先销毁幽灵预览窗口，再一次性把 scrcpy + overlay
                    # 两个真实窗口移到最终目标位置（只移动这一次，无抖动源）。
                    # ═══════════════════════════════════════════════════════════
                    if was_drag:
                        self._destroy_drag_ghost(card)
                    # 最终位置保证对齐（Repaint=True 一次，最终帧视觉干净）
                    self._atomic_move_two_windows(
                        int(card.scrcpy_hwnd), nx, ny, nw, nh,
                        int(card.overlay_hwnd), nx, ny, nw, nh,
                        repaint=True
                    )
                    card.overlay_width = nw
                    card.overlay_height = nh
                    # DRAG 结束也调整一次 z-order 保证 overlay 在 scrcpy 之上
                    self._ensure_overlay_above_scrcpy(card)
                    if not was_drag:
                        # 只有 RESIZE（大小真的变了）才需要 redraw
                        self._redraw_overlay_on_ui(card)
                    # 保存最终 geometry
                    if card.on_geometry_changed:
                        try:
                            card.on_geometry_changed(nx, ny, nw, nh)
                        except Exception:
                            pass
                    return 0
                user32.ReleaseCapture()
                return 0

            if msg_val == WM_RBUTTONDOWN:
                return 0
            if msg_val == WM_DESTROY:
                return 0

            # overlay 窗口不处理的消息走默认
            return user32.DefWindowProcW(hwnd_c, wintypes.UINT(msg_val),
                                        wintypes.WPARAM(wParam), wintypes.LPARAM(lParam))

        if msg_val == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd_c, wintypes.UINT(msg_val),
                                    wintypes.WPARAM(wParam), wintypes.LPARAM(lParam))

    # ──────────── Hook Thread ────────────

    def _start_hook(self):
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True)
        self._hook_thread.start()

    def _hook_loop(self):
        self._hook_proc = LOWLEVELMOUSEPROC(self._mouse_hook_proc)
        self._hook_handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._hook_proc, wintypes.HINSTANCE(0), 0
        )
        if not self._hook_handle:
            err = ctypes.get_last_error()
            print(f"[Hook] SetWindowsHookEx FAILED: {err}", flush=True)
            return
        print(f"[Hook] Mouse hook installed (handle={self._hook_handle})", flush=True)

        msg = MSG()
        while self._running:
            bRet = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if bRet > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                break

        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None
        print("[Hook] Hook unhooked", flush=True)

    def _mouse_hook_proc(self, nCode, wParam, lParam):
        try:
            if nCode < 0 or nCode != HC_ACTION:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)
            data = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            mx, my = data.pt.x, data.pt.y
            _ev = int(wParam)

            # ═══════════════════════════════════════════════════════════════════
            # 编辑模式（overlay全窗口覆盖自己收消息）：低层钩子全放行，
            # 绝对不吞任何硬件事件（避免系统防窃取钳制）。
            # ═══════════════════════════════════════════════════════════════════
            card_under = self._card_under_mouse(mx, my)
            if card_under and card_under.editing:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            # 以下仅处理非编辑模式：overlay 穿透给 scrcpy
            active = self._active_card
            swallowed = None

            # NC 拖拽模式（备用/兼容路径）
            if active and active._nc_drag_active:
                if wParam == WM_MOUSEMOVE:
                    self._post_ui(WM_TUI_MOVEOVERLAY, card=active)
                elif wParam == WM_LBUTTONUP:
                    swallowed = self._on_lbutton_up(active, mx, my, data)
            elif active and (active.dragging or active.resizing):
                # 自实现拖拽（备用路径）
                if wParam == WM_MOUSEMOVE:
                    swallowed = self._on_mouse_move(active, mx, my, data)
                    if active.dragging:
                        self._set_cursor_for_zone(HitZone.TITLE_BAR)
                    elif active.resizing:
                        self._set_cursor_for_zone(active.resize_zone)
                elif wParam == WM_LBUTTONUP:
                    swallowed = self._on_lbutton_up(active, mx, my, data)
                elif wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_RBUTTONUP):
                    swallowed = 1
                else:
                    swallowed = 1
            else:
                # 非编辑模式且无 active_card：正常穿透给 scrcpy
                card = card_under
                if wParam == WM_MOUSEMOVE:
                    swallowed = self._on_mouse_move(card, mx, my, data)
                elif wParam == WM_LBUTTONDOWN:
                    swallowed = self._on_lbutton_down(card, mx, my, data)
                elif wParam == WM_LBUTTONUP:
                    swallowed = self._on_lbutton_up(card, mx, my, data)
                elif wParam == WM_RBUTTONDOWN:
                    swallowed = self._on_rbutton_down(card, mx, my)
                else:
                    if card and card.editing:
                        zone = self._hit_test_card(card, mx, my)
                        self._set_cursor_for_zone(zone)

            if swallowed is not None:
                return swallowed
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception as e:
            print(f"[Hook] Exception in mouse hook: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _card_under_mouse(self, mx, my):
        def _hwnd_int(h):
            if h is None:
                return 0
            if isinstance(h, int):
                return h
            if hasattr(h, 'value'):
                v = h.value
                if isinstance(v, int):
                    return v
                if isinstance(v, bytes):
                    return int.from_bytes(v, byteorder='little', signed=False)
                return int(v) if v else 0
            try:
                return int(h)
            except (TypeError, ValueError):
                try:
                    return ctypes.cast(ctypes.pointer(h), ctypes.POINTER(ctypes.c_void_p)).contents.value or 0
                except Exception:
                    return 0

        hwnd = user32.WindowFromPoint(POINT(mx, my))
        if hwnd:
            probe_int = _hwnd_int(hwnd)
            for _ in range(8):
                for scrcpy_hwnd, card in self._cards.items():
                    scrcpy_int = int(scrcpy_hwnd)
                    if probe_int == scrcpy_int:
                        return card
                    if card.overlay_hwnd and probe_int == int(card.overlay_hwnd):
                        return card
                parent = user32.GetAncestor(wintypes.HWND(probe_int), GA_ROOT)
                parent_int = _hwnd_int(parent)
                if not parent_int or parent_int == probe_int:
                    break
                probe_int = parent_int

        for scrcpy_hwnd, card in reversed(list(self._cards.items())):
            try:
                r = RECT()
                if user32.GetWindowRect(wintypes.HWND(int(scrcpy_hwnd)), ctypes.byref(r)):
                    if r.left <= mx <= r.right and r.top <= my <= r.bottom:
                        if user32.IsWindowVisible(wintypes.HWND(int(scrcpy_hwnd))):
                            return card
            except Exception:
                continue
        return None

    def _window_rect(self, hwnd):
        r = RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def _screen_to_client(self, hwnd, x, y):
        pt = POINT(x, y)
        user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt))
        return pt.x, pt.y

    def _hit_test_card(self, card, mx, my):
        x, y = self._screen_to_client(card.scrcpy_hwnd, mx, my)
        _, _, w, h = self._window_rect(card.scrcpy_hwnd)
        return card.hit_test(x, y, w, h)

    def _set_cursor_for_zone(self, zone):
        cursor_id = IDC_ARROW
        if zone in (HitZone.TRAFFIC_CLOSE, HitZone.TRAFFIC_MIN, HitZone.TRAFFIC_PIN):
            cursor_id = IDC_HAND
        elif zone == HitZone.TITLE_BAR:
            cursor_id = IDC_SIZEALL
        elif zone in (HitZone.EDGE_TOP, HitZone.EDGE_BOTTOM):
            cursor_id = IDC_SIZENS
        elif zone in (HitZone.EDGE_LEFT, HitZone.EDGE_RIGHT):
            cursor_id = IDC_SIZEWE
        elif zone in (HitZone.CORNER_TL, HitZone.CORNER_BR):
            cursor_id = IDC_SIZENWSE
        elif zone in (HitZone.CORNER_TR, HitZone.CORNER_BL):
            cursor_id = IDC_SIZENESW
        user32.SetCursor(user32.LoadCursorW(None, cursor_id))

    def _on_mouse_move(self, card, mx, my, data):
        active = self._active_card
        _ev_injected = (int(data.flags) & 1) != 0

        # 原生 NC 拖拽模式：此分支在 hook proc 已提前拦截，一般不会到这里
        if active and active._nc_drag_active:
            if card and card.editing:
                zone = self._hit_test_card(card, mx, my)
                self._set_cursor_for_zone(zone)
                new_hover = -1
                if zone == HitZone.TRAFFIC_CLOSE: new_hover = 0
                elif zone == HitZone.TRAFFIC_MIN: new_hover = 1
                elif zone == HitZone.TRAFFIC_PIN: new_hover = 2
                if new_hover != card.hover_button:
                    card.hover_button = new_hover
                    self._post_ui(WM_TUI_REDRAW, card=card)
            return None

        # 自实现拖拽模式（备用）
        if active and active.dragging:
            if _ev_injected != active._drag_start_injected:
                _dlog(f"[_on_mouse_move DRAG] DROP mismatch: start_injected={active._drag_start_injected} ev_injected={_ev_injected} mx=({mx},{my})")
                return 1
            dx = mx - active.drag_start_x
            dy = my - active.drag_start_y
            new_x = active.win_start_x + dx
            new_y = active.win_start_y + dy
            active._target_x = new_x
            active._target_y = new_y
            active._target_w = active.win_start_w
            active._target_h = active.win_start_h
            was_pending = active._move_pending
            if not was_pending:
                active._move_pending = True
                self._post_ui(WM_TUI_MOVEWIN, card=active)
            _dlog(f"[_on_mouse_move DRAG] drag_start=({active.drag_start_x},{active.drag_start_y}) dxdy=({dx},{dy}) win_start=({active.win_start_x},{active.win_start_y}) target=({new_x},{new_y}) was_pending={was_pending}")
            return 1

        if active and active.resizing:
            if _ev_injected != active._drag_start_injected:
                _dlog(f"[_on_mouse_move RESIZE] DROP mismatch: start_injected={active._drag_start_injected} ev_injected={_ev_injected} mx=({mx},{my})")
                return 1
            dx = mx - active.drag_start_x
            dy = my - active.drag_start_y
            nx, ny = active.win_start_x, active.win_start_y
            nw, nh = active.win_start_w, active.win_start_h
            z = active.resize_zone
            if z in (HitZone.EDGE_RIGHT, HitZone.CORNER_TR, HitZone.CORNER_BR):
                nw = max(200, active.win_start_w + dx)
            if z in (HitZone.EDGE_BOTTOM, HitZone.CORNER_BL, HitZone.CORNER_BR):
                nh = max(200, active.win_start_h + dy)
            if z in (HitZone.EDGE_LEFT, HitZone.CORNER_TL, HitZone.CORNER_BL):
                nx = active.win_start_x + dx
                nw = max(200, active.win_start_w - dx)
            if z in (HitZone.EDGE_TOP, HitZone.CORNER_TL, HitZone.CORNER_TR):
                ny = active.win_start_y + dy
                nh = max(200, active.win_start_h - dy)
            active._target_x = nx
            active._target_y = ny
            active._target_w = nw
            active._target_h = nh
            was_pending = active._move_pending
            if not was_pending:
                active._move_pending = True
                self._post_ui(WM_TUI_MOVEWIN, card=active)
            _dlog(f"[_on_mouse_move RESIZE] zone={z} dxdy=({dx},{dy}) target=({nx},{ny},{nw},{nh}) was_pending={was_pending}")
            return 1

        if card and card.editing:
            zone = self._hit_test_card(card, mx, my)
            self._set_cursor_for_zone(zone)
            new_hover = -1
            if zone == HitZone.TRAFFIC_CLOSE: new_hover = 0
            elif zone == HitZone.TRAFFIC_MIN: new_hover = 1
            elif zone == HitZone.TRAFFIC_PIN: new_hover = 2
            if new_hover != card.hover_button:
                card.hover_button = new_hover
                self._post_ui(WM_TUI_REDRAW, card=card)

        return None

    def _on_lbutton_down(self, card, mx, my, data):
        if not card or not card.editing:
            _dlog(f"[_on_lbutton_down] SKIP: card={card is not None} editing={card.editing if card else 'N/A'}")
            return None
        zone = self._hit_test_card(card, mx, my)
        _zone_names = {v: k for k, v in vars(HitZone).items() if isinstance(v, int)}
        zn = _zone_names.get(zone, str(zone))
        _ev_injected = (int(data.flags) & 1) != 0
        _dlog(f"[_on_lbutton_down] zone={zn} mx=({mx},{my}) editing={card.editing} scrcpy_hwnd={card.scrcpy_hwnd} INJECTED={_ev_injected}")

        if zone in (HitZone.TRAFFIC_CLOSE, HitZone.TRAFFIC_MIN, HitZone.TRAFFIC_PIN):
            if zone == HitZone.TRAFFIC_CLOSE:
                self._on_close_clicked(card)
            elif zone == HitZone.TRAFFIC_MIN:
                self._on_min_clicked(card)
            elif zone == HitZone.TRAFFIC_PIN:
                self._on_pin_clicked(card)
            return 1

        # ═══════════════════════════════════════════════════════════════
        # 核心路径：标题栏拖拽 / 边缘缩放 → 触发 Windows 原生 NC 模态循环
        # ═══════════════════════════════════════════════════════════════
        _is_drag_zone = (zone == HitZone.TITLE_BAR)
        _is_resize_zone = (zone in (HitZone.EDGE_TOP, HitZone.EDGE_BOTTOM, HitZone.EDGE_LEFT,
                                    HitZone.EDGE_RIGHT, HitZone.CORNER_TL, HitZone.CORNER_TR,
                                    HitZone.CORNER_BL, HitZone.CORNER_BR))
        if _is_drag_zone or _is_resize_zone:
            # Step 1: 吞噬本次 LDOWN，防止 scrcpy/SDL 把它当作客户区点击传入 Android
            # Step 2: 设置原生 NC 拖拽标记
            card._nc_drag_active = True
            card._nc_drag_zone = zone
            # 自实现标记保留（兼容其他逻辑）
            card.dragging = _is_drag_zone
            card.resizing = _is_resize_zone
            card.resize_zone = zone if _is_resize_zone else HitZone.NONE
            card._drag_start_injected = _ev_injected
            card.drag_start_x = mx; card.drag_start_y = my
            sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
            card.win_start_x, card.win_start_y = sx, sy
            card.win_start_w, card.win_start_h = sw, sh
            self._active_card = card
            _dlog(f"  → NC {'DRAG' if _is_drag_zone else 'RESIZE'} START zone={zn} injected={_ev_injected} win_start=({sx},{sy}) size=({sw},{sh})")
            # Step 3: PostMessage 到 UI 线程发送 WM_NCLBUTTONDOWN
            # （不能在钩子线程里直接 PostMessage 跨进程窗口，避免死锁/吞消息；
            #  先在本进程 UI 线程中转一下更安全）
            self._post_ui(WM_TUI_NCDRAG, card=card)
            return 1  # 关键：吞噬 LDOWN

        return None

    def _on_lbutton_up(self, card, mx, my, data):
        active = self._active_card
        _ev_injected = (int(data.flags) & 1) != 0
        _dlog(f"[_on_lbutton_up] mx=({mx},{my}) active_card={active is not None} "
              f"nc_drag={active._nc_drag_active if active else 'N/A'} "
              f"drag={active.dragging if active else 'N/A'} resize={active.resizing if active else 'N/A'} "
              f"INJECTED={_ev_injected}")
        if not active:
            return None

        # ════════════════════════════════════
        # 原生 NC 拖拽结束处理
        # ════════════════════════════════════
        if active._nc_drag_active:
            # Windows 原生模态循环已经把 scrcpy 窗口移动到最终位置，
            # 我们只需要：同步 overlay + 保存 geometry + 清理状态
            if _ev_injected != active._drag_start_injected:
                _dlog(f"  → DROP NC LUP mismatch: start_injected={active._drag_start_injected} ev_injected={_ev_injected}")
                return 1
            sx, sy, sw, sh = self._window_rect(active.scrcpy_hwnd)
            _dlog(f"  → NC END final rect=({sx},{sy},{sw},{sh})")
            active._nc_drag_active = False
            active._nc_drag_zone = HitZone.NONE
            active.dragging = False
            active.resizing = False
            self._active_card = None
            self._sync_overlay_now(active)
            if active.on_geometry_changed:
                try:
                    active.on_geometry_changed(sx, sy, sw, sh)
                except Exception:
                    pass
            return 1  # 吞噬 LUP 防止误触发 Android 内部抬起

        # ════════════════════════════════════
        # 旧自实现路径（备用）
        # ════════════════════════════════════
        if active.dragging or active.resizing:
            if _ev_injected != active._drag_start_injected:
                _dlog(f"  → DROP LUP mismatch: start_injected={active._drag_start_injected} ev_injected={_ev_injected}")
                return 1
            dx = mx - active.drag_start_x
            dy = my - active.drag_start_y
            if active.dragging:
                nx = active.win_start_x + dx
                ny = active.win_start_y + dy
                nw, nh = active.win_start_w, active.win_start_h
            else:
                nx, ny = active.win_start_x, active.win_start_y
                nw, nh = active.win_start_w, active.win_start_h
                z = active.resize_zone
                if z in (HitZone.EDGE_RIGHT, HitZone.CORNER_TR, HitZone.CORNER_BR):
                    nw = max(200, active.win_start_w + dx)
                if z in (HitZone.EDGE_BOTTOM, HitZone.CORNER_BL, HitZone.CORNER_BR):
                    nh = max(200, active.win_start_h + dy)
                if z in (HitZone.EDGE_LEFT, HitZone.CORNER_TL, HitZone.CORNER_BL):
                    nx = active.win_start_x + dx
                    nw = max(200, active.win_start_w - dx)
                if z in (HitZone.EDGE_TOP, HitZone.CORNER_TL, HitZone.CORNER_TR):
                    ny = active.win_start_y + dy
                    nh = max(200, active.win_start_h - dy)
            active.dragging = False
            active.resizing = False
            self._active_card = None
            b_sx, b_sy, b_sw, b_sh = self._window_rect(active.scrcpy_hwnd)
            _dlog(f"  → FINAL MoveWindow: before=({b_sx},{b_sy},{b_sw},{b_sh}) target=({nx},{ny},{nw},{nh})")
            user32.MoveWindow(wintypes.HWND(active.scrcpy_hwnd), nx, ny, nw, nh, True)
            a_sx, a_sy, a_sw, a_sh = self._window_rect(active.scrcpy_hwnd)
            _dlog(f"  → AFTER MoveWindow: actual=({a_sx},{a_sy},{a_sw},{a_sh})")
            self._sync_overlay_now(active)
            if active.on_geometry_changed:
                try:
                    active.on_geometry_changed(nx, ny, nw, nh)
                except Exception:
                    pass
            return 1
        return None

    # ─────────── Traffic light actions ───────────

    def _on_close_clicked(self, card):
        print(f"[CardWindow] Close clicked for hwnd={card.scrcpy_hwnd}", flush=True)
        def _do_close():
            if card.on_close:
                try:
                    card.on_close()
                    return
                except Exception as e:
                    print(f"[CardWindow] on_close callback error: {e}", flush=True)
            user32.PostMessageW(wintypes.HWND(card.scrcpy_hwnd), wintypes.UINT(0x0010), 0, 0)
        threading.Thread(target=_do_close, daemon=True).start()

    def _on_min_clicked(self, card):
        print(f"[CardWindow] Minimize clicked for hwnd={card.scrcpy_hwnd}", flush=True)
        if card.overlay_hwnd:
            user32.ShowWindow(wintypes.HWND(card.overlay_hwnd), SW_HIDE)
        user32.ShowWindow(wintypes.HWND(card.scrcpy_hwnd), 6)

    def _on_pin_clicked(self, card):
        print(f"[CardWindow] Pin clicked → exiting edit mode", flush=True)
        self.set_edit_mode(card.scrcpy_hwnd, False)

    def _on_rbutton_down(self, card, mx, my):
        return None

    # ─────────── Public API ───────────

    def register_window(self, scrcpy_hwnd, on_close=None, on_geometry_changed=None, on_minimize=None):
        scrcpy_hwnd = int(scrcpy_hwnd)
        if scrcpy_hwnd in self._cards:
            return
        card = CardState(scrcpy_hwnd, on_close=on_close,
                        on_geometry_changed=on_geometry_changed,
                        on_minimize=on_minimize)
        card.editing = False
        self._cards[scrcpy_hwnd] = card
        self._card_ptrs[id(card)] = card
        self._post_ui(WM_TUI_CREATEOVERLAY, card=card)

    def unregister_window(self, scrcpy_hwnd):
        scrcpy_hwnd = int(scrcpy_hwnd)
        if scrcpy_hwnd not in self._cards:
            return
        card = self._cards.pop(scrcpy_hwnd)
        self._card_ptrs.pop(id(card), None)
        self._post_ui(WM_TUI_DESTROYOVERLAY, card=card)
        if self._active_card == card:
            self._active_card = None

    def set_edit_mode(self, scrcpy_hwnd, editing):
        scrcpy_hwnd = int(scrcpy_hwnd)
        if scrcpy_hwnd not in self._cards:
            return
        card = self._cards[scrcpy_hwnd]
        card.editing = editing
        card.hover_button = -1
        self._post_ui(WM_TUI_SHOWHIDE, 1 if editing else 0, card=card)
        if not editing:
            user32.SetCursor(user32.LoadCursorW(None, IDC_ARROW))
            try:
                sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
                if card.on_geometry_changed:
                    card.on_geometry_changed(sx, sy, sw, sh)
            except Exception:
                pass
            print(f"[CardWindow] Edit mode OFF (pinned), geometry saved for hwnd={scrcpy_hwnd}", flush=True)
        else:
            print(f"[CardWindow] Edit mode ON for hwnd={scrcpy_hwnd}", flush=True)

    def toggle_edit_mode(self, scrcpy_hwnd):
        scrcpy_hwnd = int(scrcpy_hwnd)
        if scrcpy_hwnd in self._cards:
            card = self._cards[scrcpy_hwnd]
            self.set_edit_mode(scrcpy_hwnd, not card.editing)

    def set_all_edit_mode(self, editing):
        for scrcpy_hwnd in list(self._cards.keys()):
            self.set_edit_mode(scrcpy_hwnd, editing)
        print(f"[CardWindow] All cards edit mode: {'ON' if editing else 'OFF (pinned)'}", flush=True)

    def refresh_card_overlay(self, scrcpy_hwnd):
        """Re-sync the overlay window position/size after programmatic window move/resize."""
        scrcpy_hwnd = int(scrcpy_hwnd)
        if scrcpy_hwnd in self._cards:
            self._post_ui(WM_TUI_MOVEOVERLAY, card=self._cards[scrcpy_hwnd])

    def shutdown(self):
        self._running = False
        for hwnd in list(self._cards.keys()):
            self.unregister_window(hwnd)
        if self._msg_hwnd:
            self._post_ui(WM_TUI_QUIT)


# ──────────────────────── X11 Decorator (stub) ────────────────────────

class X11CardDecorator:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._cards = {}

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = X11CardDecorator()
            return cls._instance

    def register_window(self, xid, on_close=None):
        pass

    def unregister_window(self, xid):
        pass

    def set_edit_mode(self, xid, editing):
        pass

    def toggle_edit_mode(self, xid):
        pass

    def refresh_card_overlay(self, xid):
        pass

    def shutdown(self):
        pass


# ──────────────────────── Public API ────────────────────────

def get_card_window_decorator():
    if os.name == 'nt':
        return WindowsCardDecorator.get_instance()
    else:
        return X11CardDecorator.get_instance()
