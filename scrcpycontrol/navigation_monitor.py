"""
Navigation Monitor - 检测地图app是否处于导航状态

检测方式：
1. 方案1（主）：通过adb shell dumpsys notification检测导航前台通知
2. 方案2（辅助）：通过adb shell dumpsys activity activities检测前台导航Activity
组合两种方式提高准确率。
"""

import os
import re
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple


class NavigationState:
    """导航状态数据"""
    def __init__(self):
        self.is_navigating: bool = False
        self.app_package: Optional[str] = None
        self.app_name: Optional[str] = None
        self.notification_title: str = ""
        self.notification_text: str = ""
        self.current_activity: str = ""
        self.detect_source: str = ""  # "notification" / "activity" / "both"
        self.start_time: float = 0
        self.last_update: float = 0

    def __repr__(self):
        return f"NavigationState(navigating={self.is_navigating}, app={self.app_name}, source={self.detect_source})"


# 地图app配置
MAP_APPS: Dict[str, Dict] = {
    "com.autonavi.minimap": {
        "name": "高德地图",
        # 导航通知关键词
        "title_keywords": ["导航中", "导航", "路线规划", "路线导航", "开始导航"],
        "text_keywords": ["剩余", "公里", "米后", "转向", "预计到达", "到达", "分钟", "导航", "前方"],
        # 导航Activity关键词（Activity类名中包含这些关键词）
        "activity_keywords": ["navi", "Navi", "drive", "Drive", "route", "Route", "guidance", "Guidance",
                              "map", "Map", "track", "Track"],
        # 排除的Activity（非导航状态的地图页面）
        "activity_exclude": ["main", "Main", "home", "Home", "splash", "Splash", "search", "Search"],
        # ongoing通知必须为true（前台服务）
        "require_ongoing": True,
    },
    "com.baidu.BaiduMap": {
        "name": "百度地图",
        "title_keywords": ["导航中", "导航", "路线", "开始导航"],
        "text_keywords": ["剩余", "公里", "米", "转向", "预计", "到达", "分钟", "前方"],
        "activity_keywords": ["navi", "Navi", "BNavi", "route", "Route", "guide", "Guide", "drive", "Drive"],
        "activity_exclude": ["main", "Main", "home", "Home", "splash", "Splash"],
        "require_ongoing": True,
    },
}


