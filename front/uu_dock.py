# -*- coding: utf-8 -*-
"""
AR UU Dock —— UU 远程桌面（桌面 #2）上的回程 Dock。

只由 virtual_desktop.py bootstrap 以 detached 方式在桌面 #2 启动，用户不会
直接运行本文件。视觉与交互完全复刻 front/qt_backdrop.py 的 DockToolbarWidget：

    ┌─────────┬─────────┐
    │  设置    │  电脑   │
    └─────────┴─────────┘

  · 设置（齿轮，index 0）：占位按钮，**无功能**（手机控制界面的配置在桌面 #1）；
  · 电脑（显示器，index 1）：点击直接切回手机控制界面所在虚拟桌面
    （编号读项目根 .vd_state.json 的 server_desktop，默认 #1）。

样式硬约束与 AR Dock 一致：WS_EX_APPWINDOW + 清除 WS_EX_TOOLWINDOW，
否则窗口不被 Virtual Desktop Manager 注册、会在所有桌面漂浮。
"""

import ctypes
import json
import math
import os
import sys

# 物理像素坐标，避免 125% 缩放下 Dock 位置/点击偏移
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import ctypes.wintypes as wintypes
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

user32 = ctypes.windll.user32

# ── Win32 常量 ────────────────────────────────────────────────────────────
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_NOACTIVATE = 0x08000000
WS_EX_NOPARENTNOTIFY = 0x00000004
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_FILE = os.path.join(_PROJECT_ROOT, ".vd_state.json")


def _safe_hwnd(h):
    if h is None:
        return 0
    if isinstance(h, int):
        return h
    if hasattr(h, "value"):
        v = h.value
        if isinstance(v, int):
            return v
        if isinstance(v, bytes):
            return int.from_bytes(v, byteorder="little", signed=False)
        return int(v) if v else 0
    try:
        return int(h)
    except (TypeError, ValueError):
        try:
            return ctypes.cast(ctypes.pointer(h), ctypes.POINTER(ctypes.c_void_p)).contents.value or 0
        except Exception:
            return 0


def _server_desktop_index() -> int:
    """手机控制界面（server.py）所在虚拟桌面编号，0-based，默认 1。"""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("server_desktop", 1))
    except Exception:
        return 1


