#!/usr/bin/env python3
"""
Display Server - HTTP API for managing virtual displays.

On startup, creates a permanent main display (phone screen mirror) that stays alive.
Additional app virtual displays can be created/destroyed via API.

API Endpoints:
    POST /start_display - Start a virtual display for an app
    POST /stop_display - Stop a virtual display for an app
    GET /list_displays - List all active displays
    GET /health - Health check
"""

import os
import json
import subprocess
import shutil
import threading
import time
import atexit
import re
import adbutils
from flask import Flask, request, jsonify
from .multi_virtualdisplay import MainDisplay, MultiVirtualDisplay, AppVirtualDisplay
from .window_decorator import get_window_decorator
from .screen_control_keyhot import ScreenControlKeyHot

app = Flask(__name__)

# Global state
main_display = None  # MainDisplay instance
main_display_running = False
app_displays = {}  # {app_package: display_info}  # TODO: remove after migration
multi_display = None  # MultiVirtualDisplay instance
app_display_map = {}  # {app_package: AppVirtualDisplay}
screen_keyhot = None  # ScreenControlKeyHot instance
config = None
config_path = None
lock = threading.Lock()
next_port = 8887


def load_config():
    """Load display configuration from JSON file (called once at startup)."""
    global config_path, config
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "display_config.json")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config


def save_config():
    """Save current in-memory configuration back to JSON file."""
    global config_path, config
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "display_config.json")
    
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def find_scrcpy():
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


def read_output(process, title, stop_event, display_info=None):
    """Read and print scrcpy output, and parse Android display ID for app displays."""
    try:
        while not stop_event.is_set() and process.poll() is None:
            line = process.stdout.readline()
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
                print(f"[{title}] {decoded_line}")
                
                # Parse Android virtual display ID for app displays
                # Format: "[server] INFO: New display: 540x960/182 (id=641)"
                if display_info is not None and "New display:" in decoded_line and "id=" in decoded_line:
                    match = re.search(r'id=(\d+)', decoded_line)
                    if match:
                        display_info['android_display_id'] = int(match.group(1))
                        print(f"[{title}] Parsed Android display ID: {display_info['android_display_id']}")
    except Exception as e:
        if not stop_event.is_set():
            print(f"[{title}] Output reader error: {e}")


def launch_app_on_display(app_package, display_info, title):
    """Launch the app on the virtual display after it initializes."""
    try:
        # Wait for virtual display to fully initialize and parse display ID
        print(f"[{title}] Waiting for display initialization...")
        max_wait = 20
        waited = 0
        while waited < max_wait and display_info.get('android_display_id') is None:
            time.sleep(1)
            waited += 1
        
        android_display_id = display_info.get('android_display_id')
        if android_display_id is None:
            print(f"[{title}] ERROR: Could not parse Android display ID after {max_wait}s")
            # Try to get display ID from dumpsys
            devices = adbutils.adb.device_list()
            if devices:
                device = devices[0]
                dumpsys_result = device.shell("dumpsys display")
                print(f"[{title}] Current displays:\n{dumpsys_result[:1000]}")
            return
        
        print(f"[{title}] Android display ID parsed: {android_display_id}")
        
        # Wait a bit more for display to be fully ready
        print(f"[{title}] Waiting 3 seconds for display to be ready...")
        time.sleep(3)
        
        # Get ADB device
        devices = adbutils.adb.device_list()
        if not devices:
            print(f"[{title}] ERROR: No ADB device found")
            return
        device = devices[0]
        
        # First, check if app is installed
        print(f"[{title}] Checking if app {app_package} is installed...")
        pm_result = device.shell(f"pm list packages | grep {app_package}")
        if app_package not in pm_result:
            print(f"[{title}] ERROR: App {app_package} is not installed")
            return
        print(f"[{title}] App {app_package} is installed")
        
        # Try monkey with display ID
        print(f"[{title}] Launching app {app_package} on display {android_display_id} using monkey...")
        monkey_cmd = f"monkey -p {app_package} --display {android_display_id} -c android.intent.category.LAUNCHER 1"
        print(f"[{title}] Command: {monkey_cmd}")
        result = device.shell(monkey_cmd)
        print(f"[{title}] monkey result: {result}")
        
        if "No activities found" in result or "Error" in result or not result.strip():
            print(f"[{title}] WARNING: monkey command may have failed, trying am start...")
            # Fallback to am start
            intent_result = device.shell(f"cmd package resolve-activity --brief {app_package}")
            print(f"[{title}] resolve-activity result: {intent_result}")
            
            if intent_result and "/" in intent_result:
                lines = intent_result.strip().split('\n')
                activity = None
                for line in lines:
                    if '/' in line and not line.startswith('#'):
                        activity = line.strip()
                        break
                if activity is None:
                    activity = intent_result.strip().split('\n')[-1]
                
                am_cmd = f"am start -n {activity} --display {android_display_id}"
                print(f"[{title}] Command: {am_cmd}")
                result = device.shell(am_cmd)
                print(f"[{title}] am start result: {result}")
                
                if "Error" in result or "Exception" in result:
                    print(f"[{title}] ERROR: Failed to launch app: {result}")
                else:
                    print(f"[{title}] App launched successfully via am start")
            else:
                print(f"[{title}] ERROR: Could not resolve activity for {app_package}")
        else:
            print(f"[{title}] App launched successfully via monkey")
            
    except Exception as e:
        print(f"[{title}] ERROR: Launch app failed: {e}")
        import traceback
        traceback.print_exc()


