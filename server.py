#!/usr/bin/env python3
"""
Phone Agent HTTP Server - 将 Phone Agent 作为 HTTP 服务运行

使用方法：
    python server.py                      # 启动参数读同目录 start_proporties.yaml
    python server.py --port 9000          # 命令行参数覆盖 YAML 同名项

API 端点：
    POST /run  - 执行任务
    GET  /status - 查看状态
    POST /reset - 重置 Agent
"""

import ctypes
import os
import sys
import multiprocessing

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
import threading
import re
from flask import Flask, request, jsonify
from phone_agent import PhoneAgent
from phone_agent.model import ModelConfig
from phone_agent.agent import AgentConfig

from scrcpycontrol.display_interface import get_display_manager

multiprocessing.freeze_support()

app = Flask(__name__)

# 全局配置
model_config = None
agent_config = None
config_initialized = False
display_manager = None
agent = None
agent_initialized = False
backdrop_manager = None
# HTTP 服务句柄（/exit 用 httpd.shutdown() 进程内优雅停；无控制台进程收不到
# CTRL_C 事件，信号方案已弃用）
_httpd = None

_task_lock = threading.Lock()
_task_running = False
_current_agent = None
_current_task = None

STOP_KEYWORDS = re.compile(
    r'(停止任务|快停下|跑飞了|终止任务|立即停止|紧急停止|立刻停下|中止任务|取消任务)',
    re.IGNORECASE
)


def _is_stop_command(task: str) -> bool:
    """检测任务文本是否为停止指令。"""
    if not task:
        return False
    text = task.strip().lower()
    # 直接匹配停止关键词（整个文本很短且包含停止词，或包含紧急短语）
    if len(text) <= 20 and STOP_KEYWORDS.search(text):
        return True
    # 包含紧急短语
    emergency_phrases = ['快停下', '跑飞了', '紧急停止', '立即停止', 'emergency stop', 'abort']
    for phrase in emergency_phrases:
        if phrase in text:
            return True
    return False


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


def start_backdrop_window():
    """启动Qt深色背景窗口，模拟无桌面Linux环境"""
    global backdrop_manager
    try:
        from front.qt_backdrop import get_backdrop_manager
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "scrcpycontrol", "display_config.json")
        backdrop_manager = get_backdrop_manager(config_path=config_path)
        if backdrop_manager.start():
            print("✅ Qt backdrop window started (Linux desktop simulation)")
            return True
        else:
            print("⚠️  Qt backdrop window failed to start (continuing without backdrop)")
            backdrop_manager = None
            return False
    except Exception as e:
        print(f"⚠️  Failed to start Qt backdrop: {e}")
        backdrop_manager = None
        return False


