"""
Window Config VO - Value Object for scrcpy window display configuration.

Mac-style card decoration (borderless, always-on-top, rounded corners, drop shadow,
traffic lights, drag & resize) is ALWAYS applied as the default style.
This VO only controls functional parameters like position, size, orientation,
resolution, bitrate, etc. — NOT visual styling.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class WindowConfig:
    """
    Configuration for a scrcpy display window.

    All windows use Mac-style card decoration by default (no style parameters needed).
    This VO only captures functional/display parameters.

    Attributes:
        window_title: Window title bar text.
        window_x: Initial window X position (pixels, screen coordinates).
        window_y: Initial window Y position (pixels, screen coordinates).
        window_width: Window width in pixels.
        window_height: Window height in pixels.
        max_fps: Maximum frame rate for video stream.
        bitrate: Video bitrate (e.g. "8M", "2M").
        video_codec: Video codec ("h264", "h265", "av1").
        keep_active: Keep scrcpy process alive and attempt reconnection.
        turn_screen_off: Turn off physical phone screen after starting (main display only).
        stay_awake: Prevent Android from sleeping while scrcpy is connected.
        default_orientation: Default orientation ("portrait" or "landscape").
        new_display_width: Virtual display width (portrait natural orientation).
        new_display_height: Virtual display height (portrait natural orientation).
        new_display_dpi: Virtual display DPI (portrait).
        landscape_new_display_width: Virtual display width (landscape natural orientation).
        landscape_new_display_height: Virtual display height (landscape natural orientation).
        landscape_new_display_dpi: Virtual display DPI (landscape).
        no_vd_system_decorations: Hide Android system status/navigation bars on virtual display.
        flex_display: Allow flexible display resizing.
        rotation: scrcpy encoding rotation (0, 90, 180, 270). App displays should use 0.
        user_rotation: Android wm user-rotation lock (0=natural, 1=landscape).
    """

    window_title: str = "scrcpy"
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    window_width: int = 540
    window_height: int = 960
    max_fps: int = 60
    bitrate: str = "8M"
    video_codec: str = "h264"
    keep_active: bool = True
    turn_screen_off: bool = True
    stay_awake: bool = True

    # Orientation & virtual display resolution
    default_orientation: str = "portrait"
    new_display_width: int = 1260
    new_display_height: int = 2844
    new_display_dpi: Optional[int] = None
    landscape_new_display_width: int = 1920
    landscape_new_display_height: int = 1080
    landscape_new_display_dpi: int = 320

    # scrcpy flags
    no_vd_system_decorations: bool = True
    flex_display: bool = False
    rotation: int = 0
    user_rotation: int = 0

    # Special mode: e.g. "notification_panel" for system notification shade
    special_mode: Optional[str] = None

    @classmethod
    def from_dict(cls, cfg: dict, is_main: bool = False) -> "WindowConfig":
        """
        Create a WindowConfig from a dictionary (e.g. loaded from display_config.json).

        Args:
            cfg: Dictionary with configuration keys.
            is_main: True if this is the main display (affects defaults).

        Returns:
            WindowConfig instance with values from the dict, falling back to defaults.
        """
        return cls(
            window_title=cfg.get("window_title", "手机桌面" if is_main else "scrcpy"),
            window_x=cfg.get("window_x"),
            window_y=cfg.get("window_y"),
            window_width=cfg.get("window_width", 540),
            window_height=cfg.get("window_height", 960),
            max_fps=cfg.get("max_fps", 60),
            bitrate=cfg.get("bitrate", "8M"),
            video_codec=cfg.get("video_codec", "h264"),
            keep_active=cfg.get("keep_active", True),
            turn_screen_off=cfg.get("turn_screen_off", True if is_main else False),
            stay_awake=cfg.get("stay_awake", True),
            default_orientation=cfg.get("default_orientation", "portrait"),
            new_display_width=cfg.get("new_display_width", 1260),
            new_display_height=cfg.get("new_display_height", 2844),
            new_display_dpi=cfg.get("new_display_dpi"),
            landscape_new_display_width=cfg.get("landscape_new_display_width", 1920),
            landscape_new_display_height=cfg.get("landscape_new_display_height", 1080),
            landscape_new_display_dpi=cfg.get("landscape_new_display_dpi", 320),
            no_vd_system_decorations=cfg.get("no_vd_system_decorations", True),
            flex_display=cfg.get("flex_display", False),
            rotation=cfg.get("rotation", 0),
            user_rotation=cfg.get("user_rotation", 0),
            special_mode=cfg.get("special_mode"),
        )

    def resolve_virtual_display_size(self, orientation: str) -> Tuple[int, int, Optional[int]]:
        """
        Resolve the correct virtual display (width, height, dpi) based on
        the requested orientation and the window aspect ratio.

        This automatically handles:
        - Portrait windows → portrait VD resolution
        - Landscape windows → landscape VD resolution
        - Square/near-square windows → square VD resolution (to avoid letterbox black bars)

        Args:
            orientation: Requested orientation ("portrait" or "landscape").

        Returns:
            Tuple of (vd_width, vd_height, vd_dpi).
        """
        win_w, win_h = self.window_width, self.window_height
        win_ratio = win_w / win_h if win_h else 1.0

        if orientation == "landscape" or win_ratio >= 1.2:
            # Landscape window → use landscape VD config
            nd_w = self.landscape_new_display_width
            nd_h = self.landscape_new_display_height
            nd_dpi = self.landscape_new_display_dpi
            if nd_w < nd_h:
                nd_w, nd_h = nd_h, nd_w
            return nd_w, nd_h, nd_dpi

        elif orientation == "portrait" and win_ratio <= 0.8:
            # Portrait window → use portrait VD config
            nd_w = self.new_display_width
            nd_h = self.new_display_height
            nd_dpi = self.new_display_dpi
            if nd_w > nd_h:
                nd_w, nd_h = nd_h, nd_w
            return nd_w, nd_h, nd_dpi

        else:
            # Square / near-square window → check for square landscape config
            ls_w = self.landscape_new_display_width
            ls_h = self.landscape_new_display_height
            if ls_w and ls_h and abs(ls_w / ls_h - 1.0) < 0.15:
                return ls_w, ls_h, self.landscape_new_display_dpi
            else:
                # Compute square VD from window size
                side = max(win_w, win_h) * 2
                return side, side, self.new_display_dpi or 400

    def swap_dimensions_for_orientation(self, orientation: str):
        """
        Swap window_width/window_height if the requested orientation doesn't match
        the natural aspect ratio of the configured dimensions.

        Args:
            orientation: Target orientation ("portrait" or "landscape").
        """
        w, h = self.window_width, self.window_height
        is_wide = w >= h

        if orientation == "landscape" and not is_wide:
            self.window_width, self.window_height = h, w
        elif orientation == "portrait" and is_wide:
            self.window_width, self.window_height = h, w