def start_main_display():
    """Start the permanent main display (phone screen mirror)."""
    global main_display, main_display_running, next_port
    
    cfg = load_config()
    main_cfg = cfg.get('main_display', {})
    
    scrcpy_path = find_scrcpy()
    port = next_port
    next_port += 1
    
    # Get device serial
    device_serial = None
    try:
        devices = adbutils.adb.device_list()
        if devices:
            device = devices[0]
            device_serial = device.serial
            device.shell("svc power stayon true")
            print("[Main] Set svc power stayon true (screen will not lock after sleep)")
    except Exception as e:
        print(f"[Main] Failed to set stayon: {e}")
    
    # Create MainDisplay instance
    main_display = MainDisplay(
        window_title=main_cfg.get('window_title', '手机桌面'),
        window_x=main_cfg.get('window_x'),
        window_y=main_cfg.get('window_y'),
        window_width=main_cfg.get('window_width'),
        window_height=main_cfg.get('window_height'),
        max_fps=main_cfg.get('max_fps', 60),
        bitrate=main_cfg.get('bitrate', '8M'),
        video_codec=main_cfg.get('video_codec', 'h264'),
        keep_active=main_cfg.get('keep_active', True),
        turn_screen_off=main_cfg.get('turn_screen_off', True),
        stay_awake=main_cfg.get('stay_awake', True),
        scrcpy_path=scrcpy_path,
        device_serial=device_serial
    )
    
    # Start the main display
    main_display.start(port=port)
    main_display_running = True
    
    # 启动锁屏监控线程
    _start_screen_monitor()
    
    print(f"[Main] Phone screen display started (PID: {main_display.process.pid}, port: {port})")


def stop_main_display():
    """Stop the permanent main display."""
    global main_display, main_display_running, screen_monitor_thread, screen_monitor_stop
    
    if not main_display_running or not main_display:
        return
    
    main_display_running = False
    
    # 停止锁屏监控线程
    if screen_monitor_thread and screen_monitor_thread.is_alive():
        screen_monitor_stop = True
        screen_monitor_thread.join(timeout=3)
    
    main_display.stop()
    main_display = None
    
    print("[Main] Phone screen display stopped")


# 锁屏监控线程相关变量
screen_monitor_thread = None
screen_monitor_stop = False