def initialize_display_manager(start_main: bool = True, start_monitor: bool = True,
                               start_backdrop: bool = True):
    """初始化显示管理器并可选启动主屏、屏幕监控和背景窗口"""
    global display_manager
    try:
        if start_backdrop:
            print("🖥️  Starting Qt Linux desktop backdrop...")
            start_backdrop_window()

        display_manager = get_display_manager()
        if start_main:
            print("📱 Starting main display...")
            result = display_manager.start_main_display()
            # ── default_load 兼容：main_display.default_load=false 时也是 success+skipped，
            #    不应当让初始化失败，只需要打印提示并继续后续流程。
            if result.get("success"):
                if result.get("skipped"):
                    print(f"⏭️  Main display skipped by config: {result.get('message')}")
                else:
                    print("✅ Main display started successfully")
            else:
                print(f"⚠️  Main display failed to start: {result.get('error')}")
                return False

        # ── default_load：批量启动 app_displays 中 default_load=true 的应用卡片 ──
        #    用户需求：启动 APP 时判断 default_load=true 才创建窗口卡片，false 不创建。
        if display_manager is not None:
            try:
                dm_cfg = display_manager.load_config()
                app_displays_cfg = dm_cfg.get("app_displays", {}) or {}
                total_apps = len(app_displays_cfg)
                auto_count = 0
                skip_count = 0
                print(f"\n🚀 Auto-starting app displays (default_load=true only): "
                      f"found {total_apps} app_display entries...")
                for pkg, app_cfg in app_displays_cfg.items():
                    if not isinstance(app_cfg, dict):
                        continue
                    default_load = bool(app_cfg.get("default_load", True))
                    if not default_load:
                        win_title = app_cfg.get("window_title", pkg)
                        print(f"   ⏭️  SKIP [{win_title} | {pkg}] -> default_load=false")
                        skip_count += 1
                        continue
                    # default_load=true → 自动创建卡片窗口
                    try:
                        _r = display_manager.start_app_display(pkg)
                        if _r.get("success"):
                            print(f"   ✅ START [{pkg}] ok (android_display_id="
                                  f"{_r.get('android_display_id')})")
                            auto_count += 1
                        else:
                            print(f"   ⚠️  START [{pkg}] failed: {_r.get('error')}")
                    except Exception as e_start:
                        print(f"   ⚠️  START [{pkg}] exception: {e_start}")
                print(f"   🔚 App displays auto-start done: {auto_count} started / "
                      f"{skip_count} skipped (default_load=false) / "
                      f"{total_apps - auto_count - skip_count} failed")
            except Exception as e_auto:
                print(f"⚠️  Failed to auto-start app_displays: {e_auto}")
                import traceback
                traceback.print_exc()

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
    global agent, agent_initialized, _task_running, _current_agent, _current_task

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

    is_stop = _is_stop_command(task)

    with _task_lock:
        if _task_running:
            if is_stop:
                # 停止指令：终止正在运行的任务
                running_task = _current_task or "unknown"
                print(f"\n🛑 Stop command received: '{task}' → stopping running task: '{running_task}'")
                if _current_agent:
                    _current_agent.stop()
                return jsonify({
                    'success': True,
                    'stopped': True,
                    'message': f'Stopped running task: {running_task}'
                })
            else:
                # 非停止指令：抛弃新任务
                print(f"\n⚠️  Task running, discarding new task: '{task}'")
                return jsonify({
                    'success': False,
                    'error': 'A task is already running',
                    'running_task': _current_task
                }), 409

        # 没有任务在运行
        if is_stop:
            # 停止指令但没有运行中的任务
            return jsonify({
                'success': True,
                'stopped': False,
                'message': 'No task is running'
            })

        # 启动新任务
        _task_running = True
        _current_task = task
        print(f"\n📱 Running task: {task}")
    
    try:
        agent = PhoneAgent(
            model_config=model_config,
            agent_config=agent_config,
        )
        with _task_lock:
            _current_agent = agent
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
    finally:
        with _task_lock:
            _task_running = False
            _current_agent = None
            _current_task = None


@app.route('/status', methods=['GET'])
def get_status():
    """获取状态"""
    global agent, agent_initialized, display_manager
    status_info = {
        'status': 'running' if agent_initialized else 'not_initialized',
        'agent_initialized': agent_initialized,
        'task_running': _task_running,
        'current_task': _current_task,
        'step_count': agent._step_count if agent else 0,
        'config_initialized': config_initialized,
    }
    if display_manager:
        status_info['main_display_running'] = display_manager.main_display_running
        status_info['active_app_displays'] = display_manager.active_app_count
    # backdrop 子进程 PID 经 HTTP 报给 virtual_desktop bootstrap 登记入它自己的
    # state；跨域只走 HTTP，不依赖任何共享文件（backdrop 未启动时字段缺省）。
    if backdrop_manager and backdrop_manager.is_running:
        status_info['backdrop_pid'] = backdrop_manager.child_pid
    return jsonify(status_info)


@app.route('/exit', methods=['POST'])
def exit_server():
    """virtualdesk stop 的优雅停止入口：回 200 后延迟 httpd.shutdown()——
    进程内标志（无控制台进程也可靠送达；CTRL_C 事件在 CREATE_NO_WINDOW
    进程里投递不到 handler，GenerateConsoleCtrlEvent 成空操作，已弃用）。
    serve_forever 返回 → 走 main() finally 停 backdrop + atexit 停全部
    scrcpy 显示树。失败不强退，交由 virtualdesk 的 taskkill 兜底收尾。"""
    def _self_shutdown():
        import time as _time
        _time.sleep(0.5)
        print("\n[exit] 收到 /exit 请求：httpd.shutdown()，走 finally/atexit 优雅链...", flush=True)
        try:
            _httpd.shutdown()
        except Exception as e:
            print(f"[exit] ⚠️ shutdown 失败（由外部 taskkill 兜底）：{e}", flush=True)

    threading.Thread(target=_self_shutdown, daemon=True).start()
    return jsonify({'success': True, 'message': 'exiting via httpd.shutdown()'})


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
    """启动 app 虚拟显示（卡片的窗口宽高/分辨率已在 display_config.json 中按各自宽高比定义）"""
    global display_manager
    if not display_manager:
        return jsonify({'success': False, 'error': 'Display manager not initialized'}), 500
    data = request.get_json() or {}
    app_package = data.get('app_package', '')
    if not app_package:
        return jsonify({'success': False, 'error': 'app_package is required'}), 400
    result = display_manager.start_app_display(app_package)
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
    # 联动通知/思考面板的编辑外观（scrcpy 卡片已由 display_manager 处理）。
    # 由独立 Toolbar 服务 Dock 的「设置」钮经 HTTP 触发；backdrop 未启动时跳过。
    if result.get('success') and backdrop_manager and backdrop_manager.is_running:
        try:
            backdrop_manager.set_panels_edit_mode(bool(editing))
        except Exception as e:
            print(f"[server] set_panels_edit_mode 联动失败（不影响卡片）: {e!r}")
    status_code = 200 if result.get('success') else 500
    return jsonify(result), status_code


