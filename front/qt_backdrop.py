"""
Qt Backdrop Window - 透明容器窗口 + macOS风格通知/思考面板 + 公用 Dock。

使用PySide6创建一个无边框、全透明的窗口作为虚拟屏幕卡片的容器基底。
背景窗口本身是WS_EX_TOPMOST，但通过SetWindowPos(HWND_BOTTOM)置于topmost序列
最底部；scrcpy卡片窗口同样是WS_EX_TOPMOST（--always-on-top），自然浮在容器之上。
容器不再绘制任何底色（桌面壁纸直接透出）。

设计要点：
- 无边框、全透明（WA_TranslucentBackground），不绘制黑底/网格/提示文字
- 窗口不激活、不抢焦点、不出现在任务栏(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
- 窗口是topmost但位于topmost Z-order最底部(HWND_BOTTOM)，与scrcpy同层但在其下
- 不设置WS_EX_TRANSPARENT——点击空白区域被拦截但不处理
- scrcpy窗口保持WS_EX_TOPMOST，始终浮在容器之上
- 底部 Mac 风格 Dock 为公用组件 front/desktop_toolbar.DesktopToolbar
- 2秒定时器持续确保容器在Z-order底部，防止被其他topmost窗口挤到前面
- 运行在独立QApplication进程中，避免与Flask/主进程冲突
"""

