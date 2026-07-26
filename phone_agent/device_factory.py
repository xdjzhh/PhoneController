"""Device factory for selecting ADB or HDC based on device type."""

from enum import Enum
from typing import Any

from scrcpycontrol.display_interface import get_display_manager
from phone_agent.config.apps import APP_PACKAGES


class DeviceType(Enum):
    """Type of device connection tool."""

    ADB = "adb"
    HDC = "hdc"
    IOS = "ios"


class DeviceFactory:
    """
    Factory class for getting device-specific implementations.

    This allows the system to work with both Android (ADB) and HarmonyOS (HDC) devices.
    """

    def __init__(self, device_type: DeviceType = DeviceType.ADB):
        """
        Initialize the device factory.

        Args:
            device_type: The type of device to use (ADB or HDC).
        """
        self.device_type = device_type
        self._module = None
        self._active_display_id = None

    @property
    def active_display_id(self):
        """Get the currently active virtual display ID. None means main display."""
        return self._active_display_id

    def set_active_display(self, display_id: int | None = None):
        """
        Set the active virtual display.

        Args:
            display_id: Virtual display ID. None or 0 means main display.
        """
        if display_id is not None and display_id == 0:
            display_id = None
        self._active_display_id = display_id
        print(f"[DeviceFactory] Active display set to: {display_id if display_id else 'main'}")

    def reset_display(self):
        """Reset to main display."""
        self._active_display_id = None
        print("[DeviceFactory] Reset to main display")

    @property
    def module(self):
        """Get the appropriate device module (adb or hdc)."""
        if self._module is None:
            if self.device_type == DeviceType.ADB:
                from phone_agent import adb

                self._module = adb
            elif self.device_type == DeviceType.HDC:
                from phone_agent import hdc

                self._module = hdc
            else:
                raise ValueError(f"Unknown device type: {self.device_type}")
        return self._module

    def get_screenshot(self, device_id: str | None = None, timeout: int = 10):
        """Get screenshot from device."""
        return self.module.get_screenshot(device_id, timeout, self._active_display_id)

    def get_current_app(self, device_id: str | None = None) -> str:
        """Get current app name.

        If a virtual display is active, returns the app name for that display.
        Otherwise, queries the device for the current foreground app.
        """
        if self._active_display_id is not None and self._active_display_id != 0:
            app_name = self._get_app_name_for_display(self._active_display_id)
            if app_name:
                return app_name
        return self.module.get_current_app(device_id)

    def _get_app_name_for_display(self, display_id: int) -> str | None:
        """Get app name for a virtual display ID.

        Returns the app name (e.g. "微信") if found, None otherwise.
        """
        try:
            dm = get_display_manager()
            displays = dm.list_displays().get("displays", [])
            for d in displays:
                if d.get("android_display_id") == display_id:
                    app_package = d.get("app_package")
                    if app_package:
                        for name, pkg in APP_PACKAGES.items():
                            if pkg == app_package:
                                return name
                        return app_package
            return None
        except Exception:
            return None

    def tap(
        self, x: int, y: int, device_id: str | None = None, delay: float | None = None
    ):
        """Tap at coordinates."""
        return self.module.tap(x, y, device_id, delay, self._active_display_id)

    def double_tap(
        self, x: int, y: int, device_id: str | None = None, delay: float | None = None
    ):
        """Double tap at coordinates."""
        return self.module.double_tap(x, y, device_id, delay, self._active_display_id)

    def long_press(
        self,
        x: int,
        y: int,
        duration_ms: int = 3000,
        device_id: str | None = None,
        delay: float | None = None,
    ):
        """Long press at coordinates."""
        return self.module.long_press(x, y, duration_ms, device_id, delay, self._active_display_id)

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int | None = None,
        device_id: str | None = None,
        delay: float | None = None,
    ):
        """Swipe from start to end."""
        return self.module.swipe(
            start_x, start_y, end_x, end_y, duration_ms, device_id, delay, self._active_display_id
        )

    def back(self, device_id: str | None = None, delay: float | None = None):
        """Press back button."""
        return self.module.back(device_id, delay, self._active_display_id)

    def home(self, device_id: str | None = None, delay: float | None = None):
        """Press home button.

        If a virtual display is active, switches back to main display instead
        of pressing home key. On main display, presses home key as normal.
        """
        if self._active_display_id is not None and self._active_display_id != 0:
            print("[DeviceFactory] Virtual display active, switching to main display instead of home")
            self.reset_display()
            try:
                dm = get_display_manager()
                dm.activate_app_window(None)
            except Exception:
                pass
            return
        return self.module.home(device_id, delay, self._active_display_id)

    def launch_app(
        self, app_name: str, device_id: str | None = None, delay: float | None = None
    ) -> bool:
        """Launch an app."""
        return self.module.launch_app(app_name, device_id, delay)

    def type_text(self, text: str, device_id: str | None = None):
        """Type text."""
        return self.module.type_text(text, device_id)

    def clear_text(self, device_id: str | None = None):
        """Clear text."""
        return self.module.clear_text(device_id)

    def detect_and_set_adb_keyboard(self, device_id: str | None = None) -> str:
        """Detect and set keyboard."""
        return self.module.detect_and_set_adb_keyboard(device_id)

    def restore_keyboard(self, ime: str, device_id: str | None = None):
        """Restore keyboard."""
        return self.module.restore_keyboard(ime, device_id)

    def list_devices(self):
        """List connected devices."""
        return self.module.list_devices()

    def get_connection_class(self):
        """Get the connection class (ADBConnection or HDCConnection)."""
        if self.device_type == DeviceType.ADB:
            from phone_agent.adb import ADBConnection

            return ADBConnection
        elif self.device_type == DeviceType.HDC:
            from phone_agent.hdc import HDCConnection

            return HDCConnection
        else:
            raise ValueError(f"Unknown device type: {self.device_type}")


# Global device factory instance
_device_factory: DeviceFactory | None = None


def set_device_type(device_type: DeviceType):
    """
    Set the global device type.

    Args:
        device_type: The device type to use (ADB or HDC).
    """
    global _device_factory
    _device_factory = DeviceFactory(device_type)


def get_device_factory() -> DeviceFactory:
    """
    Get the global device factory instance.

    Returns:
        The device factory instance.
    """
    global _device_factory
    if _device_factory is None:
        _device_factory = DeviceFactory(DeviceType.ADB)  # Default to ADB
    return _device_factory
