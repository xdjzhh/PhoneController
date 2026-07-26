"""
Display Switcher - 屏幕切换指令识别

从 display_config.json 读取已配置虚拟显示的 app 列表，
通过关键词匹配识别用户输入中的屏幕切换意图。
"""

import json
import os
import re
from typing import Optional, Tuple


class DisplaySwitcher:
    """屏幕切换指令识别器"""

    MAIN_DISPLAY_KEYWORDS = [
        "主屏",
        "主屏幕",
        "桌面",
        "主屏上",
        "主屏幕上",
        "回到主屏",
        "回到桌面",
        "切换到主屏",
        "切换到桌面",
        "返回主屏",
        "返回桌面",
    ]

    SWITCH_VERBS = [
        "切换到",
        "操作",
        "打开",
        "在",
        "去",
        "到",
    ]

    def __init__(self, config_path=None):
        self._script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if config_path is None:
            config_path = os.path.join(self._script_dir, "scrcpycontrol", "display_config.json")
        self.config_path = config_path
        self._app_list = None
        self._app_names_lower = {}

    def _load_app_list(self):
        """从 display_config.json 加载已配置虚拟显示的 app 列表"""
        if self._app_list is not None:
            return

        self._app_list = {}
        self._app_names_lower = {}

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            app_displays = config.get("app_displays", {})
            for app_package, app_cfg in app_displays.items():
                window_title = app_cfg.get("window_title", app_package)
                # 存储包名 → 窗口标题的映射
                self._app_list[app_package] = window_title
                # 存储小写名称 → 包名的映射，用于模糊匹配
                self._app_names_lower[window_title.lower()] = app_package
                self._app_names_lower[app_package.lower()] = app_package
        except Exception as e:
            print(f"[DisplaySwitcher] Failed to load display config: {e}")

    def reload_config(self):
        """重新加载配置"""
        self._app_list = None
        self._app_names_lower = {}
        self._load_app_list()

    @property
    def available_apps(self):
        """获取所有可用的虚拟显示 app 列表"""
        self._load_app_list()
        return list(self._app_list.keys())

    def detect_switch_intent(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        检测文本中的屏幕切换意图。

        Args:
            text: 用户输入文本

        Returns:
            (target_type, target) 元组:
            - target_type: "main" 表示切换到主屏, "app" 表示切换到 app, None 表示无切换意图
            - target: app包名（当 target_type="app" 时），None（当 target_type="main" 或 None 时）
        """
        self._load_app_list()

        if not text or not self._app_list:
            return None, None

        text = text.strip()

        # 1. 检测是否切换到主屏
        for keyword in self.MAIN_DISPLAY_KEYWORDS:
            if keyword in text:
                return "main", None

        # 2. 检测是否切换到某个 app
        # 方式：遍历所有已配置的 app 名称，检查是否出现在文本中
        for app_package, window_title in self._app_list.items():
            # 检查窗口标题是否出现在文本中（如"微信"、"淘宝"）
            if window_title in text:
                # 进一步检查是否有切换动词前缀
                for verb in self.SWITCH_VERBS:
                    if verb + window_title in text:
                        return "app", app_package
                # 即使没有切换动词，如果文本很短且只有 app 名，也认为是切换
                if text == window_title or text.startswith(window_title):
                    return "app", app_package

            # 检查包名是否出现在文本中
            if app_package in text:
                for verb in self.SWITCH_VERBS:
                    if verb + app_package in text:
                        return "app", app_package

        # 3. 使用正则匹配更复杂的模式
        # 模式：切换到/操作/在 + App名 + 后面的内容
        for verb in self.SWITCH_VERBS:
            pattern = re.compile(rf"{re.escape(verb)}([\u4e00-\u9fa5a-zA-Z0-9_]+)")
            matches = pattern.findall(text)
            for match in matches:
                match_lower = match.lower()
                if match_lower in self._app_names_lower:
                    return "app", self._app_names_lower[match_lower]
                # 模糊匹配：检查 app 名称是否是匹配词的一部分
                for name_lower, pkg in self._app_names_lower.items():
                    if name_lower and name_lower in match_lower:
                        return "app", pkg

        return None, None

    def is_main_display_intent(self, text: str) -> bool:
        """检测是否是切换到主屏的意图"""
        target_type, _ = self.detect_switch_intent(text)
        return target_type == "main"

    def get_app_display_target(self, text: str) -> Optional[str]:
        """获取要切换到的 app 包名"""
        target_type, target = self.detect_switch_intent(text)
        if target_type == "app":
            return target
        return None


_switcher = None


def get_display_switcher(config_path=None):
    """获取全局 DisplaySwitcher 单例"""
    global _switcher
    if _switcher is None:
        _switcher = DisplaySwitcher(config_path)
    return _switcher
