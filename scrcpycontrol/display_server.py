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
import sys
import json
import subprocess
import shutil
import threading
import time
import atexit
import re
import socket
import adbutils
import pyautogui
import ctypes
from ctypes import wintypes
import win32gui
import win32con
import win32api
from flask import Flask, request, jsonify
from multi_virtualdisplay import MainDisplay

app = Flask(__name__)

# Global state
main_display = None  # MainDisplay instance
main_display_running = False
app_displays = {}  # {app_package: display_info}
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


def _resolve_activity(device, app_package):
    """Resolve the launcher activity for an app package."""
    intent_result = device.shell(f"cmd package resolve-activity --brief {app_package}")
    if intent_result and "/" in intent_result:
        lines = intent_result.strip().split('\n')
        for line in lines:
            if '/' in line and not line.startswith('#'):
                return line.strip()
        return intent_result.strip().split('\n')[-1]
    return None


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
    
    # 设置 stayon true，让屏幕休眠后不锁屏
    try:
        devices = adbutils.adb.device_list()
        if devices:
            device = devices[0]
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
        keep_active=main_cfg.get('keep_active', True),
        turn_screen_off=True,  # Always turn off screen for main display
        stay_awake=True,  # Keep device awake to prevent lock screen
        scrcpy_path=scrcpy_path
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
    """监控锁屏状态，自动输入密码解锁"""
    global screen_monitor_stop
    
    lock_password = config.get('lock_password', '')
    if not lock_password:
        print("[ScreenMonitor] No lock password configured, skipping auto-unlock")
        return
    
    print(f"[ScreenMonitor] Starting screen lock monitor (password: {'*' * len(lock_password)})")
    
    devices = adbutils.adb.device_list()
    if not devices:
        print("[ScreenMonitor] No ADB device found")
        return
    device = devices[0]
    
    # 启动宽限期，等待 scrcpy 启动完成
    grace_cycles = 3
    
    while not screen_monitor_stop:
        try:
            # 检查 Keyguard 状态（真正的锁屏状态）
            keyguard_result = device.shell("dumpsys keyguard | grep 'mShowing'")
            is_locked = "mShowing=true" in keyguard_result
            
            # 检查电源状态
            power_result = device.shell("dumpsys power | grep 'mWakefulness'")
            is_asleep = "Asleep" in power_result
            
            print(f"[ScreenMonitor] is_locked={is_locked}, is_asleep={is_asleep}")
            
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
                print(f"[ScreenMonitor] Inputting password: {lock_password}")
                device.shell(f"input text {lock_password}")
                time.sleep(0.5)
                
                # 3. 按回车确认
                print("[ScreenMonitor] Pressing enter...")
                device.shell("input keyevent KEYCODE_ENTER")
                time.sleep(2)
                
                # 4. 验证解锁成功
                keyguard_check = device.shell("dumpsys keyguard | grep 'mShowing'")
                print(f"[ScreenMonitor] Keyguard state after unlock: {keyguard_check.strip()}")
                
                if "mShowing=false" in keyguard_check:
                    print("[ScreenMonitor] ✅ Screen unlocked successfully")
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
    
    # Stop all app displays
    with lock:
        for app_package in list(app_displays.keys()):
            display_info = app_displays[app_package]
            if display_info.get('stop_event'):
                display_info['stop_event'].set()
            if display_info.get('process'):
                try:
                    display_info['process'].terminate()
                    display_info['process'].wait(timeout=5)
                except:
                    try:
                        display_info['process'].kill()
                    except:
                        pass
    
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
    
    If app is in config: creates a new virtual display
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
        if app_package in app_displays and app_displays[app_package].get('running'):
            return jsonify({
                'success': False,
                'error': f'Display for {app_package} already running'
            }), 409
        
        display_config = app_displays_cfg[app_package]
        scrcpy_path = find_scrcpy()
        
        global next_port
        port = next_port
        next_port += 1
        
        # Build command with --new-display
        cmd = [scrcpy_path]
        
        # Create virtual display with configured resolution
        nd_w = display_config.get('new_display_width', 1080)
        nd_h = display_config.get('new_display_height', 1920)
        cmd.append(f"--new-display={nd_w}x{nd_h}")
        
        # Window size
        w = display_config.get('window_width', 540)
        h = display_config.get('window_height', 960)
        cmd.extend(["--window-width", str(w)])
        cmd.extend(["--window-height", str(h)])
        
        # Window position
        if display_config.get('window_x') is not None:
            cmd.extend(["--window-x", str(display_config['window_x'])])
        if display_config.get('window_y') is not None:
            cmd.extend(["--window-y", str(display_config['window_y'])])
        
        # Window title
        cmd.extend(["--window-title", display_config.get('window_title', app_package)])
        
        # Video settings
        cmd.extend(["--max-fps", str(display_config.get('max_fps', 60))])
        cmd.extend(["--video-bit-rate", display_config.get('bitrate', '8M')])
        
        # Virtual display decorations
        cmd.append("--no-vd-system-decorations")
        
        # Keep active
        cmd.append("--keep-active")
        
        # Turn screen off
        cmd.append("--turn-screen-off")
        
        # Port
        cmd.extend(["--port", str(port)])
        
        print(f"[{display_config.get('window_title', app_package)}] Starting: {' '.join(cmd)}")
        
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env
        )
        
        stop_event = threading.Event()
        title = display_config.get('window_title', app_package)
        
        # Create display_info dict first so read_output can update it
        display_info = {
            'process': process,
            'stop_event': stop_event,
            'config': display_config,
            'running': True,
            'port': port,
            'android_display_id': None
        }
        
        output_thread = threading.Thread(
            target=read_output,
            args=(process, title, stop_event, display_info),
            daemon=True
        )
        output_thread.start()
        display_info['output_thread'] = output_thread
        
        app_displays[app_package] = display_info
        
        # Launch app on the virtual display in a separate thread
        app_thread = threading.Thread(
            target=launch_app_on_display,
            args=(app_package, display_info, title),
            daemon=True
        )
        app_thread.start()
    
    return jsonify({
        'success': True,
        'app_package': app_package,
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
        if app_package not in app_displays or not app_displays[app_package].get('running'):
            return jsonify({
                'success': False,
                'error': f'No display running for {app_package}'
            }), 404
        
        display_info = app_displays[app_package]
        title = display_info['config'].get('window_title', app_package)
        
        print(f"[{title}] Stopping display...")
        
        display_info['stop_event'].set()
        
        try:
            display_info['process'].terminate()
            display_info['process'].wait(timeout=5)
        except subprocess.TimeoutExpired:
            display_info['process'].kill()
            display_info['process'].wait()
        except Exception as e:
            print(f"[{title}] Error stopping: {e}")
        
        display_info['running'] = False
        del app_displays[app_package]
    
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
    
    # App displays
    with lock:
        for app_package, display_info in app_displays.items():
            displays_info.append({
                'type': 'app',
                'app_package': app_package,
                'window_title': display_info['config'].get('window_title', app_package),
                'running': display_info['running'],
                'port': display_info['port']
            })
    
    return jsonify({
        'success': True,
        'displays': displays_info
    })


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    with lock:
        active_app_displays = sum(1 for d in app_displays.values() if d.get('running'))
    
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
            from scrcpy import Client
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
        
        # 创建客户端
        print(f"[ScreenPower] Creating scrcpy client...")
        if serial:
            client = Client(serial=serial)
        else:
            client = Client()
        
        # 启动客户端（建立 video 和 control 连接）
        print(f"[ScreenPower] Starting scrcpy client (threaded=True)...")
        client.start(threaded=True)
        
        # 等待一下确保连接建立
        import time
        time.sleep(1)
        
        # 使用内置方法设置屏幕电源模式为 OFF
        print(f"[ScreenPower] Setting screen power mode to OFF...")
        client.control.set_screen_power_mode(scrcpy.POWER_MODE_OFF)
        
        time.sleep(0.5)
        
        # 停止客户端
        print(f"[ScreenPower] Stopping scrcpy client...")
        client.stop()
        
        print(f"[ScreenPower] ✅ Screen power OFF command sent via scrcpy-client (set_screen_power_mode)")
        
        return jsonify({
            "success": True,
            "message": "Screen power OFF command sent via scrcpy-client (set_screen_power_mode)",
            "method": "scrcpy_client_control_sender"
        })
        
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
            from scrcpy import Client
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
        
        # 创建客户端
        print(f"[ScreenPower] Creating scrcpy client...")
        if serial:
            client = Client(serial=serial)
        else:
            client = Client()
        
        # 启动客户端（建立 video 和 control 连接）
        print(f"[ScreenPower] Starting scrcpy client (threaded=True)...")
        client.start(threaded=True)
        
        # 等待一下确保连接建立
        import time
        time.sleep(1)
        
        # 使用内置方法设置屏幕电源模式为 NORMAL
        print(f"[ScreenPower] Setting screen power mode to NORMAL...")
        client.control.set_screen_power_mode(scrcpy.POWER_MODE_NORMAL)
        
        time.sleep(0.5)
        
        # 停止客户端
        print(f"[ScreenPower] Stopping scrcpy client...")
        client.stop()
        
        print(f"[ScreenPower] ✅ Screen power ON command sent via scrcpy-client (set_screen_power_mode)")
        
        return jsonify({
            "success": True,
            "message": "Screen power ON command sent via scrcpy-client (set_screen_power_mode)",
            "method": "scrcpy_client_control_sender"
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ==================== 实验接口结束 ====================


# ==================== 键盘快捷键控制接口 ====================

# Windows API 常量
VK_MENU = 0x12  # Alt 键
VK_SHIFT = 0x10  # Shift 键
VK_O = 0x4F  # O 键
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1
SW_SHOW = 5
SW_RESTORE = 9
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# 加载 Windows API 函数
user32 = ctypes.windll.user32
FindWindowW = user32.FindWindowW
SetForegroundWindow = user32.SetForegroundWindow
GetForegroundWindow = user32.GetForegroundWindow
SendInput = user32.SendInput
ShowWindow = user32.ShowWindow
SetWindowPos = user32.SetWindowPos
PostMessageW = user32.PostMessageW

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]

def _enum_scrcpy_windows():
    """枚举所有 scrcpy 窗口（调试用）"""
    windows = []
    
    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            # 检查是否是 scrcpy 窗口（通过类名或标题）
            class_name = win32gui.GetClassName(hwnd)
            if 'scrcpy' in class_name.lower() or (title and len(title) > 0):
                windows.append({
                    'hwnd': hwnd,
                    'title': title,
                    'class_name': class_name
                })
        return True
    
    win32gui.EnumWindows(callback, None)
    return windows

def find_scrcpy_window(title=None):
    """查找 scrcpy 窗口句柄
    
    Args:
        title: 窗口标题，如果为 None 则使用主显示配置的标题
    """
    # scrcpy 窗口标题通常是配置中的 window_title
    if title is None:
        title = config.get('main_display', {}).get('window_title', '手机桌面')
    
    print(f"[ScreenPower] Looking for window with title: '{title}'")
    
    hwnd = FindWindowW(None, title)
    if hwnd:
        print(f"[ScreenPower] ✅ Found window: '{title}' (hwnd={hwnd})")
        return hwnd
    
    # 如果没找到，枚举所有窗口进行调试
    print(f"[ScreenPower] Window with title '{title}' not found, enumerating all windows...")
    all_windows = _enum_scrcpy_windows()
    for w in all_windows:
        print(f"[ScreenPower]   - hwnd={w['hwnd']}, title='{w['title']}', class='{w['class_name']}'")
    
    # 尝试其他可能的标题
    for alt_title in ['手机桌面', 'scrcpy', 'Screen Mirror']:
        if alt_title == title:
            continue
        hwnd = FindWindowW(None, alt_title)
        if hwnd:
            print(f"[ScreenPower] ✅ Found scrcpy window with title: '{alt_title}' (hwnd={hwnd})")
            return hwnd
    
    print(f"[ScreenPower] ❌ No scrcpy window found")
    
    # 调试：获取当前前台窗口
    current_fg = GetForegroundWindow()
    print(f"[ScreenPower] DEBUG: Current foreground window: {current_fg}")
    
    return None

def force_activate_window(hwnd):
    """使用多种方法强制激活窗口（绕过 Windows 前台锁定限制）
    
    即使激活失败也返回 True，让 pyautogui 继续尝试。
    """
    if not hwnd:
        print("[ScreenPower] ❌ Invalid window handle for force_activate")
        return False
    
    try:
        # 先检查窗口是否有效
        if not win32gui.IsWindow(hwnd):
            print("[ScreenPower] ❌ Window handle is not a valid window")
            return False
        
        if not win32gui.IsWindowVisible(hwnd):
            print("[ScreenPower] ⚠️ Window is not visible, trying to show it...")
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                time.sleep(0.1)
            except:
                pass
        
        # 如果窗口已经是前台窗口，直接返回成功
        try:
            current_fg = win32gui.GetForegroundWindow()
            if current_fg == hwnd:
                print(f"[ScreenPower] ✅ Window is already foreground (hwnd={hwnd})")
                return True
        except:
            pass
        
        print(f"[ScreenPower] Activating window hwnd={hwnd}...")
        
        # 方法 1: 先模拟 Alt 键（绕过前台锁定）
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
        except:
            pass
        
        # 方法 2: 恢复窗口（如果最小化）
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)
        except:
            pass
        
        # 方法 3: BringWindowToTop
        try:
            win32gui.BringWindowToTop(hwnd)
        except:
            pass
        
        # 方法 4: SetForegroundWindow
        try:
            result = win32gui.SetForegroundWindow(hwnd)
            print(f"[ScreenPower] SetForegroundWindow result: {result}")
        except Exception as e:
            print(f"[ScreenPower] ⚠️ SetForegroundWindow failed: {e}")
        
        time.sleep(0.2)
        
        # 验证是否成功
        try:
            new_foreground = win32gui.GetForegroundWindow()
            if new_foreground == hwnd:
                print(f"[ScreenPower] ✅ Window activated successfully (hwnd={hwnd})")
                return True
            else:
                try:
                    fg_title = win32gui.GetWindowText(new_foreground)
                    print(f"[ScreenPower] ⚠️ Window not activated, current fg: {new_foreground} ('{fg_title}')")
                except:
                    print(f"[ScreenPower] ⚠️ Window not activated, current fg: {new_foreground}")
        except:
            pass
        
        # 即使激活失败也返回 True，让流程继续
        print(f"[ScreenPower] ⚠️ Activation may have failed, proceeding anyway...")
        return True
        
    except Exception as e:
        print(f"[ScreenPower] ⚠️ Unexpected error in force_activate_window: {e}")
        # 即使出错也返回 True，让 pyautogui 继续尝试
        return True


def _make_keybd_input(vk, scan, flags):
    """创建一个键盘输入结构"""
    ki = KEYBDINPUT()
    ki.wVk = vk
    ki.wScan = scan
    ki.dwFlags = flags
    ki.time = 0
    ki.dwExtraInfo = ctypes.POINTER(ctypes.c_ulong)()
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = ki
    return inp

def send_hotkey_to_scrcpy(hwnd, vk_code, modifiers=None):
    """向 scrcpy 窗口发送快捷键（pyautogui 方案：成熟可靠的键盘模拟）
    
    使用 pyautogui 而不是直接调用 SendInput，因为 pyautogui 已经处理了各种
    Windows 输入问题（权限、结构对齐、错误处理等）。
    """
    if not hwnd:
        print("[ScreenPower] ❌ Invalid window handle")
        return False

    modifiers = modifiers or []

    print(f"[ScreenPower] Using pyautogui method for window {hwnd}...")
    print(f"[ScreenPower] modifiers={modifiers}, vk_code={vk_code}")

    # 第一步：强制激活 scrcpy 窗口（必须是前台窗口才能接收键盘输入）
    print("[ScreenPower] Activating scrcpy window...")
    if not force_activate_window(hwnd):
        print("[ScreenPower] ⚠️ Window activation may have failed, continuing anyway...")
    time.sleep(0.3)  # 等待窗口激活完成

    # 第二步：将虚拟键码转换为 pyautogui 的键名
    vk_to_name = {
        VK_MENU: 'alt',
        VK_SHIFT: 'shift',
        VK_O: 'o',
    }
    
    pyautogui_keys = []
    for mod in modifiers:
        name = vk_to_name.get(mod)
        if name:
            pyautogui_keys.append(name)
        else:
            print(f"[ScreenPower] ⚠️ Unknown modifier vk: {mod}")
    
    main_key_name = vk_to_name.get(vk_code)
    if not main_key_name:
        print(f"[ScreenPower] ❌ Unknown main key vk: {vk_code}")
        return False
    
    pyautogui_keys.append(main_key_name)
    
    print(f"[ScreenPower] pyautogui hotkey: {'+'.join(pyautogui_keys)}")
    
    try:
        # 使用 pyautogui 的 hotkey 函数
        pyautogui.hotkey(*pyautogui_keys)
        print(f"[ScreenPower] ✅ Hotkey sent successfully via pyautogui")
        return True
    except Exception as e:
        print(f"[ScreenPower] ❌ pyautogui hotkey failed: {e}")
        import traceback
        traceback.print_exc()
        return False

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
    try:
        # 查找 scrcpy 窗口
        hwnd = find_scrcpy_window()
        if not hwnd:
            return jsonify({
                "success": False,
                "error": "scrcpy window not found. Make sure scrcpy is running."
            }), 500
        
        print(f"[ScreenPower] Sending Alt+O to scrcpy window (hwnd={hwnd})...")
        
        # 发送 Alt+O
        success = send_hotkey_to_scrcpy(hwnd, VK_O, [VK_MENU])
        time.sleep(0.5)
        
        if success:
            print("[ScreenPower] ✅ Alt+O sent successfully")
            return jsonify({
                "success": True,
                "message": "Alt+O command sent to scrcpy window",
                "method": "windows_api",
                "hwnd": hwnd
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to send key message"
            }), 500
        
    except Exception as e:
        print(f"[ScreenPower] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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
    try:
        # 查找 scrcpy 窗口
        hwnd = find_scrcpy_window()
        if not hwnd:
            return jsonify({
                "success": False,
                "error": "scrcpy window not found. Make sure scrcpy is running."
            }), 500
        
        print(f"[ScreenPower] Sending Alt+Shift+O to scrcpy window (hwnd={hwnd})...")
        
        # 发送 Alt+Shift+O
        success = send_hotkey_to_scrcpy(hwnd, VK_O, [VK_MENU, VK_SHIFT])
        time.sleep(0.5)
        
        if success:
            print("[ScreenPower] ✅ Alt+Shift+O sent successfully")
            return jsonify({
                "success": True,
                "message": "Alt+Shift+O command sent to scrcpy window",
                "method": "windows_api",
                "hwnd": hwnd
            })
        else:
            return jsonify({
                "success": False,
                "error": "Failed to send key message"
            }), 500
        
    except Exception as e:
        print(f"[ScreenPower] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


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
