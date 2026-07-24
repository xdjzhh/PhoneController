"""
基础功能验证脚本 - Display Server Base Test

用于大模型修改代码后快速验证基本功能是否正常。

使用方法：
    python test_base.py
    python test_base.py --no-api        # 跳过 API 服务测试
    python test_base.py --api-only      # 只测 API 服务（假设服务已启动）

测试项：
    1. 模块导入测试
    2. 配置文件加载测试
    3. 类实例化测试 (MainDisplay, MultiVirtualDisplay, AppVirtualDisplay, ScreenControlKeyHot)
    4. Flask API 服务启动测试
    5. API 端点健康检查
    6. 虚拟显示启动/停止测试
    7. 屏幕电源控制测试 (hotkey 方式)
"""

import sys
import os
import time
import json
import subprocess
import argparse
import threading

import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "display_config.json")
SERVER_URL = "http://127.0.0.1:5000"

passed = 0
failed = 0
total = 0


def test(name, condition, detail=""):
    global passed, failed, total
    total += 1
    status = "✅ PASS" if condition else "❌ FAIL"
    if condition:
        passed += 1
    else:
        failed += 1
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return condition


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# Test 1: Module imports
# ============================================================
def test_imports():
    section("1. 模块导入测试")

    try:
        from multi_virtualdisplay import MainDisplay
        test("MainDisplay 导入", True)
    except Exception as e:
        test("MainDisplay 导入", False, str(e))
        return False

    try:
        from multi_virtualdisplay import MultiVirtualDisplay
        test("MultiVirtualDisplay 导入", True)
    except Exception as e:
        test("MultiVirtualDisplay 导入", False, str(e))
        return False

    try:
        from multi_virtualdisplay import AppVirtualDisplay
        test("AppVirtualDisplay 导入", True)
    except Exception as e:
        test("AppVirtualDisplay 导入", False, str(e))
        return False

    try:
        from screen_control_keyhot import ScreenControlKeyHot
        test("ScreenControlKeyHot 导入", True)
    except Exception as e:
        test("ScreenControlKeyHot 导入", False, str(e))
        return False

    try:
        import display_server
        test("display_server 模块导入", True)
    except Exception as e:
        test("display_server 模块导入", False, str(e))
        return False

    return True


# ============================================================
# Test 2: Config loading
# ============================================================
def test_config():
    section("2. 配置文件测试")

    test("配置文件存在", os.path.exists(CONFIG_PATH), CONFIG_PATH)

    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        test("配置文件 JSON 解析", True)
    except Exception as e:
        test("配置文件 JSON 解析", False, str(e))
        return False

    test("包含 lock_password", "lock_password" in cfg)
    test("包含 main_display", "main_display" in cfg)
    test("包含 app_displays", "app_displays" in cfg)

    if "main_display" in cfg:
        main = cfg["main_display"]
        test("main_display.window_title", "window_title" in main)
        test("main_display.window_width", "window_width" in main)
        test("main_display.window_height", "window_height" in main)
        test("main_display.max_fps", "max_fps" in main)
        test("main_display.bitrate", "bitrate" in main)

    if "app_displays" in cfg:
        apps = cfg["app_displays"]
        test("app_displays 非空", len(apps) > 0, f"共 {len(apps)} 个应用")
        for pkg, app_cfg in apps.items():
            test(f"  {pkg} 含 new_display_width", "new_display_width" in app_cfg)
            test(f"  {pkg} 含 new_display_height", "new_display_height" in app_cfg)
            break

    return True


# ============================================================
# Test 3: Class instantiation
# ============================================================
def test_classes():
    section("3. 类实例化测试")

    from multi_virtualdisplay import MainDisplay, MultiVirtualDisplay, AppVirtualDisplay
    from screen_control_keyhot import ScreenControlKeyHot

    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    try:
        md = MainDisplay(
            window_title="test",
            window_width=540,
            window_height=960,
            max_fps=60,
            bitrate='8M'
        )
        test("MainDisplay 实例化", True)
        test("MainDisplay.window_title", md.window_title == "test")
        test("MainDisplay.window_width", md.window_width == 540)
        test("MainDisplay.window_height", md.window_height == 960)
    except Exception as e:
        test("MainDisplay 实例化", False, str(e))

    try:
        mvd = MultiVirtualDisplay(
            new_display_width=1260,
            new_display_height=2844,
            base_port=9000
        )
        test("MultiVirtualDisplay 实例化", True)
        test("MultiVirtualDisplay.new_display_width", mvd.new_display_width == 1260)
        test("MultiVirtualDisplay.new_display_height", mvd.new_display_height == 2844)
        test("MultiVirtualDisplay.base_port", mvd.base_port == 9000)
    except Exception as e:
        test("MultiVirtualDisplay 实例化", False, str(e))

    try:
        avd = AppVirtualDisplay(
            app_package="com.test.app",
            display_id=0,
            window_title="Test App",
            window_width=540,
            window_height=960,
            new_display_width=1260,
            new_display_height=2844,
            base_port=9000
        )
        test("AppVirtualDisplay 实例化", True)
        test("AppVirtualDisplay.app_package", avd.app_package == "com.test.app")
        test("AppVirtualDisplay.new_display_width", avd.new_display_width == 1260)
        test("AppVirtualDisplay.new_display_height", avd.new_display_height == 2844)
    except Exception as e:
        test("AppVirtualDisplay 实例化", False, str(e))

    try:
        sch = ScreenControlKeyHot(cfg)
        test("ScreenControlKeyHot 实例化", True)
        test("ScreenControlKeyHot.config 已设置", sch.config is not None)
        test("ScreenControlKeyHot._get_main_window_title",
             sch._get_main_window_title() == cfg['main_display']['window_title'])
    except Exception as e:
        test("ScreenControlKeyHot 实例化", False, str(e))

    return True