class UUDockWidget(QWidget):
    """Mac 风格底部 Dock：悬停淡入，两个图标（设置=占位、电脑=切回手机桌面）。"""

    def __init__(self):
        super().__init__()
        self._hovering = False
        self._hover_icon = -1
        self._pressed_icon = -1
        self._icon_size = 48
        self._icon_spacing = 16
        self._padding = 16
        self._dock_hidden = True
        self._opacity = 0.0
        self._trigger_zone = 4
        self._dock_w = 2 * self._icon_size + self._icon_spacing + 2 * self._padding
        self._dock_h = self._icon_size + 2 * self._padding
        self._init_ui()

    def _init_ui(self):
        self.setWindowTitle("AR UU Dock")
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            x = sg.center().x() - self._dock_w // 2
            y = sg.bottom() - self._dock_h - 8
            self.move(x, y)
        self.setFixedSize(self._dock_w, self._dock_h)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)

        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._do_fade)
        self._fade_timer.setInterval(16)

        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._check_mouse_hover)
        self._hover_timer.setInterval(50)
        self._hover_timer.start()

    # ── 布局 / 命中 ────────────────────────────────────────────────────────
    def _get_icon_rect(self, index):
        start_x = self._padding
        x = start_x + index * (self._icon_size + self._icon_spacing)
        y = self._padding
        scale = 1.0
        if self._hover_icon == index:
            scale = 1.25
        if self._pressed_icon == index:
            scale = 1.1
        size = int(self._icon_size * scale)
        offset = (self._icon_size - size) // 2
        return x + offset, y + offset, size, size

    def _icon_at(self, pos):
        for i in range(2):
            ix, iy, isize, _ = self._get_icon_rect(i)
            if ix <= pos.x() <= ix + isize and iy <= pos.y() <= iy + isize:
                return i
        return -1

    # ── 按钮动作 ──────────────────────────────────────────────────────────
    def _switch_to_phone_desktop(self):
        """点击「电脑」：切回手机控制界面桌面（.vd_state.json: server_desktop）。"""
        idx = _server_desktop_index()
        try:
            from pyvda import get_virtual_desktops
            desktops = get_virtual_desktops()
            if 0 <= idx < len(desktops):
                desktops[idx].go()
                print(f"[UUDock] 已切换到手机控制桌面 #{idx}", flush=True)
            else:
                print(f"[UUDock] 目标桌面 #{idx} 越界（当前共 {len(desktops)} 个），未切换。",
                      flush=True)
        except Exception as e:
            print(f"[UUDock] 切换手机控制桌面失败: {e}", flush=True)

    # ── 悬停淡入淡出（与 AR Dock 相同） ───────────────────────────────────
    def _do_fade(self):
        target = 0.92 if self._hovering else 0.0
        diff = target - self._opacity
        step = 0.08
        if abs(diff) < step:
            self._opacity = target
            self._fade_timer.stop()
            if self._opacity <= 0.01:
                self._dock_hidden = True
        else:
            self._opacity += step if diff > 0 else -step
        self.setWindowOpacity(self._opacity)
        self.update()

    def _check_mouse_hover(self):
        cursor_pos = QCursor.pos()
        screen = QApplication.primaryScreen()
        if not screen:
            return
        sg = screen.availableGeometry()
        g = self.geometry()
        in_trigger = (cursor_pos.y() >= sg.bottom() - self._trigger_zone and
                      abs(cursor_pos.x() - g.center().x()) < self._dock_w // 2 + 40)
        in_dock = (g.left() - 20 <= cursor_pos.x() <= g.right() + 20 and
                   g.top() - 10 <= cursor_pos.y() <= g.bottom() + 10)
        if self._dock_hidden:
            if in_trigger or in_dock:
                self._start_fade(True)
        elif not in_dock and not in_trigger:
            self._start_fade(False)

    def _start_fade(self, show):
        self._hovering = show
        if show and self._dock_hidden:
            self.show()
            self._dock_hidden = False
            QTimer.singleShot(0, self._apply_win32_style)
        self._fade_timer.start()

    def enterEvent(self, event):
        if not self._dock_hidden:
            self._start_fade(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_icon = -1
        self._start_fade(False)
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_win32_style)
        QTimer.singleShot(100, self._apply_win32_style)

    def nativeEvent(self, eventType, message):
        try:
            if eventType == b"windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                if msg.message == 0x0001:  # WM_CREATE
                    QTimer.singleShot(0, self._apply_win32_style)
                elif msg.message == 0x0018:  # WM_SHOWWINDOW
                    QTimer.singleShot(50, self._apply_win32_style)
                elif msg.message in (0x0086, 0x0006, 0x0007):  # NCACTIVATE/ACTIVATE/SETFOCUS
                    return True, 0
                elif msg.message == 0x0021:  # WM_MOUSEACTIVATE
                    return True, 3
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def _apply_win32_style(self):
        try:
            hwnd = _safe_hwnd(self.winId())
            if not hwnd:
                return
            ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            new_ex = (ex_style
                      | WS_EX_NOACTIVATE
                      | WS_EX_NOPARENTNOTIFY
                      | WS_EX_TOPMOST
                      | WS_EX_LAYERED
                      | WS_EX_APPWINDOW)
            new_ex &= ~WS_EX_TOOLWINDOW
            user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)
            style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
            user32.SetWindowLongW(
                wintypes.HWND(hwnd), GWL_STYLE,
                style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), 33,
                    ctypes.byref(ctypes.c_int(2)),  # DWMWCP_ROUND
                    ctypes.sizeof(ctypes.c_int))
            except Exception:
                pass
            user32.SetWindowPos(
                wintypes.HWND(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
        except Exception as e:
            print(f"[UUDock] Style error: {e}", flush=True)

    # ── 图标绘制（与 AR Dock 同款） ────────────────────────────────────────
    def _draw_settings_icon(self, painter, x, y, size):
        color = QColor(220, 220, 230)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        cx, cy = x + size // 2, y + size // 2
        outer, inner, tooth = size * 0.38, size * 0.22, size * 0.08
        path = QPainterPath()
        for i in range(8):
            angle = i * math.pi / 4
            ox = cx + int(outer * math.cos(angle))
            oy = cy + int(outer * math.sin(angle))
            if i == 0:
                path.moveTo(ox, oy)
            else:
                path.lineTo(ox, oy)
            a1 = angle + math.pi / 8
            a2 = angle + math.pi / 4 - math.pi / 8
            path.lineTo(cx + int(outer * math.cos(a1)), cy + int(outer * math.sin(a1)))
            path.lineTo(cx + int((outer + tooth * 0.5) * math.cos((a1 + a2) / 2)),
                        cy + int((outer + tooth * 0.5) * math.sin((a1 + a2) / 2)))
            path.lineTo(cx + int(outer * math.cos(a2)), cy + int(outer * math.sin(a2)))
        path.closeSubpath()
        painter.drawPath(path)
        painter.setBrush(QColor(20, 20, 30))
        painter.drawEllipse(QPoint(cx, cy), int(inner), int(inner))

    def _draw_desktop_icon(self, painter, x, y, size):
        color = QColor(220, 220, 230)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        monitor_w, monitor_h = size * 0.72, size * 0.5
        bezel = size * 0.05
        mx = x + (size - monitor_w) / 2
        my = y + size * 0.14
        painter.drawRoundedRect(int(mx), int(my), int(monitor_w), int(monitor_h), 4, 4)
        painter.setBrush(QColor(20, 20, 30))
        painter.drawRoundedRect(int(mx + bezel), int(my + bezel),
                                int(monitor_w - 2 * bezel), int(monitor_h - 2 * bezel), 2, 2)
        painter.setBrush(color)
        stand_w, stand_h = size * 0.24, size * 0.08
        painter.drawRect(int(x + size / 2 - stand_w / 2),
                         int(my + monitor_h), int(stand_w), int(stand_h))
        base_w, base_h = size * 0.5, size * 0.06
        painter.drawRoundedRect(int(x + size / 2 - base_w / 2),
                                int(my + monitor_h + stand_h - 2),
                                int(base_w), int(base_h), 2, 2)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.setBrush(QColor(40, 40, 50, 220))
            radius = 14
            p.drawRoundedRect(QRect(0, 0, w, h).adjusted(1, 1, -1, -1), radius, radius)
            hl_rect = QRectF(4, 2, w - 8, radius * 0.7)
            path = QPainterPath()
            path.addRoundedRect(hl_rect, radius - 4, radius - 4)
            p.fillPath(path, QColor(255, 255, 255, 25))
            p.setPen(Qt.NoPen)
            for i in range(2):
                ix, iy, isize, _ = self._get_icon_rect(i)
                if self._hover_icon == i:
                    p.setBrush(QColor(255, 255, 255, 30))
                    p.drawEllipse(QPoint(ix + isize // 2, iy + isize // 2),
                                  isize // 2 + 6, isize // 2 + 6)
                if i == 0:
                    self._draw_settings_icon(p, ix, iy, isize)
                else:
                    self._draw_desktop_icon(p, ix, iy, isize)
        finally:
            p.end()

    # ── 鼠标事件 ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed_icon = self._icon_at(event.position().toPoint())
            if self._pressed_icon >= 0:
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            icon_idx = self._icon_at(event.position().toPoint())
            pressed = self._pressed_icon
            self._pressed_icon = -1
            if icon_idx == 1 and pressed == 1:
                # 电脑：切回手机控制桌面
                self._switch_to_phone_desktop()
            # index 0 设置按钮：占位，无功能
            self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        old_hover = self._hover_icon
        self._hover_icon = self._icon_at(pos)
        if self._hover_icon != old_hover:
            self.update()
        self.setCursor(Qt.PointingHandCursor if self._hover_icon >= 0 else Qt.ArrowCursor)
        super().mouseMoveEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    dock = UUDockWidget()
    # 启动后定期保持 topmost（与 AR Dock 的 3s 定时器策略一致）
    keep_top = QTimer()
    keep_top.timeout.connect(dock._apply_win32_style)
    keep_top.start(3000)
    print(f"[UUDock] 已启动，目标手机控制桌面=#{_server_desktop_index()}（隐藏中，鼠标移到屏幕底部唤出）",
          flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
