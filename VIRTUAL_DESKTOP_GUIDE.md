# 虚拟桌面一键启动 / 停止说明（virtual_desktop.py）

> 适用项目：`d:\TraeProj\Open-AutoGLM`
> 所有命令均在**项目根目录**下执行：
>
> ```powershell
> cd d:\TraeProj\Open-AutoGLM
> ```

## 1. 桌面布局（严格 0-based，从左到右）

| 桌面 | 编号 | 放什么 |
|---|---|---|
| 桌面 0（主桌面） | `0` | **手机桌面布局**：`server.py` + 全部 scrcpy 手机窗口、AR Backdrop/Dock/通知/思考窗；bootstrap 结束后自动切回这里，直接使用 |
| 桌面 1（新建） | `1` | UU 远程 GameViewer（向日葵/ToDesk 同理）+ 回程 Dock |

bootstrap 会自动确保至少存在 2 个虚拟桌面（只新建 1 个），不足则新建；已有 ≥2 个不做任何动作。

## 2. 环境前提

- Python 3.10（用哪个 Python 运行 virtual_desktop.py，就用同一个解释器启动 server.py；等价于 `py -3.10 server.py`）
- 已安装 pyvda（脚本检测到缺失会提示，执行一次即可）：

```powershell
py -3.10 -m pip install pyvda
```

- UU 远程默认路径：`D:\UUdesk\GameViewer\GameViewer.exe`（不在该位置会自动探测常见路径，也可用 `--uu-remote-path` 指定）

## 3. 一键启动（日常只用这一条）

```powershell
py -3.10 virtual_desktop.py bootstrap --server-min-hwnds 7 --server-wait-ms 25000 --uu-min-hwnds 1 --uu-wait-ms 15000
```

执行后自动完成：

1. 确保 ≥2 个虚拟桌面（主桌面 #0 + 新建的 UU 桌面 #1）；
2. 在主桌面 0 启动 `server.py`，等到 ≥7 个窗口真正出现（保证高德、天气等 scrcpy 卡片都落在桌面 0）；
3. 切到桌面 1 处理 UU 远程——
   - **UU 没运行**：启动它；
   - **UU 已运行且窗口开着**：不重复启动，直接把窗口归位到桌面 1；
   - **UU 已运行但缩在系统托盘（最常见）**：自动再启动一次 exe 把旧实例的主窗口**唤出到桌面 1**（单例软件不会开第二个程序），日志可见 `✅ 远程客户端主窗口已唤出并归位到桌面 #1`；
