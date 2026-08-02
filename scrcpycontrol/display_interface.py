"""
Display Interface - Unified virtual display management interface.

Provides a clean Python API for managing scrcpy virtual displays without Flask.
Used by both the HTTP display server and the phone agent directly.

Features:
- Main display and app virtual display management
- Screen power control via scrcpy-client (not hotkey)
- Screen lock monitor with auto-unlock and screen-off after unlock
"""

import atexit
import json
import os
import shutil
import sys
import threading
import time

# Frontend card window styling (traffic lights, drag, resize, DWM rounded corners + shadow)
# and WindowConfig VO for clean separation of display configuration from style
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .multi_virtualdisplay import MainDisplay, MultiVirtualDisplay, AppVirtualDisplay
from .navigation_monitor import get_navigation_monitor, MAP_APPS
from front.window_config import WindowConfig


class DisplayManager:
    """Unified display manager for main display and app virtual displays."""

    def __init__(self, config_path=None, scrcpy_path=None, device_serial=None):
        """
        Initialize DisplayManager.

        Args:
            config_path: Path to display_config.json. Auto-detected if None.
            scrcpy_path: Path to scrcpy executable. Auto-detected if None.
            device_serial: ADB device serial. Auto-detected if None.
        """
        self._script_dir = os.path.dirname(os.path.abspath(__file__))
        if config_path is None:
            config_path = os.path.join(self._script_dir, "display_config.json")
        self.config_path = config_path
        self._config = None

        if scrcpy_path is None:
            scrcpy_path = self._find_scrcpy()
        self.scrcpy_path = scrcpy_path

        if device_serial is None:
            device_serial = self._detect_device()
        self.device_serial = device_serial

        self._main_display = None
        self._main_display_running = False
        self._main_rotation = 0
        self._multi_display = None
        self._app_display_map = {}
        self._app_orientation = {}
        self._lock = threading.Lock()
        self._initialized = False

        self._screen_monitor_thread = None
        self._screen_monitor_stop = False
        self._screen_monitor_running = False

        # Navigation monitor for map auto-zoom
        self._nav_monitor = None
        self._navigating_app = None  # 当前正在导航的地图app包名
        self._app_saved_geometry = {}  # 保存导航前的窗口几何，用于恢复

        atexit.register(self.stop_all)

    def _find_scrcpy(self):
        """Find scrcpy executable path."""
        scrcpy = shutil.which("scrcpy")
        if scrcpy:
            return scrcpy

        common_paths = [
            "D:/scrcpy-win64-v4.1/scrcpy.exe",
            "C:/Program Files/scrcpy/scrcpy.exe",
            "C:/Program Files (x86)/scrcpy/scrcpy.exe",
            os.path.expanduser("~/AppData/Local/Programs/scrcpy/scrcpy.exe"),
        ]

        for path in common_paths:
            if os.path.exists(path):
                return path

        raise FileNotFoundError("scrcpy not found.")

    def _detect_device(self):
        """Detect first connected ADB device."""
        try:
            import adbutils
            devices = adbutils.adb.device_list()
            if devices:
                return devices[0].serial
        except Exception:
            pass
        return None

    def _get_adb_device(self):
        """Get adbutils device object."""
        try:
            import adbutils
            devices = adbutils.adb.device_list()
            if devices:
                return devices[0]
        except Exception:
            pass
        return None

    def load_config(self):
        """Load display configuration from JSON file."""
        if self._config is None:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        return self._config

    def reload_config(self):
        """Force reload configuration from file."""
        self._config = None
        return self.load_config()

    def save_config(self):
        """Save current configuration back to JSON file."""
        if self._config is not None:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)

    @property
    def config(self):
        """Get config property - loaded configuration."""
        return self.load_config()

    def start_main_display(self, rotation=None):
        """
        Start the main display (phone screen mirror).

        Args:
            rotation: Screen rotation (0, 1, 2, 3). None = use current setting.

        Returns:
            Dict with success status and display info.
        """
        with self._lock:
            return self._start_main_display_locked(rotation)

    def _start_main_display_locked(self, rotation=None):
        """Start main display assuming lock is already held."""
        if rotation is None:
            rotation = self._main_rotation

        if self._main_display_running and self._main_display and self._main_display.running:
            if hasattr(self._main_display.config, 'rotation') and self._main_display.config.rotation == rotation:
                return {
                    "success": True,
                    "message": "Main display already running",
                    "display_type": "main",
                    "rotation": rotation,
                }
            else:
                print(f"[DisplayManager] Main display rotation mismatch ({rotation}), restarting...")
                self._stop_main_display_locked()

        cfg = self.load_config()
        main_cfg = cfg.get("main_display", {})

        # Build WindowConfig VO from config dict
        win_config = WindowConfig.from_dict(main_cfg, is_main=True)

        # Apply rotation to window dimensions (landscape swaps w/h)
        if rotation in (90, 270):
            win_config.swap_dimensions_for_orientation("landscape")
            win_config.rotation = rotation
            print(f"[DisplayManager] Landscape mode: window size {win_config.window_width}x{win_config.window_height}")

        try:
            self._main_display = MainDisplay(
                config=win_config,
                scrcpy_path=self.scrcpy_path,
                device_serial=self.device_serial,
                display_manager=self,
            )

            print(f"[DisplayManager] Starting main display (rotation={rotation})...")
            self._main_display.start()
            time.sleep(2)

            if self._main_display.running:
                self._main_display_running = True
                self._main_rotation = rotation
                self._initialized = True
                print("[DisplayManager] Main display started successfully")
                # Start navigation monitor after main display is up
                self._start_navigation_monitor()
                return {
                    "success": True,
                    "message": "Main display started",
                    "display_type": "main",
                    "window_title": win_config.window_title,
                    "rotation": rotation,
                }
            else:
                self._main_display = None
                return {
                    "success": False,
                    "error": "Main display failed to start",
                }

        except Exception as e:
            print(f"[DisplayManager] Failed to start main display: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def _stop_main_display_locked(self):
        """Stop main display assuming lock is already held."""
        if self._main_display:
            try:
                self._main_display.stop()
            except Exception:
                pass
            self._main_display = None
            self._main_display_running = False
            time.sleep(0.5)

    def _get_multi_display(self):
        """Get or create MultiVirtualDisplay instance."""
        if self._multi_display is None:
            self._multi_display = MultiVirtualDisplay(
                device_serial=self.device_serial,
                scrcpy_path=self.scrcpy_path,
                turn_screen_off=True,
                keep_active=True,
            )
        return self._multi_display

    def start_app_display(self, app_package, orientation=None):
        """
        Start a virtual display for an app.

        If the app is already running, returns immediately without creating a new one.

        Args:
            app_package: Android app package name.
            orientation: Display orientation - "portrait", "landscape", or None to use config default.

        Returns:
            Dict with success status and display info.
        """
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {}) or {}

        # 若未显式指定orientation，则使用config中的default_orientation，默认portrait
        if orientation is None:
            orientation = app_cfg.get("default_orientation", "portrait")

        if orientation not in ("portrait", "landscape"):
            return {"success": False, "error": "orientation must be 'portrait' or 'landscape'"}

        with self._lock:
            if app_package in self._app_display_map:
                display = self._app_display_map[app_package]
                if display.running:
                    current_orient = self._app_orientation.get(app_package, "portrait")
                    if current_orient == orientation:
                        print(f"[DisplayManager] App {app_package} already running ({orientation})")
                        self.activate_app_window(app_package)
                        return {
                            "success": True,
                            "message": "App display already running",
                            "display_type": "virtual",
                            "app_package": app_package,
                            "window_title": display.window_title,
                            "orientation": orientation,
                        }
                    else:
                        print(f"[DisplayManager] App {app_package} running but orientation mismatch (current={current_orient}, target={orientation}), restarting...")
                        stopped = self._stop_app_display_locked(app_package)
                        print(f"[DisplayManager] Old display stopped result: {stopped}")
                        time.sleep(3.0)  # 等待旧scrcpy进程退出、端口释放、Android端虚拟屏销毁
                else:
                    del self._app_display_map[app_package]

            if not app_cfg:
                print(f"[DisplayManager] No display config for {app_package}, using main display")
                if not self._main_display_running:
                    self._start_main_display_locked()
                return {
                    "success": True,
                    "message": "Using main display (no virtual display config)",
                    "display_type": "main",
                    "app_package": app_package,
                }

            win_w = app_cfg.get("window_width", 540)
            win_h = app_cfg.get("window_height", 960)

            # ===== Build WindowConfig VO and resolve virtual display size =====
            # Mac card style is always applied (hardcoded in AppVirtualDisplay).
            # WindowConfig.resolve_virtual_display_size automatically matches
            # VD resolution to window aspect ratio to avoid letterbox black bars.
            win_config = WindowConfig.from_dict(app_cfg, is_main=False)
            win_config.default_orientation = orientation

            # Ensure window dimensions match the requested orientation
            win_config.swap_dimensions_for_orientation(orientation)

            # Resolve correct VD width/height/DPI based on window aspect ratio
            nd_w, nd_h, nd_dpi = win_config.resolve_virtual_display_size(orientation)
            win_config.new_display_width = nd_w
            win_config.new_display_height = nd_h
            win_config.new_display_dpi = nd_dpi
            win_config.rotation = 0
            win_config.user_rotation = 0

            # Determine orientation description for debug output
            is_landscape_vd = nd_w > nd_h
            nd_orient_desc = f"{nd_w}x{nd_h}/{nd_dpi or 'default'}dpi({'横屏' if is_landscape_vd else '竖屏' if win_config.window_width < win_config.window_height else '正方形'}natural)"

            mvd = self._get_multi_display()

            try:
                print(f"[DisplayManager] ========== 创建{orientation}虚拟屏参数 ==========")
                print(f"[DisplayManager] app_package           : {app_package}")
                print(f"[DisplayManager] window 尺寸           : {win_config.window_width}x{win_config.window_height} ({'横屏✅' if win_config.window_width > win_config.window_height else '竖屏'})")
                print(f"[DisplayManager] new_display (安卓)    : {nd_orient_desc}")
                print(f"[DisplayManager] scrcpy rotation       : 0 (不做编码旋转)")
                print(f"[DisplayManager] wm user-rotation      : lock 0 (natural方向，防FLAG_ROTATES_WITH_CONTENT)")
                print(f"[DisplayManager] =================================================")

                display = AppVirtualDisplay(
                    app_package=app_package,
                    config=win_config,
                    display_id=mvd._alloc_display_id(),
                    scrcpy_path=self.scrcpy_path,
                    device_serial=self.device_serial,
                    base_port=mvd.base_port,
                    display_manager=self,
                )

                mvd.displays.append(display)
                self._app_display_map[app_package] = display
                self._app_orientation[app_package] = orientation

                print(f"[DisplayManager] Starting virtual display for {app_package} ({orientation})...")
                display.start()
                time.sleep(2)

                if display.running:
                    self.activate_app_window(app_package)
                    # 如果该app是地图app且当前正在导航中，自动切换到导航模式
                    if self._nav_monitor and app_package in MAP_APPS:
                        nav_state = self._nav_monitor.get_state()
                        if nav_state.is_navigating and nav_state.app_package == app_package:
                            # 延迟一点时间等待窗口完全创建
                            def _delayed_switch():
                                time.sleep(1.5)
                                self._switch_app_to_nav_mode(app_package)
                                self._navigating_app = app_package
                            threading.Thread(target=_delayed_switch, daemon=True).start()
                    return {
                        "success": True,
                        "message": f"App virtual display started ({orientation})",
                        "display_type": "virtual",
                        "app_package": app_package,
                        "window_title": display.window_title,
                        "display_id": display.display_id,
                        "android_display_id": getattr(display, "android_display_id", None),
                        "orientation": orientation,
                    }
                else:
                    failed_id = display.display_id
                    if display in mvd.displays:
                        mvd.displays.remove(display)
                    mvd._free_display_id(failed_id)  # 启动失败，归还id
                    if app_package in self._app_display_map:
                        del self._app_display_map[app_package]
                    self._app_orientation.pop(app_package, None)
                    return {
                        "success": False,
                        "error": "App virtual display failed to start",
                    }

            except Exception as e:
                print(f"[DisplayManager] Failed to start app display: {e}")
                import traceback
                traceback.print_exc()
                # 异常情况下也归还id
                try:
                    if 'display' in dir() and display is not None:
                        fid = display.display_id
                        if display in mvd.displays:
                            mvd.displays.remove(display)
                        mvd._free_display_id(fid)
                except Exception:
                    pass
                return {"success": False, "error": str(e)}

    def stop_app_display(self, app_package):
        """
        Stop a virtual display for an app.

        Args:
            app_package: Android app package name.

        Returns:
            Dict with success status.
        """
        with self._lock:
            return self._stop_app_display_locked(app_package)

    def _stop_app_display_locked(self, app_package):
        """Stop app display assuming lock is already held."""
        if app_package not in self._app_display_map:
            return {
                "success": False,
                "error": f"No running display for {app_package}",
            }

        display = self._app_display_map[app_package]
        try:
            old_display_id = display.display_id
            display.stop()
            if self._multi_display:
                if display in self._multi_display.displays:
                    self._multi_display.displays.remove(display)
                self._multi_display._free_display_id(old_display_id)  # 归还id到空闲池，供后续安全复用
            del self._app_display_map[app_package]
            self._app_orientation.pop(app_package, None)
            print(f"[DisplayManager] Stopped virtual display for {app_package} (display_id={old_display_id} returned to pool)")
            time.sleep(0.5)
            return {"success": True, "message": "App display stopped"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_displays(self):
        """
        List all active displays.

        Returns:
            Dict with list of displays.
        """
        displays = []

        if self._main_display and self._main_display_running:
            displays.append({
                "type": "main",
                "running": self._main_display.running,
                "window_title": self._main_display.window_title,
                "app_package": None,
            })

        for pkg, display in self._app_display_map.items():
            displays.append({
                "type": "virtual",
                "running": display.running,
                "window_title": display.window_title,
                "app_package": pkg,
                "display_id": display.display_id,
                "android_display_id": getattr(display, "android_display_id", None),
            })

        return {"success": True, "displays": displays}

    def _set_screen_power_mode(self, mode):
        """
        Set screen power mode via scrcpy-client library.

        Args:
            mode: Power mode (POWER_MODE_OFF or POWER_MODE_NORMAL from scrcpy).

        Returns:
            (success: bool, message: str)
        """
        try:
            try:
                import scrcpy
                from scrcpy import Client
            except ImportError:
                return False, "scrcpy-client not installed. Run: pip install scrcpy-client"

            mode_name = "OFF" if mode == scrcpy.POWER_MODE_OFF else "NORMAL"
            print(f"[ScreenPower] Setting screen power mode to {mode_name} via scrcpy-client...")

            if self.device_serial:
                client = Client(device=self.device_serial)
            else:
                client = Client()

            client.start(threaded=True)
            time.sleep(1)

            client.control.set_screen_power_mode(mode)
            time.sleep(0.5)

            client.stop()

            print(f"[ScreenPower] Screen power mode set to {mode_name} via scrcpy-client")
            return True, f"Screen power mode set to {mode_name}"

        except Exception as e:
            print(f"[ScreenPower] Failed to set screen power mode: {e}")
            import traceback
            traceback.print_exc()
            return False, str(e)

    def screen_off(self):
        """
        Turn off the physical screen via scrcpy-client.

        Only turns off the backlight, does not trigger lock screen.

        Returns:
            Dict with success status.
        """
        try:
            import scrcpy
            success, message = self._set_screen_power_mode(scrcpy.POWER_MODE_OFF)
            return {
                "success": success,
                "message": message,
                "method": "scrcpy_client" if success else "failed",
            }
        except ImportError:
            return {
                "success": False,
                "error": "scrcpy-client not installed. Run: pip install scrcpy-client",
            }

    def screen_on(self):
        """
        Turn on the physical screen via scrcpy-client.

        Returns:
            Dict with success status.
        """
        try:
            import scrcpy
            success, message = self._set_screen_power_mode(scrcpy.POWER_MODE_NORMAL)
            return {
                "success": success,
                "message": message,
                "method": "scrcpy_client" if success else "failed",
            }
        except ImportError:
            return {
                "success": False,
                "error": "scrcpy-client not installed. Run: pip install scrcpy-client",
            }

    def _is_keyguard_locked(self, device=None, verbose=False):
        """
        Check if the keyguard (lock screen) is showing.

        Args:
            device: adbutils device object. Auto-detected if None.
            verbose: Print debug info.

        Returns:
            True if locked, False if unlocked.
        """
        if device is None:
            device = self._get_adb_device()
        if device is None:
            return False

        # Method 1: dumpsys window (most reliable)
        try:
            result = device.shell("dumpsys window | grep -i keyguard")
            if verbose:
                print(f"[ScreenMonitor] dumpsys window keyguard:\n{result}")

            if "isKeyguardShowing=true" in result:
                return True
            if "mIsShowing=true" in result:
                return True
            for line in result.split('\n'):
                line = line.strip()
                if line.startswith('showing=') and 'showing=true' in line:
                    return True
                if line == 'showing=true':
                    return True

            if "isKeyguardShowing=false" in result:
                return False
            if "mIsShowing=false" in result:
                return False
        except Exception as e:
            print(f"[ScreenMonitor] dumpsys window keyguard failed: {e}")

        # Method 2: dumpsys keyguard
        try:
            result = device.shell("dumpsys keyguard")
            if verbose:
                lines = result.strip().split('\n')
                print(f"[ScreenMonitor] dumpsys keyguard first 20 lines:")
                for line in lines[:20]:
                    if line.strip():
                        print(f"  {line.strip()}")

            if "mShowing=true" in result:
                return True
            if "isShowing=true" in result:
                return True
            if "mShowing=false" in result:
                return False
        except Exception as e:
            if verbose:
                print(f"[ScreenMonitor] dumpsys keyguard failed: {e}")

        return False

    def _get_window_title_for_app(self, app_package):
        """Get window title for an app virtual display (alias for _get_window_title)."""
        return self._get_window_title(app_package)

    def _find_window_by_title(self, title):
        """Find window handle by title using ctypes. Uses EnumWindows for fuzzy match."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            result = [None]

            def enum_windows_proc(hwnd, lparam):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    win_title = buf.value
                    # Exact match first
                    if win_title == title:
                        if user32.IsWindowVisible(hwnd):
                            result[0] = hwnd
                            return 0
                    # Fuzzy match: window title contains the target title (handles prefixes like [scrcpy])
                    if title in win_title and user32.IsWindowVisible(hwnd):
                        result[0] = hwnd
                return 1

            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)
            return result[0]
        except Exception:
            return None

    def _force_activate_window(self, hwnd):
        """Force activate a window (bypass Windows foreground lock)."""
        if not hwnd:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32

            if not user32.IsWindow(hwnd):
                return False

            if not user32.IsWindowVisible(hwnd):
                try:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    time.sleep(0.1)
                except:
                    pass

            current_fg = user32.GetForegroundWindow()
            if current_fg == hwnd:
                return True

            try:
                import win32gui
                import win32con
                import win32api

                try:
                    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                    time.sleep(0.05)
                    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                    time.sleep(0.05)
                except:
                    pass

                try:
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        time.sleep(0.1)
                except:
                    pass

                try:
                    win32gui.BringWindowToTop(hwnd)
                except:
                    pass

                try:
                    win32gui.SetForegroundWindow(hwnd)
                except:
                    pass

                time.sleep(0.2)

                new_fg = win32gui.GetForegroundWindow()
                return new_fg == hwnd
            except ImportError:
                try:
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.2)
                    return user32.GetForegroundWindow() == hwnd
                except:
                    return False
        except Exception:
            return False

    def set_display_orientation(self, app_package=None, orientation="portrait"):
        """
        Set the orientation of a display (main or virtual).

        - For main display: uses scrcpy --rotation (0=portrait, 1=landscape)
        - For virtual display: stops and recreates with swapped width/height

        Args:
            app_package: App package name. None or "main" means main display.
            orientation: "portrait" or "landscape".

        Returns:
            Dict with success status and display info.
        """
        if orientation not in ("portrait", "landscape"):
            return {"success": False, "error": "orientation must be 'portrait' or 'landscape'"}

        is_main = (app_package is None or app_package == "main")

        if is_main:
            target_rotation = 270 if orientation == "landscape" else 0
            print(f"[DisplayManager] Setting main display to {orientation} (rotation={target_rotation})...")
            return self.start_main_display(rotation=target_rotation)

        print(f"[DisplayManager] Setting {app_package} to {orientation} mode...")
        return self.start_app_display(app_package, orientation=orientation)

    def set_window_opacity(self, app_package=None, opacity=1.0):
        """
        Set window transparency/opacity for an app or main display.

        Args:
            app_package: App package name. None means main display.
            opacity: Opacity value from 0.0 (fully transparent) to 1.0 (fully opaque).

        Returns:
            Dict with success status.
        """
        if not (0.0 <= opacity <= 1.0):
            return {"success": False, "error": "Opacity must be between 0.0 and 1.0"}

        title = self._get_window_title(app_package)
        hwnd = self._find_window_by_title(title)
        if not hwnd:
            return {"success": False, "error": f"Window not found: {title}"}

        try:
            self._apply_opacity_to_hwnd(hwnd, opacity)
            print(f"[DisplayManager] Window '{title}' opacity set to {opacity:.2f}")
            return {
                "success": True,
                "window_title": title,
                "opacity": opacity,
                "message": f"Opacity set to {opacity:.2f}",
            }
        except Exception as e:
            print(f"[DisplayManager] Failed to set window opacity: {e}")
            return {"success": False, "error": str(e)}

    def _apply_opacity_to_hwnd(self, hwnd, opacity):
        """Apply opacity to a specific HWND using SetLayeredWindowAttributes."""
        import ctypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        LWA_ALPHA = 0x00000002

        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex_style & WS_EX_LAYERED):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)

        alpha = int(opacity * 255)
        result = user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
        if not result:
            raise ctypes.WinError()

    def _move_and_resize_hwnd(self, hwnd, x, y, w, h):
        """Move and resize a window using MoveWindow Win32 API."""
        import ctypes
        user32 = ctypes.windll.user32
        result = user32.MoveWindow(ctypes.wintypes.HWND(hwnd), int(x), int(y), int(w), int(h), True)
        if not result:
            raise ctypes.WinError()
        # 同步更新overlay窗口位置（card decorator）
        try:
            from front.card_window import get_card_window_decorator
            decorator = get_card_window_decorator()
            decorator.refresh_card_overlay(int(hwnd))
        except Exception:
            pass

    def _get_app_hwnd(self, app_package):
        """Get the HWND of a running app display window."""
        if app_package is None:
            if self._main_display and self._main_display._hwnd:
                return self._main_display._hwnd
        elif app_package in self._app_display_map:
            display = self._app_display_map[app_package]
            if hasattr(display, '_hwnd') and display._hwnd:
                return display._hwnd
        # Fallback: find by window title
        title = self._get_window_title(app_package)
        return self._find_window_by_title(title)

    def _get_window_title(self, app_package=None):
        """Get window title for a display (main or app)."""
        cfg = self.load_config()
        if app_package is None:
            return cfg.get("main_display", {}).get("window_title", "手机桌面")
        return cfg.get("app_displays", {}).get(app_package, {}).get("window_title", app_package)

    def _get_config_section(self, app_package=None):
        """Get the config dict section for a display."""
        cfg = self.load_config()
        if app_package is None:
            return cfg, "main_display", cfg.get("main_display", {})
        return cfg, ("app_displays", app_package), cfg.get("app_displays", {}).get(app_package, {})

    def _save_window_geometry(self, app_package, x, y, w, h):
        """Persist window geometry to display_config.json after drag/resize."""
        try:
            cfg = self.load_config()
            if app_package is None:
                if "main_display" not in cfg:
                    cfg["main_display"] = {}
                section = cfg["main_display"]
                title = section.get("window_title", "手机桌面")
            elif app_package == "notification_panel":
                if "notification_panel" not in cfg:
                    cfg["notification_panel"] = {}
                section = cfg["notification_panel"]
                title = section.get("window_title", "通知")
            else:
                if "app_displays" not in cfg:
                    cfg["app_displays"] = {}
                if app_package not in cfg["app_displays"]:
                    cfg["app_displays"][app_package] = {}
                section = cfg["app_displays"][app_package]
                title = section.get("window_title", app_package)

            section["window_x"] = x
            section["window_y"] = y
            section["window_width"] = w
            section["window_height"] = h
            self.save_config()
            print(f"[DisplayManager] Saved geometry for '{title}': {x},{y} {w}x{h}")
        except Exception as e:
            print(f"[DisplayManager] Failed to save geometry: {e}")

    def _apply_default_opacity_delayed(self, app_package=None, delay=3.5):
        """Apply configured default_opacity after a short delay (waiting for window to appear)."""
        def _apply():
            time.sleep(delay)
            try:
                cfg = self.load_config()
                if app_package is None:
                    opacity = cfg.get("main_display", {}).get("default_opacity", 1.0)
                    title = cfg.get("main_display", {}).get("window_title", "手机桌面")
                elif app_package == "notification_panel":
                    opacity = cfg.get("notification_panel", {}).get("default_opacity", 1.0)
                    title = cfg.get("notification_panel", {}).get("window_title", "通知")
                else:
                    opacity = cfg.get("app_displays", {}).get(app_package, {}).get("default_opacity", 1.0)
                    title = self._get_window_title(app_package)

                if opacity < 1.0:
                    hwnd = self._find_window_by_title(title)
                    if hwnd:
                        self._apply_opacity_to_hwnd(hwnd, opacity)
                        print(f"[DisplayManager] Default opacity applied to '{title}': {opacity}")
                    else:
                        print(f"[DisplayManager] Window not found for default opacity: {title}")
            except Exception as e:
                print(f"[DisplayManager] Failed to apply default opacity: {e}")

        t = threading.Thread(target=_apply, daemon=True)
        t.start()

    def _make_geometry_callback(self, app_package):
        """Create an on_geometry_changed callback bound to a specific app package."""
        manager = self
        def _cb(x, y, w, h):
            # 如果当前正在导航中，不保存普通几何（避免覆盖nav_geometry）
            if manager._navigating_app == app_package:
                return
            manager._save_window_geometry(app_package, x, y, w, h)
        return _cb

    def _start_navigation_monitor(self):
        """Start the navigation state monitor."""
        if self._nav_monitor is not None:
            return
        try:
            self._nav_monitor = get_navigation_monitor(
                device_serial=self.device_serial,
                poll_interval=3.0
            )
            self._nav_monitor.on_navigation_start(self._on_navigation_start)
            self._nav_monitor.on_navigation_end(self._on_navigation_end)
            self._nav_monitor.start()
        except Exception as e:
            print(f"[DisplayManager] Failed to start navigation monitor: {e}")
            self._nav_monitor = None

    def _stop_navigation_monitor(self):
        """Stop the navigation state monitor."""
        if self._nav_monitor:
            try:
                self._nav_monitor.stop()
            except Exception:
                pass
            self._nav_monitor = None
        # 如果正在导航，恢复窗口
        if self._navigating_app:
            try:
                self._restore_app_from_nav_mode(self._navigating_app)
            except Exception:
                pass
            self._navigating_app = None

    def _on_navigation_start(self, state):
        """Handle navigation start event - zoom map window to nav mode."""
        pkg = state.app_package
        if not pkg or pkg not in MAP_APPS:
            return

        # 如果已经在导航同一个app，忽略
        if self._navigating_app == pkg:
            return

        # 如果正在导航另一个app，先恢复它
        if self._navigating_app and self._navigating_app != pkg:
            try:
                self._restore_app_from_nav_mode(self._navigating_app)
            except Exception:
                pass

        print(f"[Navigation] {state.app_name} navigation started, switching map to full screen...")
        self._switch_app_to_nav_mode(pkg)
        self._navigating_app = pkg

    def _on_navigation_end(self, state):
        """Handle navigation end event - restore map window to normal size."""
        pkg = state.app_package
        if not pkg:
            return
        print(f"[Navigation] {state.app_name} navigation ended, restoring map window...")
        try:
            self._restore_app_from_nav_mode(pkg)
        except Exception as e:
            print(f"[Navigation] Failed to restore window: {e}")
        if self._navigating_app == pkg:
            self._navigating_app = None

    def _switch_app_to_nav_mode(self, app_package, retry_count=0):
        """Switch an app window to navigation mode (large centered)."""
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {})
        nav_geom = app_cfg.get("nav_geometry")
        if not nav_geom:
            print(f"[Navigation] No nav_geometry config for {app_package}, skipping")
            return

        hwnd = self._get_app_hwnd(app_package)
        if not hwnd:
            if retry_count < 10:
                # 延迟重试，等待窗口创建完成
                def _retry():
                    time.sleep(1.0)
                    self._switch_app_to_nav_mode(app_package, retry_count + 1)
                threading.Thread(target=_retry, daemon=True).start()
                print(f"[Navigation] Window not found for {app_package}, retry {retry_count+1}/10 in 1s...")
                return
            else:
                print(f"[Navigation] Window for {app_package} not found after retries, switching to nav mode later when window starts")
                return

        print(f"[Navigation] Found HWND {hwnd} for {app_package}, switching to nav mode...")

        # 保存当前几何信息用于恢复
        cur_rect = self._window_rect(hwnd)
        if cur_rect:
            cx, cy, cw, ch = cur_rect
            self._app_saved_geometry[app_package] = {
                "window_x": cx, "window_y": cy,
                "window_width": cw, "window_height": ch
            }
            print(f"[Navigation] Saved current geometry: {cx},{cy} {cw}x{ch}")

        # 切换到导航几何
        nx = nav_geom.get("window_x", 1027)
        ny = nav_geom.get("window_y", 543)
        nw = nav_geom.get("window_width", 2076)
        nh = nav_geom.get("window_height", 1193)
        nav_opacity = nav_geom.get("default_opacity", 1.0)

        try:
            self._move_and_resize_hwnd(hwnd, nx, ny, nw, nh)
            if nav_opacity < 1.0:
                self._apply_opacity_to_hwnd(hwnd, nav_opacity)
            else:
                # 导航时完全不透明
                self._apply_opacity_to_hwnd(hwnd, 1.0)
            # 激活窗口到最前
            self._force_activate_window(hwnd)
            print(f"[Navigation] Switched {app_package} to nav mode: {nx},{ny} {nw}x{nh}")
        except Exception as e:
            print(f"[Navigation] Failed to switch to nav mode: {e}")
            import traceback
            traceback.print_exc()

    def _restore_app_from_nav_mode(self, app_package):
        """Restore an app window from navigation mode to normal size."""
        hwnd = self._get_app_hwnd(app_package)
        if not hwnd:
            print(f"[Navigation] Window not found for {app_package} during restore")
            # 清除保存的几何
            self._app_saved_geometry.pop(app_package, None)
            return

        # 恢复到保存的几何或配置中的默认几何
        saved = self._app_saved_geometry.pop(app_package, None)
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {})

        if saved:
            rx, ry, rw, rh = saved["window_x"], saved["window_y"], saved["window_width"], saved["window_height"]
        else:
            rx = app_cfg.get("window_x", 60)
            ry = app_cfg.get("window_y", 60)
            rw = app_cfg.get("window_width", 960)
            rh = app_cfg.get("window_height", 540)

        normal_opacity = app_cfg.get("default_opacity", 1.0)

        try:
            self._move_and_resize_hwnd(hwnd, rx, ry, rw, rh)
            self._apply_opacity_to_hwnd(hwnd, normal_opacity)
            print(f"[Navigation] Restored {app_package} to normal mode: {rx},{ry} {rw}x{rh}")
        except Exception as e:
            print(f"[Navigation] Failed to restore window: {e}")

    def _window_rect(self, hwnd):
        """Get window rect (x, y, width, height) for a HWND."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if user32.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        return None

    def activate_app_window(self, app_package=None):
        """
        Activate (bring to front) the scrcpy window for an app or main display.

        Args:
            app_package: App package name. None means main display.

        Returns:
            Dict with success status.
        """
        if app_package is None:
            cfg = self.load_config()
            title = cfg.get("main_display", {}).get("window_title", "手机桌面")
        else:
            title = self._get_window_title_for_app(app_package)

        hwnd = self._find_window_by_title(title)
        if not hwnd:
            return {"success": False, "error": f"Window not found: {title}"}

        success = self._force_activate_window(hwnd)
        return {
            "success": success,
            "window_title": title,
            "message": "Window activated" if success else "Window activation failed",
        }

    def set_edit_mode(self, app_package=None, editing=True):
        """
        Toggle edit mode (show/hide traffic lights and resize hotzones) for a window.

        Args:
            app_package: App package name. None or "main" means main display.
            editing: True to enter edit mode, False to pin/fix.

        Returns:
            Dict with success status.
        """
        from front.card_window import get_card_window_decorator

        if app_package is None or app_package == "main":
            cfg = self.load_config()
            title = cfg.get("main_display", {}).get("window_title", "手机桌面")
            display_obj = self._main_display
        else:
            title = self._get_window_title_for_app(app_package)
            display_obj = self._app_display_map.get(app_package)

        if not display_obj:
            return {"success": False, "error": f"Display not found for: {app_package}"}

        hwnd = getattr(display_obj, '_hwnd', None)
        if not hwnd:
            hwnd = self._find_window_by_title(title)
        if not hwnd:
            return {"success": False, "error": f"Window not found: {title}"}

        try:
            decorator = get_card_window_decorator()
            decorator.set_edit_mode(hwnd, editing)
            return {
                "success": True,
                "window_title": title,
                "editing": editing,
                "message": f"Edit mode {'ON' if editing else 'OFF (pinned)'}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _monitor_screen_lock_loop(self):
        """Screen lock monitor thread main loop."""
        self.reload_config()
        cfg = self.load_config()
        lock_password = cfg.get('lock_password', '')

        if not lock_password:
            print("[ScreenMonitor] No lock password configured, skipping auto-unlock")
            self._screen_monitor_running = False
            return

        print(f"[ScreenMonitor] Starting screen lock monitor (password: {'*' * len(lock_password)})")

        device = self._get_adb_device()
        if not device:
            print("[ScreenMonitor] No ADB device found")
            self._screen_monitor_running = False
            return

        print(f"[ScreenMonitor] Device found: {device.serial}")

        grace_cycles = 3
        debug_printed = False
        last_locked_state = None

        while not self._screen_monitor_stop:
            try:
                verbose = not debug_printed
                is_locked = self._is_keyguard_locked(device, verbose=verbose)
                if not debug_printed:
                    debug_printed = True
                    print(f"[ScreenMonitor] Initial lock state: is_locked={is_locked}")

                try:
                    power_result = device.shell("dumpsys power | grep 'mWakefulness'")
                    is_asleep = "Asleep" in power_result
                except:
                    is_asleep = False

                if is_locked != last_locked_state:
                    print(f"[ScreenMonitor] Lock state changed: {last_locked_state} -> {is_locked}, is_asleep={is_asleep}")
                    last_locked_state = is_locked

                if grace_cycles > 0:
                    grace_cycles -= 1
                    if is_asleep and not is_locked:
                        print("[ScreenMonitor] Grace period: screen asleep but not locked (normal after startup)")
                        time.sleep(2)
                        continue

                if is_locked:
                    print("[ScreenMonitor] Device locked! Attempting to unlock...")
                    time.sleep(0.5)

                    # === 快速解锁方案（优先）：唤醒→input text→回车 ===
                    # 适用于密码框已可输入的场景（如刚锁屏、密码键盘已显示）
                    print("[ScreenMonitor] Waking up screen...")
                    device.shell("input keyevent KEYCODE_WAKEUP")
                    time.sleep(1.5)

                    print(f"[ScreenMonitor] Quick unlock: inputting password ({'*' * len(lock_password)})...")
                    device.shell(f"input text {lock_password}")
                    time.sleep(0.3)
                    device.shell("input keyevent KEYCODE_ENTER")
                    time.sleep(1.5)

                    keyguard_check = self._is_keyguard_locked(device)
                    if not keyguard_check:
                        print("[ScreenMonitor] Quick unlock succeeded!")
                    else:
                        # === 备用方案：上滑→逐位按键 ===
                        # 快速方案失败（通常是华为/鸿蒙锁屏遮罩遮挡了密码框），走完整流程
                        print("[ScreenMonitor] Quick unlock failed, trying swipe + keyevent fallback...")

                        # 获取屏幕分辨率计算滑动坐标
                        try:
                            size_output = device.shell("wm size").strip()
                            import re as _re
                            size_match = _re.search(r'(\d+)x(\d+)', size_output)
                            sw = int(size_match.group(1)) if size_match else 1080
                            sh = int(size_match.group(2)) if size_match else 1920
                        except Exception:
                            sw, sh = 1080, 1920

                        # 上滑显示密码键盘
                        print("[ScreenMonitor] Swiping up to show unlock keyboard...")
                        device.shell(f"input swipe {sw//2} {int(sh*0.85)} {sw//2} {int(sh*0.35)} 300")
                        time.sleep(1.5)

                        # 检查上滑是否直接解锁（智能锁/无密码场景）
                        keyguard_pre = self._is_keyguard_locked(device)
                        if not keyguard_pre:
                            print("[ScreenMonitor] Screen unlocked by swipe (smart lock / no PIN required)")
                        else:
                            # 输入密码/PIN
                            print(f"[ScreenMonitor] Fallback: inputting password/PIN via keyevents...")
                            if lock_password.isdigit():
                                for ch in lock_password:
                                    device.shell(f"input keyevent KEYCODE_{ch}")
                                    time.sleep(0.15)
                            else:
                                device.shell(f"input text {lock_password}")
                                time.sleep(0.5)
                                device.shell("input keyevent KEYCODE_ENTER")
                            time.sleep(1.5)

                        keyguard_check = self._is_keyguard_locked(device, verbose=True)

                    # 最终验证
                    print(f"[ScreenMonitor] Unlock result: is_locked={keyguard_check}")

                    if not keyguard_check:
                        print("[ScreenMonitor] Screen unlocked successfully")
                        print("[ScreenMonitor] Turning off physical screen after unlock...")
                        try:
                            self.screen_off()
                        except Exception as e:
                            print(f"[ScreenMonitor] Failed to turn off screen after unlock: {e}")
                    else:
                        print("[ScreenMonitor] Unlock may have failed - retrying on next cycle")

            except Exception as e:
                print(f"[ScreenMonitor] Error: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(2)

        print("[ScreenMonitor] Screen monitor stopped")
        self._screen_monitor_running = False

    def start_screen_monitor(self):
        """
        Start the screen lock monitor thread.

        Monitors for lock screen, auto-inputs password, then turns off the screen.

        Returns:
            Dict with success status.
        """
        if self._screen_monitor_running:
            return {"success": True, "message": "Screen monitor already running"}

        cfg = self.load_config()
        lock_password = cfg.get('lock_password', '')
        if not lock_password:
            return {
                "success": False,
                "error": "No lock_password configured in display_config.json",
            }

        self._screen_monitor_stop = False
        self._screen_monitor_running = True
        self._screen_monitor_thread = threading.Thread(
            target=self._monitor_screen_lock_loop,
            daemon=True,
        )
        self._screen_monitor_thread.start()
        print("[DisplayManager] Screen lock monitor started")
        return {"success": True, "message": "Screen monitor started"}

    def stop_screen_monitor(self):
        """
        Stop the screen lock monitor thread.

        Returns:
            Dict with success status.
        """
        if not self._screen_monitor_running:
            return {"success": True, "message": "Screen monitor not running"}

        self._screen_monitor_stop = True
        print("[DisplayManager] Stopping screen lock monitor...")

        if self._screen_monitor_thread:
            self._screen_monitor_thread.join(timeout=5)

        return {"success": True, "message": "Screen monitor stopped"}

    @property
    def screen_monitor_running(self):
        """Check if screen monitor is running."""
        return self._screen_monitor_running

    def stop_all(self):
        """Stop all displays and screen monitor."""
        print("[DisplayManager] Stopping all displays and services...")

        self.stop_screen_monitor()
        self._stop_navigation_monitor()

        for pkg in list(self._app_display_map.keys()):
            try:
                self.stop_app_display(pkg)
            except Exception:
                pass

        if self._main_display:
            try:
                self._main_display.stop()
            except Exception:
                pass
            self._main_display = None
            self._main_display_running = False

        if self._multi_display:
            try:
                self._multi_display.stop_all()
            except Exception:
                pass
            self._multi_display = None

        print("[DisplayManager] All displays stopped")

    @property
    def main_display_running(self):
        return self._main_display_running

    @property
    def active_app_count(self):
        return len(self._app_display_map)


_default_manager = None


def get_display_manager(config_path=None, scrcpy_path=None, device_serial=None):
    """
    Get or create the default DisplayManager singleton.

    Args:
        config_path: Path to display_config.json.
        scrcpy_path: Path to scrcpy executable.
        device_serial: ADB device serial.

    Returns:
        DisplayManager instance.
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = DisplayManager(
            config_path=config_path,
            scrcpy_path=scrcpy_path,
            device_serial=device_serial,
        )
    return _default_manager
