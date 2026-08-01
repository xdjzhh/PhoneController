#!/usr/bin/env python3
"""
Phone Agent HTTP Server - 将 Phone Agent 作为 HTTP 服务运行

使用方法：
    python server.py --port 8080

API 端点：
    POST /run  - 执行任务
    GET  /status - 查看状态
    POST /reset - 重置 Agent
"""

import ctypes
import os
import sys

# Set DPI awareness BEFORE any imports/window creation to ensure
# GetWindowRect and mouse hook coordinates use physical pixels.
if os.name == 'nt':
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

import argparse
import json
from flask import Flask, request, jsonify
from phone_agent import PhoneAgent
from phone_agent.model import ModelConfig
from phone_agent.agent import AgentConfig

from scrcpycontrol.display_interface import get_display_manager

app = Flask(__name__)

# 全局配置
model_config = None
agent_config = None
config_initialized = False
display_manager = None
agent = None
agent_initialized = False


def initialize_config(base_url: str, model: str, api_key: str):
    """初始化配置"""
    global model_config, agent_config, config_initialized
    
    model_config = ModelConfig(
        base_url=base_url,
        model_name=model,
        api_key=api_key,
    )
    
    agent_config = AgentConfig(
        max_steps=100,
        verbose=True,
    )
    
    config_initialized = True
    print("✅ Configuration initialized successfully")


def initialize_display_manager(start_main: bool = True, start_monitor: bool = True):
    """初始化显示管理器并可选启动主屏和屏幕监控"""
    global display_manager
    try:
        display_manager = get_display_manager()
        if start_main:
            print("📱 Starting main display...")
            result = display_manager.start_main_display()
            if result.get("success"):
                print("✅ Main display started successfully")
            else:
                print(f"⚠️  Main display failed to start: {result.get('error')}")
                return False
        if start_monitor:
            print("👁️  Starting screen lock monitor...")
            monitor_result = display_manager.start_screen_monitor()
            if monitor_result.get("success"):
                print("✅ Screen lock monitor started")
            else:
                print(f"⚠️  Screen monitor failed: {monitor_result.get('error')}")
        return True
    except Exception as e:
        print(f"⚠️  Display manager initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False


@app.route('/run', methods=['POST'])
def run_task():
    """执行任务"""
    global agent, agent_initialized
    if not config_initialized:
        return jsonify({
            'success': False,
            'error': 'Configuration not initialized'
        }), 500
    
    data = request.get_json()
    task = data.get('task', '')
    print(data)
    
    if not task:
        return jsonify({
            'success': False,
            'error': 'Task is required'
        }), 400
    
    try:
        print(f"\n📱 Running task: {task}")
        agent = PhoneAgent(
            model_config=model_config,
            agent_config=agent_config,
        )
        agent_initialized = True
        result = agent.run(task)
        
        return jsonify({
            'success': True,
            'task': task,
            'result': result
        })
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/status', methods=['GET'])
def get_status():
    """获取状态"""
    global agent, agent_initialized, display_manager
    status_info = {
        'status': 'running' if agent_initialized else 'not_initialized',
        'agent_initialized': agent_initialized,
        'step_count': agent._step_count if agent else 0,
        'config_initialized': config_initialized,
    }
    if display_manager:
        status_info['main_display_running'] = display_manager.main_display_running
        status_info['active_app_displays'] = display_manager.active_app_count
    return jsonify(status_info)


@app.route('/reset', methods=['POST'])
def reset_agent():
    """重置 Agent"""
    global agent, agent_initialized
    if agent:
        agent.reset()
        return jsonify({'success': True, 'message': 'Agent reset'})
    return jsonify({'success': False, 'error': 'Agent not initialized'}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    global display_manager
    info = {'status': 'healthy'}
    if display_manager:
        info['main_display_alive'] = display_manager.main_display_running
        info['active_app_displays'] = display_manager.active_app_count
    return jsonify(info)


@app.route('/display/list', methods=['GET'])
def list_displays():
    """列出所有虚拟显示"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    result = display_manager.list_displays()
    return jsonify(result)


@app.route('/display/start', methods=['POST'])
def start_app_display():
    """启动 app 虚拟显示"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package', '')
    if not app_package:
        return jsonify({'success': False, 'error': 'app_package is required'}), 400
    orientation = data.get('orientation', None)
    result = display_manager.start_app_display(app_package, orientation=orientation)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/stop', methods=['POST'])
def stop_app_display():
    """停止 app 虚拟显示"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package', '')
    if not app_package:
        return jsonify({'success': False, 'error': 'app_package is required'}), 400
    result = display_manager.stop_app_display(app_package)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/screen_off', methods=['POST'])
def screen_off():
    """通过 scrcpy-client 关闭屏幕（只关背光，不锁屏）"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    result = display_manager.screen_off()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/screen_on', methods=['POST'])
def screen_on():
    """通过 scrcpy-client 点亮屏幕"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    result = display_manager.screen_on()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/screen_monitor/start', methods=['POST'])
def start_screen_monitor():
    """启动锁屏监控（自动解锁并息屏）"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    result = display_manager.start_screen_monitor()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/screen_monitor/stop', methods=['POST'])
def stop_screen_monitor():
    """停止锁屏监控"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    result = display_manager.stop_screen_monitor()
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/screen_monitor/status', methods=['GET'])
def screen_monitor_status():
    """获取锁屏监控状态"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    return jsonify({
        'success': True,
        'running': display_manager.screen_monitor_running,
    })


