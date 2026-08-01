"""
Multi Virtual Display implementation using scrcpy v4.0+ --new-display feature.
Supports creating multiple virtual displays on a single Android device,
each showing a different app, like a desktop environment.

All windows use Mac-style card decoration by default (borderless, always-on-top,
rounded corners, drop shadow, traffic lights, drag & resize).
Functional parameters (position, size, orientation, resolution, bitrate) are
passed via the WindowConfig VO from the front module.

Requirements:
    - scrcpy v4.0 or later
    - Android 15+ (virtual display support from v3.0, improved in v4.0)
"""

import subprocess
import os
import sys
import atexit
import shutil
import threading
import time
import re
import adbutils

# Frontend imports: card window styling + WindowConfig VO
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from front.card_window import (
    get_card_window_decorator,
    apply_mac_card_style,
    CARD_BACKGROUND_COLOR,
)
from front.window_config import WindowConfig


class MainDisplay:
    """
    Main display that mirrors the physical phone screen.
    This is a permanent display that stays alive.

    Uses Mac-style card window decoration by default:
    - Borderless window (--window-borderless)
    - Always on top (--always-on-top)
    - Dark card background (#1a1a2e)
    - DWM rounded corners + drop shadow (Windows 11)
    - Mac traffic light buttons (close/min/pin)
    - Drag from title bar, resize from edges/corners
    """

    def __init__(self, config: WindowConfig, scrcpy_path=None, device_serial=None):
        """
        Initialize MainDisplay.

        Args:
            config: WindowConfig VO with all display parameters.
            scrcpy_path: Path to scrcpy executable. Auto-detected if None.
            device_serial: ADB device serial. Auto-detected if None.
        """
        self.config = config
        self.window_title = config.window_title
        self.device_serial = device_serial

        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        self.process = None
        self.running = False
        self.output_thread = None
        self.stop_event = threading.Event()
        self._hwnd = None
        self._style_thread = None

    def _find_scrcpy(self):
        """Find scrcpy executable in PATH or common install locations."""
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

        raise FileNotFoundError(
            "scrcpy not found. Please install scrcpy v4.0+ "
            "or provide scrcpy_path parameter."
        )

    def _build_command(self, port=8887):
        """Build the scrcpy command line with Mac card style defaults."""
        cmd = [self.scrcpy_path]
        cfg = self.config

        if self.device_serial:
            cmd.extend(["-s", self.device_serial])

        # Window size and position
        if cfg.window_width and cfg.window_height:
            cmd.extend(["--window-width", str(cfg.window_width)])
            cmd.extend(["--window-height", str(cfg.window_height)])

        if cfg.window_x is not None:
            cmd.extend(["--window-x", str(cfg.window_x)])
        if cfg.window_y is not None:
            cmd.extend(["--window-y", str(cfg.window_y)])

        cmd.extend(["--window-title", cfg.window_title])
        cmd.extend(["--max-fps", str(cfg.max_fps)])
        cmd.extend(["--video-bit-rate", cfg.bitrate])
        cmd.extend(["--video-codec", cfg.video_codec])

        # Display orientation (270 = landscape counter-clockwise for main display)
        if cfg.rotation and cfg.rotation != 0:
            cmd.extend(["--display-orientation", str(cfg.rotation)])

        # Behavior flags
        if cfg.keep_active:
            cmd.append("--keep-active")
        if cfg.turn_screen_off:
            cmd.append("--turn-screen-off")
        if cfg.stay_awake:
            cmd.append("--stay-awake")

        # ── Mac card style defaults (hardcoded, not configurable) ──
        # These provide the floating card appearance:
        # --window-borderless: removes WS_CAPTION/WS_THICKFRAME/WS_SYSMENU for true borderless look
        # --always-on-top: keeps the card above other windows
        # --background-color: sets scrcpy letterbox area to dark card color
        cmd.append("--window-borderless")
        cmd.append("--always-on-top")
        cmd.extend(["--background-color", CARD_BACKGROUND_COLOR])

        cmd.extend(["--port", str(port)])
        return cmd

    def start(self, port=8887):
        """Start the main scrcpy display process and apply card styling."""
        if self.running:
            print(f"[{self.window_title}] Already running")
            return

        cmd = self._build_command(port)
        print(f"[{self.window_title}] Starting: {' '.join(cmd)}")

        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env
        )

        self.running = True
        self.stop_event.clear()
        print(f"[{self.window_title}] Main display started (PID: {self.process.pid})")

        # Check for immediate startup failure
        time.sleep(1.5)
        if self.process.poll() is not None:
            self.running = False
            exit_code = self.process.returncode
            output = ""
            try:
                output = self.process.stdout.read().decode('utf-8', errors='replace')
            except:
                pass
            print(f"[{self.window_title}] FAILED to start (exit code: {exit_code})")
            if output:
                print(f"[{self.window_title}] Output:\n{output[:1000]}")
            self.process = None
            return

        # Start background threads
        self.output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.output_thread.start()

        self._style_thread = threading.Thread(
            target=self._apply_card_style_delayed, daemon=True
        )
        self._style_thread.start()

    def _read_output(self):
        """Read and print scrcpy stdout/stderr in a background thread."""
        try:
            while self.running and self.process.poll() is None and not self.stop_event.is_set():
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    decoded_line = line.decode('utf-8', errors='replace').strip()
                except:
                    try:
                        decoded_line = line.decode('gbk', errors='replace').strip()
                    except:
                        decoded_line = line.decode('latin-1', errors='replace').strip()

                if decoded_line:
                    print(f"[{self.window_title}] {decoded_line}")
        except Exception as e:
            if self.running and not self.stop_event.is_set():
                print(f"[{self.window_title}] Output reader error: {e}")

    def _find_window_by_title(self, title):
        """Find scrcpy window HWND by title using Win32 EnumWindows."""
        if os.name != 'nt':
            return None
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        result = [None]

        def enum_windows_proc(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    if user32.IsWindowVisible(hwnd):
                        result[0] = hwnd
                        return 0
            return 1

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)
        return result[0]

    def _apply_card_style_delayed(self):
        """
        Wait for the scrcpy window to appear (up to ~9 seconds), then apply:
        1. DWM rounded corners + drop shadow (Windows 11)
        2. Register with card window decorator for traffic lights, drag & resize
        """
        try:
            hwnd = None
            for attempt in range(30):
                time.sleep(0.3)
                hwnd = self._find_window_by_title(self.window_title)
                if hwnd:
                    break
            if not hwnd:
                print(f"[{self.window_title}] Could not find window for styling")
                return

            print(f"[{self.window_title}] Applying Mac card style...")

            # Apply DWM rounded corners and shadow
            if os.name == 'nt':
                apply_mac_card_style(hwnd, self.window_title)

            # Register with the global card decorator for traffic lights + drag/resize
            try:
                decorator = get_card_window_decorator()
                _self_ref = self

                def _on_close_main():
                    """Callback for red close button: stop this display."""
                    _self_ref.stop()

                decorator.register_window(hwnd, on_close=_on_close_main)
                self._hwnd = hwnd
                print(f"[{self.window_title}] Registered with card window decorator (traffic lights + drag/resize)")
            except Exception as de:
                print(f"[{self.window_title}] Card decorator registration failed: {de}")
        except Exception as e:
            print(f"[{self.window_title}] Window style error: {e}")

    def stop(self):
        """Stop the main display, unregister decorator, and terminate scrcpy."""
        if not self.running:
            return

        self.running = False
        self.stop_event.set()
        print(f"[{self.window_title}] Stopping main display...")

        # Unregister from card decorator before killing the window
        try:
            if hasattr(self, '_hwnd') and self._hwnd:
                decorator = get_card_window_decorator()
                decorator.unregister_window(self._hwnd)
                self._hwnd = None
        except Exception as e:
            print(f"[{self.window_title}] Decorator unregister error: {e}")

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"[{self.window_title}] Error stopping: {e}")
            finally:
                self.process = None

        print(f"[{self.window_title}] Main display stopped")

    def wait(self):
        """Block until the scrcpy process exits."""
        if self.process:
            self.process.wait()
            self.running = False


