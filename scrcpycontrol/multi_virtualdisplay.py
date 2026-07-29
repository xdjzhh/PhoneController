"""
Multi Virtual Display implementation using scrcpy v4.0+ --new-display feature.
Supports creating multiple virtual displays on a single Android device,
each showing a different app, like a desktop environment.

Requirements:
    - scrcpy v4.0 or later
    - Android 15+ (virtual display support from v3.0, improved in v4.0)
"""

import subprocess
import os
import atexit
import shutil
import threading
import time
import re
import adbutils


class MainDisplay:
    """
    Main display that mirrors the physical phone screen.
    This is a permanent display that stays alive.
    """

    def __init__(self, window_title=None, window_x=None, window_y=None,
                 window_width=None, window_height=None,
                 max_fps=60, bitrate='8M', video_codec='h264',
                 keep_active=True, turn_screen_off=True, stay_awake=True,
                 rotation=0,
                 scrcpy_path=None, device_serial=None):
        """
        Initialize main display.

        Args:
            window_title: Custom window title
            window_x: Window X position on desktop
            window_y: Window Y position on desktop
            window_width: Window width
            window_height: Window height
            max_fps: Maximum frames per second
            bitrate: Video bitrate (e.g., '8M', '16M')
            video_codec: Video codec ('h264', 'h265', 'av1')
            keep_active: Prevent device auto sleep
            turn_screen_off: Turn off physical screen
            stay_awake: Keep device awake (prevent screen timeout)
            scrcpy_path: Path to scrcpy executable
            device_serial: ADB device serial number
        """
        self.window_title = window_title or "手机桌面"
        self.window_x = window_x
        self.window_y = window_y
        self.window_width = window_width
        self.window_height = window_height
        self.max_fps = max_fps
        self.bitrate = bitrate
        self.video_codec = video_codec
        self.keep_active = keep_active
        self.turn_screen_off = turn_screen_off
        self.stay_awake = stay_awake
        self.rotation = rotation  # 0, 1, 2, 3 (0=0°, 1=90°, 2=180°, 3=270°)
        self.device_serial = device_serial

        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        self.process = None
        self.running = False
        self.output_thread = None
        self.stop_event = threading.Event()

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

        raise FileNotFoundError(
            "scrcpy not found. Please install scrcpy v4.0+ "
            "or provide scrcpy_path parameter."
        )

    def _build_command(self, port=8887):
        """Build scrcpy command for main display."""
        cmd = [self.scrcpy_path]

        # Device serial
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])

        # No --new-display, this mirrors the physical screen

        # Window size
        if self.window_width and self.window_height:
            cmd.extend(["--window-width", str(self.window_width)])
            cmd.extend(["--window-height", str(self.window_height)])

        # Window position
        if self.window_x is not None:
            cmd.extend(["--window-x", str(self.window_x)])
        if self.window_y is not None:
            cmd.extend(["--window-y", str(self.window_y)])

        # Window title
        cmd.extend(["--window-title", self.window_title])

        # Video settings
        cmd.extend(["--max-fps", str(self.max_fps)])
        cmd.extend(["--video-bit-rate", self.bitrate])
        cmd.extend(["--video-codec", self.video_codec])

        # Rotation (0, 90, 180, 270 degrees clockwise)
        if self.rotation and self.rotation != 0:
            cmd.extend(["--display-orientation", str(self.rotation)])

        # Keep active
        if self.keep_active:
            cmd.append("--keep-active")

        # Turn screen off
        if self.turn_screen_off:
            cmd.append("--turn-screen-off")

        # Stay awake
        if self.stay_awake:
            cmd.append("--stay-awake")

        # Port
        cmd.extend(["--port", str(port)])

        return cmd

    def start(self, port=8887):
        """Start this main display."""
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

        self.output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.output_thread.start()

    def _read_output(self):
        """Read and print scrcpy output."""
        try:
            while self.running and self.process.poll() is None and not self.stop_event.is_set():
                line = self.process.stdout.readline()
                if not line:
                    break
                # Try multiple encodings
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

    def stop(self):
        """Stop this main display."""
        if not self.running:
            return

        self.running = False
        self.stop_event.set()
        print(f"[{self.window_title}] Stopping main display...")

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
        """Wait for this display to exit."""
        if self.process:
            self.process.wait()
            self.running = False


