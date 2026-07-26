"""
scrcpycontrol - Screen control and virtual display management module.

Provides display management, virtual display creation, and screen control
functionality for Android device automation.
"""

from scrcpycontrol.display_interface import DisplayManager, get_display_manager

__version__ = "0.1.0"
__all__ = ["DisplayManager", "get_display_manager"]