class NavigationMonitor:
    """导航状态监听器"""

    def __init__(self, device_serial: Optional[str] = None, adb_path: str = "adb",
                 poll_interval: float = 3.0):
        self.device_serial = device_serial
        self.adb_path = adb_path
        self.poll_interval = poll_interval

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state = NavigationState()
        self._on_start_callbacks: List[Callable[[NavigationState], None]] = []
        self._on_end_callbacks: List[Callable[[NavigationState], None]] = []
        self._on_update_callbacks: List[Callable[[NavigationState], None]] = []
        self._lock = threading.Lock()
        self._debug = True  # 开启调试日志

    def _log(self, msg: str):
        if self._debug:
            print(f"[NavMonitor] {msg}", flush=True)

    def _adb_cmd(self, *args) -> List[str]:
        cmd = [self.adb_path]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += list(args)
        return cmd

    def _run_adb_shell(self, *args) -> str:
        """运行adb shell命令并返回输出"""
        try:
            cmd = self._adb_cmd("shell", *args)
            result = subprocess.run(
                cmd, capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            return result.stdout.decode('utf-8', errors='replace')
        except Exception as e:
            self._log(f"ADB command failed: {e}")
            return ""

    def _fetch_notifications_raw(self) -> str:
        """获取dumpsys notification原始输出"""
        return self._run_adb_shell("dumpsys", "notification", "--noredact")

    def _fetch_current_activity(self) -> Tuple[Optional[str], Optional[str]]:
        """
        获取当前前台Activity。
        返回: (package_name, activity_name) 或 (None, None)
        """
        try:
            output = self._run_adb_shell("dumpsys", "activity", "activities")
            # 查找mResumedActivity
            # 格式示例: mResumedActivity: ActivityRecord{... u0 com.autonavi.minimap/.navi.DriveRouteMapActivity t1234}
            resumed_match = re.search(r'mResumedActivity:\s*ActivityRecord\{[^}]*\s+([^/\s]+)/([^}\s]+)', output)
            if resumed_match:
                return (resumed_match.group(1), resumed_match.group(2))
            # 也尝试mFocusedApp
            focus_match = re.search(r'mFocusedApp=ActivityRecord\{[^}]*\s+([^/\s]+)/([^}\s]+)', output)
            if focus_match:
                return (focus_match.group(1), focus_match.group(2))
        except Exception as e:
            self._log(f"Failed to get current activity: {e}")
        return (None, None)

    def _extract_notification_fields(self, record: str) -> Tuple[str, str, bool]:
        """
        从NotificationRecord文本中提取(title, text, is_ongoing)。
        支持多种dumpsys格式。
        """
        title = ""
        text = ""
        is_ongoing = False

        # 检测ongoing - 多种格式
        # 格式1: ongoing=true
        ongoing_match = re.search(r'(?:^|\s|,)ongoing\s*=\s*(true|false)', record, re.IGNORECASE | re.MULTILINE)
        if ongoing_match:
            is_ongoing = ongoing_match.group(1).lower() == 'true'
        # 格式2: flags=0x... (检查FLAG_ONGOING_EVENT = 0x02)
        if not is_ongoing:
            flags_match = re.search(r'flags=0x([0-9a-fA-F]+)', record)
            if flags_match:
                flags = int(flags_match.group(1), 16)
                if flags & 0x02:  # FLAG_ONGOING_EVENT
                    is_ongoing = True

        # 提取标题 - 多种格式
        # 格式1: android.title=String (xxx)
        title_patterns = [
            r'android\.title=String\s*(?:\([^)]*\))?\s*"([^"]*)"',
            r'android\.title=String\s*(.+?)(?:\n|$)',
            r'tickerText=String\s*(?:\([^)]*\))?\s*"([^"]*)"',
            r'tickerText=String\s*(.+?)(?:\n|$)',
            r'android:title="([^"]*)"',
            r"android\.title='([^']*)'",
        ]
        for pat in title_patterns:
            m = re.search(pat, record)
            if m:
                title = m.group(1).strip().strip('"').strip("'")
                if title:
                    break

        # 提取文本 - 多种格式
        text_patterns = [
            r'android\.text=String\s*(?:\([^)]*\))?\s*"([^"]*)"',
            r'android\.text=String\s*(.+?)(?:\n|$)',
            r'android:text="([^"]*)"',
            r"android\.text='([^']*)'",
            r'android\.bigText=String\s*(?:\([^)]*\))?\s*"([^"]*)"',
            r'android\.bigText=String\s*(.+?)(?:\n|$)',
        ]
        for pat in text_patterns:
            m = re.search(pat, record)
            if m:
                text = m.group(1).strip().strip('"').strip("'")
                if text:
                    break

        # 如果还是空，尝试提取所有可见文本行
        if not title and not text:
            # 查找类似"导航中"、"剩余XX公里"的中文文本
            chinese_match = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9\s\.]+(?:导航|剩余|公里|米|分钟|到达|转向)[\u4e00-\u9fa5a-zA-Z0-9\s\.]*', record)
            if chinese_match:
                text = " ".join(chinese_match[:2])

        return (title, text, is_ongoing)

    def _check_navigation_notification(self, output: str) -> Optional[Tuple[str, str, str, str]]:
        """
        从dumpsys notification输出中检测导航通知。
        返回: (package, app_name, title, text) 或 None
        """
        # 按StatusBarNotification分割（比NotificationRecord更可靠）
        records = re.split(r'StatusBarNotification\(|NotificationRecord\(', output)
        for record in records[1:]:
            # 提取包名 - 多种格式
            pkg = None
            pkg_patterns = [
                r'pkg=([^\s,)}]+)',
                r'package=([^\s,)}]+)',
                r'\|([^|]+)\|',  # key=0|com.xxx|...
            ]
            for pat in pkg_patterns:
                pkg_match = re.search(pat, record)
                if pkg_match:
                    candidate = pkg_match.group(1).strip()
                    if candidate in MAP_APPS or candidate.startswith("com."):
                        pkg = candidate
                        break

            if not pkg or pkg not in MAP_APPS:
                continue

            app_cfg = MAP_APPS[pkg]
            title, text, is_ongoing = self._extract_notification_fields(record)
            combined_text = f"{title} {text}"

            if self._debug and combined_text.strip():
                self._log(f"  Notif from {pkg}: ongoing={is_ongoing}, title='{title[:30]}', text='{text[:50]}'")

            # 如果不要求ongoing，或者确实是ongoing
            if app_cfg["require_ongoing"] and not is_ongoing:
                # 特殊情况：有些导航通知可能ongoing检测失败，但文本明显是导航
                if "导航中" not in combined_text and "剩余" not in combined_text:
                    continue

            # 检查关键词匹配
            all_keywords = app_cfg["title_keywords"] + app_cfg["text_keywords"]
            hit_count = sum(1 for kw in all_keywords if kw in combined_text)

            # 明确的导航特征：包含"导航中"或"开始导航"，或者同时包含"剩余"+"公里/米"
            is_navi = False
            if "导航中" in combined_text or "开始导航" in combined_text:
                is_navi = True
            elif ("剩余" in combined_text and ("公里" in combined_text or "米" in combined_text)):
                is_navi = True
            elif hit_count >= 2 and ("导航" in combined_text or "公里" in combined_text or "米后" in combined_text):
                is_navi = True

            if is_navi:
                self._log(f"  -> NAVIGATION DETECTED via notification for {pkg}!")
                return (pkg, app_cfg["name"], title, text)

        return None

    def _check_navigation_activity(self, pkg: str, activity: str) -> bool:
        """
        检测当前Activity是否是导航界面。
        """
        if not pkg or pkg not in MAP_APPS:
            return False

        app_cfg = MAP_APPS[pkg]
        act_lower = activity.lower()
        act_exact = activity

        # 检查排除关键词
        for ex_kw in app_cfg.get("activity_exclude", []):
            if ex_kw in act_exact:
                return False

        # 检查导航关键词
        for kw in app_cfg.get("activity_keywords", []):
            if kw in activity:
                self._log(f"Activity '{activity}' matches nav keyword '{kw}'")
                return True

        # 额外检查：Activity名中包含navi（导航通用关键词）
        if "navi" in act_lower and "main" not in act_lower and "home" not in act_lower:
            self._log(f"Activity '{activity}' matches generic nav keyword")
            return True

        return False

    def _poll_loop(self):
        """后台轮询循环"""
        consecutive_miss = 0  # 连续检测不到导航的次数
        last_navi_pkg = None

        while self._running:
            try:
                navi_by_notif = None
                navi_by_activity = None
                current_pkg = None
                current_act = ""
                notif_title = ""
                notif_text = ""

                # 方式1：检测前台Activity（辅助）
                cur_pkg, cur_act = self._fetch_current_activity()
                if cur_pkg and cur_pkg in MAP_APPS:
                    current_pkg = cur_pkg
                    current_act = cur_act
                    if self._check_navigation_activity(cur_pkg, cur_act):
                        navi_by_activity = (cur_pkg, MAP_APPS[cur_pkg]["name"], "", "")

                # 方式2：检测导航通知（主）
                notif_output = self._fetch_notifications_raw()
                if notif_output:
                    navi_by_notif = self._check_navigation_notification(notif_output)
                    if navi_by_notif:
                        notif_title = navi_by_notif[2]
                        notif_text = navi_by_notif[3]

                # 综合判定
                navi_result = None
                detect_source = ""

                # 通知检测优先级更高（更准确）
                if navi_by_notif:
                    navi_result = navi_by_notif
                    detect_source = "notification"
                    # 如果Activity也检测到，标记为both
                    if navi_by_activity and navi_by_activity[0] == navi_by_notif[0]:
                        detect_source = "both"
                elif navi_by_activity:
                    navi_result = navi_by_activity
                    detect_source = "activity"

                with self._lock:
                    now = time.time()
                    if navi_result:
                        pkg, app_name, title, text = navi_result
                        if navi_by_notif:
                            notif_title = title
                            notif_text = text
                        just_started = (not self._state.is_navigating) or (self._state.app_package != pkg)

                        self._state.is_navigating = True
                        self._state.app_package = pkg
                        self._state.app_name = app_name
                        self._state.notification_title = notif_title
                        self._state.notification_text = notif_text
                        self._state.current_activity = current_act
                        self._state.detect_source = detect_source
                        self._state.last_update = now
                        if just_started:
                            self._state.start_time = now
                            self._log(f"Navigation STARTED for {app_name} (source={detect_source})")
                            self._fire_callbacks(self._on_start_callbacks, self._state)
                        else:
                            self._fire_callbacks(self._on_update_callbacks, self._state)
                        consecutive_miss = 0
                        last_navi_pkg = pkg
                    else:
                        # 只有连续多次检测不到才判定为结束（防止单次检测失败误判）
                        if self._state.is_navigating:
                            consecutive_miss += 1
                            self._log(f"Navigation not detected, miss count={consecutive_miss}/3")
                            if consecutive_miss >= 3:
                                # 导航结束
                                ended_state = NavigationState()
                                ended_state.is_navigating = False
                                ended_state.app_package = self._state.app_package
                                ended_state.app_name = self._state.app_name
                                ended_state.notification_title = self._state.notification_title
                                ended_state.notification_text = self._state.notification_text
                                ended_state.current_activity = self._state.current_activity
                                ended_state.start_time = self._state.start_time
                                ended_state.last_update = now

                                self._state.is_navigating = False
                                self._state.app_package = None
                                self._state.app_name = None
                                self._state.notification_title = ""
                                self._state.notification_text = ""
                                self._state.current_activity = ""
                                self._state.detect_source = ""

                                self._log(f"Navigation ENDED for {ended_state.app_name}")
                                self._fire_callbacks(self._on_end_callbacks, ended_state)
                                consecutive_miss = 0
                                last_navi_pkg = None
                        else:
                            consecutive_miss = 0

            except Exception as e:
                self._log(f"Poll error: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(self.poll_interval)

    def _fire_callbacks(self, callbacks: List[Callable], state: NavigationState):
        """安全触发回调"""
        for cb in callbacks:
            try:
                cb(state)
            except Exception as e:
                self._log(f"Callback error: {e}")

    def start(self):
        """启动监听器"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self._log("Started monitoring navigation state (notification + activity detection)")

    def stop(self):
        """停止监听器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._log("Stopped")

    def get_state(self) -> NavigationState:
        """获取当前导航状态"""
        with self._lock:
            state = NavigationState()
            state.is_navigating = self._state.is_navigating
            state.app_package = self._state.app_package
            state.app_name = self._state.app_name
            state.notification_title = self._state.notification_title
            state.notification_text = self._state.notification_text
            state.current_activity = self._state.current_activity
            state.detect_source = self._state.detect_source
            state.start_time = self._state.start_time
            state.last_update = self._state.last_update
            return state

    def on_navigation_start(self, callback: Callable[[NavigationState], None]):
        """注册导航开始回调"""
        self._on_start_callbacks.append(callback)

    def on_navigation_end(self, callback: Callable[[NavigationState], None]):
        """注册导航结束回调"""
        self._on_end_callbacks.append(callback)

    def on_navigation_update(self, callback: Callable[[NavigationState], None]):
        """注册导航状态更新回调"""
        self._on_update_callbacks.append(callback)


# 全局单例
_monitor: Optional[NavigationMonitor] = None


def get_navigation_monitor(device_serial: Optional[str] = None, adb_path: str = "adb",
                          poll_interval: float = 2.0) -> NavigationMonitor:
    """获取全局NavigationMonitor单例"""
    global _monitor
    if _monitor is None:
        _monitor = NavigationMonitor(device_serial, adb_path, poll_interval)
    return _monitor
