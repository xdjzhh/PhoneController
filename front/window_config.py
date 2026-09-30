"""
Window Config VO - Value Object for scrcpy window display configuration.

Mac-style card decoration (borderless, always-on-top, rounded corners, drop shadow,
traffic lights, drag & resize) is ALWAYS applied as the default style.
This VO only controls functional parameters like position, size, resolution,
bitrate, etc. — NOT visual styling.

Orientation/landscape/portrait logic has been removed entirely.
Virtual display resolution is selected purely from the window aspect ratio:
    - window_width >= window_height → wide VD config
    - window_width <  window_height → tall VD config
This lets each card define its own resolution and aspect ratio freely, without
any explicit "横屏" mode swapping or rotation handling.
"""

from dataclasses import dataclass
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
        window_width: Window width in pixels (defines card aspect ratio).
        window_height: Window height in pixels (defines card aspect ratio).
        max_fps: Maximum frame rate for video stream.
        bitrate: Video bitrate (e.g. "8M", "2M").
        video_codec: Video codec ("h264", "h265", "av1").
        keep_active: Keep scrcpy process alive and attempt reconnection.
        turn_screen_off: Turn off physical phone screen after starting (main display only).
        stay_awake: Prevent Android from sleeping while scrcpy is connected.
        audio: 是否转发音频（默认 True）。False → scrcpy 加 --no-audio，
               卡片静音且不占用音频通道带宽；多卡片场景通常只让主卡开音频。

        tall_new_display_width:  VD width  used when window is tall  (w <  h).
        tall_new_display_height: VD height used when window is tall  (w <  h).
        tall_new_display_dpi:    VD DPI    used when window is tall  (w <  h).
        wide_new_display_width:  VD width  used when window is wide  (w >= h).
        wide_new_display_height: VD height used when window is wide  (w >= h).
        wide_new_display_dpi:    VD DPI    used when window is wide  (w >= h).

        no_vd_system_decorations: Hide Android system status/navigation bars on virtual display.
        flex_display: Allow flexible display resizing.
        special_mode: e.g. "notification_panel" for system notification shade.
    """

    window_title: str = "scrcpy"
    window_x: Optional[int] = None
    window_y: Optional[int] = None
    window_width: int = 540
    window_height: int = 960
    max_fps: int = 60
    bitrate: str = "8M"
    video_codec: str = "h264"
    # 客户端显示前的抖动缓冲（毫秒）。无线链路建议视频类卡片设 80~150，
    # 以延迟换流畅；交互主屏保持 None（不追加参数，scrcpy 默认 0）。
    video_buffer_ms: Optional[int] = None
    keep_active: bool = True
    turn_screen_off: bool = True
    stay_awake: bool = True
    # 音频转发开关：False 时 scrcpy 以 --no-audio 启动（卡片静音 + 省音频通道）。
    # 默认 True 与 scrcpy 原生行为一致；需要静音的卡片在 display_config.json 里显式设 false。
    audio: bool = True

    # Virtual display resolution - selected purely by window aspect ratio (w < h → tall).
    # (Names kept deliberately neutral — no "portrait/landscape" semantics, no swapping.)
    tall_new_display_width: int = 1260
    tall_new_display_height: int = 2844
    tall_new_display_dpi: Optional[int] = None
    wide_new_display_width: int = 1920
    wide_new_display_height: int = 1080
    wide_new_display_dpi: int = 320

    # scrcpy flags
    no_vd_system_decorations: bool = True
    flex_display: bool = False

    # Special mode: e.g. "notification_panel" for system notification shade
    special_mode: Optional[str] = None

    @classmethod
    def from_dict(cls, cfg: dict, is_main: bool = False) -> "WindowConfig":
        """
        Create a WindowConfig from a dictionary (e.g. loaded from display_config.json).

        Legacy landscape_*/portrait/default_orientation/rotation/user_rotation keys
        are mapped to the new neutral tall_*/wide_* names so existing config files
        still work without migration.
        """
        # Resolution priority: new wide/tall names > legacy landscape/new_display names
        wide_w = cfg.get("wide_new_display_width",
                 cfg.get("landscape_new_display_width", 1920))
        wide_h = cfg.get("wide_new_display_height",
                 cfg.get("landscape_new_display_height", 1080))
        wide_dpi = cfg.get("wide_new_display_dpi",
                   cfg.get("landscape_new_display_dpi", 320))

        tall_w = cfg.get("tall_new_display_width",
                 cfg.get("new_display_width", 1260))
        tall_h = cfg.get("tall_new_display_height",
                 cfg.get("new_display_height", 2844))
        tall_dpi = cfg.get("tall_new_display_dpi",
                   cfg.get("new_display_dpi"))

        return cls(
            window_title=cfg.get("window_title", "手机桌面" if is_main else "scrcpy"),
            window_x=cfg.get("window_x"),
            window_y=cfg.get("window_y"),
            window_width=cfg.get("window_width", 540),
            window_height=cfg.get("window_height", 960),
            max_fps=cfg.get("max_fps", 60),
            bitrate=cfg.get("bitrate", "8M"),
            video_codec=cfg.get("video_codec", "h264"),
            video_buffer_ms=cfg.get("video_buffer_ms"),
            keep_active=cfg.get("keep_active", True),
            turn_screen_off=cfg.get("turn_screen_off", True if is_main else False),
            stay_awake=cfg.get("stay_awake", True),
            audio=cfg.get("audio", True),
            tall_new_display_width=tall_w,
            tall_new_display_height=tall_h,
            tall_new_display_dpi=tall_dpi,
            wide_new_display_width=wide_w,
            wide_new_display_height=wide_h,
            wide_new_display_dpi=wide_dpi,
            no_vd_system_decorations=cfg.get("no_vd_system_decorations", True),
            flex_display=cfg.get("flex_display", False),
            special_mode=cfg.get("special_mode"),
        )

    def resolve_virtual_display_size(self) -> Tuple[int, int, Optional[int]]:
        """
        Resolve virtual display (width, height, dpi) based purely on the card's
        own window aspect ratio.

        Rules (simple, deterministic, no orientation semantics):
          1. w >= h → wide resolution   (wide_new_display_*)
          2. w <  h → tall   resolution (tall_new_display_*)
        VD dimensions are normalized to match the requested window aspect so apps
        never receive unexpected "横屏 → 竖屏" orientation-change events.
        """
        w, h = self.window_width, self.window_height

        if w >= h:
            nd_w, nd_h, nd_dpi = (self.wide_new_display_width,
                                  self.wide_new_display_height,
                                  self.wide_new_display_dpi)
            # Always keep w >= h for wide cards (guard against reversed configs).
            if nd_w < nd_h:
                nd_w, nd_h = nd_h, nd_w
            return nd_w, nd_h, nd_dpi

        nd_w, nd_h, nd_dpi = (self.tall_new_display_width,
                              self.tall_new_display_height,
                              self.tall_new_display_dpi)
        # Always keep w < h for tall cards (guard against reversed configs).
        if nd_w > nd_h:
            nd_w, nd_h = nd_h, nd_w
        return nd_w, nd_h, nd_dpi