@app.route('/display/opacity', methods=['POST'])
def set_window_opacity():
    """设置窗口透明度"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package')
    opacity = data.get('opacity', 1.0)
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'opacity must be a number between 0.0 and 1.0'}), 400
    result = display_manager.set_window_opacity(app_package, opacity)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/orientation', methods=['POST'])
def set_display_orientation():
    """设置屏幕方向（横屏/竖屏），支持主屏和虚拟屏"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package')  # None 或 "main" 表示主屏
    orientation = data.get('orientation', 'portrait')
    if orientation not in ('portrait', 'landscape'):
        return jsonify({'success': False, 'error': "orientation must be 'portrait' or 'landscape'"}), 400
    result = display_manager.set_display_orientation(app_package, orientation)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/display/edit_mode', methods=['POST'])
def set_edit_mode():
    """切换窗口编辑模式（显示/隐藏红绿灯和缩放热区）"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package')  # None 或 "main" 表示主屏
    editing = data.get('editing', True)
    result = display_manager.set_edit_mode(app_package, editing=editing)
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


def main():
    parser = argparse.ArgumentParser(description='Phone Agent HTTP Server')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8080, help='Port to listen on')
    parser.add_argument('--base-url', default='https://open.bigmodel.cn/api/paas/v4', 
                        help='Model API base URL')
    parser.add_argument('--model', default='autoglm-phone', help='Model name')
    parser.add_argument('--apikey', default='cae66fb0786343ee9e078a00ec683721.3HmwuIx9d5Vf22DD', help='API key')
    parser.add_argument('--no-main-display', action='store_true', help='Do not start main display on startup')
    parser.add_argument('--no-screen-monitor', action='store_true', help='Do not start screen lock monitor')
    parser.add_argument('--auto-start', default='com.huawei.android.totemweather',
                        help='Comma-separated app packages to auto-start on launch (default: weather)')
    args = parser.parse_args()
    
    # Initialize agent config
    print("🔧 Initializing Phone Agent...")
    initialize_config(
        base_url=args.base_url,
        model=args.model,
        api_key=args.apikey
    )
    
    # Initialize display manager and start main display
    if not args.no_main_display:
        print("🖥️  Initializing display manager...")
        initialize_display_manager(
            start_main=True,
            start_monitor=not args.no_screen_monitor
        )

    # Auto-start configured app cards (e.g. weather)
    if display_manager and args.auto_start:
        auto_apps = [p.strip() for p in args.auto_start.split(',') if p.strip()]
        for app_pkg in auto_apps:
            print(f"📱 Auto-starting app card: {app_pkg}...")
            import time as _time
            _time.sleep(3)  # wait for main display to settle
            result = display_manager.start_app_display(app_pkg)
            if result.get('success'):
                print(f"✅ {app_pkg} started: {result.get('message', '')}")
            else:
                print(f"⚠️  Failed to start {app_pkg}: {result.get('error', '')}")
    
    # Start server
    print(f"\n🚀 Starting server on {args.host}:{args.port}")
    print(f"📡 API endpoints:")
    print(f"   POST http://{args.host}:{args.port}/run")
    print(f"   GET  http://{args.host}:{args.port}/status")
    print(f"   POST http://{args.host}:{args.port}/reset")
    print(f"   GET  http://{args.host}:{args.port}/health")
    print(f"   GET  http://{args.host}:{args.port}/display/list")
    print(f"   POST http://{args.host}:{args.port}/display/start")
    print(f"   POST http://{args.host}:{args.port}/display/stop")
    print(f"   POST http://{args.host}:{args.port}/display/screen_off")
    print(f"   POST http://{args.host}:{args.port}/display/screen_on")
    print(f"   POST http://{args.host}:{args.port}/display/screen_monitor/start")
    print(f"   POST http://{args.host}:{args.port}/display/screen_monitor/stop")
    print(f"   GET  http://{args.host}:{args.port}/display/screen_monitor/status")
    print(f"   POST http://{args.host}:{args.port}/display/opacity")
    print(f"   POST http://{args.host}:{args.port}/display/orientation")
    print(f"   POST http://{args.host}:{args.port}/display/edit_mode")
    print(f"\n🖱️  Window controls:")
    print(f"   🟥 Red button    → Close window")
    print(f"   🟨 Yellow button → Minimize")
    print(f"   🟩 Green button  → Pin/fix window (hide controls)")
    print(f"   🖱️  Right-click (on pinned card) → Re-enter edit mode")
    print(f"\nPress Ctrl+C to stop")
    
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