# ============================================================
# Test 4: API Server
# ============================================================
def api_get(path):
    url = f"{SERVER_URL}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return None, str(e)


def api_post(path, data=None):
    url = f"{SERVER_URL}{path}"
    try:
        body = json.dumps(data or {}).encode('utf-8')
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return None, str(e)


def wait_for_server(timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            code, _ = api_get('/health')
            if code == 200:
                return True
        except:
            pass
        time.sleep(1)
    return False


def test_api_server():
    section("4. API 服务测试")

    server_proc = None

    try:
        print("  启动 display_server.py...")
        server_proc = subprocess.Popen(
            [sys.executable, "display_server.py"],
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        if not wait_for_server(30):
            test("服务启动", False, "30秒内未就绪")
            return False
        test("服务启动", True)

        # --- Health check ---
        code, data = api_get('/health')
        test("GET /health", code == 200, f"status={code}")
        if code == 200:
            test("  status=healthy", data.get('status') == 'healthy')

        # --- Config endpoint ---
        code, data = api_get('/config')
        test("GET /config", code == 200, f"status={code}")

        # --- List displays ---
        code, data = api_get('/list_displays')
        test("GET /list_displays", code == 200, f"status={code}")

        # --- Start virtual display ---
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        apps = list(cfg.get('app_displays', {}).keys())
        if apps:
            test_app = apps[0]
            code, data = api_post('/start_display', {'app_package': test_app})
            test(f"POST /start_display ({test_app})", code == 200, f"status={code}")

            if code == 200 and data.get('success'):
                time.sleep(3)
                code2, data2 = api_get('/list_displays')
                test("  list_displays 包含新启动的显示", test_app in str(data2))

                # --- Stop virtual display ---
                code3, data3 = api_post('/stop_display', {'app_package': test_app})
                test(f"POST /stop_display ({test_app})", code3 == 200, f"status={code3}")

        # --- Screen power (hotkey) ---
        code, data = api_post('/screen_off_hotkey')
        test("POST /screen_off_hotkey", code == 200, f"status={code}")
        if code == 200:
            test("  success 字段存在", 'success' in data)

        time.sleep(1)

        code, data = api_post('/screen_on_hotkey')
        test("POST /screen_on_hotkey", code == 200, f"status={code}")
        if code == 200:
            test("  success 字段存在", 'success' in data)

    except Exception as e:
        test("API 服务测试异常", False, str(e))
    finally:
        if server_proc:
            print("  停止服务...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except:
                server_proc.kill()
            print("  服务已停止")

    return True


# ============================================================
# Main
# ============================================================
def main():
    global passed, failed, total

    parser = argparse.ArgumentParser(description='Display Server Base Test')
    parser.add_argument('--no-import', action='store_true', help='跳过导入测试')
    parser.add_argument('--no-config', action='store_true', help='跳过配置测试')
    parser.add_argument('--no-class', action='store_true', help='跳过类实例化测试')
    parser.add_argument('--no-api', action='store_true', help='跳过 API 服务测试')
    parser.add_argument('--api-only', action='store_true', help='只测 API（服务需已启动）')
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  Display Server 基础功能验证")
    print("="*60)

    if args.api_only:
        test_api_server()
    else:
        if not args.no_import:
            test_imports()
        if not args.no_config:
            test_config()
        if not args.no_class:
            test_classes()
        if not args.no_api:
            test_api_server()

    # Summary
    section("测试总结")
    print(f"  总计: {total} 项")
    print(f"  通过: {passed} 项")
    print(f"  失败: {failed} 项")
    print(f"  通过率: {passed/total*100:.1f}%" if total > 0 else "  无测试项")

    if failed == 0:
        print("\n  ✅ 所有测试通过！")
        return 0
    else:
        print(f"\n  ❌ {failed} 项测试失败，请检查！")
        return 1


if __name__ == '__main__':
    sys.exit(main())