class AppVirtualDisplay:
    """
    A single virtual display window for a specific app.
    Uses scrcpy v4.0 --new-display to create an independent virtual screen.
    """

    def __init__(self, app_package, display_id=None,
                 window_title=None, window_x=None, window_y=None,
                 window_width=None, window_height=None,
                 new_display_width=1260, new_display_height=2844,
                 new_display_dpi=None,
                 max_fps=60, bitrate='8M', video_codec='h264',
                 no_vd_system_decorations=True, flex_display=False,
                 borderless=False, background_color=None,
                 always_on_top=False, corner_radius=None,
                 card_style=False,
                 keep_active=True, turn_screen_off=True, stay_awake=True,
                 record=None, scrcpy_path=None, device_serial=None,
                 base_port=8887, rotation=0, user_rotation=0):
        """
        Initialize a virtual display for an app.

        Args:
            app_package: Package name of the app to launch (e.g., 'com.taobao.taobao')
            display_id: Virtual display ID (auto-assigned if None)
            window_title: Custom window title
            window_x: Window X position on desktop
            window_y: Window Y position on desktop
            window_width: Window width
            window_height: Window height
            new_display_width: Virtual display resolution width (default: 1260)
            new_display_height: Virtual display resolution height (default: 2844)
            new_display_dpi: Virtual display DPI (e.g., 320 for landscape car mode; None for default)
            max_fps: Maximum frames per second
            bitrate: Video bitrate (e.g., '8M', '16M')
            video_codec: Video codec ('h264', 'h265', 'av1')
            no_vd_system_decorations: Hide status bar and navigation bar
            flex_display: Enable flexible display (resize with window drag)
            borderless: Borderless window
            background_color: Background color (e.g., '#2a2a2a')
            always_on_top: Keep window always on top (floating widget)
            corner_radius: Window corner radius in pixels (0=no round, None=system default, e.g. 16=Mac-style)
            card_style: Mac-style card (borderless + always-on-top + rounded corners + shadow)
            keep_active: Prevent device auto sleep
            turn_screen_off: Turn off physical screen
            stay_awake: Keep device awake (prevent screen timeout)
            record: Record to file
            scrcpy_path: Path to scrcpy executable
            device_serial: ADB device serial number
            base_port: Base port number for scrcpy server (default: 8887)
            rotation: scrcpy编码级旋转(0/90/180/270)，CarPlay方案中为0（不旋转）
            user_rotation: Android wm user-rotation值。CarPlay式方案：
                          - 竖屏VD(h>w): natural=portrait, lock 0=竖屏
                          - 横屏VD(w>h): natural=landscape, lock 0=横屏
                          锁定natural方向可防止FLAG_ROTATES_WITH_CONTENT导致
                          强制竖屏app触发display旋转回竖屏。
        """
        self.app_package = app_package
        self.display_id = display_id
        self.window_title = window_title or app_package
        self.window_x = window_x
        self.window_y = window_y
        self.window_width = window_width
        self.window_height = window_height
        self.new_display_width = new_display_width
        self.new_display_height = new_display_height
        self.new_display_dpi = new_display_dpi
        self.max_fps = max_fps
        self.bitrate = bitrate
        self.video_codec = video_codec
        self.no_vd_system_decorations = no_vd_system_decorations
        self.flex_display = flex_display
        self.borderless = borderless
        self.background_color = background_color
        self.always_on_top = always_on_top
        self.corner_radius = corner_radius
        self.card_style = card_style
        # card_style快捷方式：开启borderless+always_on_top+圆角+阴影
        if self.card_style:
            self.borderless = True
            self.always_on_top = True
            if self.corner_radius is None:
                self.corner_radius = 20
            if self.background_color is None:
                self.background_color = "#1a1a2e"
        self.keep_active = keep_active
        self.turn_screen_off = turn_screen_off
        self.stay_awake = stay_awake
        self.record = record
        self.device_serial = device_serial
        self.base_port = base_port
        self.rotation = rotation  # scrcpy --display-orientation参数（编码级旋转），0=不旋转
        self.user_rotation = user_rotation  # Android wm user-rotation值（系统级旋转）

        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        self.process = None
        self.running = False
        self.monitor_thread = None
        self.stop_monitoring = False
        self.android_display_id = None  # Actual Android virtual display ID

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

        raise FileNotFoundError(
            "scrcpy not found. Please install scrcpy v4.0+ "
            "or provide scrcpy_path parameter."
        )

    def _build_command(self):
        """Build scrcpy command for this virtual display."""
        cmd = [self.scrcpy_path]

        # Device serial
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])

        # Create new virtual display with resolution and optional DPI
        nd_w = self.new_display_width
        nd_h = self.new_display_height
        nd_dpi = self.new_display_dpi
        if nd_w and nd_h:
            nd_arg = f"{nd_w}x{nd_h}"
            if nd_dpi:
                nd_arg += f"/{nd_dpi}"
            cmd.append(f"--new-display={nd_arg}")
        else:
            cmd.append("--new-display")

        # Window size
        if self.window_width and self.window_height:
            cmd.extend(["--window-width", str(self.window_width)])
            cmd.extend(["--window-height", str(self.window_height)])

        # Window position
        if self.window_x is not None:
            cmd.extend(["--window-x", str(self.window_x)])
        if self.window_y is not None:
            cmd.extend(["--window-y", str(self.window_y)])

        # Window title
        cmd.extend(["--window-title", self.window_title])

        # Video settings
        cmd.extend(["--max-fps", str(self.max_fps)])
        cmd.extend(["--video-bit-rate", self.bitrate])
        cmd.extend(["--video-codec", self.video_codec])

        # Rotation (0, 90, 180, 270 degrees). 270 = landscape (CCW 90°)
        if self.rotation and self.rotation != 0:
            cmd.extend(["--display-orientation", str(self.rotation)])

        # Virtual display decorations
        if self.no_vd_system_decorations:
            cmd.append("--no-vd-system-decorations")

        # Flex display
        if self.flex_display:
            cmd.append("--flex-display")

        # Borderless
        if self.borderless:
            cmd.append("--window-borderless")

        # Always on top (floating widget)
        if self.always_on_top:
            cmd.append("--always-on-top")

        # Background color
        if self.background_color:
            cmd.extend(["--background-color", self.background_color])

        # Keep active
        if self.keep_active:
            cmd.append("--keep-active")

        # Turn screen off
        if self.turn_screen_off:
            cmd.append("--turn-screen-off")

        # Stay awake
        if self.stay_awake:
            cmd.append("--stay-awake")

        # Record
        if self.record:
            cmd.extend(["--record", self.record])

        # Use different ports for multiple instances to avoid conflicts
        # Port = base_port + display_id
        port = self.base_port + (self.display_id or 0)
        cmd.extend(["--port", str(port)])

        return cmd

    def start(self):
        """Start this virtual display."""
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

        self.output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.output_thread.start()

        # 应用Mac风格卡片窗口样式（圆角、阴影）
        if self.corner_radius is not None or self.card_style:
            self._style_thread = threading.Thread(
                target=self._apply_window_style_delayed, daemon=True
            )
            self._style_thread.start()

        if self.app_package:
            self.app_thread = threading.Thread(
                target=self._launch_app, daemon=True
            )
            self.app_thread.start()

        if self.app_package:
            self._start_app_monitor()

    def _read_output(self):
        """Read and print scrcpy output, and parse Android display ID."""
        try:
            while self.running and self.process.poll() is None:
                line = self.process.stdout.readline()
                if not line:
                    break
                # Try multiple encodings
                try:
                    decoded_line = line.decode('utf-8', errors='replace').strip()
                except:
                    try:
                        decoded_line = line.decode('gbk', errors='replace').strip()
                    except:
                        decoded_line = line.decode('latin-1', errors='replace').strip()
                
                if decoded_line:
                    print(f"[{self.window_title}] {decoded_line}")
                    
                    # Parse Android virtual display ID from scrcpy output
                    # Format: "[server] INFO: New display: 540x960/182 (id=641)"
                    if "New display:" in decoded_line and "id=" in decoded_line:
                        import re
                        match = re.search(r'id=(\d+)', decoded_line)
                        if match:
                            self.android_display_id = int(match.group(1))
                            print(f"[{self.window_title}] Parsed Android display ID: {self.android_display_id}")
        except Exception as e:
            if self.running:
                print(f"[{self.window_title}] Output reader error: {e}")

    def _apply_window_style_delayed(self):
        """Wait for scrcpy window to appear, then apply Mac-style card effects (rounded corners + shadow)."""
        try:
            # scrcpy窗口创建需要一点时间，轮询查找窗口
            hwnd = None
            for attempt in range(30):
                time.sleep(0.3)
                hwnd = self._find_window_by_title(self.window_title)
                if hwnd:
                    break
            if not hwnd:
                print(f"[{self.window_title}] Could not find window for styling")
                return

            print(f"[{self.window_title}] Applying Mac card style (corner_radius={self.corner_radius})...")

            if os.name == 'nt':
                self._apply_windows_card_style(hwnd)
            else:
                # Linux/macOS could use other approaches
                pass

        except Exception as e:
            print(f"[{self.window_title}] Window style error: {e}")

    def _find_window_by_title(self, title):
        """Find window HWND by exact title match using Win32 API."""
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
                    # Ensure it's a visible top-level window
                    if user32.IsWindowVisible(hwnd):
                        result[0] = hwnd
                        return 0
            return 1

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)
        return result[0]

    def _apply_windows_card_style(self, hwnd):
        """Apply Mac-style card look to a window on Windows: rounded corners + shadow.
        Uses DWM API (Windows 11 native rounded corners) + SetWindowRgn fallback for older Windows.
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi

        # --- 1. DWM rounded corners (Windows 11) ---
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33
        # Values: DWMWCP_DEFAULT=0, DWMWCP_DONOTROUND=1, DWMWCP_ROUND=2, DWMWCP_ROUNDSMALL=3
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
                print(f"[{self.window_title}] DWM rounded corners applied (DWMWCP_ROUND)")
            else:
                # Win10 or older, use SetWindowRgn fallback
                self._apply_rounded_region(hwnd)
        except Exception as e:
            print(f"[{self.window_title}] DWM rounded corners not available: {e}")
            self._apply_rounded_region(hwnd)

        # --- 2. Drop shadow (ensure it's enabled) ---
        # For borderless windows on Windows, extending a tiny margin into client area
        # tricks DWM into drawing the drop shadow around the window.
        # Using 1px margins (not -1 which would cover content with glass).
        try:
            class MARGINS(ctypes.Structure):
                _fields_ = [("cxLeftWidth", ctypes.c_int),
                            ("cxRightWidth", ctypes.c_int),
                            ("cyTopHeight", ctypes.c_int),
                            ("cyBottomHeight", ctypes.c_int)]
            margins = MARGINS(1, 1, 1, 1)  # 1px frame extension to enable shadow
            dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
            print(f"[{self.window_title}] Drop shadow enabled via DwmExtendFrameIntoClientArea")
        except Exception as e:
            print(f"[{self.window_title}] Shadow setup error: {e}")

        # --- 3. Add CS_DROPSHADOW class style (additional shadow for Win10) ---
        try:
            GCL_STYLE = -26
            CS_DROPSHADOW = 0x00020000
            class_style = user32.GetClassLongW(wintypes.HWND(hwnd), GCL_STYLE)
            user32.SetClassLongW(wintypes.HWND(hwnd), GCL_STYLE, class_style | CS_DROPSHADOW)
        except Exception:
            pass  # non-critical

        # --- 4. Force a repaint ---
        try:
            user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0004 | 0x0020)  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
        except Exception:
            pass

    def _apply_rounded_region(self, hwnd):
        """Fallback: use SetWindowRgn to create rounded corners on older Windows."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        radius = self.corner_radius if self.corner_radius else 20
        rect = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top

        # Create rounded rect region
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        if rgn:
            user32.SetWindowRgn(wintypes.HWND(hwnd), rgn, True)
            print(f"[{self.window_title}] Rounded region applied (radius={radius}) via SetWindowRgn")

    def _launch_app(self):
        """Launch/move the app to this virtual display after it initializes.
        重启虚拟屏（如切换横竖屏）时，app进程通常还在后台运行，优先用am start把已有task
        移到新display，避免冷启动重新走splash页。
        """
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            # Wait for virtual display to fully initialize and parse display ID
            print(f"[{self.window_title}] Waiting for display initialization...")
            max_wait = 15
            waited = 0
            while waited < max_wait and self.android_display_id is None:
                time.sleep(1)
                waited += 1
            
            if self.android_display_id is None:
                print(f"[{self.window_title}] Warning: Could not parse Android display ID, using fallback")
                dumpsys_result = device.shell("dumpsys display")
                import re
                match = re.search(r'Display Id=(\d+).*VirtualDisplay', dumpsys_result, re.DOTALL)
                if match:
                    self.android_display_id = int(match.group(1))
                    print(f"[{self.window_title}] Fallback: Found display ID {self.android_display_id}")
                else:
                    print(f"[{self.window_title}] Error: Could not determine display ID")
                    return
            
            display_id = self.android_display_id
            
            # 在启动app之前，先锁定display的natural方向（wm user-rotation lock 0）。
            # CarPlay式方案核心：横屏VD(w>h)的natural=landscape，lock 0=横屏；
            # 竖屏VD(h>w)的natural=portrait，lock 0=竖屏。
            # 锁定后可防止FLAG_ROTATES_WITH_CONTENT导致强制竖屏app触发display旋转。
            if hasattr(self, 'user_rotation'):
                try:
                    wm_cmd = f"wm user-rotation -d {display_id} lock {self.user_rotation}"
                    wm_result = device.shell(wm_cmd)
                    if wm_result and ("Error" in wm_result or "Unknown" in wm_result or "Exception" in wm_result):
                        print(f"[{self.window_title}] wm user-rotation failed: {wm_result.strip()[:200]}")
                    else:
                        is_landscape_nd = (self.new_display_width or 0) > (self.new_display_height or 0)
                        if self.user_rotation == 0:
                            orient_desc = "natural(横屏)" if is_landscape_nd else "natural(竖屏)"
                        else:
                            orient_desc = f"rot={self.user_rotation}"
                        print(f"[{self.window_title}] Set wm user-rotation to lock {self.user_rotation} ({orient_desc}) on display {display_id}")
                        time.sleep(0.5)
                except Exception as wm_err:
                    print(f"[{self.window_title}] wm user-rotation error (non-critical): {wm_err}")
            
            print(f"[{self.window_title}] Moving app {self.app_package} to display {display_id}...")
            
            # Method 1 (preferred for restart cases): 用 am start 把app task移到新display
            # 这样即使app已在后台运行（重启虚拟屏场景），也只是把已有task bring to front到新display，
            # 而不是冷启动app重新创建Activity走splash页
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
            
            # Method 2 (fallback / cold start): monkey LAUNCHER
            if not am_launched:
                print(f"[{self.window_title}] Trying monkey launcher...")
                monkey_cmd = f"monkey -p {self.app_package} --display {display_id} -c android.intent.category.LAUNCHER 1"
                result = device.shell(monkey_cmd)
                
                if "No activities found" in result or "Error" in result:
                    print(f"[{self.window_title}] Monkey also failed: {result.strip()[:200]}")
                else:
                    print(f"[{self.window_title}] App launched via monkey (cold start)")
            
            # 等待app启动和旋转重绘完成
            if hasattr(self, 'user_rotation') and self.user_rotation != 0:
                time.sleep(2.0)  # 横屏模式等待app完成横屏布局
                
        except Exception as e:
            print(f"[{self.window_title}] Launch app error: {e}")

    def _start_app_monitor(self):
        """Start monitoring thread."""
        self.stop_monitoring = False
        self.monitor_thread = threading.Thread(
            target=self._monitor_app, daemon=True
        )
        self.monitor_thread.start()

    def _monitor_app(self):
        """Monitor if the app is still in foreground on this virtual display."""
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            # Wait for app to start (20 seconds for slower apps)
            print(f"[{self.window_title}] Waiting 20 seconds for app to start...")
            time.sleep(20)
            
            consecutive_failures = 0
            max_failures = 15  # Require 15 consecutive failures before stopping (45 seconds)

            while not self.stop_monitoring and self.running:
                try:
                    # Method 1: Use pidof command (most reliable)
                    pid_result = device.shell(f"pidof {self.app_package}").strip()
                    
                    if pid_result:
                        # App is running
                        if consecutive_failures > 0:
                            print(f"[{self.window_title}] App {self.app_package} found with PID: {pid_result}")
                        consecutive_failures = 0
                    else:
                        # Method 2: Use ps command with exact match
                        ps_result = device.shell(f"ps -A").strip()
                        # Check for exact package name match (not partial)
                        app_running = False
                        for line in ps_result.split('\n'):
                            if self.app_package in line:
                                # Verify it's the exact package name, not a substring
                                parts = line.split()
                                if len(parts) >= 9 and parts[-1] == self.app_package:
                                    app_running = True
                                    break
                        
                        if app_running:
                            if consecutive_failures > 0:
                                print(f"[{self.window_title}] App {self.app_package} found in ps output")
                            consecutive_failures = 0
                        else:
                            # Method 3: Use dumpsys to check if app is in foreground
                            dumpsys_result = device.shell("dumpsys activity activities")
                            if self.app_package in dumpsys_result:
                                if consecutive_failures > 0:
                                    print(f"[{self.window_title}] App {self.app_package} found in foreground")
                                consecutive_failures = 0
                            else:
                                consecutive_failures += 1
                                print(f"[{self.window_title}] App {self.app_package} not found ({consecutive_failures}/{max_failures})")
                                
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
        """Stop this virtual display."""
        if not self.running:
            return

        self.running = False
        self.stop_monitoring = True
        print(f"[{self.window_title}] Stopping virtual display...")

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
        """Wait for this display to exit."""
        if self.process:
            self.process.wait()
            self.running = False


