"""
Qt Backdrop Window - 模拟无桌面Linux的深色背景容器窗口。

使用PySide6创建一个无边框、深色背景的窗口作为虚拟屏幕卡片的"桌面"基底。
背景窗口本身是WS_EX_TOPMOST，但通过SetWindowPos(HWND_BOTTOM)置于topmost序列
最底部；scrcpy卡片窗口同样是WS_EX_TOPMOST（--always-on-top），自然浮在背景之上，
营造悬浮于Linux黑色虚空的视觉效果。

设计要点：
- 无边框、深黑色背景(#0a0a0f)，模拟无桌面Linux的X11 root window
- 窗口不激活、不抢焦点、不出现在任务栏(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
- 窗口是topmost但位于topmost Z-order最底部(HWND_BOTTOM)，与scrcpy同层但在其下
- 不设置WS_EX_TRANSPARENT——点击空白区域被拦截但不处理
- scrcpy窗口保持WS_EX_TOPMOST，始终浮在背景之上
- 2秒定时器持续确保背景在Z-order底部，防止被其他topmost窗口挤到前面
- 运行在独立QApplication进程中，避免与Flask/主进程冲突
"""

import os
import sys
import ctypes
import math
from ctypes import wintypes
import multiprocessing
import json


def _safe_hwnd(h):
    """Safely convert a window handle to integer."""
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


