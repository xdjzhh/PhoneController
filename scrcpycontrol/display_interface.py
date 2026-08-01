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

from .multi_virtualdisplay import MainDisplay, MultiVirtualDisplay, AppVirtualDisplay


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
            if self._main_display.rotation == rotation:
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

        win_w = main_cfg.get("window_width", 540)
        win_h = main_cfg.get("window_height", 960)
        if rotation in (90, 270):
            win_w, win_h = win_h, win_w
            print(f"[DisplayManager] Landscape mode: swapping window size to {win_w}x{win_h}")

        try:
            self._main_display = MainDisplay(
                window_title=main_cfg.get("window_title", "手机桌面"),
                window_x=main_cfg.get("window_x", 100),
                window_y=main_cfg.get("window_y", 100),
                window_width=win_w,
                window_height=win_h,
                max_fps=main_cfg.get("max_fps", 60),
                bitrate=main_cfg.get("bitrate", "8M"),
                video_codec=main_cfg.get("video_codec", "h264"),
                keep_active=main_cfg.get("keep_active", True),
                turn_screen_off=main_cfg.get("turn_screen_off", True),
                stay_awake=main_cfg.get("stay_awake", True),
                rotation=rotation,
                borderless=main_cfg.get("borderless", False),
                background_color=main_cfg.get("background_color", None),
                always_on_top=main_cfg.get("always_on_top", False),
                corner_radius=main_cfg.get("corner_radius", None),
                card_style=main_cfg.get("card_style", False),
                scrcpy_path=self.scrcpy_path,
                device_serial=self.device_serial,
            )

            print(f"[DisplayManager] Starting main display (rotation={rotation})...")
            self._main_display.start()
            time.sleep(2)

            if self._main_display.running:
                self._main_display_running = True
                self._main_rotation = rotation
                self._initialized = True
                print("[DisplayManager] Main display started successfully")
                return {
                    "success": True,
                    "message": "Main display started",
                    "display_type": "main",
                    "window_title": main_cfg.get("window_title", "手机桌面"),
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
            nd_w = app_cfg.get("new_display_width", 1260)
            nd_h = app_cfg.get("new_display_height", 2844)

            # ===== CarPlay式方案：根据横竖屏使用不同物理分辨率的VirtualDisplay =====
            # 核心原理：Android VirtualDisplay的natural orientation由width/height比例决定：
            #   - width < height → natural=竖屏(portrait)，rotation=0=竖屏
            #   - width > height → natural=横屏(landscape)，rotation=0=横屏
            # 之前用竖屏new_display + rotation=270只是视频编码旋转，app始终认为竖屏。
            # CarPlay/HiCar的做法是直接创建横屏分辨率的VirtualDisplay，然后锁定user-rotation=0
            # （natural方向），app启动时就看到横屏屏幕，渲染原生横屏UI。
            #
            # 竖屏模式：使用app配置中的竖屏分辨率（如1260x2844），wm lock 0
            # 横屏模式：使用独立的横屏分辨率配置（默认1920x1080/320dpi），wm lock 0
            # wm user-rotation=lock 0 锁定natural方向，防止FLAG_ROTATES_WITH_CONTENT
            # 导致app强制竖屏时系统自动旋转display。
            nd_dpi = app_cfg.get("new_display_dpi", None)
            if orientation == "landscape":
                # 横屏：窗口横屏，new_display使用横屏分辨率
                if win_w < win_h:
                    win_w, win_h = win_h, win_w
                nd_w = app_cfg.get("landscape_new_display_width", 1920)
                nd_h = app_cfg.get("landscape_new_display_height", 1080)
                nd_dpi = app_cfg.get("landscape_new_display_dpi", 320)
                target_rotation = 0
                target_user_rotation = 0  # lock 0 = natural方向=横屏（因为w>h）
                nd_orient_desc = f"{nd_w}x{nd_h}/{nd_dpi}dpi(横屏natural)"
            else:
                # 竖屏：窗口竖屏，new_display使用竖屏分辨率
                if win_w > win_h:
                    win_w, win_h = win_h, win_w
                if nd_w > nd_h:
                    nd_w, nd_h = nd_h, nd_w
                target_rotation = 0
                target_user_rotation = 0  # lock 0 = natural方向=竖屏（因为h>w）
                nd_orient_desc = f"{nd_w}x{nd_h}/{nd_dpi or 'default'}dpi(竖屏natural)"

            mvd = self._get_multi_display()

            try:
                ur_desc = "lock 0 (natural方向)" if target_user_rotation == 0 else f"lock {target_user_rotation}"
                print(f"[DisplayManager] ========== 创建{orientation}虚拟屏参数 ==========")
                print(f"[DisplayManager] app_package           : {app_package}")
                print(f"[DisplayManager] window 尺寸           : {win_w}x{win_h} ({'横屏✅' if win_w > win_h else '竖屏'})")
                print(f"[DisplayManager] new_display (安卓)    : {nd_orient_desc}")
                print(f"[DisplayManager] scrcpy rotation       : {target_rotation} (不做编码旋转)")
                print(f"[DisplayManager] wm user-rotation      : {ur_desc} (锁定natural方向，防FLAG_ROTATES_WITH_CONTENT)")
                print(f"[DisplayManager] =================================================")

                display = AppVirtualDisplay(
                    app_package=app_package,
                    display_id=mvd._alloc_display_id(),
                    window_title=app_cfg.get("window_title", app_package),
                    window_x=app_cfg.get("window_x"),
                    window_y=app_cfg.get("window_y"),
                    window_width=win_w,
                    window_height=win_h,
                    new_display_width=nd_w,
                    new_display_height=nd_h,
                    new_display_dpi=nd_dpi,
                    max_fps=app_cfg.get("max_fps", 60),
                    bitrate=app_cfg.get("bitrate", "8M"),
                    video_codec=app_cfg.get("video_codec", "h264"),
                    no_vd_system_decorations=True,
                    borderless=app_cfg.get("borderless", False),
                    background_color=app_cfg.get("background_color", None),
                    always_on_top=app_cfg.get("always_on_top", False),
                    corner_radius=app_cfg.get("corner_radius", None),
                    card_style=app_cfg.get("card_style", False),
                    keep_active=app_cfg.get("keep_active", True),
                    turn_screen_off=False,
                    stay_awake=app_cfg.get("stay_awake", True),
                    scrcpy_path=self.scrcpy_path,
                    device_serial=self.device_serial,
                    base_port=mvd.base_port,
                    rotation=target_rotation,
                    user_rotation=target_user_rotation,
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
        """Get window title for an app virtual display."""
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {})
        return app_cfg.get("window_title", app_package)

    def _find_window_by_title(self, title):
        """Find window handle by title using ctypes."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, title)
            return hwnd if hwnd else None
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

        if app_package is None:
            cfg = self.load_config()
            title = cfg.get("main_display", {}).get("window_title", "手机桌面")
        else:
            title = self._get_window_title_for_app(app_package)

        hwnd = self._find_window_by_title(title)
        if not hwnd:
            return {"success": False, "error": f"Window not found: {title}"}

        try:
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

            if result:
                print(f"[DisplayManager] Window '{title}' opacity set to {opacity:.2f}")
                return {
                    "success": True,
                    "window_title": title,
                    "opacity": opacity,
                    "message": f"Opacity set to {opacity:.2f}",
                }
            else:
                return {"success": False, "error": "SetLayeredWindowAttributes failed"}

        except Exception as e:
            print(f"[DisplayManager] Failed to set window opacity: {e}")
            return {"success": False, "error": str(e)}

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
        from .window_decorator import get_window_decorator

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
            decorator = get_window_decorator()
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
