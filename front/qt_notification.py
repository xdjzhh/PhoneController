"""
Qt Notification Panel - macOS-style notification center rendered natively on PC.

Reads Android notifications via `adb shell dumpsys notification --noredact` and
displays them in a macOS Big Sur+ style notification center card window.

Design (pure Qt, minimal Win32):
- Runs inside the same QApplication process as the Qt backdrop window
- Two modes: EDIT (shows traffic lights, draggable, resizable) and PINNED (no chrome)
- Right-click in PINNED mode re-enters EDIT mode (matches weather card behavior)
- Frameless window with Mac-style traffic lights
- Frosted-glass background via DWM acrylic blur (Win32 only for window attributes)
- Scrollable list of glass-effect notification cards
- Auto-refreshes notifications when data is pushed from main process
- Topmost, no-activate, toolwindow (same Z-tier as scrcpy cards)
"""

import os
import re
import sys
import json
import time
import ctypes
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

if os.name == 'nt':
    user32 = ctypes.windll.user32

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CLIPSIBLINGS = 0x04000000
    WS_CLIPCHILDREN = 0x02000000
    WS_DISABLED = 0x08000000
    WS_POPUP = 0x80000000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_NOPARENTNOTIFY = 0x00000004
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_LAYERED = 0x00080000
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_NOSENDCHANGING = 0x0400
    SWP_NOOWNERZORDER = 0x0200
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2

    class ACCENT_POLICY(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_uint),
            ("AccentFlags", ctypes.c_uint),
            ("GradientColor", ctypes.c_uint),
            ("AnimationId", ctypes.c_uint),
        ]

    class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_uint),
            ("Data", ctypes.POINTER(ACCENT_POLICY)),
            ("SizeOfData", ctypes.c_size_t),
        ]

    ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
    WCA_ACCENT_POLICY = 19


def _apply_acrylic_blur(hwnd, color=0x00000000):
    """Apply DWM acrylic blur behind window. Minimal Win32 - just sets window attribute."""
    if os.name != 'nt' or not hwnd:
        return False
    try:
        accent = ACCENT_POLICY()
        accent.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 2
        accent.GradientColor = color
        accent.AnimationId = 0
        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.pointer(accent)
        data.SizeOfData = ctypes.sizeof(accent)
        return bool(user32.SetWindowCompositionAttribute(
            wintypes.HWND(hwnd), ctypes.byref(data)
        ))
    except Exception:
        return False


def _safe_hwnd(h):
    if h is None:
        return 0
    if isinstance(h, int):
        return h
    if hasattr(h, 'value'):
        v = h.value
        if isinstance(v, int):
            return v
        if isinstance(v, bytes):
            return int.from_bytes(v, byteorder='little', signed=False)
        return int(v) if v else 0
    try:
        return int(h)
    except (TypeError, ValueError):
        try:
            return ctypes.cast(ctypes.pointer(h), ctypes.POINTER(ctypes.c_void_p)).contents.value or 0
        except Exception:
            return 0


