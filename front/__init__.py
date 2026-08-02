"""
Frontend UI module for AR card windows.

Provides:
- Mac-style card window decoration (traffic lights, drag, resize)
- Windows DWM styling (rounded corners, drop shadow) for scrcpy windows
- WindowConfig VO for clean separation of display configuration from style
- Qt backdrop window simulating headless Linux desktop environment
- Qt macOS-style notification panel (ADB-driven, no virtual display needed)
"""

from .card_window import (
    get_card_window_decorator,
    apply_mac_card_style,
)
from .window_config import WindowConfig
from .qt_backdrop import get_backdrop_manager, QtBackdropManager
from .qt_notification import (
    create_notification_window,
    NotificationParser,
    NotificationItem,
)

__all__ = [
    'get_card_window_decorator',
    'apply_mac_card_style',
    'WindowConfig',
    'get_backdrop_manager',
    'QtBackdropManager',
    'create_notification_window',
    'NotificationParser',
    'NotificationItem',
]
