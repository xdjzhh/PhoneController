# -*- coding: utf-8 -*-
"""
Toolbar 常驻服务（`python virtualdesk/virtual_desktop.py toolbar`，detached 长驻）。

一个 Qt 进程为【每个虚拟桌面】各托管一个 DesktopToolbar：

    桌面 #0（server 手机控制桌面，"AR Dock"）
      设置 → POST server /display/edit_mode（编辑模式）
      手机 → 手机主屏嵌入当前桌面
      电脑 → detached 拉起 virtualdesk/virtual_desktop.py uu-open（懒创建/切换 UU 桌面）
      键盘 → 开/关系统屏幕键盘 osk
    其余桌面（"AR UU Dock"，如 UU 远程桌面 #1）
      设置 → 占位无功能
      手机 → 手机主屏嵌入当前桌面
      电脑 → 切回 server_desktop（读 .vd_state.json）
      键盘 → 开/关 osk

服务 2s 轮询虚拟桌面列表自动增删/归位 dock（桌面懒创建/删除无需 IPC 通知）；
800ms 轮询 osk 状态：osk 打开期间把【当前桌面】的 dock 钉住显示，
保证「再点键盘钮关闭」始终可点；3s 定时保持 topmost。
"""

import json
import os
import subprocess
import sys
import time

# 服务文件（state/pid/log/脚本）随服务包落在 virtualdesk/ 内，与
# virtual_desktop.py 的 _SCRIPT_DIR 锚指向同一目录（单一真相源）。
_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_SERVICE_DIR, ".vd_state.json")
_VD_SCRIPT = os.path.join(_SERVICE_DIR, "virtual_desktop.py")

TOOLBAR_PID_FILE = os.path.join(_SERVICE_DIR, ".vd_toolbar.pid")
TOOLBAR_LOG = os.path.join(_SERVICE_DIR, ".vd_toolbar.log")
_UU_LOG = os.path.join(_SERVICE_DIR, ".vd_uu.log")


# ── state 读取 ────────────────────────────────────────────────────────────
def _read_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _server_desktop_index() -> int:
    """手机控制界面（server.py）所在虚拟桌面编号，0-based，默认 0。"""
    try:
        return int(_read_state().get("server_desktop", 0))
    except Exception:
        return 0


def _pid_alive(pid) -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return True


# ── 「电脑」回程：切回 server_desktop（逻辑迁自 front/uu_dock.py）─────────
def _switch_to_phone_desktop() -> None:
    idx = _server_desktop_index()
    try:
        from pyvda import VirtualDesktop, get_virtual_desktops
        desktops = get_virtual_desktops()
        if not (0 <= idx < len(desktops)):
            print(f"[Toolbar] 目标桌面 #{idx} 越界（当前共 {len(desktops)} 个），未切换。",
                  flush=True)
            return
        # 已在目标桌面时不要调 go()：SwitchDesktop 目标=当前桌面也会弹「桌面N」浮窗
        try:
            cur_ids = [d.id for d in get_virtual_desktops()]
            if cur_ids.index(VirtualDesktop.current().id) == idx:
                print(f"[Toolbar] 当前已在手机控制桌面 #{idx}，无需切换（跳过 go()）。",
                      flush=True)
                return
        except Exception:
            pass
        for attempt in range(4):
            desktops[idx].go()
            time.sleep(0.25)
            try:
                cur_ids = [d.id for d in get_virtual_desktops()]
                cur_idx = cur_ids.index(VirtualDesktop.current().id)
            except Exception:
                cur_idx = idx
            if cur_idx == idx:
                if attempt > 0:
                    print(f"[Toolbar] 第 {attempt + 1} 次尝试后切到手机控制桌面 #{idx}",
                          flush=True)
                else:
                    print(f"[Toolbar] 已切换到手机控制桌面 #{idx}", flush=True)
                return
            print(f"[Toolbar] go(#{idx}) 后当前仍为 #{cur_idx}，重试…", flush=True)
        print(f"[Toolbar] 4 次尝试后仍未切到手机控制桌面 #{idx}。", flush=True)
    except Exception as e:
        print(f"[Toolbar] 切换手机控制桌面失败: {e}", flush=True)


