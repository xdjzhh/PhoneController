"""
Qt Thinking Log Panel - macOS-style card window for streaming agent thoughts /
background status into a scrollable, bounded log.

Design goals:
  - Same window chrome as QtNotificationWindow (frameless, acrylic blur,
    Mac-style traffic lights, draggable, resizable, topmost + no-activate).
  - **Bounded memory & CPU**: uses QPlainTextEdit.setMaximumBlockCount(N) so
    Qt itself discards old blocks once the cap is hit; we also batch UI updates
    via a coalescing timer so rapid appends don't hammer repaints.
  - **Thread-safe**: append_log() / clear_log() may be called from any thread;
    updates are marshaled to the Qt UI thread via Signal.

Usage (from within a running QApplication):
    from front.qt_thinking_log import create_thinking_log_panel
    win = create_thinking_log_panel(cfg_path="...", max_blocks=500)
    win.append_log("think step 1", step=1, source="LLM")
"""

import os
import json
import time
import ctypes
from collections import deque
from ctypes import wintypes
from typing import Optional, List, Any

if os.name == 'nt':
    user32 = ctypes.windll.user32

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CLIPSIBLINGS = 0x04000000
    WS_CLIPCHILDREN = 0x02000000
    WS_DISABLED = 0x08000000
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


# ─── Visual constants (same palette as qt_notification for consistency) ───

CHROME_H = 34
RESIZE_BORDER = 6
CARD_RADIUS = 16
PANEL_FILL_HEX = 0xCC1E1E22   # ~80% dark background (ARGB non-premul = little-endian 0x221E1ECC)
# Note: DWM accent expects AABBGGRR packed as uint. We want dark translucent:
#  A=0xCC(80%) R=0x1E G=0x1E B=0x22  →  0xCC 0x22 0x1E 0x1E  →  0xCC221E1E
PANEL_ACCENT_COLOR = 0xCC221E1E

TRAFFIC_CLOSE = 0xFF5F57
TRAFFIC_MIN = 0xFEBC2E
TRAFFIC_MAX = 0x28C840
TRAFFIC_CLOSE_H = 0xFF7A6F
TRAFFIC_MIN_H = 0xFFD056
TRAFFIC_MAX_H = 0x4ED964

TEXT_PRIMARY_HEX = "#F5F5F7"
TEXT_SECONDARY_HEX = "#C7C7CC"
TEXT_TERTIARY_HEX = "#8E8E93"
ACCENT_HEX = "#0A84FF"

DEFAULT_MAX_BLOCKS = 500
# Coalesce rapid appends into one UI flush every N ms to save CPU
FLUSH_INTERVAL_MS = 80


