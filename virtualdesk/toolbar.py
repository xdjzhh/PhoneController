# -*- coding: utf-8 -*-
"""
DesktopToolbar —— 全项目唯一的 Mac 风格底部 Dock 公用组件（Qt/PySide6）。

    ┌──────────┬────────┬────────┬────────┐
    │  设置    │  手机  │  电脑  │  键盘  │
    └──────────┴────────┴────────┴────────┘

本组件只负责「绘制 + 悬停/淡入交互 + Win32 窗口样式」，不持有任何业务知识：
  · 「设置」做什么 → on_settings 回调（None = 占位无功能，如 UU 桌面的回程 Dock）
  · 「手机」做什么 → on_phone 回调（在哪个桌面点，手机主屏就嵌入哪个桌面）
  · 「电脑」做什么 → on_computer 回调
  · 「键盘」做什么 → on_keyboard 回调（系统屏幕键盘 osk 开/关）

由 virtualdesk/toolbar_service.py 一个常驻进程在每个虚拟桌面各托管一个，
替代旧的 qt_backdrop 内嵌 Dock 与 front/uu_dock.py 第二实例。

样式硬约束：WS_EX_APPWINDOW + 清除 WS_EX_TOOLWINDOW，否则窗口不被
Virtual Desktop Manager 注册，会在所有虚拟桌面漂浮。
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget


# ── 几何常量（virtual_desktop.py 的 osk 定位会引用，见该文件 _OSK_* 注释）────
# dock 贴工作区底边的边距；osk 打开时其上沿与 dock 顶边的最小间隔（逻辑像素）
DOCK_BOTTOM_MARGIN = 6
OSK_DOCK_GAP = 8


def declare_dpi_awareness() -> None:
    """物理像素坐标，避免 125%/150% 缩放下 Dock 位置/点击偏移。

    常驻服务在创建 QApplication 前调用；重复调用（宿主进程已声明）抛异常，安全忽略。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ── Win32 常量 ────────────────────────────────────────────────────────────
_user32 = ctypes.windll.user32
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


# ── 「手机」按钮：把指定窗口嵌入当前所在虚拟桌面 ───────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_DISPLAY_CONFIG = os.path.join(_PROJECT_ROOT, "scrcpycontrol", "display_config.json")


