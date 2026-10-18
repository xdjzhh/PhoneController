"""Action handler for processing AI model outputs."""

import ast
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from scrcpycontrol.display_interface import get_display_manager
from phone_agent.config.apps import APP_PACKAGES
from phone_agent.config.timing import TIMING_CONFIG
from phone_agent.device_factory import get_device_factory


@dataclass
class ActionResult:
    """Result of an action execution."""

    success: bool
    should_finish: bool
    message: str | None = None
    requires_confirmation: bool = False


class ActionHandler:
    """
    Handles execution of actions from AI model output.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        confirmation_callback: Optional callback for sensitive action confirmation.
            Should return True to proceed, False to cancel.
        takeover_callback: Optional callback for takeover requests (login, captcha).
    """

    def __init__(
        self,
        device_id: str | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.device_id = device_id
        self.confirmation_callback = confirmation_callback or self._default_confirmation
        self.takeover_callback = takeover_callback or self._default_takeover

    def execute(
        self, action: dict[str, Any], screen_width: int, screen_height: int
    ) -> ActionResult:
        """
        Execute an action from the AI model.

        Args:
            action: The action dictionary from the model.
            screen_width: Current screen width in pixels.
            screen_height: Current screen height in pixels.

        Returns:
            ActionResult indicating success and whether to finish.
        """
        action_type = action.get("_metadata")

        if action_type == "finish":
            return ActionResult(
                success=True, should_finish=True, message=action.get("message")
            )

        if action_type != "do":
            return ActionResult(
                success=False,
                should_finish=True,
                message=f"Unknown action type: {action_type}",
            )

        action_name = action.get("action")
        handler_method = self._get_handler(action_name)

        if handler_method is None:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Unknown action: {action_name}",
            )

        try:
            return handler_method(action, screen_width, screen_height)
        except Exception as e:
            return ActionResult(
                success=False, should_finish=False, message=f"Action failed: {e}"
            )

    def _get_handler(self, action_name: str) -> Callable | None:
        """Get the handler method for an action."""
        handlers = {
            "Launch": self._handle_launch,
            "Close": self._handle_close,
            "Close_App": self._handle_close,
            "关闭应用": self._handle_close,
            "Tap": self._handle_tap,
            "Type": self._handle_type,
            "Type_Name": self._handle_type,
            "Swipe": self._handle_swipe,
            "Back": self._handle_back,
            "Home": self._handle_home,
            "Double Tap": self._handle_double_tap,
            "Long Press": self._handle_long_press,
            "Wait": self._handle_wait,
            "Take_over": self._handle_takeover,
            "Note": self._handle_note,
            "Call_API": self._handle_call_api,
            "Interact": self._handle_interact,
        }
        return handlers.get(action_name)

    def _convert_relative_to_absolute(
        self, element: list[int], screen_width: int, screen_height: int,
        label: str = "point",
    ) -> tuple[int, int]:
        """Convert model coordinates to absolute device pixels.

        协议要求模型给 0~999 相对坐标；但实测部分视觉模型（如 glm-5.3-flash）
        会直接输出【截图像素坐标】（1920x1080 横屏下 [542,980] 这类值），
        再乘一次 /1000 会让点击落点偏移近一倍（导航按钮就是这样点不中的）。
        任一维 >999 时像素意图是确定的（相对坐标不可能出现），此时按像素
        坐标直接使用并裁剪到屏幕范围内；≤999 仍按协议相对坐标换算。
        """
        rx, ry = element[0], element[1]
        if rx > 999 or ry > 999:
            x = max(0, min(int(rx), screen_width - 1))
            y = max(0, min(int(ry), screen_height - 1))
            print(f"[ActionHandler] {label}: 模型输出疑似像素坐标 {[rx, ry]} "
                  f"（截图 {screen_width}x{screen_height}）→ 直接按像素落点 ({x},{y})",
                  flush=True)
            return x, y

        x = round(rx / 1000 * screen_width)
        y = round(ry / 1000 * screen_height)
        print(f"[ActionHandler] {label}: 相对坐标 {[rx, ry]} "
              f"（截图 {screen_width}x{screen_height}）→ 像素落点 ({x},{y})",
              flush=True)
        return x, y

    def _handle_launch(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle app launch action."""
        app_name = action.get("app")
        if not app_name:
            return ActionResult(False, False, "No app name specified")

        device_factory = get_device_factory()
        success = device_factory.launch_app(app_name, self.device_id)

        if success:
            try:
                app_package = APP_PACKAGES.get(app_name)
                if app_package:
                    try:
                        # device_factory.launch_app already called dm.start_app_display
                        # internally. Do NOT call start_app_display again here — that
                        # would trigger a duplicate am start and pop the app twice.
                        # Instead, just look up the running display to get its ID.
                        dm = get_display_manager()
                        disp = dm._app_display_map.get(app_package)
                        if disp is not None and disp.running:
                            android_display_id = getattr(disp, "android_display_id", None)
                            if android_display_id is not None:
                                device_factory.set_active_display(android_display_id)
                                print(f"[ActionHandler] Switched to virtual display for "
                                      f"{app_name} (id={android_display_id})")
                            else:
                                print(f"[ActionHandler] {app_name} virtual display running "
                                      f"but display ID not parsed yet → using main display")
                        else:
                            # App was launched on main display (no virtual config)
                            device_factory.set_active_display(0)
                            print(f"[ActionHandler] {app_name} using main display (no virtual card)")
                    except Exception as e:
                        print(f"[ActionHandler] Failed to switch display: {e}")
            except Exception:
                pass
            return ActionResult(True, False)
        return ActionResult(False, False, f"App not found: {app_name}")

    def _handle_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)

        # Check for sensitive operation
        if "message" in action:
            if not self.confirmation_callback(action["message"]):
                return ActionResult(
                    success=False,
                    should_finish=True,
                    message="User cancelled sensitive operation",
                )

        device_factory = get_device_factory()
        device_factory.tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_close(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle close-app action.

        Behavior (matches user requirement exactly):
          1) If the target app has a configured virtual-display card entry in
             `display_config.json → app_displays.<package>` → reuse the EXACT
             traffic-light red-close cleanup path (i.e. call
             DisplayManager.stop_app_display so the scrcpy window dies, the
             Android VirtualDisplay is destroyed, the display-id is returned
             to the free pool and the entry is removed from the internal
             app map).  This is 100% identical to what happens when the user
             clicks the 🔴 red close button on the card.
          2) If the target app is NOT a configured card app → keep the
             original main-display behavior: just kill the app process on
             the phone via `am force-stop` and leave all windows alone.
        """
        from phone_agent.config.apps import get_package_name
        from scrcpycontrol.config_manager import ConfigManager

        app_name = action.get("app")
        if not app_name:
            return ActionResult(False, False, "No app name specified for Close")

        package = get_package_name(app_name)
        if not package:
            return ActionResult(
                False, False,
                f"Close failed: unknown app name '{app_name}'. "
                "Use the same display name as the Launch action."
            )

        dm = get_display_manager()
        cfg_obj = {}
        try:
            mgr = getattr(dm, "_config_manager_singleton", None)
            if mgr is None:
                cfg_path = getattr(dm, "config_path", None)
                if cfg_path:
                    mgr = ConfigManager.get(config_path=cfg_path)
                else:
                    mgr = ConfigManager.get()
            cfg_obj = mgr.get_config_object() or {}
        except Exception as e_cfg:
            print(f"[ActionHandler] Close: failed to read ConfigManager config: {e_cfg}")
            cfg_obj = {}

        app_displays_cfg = cfg_obj.get("app_displays", {}) or {}

        def _force_stop_main_display(pkg: str) -> tuple[bool, str]:
            """Fallback for non-card apps: `am force-stop` on the phone."""
            try:
                df = get_device_factory()
                if self.device_id:
                    import adbutils
                    dev = adbutils.adb.device(self.device_id)
                else:
                    dev = df.device
                if dev is None:
                    return False, "No ADB device connected for force-stop"
                dev.shell(f"am force-stop {pkg}")
                # Reset active display to main in case the agent was
                # targeting the now-dead app's display id.
                try:
                    df.set_active_display(0)
                except Exception:
                    pass
                return True, f"am force-stop {pkg} OK (main display path)"
            except Exception as e:
                return False, f"am force-stop {pkg} failed: {e}"

        has_card_entry = (
            isinstance(app_displays_cfg, dict)
            and package in app_displays_cfg
        )

        # ==============================================================
        # 【用户硬约束】：default_load=true 的卡片应用，我们植入的"卡片关闭 +
        # set_active_display(0) + am force-stop"整条链路全部短路跳过，
        # 以 return False 形式让上层把原命令**直接交给 AI 原生逻辑**继续。
        # 注：main_display 关闭在 DisplayManager.stop_main_display 已做同
        # 样的旁路，这里只处理 app_displays 卡片。
        # ==============================================================
        if has_card_entry:
            try:
                default_load = bool(
                    (app_displays_cfg.get(package, {}) or {}).get("default_load", False)
                )
            except Exception:
                default_load = False
            if default_load:
                print(f"[ActionHandler] Close '{app_name}' ({package}): "
                      f"default_load=true → BYPASS implant close-path, "
                      f"hand off to AI original logic (no stop_app_display, "
                      f"no display_id recycle, no set_active_display, no "
                      f"am force-stop).")
                return ActionResult(
                    False, False,
                    message=(
                        f"Close skipped (implant bypassed for default_load=true). "
                        f"AI should continue with its original stop logic."
                    ),
                )

        if has_card_entry:
            # ── Path A: card app → reuse red-close button behavior ──
            print(f"[ActionHandler] Close '{app_name}' ({package}): "
                  "configured in app_displays → calling stop_app_display "
                  "(same as traffic-light red close button)")
            try:
                result = dm.stop_app_display(package)
            except Exception as e_stop:
                result = {"success": False, "error": f"stop_app_display exception: {e_stop}"}
            if result.get("success"):
                msg = (f"Closed '{app_name}' card window (same as 🔴 traffic light): "
                       f"scrcpy + VD + display map entry + display id all cleaned.")
                try:
                    get_device_factory().set_active_display(0)
                except Exception:
                    pass
                return ActionResult(True, False, message=msg)
            # Graceful degradation: the card might simply not be running (no
            # entry in _app_display_map) → still try am force-stop so the
            # user still sees the app go away on the phone side.
            print(f"[ActionHandler] Close '{app_name}' ({package}): "
                  f"stop_app_display returned '{result}', falling back to am force-stop.")
            ok, extra = _force_stop_main_display(package)
            return ActionResult(
                bool(ok or result.get("success")),
                False,
                message=(
                    f"stop_app_display: {result}; "
                    f"main-display fallback: {extra}"
                ),
            )

        # ── Path B: NOT a card app → original behavior ──
        print(f"[ActionHandler] Close '{app_name}' ({package}): "
              "no card entry in app_displays → keeping original behavior "
              "(am force-stop only, do not touch windows)")
        ok, msg = _force_stop_main_display(package)
        return ActionResult(ok, False, message=msg if not ok else f"Closed '{app_name}' on main display")

    def _handle_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height, label="tap")

        # Check for sensitive operation
        if "message" in action:
            if not self.confirmation_callback(action["message"]):
                return ActionResult(
                    success=False,
                    should_finish=True,
                    message="User cancelled sensitive operation",
                )

        device_factory = get_device_factory()
        print(f"[ActionHandler] tap → display="
              f"{device_factory.active_display_id or 'main(0)'} ({x},{y})", flush=True)
        device_factory.tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_type(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle text input action."""
        text = action.get("text", "")

        device_factory = get_device_factory()

        # Switch to ADB keyboard
        original_ime = device_factory.detect_and_set_adb_keyboard(self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_switch_delay)

        # Clear existing text and type new text
        device_factory.clear_text(self.device_id)
        time.sleep(TIMING_CONFIG.action.text_clear_delay)

        # Handle multiline text by splitting on newlines
        device_factory.type_text(text, self.device_id)
        time.sleep(TIMING_CONFIG.action.text_input_delay)

        # Restore original keyboard
        device_factory.restore_keyboard(original_ime, self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_restore_delay)

        return ActionResult(True, False)

    def _handle_swipe(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle swipe action."""
        start = action.get("start")
        end = action.get("end")

        if not start or not end:
            return ActionResult(False, False, "Missing swipe coordinates")

        start_x, start_y = self._convert_relative_to_absolute(
            start, width, height, label="swipe-start")
        end_x, end_y = self._convert_relative_to_absolute(
            end, width, height, label="swipe-end")

        device_factory = get_device_factory()
        print(f"[ActionHandler] swipe → display="
              f"{device_factory.active_display_id or 'main(0)'} "
              f"({start_x},{start_y})→({end_x},{end_y})", flush=True)
        device_factory.swipe(start_x, start_y, end_x, end_y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_back(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle back button action."""
        device_factory = get_device_factory()
        device_factory.back(self.device_id)
        return ActionResult(True, False)

    def _handle_home(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle home button action."""
        device_factory = get_device_factory()
        device_factory.home(self.device_id)
        return ActionResult(True, False)

    def _handle_double_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle double tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        device_factory = get_device_factory()
        device_factory.double_tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_long_press(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle long press action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        device_factory = get_device_factory()
        device_factory.long_press(x, y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_wait(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle wait action."""
        duration_str = action.get("duration", "1 seconds")
        try:
            duration = float(duration_str.replace("seconds", "").strip())
        except ValueError:
            duration = 1.0

        time.sleep(duration)
        return ActionResult(True, False)

    def _handle_takeover(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle takeover request (login, captcha, etc.)."""
        message = action.get("message", "User intervention required")
        self.takeover_callback(message)
        return ActionResult(True, False)

    def _handle_note(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle note action (placeholder for content recording)."""
        # This action is typically used for recording page content
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_call_api(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle API call action (placeholder for summarization)."""
        # This action is typically used for content summarization
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_interact(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle interaction request (user choice needed)."""
        # This action signals that user input is needed
        return ActionResult(True, False, message="User interaction required")

    def _send_keyevent(self, keycode: str) -> None:
        """Send a keyevent to the device."""
        from phone_agent.device_factory import DeviceType, get_device_factory
        from phone_agent.hdc.connection import _run_hdc_command

        device_factory = get_device_factory()

        # Handle HDC devices with HarmonyOS-specific keyEvent command
        if device_factory.device_type == DeviceType.HDC:
            hdc_prefix = ["hdc", "-t", self.device_id] if self.device_id else ["hdc"]
            
            # Map common keycodes to HarmonyOS keyEvent codes
            # KEYCODE_ENTER (66) -> 2054 (HarmonyOS Enter key code)
            if keycode == "KEYCODE_ENTER" or keycode == "66":
                _run_hdc_command(
                    hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                    capture_output=True,
                    text=True,
                )
            else:
                # For other keys, try to use the numeric code directly
                # If keycode is a string like "KEYCODE_ENTER", convert it
                try:
                    # Try to extract numeric code from string or use as-is
                    if keycode.startswith("KEYCODE_"):
                        # For now, only handle ENTER, other keys may need mapping
                        if "ENTER" in keycode:
                            _run_hdc_command(
                                hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                                capture_output=True,
                                text=True,
                            )
                        else:
                            # Fallback to ADB-style command for unsupported keys
                            subprocess.run(
                                hdc_prefix + ["shell", "input", "keyevent", keycode],
                                capture_output=True,
                                text=True,
                            )
                    else:
                        # Assume it's a numeric code
                        _run_hdc_command(
                            hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", str(keycode)],
                            capture_output=True,
                            text=True,
                        )
                except Exception:
                    # Fallback to ADB-style command
                    subprocess.run(
                        hdc_prefix + ["shell", "input", "keyevent", keycode],
                        capture_output=True,
                        text=True,
                    )
        else:
            # ADB devices use standard input keyevent command
            cmd_prefix = ["adb", "-s", self.device_id] if self.device_id else ["adb"]
            subprocess.run(
                cmd_prefix + ["shell", "input", "keyevent", keycode],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    @staticmethod
    def _default_confirmation(message: str) -> bool:
        """Default confirmation callback using console input."""
        response = input(f"Sensitive operation: {message}\nConfirm? (Y/N): ")
        return response.upper() == "Y"

    @staticmethod
    def _default_takeover(message: str) -> None:
        """Default takeover callback - no longer blocks, just prints and returns."""
        print(f"{message}\n(no manual wait, continuing to next screenshot)")


# 模型输出里动作调用的起始位置（do( / finish(，排除 my_do( 这类前缀误匹配）
_ACTION_CALL_RE = re.compile(r"(?<![A-Za-z0-9_])(do|finish)\s*\(")


def _extract_action_call(raw: str) -> str | None:
    """从可能夹带噪声的模型输出中提取完整的 do(...) / finish(...) 调用串。

    更换模型后输出常带包装，例如尾部 ``</answer>`` 标签、```python 代码围栏、
    <answer> 标签或前后解释性文字。这里用【字符串感知】的括号配对扫描定位完整
    调用——参数文本里出现括号（如 text="打开（设置）"）也不会截断。
    提取不到完整调用时返回 None。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # 去掉 ```python ... ``` / ``` ... ``` 代码围栏（无闭合围栏则回退全文搜索）
    fence = re.search(r"```(?:[A-Za-z0-9_+-]*)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    m = _ACTION_CALL_RE.search(text)
    if not m:
        return None
    start = m.start()

    # 从调用左括号起做字符串感知的括号配对
    depth = 0
    quote = None       # 非 None 表示当前位于字符串内，值为引号字符（' 或 "）
    triple = False     # 当前字符串是否为三引号
    j = m.end() - 1    # m 的末尾即 '('
    while j < len(text):
        ch = text[j]
        if quote is not None:
            if triple:
                if text.startswith(quote * 3, j):
                    quote, triple = None, False
                    j += 3
                    continue
            else:
                if ch == "\\":
                    j += 2  # 跳过转义对，避免 \" 提前闭合字符串
                    continue
                if ch == quote:
                    quote = None
            j += 1
            continue

        if ch in ("'", '"'):
            triple = text.startswith(ch * 3, j)
            quote = ch
            if triple:
                j += 3
                continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:j + 1].strip()
        j += 1
    return None  # 括号未闭合


def parse_action(response: str) -> dict[str, Any]:
    """
    Parse action from model response.

    Args:
        response: Raw response string from the model.

    Returns:
        Parsed action dictionary.

    Raises:
        ValueError: If the response cannot be parsed.
    """
    print(f"Parsing action: {response}")
    raw = (response or "").strip()
    if not raw:
        raise ValueError("Action response is empty (model returned no do()/finish())")

    # 先剥离模型可能附加的 XML 标签 / 代码围栏 / 解释文字，只留动作调用本身
    call_text = _extract_action_call(raw)
    if call_text is None:
        snippet = raw if len(raw) <= 200 else raw[:200] + "…"
        raise ValueError(f"No do()/finish() call found in action response: {snippet!r}")

    if call_text.startswith('do(action="Type"') or call_text.startswith(
        'do(action="Type_Name"'
    ):
        text = call_text.split("text=", 1)[1][1:-2]
        return {"_metadata": "do", "action": "Type", "text": text}

    if call_text.startswith("do"):
        # Use AST parsing instead of eval for safety
        # Escape special characters (newlines, tabs, etc.) for valid Python syntax
        escaped = call_text.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        try:
            tree = ast.parse(escaped, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Failed to parse do() action {call_text!r}: {e}") from e
        if not isinstance(tree.body, ast.Call):
            raise ValueError(f"Expected a function call, got: {call_text!r}")

        # Extract keyword arguments safely
        action = {"_metadata": "do"}
        for keyword in tree.body.keywords:
            key = keyword.arg
            try:
                action[key] = ast.literal_eval(keyword.value)
            except ValueError as e:
                raise ValueError(
                    f"Failed to parse argument {key!r} in {call_text!r}: {e}") from e
        return action

    if call_text.startswith("finish"):
        return {
            "_metadata": "finish",
            "message": call_text.replace("finish(message=", "")[1:-2],
        }

    raise ValueError(f"Unsupported action call: {call_text!r}")


def do(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'do' actions."""
    kwargs["_metadata"] = "do"
    return kwargs


def finish(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'finish' actions."""
    kwargs["_metadata"] = "finish"
    return kwargs
