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
import re
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

        # 声明 ConfigManager 身份：DisplayManager 维护 main_display / app_displays
        try:
            from scrcpycontrol.config_manager import ConfigManager, OWNER_DISPLAY_MANAGER
            ConfigManager.get(config_path=self.config_path).set_owner(OWNER_DISPLAY_MANAGER)
        except Exception as e:
            print(f"[DisplayManager] WARNING: set_owner failed: {e}", flush=True)

        if scrcpy_path is None:
            scrcpy_path = self._find_scrcpy()
        self.scrcpy_path = scrcpy_path

        if device_serial is None:
            device_serial = self._detect_device()
        self.device_serial = device_serial

        self._main_display = None
        self._main_display_running = False
        self._multi_display = None
        self._app_display_map = {}
        self._lock = threading.Lock()
        self._initialized = False

        # Idempotency cache: avoid re-launching the same no-config app on
        # main display within a short window. Prevents double-am-start bugs
        # when callers invoke start_app_display twice for the same package.
        self._main_launch_cache = {}   # pkg → last launch timestamp
        self._MAIN_LAUNCH_DEDUP_SEC = 20

        self._screen_monitor_thread = None
        self._screen_monitor_stop = False
        self._screen_monitor_running = False

        # 启动孤儿回收：本机任何 scrcpy 启动前，清掉设备端上一轮残留的
        # scrcpy server / --new-display 僵尸虚拟屏（每进程只执行一次）
        self._orphans_reaped = False

        # 全局布局编辑模式（Dock 齿轮）：开启时所有 scrcpy 卡片
        # （主屏 + 天气/地图等 app 卡片）显示红绿灯与缩放热区。
        # 晚启动的卡片在 register_window 后读取此状态继承外观。
        self._global_edit_mode = False

        # Navigation monitor for map auto-zoom
        self._nav_monitor = None
        self._navigating_app = None  # 当前正在导航的地图app包名
        # 注：不再使用 _app_saved_geometry，导航/非导航尺寸直接从 config 读取

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
        """Detect first connected ADB device.
        If no device is found, print a full, actionable Chinese diagnostic so the
        user understands exactly why scrcpy 4.1 later reports "Could not find any
        ADB device" and what to do next.
        """
        try:
            import adbutils
            devices = adbutils.adb.device_list()
            if devices:
                print(f"[DisplayManager] ADB 检测到 {len(devices)} 台设备，使用: {devices[0].serial}")
                return devices[0].serial
        except Exception as e:
            print(f"[DisplayManager] 警告: ADB 设备枚举异常: {e}")

        # --- No ADB device found.  Print a human-readable fix guide immediately ---
        print("=" * 78, flush=True)
        print("[DisplayManager] ❌ 未检测到任何 ADB 设备 → scrcpy 4.1 必然报错：", flush=True)
        print("                  ERROR: Could not find any ADB device", flush=True)
        print("                  ERROR: Server connection failed", flush=True)
        print("", flush=True)
        print("请按下列【按场景分步骤】修复，完成后再次运行 `adb devices` 确认列表有设备：", flush=True)
        print("", flush=True)
        print("  场景 A : 真机用 USB 线连接 (推荐)", flush=True)
        print("    ① 手机: 设置 → 关于手机 → 连点 7 次「版本号」开启开发者选项", flush=True)
        print("    ② 手机: 设置 → 开发者选项 → 打开【USB 调试】", flush=True)
        print("    ③ 电脑: 换一根支持「数据传输」的 USB 线 (纯充电线不行)", flush=True)
        print("    ④ 插上后手机会弹【允许 USB 调试】，必须勾选「始终允许」→ 确定", flush=True)
        print("", flush=True)
        print("  场景 B : 本地模拟器 (夜神/雷电/MuMu/逍遥/Hyper-V 安卓模拟器)", flush=True)
        print("    模拟器必须先手动执行 ADB 连接命令 (按模拟器端口查官方文档，常见端口：", flush=True)
        print("      夜神 127.0.0.1:62001  雷电 127.0.0.1:5555  MuMu 127.0.0.1:7555", flush=True)
        print("      逍遥 127.0.0.1:21503  BlueStacks 127.0.0.1:5555)", flush=True)
        print("    示例: adb connect 127.0.0.1:5555", flush=True)
        print("", flush=True)
        print("  场景 C : 无线调试 (Wifi ADB)", flush=True)
        print("    先让手机和电脑在同一 Wi-Fi，手机开发者选项里打开「无线调试」→", flush=True)
        print("    配对码配对: adb pair <ip>:<pair_port> → adb connect <ip>:<debug_port>", flush=True)
        print("", flush=True)
        print("  万能修复（adb server 卡住时立即执行）:", flush=True)
        print("    adb kill-server", flush=True)
        print("    adb start-server", flush=True)
        print("    adb devices  <-- 此时 List of devices attached 下方必须至少有一行", flush=True)
        print("=" * 78, flush=True)
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
        """Load display configuration from JSON file.

        唯一真相源：ConfigManager 单例。
        self._config 直接绑定单例内部缓存对象的引用 → 后续原地修改 app_displays /
        main_display 等节就是直接刷新 ConfigManager 缓存，由 ConfigManager 统一写磁盘。
        """
        from scrcpycontrol.config_manager import ConfigManager
        mgr = ConfigManager.get(config_path=self.config_path)
        self._config = mgr.get_config_object()
        return self._config

    def reload_config(self):
        """Force reload configuration from disk (覆盖当前缓存)。"""
        from scrcpycontrol.config_manager import ConfigManager
        mgr = ConfigManager.get(config_path=self.config_path)
        self._config = mgr.reload(from_disk=True)
        return self._config

    def save_config(self):
        """Save current configuration back to JSON file (立即写磁盘)。

        真正的写逻辑由 ConfigManager 统一负责：写前自动与磁盘最新 JSON 合并、
        原子写磁盘，不会覆盖 Qt 窗口或其他进程刚写入的新坐标/尺寸。
        """
        from scrcpycontrol.config_manager import ConfigManager
        ConfigManager.get(config_path=self.config_path).save_now()

    @property
    def config(self):
        """Get config property - loaded configuration."""
        return self.load_config()

    def start_main_display(self):
        """
        Start the main display (phone screen mirror).

        Returns:
            Dict with success status and display info.
        """
        with self._lock:
            return self._start_main_display_locked()

    def _start_main_display_locked(self):
        """Start main display assuming lock is already held."""

        if self._main_display_running and self._main_display and self._main_display.running:
            return {
                "success": True,
                "message": "Main display already running",
                "display_type": "main",
            }

        cfg = self.load_config()
        main_cfg = cfg.get("main_display", {})

        # ── default_load 开关 (用户需求：default_load=false 不创建窗口卡片) ──
        #    缺省默认 True (兼容未写该字段的旧配置)；False 时直接 SKIP，不启动 scrcpy。
        default_load = bool(main_cfg.get("default_load", True))
        if not default_load:
            msg = "main_display.default_load=false → SKIP starting main display (no window card created)"
            print(f"[DisplayManager] ⏭️  {msg}", flush=True)
            return {
                "success": True,
                "skipped": True,
                "default_load": False,
                "message": msg,
                "display_type": "main",
            }

        # Early-fail: no ADB device detected -> scrcpy will 100% fail.
        # Skip actually invoking scrcpy to avoid confusing stderr.
        if not self.device_serial:
            msg = (
                "未检测到 ADB 设备，已跳过启动主屏（请按上面的诊断指南连接设备后重试）。"
            )
            print(f"[DisplayManager] ⚠️  {msg}", flush=True)
            return {"success": False, "error": msg, "need_device": True}

        # 启动主屏前先回收上一轮残留的孤儿 scrcpy server / 僵尸虚拟屏
        # （内部有每进程一次的开关，与卡片路径共用同一次回收）
        self._reap_orphan_scrcpy_once()

        cfg = self.load_config()
        main_cfg = cfg.get("main_display", {})

        # Build WindowConfig VO from config dict
        win_config = WindowConfig.from_dict(main_cfg, is_main=True)

        try:
            self._main_display = MainDisplay(
                config=win_config,
                scrcpy_path=self.scrcpy_path,
                device_serial=self.device_serial,
                display_manager=self,
            )

            print(f"[DisplayManager] Starting main display...")
            self._main_display.start()
            time.sleep(2)

            if self._main_display.running:
                self._main_display_running = True
                self._initialized = True
                print("[DisplayManager] Main display started successfully")
                # Start navigation monitor after main display is up
                self._start_navigation_monitor()
                return {
                    "success": True,
                    "message": "Main display started",
                    "display_type": "main",
                    "window_title": win_config.window_title,
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

    def stop_main_display(self):
        """Public API: AI / HTTP / 用户请求关闭主屏。

        Unique semantics (matches user-enforced bypass rule):
        - 若 main_display.default_load=true：**完全不做任何停止动作**
          （连 MainDisplay.stop() 都不调用，绝不 terminate 主屏 scrcpy；
          因为主屏 scrcpy = 主屏窗口 + 控制通道，一关就等于主屏窗口消失
          且 agent 的截图/操作/控制主链路全部失效）。
          此时返回 success=False + skipped=True 明确告诉调用方：
          "没执行关闭，请把请求交给 AI 原逻辑链路继续。"
        - 若 main_display.default_load=false：正常调用底层
          `_stop_main_display_locked()` 执行 terminate + state reset。

        NOTE: 本 public API 故意**不提供**任何"强制关闭"参数。
        DisplayManager 全局生命周期关机（stop_all / Ctrl+C 析构）场景若想硬清
        主屏，请**直接调用 private 的 `_stop_main_display_locked()`**，
        避免在 public API 上同时暴露"两种语义"让调用方混淆。
        """
        with self._lock:
            if self._is_default_load(None):
                print(
                    "[DisplayManager] Bypass: main_display.default_load=true → "
                    "AI/close request HAND-OFF to original logic. "
                    "NOTE: main scrcpy is NOT stopped at all (stopping it would "
                    "kill the main window + control socket, agent would lose the "
                    "primary display channel)."
                )
                return {
                    "success": False,
                    "skipped": True,
                    "default_load": True,
                    "bypassed_implant_cleanup": True,
                    "message": (
                        "Main display close skipped: default_load=true. "
                        "Handing the request to the original AI logic "
                        "(main scrcpy process + window intentionally kept alive)."
                    ),
                }
            self._stop_main_display_locked()
            return {"success": True, "message": "Main display stopped"}

    def _stop_main_display_locked(self):
        """Private atomic: actually terminate the main scrcpy + reset all state.

        ONLY call sites:
        • DisplayManager.stop_all()  (global service shutdown, atexit)
        • stop_main_display() 当 default_load=false 时（仅这一个 public 路径）
        """
        if self._main_display:
            try:
                self._main_display.stop()
            except Exception:
                pass
            self._main_display = None
            self._main_display_running = False
            time.sleep(0.5)

    def _reap_orphan_scrcpy_once(self):
        """启动任何本机 scrcpy 之前，回收设备端上一轮残留的孤儿 scrcpy。

        泄漏场景：PC 端被硬杀（taskkill /F、断电）、WiFi adb TCP 半开、
        系统 ANR → 设备端 scrcpy server 收不到客户端断连而常驻，其
        --new-display VirtualDisplay 随之变成僵尸块（实测 mDisplayId=7396，
        rootTasks=0 且 isKeyguardShowing 恒为 true，还会污染锁屏判定）。

        此刻（bootstrap 早期、本进程尚未启动任何 scrcpy）设备上若还存在
        scrcpy server，必属上一轮残留 → 全部 kill -9，VD 由 binder death
        触发 system_server 回收。每个进程只执行一次；无设备/异常均静默跳过。
        """
        if self._orphans_reaped:
            return
        self._orphans_reaped = True
        if not self.device_serial:
            return
        try:
            from .multi_virtualdisplay import _get_device, reap_orphan_scrcpy
            device = _get_device(self.device_serial)
            if device is None:
                return
            result = reap_orphan_scrcpy(device)
            if result["killed"] or result["displays_before"]:
                print(
                    f"[DisplayManager] ♻️ 启动前回收孤儿 scrcpy："
                    f"设备端 kill {len(result['killed'])} 个进程"
                    f"（{result['killed']}），残留虚拟屏 "
                    f"{result['displays_before']} → {result['displays_after']}",
                    flush=True)
                if result["displays_after"]:
                    print("[DisplayManager] ⚠️ 部分虚拟屏 kill 后仍未消失"
                          "（可能需重启手机/SystemUI 释放）", flush=True)
        except Exception as e:
            print(f"[DisplayManager] orphan scrcpy reap skipped: {e}", flush=True)

    def _get_multi_display(self):
        """Get or create MultiVirtualDisplay instance."""
        if self._multi_display is None:
            # 首个卡片启动前同样先回收孤儿（覆盖 default_load=false、
            # 主屏未启动但直接开卡片的路径）
            self._reap_orphan_scrcpy_once()
            self._multi_display = MultiVirtualDisplay(
                device_serial=self.device_serial,
                scrcpy_path=self.scrcpy_path,
                turn_screen_off=True,
                keep_active=True,
            )
        return self._multi_display

    def start_app_display(self, app_package):
        """
        Start a virtual display for an app.

        Each card defines its own window_width/window_height in config; virtual
        display resolution is selected automatically from the card's aspect ratio
        (w >= h → wide VD, w < h → tall VD). No orientation mode or w/h swapping.

        If the app is already running, returns immediately without creating a new one.

        Args:
            app_package: Android app package name.

        Returns:
            Dict with success status and display info.
        """
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {}) or {}

        with self._lock:
            if app_package in self._app_display_map:
                display = self._app_display_map[app_package]
                if display.running:
                    print(f"[DisplayManager] App {app_package} already running")
                    self.activate_app_window(app_package)
                    return {
                        "success": True,
                        "message": "App display already running",
                        "display_type": "virtual",
                        "app_package": app_package,
                        "window_title": display.window_title,
                    }
                else:
                    del self._app_display_map[app_package]

            if not app_cfg:
                # Step 1: Check display_config → app NOT in config → use main display.
                # Step 2: Switch to main (home) screen first, then launch app on main display.
                print(f"[DisplayManager] No display config for {app_package} → main display flow")

                # Idempotency guard: if we already launched this package on main
                # display within the last N seconds, skip re-launching to avoid
                # double-popup bugs caused by duplicate upstream calls.
                now = time.time()
                last_ts = self._main_launch_cache.get(app_package)
                if last_ts is not None and (now - last_ts) < self._MAIN_LAUNCH_DEDUP_SEC:
                    print(f"[DisplayManager] {app_package} already launched on main display "
                          f"{now - last_ts:.1f}s ago → skipping duplicate am start")
                    return {
                        "success": True,
                        "message": "App already launched on main display (deduplicated)",
                        "display_type": "main",
                        "app_package": app_package,
                        "android_display_id": None,
                        "deduplicated": True,
                    }

                if not self._main_display_running:
                    self._start_main_display_locked()

                device = self._get_adb_device()
                if device is None:
                    return {
                        "success": False,
                        "message": "No ADB device available",
                        "display_type": "main",
                        "app_package": app_package,
                        "android_display_id": None,
                    }

                # 2a. Switch to main (home) screen so user sees main display content
                print(f"[DisplayManager] Switching to main (home) screen on display 0...")
                try:
                    device.shell("am start -a android.intent.action.MAIN -c android.intent.category.HOME --display 0")
                    time.sleep(0.8)
                except Exception as h_err:
                    print(f"[DisplayManager] am start HOME warning (non-critical): {h_err}")
                    try:
                        device.shell("input keyevent KEYCODE_HOME")
                        time.sleep(0.5)
                    except Exception:
                        pass

                # 2b. Launch the app on main display (display 0)
                print(f"[DisplayManager] Launching {app_package} on main display...")
                launched = False
                err_msg = ""
                try:
                    # Prefer am start (no cold start if app is already running)
                    resolve = device.shell(f"cmd package resolve-activity --brief {app_package}")
                    if resolve and "/" in resolve and "No activity found" not in resolve:
                        activity = None
                        for line in resolve.strip().split('\n'):
                            if '/' in line and not line.startswith('#'):
                                activity = line.strip()
                                break
                        if activity is None:
                            activity = resolve.strip().split('\n')[-1].strip()
                        if activity and '/' in activity:
                            am_result = device.shell(f"am start -n {activity} --display 0")
                            if am_result and ("Error" not in am_result and "Exception" not in am_result):
                                launched = True
                                print(f"[DisplayManager] App started via am start on display 0: {activity}")
                            else:
                                err_msg = (am_result or "").strip()[:200]
                except Exception as a_err:
                    err_msg = f"am start error: {a_err}"

                if not launched:
                    # Fallback: monkey launcher for cold start
                    try:
                        monkey_result = device.shell(
                            f"monkey -p {app_package} --display 0 "
                            f"-c android.intent.category.LAUNCHER 1"
                        )
                        if monkey_result and ("No activities found" in monkey_result or "Error" in monkey_result):
                            err_msg = f"monkey failed: {monkey_result.strip()[:200]}"
                        else:
                            launched = True
                            print(f"[DisplayManager] App started via monkey on display 0")
                    except Exception as m_err:
                        err_msg = f"monkey error: {m_err}"

                if not launched:
                    return {
                        "success": False,
                        "message": f"Failed to launch app on main display: {err_msg}",
                        "display_type": "main",
                        "app_package": app_package,
                        "android_display_id": None,
                    }

                self._main_launch_cache[app_package] = time.time()
                return {
                    "success": True,
                    "message": "App launched on main display",
                    "display_type": "main",
                    "app_package": app_package,
                    "android_display_id": None,
                }

            win_w = app_cfg.get("window_width", 540)
            win_h = app_cfg.get("window_height", 960)

            # ===== Build WindowConfig VO and resolve virtual display size =====
            # VD resolution is selected automatically by window aspect ratio.
            win_config = WindowConfig.from_dict(app_cfg, is_main=False)
            nd_w, nd_h, nd_dpi = win_config.resolve_virtual_display_size()
            win_config.new_display_width = nd_w
            win_config.new_display_height = nd_h
            win_config.new_display_dpi = nd_dpi

            nd_desc = f"{nd_w}x{nd_h}/{nd_dpi or 'default'}dpi"
            win_desc = (f"{win_config.window_width}x{win_config.window_height}"
                        f"({'wide' if win_config.window_width >= win_config.window_height else 'tall'})")

            # Early-fail: no ADB device detected -> scrcpy will 100% fail.
            if not self.device_serial:
                msg = (
                    f"未检测到 ADB 设备，跳过创建 {app_package} 虚拟屏"
                    "（请按启动时的诊断指南连接设备后重试）。"
                )
                print(f"[DisplayManager] ⚠️  {msg}", flush=True)
                return {"success": False, "error": msg, "need_device": True,
                        "display_type": "virtual", "app_package": app_package}

            mvd = self._get_multi_display()

            display = None
            # 并发 adb reverse 竞态在互斥锁下已不应再发生；仅对设备侧瞬时
            # ADB 抖动（push 中断 / app_process 启动慢）保留一次自动重试。
            max_attempts = 2
            try:
                print(f"[DisplayManager] ========== 创建虚拟屏参数 ==========")
                print(f"[DisplayManager] app_package           : {app_package}")
                print(f"[DisplayManager] window 尺寸           : {win_desc}")
                print(f"[DisplayManager] new_display (安卓)    : {nd_desc}")
                print(f"[DisplayManager] =================================================")

                for attempt in range(1, max_attempts + 1):
                    allocated_id = mvd._alloc_display_id()
                    display = AppVirtualDisplay(
                        app_package=app_package,
                        config=win_config,
                        display_id=allocated_id,
                        scrcpy_path=self.scrcpy_path,
                        device_serial=self.device_serial,
                        base_port=mvd.base_port,
                        display_manager=self,
                    )

                    print(f"[DisplayManager] Starting virtual display for {app_package} "
                          f"(attempt {attempt}/{max_attempts}, local display_id={allocated_id})...")
                    # start() 内部会在进程级互斥锁保护下同步完成
                    # push → adb reverse → server 连接 → New display 创建，
                    # 返回 True 时 android_display_id 必已解析。
                    ok = display.start()

                    if ok and display.running:
                        # 成功后才登记，避免失败实例残留在 map/displays 中
                        mvd.displays.append(display)
                        self._app_display_map[app_package] = display

                        # 给 scrcpy 窗口一点创建时间再激活（样式线程自身会继续重试）
                        time.sleep(1)
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
                            "message": "App virtual display started",
                            "display_type": "virtual",
                            "app_package": app_package,
                            "window_title": display.window_title,
                            "display_id": display.display_id,
                            "android_display_id": getattr(display, "android_display_id", None),
                        }

                    # ── 启动硬失败：回收本次分配的 display_id/端口，准备重试 ──
                    boot_error = getattr(display, "_boot_error", None) \
                        or "App virtual display failed to start"
                    if display in mvd.displays:
                        mvd.displays.remove(display)
                    mvd._free_display_id(allocated_id)
                    display = None

                    if attempt < max_attempts:
                        print(f"[DisplayManager] Virtual display for {app_package} failed "
                              f"({boot_error}); retrying once after 2s...")
                        time.sleep(2)
                        continue

                    return {
                        "success": False,
                        "error": boot_error,
                    }

            except Exception as e:
                print(f"[DisplayManager] Failed to start app display: {e}")
                import traceback
                traceback.print_exc()
                # 异常情况下也归还id
                try:
                    if display is not None:
                        fid = display.display_id
                        if display in mvd.displays:
                            mvd.displays.remove(display)
                        mvd._free_display_id(fid)
                except Exception:
                    pass
                return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # default_load 旁路工具（AI 原生关闭链短路判断）
    # ------------------------------------------------------------------
    def _is_default_load(self, app_package=None):
        """查 display_config：
        - app_package=None  -> main_display.default_load
        - app_package=str   -> app_displays[app_package].default_load
        返回：T/F（查不到当 False）。
        """
        try:
            cfg = self.load_config()
        except Exception:
            return False
        if app_package is None:
            return bool(cfg.get("main_display", {}).get("default_load", False))
        apps = cfg.get("app_displays", {}) or {}
        return bool(apps.get(app_package, {}).get("default_load", False))

    def stop_app_display(self, app_package):
        """
        Stop a virtual display for an app.

        Args:
            app_package: Android app package name.

        Returns:
            Dict with success status.
        """
        with self._lock:
            # ==============================================================
            # 【AI 关闭链旁路 · 用户需求】
            #   如果 display_config 中该 app 的 default_load=true，
            #   说明这是 bootstrap / default_load 启动流程预置的常驻卡片，
            #   关闭逻辑全部交给 AI 原执行路径（按原始 stop 语义：
            #   display.stop() 本身原生的 unregister_decorator +
            #   terminate scrcpy 即视为完成），
            #   绝不执行"我们植入的额外清理动作"：
            #     · _multi_display.displays.remove(display)
            #     · _multi_display._free_display_id(old_display_id) 归还池子
            #     · del self._app_display_map[app_package] （留 AI 之后自己重建）
            #     · time.sleep(0.5) 强制延迟
            # ==============================================================
            if self._is_default_load(app_package):
                if app_package not in self._app_display_map:
                    return {
                        "success": False,
                        "error": f"No running display for {app_package}",
                    }
                display = self._app_display_map[app_package]
                try:
                    display.stop()  # 仅此一行原 stop：交给 AI 原生路径
                except Exception as e:
                    return {"success": False, "error": str(e)}
                print(f"[DisplayManager] Bypass: pkg={app_package} default_load=true → "
                      f"AI-native stop() executed, NO post-cleanup（保留 map/displays/pool 不动）。")
                return {
                    "success": True,
                    "message": "App display stopped (AI native path, bypassed implant cleanup)",
                    "bypassed_implant_cleanup": True,
                    "default_load": True,
                }
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
        """Turn OFF the physical screen backlight only.

        Strictly uses scrcpy-client POWER_MODE_OFF which is the ONLY method
        that guarantees "only turn backlight off, never relock the device".
        Any failure (scrcpy-client missing, connection timeout, etc.) is
        caught and converted to a non-fatal dict result so the caller
        (ScreenMonitor) can continue without crashing the process.

        This matches the original behavior in Python 3.13 exactly.
        """
        try:
            try:
                import scrcpy
            except ImportError as e_im:
                return {
                    "success": False,
                    "error": f"scrcpy-client not installed: {e_im}. Run: pip install scrcpy-client",
                    "method": "scrcpy_client",
                }

            success, message = self._set_screen_power_mode(scrcpy.POWER_MODE_OFF)
            return {
                "success": success,
                "message": message,
                "method": "scrcpy_client",
            }
        except Exception as e:
            # CRITICAL: Never let screen_off throw — it runs inside the
            # background ScreenMonitor thread and must not kill the process.
            print(f"[ScreenPower] screen_off suppressed exception (non-fatal): {type(e).__name__}: {e}")
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "method": "scrcpy_client",
            }

    def screen_on(self):
        """Turn ON the physical screen backlight.

        Uses scrcpy-client POWER_MODE_NORMAL only (matches original Py313
        implementation exactly).  Any failure is swallowed and returned as
        a dict so process stability is guaranteed.
        """
        try:
            try:
                import scrcpy
            except ImportError as e_im:
                return {
                    "success": False,
                    "error": f"scrcpy-client not installed: {e_im}. Run: pip install scrcpy-client",
                    "method": "scrcpy_client",
                }

            success, message = self._set_screen_power_mode(scrcpy.POWER_MODE_NORMAL)
            return {
                "success": success,
                "message": message,
                "method": "scrcpy_client",
            }
        except Exception as e:
            print(f"[ScreenPower] screen_on suppressed exception (non-fatal): {type(e).__name__}: {e}")
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "method": "scrcpy_client",
            }

    def _is_keyguard_locked(self, device=None, verbose=False):
        """
        Check if the keyguard (lock screen) is showing on the DEFAULT display.

        Returns:
            True if locked, False if unlocked.

        注意：旧实现用 `dumpsys window | grep -i keyguard` 后做全局子串匹配。
        但完整 dumpsys 里：
          1) DISPLAY CONTENTS 会 dump 两遍，第一遍包含 scrcpy 创建的虚拟屏，
             其 DisplayPolicy 恒报 isKeyguardShowing=true；
          2) 同一遍里主屏还会残留 mDreamingLockscreen=true 的过期值。
        手机实际已解锁时，输出里仍然存在 isKeyguardShowing=true，于是误判
        为锁定并反复"解锁"，形成死循环。
        修复：用 `dumpsys window displays`（只含系统屏），并只解析
        mDisplayId=0 块内的状态；再以 policy 中 KeyguardStateMonitor 兜底。
        """
        if device is None:
            device = self._get_adb_device()
        if device is None:
            return False

        # Method 1: 默认屏 DisplayPolicy 状态（最可靠）
        try:
            result = device.shell("dumpsys window displays")
            block = self._extract_display_block(result, 0)
            if verbose:
                print("[ScreenMonitor] default display keyguard lines:")
                key_lines = [ln for ln in block.splitlines()
                             if "keyguard" in ln.lower()]
                for ln in key_lines or block.splitlines()[:0]:
                    print(f"  {ln.strip()}")
            state = self._parse_keyguard_state(block)
            if state is not None:
                return state
        except Exception as e:
            print(f"[ScreenMonitor] dumpsys window displays failed: {e}")

        # Method 2: 全局 KeyguardStateMonitor（KeyguardService 单一实例状态，
        # 不随显示块重复，本机 dumpsys keyguard 服务不存在，故从 policy 取）
        try:
            result = device.shell("dumpsys window policy")
            match = re.search(r"KeyguardStateMonitor\s*\n\s*mIsShowing=(true|false)",
                              result)
            if match:
                return match.group(1) == "true"
        except Exception as e:
            if verbose:
                print(f"[ScreenMonitor] dumpsys window policy failed: {e}")

        return False

    @staticmethod
    def _extract_display_block(dumpsys_text: str, display_id: int) -> str:
        """从 dumpsys window displays 文本中切出指定 mDisplayId 的块
        （到下一个 'Display:' 行为止；找不到返回空串）。"""
        lines = dumpsys_text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if re.match(rf"\s*Display:\s+mDisplayId={display_id}\b", line):
                start = i
                break
        if start is None:
            return ""
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if re.match(r"\s*Display:\s+mDisplayId=", lines[j]):
                end = j
                break
        return "\n".join(lines[start:end])

    @staticmethod
    def _parse_keyguard_state(display_block: str):
        """从单个 Display 块中解析锁屏状态；找不到返回 None。"""
        for pattern in (r"\bisKeyguardShowing=(true|false)",
                        r"\bmIsShowing=(true|false)"):
            for line in display_block.splitlines():
                match = re.search(pattern, line)
                if match:
                    return match.group(1) == "true"
        return None

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
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # 诊断：移动前的窗口大小
        before_rect = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(before_rect))
        before_w = before_rect.right - before_rect.left
        before_h = before_rect.bottom - before_rect.top
        print(f"[MoveWindow] hwnd={hwnd} target=({x},{y} {w}x{h}) before=({before_rect.left},{before_rect.top} {before_w}x{before_h})")

        result = user32.MoveWindow(wintypes.HWND(hwnd), int(x), int(y), int(w), int(h), True)
        if not result:
            err = ctypes.get_last_error()
            print(f"[MoveWindow] FAILED hwnd={hwnd} error={err}")
            raise ctypes.WinError()

        # 诊断：移动后的窗口大小
        after_rect = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(after_rect))
        after_w = after_rect.right - after_rect.left
        after_h = after_rect.bottom - after_rect.top
        print(f"[MoveWindow] after=({after_rect.left},{after_rect.top} {after_w}x{after_h}) result={result}")

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
            if manager._navigating_app == app_package:
                manager._save_nav_geometry(app_package, x, y, w, h)
            else:
                manager._save_window_geometry(app_package, x, y, w, h)
        return _cb

    def _save_nav_geometry(self, app_package, x, y, w, h):
        """Persist navigation-mode window geometry to nav_geometry in config."""
        try:
            cfg = self.load_config()
            app_cfg = cfg.get("app_displays", {}).get(app_package, {})
            if "nav_geometry" not in app_cfg:
                app_cfg["nav_geometry"] = {}

            nav = app_cfg["nav_geometry"]
            nav["window_x"] = x
            nav["window_y"] = y
            nav["window_width"] = w
            nav["window_height"] = h
            opacity = nav.get("default_opacity", app_cfg.get("default_opacity", 1.0))
            nav["default_opacity"] = opacity
            self.save_config()
            title = app_cfg.get("window_title", app_package)
            print(f"[DisplayManager] Saved nav geometry for '{title}': {x},{y} {w}x{h}")
        except Exception as e:
            print(f"[DisplayManager] Failed to save nav geometry: {e}")

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
        print(f"[Navigation] _on_navigation_start called: pkg={pkg}, navigating_app={self._navigating_app}")
        if not pkg or pkg not in MAP_APPS:
            print(f"[Navigation] _on_navigation_start: pkg invalid, skipping")
            return

        # 如果已经在导航同一个app，忽略
        if self._navigating_app == pkg:
            print(f"[Navigation] _on_navigation_start: already navigating {pkg}, skipping")
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
        """Switch an app window to navigation mode.

        设计原则：直接从 config 的 nav_geometry 读取固定尺寸，不做任何
        save/restore。重复调用也安全（值不变），不会出现"恢复时尺寸被覆盖"
        的问题。
        """
        print(f"[Navigation] _switch_app_to_nav_mode ENTER: pkg={app_package}, retry={retry_count}")
        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {})
        nav_geom = app_cfg.get("nav_geometry")
        print(f"[Navigation] _switch_app_to_nav_mode: app_cfg keys={list(app_cfg.keys())}, nav_geom={nav_geom}")
        if not nav_geom:
            print(f"[Navigation] No nav_geometry config for {app_package}, skipping")
            return

        hwnd = self._get_app_hwnd(app_package)
        print(f"[Navigation] _switch_app_to_nav_mode: hwnd={hwnd}")
        if not hwnd:
            if retry_count < 10:
                def _retry():
                    time.sleep(1.0)
                    self._switch_app_to_nav_mode(app_package, retry_count + 1)
                threading.Thread(target=_retry, daemon=True).start()
                print(f"[Navigation] Window not found for {app_package}, retry {retry_count+1}/10 in 1s...")
                return
            else:
                print(f"[Navigation] Window for {app_package} not found after retries")
                return

        print(f"[Navigation] Found HWND {hwnd} for {app_package}, switching to nav mode...")

        # 直接从 config 读导航模式尺寸（固定值，重复调用无副作用）
        nx = nav_geom.get("window_x", 1027)
        ny = nav_geom.get("window_y", 543)
        nw = nav_geom.get("window_width", 2076)
        nh = nav_geom.get("window_height", 1193)
        nav_opacity = nav_geom.get("default_opacity", 1.0)
        print(f"[Navigation] _switch_app_to_nav_mode: target geometry x={nx}, y={ny}, w={nw}, h={nh}, opacity={nav_opacity}")

        try:
            self._move_and_resize_hwnd(hwnd, nx, ny, nw, nh)
            if nav_opacity < 1.0:
                self._apply_opacity_to_hwnd(hwnd, nav_opacity)
            else:
                self._apply_opacity_to_hwnd(hwnd, 1.0)
            self._force_activate_window(hwnd)
            print(f"[Navigation] Switched {app_package} to nav mode: {nx},{ny} {nw}x{nh}")
        except Exception as e:
            print(f"[Navigation] Failed to switch to nav mode: {e}")
            import traceback
            traceback.print_exc()

    def _restore_app_from_nav_mode(self, app_package):
        """Restore an app window from navigation mode to normal size.

        设计原则：直接从 config 外层读取普通模式尺寸（window_x/y/width/height），
        不依赖任何运行时保存的几何信息。重复调用也安全。
        """
        hwnd = self._get_app_hwnd(app_package)
        if not hwnd:
            print(f"[Navigation] Window not found for {app_package} during restore")
            return

        cfg = self.load_config()
        app_cfg = cfg.get("app_displays", {}).get(app_package, {})

        # 直接从 config 外层读普通模式尺寸（右上角小窗口）
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
        decorator = get_card_window_decorator()

        # 全局开关（Dock 齿轮，app_package 缺省/"main"）：一次切换【所有】已注册
        # 卡片——主屏与天气/地图等 app 卡片都注册在同一个 decorator 上。
        # 历史上这里只切主屏，导致齿轮只能控制 maindisplay（notif/thinking 由
        # server 路由额外联动 backdrop），天气和地图卡片永远不显示红绿灯。
        if app_package is None or app_package == "main":
            try:
                self._global_edit_mode = bool(editing)
                count = decorator.set_all_edit_mode(editing)
                return {
                    "success": True,
                    "scope": "all",
                    "window_count": count,
                    "editing": editing,
                    "message": f"Global edit mode {'ON' if editing else 'OFF (pinned)'} "
                               f"for {count} window(s)",
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

        # 单卡片路径：仅切换指定 app 卡片
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
        # 连续解锁失败计数：用于指数退避，避免每 2s 一轮唤醒/输密码的刷屏式死循环
        failed_unlock_streak = 0

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

                if not is_locked:
                    failed_unlock_streak = 0

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
                            result = self.screen_off()
                            if result.get("success"):
                                print(f"[ScreenMonitor] ✅ Turned off physical screen OK: method={result.get('method')}, chain={result.get('methods_used')}")
                            else:
                                print(f"[ScreenMonitor] ❌ Failed to turn off physical screen: {result.get('error') or result.get('message')}, chain={result.get('methods_used')}")
                        except Exception as e:
                            print(f"[ScreenMonitor] Failed to turn off screen after unlock (exception): {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        failed_unlock_streak += 1
                        backoff_s = min(2 * (2 ** failed_unlock_streak), 60)
                        print(f"[ScreenMonitor] Unlock may have failed - next retry "
                              f"in {backoff_s}s (consecutive failures: {failed_unlock_streak})")

            except Exception as e:
                print(f"[ScreenMonitor] Error: {e}")
                import traceback
                traceback.print_exc()

            cycle_delay = min(2 * (2 ** failed_unlock_streak), 60) if failed_unlock_streak else 2
            time.sleep(cycle_delay)

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
        """Stop all displays and screen monitor (service-level total shutdown).

        Important distinction vs AI-requested per-card close:
        • This method is only called when the whole orchestrator/service wants
          to terminate (server teardown / Ctrl+C / atexit / process exit).
          At this stage we MUST terminate every scrcpy process and clean every
          state field — we intentionally SKIP the public bypass rules because
          the bypass only applies to on-the-fly "close this card" requests
          (stopping main scrcpy there would kill the agent's primary channel).
        • Implementation uses private atomic methods so the bypass on public
          APIs cannot accidentally prevent full teardown:
          - app displays → _stop_app_display_locked(pkg) (hard atomic).
          - main_display → _stop_main_display_locked()   (hard atomic, NO
            detour via stop_main_display()  — the public API is exclusively
            for AI/user close requests and intentionally never forces anything).
        """
        print("[DisplayManager] Stopping all displays and services (service-level shutdown)...")

        self.stop_screen_monitor()
        self._stop_navigation_monitor()

        # 应用卡片：服务级硬清理（绕过 public stop_app_display 的 default_load=true 旁路）
        for pkg in list(self._app_display_map.keys()):
            try:
                self._stop_app_display_locked(pkg)
            except Exception:
                pass

        # 主屏：服务级硬清理 → 直接调 private atomic，不通过 public stop_main_display()
        # （public stop_main_display() 只服务于"AI/HTTP/用户单独关主屏请求"，
        #  default_load=true 时它会完全跳过，stop_all 绝对不能依赖它。）
        try:
            with self._lock:
                if self._main_display is not None:
                    self._stop_main_display_locked()
        except Exception:
            pass

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
