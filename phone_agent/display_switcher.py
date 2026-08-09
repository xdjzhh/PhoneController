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
        "进入",
        "进",
        "切到",
        "启动",
        "开",
    ]

    # ═══════════════════════════════════════════════════════════════════
    # 同义词映射（用户常用简称）：
    #   关键词 → 匹配对应的 app_package 或 窗口标题
    #   命中关键词后，直接走"匹配动词+窗口标题"等效逻辑，用户不需要说全称。
    # ═══════════════════════════════════════════════════════════════════
    APP_ALIASES = {
        # 高德地图（导航）
        "高德": "高德地图",
        "高德地图": "高德地图",
        "地图": "高德地图",  # 用户说"打开地图"默认=高德地图（配置里有导航几何信息）
        "导航": "高德地图",  # 默认"导航"=高德地图
        "开始导航": "高德地图",
        "导航一下": "高德地图",
        "带我去": "高德地图",
        "导航去": "高德地图",
        # 百度地图
        "百度": "百度地图",
        "百度地图": "百度地图",
        # 微信
        "微信": "微信",
        "微": "微信",
        # 抖音
        "抖音": "抖音",
        "抖": "抖音",
        # 天气
        "天气": "天气",
        "天气预报": "天气",
    }

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

    def _resolve_alias_to_window_title(self, word: str) -> Optional[str]:
        """
        通过 APP_ALIASES 同义词表把用户的简称（"高德"/"导航"/"微信"）
        映射成 display_config.json 里的正式窗口标题（"高德地图"等）。
        返回 None 表示不是任何已知别名。
        """
        # 优先精确匹配
        if word in self.APP_ALIASES:
            return self.APP_ALIASES[word]
        # 其次：检查关键词是否出现在 word 里（"我要高德导航一下"→"高德"命中）
        for alias, title in self.APP_ALIASES.items():
            if alias and alias in word:
                return title
        return None

    def _find_package_by_window_title(self, window_title: str) -> Optional[str]:
        """通过窗口标题找 app_package；找不到尝试 APP_ALIASES 同义词反查再找"""
        if not self._app_list:
            return None
        # 精确匹配
        for pkg, wt in self._app_list.items():
            if wt == window_title:
                return pkg
        # 同义词表的 title → display_config 里实际 title 模糊匹配
        for pkg, wt in self._app_list.items():
            if window_title and (window_title in wt or wt in window_title):
                return pkg
        return None

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
        # 2.1 遍历所有已配置的 app 名称，检查是否出现在文本中
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

        # 2.2 使用 APP_ALIASES 同义词联想（关键：解决"高德"/"导航"/"微信"等简称匹配不到的问题）
        for verb in self.SWITCH_VERBS:
            if verb in text:
                # 取动词之后的文本片段（"切换到高德地图导航回家" → "高德地图导航回家"）
                try:
                    tail = text.split(verb, 1)[1]
                except Exception:
                    continue
                if not tail:
                    continue
                # 先用 APP_ALIASES 精确/包含匹配找同义词对应的标准 window_title
                for alias, std_title in self.APP_ALIASES.items():
                    if alias and alias in tail:
                        pkg = self._find_package_by_window_title(std_title)
                        if pkg is not None:
                            return "app", pkg

        # 2.3 文本完全不含切换动词，但只有同义词（例如用户直接说"高德"、"导航"、"微信"）
        for alias, std_title in self.APP_ALIASES.items():
            if text == alias or (text.startswith(alias) and len(text) <= max(6, len(alias) + 4)):
                pkg = self._find_package_by_window_title(std_title)
                if pkg is not None:
                    return "app", pkg

        # 2.4 使用正则匹配更复杂的模式（兜底）
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
                # 再走一次同义词联想（同义词精确匹配）
                std_title = self._resolve_alias_to_window_title(match)
                if std_title:
                    pkg = self._find_package_by_window_title(std_title)
                    if pkg is not None:
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
