# 虚拟桌面懒创建启动 / 停止说明（virtual_desktop.py）

> 适用项目：`d:\TraeProj\Open-AutoGLM`
> 所有命令均在**项目根目录**下执行：
>
> ```powershell
> cd d:\TraeProj\Open-AutoGLM
> ```

## 1. 桌面布局（懒创建模型，0-based 从左到右）

| 桌面 | 编号 | 放什么 | 何时存在 |
|---|---|---|---|
| 当前桌面（通常 #0） | `0` | **手机桌面布局**：`server.py` + 全部 scrcpy 手机窗口、AR Backdrop/Dock/通知/思考窗 | **bootstrap 时就在你当前所在桌面启动，不新建任何虚拟桌面** |
| UU 远程桌面（通常 #1） | 通常 `1` | UU 远程 GameViewer（向日葵/ToDesk 同理）+ 回程 Dock | **运行脚本后默认不存在**；点主桌面 Dock 的「电脑」按钮（触发 `uu-open`）时才按需创建 |

核心行为：

- `bootstrap` **不创建虚拟桌面、不启动 UU**，手机窗口直接出现在你执行命令时所在的桌面上；
- 首次点 Dock「电脑」→ 自动新建 UU 桌面 + 启动 GameViewer + 放置回程 Dock；
- 之后再点「电脑」→ UU 桌面和 UU 都还在，**只切换桌面，不重复创建/启动**；
- UU 桌面被手动删掉后再点 → 自动重新创建。

## 2. 环境前提

- Python 3.10（用哪个 Python 运行 virtual_desktop.py，就用同一个解释器启动 server.py；等价于 `py -3.10 server.py`）
- 已安装 pyvda（脚本检测到缺失会提示，执行一次即可）：

```powershell
py -3.10 -m pip install pyvda
```

- UU 远程默认路径：`D:\UUdesk\GameViewer\GameViewer.exe`（不在该位置会自动探测常见路径，也可给 `uu-open` 加 `--uu-remote-path` 指定）

## 3. 一键启动（日常只用这一条）

```powershell
py -3.10 virtual_desktop.py bootstrap --server-min-hwnds 7 --server-wait-ms 25000 --phoneaddress 192.168.43.1:5555
```

执行后自动完成：

1. **不新建虚拟桌面**：记录当前桌面编号（通常 #0），手机布局直接启动在该桌面；
2. 循环确认手机 ADB 可达——每 3s 检测一次（有 USB 设备直接通过，否则对 `--phoneaddress` 执行 `adb connect`），**最长等待 5 分钟**，期间可插 USB 或在手机上确认无线调试授权；超时才中止（此时 server 尚未启动）；
3. 在当前桌面启动 `server.py`，等到 ≥7 个窗口真正出现（保证高德、天气等 scrcpy 卡片都已就位）；
4. 启动常驻 watcher（每 1.5s 巡检）：AI 运行中**动态新开的 scrcpy 卡片**会在约 2 秒内自动搬回手机桌面；UU 归位逻辑待机——`uu-open` 创建 UU 桌面后无需重启 watcher 即自动接续；
5. 把会话 PID 写入 `.vd_state.json`（UU 相关字段此时为空：`uu_active=false / uu_desktop=null`）。

看到下面这行代表手机布局窗口全部落在当前桌面：

```
🎯 审计通过：共 N 个窗口全部在当前桌面 #0。
```

## 4. 打开 UU 远程桌面（点「电脑」按钮 / 手动 uu-open）

