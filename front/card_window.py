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
    WM_TUI_QUIT = WM_USER + 99

    WH_MOUSE_LL = 14
    HC_ACTION = 0

    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_MOUSEMOVE = 0x0200
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205

    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SW_SHOW = 5

    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
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
        self.on_close = on_close
        self.on_geometry_changed = on_geometry_changed
        self.on_minimize = on_minimize

        self.btn_size = 12
        self.btn_spacing = 8
        self.btn_offset_x = 12
        self.btn_offset_y = 10
        self.overlay_width = 3 * self.btn_size + 2 * self.btn_spacing + 2 * self.btn_offset_x
        self.overlay_height = self.btn_size + 2 * self.btn_offset_y

        self.title_bar_height = 32
        self.edge_size = 6
        self.corner_size = 12

        self.editing = False
        self.hover_button = -1
        self.dragging = False
        self.resizing = False
        self.resize_zone = HitZone.NONE
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.win_start_x = 0
        self.win_start_y = 0
        self.win_start_w = 0
        self.win_start_h = 0

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
            user32.PostMessageW(wintypes.HWND(self._msg_hwnd), wintypes.UINT(msg),
                                wintypes.WPARAM(wparam), wintypes.LPARAM(lparam))

    def _card_from_lparam(self, lparam):
        return self._card_ptrs.get(int(lparam))

    # ──────────── Overlay window creation ────────────

    def _create_overlay_for_card(self, card):
        inst = kernel32.GetModuleHandleW(None)
        sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)

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
        print(f"[UI] Overlay created: hwnd={hwnd} for scrcpy={card.scrcpy_hwnd}", flush=True)

        self._redraw_overlay_on_ui(card)

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
        ex = user32.GetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE)
        ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE, ex)
        user32.ShowWindow(wintypes.HWND(card.overlay_hwnd), SW_SHOWNOACTIVATE)

    def _hide_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        user32.ShowWindow(wintypes.HWND(card.overlay_hwnd), SW_HIDE)
        ex = user32.GetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE)
        ex |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(card.overlay_hwnd), GWL_EXSTYLE, ex)

    def _sync_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        sx, sy, _, _ = self._window_rect(card.scrcpy_hwnd)
        user32.MoveWindow(wintypes.HWND(card.overlay_hwnd), sx, sy,
                          card.overlay_width, card.overlay_height, True)
        user32.SetWindowPos(wintypes.HWND(card.overlay_hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _redraw_overlay_on_ui(self, card):
        if not card.overlay_hwnd:
            return
        w, h = card.overlay_width, card.overlay_height
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
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255 if card.editing else 0, AC_SRC_ALPHA)

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
        ctypes.memset(ppv_bits, 0, buf_size)

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
                if card:
                    self._sync_overlay_on_ui(card)
                return 0
            elif msg_val == WM_TUI_QUIT:
                user32.PostQuitMessage(0)
                return 0
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
            card = self._card_under_mouse(mx, my)

            swallowed = None
            if wParam == WM_MOUSEMOVE:
                swallowed = self._on_mouse_move(card, mx, my, data)
            elif wParam == WM_LBUTTONDOWN:
                swallowed = self._on_lbutton_down(card, mx, my)
            elif wParam == WM_LBUTTONUP:
                swallowed = self._on_lbutton_up(card, mx, my)
            elif wParam == WM_RBUTTONDOWN:
                swallowed = self._on_rbutton_down(card, mx, my)
            elif wParam == WM_RBUTTONUP:
                swallowed = None
            else:
                if card and card.editing and not self._active_card:
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

        if active and active.dragging:
            dx = mx - active.drag_start_x
            dy = my - active.drag_start_y
            new_x = active.win_start_x + dx
            new_y = active.win_start_y + dy
            user32.MoveWindow(wintypes.HWND(active.scrcpy_hwnd), new_x, new_y,
                              active.win_start_w, active.win_start_h, True)
            self._post_ui(WM_TUI_MOVEOVERLAY, card=active)
            return 1

        if active and active.resizing:
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
            user32.MoveWindow(wintypes.HWND(active.scrcpy_hwnd), nx, ny, nw, nh, True)
            self._post_ui(WM_TUI_MOVEOVERLAY, card=active)
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

    def _on_lbutton_down(self, card, mx, my):
        if not card or not card.editing:
            return None
        zone = self._hit_test_card(card, mx, my)

        if zone in (HitZone.TRAFFIC_CLOSE, HitZone.TRAFFIC_MIN, HitZone.TRAFFIC_PIN):
            if zone == HitZone.TRAFFIC_CLOSE:
                self._on_close_clicked(card)
            elif zone == HitZone.TRAFFIC_MIN:
                self._on_min_clicked(card)
            elif zone == HitZone.TRAFFIC_PIN:
                self._on_pin_clicked(card)
            return 1

        if zone == HitZone.TITLE_BAR:
            card.dragging = True
            card.resizing = False
            card.drag_start_x = mx; card.drag_start_y = my
            sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
            card.win_start_x, card.win_start_y = sx, sy
            card.win_start_w, card.win_start_h = sw, sh
            self._active_card = card
            user32.SetWindowPos(wintypes.HWND(card.scrcpy_hwnd), wintypes.HWND(HWND_TOPMOST),
                                0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            return 1

        if zone in (HitZone.EDGE_TOP, HitZone.EDGE_BOTTOM, HitZone.EDGE_LEFT,
                    HitZone.EDGE_RIGHT, HitZone.CORNER_TL, HitZone.CORNER_TR,
                    HitZone.CORNER_BL, HitZone.CORNER_BR):
            card.dragging = False
            card.resizing = True
            card.resize_zone = zone
            card.drag_start_x = mx; card.drag_start_y = my
            sx, sy, sw, sh = self._window_rect(card.scrcpy_hwnd)
            card.win_start_x, card.win_start_y = sx, sy
            card.win_start_w, card.win_start_h = sw, sh
            self._active_card = card
            return 1

        return None

    def _on_lbutton_up(self, card, mx, my):
        active = self._active_card
        if active and (active.dragging or active.resizing):
            active.dragging = False
            active.resizing = False
            self._active_card = None
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
