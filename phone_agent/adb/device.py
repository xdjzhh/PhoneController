"""Device control utilities for Android automation."""

import atexit
import json
import os
import re
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from scrcpycontrol.display_interface import get_display_manager
from phone_agent.config.apps import APP_PACKAGES
from phone_agent.config.timing import TIMING_CONFIG

USE_VIRTUAL_DISPLAY = os.environ.get("USE_VIRTUAL_DISPLAY", "1") == "1"


def _launch_app_via_virtual_display(package: str) -> bool:
    """
    Launch an app via DisplayManager (virtual display if configured, else main).

    Args:
        package: App package name.

    Returns:
        True if launched, False otherwise.
    """
    try:
        dm = get_display_manager()
        result = dm.start_app_display(package)
        if result.get("success"):
            display_type = result.get("display_type", "unknown")
            dedup = result.get("deduplicated", False)
            extra = " (deduplicated)" if dedup else ""
            print(f"[DisplayManager] App {package} launched via {display_type} display{extra}")
            return True
        else:
            print(f"[DisplayManager] Failed to start display for {package}: "
                  f"{result.get('message') or result.get('error')}")
            return False
    except Exception as e:
        print(f"[DisplayManager] Error launching {package}: {e}")
        import traceback
        traceback.print_exc()
        return False


def get_current_app(device_id: str | None = None) -> str:
    """
    Get the currently focused app name.

    Args:
        device_id: Optional ADB device ID for multi-device setups.

    Returns:
        The app name if recognized, otherwise "System Home".
    """
    adb_prefix = _get_adb_prefix(device_id)

    result = subprocess.run(
        adb_prefix + ["shell", "dumpsys", "window"], capture_output=True, text=True, encoding="utf-8"
    )
    output = result.stdout
    if not output:
        raise ValueError("No output from dumpsys window")

    # Parse window focus info
    for line in output.split("\n"):
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            for app_name, package in APP_PACKAGES.items():
                if package in line:
                    return app_name

    return "System Home"


def tap(
    x: int, y: int, device_id: str | None = None, delay: float | None = None,
    display_id: int | None = None,
) -> None:
    """
    Tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after tap. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_tap_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    result = subprocess.run(
        input_cmd + ["tap", str(x), str(y)], capture_output=True
    )
    err = (result.stderr or b"").decode("utf-8", "replace").strip() if result.stderr else ""
    if result.returncode != 0 or err:
        # 典型：-d 指定的虚拟 display id 已失效 / 注入被系统拒绝。
        # 以前这里静默吞掉，日志上表现为"点了没反应"且无法定位。
        print(f"[adb.input] tap failed display={display_id or 0} ({x},{y}) "
              f"rc={result.returncode} err={err!r}", flush=True)
    time.sleep(delay)


def double_tap(
    x: int, y: int, device_id: str | None = None, delay: float | None = None,
    display_id: int | None = None,
) -> None:
    """
    Double tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after double tap. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_double_tap_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    subprocess.run(
        input_cmd + ["tap", str(x), str(y)], capture_output=True
    )
    time.sleep(TIMING_CONFIG.device.double_tap_interval)
    subprocess.run(
        input_cmd + ["tap", str(x), str(y)], capture_output=True
    )
    time.sleep(delay)


def long_press(
    x: int,
    y: int,
    duration_ms: int = 3000,
    device_id: str | None = None,
    delay: float | None = None,
    display_id: int | None = None,
) -> None:
    """
    Long press at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        duration_ms: Duration of press in milliseconds.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after long press. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_long_press_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    subprocess.run(
        input_cmd
        + ["swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
        capture_output=True,
    )
    time.sleep(delay)


def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int | None = None,
    device_id: str | None = None,
    delay: float | None = None,
    display_id: int | None = None,
) -> None:
    """
    Swipe from start to end coordinates.

    Args:
        start_x: Starting X coordinate.
        start_y: Starting Y coordinate.
        end_x: Ending X coordinate.
        end_y: Ending Y coordinate.
        duration_ms: Duration of swipe in milliseconds (auto-calculated if None).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after swipe. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_swipe_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    if duration_ms is None:
        # Calculate duration based on distance
        dist_sq = (start_x - end_x) ** 2 + (start_y - end_y) ** 2
        duration_ms = int(dist_sq / 1000)
        duration_ms = max(500, min(duration_ms, 1000))  # Clamp between 1000-2000ms

    subprocess.run(
        input_cmd
        + [
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        capture_output=True,
    )
    time.sleep(delay)


def back(device_id: str | None = None, delay: float | None = None, display_id: int | None = None) -> None:
    """
    Press the back button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing back. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_back_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    subprocess.run(
        input_cmd + ["keyevent", "4"], capture_output=True
    )
    time.sleep(delay)


def home(device_id: str | None = None, delay: float | None = None, display_id: int | None = None) -> None:
    """
    Press the home button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing home. If None, uses configured default.
        display_id: Optional virtual display ID. None or 0 means main display.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_home_delay

    input_cmd = _get_input_cmd(device_id, display_id)

    subprocess.run(
        input_cmd + ["keyevent", "KEYCODE_HOME"], capture_output=True
    )
    time.sleep(delay)


def launch_app(
    app_name: str, device_id: str | None = None, delay: float | None = None
) -> bool:
    """
    Launch an app by name.

    If USE_VIRTUAL_DISPLAY is enabled, tries to launch via virtual display first.
    Falls back to ADB monkey if virtual display fails or is not configured.

    Args:
        app_name: The app name (must be in APP_PACKAGES).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after launching. If None, uses configured default.

    Returns:
        True if app was launched, False if app not found.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_launch_delay

    if app_name not in APP_PACKAGES:
        return False

    package = APP_PACKAGES[app_name]

    if USE_VIRTUAL_DISPLAY:
        success = _launch_app_via_virtual_display(package)
        if success:
            time.sleep(delay)
            return True
        print(f"[VirtualDisplay] Falling back to ADB monkey launch for {package}")

    adb_prefix = _get_adb_prefix(device_id)

    subprocess.run(
        adb_prefix
        + [
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        capture_output=True,
    )
    time.sleep(delay)
    return True


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]


def _get_input_cmd(device_id: str | None, display_id: int | None = None) -> list:
    """Get input command prefix with optional display ID.

    Args:
        device_id: Optional ADB device ID.
        display_id: Optional virtual display ID.

    Returns:
        Command prefix list (e.g., ['adb', 'shell', 'input'] or ['adb', 'shell', 'input', '-d', '144'])
    """
    cmd = _get_adb_prefix(device_id) + ["shell", "input"]
    if display_id is not None and display_id != 0:
        cmd += ["-d", str(display_id)]
    return cmd
