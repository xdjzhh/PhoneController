"""Main PhoneAgent class for orchestrating phone automation."""

import json
import threading
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from scrcpycontrol.display_interface import get_display_manager
from phone_agent.actions import ActionHandler
from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.device_factory import get_device_factory
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder


@dataclass
class AgentConfig:
    """Configuration for the PhoneAgent."""

    max_steps: int = 100
    device_id: str | None = None
    lang: str = "cn"
    system_prompt: str | None = None
    verbose: bool = True

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: dict[str, Any] | None
    thinking: str
    message: str | None = None


class PhoneAgent:
    """
    AI-powered agent for automating Android phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks.

    Args:
        model_config: Configuration for the AI model.
        agent_config: Configuration for the agent behavior.
        confirmation_callback: Optional callback for sensitive action confirmation.
        takeover_callback: Optional callback for takeover requests.

    Example:
        >>> from phone_agent import PhoneAgent
        >>> from phone_agent.model import ModelConfig
        >>>
        >>> model_config = ModelConfig(base_url="http://localhost:8000/v1")
        >>> agent = PhoneAgent(model_config)
        >>> agent.run("Open WeChat and send a message to John")
    """

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        agent_config: AgentConfig | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or AgentConfig()

        self.model_client = ModelClient(self.model_config)
        self.action_handler = ActionHandler(
            device_id=self.agent_config.device_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: list[dict[str, Any]] = []
        self._step_count = 0
        self._stop_event = threading.Event()

    def stop(self):
        """Request the agent to stop running the current task."""
        self._stop_event.set()

    @property
    def is_running(self):
        return not self._stop_event.is_set() and self._step_count > 0

    def run(self, task: str) -> str:
        """
        Run the agent to complete a task.

        Args:
            task: Natural language description of the task.

        Returns:
            Final message from the agent.
        """
        self._stop_event.clear()
        self._context = []
        self._step_count = 0

        self._check_and_switch_display(task)

        # ── 前置独立检测 #2：关闭 APP 意图（仿切换屏幕的抽象方法模式）
        #    用户原话：「在ActionHandler中增加close是完全无效的，只有像切换屏幕一样，
        #    抽象出一个方法用于关闭检测才能完成」
        #    命中：直接关闭（虚拟卡片→复用红绿灯红灯；非卡片→保留原 am force-stop 逻辑）
        #    并立刻结束 run，不再进入 AI 循环。
        if self._check_and_close_app(task):
            return f"已完成关闭操作：{task}"

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if self._stop_event.is_set():
            return "Task stopped by user"

        if result.finished:
            return result.message or "Task completed"

        # Continue until finished, max steps reached, or stopped
        while self._step_count < self.agent_config.max_steps:
            if self._stop_event.is_set():
                return "Task stopped by user"
            result = self._execute_step(is_first=False)

            if self._stop_event.is_set():
                return "Task stopped by user"

            if result.finished:
                return result.message or "Task completed"

        return "Max steps reached"

    def step(self, task: str | None = None) -> StepResult:
        """
        Execute a single step of the agent.

        Useful for manual control or debugging.

        Args:
            task: Task description (only needed for first step).

        Returns:
            StepResult with step details.
        """
        is_first = len(self._context) == 0

        if is_first and not task:
            raise ValueError("Task is required for the first step")

        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._stop_event.clear()
        self._context = []
        self._step_count = 0
        device_factory = get_device_factory()
        device_factory.reset_display()

    def _check_and_switch_display(self, text: str) -> None:
        """
        Check user input for display switch intent and switch if needed.

        If the target app's virtual display is not running, start it first.

        Args:
            text: User input text to analyze.
        """
        if not text:
            return

        try:
            from phone_agent.display_switcher import get_display_switcher
            switcher = get_display_switcher()
            target_type, target = switcher.detect_switch_intent(text)

            if target_type == "main":
                device_factory = get_device_factory()
                device_factory.reset_display()
                print("[Agent] Switched to main display (detected from user input)")
            elif target_type == "app" and target:
                try:
                    dm = get_display_manager()
                    displays = dm.list_displays().get("displays", [])
                    found = False
                    for d in displays:
                        if d.get("app_package") == target:
                            android_display_id = d.get("android_display_id")
                            if android_display_id is not None:
                                device_factory = get_device_factory()
                                device_factory.set_active_display(android_display_id)
                                print(f"[Agent] Switched to virtual display for {target} (id={android_display_id})")
                                found = True
                            break

                    if not found:
                        print(f"[Agent] Virtual display for {target} not running, starting it...")
                        result = dm.start_app_display(target)
                        if result.get("success"):
                            android_display_id = result.get("android_display_id")
                            if android_display_id is not None:
                                device_factory = get_device_factory()
                                device_factory.set_active_display(android_display_id)
                                print(f"[Agent] Started and switched to virtual display for {target} (id={android_display_id})")
                            else:
                                print(f"[Agent] Virtual display started but no display ID found")
                        else:
                            print(f"[Agent] Failed to start virtual display for {target}: {result.get('error')}")
                except Exception as e:
                    print(f"[Agent] Failed to switch display via user input: {e}")
                    import traceback
                    traceback.print_exc()
        except Exception as e:
            print(f"[Agent] Display switch detection failed: {e}")

    def _check_and_close_app(self, text: str) -> bool:
        """
        「关闭应用」前置独立检测 —— 完全仿照 _check_and_switch_display 的模式。
        不依赖 AI 输出 Close action，直接解析用户 task 命中关闭意图即执行。

        Returns:
            True  = 命中关闭意图并已执行（无论成功失败都视为命令已处理），
                    agent.run 应直接 return 结束，不再进入 AI 循环。
            False = 未命中任何关闭意图，继续原 AI 流程。
        """
        if not text:
            return False

        try:
            from phone_agent.display_switcher import get_display_switcher
            switcher = get_display_switcher()
            target_type, target_pkg = switcher.detect_close_intent(text)
            if target_type != "close_app" or not target_pkg:
                return False  # 不是关闭意图，交给 AI

            print(f"[Agent] Close-app intent detected: target={target_pkg}; "
                  f"will dispatch same-as-traffic-light path vs original force-stop path")

            # ── Step 1: 判断这个 package 是否在 display_config → app_displays 里 ──
            #    直接复用 DisplaySwitcher 已加载好的 available_apps（它的实现
            #    就是 display_config.app_displays.keys()，100% 一致、无歧义）。
            #    不再绕 ConfigManager，避免之前 ConfigManager().get_config()
            #    API 名写错 → 空 dict → 误判 Path B → force-stop 的问题。
            has_card_entry = target_pkg in switcher.available_apps

            # ── Step 2: 按两条路径执行关闭 ──
            if has_card_entry:
                # Path A = 复用红绿灯红灯关闭方式
                try:
                    from scrcpycontrol.display_interface import get_display_manager
                    dm = get_display_manager()
                    result = dm.stop_app_display(target_pkg)
                    if result.get("success"):
                        print(f"[Agent][Close] '{target_pkg}' card closed via stop_app_display "
                              f"(same as 🔴 traffic light red button): "
                              f"scrcpy window + decorator + virtual display + display map entry "
                              f"all cleaned, display_id returned to pool")
                        try:
                            df = get_device_factory()
                            df.reset_display()
                        except Exception:
                            pass
                        return True
                    else:
                        print(f"[Agent][Close] stop_app_display('{target_pkg}') returned "
                              f"non-success ({result.get('error','')}). "
                              f"Falling back to original behavior (am force-stop).")
                except Exception as e_stop:
                    print(f"[Agent][Close] stop_app_display exception (fallback force-stop): {e_stop}")
                    import traceback
                    traceback.print_exc()

                # Path A 降级：卡片未运行/报错 → 仍执行原逻辑 am force-stop 保证一致性
                try:
                    ok, msg = self._force_stop_android_app(target_pkg)
                    _info = " :".join(filter(None, [target_pkg, msg])) if msg else target_pkg
                    print(f"[Agent][Close] Path A fallback am force-stop '{_info}'")
                    if not ok:
                        print(f"[Agent][Close] am force-stop warning: {msg}")
                except Exception as e_fs:
                    print(f"[Agent][Close] Path A fallback force-stop failed: {e_fs}")
                return True
            else:
                # Path B = 非卡片应用 → 保持原逻辑不变（直接 am force-stop 主屏路径，不动窗口）
                try:
                    ok, msg = self._force_stop_android_app(target_pkg)
                    if ok:
                        print(f"[Agent][Close] '{target_pkg}' not in app_displays (no virtual card). "
                              f"Keeping original behavior: am force-stop succeeded.")
                    else:
                        print(f"[Agent][Close] '{target_pkg}' not in app_displays. Original behavior "
                              f"am force-stop executed but failed: {msg}")
                except Exception as e_b:
                    print(f"[Agent][Close] Path B (original am force-stop) exception: {e_b}")
                    import traceback
                    traceback.print_exc()
                return True

        except Exception as e_outer:
            print(f"[Agent] Close-app pre-check failed (continuing with AI loop): {e_outer}")
            import traceback
            traceback.print_exc()
            return False

    def _force_stop_android_app(self, package: str):
        """原逻辑：主屏上关闭 app = adb shell am force-stop <package>。
        返回 (ok: bool, message: str)。不动任何窗口。"""
        if not package:
            return False, "empty package"
        try:
            import adbutils
            device_id = getattr(self, "agent_config", None) and self.agent_config.device_id
            if device_id:
                try:
                    dev = adbutils.adb.device(device_id)
                    dev.shell(f"am force-stop {package}", timeout=10)
                    return True, f"am force-stop {package} via device_id={device_id}"
                except Exception:
                    pass
            devices = adbutils.adb.device_list()
            if devices:
                dev = devices[0]
                dev.shell(f"am force-stop {package}", timeout=10)
                return True, f"am force-stop {package} via first_device={dev.serial}"
            return False, "no adb device connected"
        except Exception as e:
            return False, f"_force_stop_android_app error: {e}"

    def _execute_step(
        self, user_prompt: str | None = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1

        # Capture current screen state
        device_factory = get_device_factory()
        screenshot = device_factory.get_screenshot(self.agent_config.device_id)
        current_app = device_factory.get_current_app(self.agent_config.device_id)
        print("+++++++++++++++current_app" + current_app)

        # Build messages
        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self.agent_config.system_prompt)
            )

            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = f"{user_prompt}\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = f"** Screen Info **\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        # Get model response
        try:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print(f"💭 {msgs['thinking']}:")
            print("-" * 50)
            response = self.model_client.request(self._context)
            # response = self.model_client.local_ollama_request(self._context)
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message=f"Model error: {e}",
            )

        # Parse action from response
        try:
            action = parse_action(response.action)
        except ValueError:
            if self.agent_config.verbose:
                traceback.print_exc()
            action = finish(message=response.action)

        if self.agent_config.verbose:
            # Print thinking process
            print("-" * 50)
            print(f"🎯 {msgs['action']}:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
        try:
            result = self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            result = self.action_handler.execute(
                finish(message=str(e)), screenshot.width, screenshot.height
            )

        # Add assistant response to context
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think>{response.thinking}</think><answer>{response.action}</answer>"
            ))

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        return StepResult(
            success=result.success,
            finished=finished,
            action=action,
            thinking=response.thinking,
            message=result.message or action.get("message"),
        )

    @property
    def context(self) -> list[dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
