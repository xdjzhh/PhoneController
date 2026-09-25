# -*- coding: utf-8 -*-
"""
AR UU Dock —— UU 远程桌面（桌面 #1）上的回程 Dock。

只由 virtual_desktop.py bootstrap 以 detached 方式在桌面 #1 启动，用户不会
直接运行本文件。本文件只是 front/desktop_toolbar.py 公用组件的一个「配置实例」：

    ┌─────────┬─────────┬─────────┐
    │  设置    │  手机   │  电脑   │
    └─────────┴─────────┴─────────┘

  · 设置（齿轮）：占位，**无功能**（手机控制界面的配置在主桌面 #0）；
  · 手机（手机图标）：把手机主屏（main_display，标题「手机桌面」）嵌入
    当前所在桌面——在 #1 点就嵌到 #1，回 #0 再点即嵌回 #0；
  · 电脑（显示器）：点击直接切回手机控制界面所在虚拟桌面
    （编号读项目根 .vd_state.json 的 server_desktop，默认 #0）。

视觉/交互/Win32 样式全部来自 DesktopToolbar，与主桌面 #0 的 AR Dock 同源，
不再维护第二份绘制代码。
"""

import json
import os
import sys

# 以脚本方式（python front/uu_dock.py）启动时，把项目根加入 sys.path，
# 保证 `from front.desktop_toolbar import ...` 可用。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_STATE_FILE = os.path.join(_PROJECT_ROOT, ".vd_state.json")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from front.desktop_toolbar import (
    DesktopToolbar,
    bring_main_display_to_current_desktop,
    declare_dpi_awareness,
)


def _server_desktop_index() -> int:
    """手机控制界面（server.py）所在虚拟桌面编号，0-based，默认 0（主桌面）。"""
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("server_desktop", 0))
    except Exception:
        return 0


def _switch_to_phone_desktop() -> None:
    """点击「电脑」：切回手机控制界面桌面（.vd_state.json: server_desktop）。"""
    idx = _server_desktop_index()
    try:
        from pyvda import get_virtual_desktops
        desktops = get_virtual_desktops()
        if 0 <= idx < len(desktops):
            desktops[idx].go()
            print(f"[UUDock] 已切换到手机控制桌面 #{idx}", flush=True)
        else:
            print(f"[UUDock] 目标桌面 #{idx} 越界（当前共 {len(desktops)} 个），未切换。",
                  flush=True)
    except Exception as e:
        print(f"[UUDock] 切换手机控制桌面失败: {e}", flush=True)


def main() -> int:
    declare_dpi_awareness()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    # 设置按钮 on_settings=None → 占位无功能；
    # 手机按钮 → 主屏嵌入当前桌面；电脑按钮 → 切回手机控制桌面
    dock = DesktopToolbar(
        window_title="AR UU Dock",
        on_settings=None,
        on_phone=bring_main_display_to_current_desktop,
        on_computer=_switch_to_phone_desktop,
    )
    # 定期保持 topmost（与主桌面 #0 AR Dock 的 3s 定时器策略一致）
    keep_top = QTimer()
    keep_top.timeout.connect(dock._apply_win32_style)
    keep_top.start(3000)

    print(f"[UUDock] 已启动，目标手机控制桌面=#{_server_desktop_index()}"
          f"（隐藏中，鼠标移到屏幕底部唤出）", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
