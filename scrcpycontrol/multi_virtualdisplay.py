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

        # Use UTF-8 encoding to handle Chinese characters
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # Unbuffered
            env=env
        )

        self.running = True
        self.stop_event.clear()
        print(f"[{self.window_title}] Main display started (PID: {self.process.pid})")

        # Start output reader thread
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
                 max_fps=60, bitrate='8M', video_codec='h264',
                 no_vd_system_decorations=True, flex_display=False,
                 borderless=False, background_color=None,
                 keep_active=True, turn_screen_off=True, stay_awake=True,
                 record=None, scrcpy_path=None, device_serial=None,
                 base_port=8887):
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
            max_fps: Maximum frames per second
            bitrate: Video bitrate (e.g., '8M', '16M')
            video_codec: Video codec ('h264', 'h265', 'av1')
            no_vd_system_decorations: Hide status bar and navigation bar
            flex_display: Enable flexible display (resize with window drag)
            borderless: Borderless window
            background_color: Background color (e.g., '#2a2a2a')
            keep_active: Prevent device auto sleep
            turn_screen_off: Turn off physical screen
            stay_awake: Keep device awake (prevent screen timeout)
            record: Record to file
            scrcpy_path: Path to scrcpy executable
            device_serial: ADB device serial number
            base_port: Base port number for scrcpy server (default: 8887)
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
        self.max_fps = max_fps
        self.bitrate = bitrate
        self.video_codec = video_codec
        self.no_vd_system_decorations = no_vd_system_decorations
        self.flex_display = flex_display
        self.borderless = borderless
        self.background_color = background_color
        self.keep_active = keep_active
        self.turn_screen_off = turn_screen_off
        self.stay_awake = stay_awake
        self.record = record
        self.device_serial = device_serial
        self.base_port = base_port

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

        # Create new virtual display with resolution
        nd_w = self.new_display_width
        nd_h = self.new_display_height
        if nd_w and nd_h:
            cmd.append(f"--new-display={nd_w}x{nd_h}")
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

        # Virtual display decorations
        if self.no_vd_system_decorations:
            cmd.append("--no-vd-system-decorations")

        # Flex display
        if self.flex_display:
            cmd.append("--flex-display")

        # Borderless
        if self.borderless:
            cmd.append("--window-borderless")

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

        # Use UTF-8 encoding to handle Chinese characters
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # Unbuffered
            env=env
        )

        self.running = True
        print(f"[{self.window_title}] Virtual display started (PID: {self.process.pid})")

        # Start output reader thread
        self.output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self.output_thread.start()

        # Start app launch thread (wait for display to initialize, then launch app)
        if self.app_package:
            self.app_thread = threading.Thread(
                target=self._launch_app, daemon=True
            )
            self.app_thread.start()

        # Start app monitor
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

    def _launch_app(self):
        """Launch the app on this virtual display after it initializes."""
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
                # Fallback: try to get display ID from dumpsys
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
            
            # Try to launch app using monkey command with display ID
            print(f"[{self.window_title}] Launching app {self.app_package} on display {display_id}...")
            
            # Method 1: Use monkey to launch on specific display
            monkey_cmd = f"monkey -p {self.app_package} --display {display_id} -c android.intent.category.LAUNCHER 1"
            result = device.shell(monkey_cmd)
            
            if "No activities found" in result or "Error" in result:
                # Method 2: Try am start with display flag
                print(f"[{self.window_title}] Monkey failed, trying am start...")
                # Get launch intent
                intent_result = device.shell(f"cmd package resolve-activity --brief {self.app_package}")
                if intent_result and "/" in intent_result:
                    # Extract activity name
                    lines = intent_result.strip().split('\n')
                    for line in lines:
                        if '/' in line and not line.startswith('#'):
                            activity = line.strip()
                            break
                    else:
                        activity = intent_result.strip().split('\n')[-1]
                    
                    am_cmd = f"am start -n {activity} --display {display_id}"
                    result = device.shell(am_cmd)
                    
                    if "Error" in result or "Exception" in result:
                        print(f"[{self.window_title}] Failed to launch app: {result}")
                    else:
                        print(f"[{self.window_title}] App launched successfully")
            else:
                print(f"[{self.window_title}] App launched via monkey")
                
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

        atexit.register(self.stop_all)

    def add_display(self, app_package, window_x=None, window_y=None,
                    window_width=None, window_height=None,
                    new_display_width=None, new_display_height=None,
                    window_title=None, max_fps=60, bitrate='8M',
                    video_codec='h264', flex_display=False,
                    borderless=False, background_color=None,
                    record=None):
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

        Returns:
            self for chaining
        """
        display = AppVirtualDisplay(
            app_package=app_package,
            display_id=len(self.displays),
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
            base_port=self.base_port
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