@dataclass
class NotificationItem:
    key: str = ""
    package: str = ""
    app_name: str = ""
    title: str = ""
    text: str = ""
    sub_text: str = ""
    post_time: int = 0
    icon_color: str = ""

    _app_colors = {
        'com.tencent.mm': "#07C160",
        'com.ss.android.ugc.aweme': "#FE2C55",
        'com.huawei.android.totemweather': "#4A90D9",
        'com.autonavi.minimap': "#00BFFF",
        'com.baidu.BaiduMap': "#3388FF",
        'com.eg.android.AlipayGphone': "#1677FF",
        'com.android.settings': "#8E8E93",
        'com.android.systemui': "#5C87F5",
        'com.android.mms': "#34C759",
        'com.android.phone': "#34C759",
        'com.android.dialer': "#34C759",
        'com.google.android.gm': "#EA4335",
        'com.google.android.apps.maps': "#4285F4",
        'com.huawei.android.hwouc': "#E74C3C",
        'com.huawei.systemmanager': "#E67E22",
        'com.taobao.idlefish': "#FF6B35",
        'com.unionpay': "#E21B1B",
        'com.tencent.androidqqmail': "#12B7F5",
        'ctrip.android.view': "#2679E0",
        'com.baidu.netdisk': "#2932E1",
        'com.tencent.mobileqq': "#12B7F5",
        'com.sina.weibo': "#E6162D",
        'com.taobao.taobao': "#FF5000",
        'com.jingdong.app.mall': "#E1251B",
        'com.zhihu.android': "#0084FF",
        'com.xingin.xhs': "#FE2C55",
        'com.netease.cloudmusic': "#D33A31",
        'com.tencent.qqmusic': "#31C27C",
    }

    _app_names = {
        'com.tencent.mm': "微信",
        'com.ss.android.ugc.aweme': "抖音",
        'com.huawei.android.totemweather': "天气",
        'com.autonavi.minimap': "高德地图",
        'com.baidu.BaiduMap': "百度地图",
        'com.eg.android.AlipayGphone': "支付宝",
        'com.android.settings': "设置",
        'com.android.systemui': "系统",
        'com.android.mms': "信息",
        'com.android.phone': "电话",
        'com.android.dialer': "电话",
        'com.google.android.gm': "Gmail",
        'com.google.android.apps.maps': "地图",
        'com.huawei.android.hwouc': "系统更新",
        'com.huawei.systemmanager': "手机管家",
        'com.taobao.idlefish': "闲鱼",
        'com.unionpay': "云闪付",
        'com.tencent.androidqqmail': "QQ邮箱",
        'ctrip.android.view': "携程",
        'com.baidu.netdisk': "百度网盘",
        'com.tencent.mobileqq': "QQ",
        'com.sina.weibo': "微博",
        'com.taobao.taobao': "淘宝",
        'com.jingdong.app.mall': "京东",
        'com.zhihu.android': "知乎",
        'com.xingin.xhs': "小红书",
        'com.netease.cloudmusic': "网易云音乐",
        'com.tencent.qqmusic': "QQ音乐",
    }

    def __post_init__(self):
        if not self.icon_color:
            self.icon_color = self._app_colors.get(self.package, "#5C87F5")
        if not self.app_name:
            parts = self.package.split('.')
            self.app_name = self._app_names.get(
                self.package,
                parts[-1].capitalize() if parts else self.package
            )

    def to_dict(self):
        return {
            'key': self.key,
            'package': self.package,
            'title': self.title,
            'text': self.text,
            'sub_text': self.sub_text,
            'post_time': self.post_time,
            'app_name': self.app_name,
            'icon_color': self.icon_color,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            key=d.get('key', ''),
            package=d.get('package', ''),
            title=d.get('title', ''),
            text=d.get('text', ''),
            sub_text=d.get('sub_text', ''),
            post_time=d.get('post_time', 0),
            app_name=d.get('app_name', ''),
            icon_color=d.get('icon_color', ''),
        )

    @property
    def time_str(self):
        if self.post_time <= 0:
            return ""
        now_ms = int(time.time() * 1000)
        diff = now_ms - self.post_time
        if diff < 0:
            return ""
        seconds = diff // 1000
        if seconds < 60:
            return "刚刚"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}小时前"
        days = hours // 24
        return f"{days}天前"