class MultiVirtualDisplay:
    """
    Manage multiple virtual displays on a single Android device.
    Each display shows a different app in its own window.
    """

    def __init__(self, device_serial=None, scrcpy_path=None,
                 turn_screen_off=True, keep_active=True,
                 new_display_width=1260, new_display_height=2844,
                 base_port=8887):
        """
        Initialize multi virtual display manager.

        Args:
            device_serial: ADB device serial number
            scrcpy_path: Path to scrcpy executable
            turn_screen_off: Turn off physical screen
            keep_active: Prevent device auto sleep
            new_display_width: Default virtual display resolution width (default: 1260)
            new_display_height: Default virtual display resolution height (default: 2844)
            base_port: Base port number for scrcpy servers (default: 8887)
        """
        self.device_serial = device_serial
        self.scrcpy_path = scrcpy_path
        self.turn_screen_off = turn_screen_off
        self.keep_active = keep_active
        self.new_display_width = new_display_width
        self.new_display_height = new_display_height
        self.base_port = base_port
        self.displays = []
        self.running = False
        # 注意：主屏固定用port=base_port(8887)，所以AppVirtualDisplay的id从1开始，避免端口冲突
        self._next_display_id = 1
        self._free_display_ids = []  # 已停止display的id池，安全复用以避免端口无限增长

        atexit.register(self.stop_all)

    def _alloc_display_id(self):
        """分配display_id：优先复用已停止display的id(端口已释放)，否则递增。"""
        if self._free_display_ids:
            return self._free_display_ids.pop(0)  # 复用最早释放的id
        did = self._next_display_id
        self._next_display_id += 1
        return did

    def _free_display_id(self, display_id):
        """归还display_id到空闲池（display完全停止后调用）。"""
        if display_id is not None and display_id not in self._free_display_ids:
            self._free_display_ids.append(display_id)

    def add_display(self, app_package, window_x=None, window_y=None,
                    window_width=None, window_height=None,
                    new_display_width=None, new_display_height=None,
                    window_title=None, max_fps=60, bitrate='8M',
                    video_codec='h264', flex_display=False,
                    borderless=False, background_color=None,
                    record=None, rotation=0):
        """
        Add a virtual display for an app.

        Args:
            app_package: Package name of the app
            window_x: Window X position
            window_y: Window Y position
            window_width: Window width
            window_height: Window height
            new_display_width: Virtual display resolution width (uses manager default if None)
            new_display_height: Virtual display resolution height (uses manager default if None)
            window_title: Custom window title
            max_fps: Maximum frames per second
            bitrate: Video bitrate
            video_codec: Video codec
            flex_display: Enable flexible display
            borderless: Borderless window
            background_color: Background color
            record: Record to file
            rotation: Display rotation (0, 90, 180, 270). 0=portrait, 270=landscape

        Returns:
            self for chaining
        """
        display = AppVirtualDisplay(
            app_package=app_package,
            display_id=self._alloc_display_id(),
            window_title=window_title,
            window_x=window_x,
            window_y=window_y,
            window_width=window_width,
            window_height=window_height,
            new_display_width=new_display_width or self.new_display_width,
            new_display_height=new_display_height or self.new_display_height,
            max_fps=max_fps,
            bitrate=bitrate,
            video_codec=video_codec,
            flex_display=flex_display,
            borderless=borderless,
            background_color=background_color,
            keep_active=self.keep_active,
            turn_screen_off=self.turn_screen_off,
            record=record,
            scrcpy_path=self.scrcpy_path,
            device_serial=self.device_serial,
            base_port=self.base_port,
            rotation=rotation,
        )
        self.displays.append(display)
        return self

    def start_all(self):
        """Start all virtual displays sequentially."""
        if self.running:
            print("Multi virtual display already running")
            return

        self.running = True
        print(f"Starting {len(self.displays)} virtual displays...")

        for i, display in enumerate(self.displays):
            print(f"\n[{i+1}/{len(self.displays)}] Starting {display.window_title}...")
            display.start()
            
            # Wait for this display to be ready before starting the next one
            # Check if the app has started by looking for "Starting app" in output
            if display.app_package:
                print(f"Waiting for {display.window_title} to initialize...")
                time.sleep(3)  # Give it time to push server and start
            
            # Additional delay between instances to avoid resource conflicts
            if i < len(self.displays) - 1:
                time.sleep(2)

        print(f"\n✅ All {len(self.displays)} virtual displays started")

    def stop_all(self):
        """Stop all virtual displays."""
        if not self.running:
            return

        self.running = False
        print("Stopping all virtual displays...")

        for display in self.displays:
            display.stop()

        # Cleanup scrcpy-server.jar
        self._cleanup_server()

        print("All virtual displays stopped")

    def _cleanup_server(self):
        """Cleanup scrcpy-server.jar from device."""
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
        """Wait for all displays to exit."""
        threads = []
        for display in self.displays:
            t = threading.Thread(target=display.wait, daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        self.running = False

    def stop_display(self, index):
        """Stop a specific virtual display by index."""
        if 0 <= index < len(self.displays):
            self.displays[index].stop()
        else:
            print(f"Invalid display index: {index}")


def create_desktop_layout(device_serial=None, scrcpy_path=None):
    """
    Create a pre-configured desktop layout with multiple apps.
    Example: 2x2 grid layout.

    Returns:
        MultiVirtualDisplay instance with displays added
    """
    multi = MultiVirtualDisplay(
        device_serial=device_serial,
        scrcpy_path=scrcpy_path,
        turn_screen_off=True,
        keep_active=True
    )

    # Default window size for each virtual display
    w, h = 540, 960
    gap = 10

    # Position configs for 2x2 grid
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
    parser.add_argument(
        "--serial", "-s", type=str,
        help="Device serial number"
    )
    parser.add_argument(
        "--apps", "-a", type=str, nargs="+",
        help="Package names of apps to launch (e.g., com.taobao.taobao com.eg.android.AlipayGphone)"
    )
    parser.add_argument(
        "--layout", type=str, choices=["grid", "horizontal", "vertical", "free"],
        default="grid",
        help="Window layout (default: grid)"
    )
    parser.add_argument(
        "--window-width", type=int, default=540,
        help="Width of each window (default: 540)"
    )
    parser.add_argument(
        "--window-height", type=int, default=960,
        help="Height of each window (default: 960)"
    )
    parser.add_argument(
        "--max-fps", type=int, default=60,
        help="Maximum frames per second (default: 60)"
    )
    parser.add_argument(
        "--bitrate", type=str, default='8M',
        help="Video bitrate (default: 8M)"
    )
    parser.add_argument(
        "--video-codec", type=str, default='h264',
        choices=["h264", "h265", "av1"],
        help="Video codec (default: h264)"
    )
    parser.add_argument(
        "--turn-screen-off", action="store_true", default=True,
        help="Turn off physical screen"
    )
    parser.add_argument(
        "--no-turn-screen-off", action="store_true",
        help="Keep physical screen on"
    )
    parser.add_argument(
        "--background-color", type=str, default="#2a2a2a",
        help="Background color (default: #2a2a2a)"
    )
    parser.add_argument(
        "--flex-display", action="store_true",
        help="Enable flexible display mode"
    )
    parser.add_argument(
        "--borderless", action="store_true",
        help="Borderless windows"
    )

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
            # 2xN grid layout
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
        else:  # free
            x = gap + i * (w + gap)
            y = gap

        multi.add_display(
            app_package=app,
            window_x=x,
            window_y=y,
            window_width=w,
            window_height=h,
            window_title=app.split(".")[-1],
            max_fps=args.max_fps,
            bitrate=args.bitrate,
            video_codec=args.video_codec,
            flex_display=args.flex_display,
            borderless=args.borderless,
            background_color=args.background_color
        )

    try:
        multi.start_all()
        multi.wait_all()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        multi.stop_all()
