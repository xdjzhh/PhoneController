"""
Multi Virtual Display implementation using scrcpy v4.0+ --new-display feature.
Supports creating multiple virtual displays on a single Android device,
each showing a different app, like a desktop environment.

All windows use Mac-style card decoration by default (borderless, always-on-top,
rounded corners, drop shadow, traffic lights, drag & resize).
Functional parameters (position, size, resolution, bitrate) are
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


# ─────────────────────────────────────────────────────────────────────────────
# 进程级 scrcpy 启动握手互斥锁
#
# 根因：scrcpy 默认走 reverse tunnel，设备端 abstract socket 名固定为
# "localabstract:scrcpy"，而 `adb reverse` 映射是【整机全局】状态。两个
# scrcpy 实例并发启动时（一个还在 push server / 等待 server 连接，另一个
# 就执行了 adb reverse），后到的 reverse 映射会覆盖前者，先启动者的 server
# 连上错误的 client → client 端打印 "Aborted" /
# "ERROR: Server connection failed" 硬失败；同时并发 adb push 到同一个
# /data/local/tmp/scrcpy-server 文件也可能互相破坏。
#
# 策略（来自既有故障经验）：只串行化「Popen → adb push → adb reverse →
# app_process → server 连接建立」这段共享 ADB 资源窗口；握手成功后立即放
# 锁，窗口装饰 / app 拉起 / 监控等其余阶段继续并行。禁止用固定 sleep 充当
# 唯一手段——必须以 server 实际握手信号为准。
_SCRCPY_BOOT_LOCK = threading.Lock()
_SCRCPY_HANDSHAKE_TIMEOUT = 45

# 握手窗口内出现这些行 = 启动链路硬失败（区别于退出阶段的 "Stream closed"）
_BOOT_FAILURE_MARKERS = (
    "server connection failed",
    "aborted",
    "could not find any adb device",
    "device not found",
    "adb: error",
    "failed to push",
)


def _is_boot_failure_line(line):
    low = (line or "").lower()
    return any(marker in low for marker in _BOOT_FAILURE_MARKERS)


def _teardown_failed_process(process, window_title):
    """启动失败路径：等进程退出、抽取关键输出、兜底 kill。返回输出尾部文本。"""
    tail = ""
    try:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            raw = process.stdout.read()
            if raw:
                tail = raw.decode('utf-8', errors='replace')
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    except Exception as e:
        print(f"[{window_title}] Teardown error after boot failure: {e}")
    return tail


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

    def __init__(self, config: WindowConfig, scrcpy_path=None, device_serial=None,
                 display_manager=None):
        """
        Initialize MainDisplay.

        Args:
            config: WindowConfig VO with all display parameters.
            scrcpy_path: Path to scrcpy executable. Auto-detected if None.
            device_serial: ADB device serial. Auto-detected if None.
            display_manager: Reference to DisplayManager for callbacks (optional).
        """
        self.config = config
        self.window_title = config.window_title
        self.device_serial = device_serial
        self.display_manager = display_manager

        self.scrcpy_path = scrcpy_path or self._find_scrcpy()
        self.process = None
        self.running = False
        self.output_thread = None
        self.stop_event = threading.Event()
        self._hwnd = None
        self._style_thread = None
        # 握手状态：server 连接建立前，scrcpy 启动全程持有进程级互斥锁
        self._server_ready = threading.Event()
        self._boot_error = None
        self._handshake_done = False

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
        if cfg.video_buffer_ms:
            cmd.extend(["--video-buffer", str(cfg.video_buffer_ms)])

        # Behavior flags
        if cfg.keep_active:
            cmd.append("--keep-active")
        if cfg.turn_screen_off:
            cmd.append("--turn-screen-off")
        if cfg.stay_awake:
            cmd.append("--stay-awake")
        if not cfg.audio:
            # 该卡片静音：不建立音频流，多卡片同时播放串音问题消除，也省音频通道带宽
            cmd.append("--no-audio")

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

    def _wait_handshake(self):
        """
        在 _SCRCPY_BOOT_LOCK 内同步等待 scrcpy server 握手完成。

        成功标记（由 _read_output 解析到该行时 set event）：
          主屏：'[server] INFO: Device: ...'（server 已连上本 client 并交换
                options，此后 adb reverse 映射即使被别的实例覆盖也与本实例无关）
        失败标记：'Server connection failed' / 'Aborted' 等硬失败行，
                或 scrcpy 进程提前退出。
        超时：进程仍存活但迟迟没有标记 → 保守放行（仅打印告警），避免在极端
                慢设备上误杀一个其实正常的实例。
        """
        start_time = time.time()
        while time.time() - start_time < _SCRCPY_HANDSHAKE_TIMEOUT:
            if self._boot_error is not None:
                return False
            if self.process.poll() is not None:
                if self._boot_error is None:
                    self._boot_error = f"scrcpy exited early (code={self.process.returncode})"
                return False
            if self._server_ready.is_set():
                self._handshake_done = True
                return True
            time.sleep(0.3)

        print(f"[{self.window_title}] WARNING: handshake marker not seen within "
              f"{_SCRCPY_HANDSHAKE_TIMEOUT}s (process still alive, continuing optimistically)")
        self._handshake_done = True
        return True

    def start(self, port=8887):
        """
        Start the main scrcpy display process and apply card styling.

        Returns:
            True if the scrcpy server handshake succeeded, False on hard
            startup failure (caller MUST check this instead of only reading
            .running).
        """
        if self.running:
            print(f"[{self.window_title}] Already running")
            return True

        cmd = self._build_command(port)
        print(f"[{self.window_title}] Starting: {' '.join(cmd)}")

        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        # 关键区：push server → adb reverse → app_process → server 连上本
        # client。此窗口内整机只允许一个 scrcpy 实例处于该阶段。
        with _SCRCPY_BOOT_LOCK:
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=env
                )
            except Exception as e:
                print(f"[{self.window_title}] FAILED to launch scrcpy: {e}")
                self.process = None
                self.running = False
                return False

            self.running = True
            self.stop_event.clear()
            self._server_ready.clear()
            self._boot_error = None
            self._handshake_done = False
            print(f"[{self.window_title}] Main display started (PID: {self.process.pid})")

            # 输出线程必须在等待标记前就启动，否则会漏掉 server 的早期日志
            self.output_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self.output_thread.start()

            handshake_ok = self._wait_handshake()

        if not handshake_ok:
            print(f"[{self.window_title}] ❌ FAILED to start: {self._boot_error}")
            tail = _teardown_failed_process(self.process, self.window_title)
            if tail:
                # 只打印与失败强相关的行，避免多线程输出里关键信息被淹没
                key_lines = [ln for ln in tail.splitlines()
                             if ln.strip() and (_is_boot_failure_line(ln)
                                               or 'server' in ln.lower()
                                               or 'error' in ln.lower()
                                               or 'pushed' in ln.lower())]
                snippet = "\n".join(key_lines[-12:]) if key_lines else tail[-800:]
                print(f"[{self.window_title}] scrcpy output tail:\n{snippet}")
            self.process = None
            self.running = False
            return False

        # 握手已完成 → 窗口装饰等非关键工作并行进行
        self._style_thread = threading.Thread(
            target=self._apply_card_style_delayed, daemon=True
        )
        self._style_thread.start()
        return True

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
                    self._scan_handshake_line(decoded_line)
        except Exception as e:
            if self.running and not self.stop_event.is_set():
                print(f"[{self.window_title}] Output reader error: {e}")

    def _scan_handshake_line(self, decoded_line):
        """从 scrcpy 输出行中识别握手成功 / 硬失败标记（仅握手窗口内生效）。"""
        if self._handshake_done:
            return
        low = decoded_line.lower()
        # 主屏成功标记：[server] INFO: Device: <model> (Android x)
        if "[server]" in low and "device:" in low:
            self._server_ready.set()
            return
        if _is_boot_failure_line(decoded_line):
            self._boot_error = decoded_line
            self._server_ready.set()

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

            # Apply default opacity from config
            try:
                if self.display_manager:
                    cfg = self.display_manager.load_config()
                    opacity = cfg.get("main_display", {}).get("default_opacity", 1.0)
                    if opacity < 1.0:
                        self.display_manager._apply_opacity_to_hwnd(hwnd, opacity)
                        print(f"[{self.window_title}] Applied default opacity: {opacity}")
            except Exception as oe:
                print(f"[{self.window_title}] Failed to apply opacity: {oe}")

            # Register with the global card decorator for traffic lights + drag/resize
            try:
                decorator = get_card_window_decorator()
                _self_ref = self

                def _on_close_main():
                    """Callback for red close button: stop this display."""
                    _self_ref.stop()

                def _on_geom(x, y, w, h):
                    """Callback when drag/resize ends: persist geometry."""
                    if _self_ref.display_manager:
                        _self_ref.display_manager._save_window_geometry(None, x, y, w, h)

                decorator.register_window(hwnd, on_close=_on_close_main,
                                          on_geometry_changed=_on_geom)
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
    Resolution is automatically matched to window aspect ratio (wide/tall) to
    avoid letterbox black bars.
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
        # 握手状态：server 建连并创建虚拟屏前，scrcpy 启动全程持有进程级互斥锁
        self._server_ready = threading.Event()
        self._boot_error = None
        self._handshake_done = False

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
          directly from the window aspect ratio (handled by WindowConfig.resolve_virtual_display_size)
        - --no-vd-system-decorations hides status/nav bars on the virtual screen
        - Mac card style defaults (borderless + always-on-top + dark background)
          are hardcoded, same as MainDisplay
        """
        cmd = [self.scrcpy_path]
        cfg = self.config

        if self.device_serial:
            cmd.extend(["-s", self.device_serial])

        # ── Virtual display resolution ──
        # WindowConfig.resolve_virtual_display_size selects wide vs tall VD
        # automatically using window aspect ratio (w >= h → wide).
        nd_w, nd_h, nd_dpi = cfg.resolve_virtual_display_size()
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
        if cfg.video_buffer_ms:
            cmd.extend(["--video-buffer", str(cfg.video_buffer_ms)])

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
        if not cfg.audio:
            # 该卡片静音：不建立音频流，避免多卡片同时出声，并省音频通道带宽
            cmd.append("--no-audio")

        # ── Mac card style defaults (hardcoded) ──
        cmd.append("--window-borderless")
        cmd.append("--always-on-top")
        cmd.extend(["--background-color", CARD_BACKGROUND_COLOR])

        # Port: base_port + display_id for unique control socket
        port = self.base_port + (self.display_id or 0)
        cmd.extend(["--port", str(port)])

        return cmd

    def _wait_handshake(self):
        """
        在 _SCRCPY_BOOT_LOCK 内同步等待 scrcpy server 握手 + 虚拟屏创建完成。

        成功标记：'[server] INFO: New display: WxH/dpi (id=N)'
        （该行出现时 server 已连上本 client、VirtualDisplay 已创建，且
        android_display_id 已解析——这是后续 am start --display 的前置条件）
        失败标记：'Server connection failed' / 'Aborted' 等硬失败行，
        或 scrcpy 进程提前退出。
        """
        start_time = time.time()
        while time.time() - start_time < _SCRCPY_HANDSHAKE_TIMEOUT:
            if self._boot_error is not None:
                return False
            if self.process.poll() is not None:
                if self._boot_error is None:
                    self._boot_error = f"scrcpy exited early (code={self.process.returncode})"
                return False
            if self._server_ready.is_set():
                self._handshake_done = True
                return True
            time.sleep(0.3)

        print(f"[{self.window_title}] WARNING: 'New display:' marker not seen within "
              f"{_SCRCPY_HANDSHAKE_TIMEOUT}s (process still alive, continuing optimistically)")
        self._handshake_done = True
        return True

    def start(self):
        """
        Start the app virtual display: launch scrcpy, apply style, launch app.

        Returns:
            True if the scrcpy server handshake succeeded and the Android
            virtual display ID was parsed, False on hard startup failure.
            Callers MUST check the return value (not only .running).
        """
        if self.running:
            print(f"[{self.window_title}] Already running")
            return True

        cmd = self._build_command()
        print(f"[{self.window_title}] Starting: {' '.join(cmd)}")

        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'

        # 关键区：push server → adb reverse → app_process → server 连接 →
        # VirtualDisplay 创建。此窗口内整机只允许一个 scrcpy 实例处于该阶段，
        # 否则后启动者的 adb reverse（设备端 abstract socket 名固定）会顶掉
        # 本实例的映射 → "Aborted / Server connection failed"。
        with _SCRCPY_BOOT_LOCK:
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=env
                )
            except Exception as e:
                print(f"[{self.window_title}] FAILED to launch scrcpy: {e}")
                self.process = None
                self.running = False
                return False

            self.running = True
            self._server_ready.clear()
            self._boot_error = None
            self._handshake_done = False
            print(f"[{self.window_title}] Virtual display started (PID: {self.process.pid})")

            # 输出线程必须先启动，'New display:' 行由它解析并唤醒握手等待
            self.output_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self.output_thread.start()

            handshake_ok = self._wait_handshake()

        if not handshake_ok:
            print(f"[{self.window_title}] ❌ FAILED to start: {self._boot_error}")
            tail = _teardown_failed_process(self.process, self.window_title)
            if tail:
                key_lines = [ln for ln in tail.splitlines()
                             if ln.strip() and (_is_boot_failure_line(ln)
                                               or 'new display' in ln.lower()
                                               or 'server' in ln.lower()
                                               or 'pushed' in ln.lower())]
                snippet = "\n".join(key_lines[-12:]) if key_lines else tail[-800:]
                print(f"[{self.window_title}] scrcpy output tail:\n{snippet}")
            self.process = None
            self.running = False
            return False

        # 握手完成 → 窗口装饰 / app 拉起 / 监控全部并行
        self._style_thread = threading.Thread(
            target=self._apply_card_style_delayed, daemon=True
        )
        self._style_thread.start()

        # Launch the app on the virtual display (or show special UI)
        if self.app_package:
            special_mode = getattr(self.config, 'special_mode', None)
            if special_mode == "notification_panel":
                self.app_thread = threading.Thread(
                    target=self._show_notification_panel, daemon=True
                )
            else:
                self.app_thread = threading.Thread(
                    target=self._launch_app, daemon=True
                )
            self.app_thread.start()

        # Monitor if the app closes, auto-stop the display (skip for special modes)
        if self.app_package and getattr(self.config, 'special_mode', None) is None:
            self._start_app_monitor()
        return True

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
                    # 该行同时也是握手成功标记（server 已建连、VD 已创建）
                    if "New display:" in decoded_line and "id=" in decoded_line:
                        match = re.search(r'id=(\d+)', decoded_line)
                        if match:
                            self.android_display_id = int(match.group(1))
                            print(f"[{self.window_title}] Parsed Android display ID: {self.android_display_id}")
                            if not self._handshake_done:
                                self._server_ready.set()
                    elif not self._handshake_done and _is_boot_failure_line(decoded_line):
                        # Aborted / Server connection failed 等硬失败 → 唤醒握手等待
                        self._boot_error = decoded_line
                        self._server_ready.set()
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

            # Apply default opacity from config
            try:
                _dm = self._display_manager
                if _dm:
                    cfg = _dm.load_config()
                    opacity = cfg.get("app_displays", {}).get(self.app_package, {}).get("default_opacity", 1.0)
                    if opacity < 1.0:
                        _dm._apply_opacity_to_hwnd(hwnd, opacity)
                        print(f"[{self.window_title}] Applied default opacity: {opacity}")
            except Exception as oe:
                print(f"[{self.window_title}] Failed to apply opacity: {oe}")

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

                def _on_geom(x, y, w, h):
                    """Callback when drag/resize ends: persist geometry."""
                    if _dm:
                        if _dm._navigating_app == _pkg:
                            _dm._save_nav_geometry(_pkg, x, y, w, h)
                        else:
                            _dm._save_window_geometry(_pkg, x, y, w, h)

                decorator.register_window(hwnd, on_close=_on_close_app,
                                          on_geometry_changed=_on_geom)
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

    @staticmethod
    def _parse_scrcpy_display_id_from_dump(dumpsys_result):
        """
        从 `dumpsys display` 输出中定位 scrcpy 创建的虚拟屏 ID。

        旧实现 re.search(r'Display Id=(\\d+).*VirtualDisplay', DOTALL) 是
        贪婪匹配：当虚拟屏根本没创建成功时，它会从物理屏的 'Display Id=0'
        一路匹配到 dump 后文任意一处 'VirtualDisplay' 字样 → 错误返回 0 →
        app 被 am start 到物理屏（日志里 "Fallback: Found display ID 0"
        的根因）。这里按 'Display Id=' 切块逐段判断：
          · 永远排除 display 0（物理屏）
          · 只接受块内带 scrcpy / virtualdisplay / virtual 标识的段
          · 多实例时取最大 ID（最新创建）
        找不到返回 None——调用方必须放弃启动，绝不允许落到 display 0。
        """
        if not dumpsys_result:
            return None
        candidates = []
        for block in re.split(r'(?=^Display Id=)', dumpsys_result,
                             flags=re.MULTILINE):
            m = re.match(r'Display Id=(\d+)', block)
            if not m:
                continue
            did = int(m.group(1))
            if did == 0:
                continue
            low = block.lower()
            if ('scrcpy' in low or 'virtualdisplay' in low
                    or 'virtual:' in low or 'uniqueid=virtual' in low):
                candidates.append(did)
        if not candidates:
            return None
        return max(candidates)

    def _show_notification_panel(self):
        """
        Show the Android notification shade on this virtual display.
        Special mode for notification_panel cards:
        1. Launch the default home/launcher on the VD (so it's not black)
        2. Wait for the home screen to render with system decorations (status/nav bars)
        3. Swipe down from the top to expand the notification shade
        """
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            print(f"[{self.window_title}] Waiting for display initialization...")
            max_wait = 15
            waited = 0
            while waited < max_wait and self.android_display_id is None:
                time.sleep(1)
                waited += 1

            if self.android_display_id is None:
                dumpsys_result = device.shell("dumpsys display")
                fallback_id = self._parse_scrcpy_display_id_from_dump(dumpsys_result)
                if fallback_id is not None:
                    self.android_display_id = fallback_id
                    print(f"[{self.window_title}] Fallback: Found scrcpy virtual display ID {self.android_display_id}")
                else:
                    print(f"[{self.window_title}] Error: Could not determine virtual display ID "
                          f"(no scrcpy VirtualDisplay in dumpsys; NOT falling back to display 0)")
                    return

            display_id = self.android_display_id

            # Step 1: Launch home/launcher on the virtual display so it's not black
            # Use NEW_TASK | MULTIPLE_TASK flags to create a separate instance on the VD
            # without moving the home task away from the main display.
            print(f"[{self.window_title}] Starting home screen on display {display_id}...")
            home_launched = False
            home_cmds = [
                f"am start -a android.intent.action.MAIN -c android.intent.category.HOME -f 0x18000000 --display {display_id}",
                f"am start -a android.intent.action.MAIN -c android.intent.category.HOME --display {display_id}",
            ]
            for hcmd in home_cmds:
                try:
                    result = device.shell(hcmd)
                    if result and "Error" not in result and "Exception" not in result:
                        print(f"[{self.window_title}] Home launched via: {hcmd}")
                        home_launched = True
                        break
                except Exception:
                    continue

            if not home_launched:
                print(f"[{self.window_title}] Trying monkey for home...")
                try:
                    device.shell(f"monkey -p com.android.launcher3 --display {display_id} -c android.intent.category.HOME 1")
                    home_launched = True
                except Exception:
                    pass

            # Safety: ensure home is still on main display (display 0) in case it moved
            try:
                time.sleep(0.5)
                device.shell("am start -a android.intent.action.MAIN -c android.intent.category.HOME --display 0")
            except Exception:
                pass

            # Wait for home screen to render
            print(f"[{self.window_title}] Waiting for home screen to settle...")
            time.sleep(3)

            # Step 2: Expand notification panel on this display
            print(f"[{self.window_title}] Expanding notification panel on display {display_id}...")

            # Try cmd statusbar first (with display targeting)
            expand_cmds = [
                f"cmd statusbar expand-notifications --display {display_id}",
                f"cmd statusbar expand-notifications -d {display_id}",
                "cmd statusbar expand-notifications",
            ]
            expanded = False
            for cmd in expand_cmds:
                try:
                    result = device.shell(cmd)
                    if result and ("Error" not in result and "Exception" not in result and "Unknown" not in result and "Usage" not in result):
                        print(f"[{self.window_title}] Notification panel expanded via: {cmd}")
                        expanded = True
                        break
                except Exception:
                    continue

            # Step 3: Swipe down from top as reliable fallback
            # Use display-specific input injection to target the virtual display
            print(f"[{self.window_title}] Sending swipe-down gesture on display {display_id}...")
            nd_w, nd_h, _ = self.config.resolve_virtual_display_size()
            sx = nd_w // 2
            sy = 5
            ex = nd_w // 2
            ey = nd_h // 2
            swipe_cmds = [
                f"input -d {display_id} swipe {sx} {sy} {ex} {ey} 400",
                f"input swipe {sx} {sy} {ex} {ey} 400",
            ]
            for scmd in swipe_cmds:
                try:
                    device.shell(scmd)
                    print(f"[{self.window_title}] Swipe sent via: {scmd}")
                    time.sleep(0.5)
                except Exception:
                    continue

            if expanded:
                print(f"[{self.window_title}] Notification panel ready")
            else:
                print(f"[{self.window_title}] Notification swipe sent (home screen with status bar visible)")

        except Exception as e:
            print(f"[{self.window_title}] Notification panel error: {e}")
            import traceback
            traceback.print_exc()

    def _launch_app(self):
        """
        Launch or move the target app to this virtual display.

        Process:
        1. Wait for virtual display to initialize and parse display ID
        2. Try `am start --display` first (no cold start if app is already running)
        3. Fall back to `monkey` launcher for cold starts
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
                fallback_id = self._parse_scrcpy_display_id_from_dump(dumpsys_result)
                if fallback_id is not None:
                    self.android_display_id = fallback_id
                    print(f"[{self.window_title}] Fallback: Found scrcpy virtual display ID {self.android_display_id}")
                else:
                    # 绝不能像旧实现那样错误回退到 display 0——那会把 app
                    # 直接拉到物理屏上（高德地图事故的第二环）。
                    print(f"[{self.window_title}] Error: Could not determine virtual display ID "
                          f"(no scrcpy VirtualDisplay in dumpsys; aborting app launch, "
                          f"NOT falling back to display 0)")
                    return

            display_id = self.android_display_id

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
        """Remove the pushed scrcpy server binary from the device to avoid stale state."""
        try:
            if self.device_serial:
                device = adbutils.adb.device(self.device_serial)
            else:
                devices = adbutils.adb.device_list()
                if not devices:
                    return
                device = devices[0]

            # scrcpy v4.x 推送的是无后缀二进制 scrcpy-server；旧版本是 .jar
            device.shell("rm -f /data/local/tmp/scrcpy-server "
                         "/data/local/tmp/scrcpy-server.jar")
            print("Cleaned up scrcpy-server binary on device")
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
