# virtualdesk 包 + 独立 Toolbar 服务（仅搬 toolbar，不拆主文件）实施计划

## 需求确认（已与用户对齐）

1. **新建 `virtualdesk/` 包，只搬 toolbar 相关逻辑；`virtual_desktop.py` 本轮不拆分**，仍是 CLI 主文件（新增隐藏子命令 `toolbar`）。
2. **新服务形态**：Qt 常驻进程 + CLI（`python virtual_desktop.py toolbar`，detached，模式同 watcher）。
3. **键盘成为 toolbar 第 4 个按钮**（齿轮/手机/电脑/键盘）：不拖动、非常驻；点一下开 osk、再点关。
4. **坐标统一编排，三者互不遮挡**：
   - **dock 贴屏幕最底部居中**（工作区底边留 6px，垂直 padding 16→10，dock 高约 80→68）；
   - **osk 打开时按需上移**：底边落在 dock 顶边之上（间隔 8px），即改 `_position_osk_bottom_center` 的目标 top = 工作区底 - dock 高 - 8 - osk 高（osk 只能关态写注册表定位，现有 toggle 链路本就是先写后开，首次即到位）；
   - **IP 登录窗固定偏上初始位置**（不随 osk 跳动）：窗口 y 固定在工作区顶部约 12px、水平居中；为保证 516 高的登录窗底边也不压 osk（osk 顶≈508），同步压缩登录窗垂直留白约 30px（图标/标题区间距、卡片上下边距），目标窗高约 480；具体数值真机标定，验收=三者同时在屏时几何无交集。
   - osk 打开期间 dock **保持淡入不自动淡出**（固定底部居中），保证「再点键盘钮关闭」始终可点；osk 关后恢复 4px 触发带悬停唤出。
5. **「设置」走 server HTTP**（`POST /display/edit_mode`），server 未就绪仅打印提示。
6. 生命周期：bootstrap **第一步**启服务 → 手机检测/登录窗；取消登录 → 停服务 + 关 osk；登入成功 → 跟随 server 周期；stop → 杀服务 + 关 osk + 清 PID。

## Repository Research（现状关键事实）

- `front/desktop_toolbar.py`（576 行）：纯绘制组件，3 图标，ICON_COUNT 驱动布局/命中/绘制；悬停淡入、4px 底部触发带、WS_EX_APPWINDOW 虚拟桌面强约束、topmost、不抢焦点；含 pyvda `bring_window_to_current_desktop` / `bring_main_display_to_current_desktop` / `declare_dpi_awareness`。
- `front/uu_dock.py`：桌面 #1 的第二实例（设置占位、手机=主屏嵌入当前桌面、电脑=读 .vd_state.json 的 server_desktop 切回，带 go() 校验重试）。
- `front/qt_backdrop.py`：server.py multiprocessing 子进程，内嵌 "AR Dock"；设置回调在子进程内更新 notif/thinking 面板，并 pipe 通知父进程 listener 调 `card_mgr.set_all_edit_mode`。
- `server.py`：`POST /display/edit_mode` 只调 `display_manager.set_edit_mode`；全局 `backdrop_manager`（`start_backdrop_window`）。默认 127.0.0.1:8080。
- `virtual_desktop.py`：osk 全套（`_send_hotkey/_find_osk_hwnd/_position_osk_bottom_center/_toggle_osk/_close_osk_if_running`）、`_load_config/_update_config`、`_read_pid_file/_pid_alive/_taskkill_tree`、state、watcher、stop、uu-open 均在主文件——**新包直接 `import virtual_desktop` 复用，不复制**。
- watcher 按 uu_dock **进程 PID** 归位窗口；新架构单服务进程持有多桌面窗口，该块必须移除（由服务自愈），否则会把 #0 dock 也拽到 #1。
- 几何（1536×864 逻辑、125% DPI、任务栏约 40）：dock 4 图标约 272×80；osk 870×283 物理（≈696×226 逻辑）；登录窗 400×516。编排后：dock 贴底（约 y738–818）；osk 底=730、顶≈504；登录窗 osk 开时移到 y≤494（12px 顶边起），关闭回居中。

## 文件变更

**新增 `virtualdesk/` 包**
- `virtualdesk/__init__.py`
- `virtualdesk/toolbar.py`：由 `front/desktop_toolbar.py` 整体迁入（绘制/悬停/Win32 样式机制不变），3→4 图标：新增键盘图标绘制（`_draw_keyboard_icon`）、IDX_KEYBOARD、回调 `on_keyboard`；初始 y 改贴底 6px；新增 `set_pinned(bool)`（osk 开时钉住不淡出）。
- `virtualdesk/server_api.py`：urllib 小客户端：base_url 读 state host/port（回退 127.0.0.1:8080）、`is_up(timeout=0.4)`、`set_edit_mode(editing)`。
- `virtualdesk/toolbar_service.py`：`run_toolbar_service()` 新服务主体。