def _monitor_screen_lock():
    """监控锁屏状态，自动输入密码解锁并关闭物理屏幕"""
    global screen_monitor_stop, config
    
    def check_keyguard_locked(verbose=False):
        """检测是否锁屏，尝试多种方式"""
        # 方式1: dumpsys window (最可靠)
        try:
            result = device.shell("dumpsys window | grep -i keyguard")
            if verbose:
                print(f"[ScreenMonitor] dumpsys window keyguard:\n{result}")
            
            # 检测多种关键字
            if "isKeyguardShowing=true" in result:
                return True
            if "mIsShowing=true" in result:
                return True
            # 检查 showing=true (需要更精确的匹配，避免 showingAndNotOccluded 干扰)
            for line in result.split('\n'):
                line = line.strip()
                if line.startswith('showing=') and 'showing=true' in line:
                    return True
                if line == 'showing=true':
                    return True
            
            # 明确检测到未锁屏
            if "isKeyguardShowing=false" in result:
                return False
            if "mIsShowing=false" in result:
                return False
        except Exception as e:
            print(f"[ScreenMonitor] dumpsys window keyguard failed: {e}")
        
        # 方式2: dumpsys keyguard (某些设备可能没有这个服务)
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
        
        # 方式3: 通过 mWakefulness 和 Display Power State 推断
        # 注意：这种方式不够准确，仅作为兜底
        return False
    
    # 重新加载配置，确保获取最新的 lock_password
    try:
        config = load_config()
    except Exception as e:
        print(f"[ScreenMonitor] Failed to reload config: {e}")
    
    lock_password = config.get('lock_password', '')
    if not lock_password:
        print("[ScreenMonitor] No lock password configured, skipping auto-unlock")
        print(f"[ScreenMonitor] Config keys: {list(config.keys())}")
        return
    
    print(f"[ScreenMonitor] Starting screen lock monitor (password: {'*' * len(lock_password)})")
    
    devices = adbutils.adb.device_list()
    if not devices:
        print("[ScreenMonitor] No ADB device found")
        return
    device = devices[0]
    print(f"[ScreenMonitor] Device found: {device.serial}")
    
    # 启动宽限期，等待 scrcpy 启动完成
    grace_cycles = 3
    debug_printed = False
    last_locked_state = None
    
    while not screen_monitor_stop:
        try:
            # 第一次检测时打印详细调试信息
            verbose = not debug_printed
            is_locked = check_keyguard_locked(verbose=verbose)
            if not debug_printed:
                debug_printed = True
                print(f"[ScreenMonitor] Initial lock state: is_locked={is_locked}")
            
            # 检查电源状态
            try:
                power_result = device.shell("dumpsys power | grep 'mWakefulness'")
                is_asleep = "Asleep" in power_result
            except:
                is_asleep = False
            
            # 状态变化时打印
            if is_locked != last_locked_state:
                print(f"[ScreenMonitor] Lock state changed: {last_locked_state} -> {is_locked}, is_asleep={is_asleep}")
                last_locked_state = is_locked
            
            # 宽限期处理
            if grace_cycles > 0:
                grace_cycles -= 1
                if is_asleep and not is_locked:
                    print("[ScreenMonitor] Grace period: screen asleep but not locked (normal after startup)")
                    time.sleep(2)
                    continue
            
            # 如果检测到锁屏
            if is_locked:
                print("[ScreenMonitor] Device locked! Attempting to unlock...")
                time.sleep(0.5)
                
                # 1. 唤醒屏幕
                print("[ScreenMonitor] Waking up screen...")
                device.shell("input keyevent KEYCODE_WAKEUP")
                time.sleep(2)
                
                # 2. 输入密码
                print(f"[ScreenMonitor] Inputting password: {'*' * len(lock_password)}")
                device.shell(f"input text {lock_password}")
                time.sleep(0.5)
                
                # 3. 按回车确认
                print("[ScreenMonitor] Pressing enter...")
                device.shell("input keyevent KEYCODE_ENTER")
                time.sleep(2)
                
                # 4. 验证解锁成功
                keyguard_check = check_keyguard_locked(verbose=True)
                print(f"[ScreenMonitor] Unlock result: is_locked={keyguard_check}")
                
                if not keyguard_check:
                    print("[ScreenMonitor] ✅ Screen unlocked successfully")
                    # 5. 解锁成功后，关闭物理屏幕（只关背光，不锁屏）
                    print("[ScreenMonitor] Turning off physical screen after unlock...")
                    try:
                        import scrcpy
                        _set_screen_power_mode_via_scrcpy_client(scrcpy.POWER_MODE_OFF)
                    except ImportError:
                        print("[ScreenMonitor] scrcpy-client not available, skipping screen off")
                else:
                    print("[ScreenMonitor] ❌ Unlock may have failed")
            
        except Exception as e:
            print(f"[ScreenMonitor] Error: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(2)  # 每2秒检查一次
    
    print("[ScreenMonitor] Screen monitor stopped")


def _start_screen_monitor():
    """启动锁屏监控线程"""
    global screen_monitor_thread, screen_monitor_stop
    
    if screen_monitor_thread and screen_monitor_thread.is_alive():
        print("[ScreenMonitor] Already running")
        return
    
    screen_monitor_stop = False
    screen_monitor_thread = threading.Thread(target=_monitor_screen_lock, daemon=True)
    screen_monitor_thread.start()


def cleanup():
    """Cleanup all displays on exit."""
    print("\nCleaning up all displays...")
    
    # Stop all app virtual displays via MultiVirtualDisplay
    global multi_display
    if multi_display:
        try:
            multi_display.stop_all()
        except Exception as e:
            print(f"Error stopping multi_display: {e}")
    
    # Also clean up the display map
    with lock:
        app_display_map.clear()
    
    # Stop main display
    stop_main_display()
    
    print("All displays cleaned up")


atexit.register(cleanup)


@app.route('/start_display', methods=['POST'])
def start_display():
    """
    Start a virtual display for an app, or launch app on main display if not configured.
    
    Request body:
        {
            "app_package": "com.eg.android.AlipayGphone"
        }
    
    If app is in config: creates a new virtual display using MultiVirtualDisplay
    If app is not in config: launches on main display (phone screen)
    """
    data = request.get_json()
    if not data or 'app_package' not in data:
        return jsonify({
            'success': False,
            'error': 'app_package is required'
        }), 400
    
    app_package = data['app_package']
    
    # Use in-memory config
    app_displays_cfg = config.get('app_displays', {})
    
    # If app is not in config, launch on main display
    if app_package not in app_displays_cfg:
        try:
            # Get ADB device
            devices = adbutils.adb.device_list()
            if not devices:
                return jsonify({
                    'success': False,
                    'error': 'No ADB device found'
                }), 500
            device = devices[0]
            
            # Launch app on main display using monkey
            print(f"[Main] Launching app {app_package} on main display...")
            monkey_cmd = f"monkey -p {app_package} -c android.intent.category.LAUNCHER 1"
            result = device.shell(monkey_cmd)
            
            if "No activities found" in result or "Error" in result:
                return jsonify({
                    'success': False,
                    'error': f'Failed to launch app: {result}'
                }), 500
            else:
                return jsonify({
                    'success': True,
                    'app_package': app_package,
                    'display_type': 'main',
                    'message': f'App {app_package} launched on main display'
                })
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Failed to launch app: {str(e)}'
            }), 500
    
    with lock:
        # Check if display already exists
        if app_package in app_display_map and app_display_map[app_package].running:
            return jsonify({
                'success': False,
                'error': f'Display for {app_package} already running'
            }), 409
        
        display_config = app_displays_cfg[app_package]
        title = display_config.get('window_title', app_package)
        
        # Create AppVirtualDisplay instance through MultiVirtualDisplay
        display_id = len(multi_display.displays)
        win_w = display_config.get('window_width', 540)
        win_h = display_config.get('window_height', 960)
        is_card = display_config.get('card_style', False)

        # Determine orientation settings
        default_orientation = display_config.get('default_orientation', 'portrait')
        is_landscape = default_orientation == 'landscape'

        # For card-style windows, pick virtual display resolution that matches
        # the window aspect ratio to avoid letterbox black bars.
        if is_card:
            win_ratio = win_w / win_h if win_h else 1.0
            if win_ratio >= 1.2:
                # Landscape window → use landscape VD config
                ndw = display_config.get('landscape_new_display_width', win_w * 2)
                ndh = display_config.get('landscape_new_display_height', win_h * 2)
                ndpi = display_config.get('landscape_new_display_dpi', None)
                is_landscape = True
            elif win_ratio <= 0.8:
                # Portrait window → use portrait VD config
                ndw = display_config.get('new_display_width', 1260)
                ndh = display_config.get('new_display_height', 2844)
                ndpi = display_config.get('new_display_dpi', None)
                is_landscape = False
            else:
                # Square / near-square window → use landscape config if it's square,
                # otherwise compute a square virtual display from window size.
                ls_w = display_config.get('landscape_new_display_width')
                ls_h = display_config.get('landscape_new_display_height')
                if ls_w and ls_h and abs(ls_w / ls_h - 1.0) < 0.15:
                    ndw, ndh = ls_w, ls_h
                    ndpi = display_config.get('landscape_new_display_dpi', None)
                else:
                    # Compute square VD: use the larger dimension * 2 as side
                    side = max(win_w, win_h) * 2
                    ndw, ndh = side, side
                    ndpi = display_config.get('new_display_dpi', 400)
                is_landscape = (ndw > ndh)
        else:
            ndw = display_config.get('landscape_new_display_width' if is_landscape else 'new_display_width', 1260)
            ndh = display_config.get('landscape_new_display_height' if is_landscape else 'new_display_height', 2844)
            ndpi = display_config.get('landscape_new_display_dpi' if is_landscape else 'new_display_dpi', None)

        # For landscape: set user_rotation=1 (system rotation lock to landscape)
        # scrcpy --display-orientation=0 (no encoding rotation; rotation done at Android level)
        user_rot = 1 if is_landscape else 0

        app_display = AppVirtualDisplay(
            app_package=app_package,
            display_id=display_id,
            window_title=title,
            window_x=display_config.get('window_x'),
            window_y=display_config.get('window_y'),
            window_width=win_w,
            window_height=win_h,
            new_display_width=ndw,
            new_display_height=ndh,
            new_display_dpi=ndpi,
            max_fps=display_config.get('max_fps', 60),
            bitrate=display_config.get('bitrate', '8M'),
            video_codec=display_config.get('video_codec', 'h264'),
            no_vd_system_decorations=True,
            flex_display=display_config.get('flex_display', False),
            borderless=display_config.get('borderless', False),
            background_color=display_config.get('background_color'),
            always_on_top=display_config.get('always_on_top', False),
            corner_radius=display_config.get('corner_radius'),
            card_style=display_config.get('card_style', False),
            keep_active=display_config.get('keep_active', True),
            turn_screen_off=False,
            stay_awake=display_config.get('stay_awake', True),
            rotation=0,  # app displays: no encoding rotation; use user_rotation
            user_rotation=user_rot,
            scrcpy_path=multi_display.scrcpy_path,
            device_serial=multi_display.device_serial,
            base_port=multi_display.base_port
        )
        
        # Add to MultiVirtualDisplay manager
        multi_display.displays.append(app_display)
        
        # Add to our app -> display mapping
        app_display_map[app_package] = app_display
        
        # Start the display
        print(f"[{title}] Starting virtual display via MultiVirtualDisplay...")
        app_display.start()
    
    return jsonify({
        'success': True,
        'app_package': app_package,
        'display_type': 'virtual',
        'display_id': display_id,
        'message': f'Display for {app_package} started successfully'
    })