class AppVirtualDisplay:
    """
    A single virtual display window for a specific app.
    Uses scrcpy v4.0 --new-display to create an independent virtual screen,
    then launches/moves the target app onto that display.

    Uses Mac-style card window decoration by default (same as MainDisplay).
    Resolution is automatically matched to window aspect ratio to avoid
    letterbox black bars (portrait/landscape/square auto-detection).
    """

    def __init__(self, app_package, config: WindowConfig, display_id=None,
                 scrcpy_path=None, device_serial=None, base_port=8887,
                 display_manager=None):
        """
        Initialize AppVirtualDisplay.

        Args:
            app_package: Android app package name (e.g. "com.huawei.android.totemweather").
            config: WindowConfig VO with display parameters.
            display_id: Virtual display ID for port allocation.
            scrcpy_path: Path to scrcpy executable.
            device_serial: ADB device serial.
            base_port: Base port for scrcpy (display_id added to get actual port).
            display_manager: Reference to DisplayManager for on-close cleanup.
        """
        self.app_package = app_package
        self.config = config
        self.display_id = display_id
        self.window_title = config.window_title
        self.device_serial = device_serial
        self.base_port = base_port
        self._display_manager = display_manager

        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        self.process = None
        self.running = False
        self.monitor_thread = None
        self.stop_monitoring = False
        self.android_display_id = None
        self._hwnd = None
        self._style_thread = None
        self.output_thread = None
        self.app_thread = None

    def _find_scrcpy(self):
        """Find scrcpy executable in PATH or common install locations."""
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

        raise FileNotFoundError(
            "scrcpy not found. Please install scrcpy v4.0+ "
            "or provide scrcpy_path parameter."
        )

    def _build_command(self):
        """
        Build the scrcpy command line for app virtual display.

        Key design points:
        - --new-display creates an Android VirtualDisplay; resolution/DPI are chosen
          to match the window aspect ratio (handled by WindowConfig.resolve_virtual_display_size)
        - --no-vd-system-decorations hides status/nav bars on the virtual screen
        - --display-orientation=0 (no encoding rotation); rotation is done at the
          Android system level via `wm user-rotation lock` to ensure apps receive
          proper orientation change events
        - Mac card style defaults (borderless + always-on-top + dark background)
          are hardcoded, same as MainDisplay
        """
        cmd = [self.scrcpy_path]
        cfg = self.config

        if self.device_serial:
            cmd.extend(["-s", self.device_serial])

        # ── Virtual display resolution ──
        # Use WindowConfig to resolve correct VD size matching window aspect ratio.
        # This prevents letterbox black bars when window ratio ≠ VD ratio.
        nd_w, nd_h, nd_dpi = cfg.resolve_virtual_display_size(cfg.default_orientation)
        nd_arg = f"{nd_w}x{nd_h}"
        if nd_dpi:
            nd_arg += f"/{nd_dpi}"
        cmd.append(f"--new-display={nd_arg}")

        # Window size and position
        if cfg.window_width and cfg.window_height:
            cmd.extend(["--window-width", str(cfg.window_width)])
            cmd.extend(["--window-height", str(cfg.window_height)])

        if cfg.window_x is not None:
            cmd.extend(["--window-x", str(cfg.window_x)])
        if cfg.window_y is not None:
            cmd.extend(["--window-y", str(cfg.window_y)])

        cmd.extend(["--window-title", cfg.window_title])
        cmd.extend(["--max-fps", str(cfg.max_fps)])
        cmd.extend(["--video-bit-rate", cfg.bitrate])
        cmd.extend(["--video-codec", cfg.video_codec])

        # scrcpy encoding rotation: fixed at 0 for app displays.
        # Actual screen rotation is handled by Android wm user-rotation lock
        # (applied after display creation), so apps receive native orientation events.
        if cfg.rotation and cfg.rotation != 0:
            cmd.extend(["--display-orientation", str(cfg.rotation)])

        # Behavior flags
        if cfg.no_vd_system_decorations:
            cmd.append("--no-vd-system-decorations")
        if cfg.flex_display:
            cmd.append("--flex-display")
        if cfg.keep_active:
            cmd.append("--keep-active")
        if cfg.turn_screen_off:
            cmd.append("--turn-screen-off")
        if cfg.stay_awake:
            cmd.append("--stay-awake")

        # ── Mac card style defaults (hardcoded) ──
        cmd.append("--window-borderless")
        cmd.append("--always-on-top")
        cmd.extend(["--background-color", CARD_BACKGROUND_COLOR])

        # Port: base_port + display_id for unique control socket
        port = self.base_port + (self.display_id or 0)
        cmd.extend(["--port", str(port)])

        return cmd

    def start(self):
        """Start the app virtual display: launch scrcpy, apply style, launch app."""
        if self.running:
            print(f"[{self.window_title}] Already running")
            return

        cmd = self._build_command()
        print(f"[{self.window_title}] Starting: {' '.join(cmd)}")

        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env
        )

        self.running = True
        print(f"[{self.window_title}] Virtual display started (PID: {self.process.pid})")

        # Check for immediate startup failure
        time.sleep(1.5)
        if self.process.poll() is not None:
            self.running = False
            exit_code = self.process.returncode
            output = ""
            try:
                output = self.process.stdout.read().decode('utf-8', errors='replace')
            except:
                pass
            print(f"[{self.window_title}] FAILED to start (exit code: {exit_code})")
            if output:
                print(f"[{self.window_title}] Output:\n{output[:1500]}")
            self.process = None
            return

        # Start background threads
        self.output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.output_thread.start()

        self._style_thread = threading.Thread(
            target=self._apply_card_style_delayed, daemon=True
        )
        self._style_thread.start()

        # Launch the app on the virtual display
        if self.app_package:
            self.app_thread = threading.Thread(
                target=self._launch_app, daemon=True
            )
            self.app_thread.start()

        # Monitor if the app closes, auto-stop the display
        if self.app_package:
            self._start_app_monitor()

    def _read_output(self):
        """Read scrcpy output and parse the Android virtual display ID from logs."""
        try:
            while self.running and self.process.poll() is None:
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    decoded_line = line.decode('utf-8', errors='replace').strip()
                except:
                    try:
                        decoded_line = line.decode('gbk', errors='replace').strip()
                    except:
                        decoded_line = line.decode('latin-1', errors='replace').strip()

                if decoded_line:
                    print(f"[{self.window_title}] {decoded_line}")

                    # Parse Android display ID from scrcpy log:
                    # "[server] INFO: New display: 720x720/480 (id=4)"
                    if "New display:" in decoded_line and "id=" in decoded_line:
                        match = re.search(r'id=(\d+)', decoded_line)
                        if match:
                            self.android_display_id = int(match.group(1))
                            print(f"[{self.window_title}] Parsed Android display ID: {self.android_display_id}")
        except Exception as e:
            if self.running:
                print(f"[{self.window_title}] Output reader error: {e}")

    def _apply_card_style_delayed(self):
        """
        Wait for the scrcpy window to appear, then apply Mac card style
        (DWM rounded corners + shadow) and register with the card decorator.
        """
        try:
            hwnd = None
            for attempt in range(30):
                time.sleep(0.3)
                hwnd = self._find_window_by_title(self.window_title)
                if hwnd:
                    break
            if not hwnd:
                print(f"[{self.window_title}] Could not find window for styling")
                return

            print(f"[{self.window_title}] Applying Mac card style...")

            # Apply DWM rounded corners and shadow
            if os.name == 'nt':
                apply_mac_card_style(hwnd, self.window_title)

            # Register with card decorator (traffic lights, drag, resize)
            try:
                decorator = get_card_window_decorator()
                _self_ref = self
                _pkg = self.app_package
                _dm = self._display_manager

                def _on_close_app():
                    """
                    Callback for red close button: stop the app display.
                    Uses DisplayManager if available for complete cleanup
                    (stop scrcpy, unregister decorator, return display ID to pool,
                    remove from app map).
                    """
                    if _dm:
                        _dm.stop_app_display(_pkg)
                    else:
                        _self_ref.stop()

                decorator.register_window(hwnd, on_close=_on_close_app)
                self._hwnd = hwnd
                print(f"[{self.window_title}] Registered with card window decorator (traffic lights + drag/resize)")
            except Exception as de:
                print(f"[{self.window_title}] Card decorator registration failed: {de}")

        except Exception as e:
            print(f"[{self.window_title}] Window style error: {e}")

    def _find_window_by_title(self, title):
        """Find scrcpy window HWND by title using Win32 EnumWindows."""
        if os.name != 'nt':
            return None
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        result = [None]

        def enum_windows_proc(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    if user32.IsWindowVisible(hwnd):
                        result[0] = hwnd
                        return 0
            return 1

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)
        return result[0]

    def _launch_app(self):
        """
        Launch or move the target app to this virtual display.

        Process:
        1. Wait for virtual display to initialize and parse display ID
        2. Lock Android user-rotation to the natural orientation (prevents
           FLAG_ROTATES_WITH_CONTENT from forcing rotation back to portrait)
        3. Try `am start --display` first (no cold start if app is already running)
        4. Fall back to `monkey` launcher for cold starts
        """
        try:
            # Get ADB device
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            # Wait for Android display ID to be parsed
            print(f"[{self.window_title}] Waiting for display initialization...")
            max_wait = 15
            waited = 0
            while waited < max_wait and self.android_display_id is None:
                time.sleep(1)
                waited += 1

            if self.android_display_id is None:
                print(f"[{self.window_title}] Warning: Could not parse Android display ID, using fallback")
                dumpsys_result = device.shell("dumpsys display")
                match = re.search(r'Display Id=(\d+).*VirtualDisplay', dumpsys_result, re.DOTALL)
                if match:
                    self.android_display_id = int(match.group(1))
                    print(f"[{self.window_title}] Fallback: Found display ID {self.android_display_id}")
                else:
                    print(f"[{self.window_title}] Error: Could not determine display ID")
                    return

            display_id = self.android_display_id

            # Lock user-rotation to natural direction (0)
            # This is critical for landscape apps: without this lock, apps with
            # FLAG_ROTATES_WITH_CONTENT can cause the system to rotate the virtual
            # display back to portrait even if the VD was created landscape.
            if hasattr(self.config, 'user_rotation'):
                try:
                    wm_cmd = f"wm user-rotation -d {display_id} lock {self.config.user_rotation}"
                    wm_result = device.shell(wm_cmd)
                    if wm_result and ("Error" in wm_result or "Unknown" in wm_result or "Exception" in wm_result):
                        print(f"[{self.window_title}] wm user-rotation failed: {wm_result.strip()[:200]}")
                    else:
                        is_landscape_nd = (self.config.new_display_width or 0) > (self.config.new_display_height or 0)
                        if self.config.user_rotation == 0:
                            orient_desc = "natural(横屏)" if is_landscape_nd else "natural(竖屏)"
                        else:
                            orient_desc = f"rot={self.config.user_rotation}"
                        print(f"[{self.window_title}] Set wm user-rotation to lock {self.config.user_rotation} ({orient_desc}) on display {display_id}")
                        time.sleep(0.5)
                except Exception as wm_err:
                    print(f"[{self.window_title}] wm user-rotation error (non-critical): {wm_err}")

            print(f"[{self.window_title}] Moving app {self.app_package} to display {display_id}...")

            # Method 1: am start (no cold start - works if app is already running)
            intent_result = device.shell(f"cmd package resolve-activity --brief {self.app_package}")
            am_launched = False
            if intent_result and "/" in intent_result and "No activity found" not in intent_result:
                lines = intent_result.strip().split('\n')
                activity = None
                for line in lines:
                    if '/' in line and not line.startswith('#'):
                        activity = line.strip()
                        break
                if activity is None:
                    activity = lines[-1].strip()

                if activity and '/' in activity:
                    am_cmd = f"am start -n {activity} --display {display_id}"
                    result = device.shell(am_cmd)

                    if "Error" not in result and "Exception" not in result:
                        print(f"[{self.window_title}] App moved to display via am start (no cold start)")
                        am_launched = True
                    else:
                        print(f"[{self.window_title}] am start returned: {result.strip()[:200]}")

            # Method 2: monkey launcher (cold start fallback)
            if not am_launched:
                print(f"[{self.window_title}] Trying monkey launcher...")
                monkey_cmd = f"monkey -p {self.app_package} --display {display_id} -c android.intent.category.LAUNCHER 1"
                result = device.shell(monkey_cmd)

                if "No activities found" in result or "Error" in result:
                    print(f"[{self.window_title}] Monkey also failed: {result.strip()[:200]}")
                else:
                    print(f"[{self.window_title}] App launched via monkey (cold start)")

            # Wait for rotation lock to settle
            if hasattr(self.config, 'user_rotation') and self.config.user_rotation != 0:
                time.sleep(2.0)

        except Exception as e:
            print(f"[{self.window_title}] Launch app error: {e}")

    def _start_app_monitor(self):
        """Start a background thread to monitor if the app is still running."""
        self.stop_monitoring = False
        self.monitor_thread = threading.Thread(
            target=self._monitor_app, daemon=True
        )
        self.monitor_thread.start()

    def _monitor_app(self):
        """
        Monitor if the app is still in foreground on this virtual display.
        If the app closes (consecutive failures), automatically stop the display
        to free resources.
        """
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            # Wait 20 seconds for app to fully start before monitoring
            print(f"[{self.window_title}] Waiting 20 seconds for app to start...")
            time.sleep(20)

            consecutive_failures = 0
            max_failures = 15

            while not self.stop_monitoring and self.running:
                try:
                    pid_result = device.shell(f"pidof {self.app_package}").strip()

                    if pid_result:
                        if consecutive_failures > 0:
                            print(f"[{self.window_title}] App {self.app_package} found with PID: {pid_result}")
                        consecutive_failures = 0
                    else:
                        # pidof returned empty; double-check with ps and dumpsys
                        ps_result = device.shell(f"ps -A").strip()
                        app_running = False
                        for line in ps_result.split('\n'):
                            if self.app_package in line:
                                parts = line.split()
                                if len(parts) >= 9 and parts[-1] == self.app_package:
                                    app_running = True
                                    break

                        if app_running:
                            if consecutive_failures > 0:
                                print(f"[{self.window_title}] App {self.app_package} found in ps output")
                            consecutive_failures = 0
                        else:
                            dumpsys_result = device.shell("dumpsys activity activities")
                            if self.app_package in dumpsys_result:
                                if consecutive_failures > 0:
                                    print(f"[{self.window_title}] App {self.app_package} found in foreground")
                                consecutive_failures = 0
                            else:
                                consecutive_failures += 1
                                print(f"[{self.window_title}] App {self.app_package} not found ({consecutive_failures}/{max_failures})")

                                # App seems closed after multiple checks; stop display
                                if consecutive_failures >= max_failures:
                                    print(f"[{self.window_title}] App {self.app_package} closed, stopping display...")
                                    self.stop()
                                    break
                except Exception as e:
                    print(f"[{self.window_title}] Monitor error: {e}")
                time.sleep(3)
        except Exception as e:
            print(f"[{self.window_title}] Monitor failed to start: {e}")

    def stop(self):
        """Stop the app virtual display: unregister decorator, kill scrcpy."""
        if not self.running:
            return

        self.running = False
        self.stop_monitoring = True
        print(f"[{self.window_title}] Stopping virtual display...")

        # Unregister from card decorator
        try:
            if hasattr(self, '_hwnd') and self._hwnd:
                decorator = get_card_window_decorator()
                decorator.unregister_window(self._hwnd)
                self._hwnd = None
        except Exception as e:
            print(f"[{self.window_title}] Decorator unregister error: {e}")

        # Terminate scrcpy process
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"[{self.window_title}] Error stopping: {e}")
            finally:
                self.process = None

        print(f"[{self.window_title}] Virtual display stopped")

    def wait(self):
        """Block until the scrcpy process exits."""
        if self.process:
            self.process.wait()
            self.running = False


