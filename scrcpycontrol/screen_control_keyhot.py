import ctypes
from ctypes import wintypes
import time
import win32gui
import win32api
import win32con
import pyautogui


VK_MENU = 0x12
VK_SHIFT = 0x10
VK_O = 0x4F
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1
SW_SHOW = 5
SW_RESTORE = 9
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


class ScreenControlKeyHot:
    """通过键盘快捷键控制 scrcpy 窗口的屏幕电源

    通过向 scrcpy 窗口发送快捷键（Alt+O / Alt+Shift+O）来控制物理屏幕的开关。
    """

    def __init__(self, config=None):
        """Initialize screen control via keyboard hotkey method

        Args:
            config: Display configuration dict (with main_display.window_title)
        """
        self.config = config or {}
        self._user32 = ctypes.windll.user32

    def _get_main_window_title(self):
        """Get main display window title from config"""
        return self.config.get('main_display', {}).get('window_title', '手机桌面')

    def enum_scrcpy_windows(self):
        """枚举所有 scrcpy 窗口（调试用）"""
        windows = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                class_name = win32gui.GetClassName(hwnd)
                if 'scrcpy' in class_name.lower() or (title and len(title) > 0):
                    windows.append({
                        'hwnd': hwnd,
                        'title': title,
                        'class_name': class_name
                    })
            return True

        win32gui.EnumWindows(callback, None)
        return windows

    def find_scrcpy_window(self, title=None):
        """查找 scrcpy 窗口句柄

        Args:
            title: 窗口标题，如果为 None 则使用主显示配置的标题
        """
        if title is None:
            title = self._get_main_window_title()

        print(f"[ScreenPower] Looking for window with title: '{title}'")

        hwnd = self._user32.FindWindowW(None, title)
        if hwnd:
            print(f"[ScreenPower] ✅ Found window: '{title}' (hwnd={hwnd})")
            return hwnd

        print(f"[ScreenPower] Window with title '{title}' not found, enumerating all windows...")
        all_windows = self.enum_scrcpy_windows()
        for w in all_windows:
            print(f"[ScreenPower]   - hwnd={w['hwnd']}, title='{w['title']}', class='{w['class_name']}'")

        for alt_title in ['手机桌面', 'scrcpy', 'Screen Mirror']:
            if alt_title == title:
                continue
            hwnd = self._user32.FindWindowW(None, alt_title)
            if hwnd:
                print(f"[ScreenPower] ✅ Found scrcpy window with title: '{alt_title}' (hwnd={hwnd})")
                return hwnd

        print(f"[ScreenPower] ❌ No scrcpy window found")

        current_fg = self._user32.GetForegroundWindow()
        print(f"[ScreenPower] DEBUG: Current foreground window: {current_fg}")

        return None

    def force_activate_window(self, hwnd):
        """使用多种方法强制激活窗口（绕过 Windows 前台锁定限制）

        即使激活失败也返回 True，让 pyautogui 继续尝试。
        """
        if not hwnd:
            print("[ScreenPower] ❌ Invalid window handle for force_activate")
            return False

        try:
            if not win32gui.IsWindow(hwnd):
                print("[ScreenPower] ❌ Window handle is not a valid window")
                return False

            if not win32gui.IsWindowVisible(hwnd):
                print("[ScreenPower] ⚠️ Window is not visible, trying to show it...")
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    time.sleep(0.1)
                except:
                    pass

            try:
                current_fg = win32gui.GetForegroundWindow()
                if current_fg == hwnd:
                    print(f"[ScreenPower] ✅ Window is already foreground (hwnd={hwnd})")
                    return True
            except:
                pass

            print(f"[ScreenPower] Activating window hwnd={hwnd}...")

            try:
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.05)
            except:
                pass

            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.1)
            except:
                pass

            try:
                win32gui.BringWindowToTop(hwnd)
            except:
                pass

            try:
                result = win32gui.SetForegroundWindow(hwnd)
                print(f"[ScreenPower] SetForegroundWindow result: {result}")
            except Exception as e:
                print(f"[ScreenPower] ⚠️ SetForegroundWindow failed: {e}")

            time.sleep(0.2)

            try:
                new_foreground = win32gui.GetForegroundWindow()
                if new_foreground == hwnd:
                    print(f"[ScreenPower] ✅ Window activated successfully (hwnd={hwnd})")
                    return True
                else:
                    try:
                        fg_title = win32gui.GetWindowText(new_foreground)
                        print(f"[ScreenPower] ⚠️ Window not activated, current fg: {new_foreground} ('{fg_title}')")
                    except:
                        print(f"[ScreenPower] ⚠️ Window not activated, current fg: {new_foreground}")
            except:
                pass

            print(f"[ScreenPower] ⚠️ Activation may have failed, proceeding anyway...")
            return True

        except Exception as e:
            print(f"[ScreenPower] ⚠️ Unexpected error in force_activate_window: {e}")
            return True

    def send_hotkey_to_scrcpy(self, hwnd, vk_code, modifiers=None):
        """向 scrcpy 窗口发送快捷键（pyautogui 方案：成熟可靠的键盘模拟）

        使用 pyautogui 而不是直接调用 SendInput，因为 pyautogui 已经处理了各种
        Windows 输入问题（权限、结构对齐、错误处理等）。
        """
        if not hwnd:
            print("[ScreenPower] ❌ Invalid window handle")
            return False

        modifiers = modifiers or []

        print(f"[ScreenPower] Using pyautogui method for window {hwnd}...")
        print(f"[ScreenPower] modifiers={modifiers}, vk_code={vk_code}")

        print("[ScreenPower] Activating scrcpy window...")
        if not self.force_activate_window(hwnd):
            print("[ScreenPower] ⚠️ Window activation may have failed, continuing anyway...")
        time.sleep(0.3)

        vk_to_name = {
            VK_MENU: 'alt',
            VK_SHIFT: 'shift',
            VK_O: 'o',
        }

        pyautogui_keys = []
        for mod in modifiers:
            name = vk_to_name.get(mod)
            if name:
                pyautogui_keys.append(name)
            else:
                print(f"[ScreenPower] ⚠️ Unknown modifier vk: {mod}")

        main_key_name = vk_to_name.get(vk_code)
        if not main_key_name:
            print(f"[ScreenPower] ❌ Unknown main key vk: {vk_code}")
            return False

        pyautogui_keys.append(main_key_name)

        print(f"[ScreenPower] pyautogui hotkey: {'+'.join(pyautogui_keys)}")

        try:
            pyautogui.hotkey(*pyautogui_keys)
            print(f"[ScreenPower] ✅ Hotkey sent successfully via pyautogui")
            return True
        except Exception as e:
            print(f"[ScreenPower] ❌ pyautogui hotkey failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def screen_off(self, window_title=None):
        """通过 Alt+O 关闭物理屏幕

        Args:
            window_title: scrcpy 窗口标题，None 则使用配置中的标题

        Returns:
            dict: {success, message, hwnd}
        """
        try:
            hwnd = self.find_scrcpy_window(window_title)
            if not hwnd:
                return {
                    "success": False,
                    "error": "scrcpy window not found. Make sure scrcpy is running."
                }

            print(f"[ScreenPower] Sending Alt+O to scrcpy window (hwnd={hwnd})...")

            success = self.send_hotkey_to_scrcpy(hwnd, VK_O, [VK_MENU])
            time.sleep(0.5)

            if success:
                print("[ScreenPower] ✅ Alt+O sent successfully")
                return {
                    "success": True,
                    "message": "Alt+O command sent to scrcpy window",
                    "method": "windows_api",
                    "hwnd": hwnd
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to send key message"
                }

        except Exception as e:
            print(f"[ScreenPower] ❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e)
            }

    def screen_on(self, window_title=None):
        """通过 Alt+Shift+O 点亮物理屏幕

        Args:
            window_title: scrcpy 窗口标题，None 则使用配置中的标题

        Returns:
            dict: {success, message, hwnd}
        """
        try:
            hwnd = self.find_scrcpy_window(window_title)
            if not hwnd:
                return {
                    "success": False,
                    "error": "scrcpy window not found. Make sure scrcpy is running."
                }

            print(f"[ScreenPower] Sending Alt+Shift+O to scrcpy window (hwnd={hwnd})...")

            success = self.send_hotkey_to_scrcpy(hwnd, VK_O, [VK_MENU, VK_SHIFT])
            time.sleep(0.5)

            if success:
                print("[ScreenPower] ✅ Alt+Shift+O sent successfully")
                return {
                    "success": True,
                    "message": "Alt+Shift+O command sent to scrcpy window",
                    "method": "windows_api",
                    "hwnd": hwnd
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to send key message"
                }

        except Exception as e:
            print(f"[ScreenPower] ❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e)
            }