@app.route('/stop_display', methods=['POST'])
def stop_display():
    """
    Stop a virtual display for an app.
    
    Request body:
        {
            "app_package": "com.eg.android.AlipayGphone"
        }
    """
    data = request.get_json()
    if not data or 'app_package' not in data:
        return jsonify({
            'success': False,
            'error': 'app_package is required'
        }), 400
    
    app_package = data['app_package']
    
    with lock:
        if app_package not in app_display_map or not app_display_map[app_package].running:
            return jsonify({
                'success': False,
                'error': f'No display running for {app_package}'
            }), 404
        
        app_display = app_display_map[app_package]
        title = app_display.window_title
        
        print(f"[{title}] Stopping display via MultiVirtualDisplay...")
        
        # Stop the display
        app_display.stop()
        
        # Remove from our mapping
        del app_display_map[app_package]
        
        # Also remove from multi_display.displays
        if app_display in multi_display.displays:
            multi_display.displays.remove(app_display)
        
        # Ensure physical screen stays off if main display is still running
        if main_display_running:
            print(f"[{title}] Ensuring physical screen stays off...")
            try:
                import scrcpy
                _set_screen_power_mode_via_scrcpy_client(scrcpy.POWER_MODE_OFF)
            except ImportError:
                pass
            except Exception as e:
                print(f"[{title}] Failed to turn off physical screen: {e}")
    
    return jsonify({
        'success': True,
        'app_package': app_package,
        'message': f'Display for {app_package} stopped successfully'
    })