class MultiVirtualDisplay:
    """
    Manage multiple virtual displays on a single Android device.
    Each display shows a different app in its own floating card window.

    Features:
    - Display ID pool with safe reuse (3-second port release delay)
    - All windows use Mac-style card decoration by default
    - Automatic app launch on each virtual display
    - App lifecycle monitoring (auto-cleanup when app closes)
    """

    def __init__(self, device_serial=None, scrcpy_path=None,
                 turn_screen_off=True, keep_active=True,
                 new_display_width=1260, new_display_height=2844,
                 base_port=8887):
        self.device_serial = device_serial
        self.scrcpy_path = scrcpy_path
        self.turn_screen_off = turn_screen_off
        self.keep_active = keep_active
        self.new_display_width = new_display_width
        self.new_display_height = new_display_height
        self.base_port = base_port
        self.displays = []
        self.running = False
        self._next_display_id = 1
        self._free_display_ids = []

        atexit.register(self.stop_all)

    def _alloc_display_id(self):
        """
        Allocate a display ID, reusing freed IDs when available.
        IDs are returned to the pool after stop with a 3-second delay
        handled by the caller (via time.sleep before reuse) to ensure
        the scrcpy port is released and Android destroys the old VD.
        """
        if self._free_display_ids:
            return self._free_display_ids.pop(0)
        did = self._next_display_id
        self._next_display_id += 1
        return did

    def _free_display_id(self, display_id):
        """Return a display ID to the free pool for later reuse."""
        if display_id is not None and display_id not in self._free_display_ids:
            self._free_display_ids.append(display_id)

    def add_display(self, app_package, config: WindowConfig,
                    display_manager=None):
        """
        Add an app virtual display to the manager.

        Args:
            app_package: Android app package name.
            config: WindowConfig VO with display parameters.
            display_manager: Reference to DisplayManager for on-close cleanup.

        Returns:
            self for chaining.
        """
        display = AppVirtualDisplay(
            app_package=app_package,
            config=config,
            display_id=self._alloc_display_id(),
            scrcpy_path=self.scrcpy_path,
            device_serial=self.device_serial,
            base_port=self.base_port,
            display_manager=display_manager,
        )
        self.displays.append(display)
        return self

    def start_all(self):
        """Start all added virtual displays sequentially."""
        if self.running:
            print("Multi virtual display already running")
            return

        self.running = True
        print(f"Starting {len(self.displays)} virtual displays...")

        for i, display in enumerate(self.displays):
            print(f"\n[{i+1}/{len(self.displays)}] Starting {display.window_title}...")
            display.start()

            if display.app_package:
                print(f"Waiting for {display.window_title} to initialize...")
                time.sleep(3)

            if i < len(self.displays) - 1:
                time.sleep(2)

        print(f"\n✅ All {len(self.displays)} virtual displays started")

    def stop_all(self):
        """Stop all virtual displays and clean up."""
        if not self.running:
            return

        self.running = False
        print("Stopping all virtual displays...")

        for display in self.displays:
            display.stop()

        self._cleanup_server()

        print("All virtual displays stopped")

    def _cleanup_server(self):
        """Remove scrcpy-server.jar from the device to avoid stale state."""
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            device.shell("rm -f /data/local/tmp/scrcpy-server.jar")
            print("Cleaned up scrcpy-server.jar")
        except Exception as e:
            print(f"Cleanup warning: {e}")

    def wait_all(self):
        """Wait for all display processes to exit."""
        threads = []
        for display in self.displays:
            t = threading.Thread(target=display.wait, daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        self.running = False

    def stop_display(self, index):
        """Stop a specific display by index."""
        if 0 <= index < len(self.displays):
            self.displays[index].stop()
        else:
            print(f"Invalid display index: {index}")


def create_desktop_layout(device_serial=None, scrcpy_path=None):
    """Helper to create a 2x2 grid layout of default-sized windows."""
    multi = MultiVirtualDisplay(
        device_serial=device_serial,
        scrcpy_path=scrcpy_path,
        turn_screen_off=True,
        keep_active=True
    )

    w, h = 540, 960
    gap = 10

    positions = [
        {"window_x": gap, "window_y": gap,
         "window_width": w, "window_height": h},
        {"window_x": w + gap * 2, "window_y": gap,
         "window_width": w, "window_height": h},
        {"window_x": gap, "window_y": h + gap * 2,
         "window_width": w, "window_height": h},
        {"window_x": w + gap * 2, "window_y": h + gap * 2,
         "window_width": w, "window_height": h},
    ]

    return multi, positions


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Multi Virtual Display using scrcpy v4.0+"
    )
    parser.add_argument("--serial", "-s", type=str, help="Device serial number")
    parser.add_argument("--apps", "-a", type=str, nargs="+",
                        help="Package names of apps to launch")
    parser.add_argument("--layout", type=str, choices=["grid", "horizontal", "vertical", "free"],
                        default="grid", help="Window layout (default: grid)")
    parser.add_argument("--window-width", type=int, default=540)
    parser.add_argument("--window-height", type=int, default=960)
    parser.add_argument("--max-fps", type=int, default=60)
    parser.add_argument("--bitrate", type=str, default='8M')
    parser.add_argument("--video-codec", type=str, default='h264', choices=["h264", "h265", "av1"])
    parser.add_argument("--turn-screen-off", action="store_true", default=True)
    parser.add_argument("--no-turn-screen-off", action="store_true")
    parser.add_argument("--flex-display", action="store_true")

    args = parser.parse_args()

    if not args.apps:
        print("Error: --apps is required. Example: --apps com.taobao.taobao com.eg.android.AlipayGphone")
        exit(1)

    turn_screen_off = args.turn_screen_off and not args.no_turn_screen_off

    multi = MultiVirtualDisplay(
        device_serial=args.serial,
        turn_screen_off=turn_screen_off,
        keep_active=True
    )

    w = args.window_width
    h = args.window_height
    gap = 10

    for i, app in enumerate(args.apps):
        if args.layout == "grid":
            col = i % 2
            row = i // 2
            x = gap + col * (w + gap)
            y = gap + row * (h + gap)
        elif args.layout == "horizontal":
            x = gap + i * (w + gap)
            y = gap
        elif args.layout == "vertical":
            x = gap
            y = gap + i * (h + gap)
        else:
            x = gap + i * (w + gap)
            y = gap

        config = WindowConfig(
            window_title=app.split(".")[-1],
            window_x=x,
            window_y=y,
            window_width=w,
            window_height=h,
            max_fps=args.max_fps,
            bitrate=args.bitrate,
            video_codec=args.video_codec,
            flex_display=args.flex_display,
        )

        multi.add_display(
            app_package=app,
            config=config,
        )

    try:
        multi.start_all()
        multi.wait_all()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        multi.stop_all()
