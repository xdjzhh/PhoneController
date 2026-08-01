"""
Frontend UI module for AR card windows.

Provides:
- Mac-style card window decoration (traffic lights, drag, resize)
- Windows DWM styling (rounded corners, drop shadow) for scrcpy windows
- WindowConfig VO for clean separation of display configuration from style
"""

from .card_window import (
    get_card_window_decorator,
    apply_mac_card_style,
)
from .window_config import WindowConfig

__all__ = ['get_card_window_decorator', 'apply_mac_card_style', 'WindowConfig']