class NotificationParser:
    """Parse Android notifications from `adb shell dumpsys notification --noredact`."""

    def __init__(self, device_serial=None, adb_path="adb"):
        self.device_serial = device_serial
        self.adb_path = adb_path

    def _adb_cmd(self, *args):
        cmd = [self.adb_path]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += list(args)
        return cmd

    def fetch_notifications(self) -> List[NotificationItem]:
        try:
            cmd = self._adb_cmd("shell", "dumpsys", "notification", "--noredact")
            result = subprocess.run(
                cmd, capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            output = result.stdout.decode('utf-8', errors='replace')
            return self._parse_output(output)
        except Exception:
            return []

    def _parse_output(self, output: str) -> List[NotificationItem]:
        notifications = []
        current = None
        seen_keys = set()

        lines = output.split('\n')
        for line in lines:
            stripped = line.strip()

            if 'NotificationRecord(' in stripped:
                if current and current.package and (current.title or current.text):
                    key_id = current.key or f"{current.package}:{current.title}"
                    if key_id not in seen_keys:
                        seen_keys.add(key_id)
                        notifications.append(current)

                pkg_match = re.search(r'pkg=([^\s,)}]+)', stripped)
                key_match = re.search(r'key=([0-9a-fA-F-]+\|[^|\s)]+\|[^|\s)]+\|[^|\s)]*\|[0-9]+(?:\|[^\s)]*)?)', stripped)
                if pkg_match:
                    current = NotificationItem(
                        key=key_match.group(1) if key_match else "",
                        package=pkg_match.group(1),
                    )
                else:
                    current = None
                continue

            if current is None:
                continue

            title_match = re.search(r'android\.title=\w*String\s*\(([^)]*)\)', stripped)
            if title_match and not current.title:
                current.title = self._clean(title_match.group(1))

            text_match = re.search(r'android\.text=\w*String\s*\(([^)]*)\)', stripped)
            if text_match and not current.text:
                current.text = self._clean(text_match.group(1))

            sub_match = re.search(r'android\.subText=\w*String\s*\(([^)]*)\)', stripped)
            if sub_match and not current.sub_text:
                current.sub_text = self._clean(sub_match.group(1))

            time_match = re.search(r'\bwhen=(\d+)', stripped)
            if time_match and current.post_time == 0:
                try:
                    ts = int(time_match.group(1))
                    if ts > 0:
                        current.post_time = ts
                except ValueError:
                    pass

        if current and current.package and (current.title or current.text):
            key_id = current.key or f"{current.package}:{current.title}"
            if key_id not in seen_keys:
                notifications.append(current)

        _skip_packages = {'android', 'com.android.systemui'}
        notifications = [n for n in notifications if n.package not in _skip_packages]

        seen_content = set()
        deduped = []
        for n in notifications:
            content_key = f"{n.package}|{n.title}|{n.text}"
            if content_key not in seen_content:
                seen_content.add(content_key)
                deduped.append(n)
        notifications = deduped

        return notifications[:20]

    @staticmethod
    def _clean(s: str) -> str:
        return s.replace('\\n', ' ').replace('\\r', '').replace('\\"', '"').strip()


def create_notification_window(config=None, config_path=None):
    """
    Create and return the Qt notification panel window.
    Must be called within a running QApplication.
    """
    try:
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
            QFrame, QSizePolicy, QApplication
        )
        from PySide6.QtGui import (
            QPainter, QColor, QFont, QPen, QBrush, QPainterPath,
            QLinearGradient, QFontMetrics,
        )
        from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QRectF, Signal
    except ImportError:
        print("[QtNotification] PySide6 not available", flush=True)
        return None

    win_cfg = config or {}
    win_x = win_cfg.get('window_x', 1200)
    win_y = win_cfg.get('window_y', 100)
    win_w = win_cfg.get('window_width', 380)
    win_h = win_cfg.get('window_height', 680)
    default_opacity = win_cfg.get('default_opacity', 1.0)
    window_title = win_cfg.get('window_title', '通知')

    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'scrcpycontrol', 'display_config.json')

    CHROME_H = 36
    RESIZE_BORDER = 6
    HIT_ALPHA = 2

    PANEL_FILL = QColor(0, 0, 0, HIT_ALPHA)

    CARD_GLASS_BG = QColor(255, 255, 255, 36)
    CARD_GLASS_BG_HOVER = QColor(255, 255, 255, 54)
    CARD_GLASS_BORDER = QColor(255, 255, 255, 60)

    TEXT_PRIMARY = QColor(255, 255, 255)
    TEXT_SECONDARY = QColor(220, 220, 228)
    TEXT_TERTIARY = QColor(180, 180, 195)
    EMPTY_TEXT = QColor(255, 255, 255, 100)

    TRAFFIC_CLOSE = QColor(255, 95, 87)
    TRAFFIC_MIN = QColor(254, 188, 46)
    TRAFFIC_MAX = QColor(40, 200, 64)
    TRAFFIC_CLOSE_H = QColor(224, 74, 67)
    TRAFFIC_MIN_H = QColor(222, 161, 35)
    TRAFFIC_MAX_H = QColor(31, 171, 47)

    class NotifCardWidget(QWidget):
        def __init__(self, notif, parent=None):
            super().__init__(parent)
            self.notif = notif
            self._hover = False
            self.setFixedHeight(92)
            self.setMouseTracking(True)
            self.setAutoFillBackground(False)
            self.setAttribute(Qt.WA_TranslucentBackground, True)

        def enterEvent(self, event):
            self._hover = True
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event):
            self._hover = False
            self.update()
            super().leaveEvent(event)

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            r = self.rect().adjusted(5, 3, -5, -4)
            radius = 14

            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(r.translated(0, 2), radius, radius)
            p.fillPath(shadow_path, QColor(0, 0, 0, 30))

            glass_path = QPainterPath()
            glass_path.addRoundedRect(r, radius, radius)
            bg = CARD_GLASS_BG_HOVER if self._hover else CARD_GLASS_BG
            p.fillPath(glass_path, bg)

            hl_path = QPainterPath()
            hl_rect = QRectF(r.x() + 2, r.y() + 1.5, r.width() - 4, radius * 0.55)
            hl_path.addRoundedRect(hl_rect, radius - 2, radius - 2)
            p.setClipPath(glass_path)
            p.fillPath(hl_path, QColor(255, 255, 255, 45))
            p.setClipping(False)

            grad = QLinearGradient(r.x(), r.y(), r.x(), r.bottom())
            grad.setColorAt(0.0, QColor(255, 255, 255, 18))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(glass_path, grad)

            pen = QPen(CARD_GLASS_BORDER)
            pen.setWidth(1)
            p.setPen(pen)
            p.drawPath(glass_path)

            color = QColor(self.notif.icon_color)
            cx, cy, cr = 34, 46, 17
            p.setBrush(QBrush(QColor(0, 0, 0, 40)))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPoint(cx, cy + 1), cr, cr)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPoint(cx, cy), cr, cr)
            p.setPen(QColor(255, 255, 255))
            f = QFont()
            f.setPointSize(11)
            f.setBold(True)
            p.setFont(f)
            initial = (self.notif.app_name[0] if self.notif.app_name else "N")
            p.drawText(QRect(cx - cr, cy - cr, cr * 2, cr * 2), Qt.AlignCenter, initial)

            tx = 64
            content_x = tx
            content_w = self.width() - tx - 14

            app_f = QFont()
            app_f.setPointSize(8)
            p.setFont(app_f)
            p.setPen(TEXT_TERTIARY)
            p.drawText(content_x, 24, content_w - 48, 14, Qt.AlignLeft, self.notif.app_name)
            p.drawText(content_x, 24, content_w, 14, Qt.AlignRight, self.notif.time_str)

            cur_y = 40
            has_title = bool(self.notif.title)
            body_text = self.notif.text or self.notif.sub_text

            if has_title:
                tf = QFont()
                tf.setPointSize(10)
                tf.setBold(True)
                p.setFont(tf)
                p.setPen(TEXT_PRIMARY)
                p.drawText(content_x, cur_y, content_w, 18, Qt.AlignLeft,
                           self._elide(p, self.notif.title, tf, content_w))
                cur_y += 20

            if body_text:
                bf = QFont()
                bf.setPointSize(9)
                p.setFont(bf)
                p.setPen(TEXT_SECONDARY)
                body_h = self.height() - cur_y - 10
                p.drawText(content_x, cur_y, content_w, body_h, Qt.AlignLeft | Qt.TextWordWrap,
                           self._elide(p, body_text, bf, content_w))

            p.end()

        def _elide(self, painter, text, font, max_w):
            fm = QFontMetrics(font)
            return fm.elidedText(text, Qt.ElideRight, max_w)

    class NotificationPanelWindow(QWidget):
        closed_signal = Signal()

        NONE = 0
        DRAG = 1
        TOP = 2; BOTTOM = 3; LEFT = 4; RIGHT = 5
        TOPLEFT = 6; TOPRIGHT = 7; BOTTOMLEFT = 8; BOTTOMRIGHT = 9

        def __init__(self, cfg_path=None, def_opacity=1.0, win_title="通知"):
            super().__init__()
            self._config_path = cfg_path
            self._default_opacity = def_opacity
            self._win_title = win_title
            self._editing = True
            self._hover_btn = -1
            self._drag_mode = self.NONE
            self._drag_start = None
            self._drag_start_geom = None
            self._notifs = []
            self._card_widgets = []
            self._scrollbar_visible = False
            self._scrollbar_timer = QTimer(self)
            self._scrollbar_timer.setSingleShot(True)
            self._scrollbar_timer.timeout.connect(self._hide_scrollbar)
            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.timeout.connect(self._save_geometry)
            self._init_ui(win_x, win_y, win_w, win_h)

        def _init_ui(self, x, y, w, h):
            self.setWindowTitle(self._win_title)
            self.resize(w, h)
            self.move(x, y)
            self.setMinimumSize(280, 180)
            self.setWindowFlags(
                Qt.FramelessWindowHint
                | Qt.WindowDoesNotAcceptFocus
                | Qt.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAutoFillBackground(False)
            self.setMouseTracking(True)
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._on_context_menu)

            self._scroll = QScrollArea(self)
            self._scroll.setWidgetResizable(True)
            self._scroll.setFrameShape(QFrame.NoFrame)
            self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            vbar = self._scroll.verticalScrollBar()
            vbar.setObjectName("notifScrollBar")
            self._scroll.setStyleSheet("""
                QScrollArea { background: transparent; border: none; }
                QScrollArea > QWidget > QWidget { background: transparent; }
                QScrollBar#notifScrollBar:vertical {
                    background: transparent;
                    width: 8px;
                    margin: 4px 2px;
                }
                QScrollBar#notifScrollBar::handle:vertical {
                    background: rgba(255,255,255,0);
                    border-radius: 3px;
                    min-height: 24px;
                    transition: background 0.2s;
                }
                QScrollBar#notifScrollBar::handle:vertical:hover,
                QScrollBar#notifScrollBar[active="true"]::handle:vertical {
                    background: rgba(255,255,255,50);
                }
                QScrollBar#notifScrollBar::add-line:vertical,
                QScrollBar#notifScrollBar::sub-line:vertical { height: 0; }
                QScrollBar#notifScrollBar::add-page:vertical,
                QScrollBar#notifScrollBar::sub-page:vertical { background: transparent; }
            """)
            vbar.valueChanged.connect(self._on_scroll)
            vbar.sliderPressed.connect(self._on_scroll)
            self._scroll.setMouseTracking(True)
            self._scroll.viewport().setAutoFillBackground(False)
            self._scroll.viewport().setStyleSheet("background: transparent;")
            self._scroll.viewport().setMouseTracking(True)

            self._content = QWidget()
            self._content.setAutoFillBackground(False)
            self._content.setStyleSheet("background: transparent;")
            self._content.setMouseTracking(True)
            self._content_l = QVBoxLayout(self._content)
            self._content_l.setContentsMargins(12, 8, 12, 10)
            self._content_l.setSpacing(10)
            self._content_l.addStretch()
            self._scroll.setWidget(self._content)

            self._empty_label = QLabel("暂无通知", self)
            self._empty_label.setAlignment(Qt.AlignCenter)
            ef = QFont()
            ef.setPointSize(11)
            self._empty_label.setFont(ef)
            self._empty_label.setStyleSheet(f"color: rgba(255,255,255,{EMPTY_TEXT.alpha()}); background: transparent;")
            self._empty_label.hide()

        def _on_context_menu(self, pos):
            if not self._editing:
                self._set_edit_mode(True)

        def _set_edit_mode(self, editing):
            self._editing = editing
            self._hover_btn = -1
            self._drag_mode = self.NONE
            self.update()

        def _on_scroll(self, *args):
            vbar = self._scroll.verticalScrollBar()
            if vbar.maximum() <= 0:
                return
            if not self._scrollbar_visible:
                vbar.setProperty("active", True)
                vbar.style().unpolish(vbar)
                vbar.style().polish(vbar)
                self._scrollbar_visible = True
            self._scrollbar_timer.start(1200)

        def _hide_scrollbar(self):
            vbar = self._scroll.verticalScrollBar()
            vbar.setProperty("active", False)
            vbar.style().unpolish(vbar)
            vbar.style().polish(vbar)
            self._scrollbar_visible = False

        def _save_geometry(self):
            if not self._config_path or not os.path.exists(self._config_path):
                return
            try:
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                g = self.geometry()
                if 'notification_panel' not in cfg:
                    cfg['notification_panel'] = {}
                cfg['notification_panel']['window_x'] = g.x()
                cfg['notification_panel']['window_y'] = g.y()
                cfg['notification_panel']['window_width'] = g.width()
                cfg['notification_panel']['window_height'] = g.height()
                with open(self._config_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[Notification] save geometry failed: {e}", flush=True)

        def showEvent(self, event):
            super().showEvent(event)
            QTimer.singleShot(0, self._apply_style)
            QTimer.singleShot(100, self._apply_style)
            if self._default_opacity < 1.0:
                QTimer.singleShot(200, lambda: self.setWindowOpacity(self._default_opacity))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            w, h = self.width(), self.height()
            if self._editing:
                scroll_y = CHROME_H
                scroll_h = h - CHROME_H
            else:
                scroll_y = 0
                scroll_h = h
            if hasattr(self, '_scroll'):
                self._scroll.setGeometry(0, scroll_y, w, scroll_h)
            if hasattr(self, '_empty_label'):
                self._empty_label.setGeometry(0, scroll_y, w, scroll_h)

        def leaveEvent(self, event):
            if self._hover_btn != -1:
                self._hover_btn = -1
                self.update()
            self.setCursor(Qt.ArrowCursor)
            super().leaveEvent(event)

        def _apply_style(self):
            """Minimal Win32 style: topmost, noactivate, toolwindow, acrylic blur.
            Does NOT intercept any mouse/keyboard messages - Qt handles all events.
            """
            if os.name != 'nt':
                return
            try:
                hwnd = _safe_hwnd(self.winId())
                if not hwnd:
                    return
                ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
                new_ex = ex_style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_NOPARENTNOTIFY | WS_EX_TOPMOST | WS_EX_LAYERED
                new_ex &= ~WS_EX_APPWINDOW
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)

                style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
                new_style = style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
                new_style &= ~(WS_DISABLED | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, new_style)

                _apply_acrylic_blur(hwnd, color=0x00000000)

                user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOSENDCHANGING | SWP_NOOWNERZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                )
            except Exception as e:
                print(f"[QtNotification] Style error: {e}", flush=True)

        def ensure_topmost(self):
            self._apply_style()

        def nativeEvent(self, eventType, message):
            """Only handle window creation/style refresh messages.
            Do NOT intercept mouse/keyboard events - let Qt process them normally.
            This is the KEY fix: returning super() lets Qt handle close/minimize properly.
            """
            try:
                if os.name == 'nt' and eventType == b"windows_generic_MSG":
                    msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                    WM_CREATE = 0x0001
                    WM_SHOWWINDOW = 0x0018
                    if msg.message == WM_CREATE:
                        QTimer.singleShot(0, self._apply_style)
                    elif msg.message == WM_SHOWWINDOW:
                        QTimer.singleShot(50, self._apply_style)
            except Exception:
                pass
            return super().nativeEvent(eventType, message)

        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()

            p.fillRect(self.rect(), PANEL_FILL)

            if self._editing:
                for i in range(3):
                    colors = [TRAFFIC_CLOSE, TRAFFIC_MIN, TRAFFIC_MAX]
                    h_colors = [TRAFFIC_CLOSE_H, TRAFFIC_MIN_H, TRAFFIC_MAX_H]
                    c = h_colors[i] if self._hover_btn == i else colors[i]
                    bx = 14 + i * 20
                    by = (CHROME_H - 12) // 2
                    p.setBrush(QBrush(QColor(0, 0, 0, 60)))
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(QPoint(bx + 6, by + 7), 7, 7)
                    p.setBrush(QBrush(c))
                    p.drawEllipse(QPoint(bx + 6, by + 6), 6, 6)
                    if self._hover_btn == i:
                        glyphs = ["×", "−", "+"]
                        g = glyphs[i]
                        p.setPen(QColor(50, 40, 40))
                        gf = QFont()
                        gf.setPointSize(7)
                        gf.setBold(True)
                        p.setFont(gf)
                        p.drawText(QRect(bx, by - 1, 12, 14), Qt.AlignCenter, g)

                if self._notifs:
                    cf = QFont()
                    cf.setPointSize(9)
                    p.setFont(cf)
                    p.setPen(TEXT_TERTIARY)
                    p.drawText(QRect(10, 0, w - 20, CHROME_H), Qt.AlignVCenter | Qt.AlignRight, f"{len(self._notifs)} 条通知")

            p.end()

        def _traffic_light_at(self, pos):
            if not self._editing:
                return -1
            for i in range(3):
                bx = 14 + i * 20
                by = (CHROME_H - 12) // 2
                if bx - 2 <= pos.x() <= bx + 14 and by - 2 <= pos.y() <= by + 14:
                    return i
            return -1

        def _in_chrome_area(self, pos):
            return self._editing and pos.y() < CHROME_H

        def _edge_at(self, pos):
            if not self._editing:
                return self.NONE
            x, y = pos.x(), pos.y()
            w, h = self.width(), self.height()
            bw = RESIZE_BORDER
            on_top = y < bw
            on_bottom = y > h - bw
            on_left = x < bw
            on_right = x > w - bw
            if on_top and on_left:     return self.TOPLEFT
            if on_top and on_right:    return self.TOPRIGHT
            if on_bottom and on_left:  return self.BOTTOMLEFT
            if on_bottom and on_right: return self.BOTTOMRIGHT
            if on_top:                 return self.TOP
            if on_bottom:              return self.BOTTOM
            if on_left:                return self.LEFT
            if on_right:               return self.RIGHT
            return self.NONE

        _CURSOR_MAP = {
            0: Qt.ArrowCursor,
            2: Qt.SizeVerCursor, 3: Qt.SizeVerCursor,
            4: Qt.SizeHorCursor, 5: Qt.SizeHorCursor,
            6: Qt.SizeFDiagCursor, 9: Qt.SizeFDiagCursor,
            7: Qt.SizeBDiagCursor, 8: Qt.SizeBDiagCursor,
        }

        def mousePressEvent(self, event):
            if event.button() != Qt.LeftButton:
                super().mousePressEvent(event)
                return
            pos = event.position().toPoint()
            gpos = event.globalPosition().toPoint()

            btn = self._traffic_light_at(pos)
            if btn >= 0:
                self._drag_mode = self.NONE
                if btn == 0:
                    self.hide()
                    self.closed_signal.emit()
                elif btn == 1:
                    self.showMinimized()
                elif btn == 2:
                    self._set_edit_mode(False)
                    QTimer.singleShot(0, self._apply_style)
                event.accept()
                return

            edge = self._edge_at(pos)
            if edge != self.NONE:
                self._drag_mode = edge
                self._drag_start = gpos
                self._drag_start_geom = self.geometry()
                event.accept()
                return

            if self._in_chrome_area(pos):
                self._drag_mode = self.DRAG
                self._drag_start = gpos
                self._drag_start_geom = self.geometry()
                event.accept()
                return

            self._drag_mode = self.NONE
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            pos = event.position().toPoint()
            gpos = event.globalPosition().toPoint()

            if self._drag_mode == self.DRAG and (event.buttons() & Qt.LeftButton):
                if self._drag_start is not None:
                    delta = gpos - self._drag_start
                    self.move(self._drag_start_geom.topLeft() + delta)
                event.accept()
                return
            if self._drag_mode != self.NONE and (event.buttons() & Qt.LeftButton):
                self._do_resize(gpos)
                event.accept()
                return

            old_h = self._hover_btn
            self._hover_btn = self._traffic_light_at(pos)
            if self._hover_btn != old_h:
                self.update()

            if self._hover_btn >= 0:
                self.setCursor(Qt.PointingHandCursor)
            elif self._editing:
                edge = self._edge_at(pos)
                self.setCursor(self._CURSOR_MAP.get(edge, Qt.ArrowCursor))
            else:
                self.setCursor(Qt.ArrowCursor)
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            was_dragging = (self._drag_mode != self.NONE)
            self._drag_mode = self.NONE
            self._drag_start = None
            self._drag_start_geom = None
            if was_dragging:
                self._geometry_save_timer.start(300)
            super().mouseReleaseEvent(event)

        def _do_resize(self, gpos):
            if self._drag_start_geom is None:
                return
            g = self._drag_start_geom
            dx = gpos.x() - self._drag_start.x()
            dy = gpos.y() - self._drag_start.y()
            x, y, w, h = g.x(), g.y(), g.width(), g.height()
            min_w, min_h = self.minimumWidth(), self.minimumHeight()

            mode = self._drag_mode
            if mode in (self.LEFT, self.TOPLEFT, self.BOTTOMLEFT):
                new_w = max(min_w, w - dx)
                x = g.x() + (g.width() - new_w)
                w = new_w
            if mode in (self.RIGHT, self.TOPRIGHT, self.BOTTOMRIGHT):
                w = max(min_w, w + dx)
            if mode in (self.TOP, self.TOPLEFT, self.TOPRIGHT):
                new_h = max(min_h, h - dy)
                y = g.y() + (g.height() - new_h)
                h = new_h
            if mode in (self.BOTTOM, self.BOTTOMLEFT, self.BOTTOMRIGHT):
                h = max(min_h, h + dy)

            self.setGeometry(x, y, w, h)

        def set_notifications_data(self, notif_dicts):
            notifs = []
            for d in notif_dicts:
                try:
                    notifs.append(NotificationItem.from_dict(d))
                except Exception:
                    continue
            self._render_notifications(notifs)

        def _render_notifications(self, notifs):
            if len(notifs) == len(self._notifs):
                same = True
                for a, b in zip(notifs, self._notifs):
                    if a.key != b.key or a.title != b.title or a.text != b.text:
                        same = False
                        break
                if same:
                    return
            self._notifs = notifs
            while self._content_l.count() > 0:
                item = self._content_l.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
            self._card_widgets.clear()
            if not notifs:
                self._empty_label.show()
                self._scroll.hide()
                self._content_l.addStretch()
            else:
                self._empty_label.hide()
                self._scroll.show()
                for n in notifs:
                    cw = NotifCardWidget(n, self._content)
                    self._content_l.addWidget(cw)
                    self._card_widgets.append(cw)
                self._content_l.addStretch()
            self.update()

        def update_position(self, x, y):
            self.move(x, y)

    window = NotificationPanelWindow(cfg_path=config_path, def_opacity=default_opacity, win_title=window_title)
    return window