def create_thinking_log_panel(
    cfg_path: Optional[str] = None,
    def_opacity: float = 1.0,
    win_title: str = "思考步骤",
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    config: Optional[dict] = None,
):
    """Factory that builds a ThinkingLogWindow inside the current QApplication.

    Must be called from a thread with a running QApplication event loop.
    Returns None if PySide6 is not available.
    """
    try:
        from PySide6.QtWidgets import (
            QWidget, QPlainTextEdit, QApplication, QSizePolicy,
        )
        from PySide6.QtGui import (
            QPainter, QColor, QFont, QPen, QBrush, QPainterPath,
            QTextCursor,
        )
        from PySide6.QtCore import Qt, QTimer, QPoint, QRect, Signal, Slot
    except ImportError:
        print("[ThinkingLog] PySide6 not available", flush=True)
        return None

    win_cfg = config or {}
    if cfg_path and os.path.exists(cfg_path) and not config:
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                full = json.load(f)
            win_cfg = full.get('thinking_log', {}) or {}
        except Exception:
            win_cfg = {}

    # Default geometry: bottom right stack under notification panel
    screen = QApplication.primaryScreen()
    if screen is None:
        win_x, win_y, win_w, win_h = 300, 300, 460, 540
    else:
        geo = screen.availableGeometry()
        win_w = int(win_cfg.get("window_width", 460))
        win_h = int(win_cfg.get("window_height", 540))
        win_x = int(win_cfg.get("window_x", max(2, geo.width() - win_w - 12)))
        win_y = int(win_cfg.get("window_y", max(2, geo.height() - win_h - 80)))

    # Load optional opacity override from section
    section_opacity = win_cfg.get("default_opacity", def_opacity)
    try:
        section_opacity = float(section_opacity)
    except Exception:
        section_opacity = def_opacity
    if not 0.05 <= section_opacity <= 1.0:
        section_opacity = 1.0

    try:
        max_blocks_cfg = int(win_cfg.get("max_blocks", max_blocks))
        if max_blocks_cfg < 50:
            max_blocks_cfg = 50
        max_blocks = max_blocks_cfg
    except Exception:
        pass

    TEXT_PRIMARY = QColor(TEXT_PRIMARY_HEX)
    TEXT_SECONDARY = QColor(TEXT_SECONDARY_HEX)
    TEXT_TERTIARY = QColor(TEXT_TERTIARY_HEX)
    PANEL_FILL = QColor.fromRgba(PANEL_FILL_HEX)

    class ThinkingLogWindow(QWidget):
        closed_signal = Signal()
        # Internal marshal signal: payload is list[str] (already formatted lines)
        _flush_signal = Signal(list)
        _clear_signal = Signal()

        NONE = 0
        DRAG = 1
        TOP = 2; BOTTOM = 3; LEFT = 4; RIGHT = 5
        TOPLEFT = 6; TOPRIGHT = 7; BOTTOMLEFT = 8; BOTTOMRIGHT = 9

        # ───────────────────────────── init ─────────────────────────────
        def __init__(self):
            super().__init__()
            self._config_path = cfg_path
            self._default_opacity = section_opacity
            self._win_title = win_title
            self._max_blocks = max_blocks

            self._editing = False        # Default matches NotificationPanelWindow exactly:
                                         # pinned, chrome hidden — toggle edit via DockToolbar button
            self._hover_btn = -1
            self._drag_mode = self.NONE
            self._drag_start = None
            self._drag_start_geom = None

            # Pending lines not yet flushed to QPlainTextEdit (bounded CPU)
            self._pending: "deque[str]" = deque(maxlen=max(500, max_blocks))
            self._flush_timer = QTimer(self)
            self._flush_timer.setSingleShot(True)
            self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
            self._flush_timer.timeout.connect(self._on_flush_timer)
            self._flush_signal.connect(self._flush_lines_slot)
            self._clear_signal.connect(self._clear_slot)

            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.setInterval(1200)
            self._geometry_save_timer.timeout.connect(self._save_geometry)

            # Initialize geometry & sub-widgets BEFORE installing decorators so
            # that QApplication has a real window handle to style.
            self._init_ui(win_x, win_y, win_w, win_h)

        # ───────────────────────────── UI ─────────────────────────────
        def _init_ui(self, x, y, w, h):
            self.setWindowTitle(self._win_title)
            self.resize(w, h)
            self.move(x, y)
            self.setMinimumSize(320, 180)
            self.setWindowFlags(
                Qt.FramelessWindowHint
                | Qt.WindowDoesNotAcceptFocus
                | Qt.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAutoFillBackground(False)
            self.setMouseTracking(True)

            # ── Log view ──
            self._log_view = QPlainTextEdit(self)
            self._log_view.setReadOnly(True)
            self._log_view.setMouseTracking(True)
            self._log_view.setMaximumBlockCount(self._max_blocks)
            self._log_view.setWordWrapMode(
                __import__('PySide6.QtGui', fromlist=['QTextOption']).QTextOption.WordWrap
            )
            self._log_view.setCenterOnScroll(False)
            self._log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._log_view.setStyleSheet(f"""
                QPlainTextEdit {{
                    background: transparent;
                    border: none;
                    color: {TEXT_PRIMARY_HEX};
                    selection-background-color: {ACCENT_HEX};
                    font-family: "Consolas", "JetBrains Mono", "Microsoft YaHei UI", monospace;
                    font-size: 13px;
                    padding: 10px 14px 12px 14px;
                }}
                QScrollBar:vertical {{
                    background: transparent;
                    width: 8px;
                    margin: 4px 2px;
                }}
                QScrollBar::handle:vertical {{
                    background: rgba(255,255,255,40);
                    border-radius: 3px;
                    min-height: 24px;
                }}
                QScrollBar::handle:vertical:hover {{
                    background: rgba(255,255,255,70);
                }}
                QScrollBar::add-line:vertical,
                QScrollBar::sub-line:vertical {{ height: 0; }}
                QScrollBar::add-page:vertical,
                QScrollBar::sub-page:vertical {{ background: transparent; }}
                QScrollBar:horizontal {{
                    background: transparent;
                    height: 8px;
                    margin: 2px 4px;
                }}
                QScrollBar::handle:horizontal {{
                    background: rgba(255,255,255,40);
                    border-radius: 3px;
                    min-width: 24px;
                }}
            """)
            # Placeholder hint
            self._log_view.setPlaceholderText("等待程序思考步骤输入...")

        # ─────────────────── Thread-safe public API ───────────────────
        def append_log(self, message: Any, step: Optional[int] = None,
                       source: Optional[str] = None, tag: Optional[str] = None,
                       timestamp: bool = True) -> None:
            """Append a thinking line. Safe to call from any thread."""
            try:
                s = str(message)
            except Exception:
                s = repr(message)
            # Split by newline so each line has its own timestamp prefix
            for raw_line in s.splitlines() or [""]:
                parts = []
                if timestamp:
                    parts.append(time.strftime("%H:%M:%S"))
                if step is not None:
                    parts.append(f"Step {step}")
                if source:
                    parts.append(f"[{source}]")
                if tag:
                    parts.append(f"#{tag}")
                prefix = " ".join(parts)
                line = f"{prefix} › {raw_line}" if prefix else raw_line
                # bounded deque keeps memory from growing if UI thread is behind
                self._pending.append(line)
            # Kick the coalescing timer from whichever thread
            self._flush_timer.start()

        def clear_log(self) -> None:
            """Clear all visible + pending content. Safe from any thread."""
            self._pending.clear()
            self._clear_signal.emit()

        def lines_count(self) -> int:
            """Approximate total visible + pending lines (best effort)."""
            try:
                doc = self._log_view.document()
                return doc.blockCount() + len(self._pending)
            except Exception:
                return 0

        # ──────────────── Internal UI-side slot helpers ────────────────
        @Slot(list)
        def _flush_lines_slot(self, lines: List[str]):
            if not lines:
                return
            cursor = self._log_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText("\n".join(lines) + "\n")
            # Scroll to bottom so users see the latest thinking
            sb = self._log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

        @Slot()
        def _clear_slot(self):
            self._log_view.clear()

        def _on_flush_timer(self):
            if not self._pending:
                return
            # Atomically take everything from the deque
            lines: List[str] = []
            try:
                while True:
                    lines.append(self._pending.popleft())
            except IndexError:
                pass
            if lines:
                # Cross thread via signal even if we ARE already on UI thread - safer.
                self._flush_signal.emit(lines)

        # ───────────────── Persistence & Styling ─────────────────
        def _set_edit_mode(self, editing: bool):
            """Matches the exact API on NotificationPanelWindow; driven by DockToolbar toggle."""
            editing = bool(editing)
            if self._editing == editing:
                return
            self._editing = editing
            self._hover_btn = -1
            if not editing:
                self._save_geometry()
            self.resizeEvent(None)
            self.update()

        def _save_geometry(self):
            if not self._config_path or not os.path.exists(self._config_path):
                print(f"[ThinkingLog] [SAVE] ABORT: config_path invalid or missing (config_path={self._config_path!r}, exists={os.path.exists(self._config_path) if self._config_path else None})", flush=True)
                return
            try:
                g = self.geometry()
                new = {
                    'window_x': g.x(),
                    'window_y': g.y(),
                    'window_width': g.width(),
                    'window_height': g.height(),
                }
                # 从 ConfigManager 进程内缓存取 old 值（避免为打印日志再单独读一次磁盘）
                from scrcpycontrol.config_manager import ConfigManager
                mgr = ConfigManager.get(self._config_path)
                old = {
                    k: mgr.get_value(f'thinking_log.{k}')
                    for k in ('window_x', 'window_y', 'window_width', 'window_height')
                }
                # 唯一入口：刷新单例缓存 → debounce 写磁盘
                mgr.update_section('thinking_log', new)
                print(f"[ThinkingLog] [SAVE] SUCCESS | old={old} → new={new} | file={self._config_path!r}", flush=True)
            except Exception as e:
                print(f"[ThinkingLog] [SAVE] FAILED: {e}", flush=True)
                import traceback
                traceback.print_exc()

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
                log_y = CHROME_H
                log_h = h - CHROME_H
            else:
                log_y = 0
                log_h = h
            self._log_view.setGeometry(0, log_y, w, log_h)
            # Request a geometry save (debounced)
            self._geometry_save_timer.start()

        def moveEvent(self, event):
            super().moveEvent(event)
            self._geometry_save_timer.start()

        def leaveEvent(self, event):
            if self._hover_btn != -1:
                self._hover_btn = -1
                self.update()
            self.setCursor(Qt.ArrowCursor)
            super().leaveEvent(event)

        def _apply_style(self):
            """Minimal Win32 style: topmost, noactivate, toolwindow, acrylic blur."""
            if os.name != 'nt':
                return
            try:
                hwnd = _safe_hwnd(self.winId())
                if not hwnd:
                    return
                ex_style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
                new_ex = (ex_style
                          | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                          | WS_EX_NOPARENTNOTIFY | WS_EX_TOPMOST
                          | WS_EX_LAYERED)
                new_ex &= ~WS_EX_APPWINDOW
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, new_ex)

                style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
                new_style = style | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
                new_style &= ~(WS_DISABLED | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
                user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, new_style)

                _apply_acrylic_blur(hwnd, color=PANEL_ACCENT_COLOR)

                user32.SetWindowPos(
                    wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                    | SWP_NOSENDCHANGING | SWP_NOOWNERZORDER
                    | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
                )
            except Exception as e:
                print(f"[ThinkingLog] Style error: {e}", flush=True)

        def ensure_topmost(self):
            self._apply_style()

        def nativeEvent(self, eventType, message):
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

        # ─────────────────── Paint: chrome + traffic lights ───────────────────
        def paintEvent(self, event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()

            # Rounded panel fill with subtle shadow
            bg_rect = QRect(0, 0, w, h)
            path = QPainterPath()
            path.addRoundedRect(bg_rect.adjusted(1, 1, -1, -1), CARD_RADIUS, CARD_RADIUS)
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(bg_rect.translated(0, 3), CARD_RADIUS, CARD_RADIUS)
            p.fillPath(shadow_path, QColor(0, 0, 0, 60))
            p.fillPath(path, PANEL_FILL)

            if self._editing:
                # Chrome title bar separator
                p.setPen(QPen(QColor(255, 255, 255, 18), 1))
                p.drawLine(10, CHROME_H - 1, w - 10, CHROME_H - 1)

                # Title (right-aligned, like macOS window titles)
                title_font = QFont()
                title_font.setPointSize(10)
                title_font.setBold(True)
                p.setFont(title_font)
                p.setPen(TEXT_SECONDARY)
                p.drawText(
                    QRect(10, 0, w - 10, CHROME_H),
                    Qt.AlignVCenter | Qt.AlignCenter,
                    self._win_title,
                )

                # Traffic lights
                for i in range(3):
                    colors = [TRAFFIC_CLOSE, TRAFFIC_MIN, TRAFFIC_MAX]
                    h_colors = [TRAFFIC_CLOSE_H, TRAFFIC_MIN_H, TRAFFIC_MAX_H]
                    c = h_colors[i] if self._hover_btn == i else QColor(colors[i])
                    bx = 14 + i * 20
                    by = (CHROME_H - 12) // 2
                    # Shadow rim
                    p.setBrush(QBrush(QColor(0, 0, 0, 60)))
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(QPoint(bx + 6, by + 7), 7, 7)
                    # Fill
                    p.setBrush(QBrush(c))
                    p.drawEllipse(QPoint(bx + 6, by + 6), 6, 6)
                    # Glyphs on hover
                    if self._hover_btn == i:
                        glyphs = ["×", "−", "+"]
                        g = glyphs[i]
                        p.setPen(QColor(50, 40, 40))
                        gf = QFont()
                        gf.setPointSize(7)
                        gf.setBold(True)
                        p.setFont(gf)
                        p.drawText(QRect(bx, by - 1, 12, 14), Qt.AlignCenter, g)

                # Line count badge
                try:
                    n = self._log_view.document().blockCount()
                except Exception:
                    n = 0
                if n:
                    cf = QFont()
                    cf.setPointSize(9)
                    p.setFont(cf)
                    p.setPen(TEXT_TERTIARY)
                    p.drawText(
                        QRect(10, 0, w - 10, CHROME_H),
                        Qt.AlignVCenter | Qt.AlignRight,
                        f"{n} 行",
                    )

            p.end()

        # ───────────────── Mouse handling: chrome + resize ─────────────────
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

        def _edge_at(self, pos, include_editing_check=True):
            if include_editing_check and not self._editing:
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

            if not self._editing:
                super().mousePressEvent(event)
                return

            btn = self._traffic_light_at(pos)
            if btn >= 0:
                self._drag_mode = self.NONE
                if btn == 0:
                    # Close button → hide + emit signal
                    self.hide()
                    self.closed_signal.emit()
                    self._save_geometry()
                elif btn == 1:
                    self.showMinimized()
                elif btn == 2:
                    # Exactly same behavior as NotificationPanelWindow: exit edit mode (pin)
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
            self._drag_mode = self.NONE
            self._drag_start = None
            self._drag_start_geom = None
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

        def update_position(self, x, y):
            self.move(x, y)

    return ThinkingLogWindow()