@app.route('/list_displays', methods=['GET'])
def list_displays():
    """List all active displays."""
    displays_info = []
    
    # Main display
    displays_info.append({
        'type': 'main',
        'app_package': None,
        'window_title': config.get('main_display', {}).get('window_title', '手机桌面') if config else '手机桌面',
        'running': main_display_running
    })
    
    # App virtual displays (from MultiVirtualDisplay)
    with lock:
        for app_package, app_display in app_display_map.items():
            displays_info.append({
                'type': 'virtual',
                'app_package': app_package,
                'window_title': app_display.window_title,
                'running': app_display.running,
                'display_id': app_display.display_id,
                'android_display_id': app_display.android_display_id
            })
    
    return jsonify({
        'success': True,
        'displays': displays_info
    })


@app.route('/window/<app_package>/edit_mode', methods=['POST'])
def toggle_edit_mode(app_package):
    """Toggle edit mode (show/hide traffic lights and drag/resize zones) for a display.

    Request body (optional):
        {
            "editing": true/false  // if omitted, toggle current state
        }
    """
    data = request.get_json(silent=True) or {}
    editing = data.get('editing', None)

    with lock:
        app_display = app_display_map.get(app_package)
        if not app_display or not app_display.running:
            return jsonify({'success': False, 'error': f'Display for {app_package} not running'}), 404

        hwnd = getattr(app_display, '_hwnd', None)
        if not hwnd:
            return jsonify({'success': False, 'error': 'Window HWND not available (style may not have been applied)'}), 400

        decorator = get_window_decorator()

        if editing is None:
            # Toggle
            card = decorator._cards.get(hwnd)
            if card:
                decorator.set_edit_mode(hwnd, not card.editing)
                new_state = not card.editing
            else:
                return jsonify({'success': False, 'error': 'Window not registered with decorator'}), 400
        else:
            decorator.set_edit_mode(hwnd, bool(editing))
            new_state = bool(editing)

    return jsonify({
        'success': True,
        'app_package': app_package,
        'editing': new_state
    })


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    with lock:
        active_app_displays = sum(1 for d in app_display_map.values() if d.running)
    
    return jsonify({
        'status': 'healthy',
        'main_display_alive': main_display_running,
        'active_app_displays': active_app_displays
    })