def find_top_window_by_title(title: str) -> int:
    """按精确标题找「可见 + 真正顶层」窗口 HWND；找不到返回 0。"""
    result = wintypes.HWND(0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                result.value = hwnd
                return False  # 停止枚举
        return True

    _user32.EnumWindows(_cb, 0)
    return int(result.value or 0)


def bring_window_to_current_desktop(window_title: str) -> tuple[bool, str]:
    """把指定标题的顶层窗口迁移到【调用时当前所在】的虚拟桌面。

    不抢焦点、不切换桌面；窗口已在当前桌面时为 no-op。
    返回 (ok, message)。
    """
    hwnd = find_top_window_by_title(window_title)
    if not hwnd:
        return False, f"未找到标题为 {window_title!r} 的可见窗口（手机主屏是否已启动？）"
    try:
        from pyvda import AppView, VirtualDesktop, get_virtual_desktops
    except Exception as e:
        return False, f"pyvda 不可用: {e!r}"
    try:
        target = VirtualDesktop.current()
        desktops = get_virtual_desktops()
        cur_idx = next((i for i, d in enumerate(desktops) if d.id == target.id), -1)
        try:
            if AppView(hwnd=hwnd).desktop.id == target.id:
                return True, f"已在当前桌面 #{cur_idx}"
        except Exception:
            pass
        # 主链路 AppView.move；老版本 pyvda 回退 VirtualDesktop.move(hwnd)
        try:
            AppView(hwnd=hwnd).move(target)
        except Exception:
            target.move(hwnd)
        try:
            moved_ok = AppView(hwnd=hwnd).desktop.id == target.id
        except Exception:
            moved_ok = True  # 校验接口异常时不阻断（move 未抛即视为成功）
        if moved_ok:
            return True, f"已嵌入当前桌面 #{cur_idx}"
        return False, "move 已调用但校验窗口仍不在当前桌面"
    except Exception as e:
        return False, f"迁移失败: {e!r}"


def main_display_window_title() -> str:
    """main_display 窗口标题（单一真相源 display_config.json，回退「手机桌面」）。"""
    try:
        with open(_MAIN_DISPLAY_CONFIG, "r", encoding="utf-8") as f:
            title = json.load(f).get("main_display", {}).get("window_title")
            if title:
                return str(title)
    except Exception:
        pass
    return "手机桌面"


def bring_main_display_to_current_desktop() -> None:
    """「手机」按钮统一动作：在哪个虚拟桌面点击，就把手机主屏嵌到哪个桌面。"""
    ok, msg = bring_window_to_current_desktop(main_display_window_title())
    print(f"[DesktopToolbar] 手机主屏嵌入：{'OK' if ok else '失败'}，{msg}", flush=True)


class DesktopToolbar(QWidget):
    """Mac 风格底部 Dock：屏幕底部悬停淡入，四个图标（设置 / 手机 / 电脑 / 键盘）。

    参数
    ----
    window_title : 窗口标题（同时用于虚拟桌面识别与日志前缀区分）
    on_settings  : 点击「设置」回调；None 表示占位按钮（无功能）
    on_phone     : 点击「手机」回调（一般传 bring_main_display_to_current_desktop）
    on_computer  : 点击「电脑」回调（通常为切换虚拟桌面）
    on_keyboard  : 点击「键盘」回调（系统 osk 开/关切换）
    """

    # 图标顺序：0=设置 1=手机 2=电脑 3=键盘
    IDX_SETTINGS = 0
    IDX_PHONE = 1
    IDX_COMPUTER = 2
    IDX_KEYBOARD = 3
    ICON_COUNT = 4

    def __init__(self, window_title: str = "AR Desktop Dock",
                 on_settings=None, on_phone=None, on_computer=None,
                 on_keyboard=None, parent=None):
        super().__init__(parent)
        self._log_tag = window_title
        self._on_settings = on_settings
        self._on_phone = on_phone
        self._on_computer = on_computer
        self._on_keyboard = on_keyboard
        self._hovering = False
        self._hover_icon = -1
        self._pressed_icon = -1
        self._settings_active = False   # 「设置」激活态高亮（如编辑模式已开启）
        self._keyboard_active = False   # 「键盘」激活态高亮（osk 已打开）
        self._pinned = False            # 钉住：osk 打开期间强制显示、不淡出
        self._icon_size = 48
        self._icon_spacing = 16
        self._h_padding = 16
        self._v_padding = 10
        self._dock_hidden = True
        self._opacity = 0.0
        self._trigger_zone = 4
        self._dock_w = 0
        self._dock_h = 0
        self._init_ui(window_title)

    # ── 对外接口 ──────────────────────────────────────────────────────────
    def set_settings_active(self, active: bool) -> None:
        """由宿主在「设置」所触发的状态变化时调用（如编辑模式开关）。"""
        self._settings_active = bool(active)
        self.update()

    def is_settings_active(self) -> bool:
        return self._settings_active

    def set_keyboard_active(self, active: bool) -> None:
        """由宿主在系统 osk 开/关时调用，控制键盘图标激活高亮。"""
        self._keyboard_active = bool(active)
        self.update()

    def set_pinned(self, pinned: bool) -> None:
        """钉住显示（osk 打开期间）：强制淡入并禁止悬停淡出；解除后恢复触发带机制。"""
        pinned = bool(pinned)
        if pinned == self._pinned:
            return
        self._pinned = pinned
        if pinned:
            self._start_fade(True)
        else:
            self._hovering = False
            self._hover_icon = -1
            # 鼠标不在触发带/dock 上时立即开始淡出；在则保持悬停显示
            self._check_mouse_hover()

    def _init_ui(self, title: str):
        self.setWindowTitle(title)
        self._dock_w = self.ICON_COUNT * self._icon_size \
            + (self.ICON_COUNT - 1) * self._icon_spacing + 2 * self._h_padding
        self._dock_h = self._icon_size + 2 * self._v_padding
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            x = sg.center().x() - self._dock_w // 2
            y = sg.bottom() - self._dock_h - DOCK_BOTTOM_MARGIN
            self.move(x, y)
        self.resize(self._dock_w, self._dock_h)
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
        start_x = self._h_padding
        x = start_x + index * (self._icon_size + self._icon_spacing)
        y = self._v_padding
        scale = 1.0
        if self._hover_icon == index:
            scale = 1.25
        if self._pressed_icon == index:
            scale = 1.1
        size = int(self._icon_size * scale)
        offset = (self._icon_size - size) // 2
        return x + offset, y + offset, size, size

    def _icon_at(self, pos):
        for i in range(self.ICON_COUNT):
            ix, iy, isize, _ = self._get_icon_rect(i)
            if ix <= pos.x() <= ix + isize and iy <= pos.y() <= iy + isize:
                return i
        return -1

    # ── 按钮动作（回调注入） ──────────────────────────────────────────────
    def _activate_settings(self):
        if self._on_settings is None:
            return
        try:
            self._on_settings()
        except Exception as e:
            print(f"[{self._log_tag}] 设置动作异常: {e}", flush=True)

    def _activate_phone(self):
        if self._on_phone is None:
            return
        try:
            self._on_phone()
        except Exception as e:
            print(f"[{self._log_tag}] 手机动作异常: {e}", flush=True)

    def _activate_computer(self):
        if self._on_computer is None:
            return
        try:
            self._on_computer()
        except Exception as e:
            print(f"[{self._log_tag}] 电脑动作异常: {e}", flush=True)

    def _activate_keyboard(self):
        if self._on_keyboard is None:
            return
        try:
            self._on_keyboard()
        except Exception as e:
            print(f"[{self._log_tag}] 键盘动作异常: {e}", flush=True)

    # ── 悬停淡入淡出 ──────────────────────────────────────────────────────
    def _do_fade(self):
        target = 0.92 if (self._hovering or self._pinned) else 0.0
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
        # 钉住期间（osk 打开）不响应触发带淡出，保证「再点关闭」始终可点
        if self._pinned:
            self._hovering = True
            if self._dock_hidden:
                self._start_fade(True)
            return
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
        else:
            if not in_dock and not in_trigger:
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
        if not self._pinned:
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
                WM_CREATE = 0x0001
                WM_SHOWWINDOW = 0x0018
                WM_NCACTIVATE = 0x0086
                WM_MOUSEACTIVATE = 0x0021
                WM_ACTIVATE = 0x0006
                WM_SETFOCUS = 0x0007
                if msg.message == WM_CREATE:
                    QTimer.singleShot(0, self._apply_win32_style)
                elif msg.message == WM_SHOWWINDOW:
                    QTimer.singleShot(50, self._apply_win32_style)
                elif msg.message in (WM_NCACTIVATE, WM_ACTIVATE, WM_SETFOCUS):
                    return True, 0
                elif msg.message == WM_MOUSEACTIVATE:
                    return True, 3
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def _apply_win32_style(self):
        try:
            hwnd = _safe_hwnd(self.winId())
            if not hwnd:
                return
            ex_style = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            # 【虚拟桌面强隔离·硬约束】WS_EX_APPWINDOW 必须置位、
            # WS_EX_TOOLWINDOW 必须清除，否则 VDM 不感知此窗 → 全桌面漂浮。
            new_ex = (ex_style
                      | WS_EX_NOACTIVATE
                      | WS_EX_NOPARENTNOTIFY
                      | WS_EX_TOPMOST
                      | WS_EX_LAYERED
                      | WS_EX_APPWINDOW)
            new_ex &= ~WS_EX_TOOLWINDOW
            _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)
            style = _user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
            new_style = style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
            _user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, new_style)
            try:
                DWMWA_WINDOW_CORNER_PREFERENCE = 33
                DWMWCP_ROUND = 2
                corner_pref = ctypes.c_int(DWMWCP_ROUND)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
                    ctypes.byref(corner_pref),
                    ctypes.sizeof(corner_pref)
                )
            except Exception:
                pass
            _user32.SetWindowPos(
                wintypes.HWND(hwnd), HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
        except Exception as e:
            print(f"[{self._log_tag}] Style error: {e}", flush=True)

    # ── 图标绘制 ──────────────────────────────────────────────────────────
    def _draw_settings_icon(self, painter, x, y, size):
        color = QColor(220, 220, 230)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        cx = x + size // 2
        cy = y + size // 2
        outer = size * 0.38
        inner = size * 0.22
        tooth = size * 0.08
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
        monitor_w = size * 0.72
        monitor_h = size * 0.5
        bezel = size * 0.05
        mx = x + (size - monitor_w) / 2
        my = y + size * 0.14
        painter.drawRoundedRect(int(mx), int(my), int(monitor_w), int(monitor_h), 4, 4)
        painter.setBrush(QColor(20, 20, 30))
        painter.drawRoundedRect(int(mx + bezel), int(my + bezel),
                                int(monitor_w - 2 * bezel), int(monitor_h - 2 * bezel), 2, 2)
        painter.setBrush(color)
        stand_w = size * 0.24
        stand_h = size * 0.08
        painter.drawRect(int(x + size / 2 - stand_w / 2),
                         int(my + monitor_h), int(stand_w), int(stand_h))
        base_w = size * 0.5
        base_h = size * 0.06
        painter.drawRoundedRect(int(x + size / 2 - base_w / 2),
                                int(my + monitor_h + stand_h - 2),
                                int(base_w), int(base_h), 2, 2)

    def _draw_phone_icon(self, painter, x, y, size):
        """竖屏手机：机身圆角矩形 + 屏幕 + 顶部听筒槽。"""
        color = QColor(220, 220, 230)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        body_w = size * 0.46
        body_h = size * 0.78
        bx = x + (size - body_w) / 2
        by = y + (size - body_h) / 2
        radius = max(3, int(size * 0.09))
        painter.drawRoundedRect(int(bx), int(by), int(body_w), int(body_h),
                                radius, radius)
        # 屏幕（深色）
        bezel = size * 0.055
        painter.setBrush(QColor(20, 20, 30))
        painter.drawRoundedRect(int(bx + bezel), int(by + bezel + size * 0.06),
                                int(body_w - 2 * bezel),
                                int(body_h - 2 * bezel - size * 0.10),
                                2, 2)
        # 顶部听筒槽（机身色）
        painter.setBrush(color)
        slot_w = size * 0.16
        slot_h = max(2, int(size * 0.03))
        painter.drawRoundedRect(int(x + size / 2 - slot_w / 2),
                                int(by + size * 0.075),
                                int(slot_w), slot_h, slot_h, slot_h)

    def _draw_keyboard_icon(self, painter, x, y, size):
        """键盘：圆角机身 + 四排键帽。"""
        color = QColor(220, 220, 230)
        key_color = QColor(20, 20, 30)
        painter.setPen(Qt.NoPen)
        body_w = size * 0.86
        body_h = size * 0.66
        bx = x + (size - body_w) / 2
        by = y + (size - body_h) / 2
        painter.setBrush(color)
        painter.drawRoundedRect(int(bx), int(by), int(body_w), int(body_h),
                                max(3, int(size * 0.08)), max(3, int(size * 0.08)))
        # 键帽网格：4 列 x 3 行 + 底排长条
        painter.setBrush(key_color)
        pad = size * 0.08
        gap = size * 0.045
        key_w = (body_w - 2 * pad - 3 * gap) / 4
        rows_h = body_h * 0.52
        key_h = (rows_h - 2 * gap) / 3
        for r in range(3):
            for c in range(4):
                kx = bx + pad + c * (key_w + gap)
                ky = by + pad + r * (key_h + gap)
                painter.drawRoundedRect(int(kx), int(ky), int(key_w), int(key_h), 1, 1)
        # 底排：空格长条
        bar_y = by + pad + rows_h + gap
        bar_h = body_h - pad - (bar_y - by)
        painter.drawRoundedRect(int(bx + pad + key_w + gap), int(bar_y),
                                int(2 * key_w + gap), int(bar_h), 1, 1)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            bg_color = QColor(40, 40, 50, 220)
            border_color = QColor(255, 255, 255, 40)
            p.setPen(QPen(border_color, 1))
            p.setBrush(bg_color)
            radius = 14
            p.drawRoundedRect(QRect(0, 0, w, h).adjusted(1, 1, -1, -1), radius, radius)
            highlight = QColor(255, 255, 255, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(highlight)
            hl_rect = QRectF(4, 2, w - 8, radius * 0.7)
            path = QPainterPath()
            path.addRoundedRect(hl_rect, radius - 4, radius - 4)
            p.fillPath(path, highlight)
            for i in range(self.ICON_COUNT):
                ix, iy, isize, _ = self._get_icon_rect(i)
                is_hover = (self._hover_icon == i)
                is_active = ((i == self.IDX_SETTINGS and self._settings_active
                              and self._on_settings is not None)
                             or (i == self.IDX_KEYBOARD and self._keyboard_active))
                if is_hover or is_active:
                    glow_color = QColor(100, 180, 255, 50) if is_active else QColor(255, 255, 255, 30)
                    p.setBrush(glow_color)
                    p.drawEllipse(QPoint(ix + isize // 2, iy + isize // 2),
                                  isize // 2 + 6, isize // 2 + 6)
                if i == self.IDX_SETTINGS:
                    self._draw_settings_icon(p, ix, iy, isize)
                elif i == self.IDX_PHONE:
                    self._draw_phone_icon(p, ix, iy, isize)
                elif i == self.IDX_COMPUTER:
                    self._draw_desktop_icon(p, ix, iy, isize)
                else:
                    self._draw_keyboard_icon(p, ix, iy, isize)
        finally:
            p.end()

    # ── 鼠标事件 ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            icon_idx = self._icon_at(pos)
            self._pressed_icon = icon_idx
            if icon_idx >= 0:
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            icon_idx = self._icon_at(pos)
            pressed = self._pressed_icon
            self._pressed_icon = -1
            if icon_idx == self.IDX_SETTINGS and pressed == self.IDX_SETTINGS:
                self._activate_settings()
            elif icon_idx == self.IDX_PHONE and pressed == self.IDX_PHONE:
                self._activate_phone()
            elif icon_idx == self.IDX_COMPUTER and pressed == self.IDX_COMPUTER:
                self._activate_computer()
            elif icon_idx == self.IDX_KEYBOARD and pressed == self.IDX_KEYBOARD:
                self._activate_keyboard()
            self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        old_hover = self._hover_icon
        self._hover_icon = -1
        for i in range(self.ICON_COUNT):
            ix, iy, isize, _ = self._get_icon_rect(i)
            if ix <= pos.x() <= ix + isize and iy <= pos.y() <= iy + isize:
                self._hover_icon = i
                break
        if self._hover_icon != old_hover:
            self.update()
        if self._hover_icon >= 0:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        super().mouseMoveEvent(event)