日常操作：直接点手机桌面底部 AR Dock 的**「电脑」按钮**。[front/qt_backdrop.py](file:///d:/TraeProj/Open-AutoGLM/front/qt_backdrop.py) 会以 detached 方式拉起 `uu-open`（不卡 Qt、无黑框，日志 `.vd_uu.log`），其决策：

| 情况 | 行为 |
|---|---|
| A. UU 桌面存在 + UU 进程存活 | **只切换到该桌面**，不创建、不启动（回程 Dock 死了顺手补启） |
| B. UU 桌面存在但 UU 已退出 | 切过去，启动/唤醒 UU（缩托盘的单例实例会被唤出） |
| C. UU 桌面不存在（首次/被删） | 新建桌面 → 切过去 → 启动 GameViewer → 放置回程 Dock |

连点「电脑」时有 PID 锁文件 `.vd_uu.lock` 串行化，只有一个实例执行创建，其余直接退出。

也可手动执行（排障用）：

```powershell
py -3.10 virtual_desktop.py uu-open
```

UU 桌面的**回程 Dock**（[front/uu_dock.py](file:///d:/TraeProj/Open-AutoGLM/front/uu_dock.py)，与手机桌面的 AR Dock 同一份 Qt 组件、屏幕底部悬停唤出）：

- 左「设置」：占位按钮，**无功能**；
- 中「手机」：把手机主屏（main_display，窗口标题「手机桌面」）**嵌入当前所在桌面**——在 UU 桌面点就嵌到 UU 桌面，回手机桌面再点即嵌回；watcher 对该窗口按标题放行，不会回迁；
- 右「电脑」：点击切回手机控制界面所在桌面（读 `.vd_state.json` 的 `server_desktop`，默认 #0）。

## 5. 查看运行状态

```powershell
py -3.10 virtual_desktop.py status
```

bootstrap 后、尚未打开 UU 时：

```
活动 bootstrap 会话（.vd_state.json）：
  server.py     : PID=23656 🟢 运行中 → 桌面 #0
  watcher       : PID=26900 🟢 运行中
  UU 远程桌面   : ⚪ 未创建（点 toolbar 电脑钮 / 运行 uu-open 按需创建）
虚拟桌面总数：1
```

打开 UU 后：

```
  UU回程Dock    : PID=30112 🟢 运行中 → 桌面 #1
  UU 远程       : uu-open 启动 → 桌面 #1，PID=[...]
虚拟桌面总数：2
```

## 6. 停止服务（全量关闭，默认零残留）

```powershell
py -3.10 virtual_desktop.py stop
```

一条命令默认清理**全部项目进程 + UU 虚拟桌面**，结束后打印零残留核验报告：

1. 先杀 watcher 和 UU 桌面 toolbar（`uu_dock.py`），防止清理过程中窗口被来回搬动；
2. 向 server.py **发送 CTRL_C**：server 收到 `KeyboardInterrupt` 后 finally 关闭主桌面 toolbar（qt_backdrop）、`atexit` 逐个终止全部 scrcpy；12 秒没退出自动回退 `taskkill /PID /T /F` 强杀整棵进程树；
3. **无条件兜底核验**（不信赖任何隐式回收）：按命令行特征再扫一遍 watcher / uu_dock / server 残留；按 `.vd_backdrop.pid` 补杀主桌面 toolbar（强杀 server 时 daemon 子进程可能成孤儿）；按进程名扫净全部 scrcpy.exe；
4. **默认关闭 UU 主程序**（`taskkill /T` 整棵带走 WebView2/crashpad 子孙），并检测是否被后台服务重启；
5. **默认删除 state 记录的 UU 虚拟桌面**（手机桌面永不删）；
6. 删除 `.vd_state.json` / `.vd_backdrop.pid`，打印最终残留报告。

即使 `.vd_state.json` 已丢失/被清，`stop` 也会进入**孤儿清扫模式**：仅凭命令行特征、PID 文件、进程名把残留的 toolbar/watcher/scrcpy/UU 清干净（不发 CTRL_C）。

> ⚠️ **不要直接用任务管理器结束 server.py，也不要 `taskkill /F`**——那样跳过 atexit，scrcpy.exe 和 toolbar 会残留。请一律使用 `stop`。

两个默认行为的反向开关：

```powershell
# 保留 UU 主程序（只停项目，继续远程）
py -3.10 virtual_desktop.py stop --no-kill-uu

# 保留 UU 虚拟桌面（只停进程，空桌面留着下次直接用）
py -3.10 virtual_desktop.py stop --no-remove-desktops

# 没有状态文件时，手动指定 server.py 的 PID
py -3.10 virtual_desktop.py stop --server-pid 23656
```

**按策略保留的进程**（不算残留）：

- `GameViewerService / GameViewerServer / GameViewerHealthd`：UU 的无窗口后台服务（可能需管理员权限才能停，主程序也不会被它们立即重启）；确需彻底关闭请在 Windows「服务」管理器中停止 NetEase UU 相关服务；
- `adb` 服务（5037 端口，`adb fork-server`）：全机共享基础设施，IDE/其他工具也在用；确需关闭执行 `adb kill-server`。

### 6.1 优雅退出策略（设计说明）

#### A. 被 stop 管理的完整进程/线程拓扑

```text
py.exe (bootstrap 启动器)
└─ python.exe  server.py                         ← CTRL_C 的目标（进程树根节点）
   ├─ python.exe  qt_backdrop（multiprocessing spawn，daemon 子进程）
   │     └─ 同进程内含 4 个窗口：AR Backdrop / AR Dock(主桌面 toolbar) /
   │        通知面板 / 思考日志（不是 4 个进程，只是 1 个）
   ├─ scrcpy.exe × N（每张手机卡片 1 个，普通子进程）
   └─ 进程内线程（随进程退出自动消失，无需单独停）：
        Flask 请求线程、screen_monitor、console_tee、通知刷新、listener

python.exe  virtual_desktop.py watch             ← detached，独立进程（state 记 PID）
python.exe  front/uu_dock.py                     ← detached，UU 桌面 toolbar（state 记 PID）
GameViewer.exe                                  ← UU 主程序
├─ msedgewebview2.exe × N（界面渲染/GPU/网络等）
└─ crashpad_handler.exe（崩溃上报）
GameViewerService/Server/Healthd.exe            ← UU 后台服务（无窗口，按策略保留）
adb.exe  fork-server（5037）                     ← 全机共享（按策略保留）
```

关键事实：主桌面 toolbar 是 `multiprocessing` 的 **spawn 子进程**，其命令行是 spawn 引导串、不含 `qt_backdrop` 字样，**无法用命令行特征枚举**，只能靠 `.vd_backdrop.pid` 追踪。

#### B. 为什么是「先优雅（CTRL_C）、后强杀（/T /F）」

| | 优雅路径（CTRL_C） | 强杀路径（taskkill /T /F） |
|---|---|---|
| server 反应 | 收到 `KeyboardInterrupt` → Flask 退出 → `finally` 调 `backdrop_manager.stop()`（Pipe 发 stop → join 3s → terminate）→ `atexit` 逐个停 scrcpy | 进程立即终止，**finally/atexit 全部跳过** |
| 端口 8080 | 正常释放 | 释放，但子进程清理无序 |
| scrcpy | 随 atexit `stop_all` 逐个终止 | 仅靠 `/T` 带走，漏网即成孤儿 |
| 角色 | **主路径** | 仅当 CTRL_C 无法送达（如 `--server-no-window` 无控制台）或 12s 超时的**兜底** |

信号实现细节：stop 先 `SetConsoleCtrlHandler(自身, 忽略)` 再让一次性助手 `signal-pid` 附加到 server 控制台广播 CTRL_C——避免在单控制台宿主里 stop 自己被同源信号打断；优雅退出成功后额外等待 1s，给 daemon 子进程回收与 atexit 收尾。

#### C. 进程归因的四级锚点（防误杀、防漏杀）

旧版只信 `.vd_state.json` 里的单个 PID，一旦 PID 失配（dock 被 uu-open 补启、state 已被清、父死子孤）就漏杀。现在按以下顺序逐级兜底，每一级都有明确的存活校验，**绝不按"python.exe"这种泛进程名乱杀**：

| 级别 | 锚点 | 追踪对象 | 何时会失效 → 交给下一级 |
|---|---|---|---|
| 1 | state 单 PID | server / watcher / uu_dock | PID 被补启替换、state 丢失 |
| 2 | 命令行正则（PowerShell CIM 枚举 `Win32_Process`） | `virtual_desktop.py watch` / `front\uu_dock.py` / `server.py` | spawn 孙进程命令行无特征 |
| 3 | `.vd_backdrop.pid` | 主桌面 toolbar（qt_backdrop） | 文件陈旧（读 PID 后校验存活，陈旧自然失效） |
| 4 | 进程名 | `scrcpy.exe`（全清）、`GameViewer.exe`（树根 `/T`） | — |

附加安全规则：枚举命令行时排除 stop **自身 PID**；杀进程一律以**树根节点 + `/T`** 整棵处置（UU 用 `/T` 才能带走 WebView2/crashpad 子孙，旧版只 `/F` 主程序正是子进程残留的根因）；手机桌面（`server_desktop`）永不删除。

#### D. 清理时序为什么是这个顺序

1. **先停 watcher**：否则它会在窗口消失/桌面删除的瞬间把窗口反复搬回去；
2. **再停 UU 桌面 toolbar**：同理冻结所有搬窗动作；
3. **CTRL_C 停 server**：让 finally/atexit 走完全套有序清理，超时才 `/T /F`；
4. **三级兜底**（命令行 → backdrop PID 文件 → scrcpy 按名）：不信赖 daemon 自动回收与 atexit，只认真实存活；其中命令行正则与 `.vd_osktop.pid` **双保险终止右下角键盘图标宿主 `osk-icon`**（detached 独立进程），宿主被杀后再用 `Win+Ctrl+O` 快捷键**代为关闭系统 osk**（osk 高完整性无法 taskkill，快捷键是唯一通道，失败则告警提示手动关）；
5. **关 UU**：`/T` 整棵 → 等 1.5s → 按命令行含 `GameViewer`/`UUdesk` 补杀成孤儿的 WebView2 → 检测主程序是否被后台服务重启并给出警告；
6. **最后才删 UU 虚拟桌面**：必须等窗口所属进程都关闭，桌面变空再删（窗口合并到左侧邻居）；
7. **最终核验闭环**：重新枚举四类残留并打印「✅ 零残留」或带 PID 的「❌ 残留清单」（权限不足时据此手动处理）；
8. **清运行态文件**：`.vd_state.json`、`.vd_backdrop.pid`、`.vd_osktop.pid`。

#### E. 孤儿清扫模式（无 state 也能用）

`.vd_state.json` 丢失或已被清理时，旧版 stop 直接退出什么都不做；现在进入孤儿清扫模式：跳过 CTRL_C 与删桌（没有 PID/编号可用），其余按命令行特征、PID 文件、进程名照常清理。适合异常退出后的现场恢复。

#### F. 本轮相对旧版的优化清单

- 主桌面 toolbar（qt_backdrop）新增 `.vd_backdrop.pid` 自报机制，强杀 server 后也能精确补刀，不再依赖 daemon 回收；
- 优雅退出成功后也执行**无条件兜底**（旧版 graceful 分支完全信任 atexit，toolbar/scrcpy 漏网无人管）；
- scrcpy 改为任何情况下都按进程名扫尾（旧版只在"server 已死"分支扫）；
- UU 从 `taskkill /F`（只杀主程序）改为 `taskkill /T /F`（整棵进程树）+ 孤儿子孙二次补杀 + 服务重启检测；
- stop 默认语义改为**全量零残留**（默认关 UU、默认删 UU 桌面），旧 `--kill-uu/--remove-desktops` 保留为兼容，新增反向开关 `--no-kill-uu/--no-remove-desktops`；
- 结束打印机器可核对的零残留/残留清单，adb 与 UU 后台服务单列说明，避免把"按策略保留"误判为残留。

#### G. 手动核验残留（排障用）

```powershell
# 项目 python 进程（server / watcher / 两个 toolbar 应全部查不到）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'server\.py|uu_dock|virtual_desktop' } |
  Select-Object ProcessId, CommandLine

# scrcpy / UU 主程序（stop 默认全清后应查不到；UU 三个无窗口服务保留属正常）
Get-Process scrcpy,GameViewer -ErrorAction SilentlyContinue
```

## 7. 典型日常流程

```powershell
cd d:\TraeProj\Open-AutoGLM

# 上班：一键启动手机布局（在当前桌面，不建虚拟桌面）
py -3.10 virtual_desktop.py bootstrap --server-min-hwnds 7 --server-wait-ms 25000 --phoneaddress 192.168.43.1:5555

# 需要远程电脑时：点 Dock「电脑」（首次自动建 UU 桌面），或命令行：
py -3.10 virtual_desktop.py uu-open

# 随时查看
py -3.10 virtual_desktop.py status

# 下班：一键全量停止（两 toolbar/watcher/scrcpy 全清，UU 关闭、UU 桌面删除）
py -3.10 virtual_desktop.py stop
```

## 8. bootstrap / uu-open 常用可选参数

bootstrap（只控制 server 与手机连接）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--phoneaddress` | — | 无 USB 设备时 `adb connect` 的地址，如 `192.168.43.1:5555`（可不写端口，默认 5555）；仅检测一次，失败会弹出手机连接窗口 |
| `--server-wait-ms` | 18000 | 等 server 窗口出现的最长时间（ms）；卡片多建议 25000 |
| `--server-min-hwnds` | 2 | 至少出现几个顶层窗口才结束等待；本机完整界面建议 `7` |
| `--python` | 当前解释器 | 指定启动 server.py 的 Python，如 `C:\Python310\python.exe` |
| `--project-root` | 脚本所在目录 | server.py 的查找根目录 |
| `--host` / `--port` / `--model` / `--device-id` | — | 原样透传给 server.py |
| `--no-watcher` | 关 | 不启动常驻 watcher（动态新窗口将无人归位，不建议） |
| `--server-no-window` | 关 | 隐藏 server 控制台（**注意：无控制台时 stop 无法发 CTRL_C，会改走强杀**） |

uu-open（只控制 UU 远程桌面）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--uu-remote-path` | 自动探测 | 手动指定远程客户端 exe 路径（第 1 候选 `D:\UUdesk\GameViewer\GameViewer.exe`） |
| `--uu-remote-args` | 空 | 传给远程客户端的额外参数（如向日葵 `-silent`） |
| `--uu-wait-ms` | 10000 | 等 UU 窗口出现的最长时间（ms） |
| `--uu-min-hwnds` | 1 | UU 至少出现几个窗口才结束等待 |
| `--force-new-uu` | 关 | UU 已运行时也强制重新 Popen（默认复用/唤醒旧实例） |
| `--no-uu-dock` | 关 | 不在 UU 桌面放置回程 Dock（front/uu_dock.py） |

## 9. 虚拟桌面管理子命令（按需使用）

```powershell
py -3.10 virtual_desktop.py list               # 查看桌面总数 / 当前编号
py -3.10 virtual_desktop.py ensure --count 1   # 确保至少有 N 个桌面（默认 1；UU 桌面交给 uu-open）
py -3.10 virtual_desktop.py create             # 新建一个桌面（追加到最右）
py -3.10 virtual_desktop.py switch --index 1   # 切到指定桌面（0-based）
py -3.10 virtual_desktop.py remove --index 1   # 删除指定桌面（窗口合并到左侧邻居）
```

## 10. 排障备注

- **状态文件**：`.vd_state.json`（项目根）记录 server_pid / watcher_pid / server_desktop 及 uu-open 回填的 uu_active / uu_desktop / uu_pids / uu_dock_pid，stop 和 status 都靠它；`.vd_backdrop.pid` 是主桌面 toolbar 子进程的自报 PID（stop 精确补刀用，stop 结束自动删除）；异常残留可直接删除这两个文件后重新 bootstrap（stop 无 state 时会自动进入孤儿清扫模式）。
- **日志文件**：`.vd_watcher.log`（归位巡检，server 连续 3 轮确认退出后 watcher 自动结束）、`.vd_uu.log`（uu-open 创建/切换全过程）、`.vd_uudock.log`（回程 Dock）。
- **bootstrap 启动时弹出「连接手机」窗口（macOS 登录风）**：说明单次 ADB 检测未发现可用手机。检测顺序：① 有 USB 设备或 `--phoneaddress` 显式地址 → 单次检测直接通过；② 未显式给地址时，自动再试 `virtual_desktop_config.json` 中**上次登入成功的 IP**（控制台有日志），命中则不弹窗直接继续；③ 都不可达才弹窗，输入框**自动预填上次成功的 IP**，可直接点「登入」或改地址。**登入成功的地址原子写入项目根 `virtual_desktop_config.json`（`last_phone_address`）**；仅 USB（地址留空）登入成功不覆盖已保存 IP；该文件是用户配置，`stop` 不清理。窗口中部提示「请 USB 有线连接，并检查授权」。两种连接方式：① 插好 ADB 数据线并在手机弹窗授权，直接点「登入」（地址留空即仅重试 USB）；② 填写手机局域网 IP——可直接在整串输入框写 `192.168.x.x`（默认 5555）或 `IP:端口`，也可用下方的 **4 个数字框 + 「.」+ 「:」+ 端口框**分段输入（两种输入双向同步，四段全空=仅 USB）。未提供地址时前两段默认 `192.168`，光标落在第一个空段。输入请使用**物理键盘**或下面的系统屏幕键盘（旧版数字九宫格已移除）。**虚拟键盘（跟随会话生命周期）**：登录窗弹出时，程序即以 detached 独立进程（`osk-icon` 子命令，PID 落盘 `.vd_osktop.pid`）在屏幕**工作区最右下角（距任务栏和屏幕右边各 8px）**放一个**置顶悬浮键盘图标**（灰色=未开，蓝色=已开）。图标必须独立进程，否则 Qt 模态对话框会屏蔽同进程窗口的鼠标点击。单击用 `Win+Ctrl+O` 打开/关闭 Windows 系统屏幕键盘 `osk.exe`（可输入 IP，也可给系统 Wi-Fi 浮层里的密码框输入）；**长按 0.45s 可拖动图标到任意位置**（抓取光标、自动夹在屏幕工作区内不进任务栏），松手位置写入 `virtual_desktop_config.json` 的 `osk_icon_pos`（逻辑像素），下次启动/登入保持，分辨率或任务栏变化导致越界会自动夹回；图标不抢焦点，点完可直接继续输入。生命周期：**取消登录（「取消启动」/Esc/关窗）→ 立即杀掉图标宿主（图标消失）并代为关闭 osk；登入成功 → 宿主保留，bootstrap 幂等复用不重复拉起，图标跟随 server 周期；`stop` → 凭 PID 文件 taskkill /T /F（另有命令行特征兜底）杀宿主、用快捷键代关 osk、清除 PID 文件**（配置文件中的图标位置保留）。打开前程序会在 osk 关闭状态下写注册表 `HKCU\Software\Microsoft\Osk` 的 `WindowLeft/Top`（物理像素，自动扣除任务栏、按系统 DPI 缩放换算），使 osk 启动即位于**屏幕底部居中**。实测限制（Win11 25H2）：osk 以高完整性 + uiAccess 运行，普通进程对其窗口的 taskkill / PostMessage / SetWindowPos / SetWindowLong 一律 `error 5`（UIPI 拒绝），因此**无法隐藏 osk 标题栏、也无法直接移动/点击其窗口**（注册表 `Dock=1` 停靠模式该版本也忽略），唯一可控通道是系统开关快捷键 + 关闭状态下的注册表位置。**窗口左上角有一个设置齿轮按钮**：本程序不自建任何网络界面，点击通过 SendInput 发送 `Win+A` 唤起 Windows 右下角「快速设置 / 控制中心」原生浮层（Wi-Fi、蓝牙等磁贴都在里面，展开对应条目即可选网/配对，不跳转「设置」App；注意旧 `keybd_event` 注入的 Win 键会被 Win11 Shell 忽略，必须用 SendInput）。**连接失败窗口不关闭**：只显示一句红色短提示，完整错误收在「详细信息 ▸」下拉里（点击展开/收起，同时打印到控制台），可反复重试；点「取消启动」或按 Esc 中止 bootstrap（不启动 server，图标/osk 一并收回）。不再有旧版的后台每 3s 循环、5 分钟超时。
- **UU 桌面看不到 UU**：GameViewer 关窗默认只缩托盘、进程不退。托盘态没有可见窗口，`uu-open` 会自动再启动一次 exe 唤出旧实例；会话运行中手动关了 UU 窗，从托盘双击图标重新点开即可，watcher 会在 ≤2s 内搬回 UU 桌面。
- **再点「电脑」没有重新启动 UU**：这是预期行为——UU 桌面与进程都在时只切换桌面（单例软件重复启动无意义）。
- **启动 bootstrap 时原桌面闪一下「桌面1」浮窗**：不是建桌/切桌逻辑被触发。pyvda 的 `go()`（COM `SwitchDesktop`）即使目标就是当前桌面，也会触发 Windows 的桌面切换浮窗；懒创建模型下 server 就在当前桌面启动，但 `launch_on_desktop` 强隔离分支仍无条件执行了一次 `switch(当前桌面)`。已修复为「当前桌面 = 目标桌面时跳过 `go()`」（[virtual_desktop.py](file:///d:/TraeProj/Open-AutoGLM/virtual_desktop.py#L189-L197) 的 `switch()` 与 [uu_dock.py](file:///d:/TraeProj/Open-AutoGLM/front/uu_dock.py#L66-L74) 均有此守卫），不影响真实切换。
- **stop 请在单独的命令行窗口执行**：原理是给 server 所在控制台发 CTRL_C，独立窗口运行最稳妥；脚本内部已做信号隔离，不会误杀 stop 自己。