@app.route('/config', methods=['GET'])
def get_config():
    """Get the current display configuration."""
    return jsonify({
        'success': True,
        'config': config
    })


@app.route('/config', methods=['POST'])
def update_config():
    """
    Add or update an app display configuration.
    
    Request body:
        {
            "app_package": "com.eg.android.AlipayGphone",
            "window_title": "支付宝",
            "window_x": 1200,
            "window_y": 100,
            "window_width": 540,
            "window_height": 960,
            "max_fps": 60,
            "bitrate": "8M"
        }
    """
    data = request.get_json()
    if not data or 'app_package' not in data:
        return jsonify({
            'success': False,
            'error': 'app_package is required'
        }), 400
    
    app_package = data['app_package']
    
    # Check max displays limit
    if app_package not in config['app_displays'] and len(config['app_displays']) >= 4:
        return jsonify({
            'success': False,
            'error': 'Maximum 4 app displays allowed'
        }), 400
    
    # Build display config
    display_config = {
        'window_title': data.get('window_title', app_package),
        'window_x': data.get('window_x', 100),
        'window_y': data.get('window_y', 100),
        'window_width': data.get('window_width', 540),
        'window_height': data.get('window_height', 960),
        'max_fps': data.get('max_fps', 60),
        'bitrate': data.get('bitrate', '8M')
    }
    
    with lock:
        config['app_displays'][app_package] = display_config
        save_config()
    
    return jsonify({
        'success': True,
        'app_package': app_package,
        'config': display_config,
        'message': f'Config for {app_package} saved'
    })


@app.route('/config/<app_package>', methods=['DELETE'])
def delete_config(app_package):
    """
    Delete an app display configuration.
    
    URL parameter:
        app_package: Package name to delete
    """
    with lock:
        if app_package not in config['app_displays']:
            return jsonify({
                'success': False,
                'error': f'App {app_package} not found in configuration'
            }), 404
        
        # Check if display is running
        if app_package in app_displays and app_displays[app_package].get('running'):
            return jsonify({
                'success': False,
                'error': f'Display for {app_package} is running, stop it first'
            }), 409
        
        del config['app_displays'][app_package]
    
    return jsonify({
        'success': True,
        'app_package': app_package,
        'message': f'Config for {app_package} deleted'
    })


# ==================== 实验接口：屏幕电源控制 ====================

def _setup_scrcpy_control_socket(control_port=27183):
    """自动检测 scrcpy socket 并设置端口转发"""
    try:
        # 检测 scrcpy 的 socket 名称
        result = subprocess.check_output(
            ['adb', 'shell', 'cat', '/proc/net/unix'],
            text=True
        )
        
        # 查找 scrcpy 开头的 socket
        scrcpy_socket = None
        for line in result.split('\n'):
            if '@scrcpy' in line:
                # 提取 socket 名称，如 @scrcpy_3fb4a4fb
                parts = line.split('@scrcpy')
                if len(parts) > 1:
                    socket_name = 'scrcpy' + parts[1].split()[0]
                    scrcpy_socket = socket_name
                    break
        
        if not scrcpy_socket:
            raise Exception("未找到 scrcpy socket，请确保 scrcpy 正在运行")
        
        print(f"[ScreenPower] 检测到 scrcpy socket: {scrcpy_socket}")
        
        # 设置端口转发
        subprocess.check_call(['adb', 'forward', f'tcp:{control_port}', f'localabstract:{scrcpy_socket}'])
        print(f"[ScreenPower] 已设置端口转发: tcp:{control_port} -> localabstract:{scrcpy_socket}")
        
        return True
    except Exception as e:
        print(f"[ScreenPower] 设置端口转发失败: {e}")
        return False