def _run_backdrop_process(pipe_conn, config_path=None):
    """在独立进程中运行Qt背景窗口 + macOS风格通知面板 + Dock工具栏。"""
    if os.name != 'nt':
        return

    # Ensure project root is in sys.path for reliable imports in the spawned process
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _proj_root = os.path.dirname(_this_dir)
    if _proj_root not in sys.path:
        sys.path.insert(0, _proj_root)

    import ctypes
    user32 = ctypes.windll.user32

    try:
        from PySide6.QtWidgets import QApplication, QWidget
        from PySide6.QtGui import QPainter, QColor, QFont, QPen, QPainterPath, QCursor, QBrush
        from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QRectF
    except ImportError:
        print("[QtBackdrop] PySide6 not installed. Run: pip install PySide6", flush=True)
        pipe_conn.send({'status': 'error', 'error': 'PySide6 not installed'})
        return

    try:
        from front.qt_notification import create_notification_window
    except ImportError:
        try:
            from qt_notification import create_notification_window
        except ImportError:
            create_notification_window = None
            print("[QtBackdrop] qt_notification module not found, notification panel disabled", flush=True)

    # ── Windows 常量 ──
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CLIPSIBLINGS = 0x04000000
    WS_CLIPCHILDREN = 0x02000000
    WS_DISABLED = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_NOPARENTNOTIFY = 0x00000004
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_LAYERED = 0x00080000
    HWND_BOTTOM = 1
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_NOSENDCHANGING = 0x0400
    SWP_NOOWNERZORDER = 0x0200
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040

    def _safe_hwnd(h):
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

    class BackdropWidget(QWidget):
        """无桌面Linux风格的深色背景窗口。"""

        def __init__(self, width=2560, height=1440):
            super().__init__()
            self.bg_color = QColor("#0a0a0f")
            self.grid_color = QColor(18, 20, 28, 35)
            self.corner_color = QColor(25, 28, 40, 50)
            self.hint_color = QColor(55, 60, 75, 100)
            self._init_ui(width, height)

        def _init_ui(self, width, height):
            self.setWindowTitle("AR Desktop Backdrop")
            self.setFixedSize(width, height)
            self.move(0, 0)

            self.setWindowFlags(
                Qt.FramelessWindowHint
                | Qt.WindowDoesNotAcceptFocus
                | Qt.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_TranslucentBackground, False)
            self.setAttribute(Qt.WA_NoSystemBackground, False)
            self.setAutoFillBackground(True)
            self.setMouseTracking(True)

            pal = self.palette()
            pal.setColor(self.backgroundRole(), self.bg_color)
            self.setPalette(pal)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, False)

            w = self.width()
            h = self.height()

            painter.fillRect(0, 0, w, h, self.bg_color)

            grid_size = 80
            pen = QPen(self.grid_color)
            pen.setWidth(1)
            painter.setPen(pen)

            for x in range(0, w, grid_size):
                painter.drawLine(x, 0, x, h)
            for y in range(0, h, grid_size):
                painter.drawLine(0, y, w, y)

            pen = QPen(self.corner_color)
            pen.setWidth(2)
            painter.setPen(pen)
            corner_size = 50
            for cx, cy in [(0, 0), (w, 0), (0, h), (w, h)]:
                if cx == 0 and cy == 0:
                    painter.drawLine(cx, cy + corner_size, cx, cy)
                    painter.drawLine(cx, cy, cx + corner_size, cy)
                elif cx == w and cy == 0:
                    painter.drawLine(cx, cy + corner_size, cx, cy)
                    painter.drawLine(cx - corner_size, cy, cx, cy)
                elif cx == 0 and cy == h:
                    painter.drawLine(cx, cy - corner_size, cx, cy)
                    painter.drawLine(cx, cy, cx + corner_size, cy)
                else:
                    painter.drawLine(cx, cy - corner_size, cx, cy)
                    painter.drawLine(cx - corner_size, cy, cx, cy)

            font = QFont("Consolas", 9)
            painter.setFont(font)
            painter.setPen(self.hint_color)
            painter.drawText(24, h - 24, "AR Linux Desktop  ·  Right-click any card to enter edit mode")
            painter.drawText(24, 28, "●  scrcpy virtual display cluster")

            painter.end()

        def mousePressEvent(self, event):
            event.ignore()

        def mouseReleaseEvent(self, event):
            event.ignore()

        def mouseDoubleClickEvent(self, event):
            event.ignore()

        def nativeEvent(self, eventType, message):
            try:
                if eventType == b"windows_generic_MSG":
                    msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                    WM_CREATE = 0x0001
                    WM_SHOWWINDOW = 0x0018
                    WM_WINDOWPOSCHANGING = 0x0046
                    WM_NCACTIVATE = 0x0086
                    WM_MOUSEACTIVATE = 0x0021
                    WM_ACTIVATE = 0x0006
                    WM_SETFOCUS = 0x0007
                    WM_KILLFOCUS = 0x0008

                    if msg.message == WM_CREATE:
                        QTimer.singleShot(0, self._apply_win32_style)
                    elif msg.message == WM_SHOWWINDOW:
                        QTimer.singleShot(50, self._apply_win32_style)
                    elif msg.message in (WM_NCACTIVATE, WM_ACTIVATE, WM_SETFOCUS):
                        return True, 0
                    elif msg.message == WM_MOUSEACTIVATE:
                        MA_NOACTIVATE = 3
                        return True, MA_NOACTIVATE
            except Exception:
                pass
            return super().nativeEvent(eventType, message)

        def _apply_win32_style(self):
            try:
                hwnd = _safe_hwnd(self.winId())
                if not hwnd:
                    return

                ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
                new_ex = (
                    ex_style
                    | WS_EX_NOACTIVATE
                    | WS_EX_TOOLWINDOW
                    | WS_EX_NOPARENTNOTIFY
                    | WS_EX_TOPMOST
                )
                new_ex &= ~WS_EX_APPWINDOW
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)

                style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
                new_style = style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
                new_style &= ~WS_DISABLED
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, new_style)

                user32.SetWindowPos(
                    wintypes.HWND(hwnd),
                    wintypes.HWND(HWND_BOTTOM),
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                    | SWP_NOSENDCHANGING | SWP_NOOWNERZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                )
            except Exception as e:
                print(f"[QtBackdrop] Style error: {e}", flush=True)

        def ensure_bottom(self):
            """强制将窗口置于Z-order底部。"""
            self._apply_win32_style()

    class DockToolbarWidget(QWidget):
        """Mac风格底部Dock工具栏 - 固定在屏幕正下方，悬停显示，两个图标。"""

        def __init__(self, pipe_conn=None):
            super().__init__()
            self._pipe_conn = pipe_conn
            self._notif_window = None
            self._hovering = False
            self._hover_icon = -1
            self._pressed_icon = -1
            self._edit_mode_shown = False
            self._icon_size = 48
            self._icon_spacing = 16
            self._padding = 16
            self._dock_hidden = True
            self._opacity = 0.0
            self._trigger_zone = 4
            self._dock_w = 0
            self._dock_h = 0
            self._init_ui()

        def _init_ui(self):
            self.setWindowTitle("AR Dock")
            self._dock_w = 2 * self._icon_size + self._icon_spacing + 2 * self._padding
            self._dock_h = self._icon_size + 2 * self._padding
            screen = QApplication.primaryScreen()
            if screen:
                sg = screen.availableGeometry()
                x = sg.center().x() - self._dock_w // 2
                y = sg.bottom() - self._dock_h - 8
                self.move(x, y)
            self.resize(self._dock_w, self._dock_h)
            self.setFixedSize(self._dock_w, self._dock_h)

            self.setWindowFlags(
                Qt.FramelessWindowHint
                | Qt.WindowDoesNotAcceptFocus
                | Qt.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAutoFillBackground(False)
            self.setMouseTracking(True)

            self._fade_timer = QTimer(self)
            self._fade_timer.timeout.connect(self._do_fade)
            self._fade_timer.setInterval(16)

            self._hover_timer = QTimer(self)
            self._hover_timer.timeout.connect(self._check_mouse_hover)
            self._hover_timer.setInterval(50)
            self._hover_timer.start()

        def _get_icon_rect(self, index):
            start_x = self._padding
            x = start_x + index * (self._icon_size + self._icon_spacing)
            y = self._padding
            scale = 1.0
            if self._hover_icon == index:
                scale = 1.25
            if self._pressed_icon == index:
                scale = 1.1
            size = int(self._icon_size * scale)
            offset = (self._icon_size - size) // 2
            return x + offset, y + offset, size, size

        def set_notif_window(self, notif_window):
            self._notif_window = notif_window

        def _icon_at(self, pos):
            for i in range(2):
                ix, iy, isize, _ = self._get_icon_rect(i)
                if ix <= pos.x() <= ix + isize and iy <= pos.y() <= iy + isize:
                    return i
            return -1

        def _toggle_edit_mode(self):
            self._edit_mode_shown = not self._edit_mode_shown
            show = self._edit_mode_shown
            if self._pipe_conn:
                try:
                    self._pipe_conn.send({'cmd': 'toggle_edit_mode', 'show': show})
                except Exception:
                    pass
            if self._notif_window:
                try:
                    if show:
                        self._notif_window._set_edit_mode(True)
                    else:
                        self._notif_window._set_edit_mode(False)
                    self._notif_window.update()
                except Exception as e:
                    print(f"[DockToolbar] Toggle notif edit error: {e}", flush=True)

        def _do_fade(self):
            target = 0.92 if self._hovering else 0.0
            diff = target - self._opacity
            step = 0.08
            if abs(diff) < step:
                self._opacity = target
                self._fade_timer.stop()
                if self._opacity <= 0.01:
                    self._dock_hidden = True
            else:
                self._opacity += step if diff > 0 else -step
            self.setWindowOpacity(self._opacity)
            self.update()

        def _check_mouse_hover(self):
            cursor_pos = QCursor.pos()
            screen = QApplication.primaryScreen()
            if not screen:
                return
            sg = screen.availableGeometry()
            g = self.geometry()
            in_trigger = (cursor_pos.y() >= sg.bottom() - self._trigger_zone and
                         abs(cursor_pos.x() - g.center().x()) < self._dock_w // 2 + 40)
            in_dock = (g.left() - 20 <= cursor_pos.x() <= g.right() + 20 and
                       g.top() - 10 <= cursor_pos.y() <= g.bottom() + 10)
            if self._dock_hidden:
                if in_trigger or in_dock:
                    self._start_fade(True)
            else:
                if not in_dock and not in_trigger:
                    self._start_fade(False)

        def _start_fade(self, show):
            self._hovering = show
            if show and self._dock_hidden:
                self.show()
                self._dock_hidden = False
                QTimer.singleShot(0, self._apply_win32_style)
            self._fade_timer.start()

        def enterEvent(self, event):
            if not self._dock_hidden:
                self._start_fade(True)
            super().enterEvent(event)

        def leaveEvent(self, event):
            self._hover_icon = -1
            self._start_fade(False)
            self.setCursor(Qt.ArrowCursor)
            super().leaveEvent(event)

        def showEvent(self, event):
            super().showEvent(event)
            QTimer.singleShot(0, self._apply_win32_style)
            QTimer.singleShot(100, self._apply_win32_style)

        def nativeEvent(self, eventType, message):
            try:
                if eventType == b"windows_generic_MSG":
                    msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                    WM_CREATE = 0x0001
                    WM_SHOWWINDOW = 0x0018
                    WM_NCACTIVATE = 0x0086
                    WM_MOUSEACTIVATE = 0x0021
                    WM_ACTIVATE = 0x0006
                    WM_SETFOCUS = 0x0007
                    if msg.message == WM_CREATE:
                        QTimer.singleShot(0, self._apply_win32_style)
                    elif msg.message == WM_SHOWWINDOW:
                        QTimer.singleShot(50, self._apply_win32_style)
                    elif msg.message in (WM_NCACTIVATE, WM_ACTIVATE, WM_SETFOCUS):
                        return True, 0
                    elif msg.message == WM_MOUSEACTIVATE:
                        return True, 3
            except Exception:
                pass
            return super().nativeEvent(eventType, message)

        def _apply_win32_style(self):
            try:
                hwnd = _safe_hwnd(self.winId())
                if not hwnd:
                    return
                ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
                new_ex = ex_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_NOPARENTNOTIFY | WS_EX_TOPMOST | WS_EX_LAYERED
                new_ex &= ~WS_EX_APPWINDOW
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)
                style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
                new_style = style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, new_style)
                try:
                    DWMWA_WINDOW_CORNER_PREFERENCE = 33
                    DWMWCP_ROUND = 2
                    corner_pref = ctypes.c_int(DWMWCP_ROUND)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        wintypes.HWND(hwnd),
                        ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
                        ctypes.byref(corner_pref),
                        ctypes.sizeof(corner_pref)
                    )
                except Exception:
                    pass
                user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                )
            except Exception as e:
                print(f"[DockToolbar] Style error: {e}", flush=True)

        def _draw_settings_icon(self, painter, x, y, size):
            color = QColor(220, 220, 230)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            cx = x + size // 2
            cy = y + size // 2
            outer = size * 0.38
            inner = size * 0.22
            tooth = size * 0.08
            path = QPainterPath()
            for i in range(8):
                angle = i * math.pi / 4
                ox = cx + int(outer * math.cos(angle))
                oy = cy + int(outer * math.sin(angle))
                if i == 0:
                    path.moveTo(ox, oy)
                else:
                    path.lineTo(ox, oy)
                a1 = angle + math.pi / 8
                a2 = angle + math.pi / 4 - math.pi / 8
                path.lineTo(cx + int(outer * math.cos(a1)), cy + int(outer * math.sin(a1)))
                path.lineTo(cx + int((outer + tooth * 0.5) * math.cos((a1 + a2) / 2)), cy + int((outer + tooth * 0.5) * math.sin((a1 + a2) / 2)))
                path.lineTo(cx + int(outer * math.cos(a2)), cy + int(outer * math.sin(a2)))
            path.closeSubpath()
            painter.drawPath(path)
            painter.setBrush(QColor(20, 20, 30))
            painter.drawEllipse(QPoint(cx, cy), int(inner), int(inner))

        def _draw_desktop_icon(self, painter, x, y, size):
            color = QColor(220, 220, 230)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            monitor_w = size * 0.72
            monitor_h = size * 0.5
            bezel = size * 0.05
            mx = x + (size - monitor_w) / 2
            my = y + size * 0.14
            painter.drawRoundedRect(int(mx), int(my), int(monitor_w), int(monitor_h), 4, 4)
            painter.setBrush(QColor(20, 20, 30))
            painter.drawRoundedRect(int(mx + bezel), int(my + bezel), int(monitor_w - 2 * bezel), int(monitor_h - 2 * bezel), 2, 2)
            painter.setBrush(color)
            stand_w = size * 0.24
            stand_h = size * 0.08
            painter.drawRect(int(x + size / 2 - stand_w / 2), int(my + monitor_h), int(stand_w), int(stand_h))
            base_w = size * 0.5
            base_h = size * 0.06
            painter.drawRoundedRect(int(x + size / 2 - base_w / 2), int(my + monitor_h + stand_h - 2), int(base_w), int(base_h), 2, 2)

        def paintEvent(self, event):
            p = QPainter(self)
            try:
                p.setRenderHint(QPainter.Antialiasing)
                w, h = self.width(), self.height()
                bg_color = QColor(40, 40, 50, 220)
                border_color = QColor(255, 255, 255, 40)
                p.setPen(QPen(border_color, 1))
                p.setBrush(bg_color)
                radius = 14
                p.drawRoundedRect(QRect(0, 0, w, h).adjusted(1, 1, -1, -1), radius, radius)
                highlight = QColor(255, 255, 255, 25)
                p.setPen(Qt.NoPen)
                p.setBrush(highlight)
                hl_rect = QRectF(4, 2, w - 8, radius * 0.7)
                path = QPainterPath()
                path.addRoundedRect(hl_rect, radius - 4, radius - 4)
                p.fillPath(path, highlight)
                for i in range(2):
                    ix, iy, isize, _ = self._get_icon_rect(i)
                    is_hover = (self._hover_icon == i)
                    is_active = (i == 0 and self._edit_mode_shown)
                    if is_hover or is_active:
                        glow_color = QColor(100, 180, 255, 50) if is_active else QColor(255, 255, 255, 30)
                        p.setBrush(glow_color)
                        p.drawEllipse(QPoint(ix + isize // 2, iy + isize // 2), isize // 2 + 6, isize // 2 + 6)
                    if i == 0:
                        self._draw_settings_icon(p, ix, iy, isize)
                    else:
                        self._draw_desktop_icon(p, ix, iy, isize)
            finally:
                p.end()

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton:
                pos = event.position().toPoint()
                icon_idx = self._icon_at(pos)
                self._pressed_icon = icon_idx
                if icon_idx >= 0:
                    self.update()
            event.accept()

        def mouseReleaseEvent(self, event):
            if event.button() == Qt.LeftButton:
                pos = event.position().toPoint()
                icon_idx = self._icon_at(pos)
                pressed = self._pressed_icon
                self._pressed_icon = -1
                if icon_idx == 0 and pressed == 0:
                    self._toggle_edit_mode()
                self.update()
            event.accept()

        def mouseMoveEvent(self, event):
            pos = event.position().toPoint()
            old_hover = self._hover_icon
            self._hover_icon = -1
            for i in range(2):
                ix, iy, isize, _ = self._get_icon_rect(i)
                if ix <= pos.x() <= ix + isize and iy <= pos.y() <= iy + isize:
                    self._hover_icon = i
                    break
            if self._hover_icon != old_hover:
                self.update()
            if self._hover_icon >= 0:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            super().mouseMoveEvent(event)

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    bg_w = 2560
    bg_h = 2080
    notif_cfg = None

    try:
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            max_x = 0
            max_y = 0
            all_windows = [cfg.get('main_display', {})]
            for wcfg in cfg.get('app_displays', {}).values():
                all_windows.append(wcfg)
            notif_cfg = cfg.get('notification_panel', None)
            if notif_cfg:
                all_windows.append(notif_cfg)
            for wc in all_windows:
                wx = wc.get('window_x', 0)
                wy = wc.get('window_y', 0)
                ww = wc.get('window_width', 540)
                wh = wc.get('window_height', 960)
                max_x = max(max_x, wx + ww)
                max_y = max(max_y, wy + wh)
            if max_x > 0 and max_y > 0:
                bg_w = max(max_x + 200, 1920)
                bg_h = max(max_y + 200, 1080)
    except Exception:
        pass

    widget = BackdropWidget(width=bg_w, height=bg_h)
    widget.show()
    widget._apply_win32_style()
    QTimer.singleShot(200, widget._apply_win32_style)
    QTimer.singleShot(1000, widget._apply_win32_style)

    notif_window = None
    if create_notification_window and notif_cfg:
        try:
            notif_window = create_notification_window(config=notif_cfg, config_path=config_path)
            if notif_window:
                notif_window.show()
                QTimer.singleShot(100, notif_window._apply_style)
                QTimer.singleShot(500, notif_window._apply_style)
                print("[QtBackdrop] Notification panel created", flush=True)
        except Exception as e:
            print(f"[QtBackdrop] Failed to create notification panel: {e}", flush=True)
            import traceback
            traceback.print_exc()
            notif_window = None

    dock_window = None
    try:
        dock_window = DockToolbarWidget(pipe_conn=pipe_conn)
        dock_window.setWindowOpacity(0.0)
        dock_window.show()
        QTimer.singleShot(100, dock_window._apply_win32_style)
        QTimer.singleShot(300, dock_window._apply_win32_style)
        print("[QtBackdrop] Dock toolbar created", flush=True)
    except Exception as e:
        print(f"[QtBackdrop] Failed to create dock toolbar: {e}", flush=True)
        import traceback
        traceback.print_exc()
        dock_window = None

    if dock_window and notif_window:
        dock_window.set_notif_window(notif_window)

    bg_hwnd = _safe_hwnd(widget.winId())
    notif_hwnd = _safe_hwnd(notif_window.winId()) if notif_window else 0
    dock_hwnd = _safe_hwnd(dock_window.winId()) if dock_window else 0
    pipe_conn.send({'status': 'ready', 'hwnd': bg_hwnd, 'notif_hwnd': notif_hwnd, 'dock_hwnd': dock_hwnd})

    def poll_pipe():
        nonlocal widget, notif_window, dock_window
        while pipe_conn.poll():
            try:
                msg = pipe_conn.recv()
                if not isinstance(msg, dict):
                    continue
                cmd = msg.get('cmd', '')
                if cmd == 'stop':
                    app.quit()
                    return
                elif cmd == 'ensure_bottom':
                    widget.ensure_bottom()
                elif cmd == 'show_notification':
                    if notif_window:
                        notif_window.show()
                        notif_window._apply_style()
                elif cmd == 'hide_notification':
                    if notif_window:
                        notif_window.hide()
                elif cmd == 'toggle_notification':
                    if notif_window:
                        if notif_window.isVisible():
                            notif_window.hide()
                        else:
                            notif_window.show()
                            notif_window._apply_style()
                elif cmd == 'refresh_notifications':
                    # Legacy: no-op, data is pushed via update_notifications
                    pass
                elif cmd == 'update_notifications':
                    if notif_window:
                        data = msg.get('data', [])
                        notif_window.set_notifications_data(data)
            except EOFError:
                app.quit()
                return
            except Exception as e:
                print(f"[QtBackdrop] Pipe poll error: {e}", flush=True)

    poll_timer = QTimer()
    poll_timer.timeout.connect(poll_pipe)
    poll_timer.start(100)

    bottom_timer = QTimer()
    bottom_timer.timeout.connect(widget.ensure_bottom)
    bottom_timer.start(2000)

    def _ensure_notif_topmost():
        if notif_window and notif_window.isVisible():
            notif_window.ensure_topmost()

    notif_timer = QTimer()
    notif_timer.timeout.connect(_ensure_notif_topmost)
    notif_timer.start(3000)

    def _ensure_dock_topmost():
        if dock_window:
            dock_window._apply_win32_style()

    dock_timer = QTimer()
    dock_timer.timeout.connect(_ensure_dock_topmost)
    dock_timer.start(3000)

    sys.exit(app.exec())


class QtBackdropManager:
    """管理Qt背景窗口进程的管理器，在主进程中使用。"""

    def __init__(self, config_path=None, device_serial=None, adb_path="adb"):
        self._proc = None
        self._parent_conn = None
        self._child_conn = None
        self._config_path = config_path
        self._backdrop_hwnd = None
        self._notif_hwnd = None
        self._dock_hwnd = None
        self._ready = False
        self._notif_timer = None
        self._notif_parser = None
        self._device_serial = device_serial
        self._adb_path = adb_path
        self._notif_refresh_interval = 3
        self._hide_signal = False
        self._listener_thread = None

    def start(self):
        if os.name != 'nt':
            print("[QtBackdrop] Not on Windows, skipping backdrop", flush=True)
            return False

        if self._proc and self._proc.is_alive():
            print("[QtBackdrop] Already running", flush=True)
            return True

        try:
            multiprocessing.freeze_support()
            self._parent_conn, self._child_conn = multiprocessing.Pipe(duplex=True)
            self._proc = multiprocessing.Process(
                target=_run_backdrop_process,
                args=(self._child_conn, self._config_path),
                daemon=True,
            )
            self._proc.start()

            if self._parent_conn.poll(timeout=10):
                msg = self._parent_conn.recv()
                if msg.get('status') == 'ready':
                    self._backdrop_hwnd = msg.get('hwnd')
                    self._notif_hwnd = msg.get('notif_hwnd', 0)
                    self._dock_hwnd = msg.get('dock_hwnd', 0)
                    self._ready = True
                    print(f"[QtBackdrop] Started successfully, hwnd={self._backdrop_hwnd}, notif_hwnd={self._notif_hwnd}, dock_hwnd={self._dock_hwnd}", flush=True)
                    self._start_notification_refresh()
                    self._start_listener()
                    return True
                else:
                    err = msg.get('error', 'Unknown error')
                    print(f"[QtBackdrop] Failed to start: {err}", flush=True)
                    self._proc = None
                    return False
            else:
                print("[QtBackdrop] Timeout waiting for backdrop window", flush=True)
                self.stop()
                return False
        except Exception as e:
            print(f"[QtBackdrop] Start error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return False

    def _start_notification_refresh(self):
        """在主进程中启动定时通知获取（后台线程），通过Pipe推送给Qt子进程。"""
        try:
            from front.qt_notification import NotificationParser
        except ImportError:
            try:
                from qt_notification import NotificationParser
            except ImportError:
                print("[QtBackdrop] NotificationParser not available, skipping notification refresh", flush=True)
                return

        self._notif_parser = NotificationParser(
            device_serial=self._device_serial,
            adb_path=self._adb_path,
        )

        import threading

        def _refresh_loop():
            while self._ready and self._proc and self._proc.is_alive():
                try:
                    notifs = self._notif_parser.fetch_notifications()
                    dicts = [n.to_dict() for n in notifs]
                    if self._parent_conn and self._ready:
                        self._parent_conn.send({'cmd': 'update_notifications', 'data': dicts})
                except Exception as e:
                    print(f"[QtBackdrop] Notification fetch error: {e}", flush=True)
                for _ in range(int(self._notif_refresh_interval * 10)):
                    if not self._ready:
                        break
                    import time
                    time.sleep(0.1)

        t = threading.Thread(target=_refresh_loop, daemon=True)
        t.start()
        print(f"[QtBackdrop] Notification refresh started (every {self._notif_refresh_interval}s, main process ADB)", flush=True)

    def _start_listener(self):
        import threading
        def _listen_loop():
            while self._ready and self._proc and self._proc.is_alive():
                try:
                    if self._parent_conn and self._parent_conn.poll(timeout=0.1):
                        msg = self._parent_conn.recv()
                        if not isinstance(msg, dict):
                            continue
                        cmd = msg.get('cmd', '')
                        if cmd == 'toggle_edit_mode':
                            show = msg.get('show', False)
                            self._hide_signal = not show
                            try:
                                from front.card_window import get_card_window_decorator
                            except ImportError:
                                from card_window import get_card_window_decorator
                            card_mgr = get_card_window_decorator()
                            if card_mgr:
                                card_mgr.set_all_edit_mode(show)
                except EOFError:
                    break
                except Exception as e:
                    print(f"[QtBackdrop] Listener error: {e}", flush=True)
        self._listener_thread = threading.Thread(target=_listen_loop, daemon=True)
        self._listener_thread.start()
        print("[QtBackdrop] Dock command listener started", flush=True)

    def ensure_bottom(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'ensure_bottom'})
        except Exception:
            pass

    def show_notification(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'show_notification'})
        except Exception:
            pass

    def hide_notification(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'hide_notification'})
        except Exception:
            pass

    def toggle_notification(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'toggle_notification'})
        except Exception:
            pass

    def refresh_notifications(self):
        """立即在主进程中获取通知并推送给Qt子进程。"""
        if not self._ready or not self._notif_parser:
            return
        import threading
        def _do_refresh():
            try:
                notifs = self._notif_parser.fetch_notifications()
                dicts = [n.to_dict() for n in notifs]
                if self._parent_conn and self._ready:
                    self._parent_conn.send({'cmd': 'update_notifications', 'data': dicts})
            except Exception as e:
                print(f"[QtBackdrop] Manual notification refresh error: {e}", flush=True)
        threading.Thread(target=_do_refresh, daemon=True).start()

    def stop(self):
        self._ready = False
        if self._parent_conn:
            try:
                self._parent_conn.send({'cmd': 'stop'})
            except Exception:
                pass
        if self._proc and self._proc.is_alive():
            try:
                self._proc.join(timeout=3)
            except Exception:
                pass
            if self._proc.is_alive():
                try:
                    self._proc.terminate()
                except Exception:
                    pass
        self._proc = None
        self._backdrop_hwnd = None
        self._notif_hwnd = None
        self._dock_hwnd = None

    @property
    def is_running(self):
        return self._proc is not None and self._proc.is_alive() and self._ready

    @property
    def backdrop_hwnd(self):
        return self._backdrop_hwnd

    @property
    def notification_hwnd(self):
        return self._notif_hwnd

    @property
    def dock_hwnd(self):
        return self._dock_hwnd


_backdrop_manager = None


def get_backdrop_manager(config_path=None):
    global _backdrop_manager
    if _backdrop_manager is None:
        _backdrop_manager = QtBackdropManager(config_path=config_path)
    return _backdrop_manager