4. 启动常驻 watcher（每 1.5s 巡检）：AI 运行中**动态新开的 scrcpy 卡片**、从托盘里点开的 **UU 窗口**，都会在约 2 秒内自动搬回正确桌面；
5. 在桌面 1 放置**回程 Dock**（[front/uu_dock.py](file:///d:/TraeProj/Open-AutoGLM/front/uu_dock.py)，与桌面 0 的 AR Dock 同一份 Qt 组件、屏幕底部悬停唤出）：
   - 左「设置」：占位按钮，**无功能**；
   - 中「手机」：把手机主屏（main_display，窗口标题「手机桌面」）**嵌入当前所在桌面**——在桌面 1 点就嵌到桌面 1，回桌面 0 再点即嵌回桌面 0；watcher 对该窗口按标题放行，不会回迁；
   - 右「电脑」：点击直接切回手机控制界面（主桌面 0）；
6. 切回执行前所在桌面（通常就是主桌面 0，手机布局已就绪），并把本次会话的 PID（含回程 Dock PID）写入 `.vd_state.json`。

看到下面这行代表 100% 隔离成功：

```
🎯 100% 隔离成功：共 7 个窗口全部在正确的桌面 #0/#1。
```

## 4. 查看运行状态

```powershell
py -3.10 virtual_desktop.py status
```

输出示例：

```
活动 bootstrap 会话（.vd_state.json）：
  server.py     : PID=23656 🟢 运行中 → 桌面 #0
  watcher       : PID=26900 🟢 运行中
  UU 远程       : 早已运行(按进程名归位) → 桌面 #1，PID=[...]
虚拟桌面总数：2
```

## 5. 停止服务（优雅关闭）

```powershell
py -3.10 virtual_desktop.py stop
```

停止顺序：

1. 先杀 watcher 和桌面 1 的回程 Dock（防止清理过程中窗口被来回搬动）；
2. 向 server.py **发送 CTRL_C**：server 收到 `KeyboardInterrupt` 后走 `atexit`，逐个终止全部 scrcpy 子进程，**端口 8080 释放、无孤儿进程**；
3. 12 秒内没退出才自动回退 `taskkill /PID /T /F` 强杀整棵进程树；
4. 删除 `.vd_state.json`。

> ⚠️ **不要直接用任务管理器结束 server.py，也不要 `taskkill /F`**——那样跳过 atexit，3 个 scrcpy.exe 会残留。请一律使用 `stop`。

可选参数：

```powershell
# 连 UU 远程一起关闭（默认保留，不影响你继续远程）
py -3.10 virtual_desktop.py stop --kill-uu

# 关闭后删除 UU 桌面 #1（窗口合并回主桌面 #0，主桌面本身不删）
py -3.10 virtual_desktop.py stop --kill-uu --remove-desktops

# 没有状态文件时，手动指定 server.py 的 PID
py -3.10 virtual_desktop.py stop --server-pid 23656
```

## 6. 典型日常流程

```powershell
cd d:\TraeProj\Open-AutoGLM

# 上班：一键启动
py -3.10 virtual_desktop.py bootstrap --server-min-hwnds 7 --server-wait-ms 25000 --uu-min-hwnds 1 --uu-wait-ms 15000

# 随时查看
py -3.10 virtual_desktop.py status

# 下班：一键停止（scrcpy 随 server 一起被清理）
py -3.10 virtual_desktop.py stop
```

## 7. bootstrap 常用可选参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--server-wait-ms` | 18000 | 在主桌面 0 等 server 窗口出现的最长时间（ms）；卡片多建议 25000 |
| `--server-min-hwnds` | 2 | 至少出现几个顶层窗口才切走；本机完整界面建议 `7` |
| `--uu-wait-ms` | 10000 | 在桌面 1 等 UU 窗口的最长时间（ms），建议 15000 |
| `--uu-min-hwnds` | 1 | UU 至少出现几个窗口才切走 |
| `--python` | 当前解释器 | 指定启动 server.py 的 Python，如 `C:\Python310\python.exe` |
| `--project-root` | 脚本所在目录 | server.py 的查找根目录 |
| `--host` / `--port` / `--model` / `--device-id` | — | 原样透传给 server.py |
| `--uu-remote-path` | 自动探测 | 手动指定远程客户端 exe 路径 |
| `--return-to` | `original` | 编排结束后回到哪个桌面：`original`（通常即主桌面 0）或 `0/1` |
| `--force-new-uu` | 关 | UU 已运行时也强制重新启动它 |
| `--no-watcher` | 关 | 不启动常驻 watcher（动态新窗口将无人归位，不建议） |
| `--no-uu-dock` | 关 | 不在桌面 1 放置回程 Dock（front/uu_dock.py） |
| `--server-no-window` | 关 | 隐藏 server 控制台（**注意：无控制台时 stop 无法发 CTRL_C，会改走强杀**） |

## 8. 虚拟桌面管理子命令（按需使用）

```powershell
py -3.10 virtual_desktop.py list               # 查看桌面总数 / 当前编号
py -3.10 virtual_desktop.py ensure --count 2   # 确保至少有 2 个桌面（默认值）
py -3.10 virtual_desktop.py create             # 新建一个桌面（追加到最右）
py -3.10 virtual_desktop.py switch --index 1   # 切到指定桌面（0-based）
py -3.10 virtual_desktop.py remove --index 1   # 删除 UU 桌面（窗口合并回主桌面 0）
```

## 9. 排障备注

- **状态文件**：`.vd_state.json`（项目根）记录 server_pid / watcher_pid / UU PID / 桌面编号，stop 和 status 都靠它；异常残留可直接删除后重新 bootstrap。
- **watcher 日志**：`.vd_watcher.log`，记录每一次窗口归位动作；watcher 在 server 连续 3 轮确认退出后自动结束。
- **桌面 1 看不到 UU**：UU（GameViewer）关闭窗口默认只是缩到系统托盘、进程不退。托盘态没有任何可见窗口，自然不属于任何桌面。bootstrap 已自动处理（唤出到桌面 1）；会话运行中若手动关了 UU 窗口，从托盘**双击图标重新点开**即可，watcher 会在 ≤2s 内把它搬回桌面 1。
- **stop 请在单独的命令行窗口执行**：原理是给 server 所在控制台发 CTRL_C，独立窗口运行最稳妥；脚本内部已做信号隔离，不会误杀 stop 自己。
- **bootstrap 输出 0 字节但服务正常**：以 `.vd_state.json` 和 `status` 命令为准，属管道缓冲问题，不影响功能。