@app.route('/screen_power_off', methods=['POST'])
def screen_power_off():
    """
    关闭物理屏幕（通过 ADB 发送 KEYCODE_SLEEP）
    
    原理：adb shell input keyevent 223 (KEYCODE_SLEEP)
    尝试只关闭屏幕而不触发锁屏
    
    请求体（可选）:
        {
            "serial": "device_serial"  # 设备序列号，默认自动选择
        }
    """
    try:
        try:
            data = request.get_json(force=True) or {}
        except:
            data = {}
        serial = data.get('serial')
        
        # 构建 adb 命令
        cmd = ["adb"]
        if serial:
            cmd.extend(["-s", serial])
        cmd.extend(["shell", "input", "keyevent", "223"])  # KEYCODE_SLEEP
        
        print(f"[ScreenPower] Sending KEYCODE_SLEEP (223) via adb: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            print(f"[ScreenPower] ✅ Screen power OFF command sent via adb (KEYCODE_SLEEP)")
            return jsonify({
                "success": True,
                "message": "Screen power OFF command sent via adb (KEYCODE_SLEEP)",
                "method": "adb_shell"
            })
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            print(f"[ScreenPower] ❌ ADB command failed: {error_msg}")
            return jsonify({
                "success": False,
                "error": error_msg
            }), 500
            
    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "ADB command timeout"
        }), 500
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/screen_power_on', methods=['POST'])
def screen_power_on():
    """
    打开物理屏幕（通过 ADB 发送 KEYCODE_WAKEUP）
    
    原理：adb shell input keyevent 224 (KEYCODE_WAKEUP)
    唤醒屏幕
    
    请求体（可选）:
        {
            "serial": "device_serial"  # 设备序列号，默认自动选择
        }
    """
    try:
        try:
            data = request.get_json(force=True) or {}
        except:
            data = {}
        serial = data.get('serial')
        
        # 构建 adb 命令
        cmd = ["adb"]
        if serial:
            cmd.extend(["-s", serial])
        cmd.extend(["shell", "input", "keyevent", "224"])  # KEYCODE_WAKEUP
        
        print(f"[ScreenPower] Sending KEYCODE_WAKEUP (224) via adb: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            print(f"[ScreenPower] ✅ Screen power ON command sent via adb (KEYCODE_WAKEUP)")
            return jsonify({
                "success": True,
                "message": "Screen power ON command sent via adb (KEYCODE_WAKEUP)",
                "method": "adb_shell"
            })
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            print(f"[ScreenPower] ❌ ADB command failed: {error_msg}")
            return jsonify({
                "success": False,
                "error": error_msg
            }), 500
            
    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "ADB command timeout"
        }), 500
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


def _set_screen_power_mode_via_scrcpy_client(mode, serial=None):
    """
    通过 scrcpy-client 设置屏幕电源模式（内部公共函数）
    
    Args:
        mode: 电源模式 (scrcpy.POWER_MODE_OFF 或 scrcpy.POWER_MODE_NORMAL)
        serial: 设备序列号，默认自动选择
    
    Returns:
        (success: bool, message: str)
    """
    try:
        try:
            import scrcpy
            from scrcpy import Client
        except ImportError:
            return False, "scrcpy-client not installed"
        
        mode_name = "OFF" if mode == scrcpy.POWER_MODE_OFF else "NORMAL"
        print(f"[ScreenPower] Setting screen power mode to {mode_name} via scrcpy-client...")
        
        # 创建客户端
        if serial:
            client = Client(serial=serial)
        else:
            client = Client()
        
        # 启动客户端（建立 video 和 control 连接）
        client.start(threaded=True)
        
        # 等待一下确保连接建立
        import time
        time.sleep(1)
        
        # 使用内置方法设置屏幕电源模式
        client.control.set_screen_power_mode(mode)
        
        time.sleep(0.5)
        
        # 停止客户端
        client.stop()
        
        print(f"[ScreenPower] ✅ Screen power mode set to {mode_name} via scrcpy-client")
        return True, f"Screen power mode set to {mode_name} via scrcpy-client"
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


@app.route('/screen_power_off_client', methods=['POST'])
def screen_power_off_client():
    """
    关闭物理屏幕（通过 scrcpy-client 库的内置接口）
    
    原理：使用 scrcpy-client 的 ControlSender.set_screen_power_mode()
    只关闭物理屏幕背光，不触发锁屏
    
    请求体（可选）:
        {
            "serial": "device_serial"  # 设备序列号，默认自动选择
        }
    """
    try:
        # 尝试导入 scrcpy 客户端库
        try:
            import scrcpy
        except ImportError:
            return jsonify({
                "success": False,
                "error": "scrcpy-client not installed. Run: pip install scrcpy-client"
            }), 500
        
        try:
            data = request.get_json(force=True) or {}
        except:
            data = {}
        serial = data.get('serial')
        
        success, message = _set_screen_power_mode_via_scrcpy_client(scrcpy.POWER_MODE_OFF, serial)
        
        if success:
            return jsonify({
                "success": True,
                "message": message,
                "method": "scrcpy_client_control_sender"
            })
        else:
            return jsonify({
                "success": False,
                "error": message
            }), 500
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/screen_power_on_client', methods=['POST'])
def screen_power_on_client():
    """
    打开物理屏幕（通过 scrcpy-client 库的内置接口）
    
    原理：使用 scrcpy-client 的 ControlSender.set_screen_power_mode()
    点亮物理屏幕
    
    请求体（可选）:
        {
            "serial": "device_serial"  # 设备序列号，默认自动选择
        }
    """
    try:
        # 尝试导入 scrcpy 客户端库
        try:
            import scrcpy
        except ImportError:
            return jsonify({
                "success": False,
                "error": "scrcpy-client not installed. Run: pip install scrcpy-client"
            }), 500
        
        try:
            data = request.get_json(force=True) or {}
        except:
            data = {}
        serial = data.get('serial')
        
        success, message = _set_screen_power_mode_via_scrcpy_client(scrcpy.POWER_MODE_NORMAL, serial)
        
        if success:
            return jsonify({
                "success": True,
                "message": message,
                "method": "scrcpy_client_control_sender"
            })
        else:
            return jsonify({
                "success": False,
                "error": message
            }), 500
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ==================== 实验接口结束 ====================