**修改 `virtual_desktop.py`**（不拆分）
- 删除：`_OskToggleButton/_run_osk_icon_host/_spawn_osk_icon_host/_stop_osk_icon_host`、osk-icon CLI 子命令与 dispatch、`osk_icon_pos` 配置读写引用。
- 新增：`_TOOLBAR_PID_FILE=.vd_toolbar.pid`、`_TOOLBAR_LOG=.vd_toolbar.log`、`_spawn_toolbar_service()/_stop_toolbar_service()`（幂等，flags 同 watcher）；CLI 隐藏子命令 `toolbar` → `virtualdesk.toolbar_service.run_toolbar_service()`。
- `_prompt_phone_connection_gui`：删除宿主 spawn/取消回收；登录窗初始位置改为固定偏上（水平居中、y≈工作区顶+12），压缩垂直留白至约 480 高。
- do_bootstrap：第一步 spawn toolbar 服务；取消登录路径停服务+关 osk。
- do_uu_open：删除 `_start_uu_dock` 调用与 `uu_dock_pid` 写入（`--no-uu-dock` 保留为废弃忽略参数）。
- do_watch：删除 uu_dock 按 PID 归位块。
- do_stop：命令行模式新增 `"toolbar": virtual_desktop\.py.*\btoolbar\b`、4c 增 `.vd_toolbar.pid` 双路径杀；删 osk-icon/uu-dock 模式与 PID 处理；保留关 osk、PID 清理；零残留文案更新。

**修改 `front/qt_backdrop.py`**：删除 DesktopToolbar 导入与 dock 创建块、`_ensure_dock_topmost`、ready 消息 dock_hwnd；`poll_pipe` 新增 `set_panels_edit_mode` 处理（notif/thinking `_set_edit_mode`）。

**修改 `server.py`**：`QtBackdropManager` 新增 `set_panels_edit_mode(show)`（pipe 发新 cmd，不触发现有 listener 的卡片逻辑）；`/display/edit_mode` 在 display_manager 调用后追加该联动（backdrop 未起时跳过）。

**删除**：`front/desktop_toolbar.py`、`front/uu_dock.py`（职责全部由包+服务承接）。

## Toolbar 服务设计（virtualdesk/toolbar_service.py）

- 启动：`declare_dpi_awareness()`；写 `.vd_toolbar.pid`；QApplication；finally 自清 PID。
- 一个进程托管**每个虚拟桌面一个 dock**（2s 轮询 reconcile）：
  - 新桌面：建 `DesktopToolbar`（标题：server_desktop → "AR Dock"，其余 → "AR UU Dock"），show 后用 pyvda `AppView(hwnd).move(d)` 迁到目标桌面；
  - 桌面删除：关对应 dock；dock 跑错桌面：迁回（自愈，取代 watcher 旧块）；
  - 每 dock 3s `_apply_win32_style` 保持 topmost。
- 回调：server 桌面 dock：设置→server_api.set_edit_mode 切换+齿轮高亮自持；手机→bring_main_display_to_current_desktop；电脑→detached 拉 `virtual_desktop.py uu-open`。其余 dock：设置=None；手机同上；电脑→迁入 uu_dock 的切回 server_desktop 逻辑（含越界/已在/4 次重试）。全部 dock：键盘→`virtual_desktop._toggle_osk()`。
- osk 联动（800ms QTimer，仅操作**当前桌面**的 dock）：
  - osk 开：当前 dock `set_pinned(True)`（淡入并保持、不响应触发带淡出）；
  - osk 关：`set_pinned(False)` 恢复悬停机制。
- server URL：每秒重读 state（host/port）；设置点击时先 `is_up()`，失败打印「server 未就绪」。

## 实施步骤

1. 建 `virtualdesk/` 包；迁 `front/desktop_toolbar.py`→`virtualdesk/toolbar.py`，加键盘钮/贴底/set_pinned；py_compile。
2. 写 `server_api.py`、`toolbar_service.py`（reconcile + 回调 + osk 联动）；主文件加 `toolbar` 子命令与 PID/日志 helper；编译。
3. osk 定位增加 dock 高度偏移；登录窗加 osk 轮询避让；移除 osk-icon 全部代码。
4. qt_backdrop 去 dock + 新 pipe cmd；server.py edit_mode 联动；删 front 两旧文件。
5. uu-open/watch/stop 配套修改。

## 验证

- py_compile + 0 诊断。
- offscreen：4 图标命中/键盘回调；reconcile 在 mock 桌面增删时增删 dock；pinned 切换；登录窗固定偏上后与 dock/osk 预设几何无交集断言；旧检测顺序/配置用例不回归。
- 真机：
  1. `py -3.10 virtual_desktop.py toolbar`：dock #0 贴底、悬停 4 钮；点键盘→osk 开在 dock 正上方不重叠、dock 保持可点；再点→osk 关、dock 恢复隐藏；
  2. bootstrap 无手机：登录窗固定偏上；开 osk→osk 位于 dock 正上方、三者几何无交集、dock 保持可点；再点→osk 关、dock 恢复隐藏；取消→服务退出+osk 关+PID 清；
  3. `create` 建桌面 #1→自愈出 "AR UU Dock" 落 #1；`remove --index 1`→消失；
  4. 完整 bootstrap：设置 HTTP 生效（卡片+面板）、电脑钮 uu-open 正常、stop 零残留。

## 风险与对策

- 单进程多桌面窗口 move 时机：show 后 QTimer 延迟 move + 2s reconcile 自愈；真机先验。
- osk 注册表定位仅关态可写：toggle 开 osk 前已先写（现有链路），偏移量调整沿用同一函数；已开着时 bootstrap 不强制移动（先关再开由用户点击驱动）。
- import virtual_desktop 的循环/重依赖：toolbar_service 仅函数级 import（run 时导入），避免包导入期拉起 argparse；编译与冒烟验证。
- 旧 state 的 uu_dock_pid：读取容忍忽略。
