# -*- coding: utf-8 -*-
"""virtualdesk —— 虚拟桌面服务包。

本包承接从 front/ 与 virtual_desktop.py 中独立出来的「常驻服务」逻辑：
  · toolbar            —— Mac 风格底部 Dock 绘制组件（4 图标：设置/手机/电脑/键盘）
  · server_api         —— server.py HTTP 接口的轻量客户端
  · toolbar_service    —— 独立 Qt 常驻进程：为每个虚拟桌面托管一个 Dock

virtual_desktop.py 仍是 CLI 主文件，通过隐藏子命令 `toolbar` 启动本服务。
"""