# ==================== 键盘快捷键控制接口 ====================

@app.route('/screen_off_hotkey', methods=['POST'])
def screen_off_hotkey():
    """
    通过向 scrcpy 窗口发送 Alt+O 命令关闭物理屏幕

    原理：
    1. 查找 scrcpy 窗口句柄
    2. 激活窗口并模拟键盘按键
    3. scrcpy 收到按键后通过 control socket 发送 SET_SCREEN_POWER_MODE(OFF)
    4. Android 设备执行 SurfaceControl.setDisplayPowerMode() 关闭屏幕
    """
    global screen_keyhot, config
    if screen_keyhot is None:
        screen_keyhot = ScreenControlKeyHot(config)
    result = screen_keyhot.screen_off()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/screen_on_hotkey', methods=['POST'])
def screen_on_hotkey():
    """
    通过向 scrcpy 窗口发送 Alt+Shift+O 命令点亮物理屏幕

    原理：
    1. 查找 scrcpy 窗口句柄
    2. 激活窗口并模拟键盘按键
    3. scrcpy 收到按键后通过 control socket 发送 SET_SCREEN_POWER_MODE(NORMAL)
    4. Android 设备执行 SurfaceControl.setDisplayPowerMode() 打开屏幕
    """
    global screen_keyhot, config
    if screen_keyhot is None:
        screen_keyhot = ScreenControlKeyHot(config)
    result = screen_keyhot.screen_on()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


# ==================== 键盘快捷键控制接口结束 ====================


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Display Server - HTTP API for virtual displays')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--config', type=str, help='Path to display_config.json')
    
    args = parser.parse_args()
    
    # Load configuration from file
    global config_path
    if args.config:
        config_path = args.config
    
    print("Loading display configuration...")
    load_config()
    print(f"Loaded {len(config.get('app_displays', {}))} app display configurations")
    
    # Start permanent main display
    print("Starting permanent main display (phone screen)...")
    start_main_display()
    time.sleep(2)  # Wait for main display to initialize
    
    # Initialize MultiVirtualDisplay manager
    global multi_display
    scrcpy_path = find_scrcpy()
    
    # Get device serial
    device_serial = None
    try:
        devices = adbutils.adb.device_list()
        if devices:
            device_serial = devices[0].serial
    except Exception as e:
        print(f"Warning: Failed to get device serial: {e}")
    
    multi_display = MultiVirtualDisplay(
        scrcpy_path=scrcpy_path,
        device_serial=device_serial,
        turn_screen_off=False,
        keep_active=True,
        new_display_width=1260,
        new_display_height=2844,
        base_port=next_port
    )
    print("MultiVirtualDisplay manager initialized")
    
    print(f"\n🚀 Starting Display Server on {args.host}:{args.port}")
    print(f"📡 API endpoints:")
    print(f"   POST http://{args.host}:{args.port}/start_display")
    print(f"   POST http://{args.host}:{args.port}/stop_display")
    print(f"   GET  http://{args.host}:{args.port}/list_displays")
    print(f"   GET  http://{args.host}:{args.port}/config")
    print(f"   GET  http://{args.host}:{args.port}/health")
    print(f"\n🔬 Experimental endpoints (screen power control):")
    print(f"   POST http://{args.host}:{args.port}/screen_power_off (control socket)")
    print(f"   POST http://{args.host}:{args.port}/screen_power_on (control socket)")
    print(f"   POST http://{args.host}:{args.port}/screen_power_off_client (scrcpy-client)")
    print(f"   POST http://{args.host}:{args.port}/screen_power_on_client (scrcpy-client)")
    print("\nPress Ctrl+C to stop")
    
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