@app.route('/notification/show', methods=['POST'])
def show_notification_panel():
    """显示Qt原生macOS风格通知面板"""
    global backdrop_manager
    if not backdrop_manager or not backdrop_manager.is_running:
        return jsonify({'success': False, 'error': 'Backdrop manager not running'}), 500
    backdrop_manager.show_notification()
    return jsonify({'success': True})


@app.route('/notification/hide', methods=['POST'])
def hide_notification_panel():
    """隐藏通知面板"""
    global backdrop_manager
    if not backdrop_manager or not backdrop_manager.is_running:
        return jsonify({'success': False, 'error': 'Backdrop manager not running'}), 500
    backdrop_manager.hide_notification()
    return jsonify({'success': True})


@app.route('/notification/toggle', methods=['POST'])
def toggle_notification_panel():
    """切换通知面板显示/隐藏"""
    global backdrop_manager
    if not backdrop_manager or not backdrop_manager.is_running:
        return jsonify({'success': False, 'error': 'Backdrop manager not running'}), 500
    backdrop_manager.toggle_notification()
    return jsonify({'success': True})


@app.route('/notification/refresh', methods=['POST'])
def refresh_notifications():
    """立即刷新通知数据"""
    global backdrop_manager
    if not backdrop_manager or not backdrop_manager.is_running:
        return jsonify({'success': False, 'error': 'Backdrop manager not running'}), 500
    backdrop_manager.refresh_notifications()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# 后台日志：stdout/stderr 追加镜像到 logs/server.log（控制台/思考卡片显示
# 之外多一份文件记录，进程崩溃或无窗口场景也能事后追查）。tee 尽量早装，
# 之后的所有 print/traceback 都会落盘。
# ---------------------------------------------------------------------------
class _FileTee:
    """stdout/stderr 透传包装：原样写原流 + 追加写日志文件。"""

    def __init__(self, underlying, fp):
        self._underlying = underlying
        self._fp = fp

    def write(self, s):
        try:
            self._underlying.write(s)
        except Exception:
            pass
        try:
            self._fp.write(s)
            self._fp.flush()
        except Exception:
            pass
        return len(s)

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def flush(self):
        for target in (self._underlying, self._fp):
            try:
                target.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._underlying, name)


def _install_file_tee():
    try:
        import time as _time
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        fp = open(os.path.join(log_dir, 'server.log'), 'a', encoding='utf-8')
        fp.write(f"\n===== server start {_time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} =====\n")
        fp.flush()
        sys.stdout = _FileTee(sys.stdout, fp)
        sys.stderr = _FileTee(sys.stderr, fp)
        print(f"[server] 📄 后台日志已开启：{os.path.join(log_dir, 'server.log')}")
    except Exception as _tee_err:
        print(f"[server] file log tee skipped: {_tee_err}", flush=True)


# 启动参数 YAML（与 server.py 同目录）：python server.py 默认读取
_START_PROPERTIES_NAME = 'start_proporties.yaml'