import os
import sys
import ctypes
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

    # 声明 ConfigManager 身份：backdrop_manager 维护 notification_panel / thinking_log
    try:
        from scrcpycontrol.config_manager import ConfigManager, OWNER_BACKDROP_MANAGER
        mgr = ConfigManager.get(config_path=config_path) if config_path else ConfigManager.get()
        mgr.set_owner(OWNER_BACKDROP_MANAGER)
    except Exception as e:
        print(f"[QtBackdrop] WARNING: set_owner failed: {e}", flush=True)

    import ctypes
    user32 = ctypes.windll.user32

    try:
        from PySide6.QtWidgets import QApplication, QWidget
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

    try:
        from front.qt_thinking_log import create_thinking_log_panel
    except ImportError:
        try:
            from qt_thinking_log import create_thinking_log_panel
        except ImportError:
            create_thinking_log_panel = None
            print("[QtBackdrop] qt_thinking_log module not found, thinking panel disabled", flush=True)

    # 公用 Mac 风格 Dock（桌面 #0/#1 唯一绘制源，见 front/desktop_toolbar.py）
    bring_main_display_here = None
    try:
        from front.desktop_toolbar import DesktopToolbar
        from front.desktop_toolbar import bring_main_display_to_current_desktop \
            as bring_main_display_here
    except ImportError:
        try:
            from desktop_toolbar import DesktopToolbar
            from desktop_toolbar import bring_main_display_to_current_desktop \
                as bring_main_display_here
        except ImportError:
            DesktopToolbar = None
            print("[QtBackdrop] desktop_toolbar module not found, dock toolbar disabled",
                  flush=True)

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
        """透明背景容器窗口（不再绘制黑色底/网格/角标文字）。

        窗口本身仍然存在并保留全部原有窗口行为：topmost 置底、不抢焦点、
        被 VDM 感知、拦截空白区域点击；scrcpy 卡片与 Dock 浮于其上。"""

        def __init__(self, width=2560, height=1440):
            super().__init__()
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
            # 全透明：不绘制任何底色，桌面壁纸/其他窗口直接透出
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            self.setAutoFillBackground(False)
            self.setMouseTracking(True)

        def paintEvent(self, event):
            # 透明容器：不绘制任何内容（无黑底、无网格、无提示文字）
            return

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
                # ==============================================================
                # 【虚拟桌面强隔离·硬约束】绝对不能再 |= WS_EX_TOOLWINDOW 或
                # &= ~WS_EX_APPWINDOW 。这两个组合会让窗口从 Windows
                # IApplicationView 注册表被移除，VirtualDesktopManager 对它的
                # GetViewForHwnd() 永远返回 MK_E_ELEMENTNOTFOUND（"找不到元素"），
                # 结果就是 VDM **完全不感知这扇窗** → 窗口被当作全局浮动窗，
                # "显示在每一个虚拟桌面上"。即用户反馈："每个桌面都有黑色
                # 背景 + 通知卡片 + 思考卡片"，其实只是一套，但三个桌面都
                # 看得见，不是被复制了三份。
                #
                # 保留：NOACTIVATE/NOPARENTNOTIFY/TOPMOST（仍然不抢焦点、
                #       Topmost 置于最上层）。
                # 主动加上 WS_EX_APPWINDOW（强制让 shell 把它当成一颗"应用
                #       窗口缩略图"，这样 VDM 才分配 desktop GUID 绑定）。
                # 清除：WS_EX_TOOLWINDOW（若前面 Qt 默认 Tool 设了也清）
                # ==============================================================
                new_ex = (
                    ex_style
                    | WS_EX_NOACTIVATE
                    | WS_EX_NOPARENTNOTIFY
                    | WS_EX_TOPMOST
                    | WS_EX_APPWINDOW
                )
                new_ex &= ~WS_EX_TOOLWINDOW
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

    # DockToolbarWidget 已抽离为公用组件 front/desktop_toolbar.DesktopToolbar：
    # 主桌面 #0（本进程）与 UU 桌面 #1（front/uu_dock.py）共用同一份 Qt 绘制代码，
    # 「设置/电脑」动作通过回调注入，不再在此维护第二份实现。

    # ──────────────────────────────────────────────────────────────────
    # CATCH-ALL wrapper so any error in the Qt child process is reported
    # back to the parent via pipe instead of crashing silently.
    # ──────────────────────────────────────────────────────────────────
    import traceback as _tb

    def _inner_main():
        # Original (unmodified) Qt UI bootstrap + event loop lives here.
        # Defining as an inner function avoids re-indenting 200+ existing lines.
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        bg_w = 2560
        bg_h = 2080
        notif_cfg = None
        thinking_cfg = None  # ← 提前初始化：即使 config 不存在 / 解析失败也不会 NameError

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
                thinking_cfg = cfg.get('thinking_log', None)
                if thinking_cfg:
                    all_windows.append(thinking_cfg)
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
            # ── default_load 开关：default_load=false 时不创建通知面板窗口卡片 ──
            notif_default_load = bool(notif_cfg.get("default_load", True))
            if not notif_default_load:
                print("[QtBackdrop] ⏭️  notification_panel.default_load=false "
                      "→ SKIP creating notification window", flush=True)
                notif_window = None
            else:
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
    
        thinking_window = None
        if create_thinking_log_panel and thinking_cfg:
            # ── default_load 开关：default_load=false 时不创建思考日志窗口卡片 ──
            thinking_default_load = bool(thinking_cfg.get("default_load", True))
            if not thinking_default_load:
                print("[QtBackdrop] ⏭️  thinking_log.default_load=false "
                      "→ SKIP creating thinking log window", flush=True)
                thinking_window = None
            else:
                try:
                    thinking_window = create_thinking_log_panel(
                        config=thinking_cfg, cfg_path=config_path
                    )
                    if thinking_window:
                        thinking_window.show()
                        QTimer.singleShot(100, thinking_window._apply_style)
                        QTimer.singleShot(500, thinking_window._apply_style)
                        print("[QtBackdrop] Thinking log panel created", flush=True)
                except Exception as e:
                    print(f"[QtBackdrop] Failed to create thinking panel: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
                    thinking_window = None
    
        dock_window = None
        if DesktopToolbar is not None:
            edit_mode_shown = {"v": False}

            def _on_dock_settings():
                """主桌面 #0「设置」：切换全部卡片编辑模式，并联动通知/思考窗。
                行为与原内嵌 DockToolbar._toggle_edit_mode 完全一致。"""
                edit_mode_shown["v"] = not edit_mode_shown["v"]
                show = edit_mode_shown["v"]
                if pipe_conn:
                    try:
                        pipe_conn.send({'cmd': 'toggle_edit_mode', 'show': show})
                    except Exception:
                        pass
                if notif_window:
                    try:
                        notif_window._set_edit_mode(show)
                        notif_window.update()
                    except Exception as e:
                        print(f"[DockToolbar] Toggle notif edit error: {e}", flush=True)
                if thinking_window:
                    try:
                        thinking_window._set_edit_mode(show)
                        thinking_window.update()
                    except Exception as e:
                        print(f"[DockToolbar] Toggle thinking edit error: {e}", flush=True)
                dock_window.set_settings_active(show)

            def _on_dock_computer():
                """主桌面 #0「电脑」：切到 UU 远程桌面（.vd_state.json: uu_desktop）。"""
                try:
                    try:
                        state_path = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            '.vd_state.json')
                        with open(state_path, 'r', encoding='utf-8') as f:
                            idx = int(json.load(f).get('uu_desktop', 1))
                    except Exception:
                        idx = 1
                    from pyvda import get_virtual_desktops
                    desktops = get_virtual_desktops()
                    if 0 <= idx < len(desktops):
                        desktops[idx].go()
                        print(f"[DockToolbar] 已切换到 UU 远程桌面 #{idx}", flush=True)
                    else:
                        print(f"[DockToolbar] UU 桌面 #{idx} 越界（当前共 {len(desktops)} 个虚拟桌面），未切换。",
                              flush=True)
                except Exception as e:
                    print(f"[DockToolbar] 切换 UU 桌面失败: {e}", flush=True)

            def _on_dock_phone():
                """主桌面 #0「手机」：把手机主屏（main_display）嵌入当前所在桌面。
                在 #0 点＝留在/搬回 #0；用户切到 #1 用 UU 回程 Dock 点则嵌到 #1。"""
                if bring_main_display_here is None:
                    print("[DockToolbar] 手机动作不可用（desktop_toolbar 未加载）",
                          flush=True)
                    return
                bring_main_display_here()

            try:
                dock_window = DesktopToolbar(
                    window_title="AR Dock",
                    on_settings=_on_dock_settings,
                    on_phone=_on_dock_phone,
                    on_computer=_on_dock_computer,
                )
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
    
        bg_hwnd = _safe_hwnd(widget.winId())
        notif_hwnd = _safe_hwnd(notif_window.winId()) if notif_window else 0
        thinking_hwnd = _safe_hwnd(thinking_window.winId()) if thinking_window else 0
        dock_hwnd = _safe_hwnd(dock_window.winId()) if dock_window else 0
        pipe_conn.send({
            'status': 'ready',
            'hwnd': bg_hwnd,
            'notif_hwnd': notif_hwnd,
            'thinking_hwnd': thinking_hwnd,
            'dock_hwnd': dock_hwnd,
        })
    
        def poll_pipe():
            nonlocal widget, notif_window, thinking_window, dock_window
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
                    elif cmd == 'show_thinking':
                        if thinking_window:
                            thinking_window.show()
                            thinking_window._apply_style()
                    elif cmd == 'hide_thinking':
                        if thinking_window:
                            thinking_window.hide()
                    elif cmd == 'toggle_thinking':
                        if thinking_window:
                            if thinking_window.isVisible():
                                thinking_window.hide()
                            else:
                                thinking_window.show()
                                thinking_window._apply_style()
                    elif cmd == 'append_thinking':
                        if thinking_window:
                            data = msg.get('data') or {}
                            if isinstance(data, str):
                                thinking_window.append_log(data)
                            elif isinstance(data, dict):
                                thinking_window.append_log(
                                    data.get('message', ''),
                                    step=data.get('step'),
                                    source=data.get('source'),
                                    tag=data.get('tag'),
                                    timestamp=bool(data.get('timestamp', True)),
                                )
                    elif cmd == 'clear_thinking':
                        if thinking_window:
                            thinking_window.clear_log()
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
    
        def _ensure_thinking_topmost():
            if thinking_window and thinking_window.isVisible():
                thinking_window.ensure_topmost()
    
        thinking_timer = QTimer()
        thinking_timer.timeout.connect(_ensure_thinking_topmost)
        thinking_timer.start(3000)
    
        def _ensure_dock_topmost():
            if dock_window:
                dock_window._apply_win32_style()
    
        dock_timer = QTimer()
        dock_timer.timeout.connect(_ensure_dock_topmost)
        dock_timer.start(3000)
    
        sys.exit(app.exec())
        # ── end of _inner_main ──

    try:
        _inner_main()
    except Exception as _proc_err:
        _err_str = _tb.format_exc()
        print(f"[QtBackdrop] CHILD PROCESS CRASHED:\n{_err_str}", file=sys.stderr, flush=True)
        try:
            pipe_conn.send({'status': 'error', 'error': _err_str})
        except Exception:
            pass



