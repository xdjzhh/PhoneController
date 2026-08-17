"""
ConfigManager: display_config.json 的唯一真相源（Single Source of Truth）。

设计目标：
  1. 进程内单例：同一个 Python 进程中，所有模块共享同一份内存缓存对象。
  2. 多进程安全：每次写磁盘前，先重新读取磁盘上最新的完整 JSON，
     再用本进程缓存 self._config 合并（以缓存为准，磁盘上其他进程写入的节
     只要本进程未维护则自动保留），彻底消除"写老缓存覆盖新值"的竞态。
  3. Debounce 合并写：1 秒内多次 update_section / set_section 调用合并为
     一次真正的磁盘写入，节省 IO。
  4. 统一入口：所有模块（DisplayManager / Qt 通知窗口 / Qt 思考窗口 /
     display_server 全局 save_config）全部必须通过本类读写 JSON，
     禁止自行 open/json.dump/json.load。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from typing import Any, Dict, Optional


# ============================================================ Owner 常量
# 每个窗口配置节（main_display、notification_panel、app_displays.* 等）必须带有
# `_owner` 字段明确标记归谁维护；代码里**不再硬编码任何节的归属映射**，完全
# 由配置文件里 `_owner` 的值决定哪个 manager 有权限写该节。
# 新增任何窗口/app：只需要在 display_config.json 里给该节加上对应 `_owner` 即可。
OWNER_DISPLAY_MANAGER = "display_manager"
OWNER_BACKDROP_MANAGER = "backdrop_manager"
OWNER_KEY = "_owner"


class ConfigManager:
    """进程内单例：管理 scrcpycontrol/display_config.json 的加载 / 合并 / 写入。"""

    _instance: Optional['ConfigManager'] = None
    _singleton_lock = threading.Lock()  # 保护单例创建

    # ------------------------------------------------------------------ init
    def __init__(self, config_path: Optional[str] = None):
        self._rw_lock = threading.RLock()  # 保护 self._config 的所有读写
        self._save_timer: Optional[threading.Timer] = None
        self._save_timer_lock = threading.Lock()
        self._last_save_ts = 0.0
        self._my_owner: Optional[str] = None  # 当前进程身份（display_manager / backdrop_manager）

        # ---- 自动检测 config_path ----
        if config_path is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))  # scrcpycontrol/
            config_path = os.path.join(script_dir, 'display_config.json')
        self._config_path: str = os.path.abspath(config_path)

        # ---- 初始化缓存：必须先有一个空 dict，即使文件不存在 ----
        self._config: Dict[str, Any] = {}
        if os.path.exists(self._config_path) and os.path.isfile(self._config_path):
            try:
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
            except Exception as e:
                print(f"[ConfigManager] INIT load failed: {e}", flush=True)
                traceback.print_exc()
                self._config = {}

        # ---- baseline snapshot：记录「上次 save 之后」缓存的状态。
        #   _do_save 时，对比 baseline 和当前缓存：只有顶层节发生变化（即本进程
        #   真正修改过的顶层节），才会在 merge 时覆盖磁盘；其他顶层节一律保留
        #   磁盘上的最新值 → 彻底避免「多进程下某进程缓存的其他进程节的旧值
        #   覆盖磁盘最新值」的问题（例如 Qt 子进程 save 把主进程刚改的天气窗口
        #   位置还原，或者反过来）。
        self._baseline_snapshot: Dict[str, Any] = json.loads(json.dumps(self._config))

    # ============================================================ singleton
    @classmethod
    def get(cls, config_path: Optional[str] = None) -> 'ConfigManager':
        """获取进程内唯一的 ConfigManager 实例。

        Args:
            config_path: 首次调用时可显式指定 JSON 路径；后续调用若传入
                与实例不同的路径，会强制 reload 到新路径（仅极端场景用）。
        """
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = ConfigManager(config_path=config_path)
            else:
                abs_new = os.path.abspath(config_path) if config_path else None
                if abs_new and abs_new != cls._instance._config_path:
                    # 显式切换到另一个配置文件：替换单例
                    old_path = cls._instance._config_path
                    cls._instance = ConfigManager(config_path=abs_new)
                    print(f"[ConfigManager] switched: {old_path!r} -> {abs_new!r}", flush=True)
        return cls._instance

    # ========================================================= read APIs
    @property
    def config_path(self) -> str:
        return self._config_path

    def get_config_object(self) -> Dict[str, Any]:
        """返回内部缓存引用（供 DisplayManager 这种历史代码直接原地修改缓存
        上的 app_displays/main_display 等节；调用方自己知道哪些节归自己管）。"""
        with self._rw_lock:
            return self._config

    def get_section(self, section: str, default: Any = None) -> Any:
        with self._rw_lock:
            return self._config.get(section, default)

    def get_value(self, dotted_key: str, default: Any = None) -> Any:
        """支持 'notification_panel.window_x' 形式的点路径取值。"""
        parts = dotted_key.split('.')
        cur: Any = self._config
        with self._rw_lock:
            try:
                for p in parts:
                    cur = cur[p]
                return cur
            except (KeyError, TypeError):
                return default

    # ========================================================= identity API
    def set_owner(self, owner: str) -> None:
        """声明当前进程/实例的身份（display_manager / backdrop_manager）。

        ConfigManager._do_save 会根据身份严格过滤：只有 `_owner` 字段与
        self._my_owner 匹配的节才允许本进程写，其他进程 owner 的节一律
        保留磁盘最新值（即使本进程缓存里有旧值且显示为 dirty 也绝对不写）。
        """
        valid = (OWNER_DISPLAY_MANAGER, OWNER_BACKDROP_MANAGER)
        if owner not in valid:
            raise ValueError(f"[ConfigManager] set_owner invalid: {owner!r}; must be one of {valid}")
        self._my_owner = owner
        print(f"[ConfigManager] identity set to: {owner}", flush=True)

    # ========================================================= write APIs
    def update_section(self, section: str, update_dict: Dict[str, Any]) -> None:
        """最常用：刷新指定节的缓存（按 dict 合并），然后 schedule 写磁盘。

        注意：节的归属完全以 display_config.json 里 `_owner` 字段为准，
        本 API 不会自动补 owner；如果节没有 _owner，_do_save 会按规则判断是否允许写。
        """
        with self._rw_lock:
            if section not in self._config or not isinstance(self._config[section], dict):
                self._config[section] = {}
            self._config[section].update(update_dict)
        self._schedule_save()

    def set_section(self, section: str, value: Any) -> None:
        """直接替换整个节对象。"""
        with self._rw_lock:
            self._config[section] = value
        self._schedule_save()

    def set_value(self, dotted_key: str, value: Any) -> None:
        """支持 'notification_panel.window_x' 形式的点路径赋值。"""
        parts = dotted_key.split('.')
        with self._rw_lock:
            cur = self._config
            for p in parts[:-1]:
                if p not in cur or not isinstance(cur[p], dict):
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value
        self._schedule_save()

    # ========================================================= force ops
    def reload(self, from_disk: bool = True) -> Dict[str, Any]:
        """强制刷新缓存；from_disk=True 会重新读文件覆盖当前缓存。"""
        with self._rw_lock:
            if from_disk and os.path.exists(self._config_path):
                try:
                    with open(self._config_path, 'r', encoding='utf-8') as f:
                        self._config = json.load(f)
                except Exception as e:
                    print(f"[ConfigManager] reload failed: {e}", flush=True)
                    traceback.print_exc()
            # 无论是否 from_disk，reload 之后 baseline 必须同步刷新为当前缓存
            self._baseline_snapshot = json.loads(json.dumps(self._config))
            return dict(self._config)

    def save_now(self) -> None:
        """强制立即写磁盘（取消 debounce 定时器）。"""
        with self._save_timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        self._do_save()

    # ======================================================== debounce save
    def _schedule_save(self) -> None:
        """1 秒 debounce：合并短时间内的多次 update 为一次磁盘写。"""
        with self._save_timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(1.0, self._do_save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _deep_merge(self, base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
        """以 overlay 的值覆盖/追加到 base；非 dict 节直接替换。"""
        result = dict(base)
        for k, v in overlay.items():
            if (k in result and isinstance(result[k], dict) and isinstance(v, dict)):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    def _do_save(self) -> None:
        """真正写磁盘（dirty-section + owner 双重校验，**归属完全由配置 `_owner` 决定**）：

        规则（彻底配置驱动，代码无任何硬编码归属）：
          1. 读磁盘最新 disk_cfg
          2. 计算 dirty 顶层节（baseline vs cache）
          3. Owner 强校验（核心！完全按配置里的 `_owner` 字段）：
             - 非 dict 顶层节（如 lock_password 字符串）：非窗口配置，只要 dirty 就正常写
             - dict 顶层节（窗口配置类）：
               * 有 `_owner` 字段 → 只有 `_owner == self._my_owner` → 才允许写，否则 skip
               * 无 `_owner` 字段 → 只要 dict 里包含 window_x/y/w/h（说明是窗口配置但未分配归属）
                 → 直接 skip 谁都不写，避免冲突；否则按普通配置节正常写
             - 特殊容器 `app_displays`：逐个 app 子节判断，逻辑同上
          4. 所有不允许写的节：一律保留 disk 最新值
          5. 原子写磁盘 → 更新 baseline_snapshot
        """
        # 清除已触发的 timer 引用
        with self._save_timer_lock:
            self._save_timer = None

        save_path = self._config_path
        save_dir = os.path.dirname(save_path)

        with self._rw_lock:
            my_cache_snapshot: Dict[str, Any] = json.loads(json.dumps(self._config))  # 深拷贝快照
            baseline_snapshot: Dict[str, Any] = json.loads(json.dumps(self._baseline_snapshot))
            my_owner = self._my_owner

        # ---- 1. 读取磁盘上最新 cfg（如果存在）----
        disk_cfg: Dict[str, Any] = {}
        if os.path.exists(save_path):
            try:
                with open(save_path, 'r', encoding='utf-8') as f:
                    disk_cfg = json.load(f)
            except Exception as e:
                print(f"[ConfigManager] pre-read disk failed (continue with cache only): {e}", flush=True)
                disk_cfg = {}

        # ---- 2. 计算 dirty 顶层节（对比 baseline 和当前缓存）----
        all_top_keys = set()
        all_top_keys.update(baseline_snapshot.keys())
        all_top_keys.update(my_cache_snapshot.keys())
        dirty_top_sections: set = set()
        for k in all_top_keys:
            b_val = baseline_snapshot.get(k)
            c_val = my_cache_snapshot.get(k)
            try:
                if json.dumps(b_val, sort_keys=True, ensure_ascii=False) \
                        != json.dumps(c_val, sort_keys=True, ensure_ascii=False):
                    dirty_top_sections.add(k)
            except Exception:
                if b_val != c_val:
                    dirty_top_sections.add(k)

        # ---- 3. Owner 强过滤的合并策略（完全按配置字段，无任何硬编码归属）----
        merged: Dict[str, Any] = json.loads(json.dumps(disk_cfg))  # 起点：disk 最新
        owner_skipped_top = []  # 诊断：哪些顶层节因 owner 规则被跳过
        owner_skipped_apps = []  # 诊断：哪些 app 子节因 owner 规则被跳过

        _WINDOW_GEO_KEYS = {'window_x', 'window_y', 'window_width', 'window_height'}

        def _is_window_dict(val: Any) -> bool:
            """判断一个 dict 是否包含窗口几何字段（认为是窗口配置，必须带 owner）。"""
            if not isinstance(val, dict):
                return False
            return bool(set(val.keys()) & _WINDOW_GEO_KEYS)

        def _is_allowed_to_write_dict(section_dict: Any) -> tuple[bool, Optional[str]]:
            """给定一个 dict 节，判断当前进程是否有权写它（完全按 _owner 字段）。

            Returns:
                (allowed: bool, reason_owner_or_None)
            """
            if not isinstance(section_dict, dict):
                return True, None
            if OWNER_KEY in section_dict:
                owner = section_dict[OWNER_KEY]
                if owner == my_owner:
                    return True, owner
                else:
                    return False, owner  # 明确归属别人，禁止写
            else:
                # 无 owner：如果是窗口配置类（有几何字段）→ 禁止写（必须先明确分配归属）
                if _is_window_dict(section_dict):
                    return False, None  # None 表示：窗口配置但未分配归属，跳过
                else:
                    return True, None  # 普通 dict，按 dirty 正常写

        # 逐个处理 dirty 的顶层节
        for top_k in dirty_top_sections:
            cache_val = my_cache_snapshot.get(top_k)

            # --- 特殊容器：app_displays → 子节级别按 owner 过滤 ---
            if top_k == 'app_displays':
                if not isinstance(cache_val, dict):
                    # 非 dict 的 app_displays（极罕见）：直接赋值
                    merged[top_k] = cache_val
                    continue
                if 'app_displays' not in merged or not isinstance(merged['app_displays'], dict):
                    merged['app_displays'] = {}
                all_app_keys = set()
                all_app_keys.update(cache_val.keys())
                all_app_keys.update(merged['app_displays'].keys())
                for app_k in all_app_keys:
                    cache_app = cache_val.get(app_k)
                    if cache_app is None:
                        continue  # 本进程没这个 app，跳过
                    disk_app = merged['app_displays'].get(app_k)
                    # 先尝试用 cache_app 的 owner 判断，没有则看 disk_app 的
                    ref_dict = cache_app if isinstance(cache_app, dict) else disk_app
                    allowed, ref_owner = _is_allowed_to_write_dict(ref_dict)
                    if not allowed:
                        owner_skipped_apps.append(f"{app_k}(owner={ref_owner or 'unassigned'})")
                        continue  # 不允许写 → 保留 disk_app
                    # 通过校验 → deep merge cache_app 到 merged_app
                    if isinstance(disk_app, dict) and isinstance(cache_app, dict):
                        merged['app_displays'][app_k] = self._deep_merge(disk_app, cache_app)
                    else:
                        merged['app_displays'][app_k] = cache_app
                continue

            # --- 普通顶层节（main_display / notification_panel / thinking_log / lock_password ...）---
            if not isinstance(cache_val, dict):
                # 非 dict 顶层节（如 lock_password 字符串）：正常写
                merged[top_k] = cache_val
                continue

            disk_section = merged.get(top_k)
            # 先看 cache_val 的 owner，再看 disk_section 的 owner
            ref_dict = cache_val if isinstance(cache_val, dict) else disk_section
            allowed, ref_owner = _is_allowed_to_write_dict(ref_dict)
            if not allowed:
                why = ref_owner or ("unassigned-window" if _is_window_dict(cache_val) else None)
                owner_skipped_top.append(f"{top_k}(owner={why})")
                continue  # 不允许写 → 保留 merged 里的 disk_section 值

            # 通过校验 → deep merge 到 merged
            if isinstance(disk_section, dict) and isinstance(cache_val, dict):
                merged[top_k] = self._deep_merge(disk_section, cache_val)
            else:
                merged[top_k] = cache_val

        # ---- 诊断打印：对比磁盘/缓存/合并后 4 个关键节，以及 dirty 节、owner 跳过的节 ----
        def _brief(section: str, source_dict: Dict[str, Any]) -> str:
            s = source_dict.get(section, {})
            if not isinstance(s, dict):
                return '<not dict>'
            return {k: s.get(k) for k in _WINDOW_GEO_KEYS if k in s}
        try:
            diag_main = (f"main_display[disk={_brief('main_display', disk_cfg)}@o={disk_cfg.get('main_display', {}).get(OWNER_KEY) if isinstance(disk_cfg.get('main_display'), dict) else None} | "
                         f"cache={_brief('main_display', my_cache_snapshot)}@o={my_cache_snapshot.get('main_display', {}).get(OWNER_KEY) if isinstance(my_cache_snapshot.get('main_display'), dict) else None} | "
                         f"merged={_brief('main_display', merged)}@o={merged.get('main_display', {}).get(OWNER_KEY) if isinstance(merged.get('main_display'), dict) else None}]")
            diag_app_displays = ""
            if isinstance(disk_cfg.get('app_displays'), dict) \
                    or isinstance(my_cache_snapshot.get('app_displays'), dict) \
                    or isinstance(merged.get('app_displays'), dict):
                all_apps = set()
                for s in (disk_cfg, my_cache_snapshot, merged):
                    if isinstance(s.get('app_displays'), dict):
                        all_apps.update(s['app_displays'].keys())
                app_parts = []
                for app in sorted(all_apps):
                    def _pk(d: Dict[str, Any]) -> Dict[str, Any]:
                        a = d.get('app_displays', {}).get(app, {}) if isinstance(d.get('app_displays'), dict) else {}
                        if not isinstance(a, dict):
                            return {}
                        return {k: a.get(k) for k in _WINDOW_GEO_KEYS if k in a}
                    def _powner(d: Dict[str, Any]) -> Any:
                        a = d.get('app_displays', {}).get(app, {}) if isinstance(d.get('app_displays'), dict) else {}
                        return a.get(OWNER_KEY) if isinstance(a, dict) else None
                    app_parts.append(
                        f"{app}[d={_pk(disk_cfg)}@o={_powner(disk_cfg)}"
                        f"|c={_pk(my_cache_snapshot)}@o={_powner(my_cache_snapshot)}"
                        f"|m={_pk(merged)}@o={_powner(merged)}]"
                    )
                diag_app_displays = " | ".join(app_parts)
            diag_notif = (f"notification_panel[disk={_brief('notification_panel', disk_cfg)}@o={disk_cfg.get('notification_panel', {}).get(OWNER_KEY) if isinstance(disk_cfg.get('notification_panel'), dict) else None} | "
                          f"cache={_brief('notification_panel', my_cache_snapshot)}@o={my_cache_snapshot.get('notification_panel', {}).get(OWNER_KEY) if isinstance(my_cache_snapshot.get('notification_panel'), dict) else None} | "
                          f"merged={_brief('notification_panel', merged)}@o={merged.get('notification_panel', {}).get(OWNER_KEY) if isinstance(merged.get('notification_panel'), dict) else None}]")
            diag_thinking = (f"thinking_log[disk={_brief('thinking_log', disk_cfg)}@o={disk_cfg.get('thinking_log', {}).get(OWNER_KEY) if isinstance(disk_cfg.get('thinking_log'), dict) else None} | "
                             f"cache={_brief('thinking_log', my_cache_snapshot)}@o={my_cache_snapshot.get('thinking_log', {}).get(OWNER_KEY) if isinstance(my_cache_snapshot.get('thinking_log'), dict) else None} | "
                             f"merged={_brief('thinking_log', merged)}@o={merged.get('thinking_log', {}).get(OWNER_KEY) if isinstance(merged.get('thinking_log'), dict) else None}]")
            print(f"[ConfigManager] SAVE (me={my_owner!r}, dirty={sorted(dirty_top_sections)}"
                  f", skip_top={owner_skipped_top}, skip_apps={owner_skipped_apps}) → {diag_main}",
                  end="", flush=True)
            if diag_app_displays:
                print(f" | app_displays: {diag_app_displays}", end="", flush=True)
            print(f" | {diag_notif} | {diag_thinking} | file={save_path}", flush=True)
        except Exception as _diag_e:
            print(f"[ConfigManager] (diag print skipped: {_diag_e})", flush=True)

        # ---- 4. 原子写磁盘：temp file + replace ----
        tmp_path = save_path + '.tmp'
        write_ok = False
        try:
            os.makedirs(save_dir, exist_ok=True)
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, save_path)
            self._last_save_ts = time.time()
            write_ok = True
        except Exception as e:
            print(f"[ConfigManager] SAVE FAILED: {e}", flush=True)
            traceback.print_exc()
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

        # ---- 5. 只有写成功才更新 baseline_snapshot ----
        if write_ok:
            with self._rw_lock:
                self._baseline_snapshot = json.loads(json.dumps(self._config))


__all__ = [
    'ConfigManager',
    'OWNER_DISPLAY_MANAGER',
    'OWNER_BACKDROP_MANAGER',
    'OWNER_KEY',
]