def _load_start_properties(config_path=None):
    """读取启动参数 YAML，返回 dict。

    单一配置来源策略：命令行显式参数 > YAML > 代码内置默认值。
    文件缺失/解析失败只告警并返回 {}（回退内置默认值），绝不阻断启动。
    """
    path = config_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), _START_PROPERTIES_NAME)
    if not os.path.isfile(path):
        print(f"[server] ℹ️ 未找到启动参数文件 {path}，使用命令行/内置默认值。")
        return {}
    try:
        import yaml
    except ImportError:
        print("[server] ⚠️ 未安装 PyYAML，无法读取 "
              f"{_START_PROPERTIES_NAME}（pip install pyyaml）；回退命令行/内置默认值。")
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[server] ⚠️ 解析启动参数文件 {path} 失败，回退命令行/内置默认值：{e!r}")
        return {}
    if not isinstance(data, dict):
        print(f"[server] ⚠️ 启动参数文件 {path} 顶层不是键值映射，已忽略。")
        return {}
    print(f"[server] ⚙️ 已加载启动参数文件：{path}（命令行同名参数优先）")
    return data


def _acquire_singleton_lock(lock_name: str):
    """服务级单例锁（跨平台文件锁：msvcrt 区域锁 / fcntl.flock）：锁在服务
    身份上，与端口/参数无关（换端口启动同样被拦）。进程存活期间持有；正常
    退出或被强杀后由 OS 自动释放——无残留死锁，无需 PID 活性检查与 atexit
    清理。返回的句柄引用必须保留（丢引用=GC 关闭文件=锁释放）。"""
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             f"{lock_name}.lock")
    fp = open(lock_path, "a+", encoding="utf-8")
    fp.write(" ")          # 字节 0 = 锁区占位；PID 写在其后（冲突方可读）
    fp.flush()
    try:
        fp.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)   # Windows：区域锁
        else:
            import fcntl
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # Linux/macOS：整文件锁
    except OSError:
        old_pid = ""
        try:
            fp.seek(1)     # 跳过锁区（字节 0 被独占，读不到）
            old_pid = fp.read().strip()
        except Exception:
            pass
        print(f"[singleton] 已有实例在运行{f'（PID={old_pid}）' if old_pid else ''}，"
              f"锁文件={lock_path}，本次启动退出。", flush=True)
        sys.exit(0)
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(" ")          # 字节 0 = 锁区占位
        fp.write(str(os.getpid()))  # PID 写在锁区外，冲突方可读
        fp.flush()
    except Exception:
        pass
    # fp 故意不关闭：进程存续期间持锁，退出后由 OS 释放
    return fp


