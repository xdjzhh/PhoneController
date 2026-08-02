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
from ctypes import wintypes
import multiprocessing
import json


def _run_backdrop_process(pipe_conn, config_path=None):
    """在独立进程中运行Qt背景窗口 + macOS风格通知面板。"""
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
        from PySide6.QtGui import QPainter, QColor, QFont, QPen
        from PySide6.QtCore import Qt, QTimer
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

    bg_hwnd = _safe_hwnd(widget.winId())
    notif_hwnd = _safe_hwnd(notif_window.winId()) if notif_window else 0
    pipe_conn.send({'status': 'ready', 'hwnd': bg_hwnd, 'notif_hwnd': notif_hwnd})

    def poll_pipe():
        nonlocal widget, notif_window
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
        self._ready = False
        self._notif_timer = None
        self._notif_parser = None
        self._device_serial = device_serial
        self._adb_path = adb_path
        self._notif_refresh_interval = 3

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
                    self._ready = True
                    print(f"[QtBackdrop] Started successfully, hwnd={self._backdrop_hwnd}, notif_hwnd={self._notif_hwnd}", flush=True)
                    self._start_notification_refresh()
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

    @property
    def is_running(self):
        return self._proc is not None and self._proc.is_alive() and self._ready

    @property
    def backdrop_hwnd(self):
        return self._backdrop_hwnd

    @property
    def notification_hwnd(self):
        return self._notif_hwnd


_backdrop_manager = None


def get_backdrop_manager(config_path=None):
    global _backdrop_manager
    if _backdrop_manager is None:
        _backdrop_manager = QtBackdropManager(config_path=config_path)
    return _backdrop_manager