class QtBackdropManager:
    """管理Qt背景窗口进程的管理器，在主进程中使用。"""

    def __init__(self, config_path=None, device_serial=None, adb_path="adb"):
        self._proc = None
        self._parent_conn = None
        self._child_conn = None
        self._config_path = config_path
        self._backdrop_hwnd = None
        self._notif_hwnd = None
        self._thinking_hwnd = None
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
                    self._thinking_hwnd = msg.get('thinking_hwnd', 0)
                    self._dock_hwnd = msg.get('dock_hwnd', 0)
                    self._ready = True
                    print(f"[QtBackdrop] Started successfully, hwnd={self._backdrop_hwnd}, notif_hwnd={self._notif_hwnd}, thinking_hwnd={self._thinking_hwnd}, dock_hwnd={self._dock_hwnd}", flush=True)
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

    # ==== Thinking Log Window Control (和通知窗口完全一致：同样受 backdrop_manager 管控) ====

    @property
    def thinking_hwnd(self):
        return self._thinking_hwnd

    def show_thinking(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'show_thinking'})
        except Exception:
            pass

    def hide_thinking(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'hide_thinking'})
        except Exception:
            pass

    def toggle_thinking(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'toggle_thinking'})
        except Exception:
            pass

    def append_thinking_log(self, message, step=None, source=None, tag=None, timestamp=True):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({
                'cmd': 'append_thinking',
                'data': {
                    'message': message,
                    'step': step,
                    'source': source,
                    'tag': tag,
                    'timestamp': timestamp,
                },
            })
        except Exception:
            pass

    def clear_thinking(self):
        if not self._ready or not self._parent_conn:
            return
        try:
            self._parent_conn.send({'cmd': 'clear_thinking'})
        except Exception:
            pass

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
    else:
        # 单例已存在，但调用方新传入了有效 config_path 且原先保存的是 None（典型场景：
        # console_tee 早期先触发 get_backdrop_manager() 无参创建，后续 start_backdrop_window()
        # 才传正确路径）→ 把正确路径补进单例，避免子进程收到 None 导致窗口不加载
        if (config_path is not None) and (_backdrop_manager._config_path is None):
            old = _backdrop_manager._config_path
            _backdrop_manager._config_path = config_path
            print(f"[QtBackdrop] get_backdrop_manager: filled singleton config_path (was {old!r}, now {config_path!r})", flush=True)
    return _backdrop_manager