def main():
    # ── 单例锁必须最先：已有实例（任意端口/参数启动）在跑就直接退出 ──
    _singleton_lock = _acquire_singleton_lock("open_autoglm")

    # ── 统一 adb 来源：优先使用 scrcpy 自带的 adb.exe，必须早于任何
    #    adbutils / "adb" 子进程调用（adbutils 每次按 PATH 实时解析）──
    try:
        from scrcpycontrol.display_server import ensure_bundled_adb_on_path
        ensure_bundled_adb_on_path()
    except Exception as _adb_err:
        print(f"[server] bundled adb setup skipped: {_adb_err}", flush=True)

    # ── Install console tee FIRST so every subsequent print/stderr message
    #    appears BOTH in terminal AND in thinking log card window ──
    try:
        from front.console_tee import install_console_tee
        install_console_tee()
    except Exception as _tee_err:
        # Never let the tee bootstrap crash server startup
        print(f"[server] console_tee install skipped: {_tee_err}", flush=True)

    # ── 后台日志文件 tee：在 console_tee 之后套一层，print 双写文件 ──
    _install_file_tee()

    # 参数默认值一律为 None：用于区分「命令行显式传入」与「未传」。
    # 未传时回退 start_proporties.yaml，再回退下面的内置默认值
    # （优先级：命令行 > YAML > 内置默认），保证 `python server.py`
    # 与 virtual_desktop.py bootstrap 传参两种启动方式都不被破坏。
    parser = argparse.ArgumentParser(description='Phone Agent HTTP Server')
    parser.add_argument('--config', default=None,
                        help=f'启动参数 YAML 路径（默认=server.py 同目录 {_START_PROPERTIES_NAME}）')
    parser.add_argument('--host', default=None, help='Host to bind to（默认读 YAML: 127.0.0.1）')
    parser.add_argument('--port', type=int, default=None, help='Port to listen on（默认读 YAML: 8080）')
    parser.add_argument('--base-url', default=None,
                        help='Model API base URL（默认读 YAML）')
    parser.add_argument('--model', default=None, help='Model name（默认读 YAML: autoglm-phone）')
    parser.add_argument('--apikey', default=None, help='API key（默认读 YAML）')
    # store_true 的 default 必须是 None（而非 False），否则无法识别
    # 「用户没传该 flag」——显式传入时值为 False，未传为 None → 回退 YAML。
    parser.add_argument('--no-main-display', action='store_true', default=None,
                        help='Do not start main display on startup（YAML: no_main_display）')
    parser.add_argument('--no-screen-monitor', action='store_true', default=None,
                        help='Do not start screen lock monitor（YAML: no_screen_monitor）')
    parser.add_argument('--no-backdrop', action='store_true', default=None,
                        help='Do not start Qt Linux desktop backdrop（YAML: no_backdrop）')
    cli_args = parser.parse_args()

    props = _load_start_properties(cli_args.config)

    def _pick(cli_val, yaml_key, builtin_default):
        """命令行显式值优先；否则取 YAML；YAML 无此项再用内置默认。"""
        if cli_val is not None:
            return cli_val
        return props.get(yaml_key, builtin_default)

    args = argparse.Namespace(
        host=str(_pick(cli_args.host, 'host', '127.0.0.1')),
        port=int(_pick(cli_args.port, 'port', 8080)),
        base_url=str(_pick(cli_args.base_url, 'base_url',
                           'https://open.bigmodel.cn/api/paas/v4')),
        model=str(_pick(cli_args.model, 'model', 'autoglm-phone')),
        apikey=str(_pick(cli_args.apikey, 'apikey',
                         'cae66fb0786343ee9e078a00ec683721.3HmwuIx9d5Vf22DD')),
        no_main_display=bool(_pick(cli_args.no_main_display, 'no_main_display', False)),
        no_screen_monitor=bool(_pick(cli_args.no_screen_monitor, 'no_screen_monitor', False)),
        no_backdrop=bool(_pick(cli_args.no_backdrop, 'no_backdrop', False)),
    )

    # Initialize agent config
    print("🔧 Initializing Phone Agent...")
    initialize_config(
        base_url=args.base_url,
        model=args.model,
        api_key=args.apikey
    )
    
    # Initialize display manager (starts backdrop first, then main display)
    if not args.no_main_display:
        print("🖥️  Initializing display manager...")
        initialize_display_manager(
            start_main=True,
            start_monitor=not args.no_screen_monitor,
            start_backdrop=not args.no_backdrop,
        )
    elif not args.no_backdrop:
        start_backdrop_window()
    
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
    print(f"   POST http://{args.host}:{args.port}/display/edit_mode")
    print(f"   POST http://{args.host}:{args.port}/notification/show")
    print(f"   POST http://{args.host}:{args.port}/notification/hide")
    print(f"   POST http://{args.host}:{args.port}/notification/toggle")
    print(f"   POST http://{args.host}:{args.port}/notification/refresh")
    print(f"\n🖥️  Desktop mode:")
    if backdrop_manager and backdrop_manager.is_running:
        print(f"   ✅ Qt Linux dark backdrop active — virtual cards float above")
        if backdrop_manager.notification_hwnd:
            print(f"   ✅ macOS-style notification panel active (auto-refreshes via ADB)")
        if getattr(backdrop_manager, 'thinking_hwnd', 0):
            print(f"   ✅ Thinking log panel active — mirrors this terminal (same manager as notification)")
    else:
        print(f"   ⚠️  Backdrop disabled — cards float over regular desktop")
    print(f"\n🖱️  Window controls (Notification panel + Thinking log panel: identical behavior):")
    print(f"   🟥 Red button    → Close window")
    print(f"   🟨 Yellow button → Minimize")
    print(f"   🟩 Green button  → Pin/fix window (hide controls)")
    print(f"   🛠️  Dock toolbar  → Toggle edit mode on BOTH notification + scrcpy cards")
    print(f"\nPress Ctrl+C to stop")

    try:
        # 不用 app.run：直接持有 httpd——/exit 需要 httpd.shutdown() 做进程内
        # 优雅停（app.run 不暴露 shutdown 句柄）。threaded=True 与原 app.run
        # 语义一致（/run 长任务不阻塞 /status）。
        from werkzeug.serving import make_server
        global _httpd
        _httpd = make_server(args.host, args.port, app, threaded=True)
        print(f"✅ HTTP server listening on http://{args.host}:{args.port}")
        _httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    finally:
        if backdrop_manager:
            try:
                backdrop_manager.stop()
            except Exception:
                pass


if __name__ == '__main__':
    main()