def _spawn_uu_open() -> None:
    """detached 拉起 uu-open：创建/启动/切换的全部决策交给独立进程，
    切桌/COM 轮询不卡本服务 UI。"""
    try:
        log_fp = open(_UU_LOG, "a", encoding="utf-8")
        try:
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            )
            p = subprocess.Popen(
                [sys.executable, _VD_SCRIPT, "uu-open"],
                cwd=_SERVICE_DIR,
                stdout=log_fp, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, close_fds=True,
                creationflags=creationflags,
            )
        finally:
            log_fp.close()
        print(f"[Toolbar] 已触发 uu-open（PID={p.pid}）：按需创建/切换 UU 远程桌面，"
              f"日志见 .vd_uu.log", flush=True)
    except Exception as e:
        print(f"[Toolbar] 触发 uu-open 失败: {e}", flush=True)


def run_toolbar_service() -> int:
    # detached + stdout 重定向日志时默认 GBK，强制 UTF-8 防 emoji 崩溃
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name, None)
        if _s is not None:
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    # 单实例：PID 文件指向活进程则直接退出（spawn 方幂等复用）
    try:
        if os.path.exists(TOOLBAR_PID_FILE):
            old = int(open(TOOLBAR_PID_FILE, "r", encoding="utf-8").read().strip() or "0")
            if old and old != os.getpid() and _pid_alive(old):
                print(f"[Toolbar] 已有实例 PID={old} 在运行，本次启动退出。", flush=True)
                return 0
    except Exception:
        pass

    from virtualdesk.toolbar import (
        DesktopToolbar,
        bring_main_display_to_current_desktop,
        declare_dpi_awareness,
    )
    declare_dpi_awareness()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    # virtual_desktop 仅函数级依赖（osk 开/关、hwnd 探测）
    import virtual_desktop as vd
    from virtualdesk import server_api

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    try:
        with open(TOOLBAR_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"[Toolbar] 写 PID 文件失败（不影响运行）: {e!r}", flush=True)

    class _Service:
        def __init__(self):
            # key = pyvda 桌面 id；value = (DesktopToolbar, 目标 desktop 对象)
            self.docks = {}          # id -> DesktopToolbar
            self.targets = {}        # id -> VirtualDesktop（最近一次 reconcile）
            self._edit_mode = False
            self._osk_open = False

        # ── dock 工厂 ─────────────────────────────────────────────────────
        def _make_dock(self, desktop, idx, server_idx):
            is_server = (idx == server_idx)
            title = "AR Dock" if is_server else "AR UU Dock"
            key = desktop.id

            def on_settings():
                self._toggle_edit_mode()

            def on_computer():
                # 角色在点击时按实时编号判定：server 桌面→uu-open；其余→切回
                cur_server_idx = _server_desktop_index()
                if self._index_of_key(key) == cur_server_idx:
                    _spawn_uu_open()
                else:
                    _switch_to_phone_desktop()

            dock = DesktopToolbar(
                window_title=title,
                on_settings=on_settings if is_server else None,
                on_phone=bring_main_display_to_current_desktop,
                on_computer=on_computer,
                on_keyboard=self._toggle_osk,
            )
            # 先以完全透明 show 出窗口（创建 HWND）→ 再迁到目标桌面，
            # 避免在用户当前桌面闪现；之后由悬停机制控制可见性。
            dock.setWindowOpacity(0.0)
            dock.show()
            dock._apply_win32_style()
            self.docks[key] = dock
            self.targets[key] = desktop
            QTimer.singleShot(120, lambda: self._move_dock_to_target(key))
            print(f"[Toolbar] 新建 dock {title} → 桌面 #{idx}（id={key}）", flush=True)
            return dock

        def _index_of_key(self, key):
            try:
                from pyvda import get_virtual_desktops
                ids = [d.id for d in get_virtual_desktops()]
                return ids.index(key)
            except Exception:
                return -1

        def _move_dock_to_target(self, key):
            dock = self.docks.get(key)
            target = self.targets.get(key)
            if dock is None or target is None:
                return
            try:
                from pyvda import AppView
                hwnd = int(dock.winId())
                view = AppView(hwnd=hwnd)
                if view.desktop.id != target.id:
                    view.move(target)
                dock._apply_win32_style()
            except Exception as e:
                print(f"[Toolbar] 迁移 dock 到桌面失败: {e!r}", flush=True)

        # ── 回调 ──────────────────────────────────────────────────────────
        def _toggle_edit_mode(self):
            if not server_api.is_up():
                print("[Toolbar] 设置动作跳过：server 尚未就绪（登录/启动阶段）。",
                      flush=True)
                return
            want = not self._edit_mode
            ok, msg = server_api.set_edit_mode(want)
            if not ok:
                print(f"[Toolbar] 切换编辑模式失败：{msg}", flush=True)
                return
            self._edit_mode = want
            for key, dock in self.docks.items():
                if dock.windowTitle() == "AR Dock":
                    dock.set_settings_active(want)
            print(f"[Toolbar] 编辑模式 → {'开启' if want else '关闭'}（server HTTP）",
                  flush=True)

        def _toggle_osk(self):
            try:
                vd._toggle_osk()
            except Exception as e:
                print(f"[Toolbar] 切换系统屏幕键盘失败: {e!r}", flush=True)

        # ── 定时：虚拟桌面增删 / dock 归位 ────────────────────────────────
        def reconcile(self):
            try:
                from pyvda import get_virtual_desktops
                desktops = get_virtual_desktops()
            except Exception as e:
                print(f"[Toolbar] 枚举虚拟桌面失败: {e!r}", flush=True)
                return
            server_idx = _server_desktop_index()
            alive = set()
            for idx, d in enumerate(desktops):
                alive.add(d.id)
                self.targets[d.id] = d
                if d.id not in self.docks:
                    self._make_dock(d, idx, server_idx)
                    continue
                dock = self.docks[d.id]
                want_title = "AR Dock" if idx == server_idx else "AR UU Dock"
                if dock.windowTitle() != want_title:
                    dock.setWindowTitle(want_title)
                # 自愈：dock 被系统/其它操作带离目标桌面时迁回
                self._move_dock_to_target(d.id)
            for dead in [k for k in self.docks if k not in alive]:
                try:
                    w = self.docks.pop(dead)
                    self.targets.pop(dead, None)
                    w.close()
                    w.deleteLater()
                    print(f"[Toolbar] 虚拟桌面已删除，移除对应 dock（id={dead}）",
                          flush=True)
                except Exception:
                    pass

        # ── 定时：osk 开 → 钉住当前桌面 dock；关 → 解除 ─────────────────
        def sync_osk(self):
            try:
                open_now = bool(vd._find_osk_hwnd())
            except Exception:
                open_now = False
            if open_now == self._osk_open:
                return
            self._osk_open = open_now
            cur_key = None
            try:
                from pyvda import VirtualDesktop
                cur_key = VirtualDesktop.current().id
            except Exception:
                pass
            for key, dock in self.docks.items():
                pin = open_now and key == cur_key
                dock.set_pinned(pin)
                dock.set_keyboard_active(pin)
            print(f"[Toolbar] 系统 osk 已{'打开' if open_now else '关闭'}，"
                  f"dock {'钉住显示' if open_now else '恢复悬停'}。", flush=True)

        def keep_topmost(self):
            for dock in list(self.docks.values()):
                try:
                    dock._apply_win32_style()
                except Exception:
                    pass

    svc = _Service()

    # 先同步一次，立即创建当前桌面 dock；随后定时轮询
    svc.reconcile()
    timer_reconcile = QTimer()
    timer_reconcile.timeout.connect(svc.reconcile)
    timer_reconcile.start(2000)

    timer_osk = QTimer()
    timer_osk.timeout.connect(svc.sync_osk)
    timer_osk.start(800)

    timer_top = QTimer()
    timer_top.timeout.connect(svc.keep_topmost)
    timer_top.start(3000)

    print("[Toolbar] 服务已启动（每桌面一个 dock，鼠标移到屏幕底部居中唤出；"
          "键盘钮开/关系统 osk）。", flush=True)
    code = app.exec()

    # 正常退出（stop 直接 taskkill，一般走不到这里）：自清 PID
    try:
        if os.path.exists(TOOLBAR_PID_FILE):
            os.remove(TOOLBAR_PID_FILE)
    except Exception:
        pass
    return code
