# PhoneController
可与手势识别：https://github.com/xdjzhh/HandDetecterForEye
语音识别：https://github.com/xdjzhh/ASR
启动框架：https://github.com/xdjzhh/VirtualDesk
一直食用

## 项目背景：眼镜的未来到底是什么

纵观当前所有眼镜产品，无一例外都在注重功能炫酷，疯狂加码显示清晰度，却没有任何真正的实际用途。在动手做产品之前，我们先思考三个问题：

**问题一：现在的眼镜真的能替代手机吗？**

或者说，等眼镜 AR 成熟了，它能从什么角度、什么场景替换手机？如果答案不明朗，那在眼镜上堆砌算力和显示指标，本质上是在一块不适合的载体上复刻一块"戴在脸上的手机屏幕"。

**问题二：真的还需要一个新的独立控制设备和系统吗？**

在已有如此多终端——电脑、手机、平板，以及各种平台系统——的情况下，世界真的还需要一个新的、独立的控制设备和系统吗？多一套独立的终端体系，就多一套生态割裂、多一份学习成本、多一次数据孤岛。与其再造一个"新手机"，不如让现有终端为我所用。

**问题三：手机没有网络还能做什么？**

电话？拍照？定时？除了这些基础功能外，其他高级功能完全锁死——没有网络，手机啥也不是。既然高级功能本质上完全依赖网络和云端算力，为什么不干脆更进一步，将计算设备上云、通过远程控制驱动？

### 结论：轻终端 + 远程计算

把上述三个问题想清楚，本项目的定位就浮出水面了：

- **眼镜/头显端只保留轻量必备能力**：内置语音服务、手势识别服务和基本功能，满足穿戴设备对功耗和体积的严苛约束
- **其他高级功能全部通过远程控制实现**：计算和控制发生在远端（PC/云），眼镜只是一个轻薄的交互入口，而不是一台戴在头上的电脑

**使用场景**：消防、战术、骑行头盔、多屏交互、语音手势交互

**优势**：除内置语音服务、手势识别服务和基本功能外，其他高级功能通过远程控制实现，显著降低了设备的功耗和体积。且完美的复用已有的无论是pc端还是手机端的APP

## 项目介绍

PhoneController 是上述"轻终端 + 远程计算"理念的落地实现：远端承担全部计算与决策，通过远程控制驱动手机等现有终端，眼镜侧只需接入语音、手势等轻量交互通道。

PhoneController 是一个手机端智能助理框架，它能够以多模态方式理解手机屏幕内容，并通过自动化操作帮助用户完成任务。系统通过
ADB(Android Debug Bridge)来控制设备，以视觉语言模型进行屏幕感知，再结合智能规划能力生成并执行操作流程。用户只需用自然语言描述需求，如“打开小红书搜索美食”，Agent
即可自动解析意图、理解当前界面、规划下一步动作并完成整个流程。系统还内置敏感操作确认机制，并支持在登录或验证码场景下进行人工接管。同时，它提供远程
ADB 调试能力，可通过 WiFi 或网络连接设备，实现灵活的远程控制与开发。

服务以 Flask HTTP Server 形式运行，任务通过 `/run` 接口驱动，便于与其他系统（语音识别、手势控制等）集成。

> ⚠️
> 本项目仅供研究和学习使用。严禁用于非法获取信息、干扰系统或任何违法活动。

**当前限制**：控制端当前基于 Windows 实现，后续将迁移至裁剪的 Linux 系统，进行极限轻量化，进一步降低远端控制主机的功耗与体积。

<img width="685" height="501" alt="image" src="https://github.com/user-attachments/assets/53c46a5c-ae37-4899-b824-44dd5ade5e98" />
<img width="685" height="473" alt="image" src="https://github.com/user-attachments/assets/a8f6ab20-9cce-4524-8b6a-6f5d8ae951e3" />


## Android 环境准备

### 1. Python 环境

建议使用 Python 3.10 及以上版本。

### 2. 手机调试命令行工具

无需单独下载 ADB，直接下载 scrcpy 即可（安装包内自带 `adb.exe`）。本项目的主显示窗口也基于 scrcpy 实现，一次安装两个组件全都齐了。

1. 下载 scrcpy [安装包](https://github.com/Genymobile/scrcpy/releases)（Windows 选择 `scrcpy-win64-*.zip`），并解压到自定义路径
2. 将解压目录（同时包含 `scrcpy.exe` 和 `adb.exe`）加入环境变量



### 3. Android 7.0+ 设备，并启用 `开发者模式` 和 `USB 调试`

1. 开发者模式启用：通常启用方法是，找到 `设置-关于手机-版本号` 然后连续快速点击 10
   次左右，直到弹出弹窗显示“开发者模式已启用”。不同手机会有些许差别，如果找不到，可以上网搜索一下教程。
2. USB 调试启用：启用开发者模式之后，会出现 `设置-开发者选项-USB 调试`，勾选启用
3. 部分机型在设置开发者选项以后, 可能需要重启设备才能生效. 可以测试一下: 将手机用USB数据线连接到电脑后, `adb devices`
   查看是否有设备信息, 如果没有说明连接失败.

**请务必仔细检查相关权限**

### 4. 安装 ADB Keyboard(用于文本输入)

下载 [安装包](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk) 并在对应的安卓设备中进行安装。
注意，安装完成后还需要到 `设置-输入法` 或者 `设置-键盘列表` 中启用 `ADB Keyboard` 才能生效(或使用命令`adb shell ime enable com.android.adbkeyboard/.AdbIME`[How-to-use](https://github.com/senzhk/ADBKeyBoard/blob/master/README.md#how-to-use))

## iPhone 控制（开发中）

iPhone 控制采用 **ESP32 硬件方案**，绕过侵入式控制：

- 放弃 WebDriverAgent(WDA) 路线 —— WDA 需要开发者证书、仅支持 App 内操作，且属于侵入式方案
- 新方案通过 **AirPlay 镜像（UxPlay）** 获取屏幕画面，通过 **ESP32 BLE HID 透传网关** 模拟外接鼠标/键盘控制设备
- 无需在 iPhone 上安装任何应用、无需签名证书，利用 AssistiveTouch 与外接键盘语义实现系统级操控

详细设计与开发计划见 [iOS ESP32 HID 方案](docs/ios_esp32_hid_plan.md)。

## 部署准备工作

### 1. 安装依赖

```bash
pip install -r requirements.txt 
pip install -e .
```

### 2. 配置 ADB

确认 **USB数据线具有数据传输功能**, 而不是仅有充电功能

确保已安装 ADB 并使用 **USB数据线** 连接设备：

```bash
# 检查已连接的设备
adb devices

# 输出结果应显示你的设备，如：
# List of devices attached
# emulator-5554   device
```

#### 开启无线 ADB

开启后无需 USB 数据线即可控制设备。首次开启需要先用 USB 连接一次：

```bash
# 1. 手机通过 USB 连接电脑后，让 adbd 监听 5555 端口
adb tcpip 5555
# 输出：restarting in TCP mode port: 5555

# 2. 查看手机 IP（也可在手机 设置 > WLAN > 当前网络详情 中查看）
adb shell ip route
# 从输出中找到手机 IP，如 192.168.1.100

# 3. 拔掉 USB 数据线，通过 IP 连接（确保手机与电脑在同一 WiFi）
adb connect 192.168.1.100:5555
# 输出：connected to 192.168.1.100:5555

# 4. 验证连接
adb devices
# 应显示：192.168.1.100:5555    device
```

#### 使用无线 ADB IP 启动服务

设备通过无线连接后，直接启动服务即可（设备自动检测）：

```bash
adb connect 192.168.1.100:5555
python server.py
```

服务会自动检测已连接的设备（连接哪台就控制哪台），不依赖任何环境变量。如果电脑上连接了多台设备，建议先用 `adb disconnect` 断开不需要的设备，只保留要控制的那台。

> 注意：部分手机重启后会自动关闭无线调试，需重新用 USB 执行一次 `adb tcpip 5555` 再连接。

### 3. 启动模型服务

你可以选择自行部署模型服务，或使用第三方模型服务商。

#### 选项 A: 使用第三方模型服务

如果你不想自行部署模型，可以使用以下已部署我们模型的第三方服务：

**1. 智谱 BigModel**

- 文档: https://docs.bigmodel.cn/cn/api/introduction
- `--base-url`: `https://open.bigmodel.cn/api/paas/v4`
- `--model`: `glm-5.3-flash`
- `--apikey`: 在智谱平台申请你的 API Key

#### 选项 B: 自行部署模型

**不建议本地安装部署**——本地部署需要 GPU、推理引擎配置繁琐，且占用大量显存与磁盘空间，推荐直接使用选项 A 的第三方服务。

若执意本地安装，可使用 [Ollama](https://ollama.com) 一键下载部署：

```bash
# 1. 安装 Ollama 后，拉取模型（以 qwen2.5 为例，可按需换成其他模型）
ollama pull qwen2.5:7b

# 2. Ollama 自带 OpenAI 兼容 API，默认监听 http://localhost:11434，无需额外配置
```

然后在 `start_proporties.yaml` 中修改三个参数指向 Ollama：

```yaml
base_url: http://localhost:11434/v1
model: qwen2.5:7b
apikey: "ollama"    # Ollama 不校验 key，任意非空即可
```

## 使用

### 启动服务

```bash
python server.py
```

服务默认监听 `127.0.0.1:8080`。启动参数优先级：**命令行参数 > start_proporties.yaml > 内置默认值**。

可选参数：

| 参数                  | 说明                                              |
|---------------------|-------------------------------------------------|
| `--config`          | 指定 YAML 配置文件路径（默认读同目录 `start_proporties.yaml`） |
| `--host`            | 监听地址（默认 `127.0.0.1`）                          |
| `--port`            | 监听端口（默认 `8080`）                               |
| `--base-url`        | 模型 API 地址（默认 `http://localhost:8000/v1`）      |
| `--model`           | 模型名称（默认 `autoglm-phone`）                      |
| `--apikey`          | 模型认证 API Key                                  |
| `--no-main-display` | 不启动主显示窗口                                       |
| `--no-screen-monitor` | 不启动屏幕监控                                       |
| `--no-backdrop`     | 不启动黑背景容器                                       |



### 通过 HTTP 接口执行任务

任务通过 `POST /run` 接口下发：

```bash
curl -X POST http://127.0.0.1:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task": "打开美团搜索附近的火锅店"}'
```

- 任务运行期间再次下发新任务会被拒绝（返回 409）
- 下发停止指令可终止正在运行的任务


## 远程调试

PhoneController 支持通过 WiFi/网络进行远程 ADB 调试，无需 USB 连接即可控制设备。

### 配置远程调试

#### 在手机端开启无线调试

1. 确保手机和电脑在同一个WiFi中
2. 进入 `设置 > 开发者选项 > 无线调试`，开启该功能
3. 记录显示的 IP 地址和端口号

#### 在电脑端使用标准 ADB 命令

```bash
# 通过 WiFi 连接, 改成手机显示的 IP 地址和端口
adb connect 192.168.1.100:5555

# 验证连接
adb devices
# 应显示：192.168.1.100:5555    device
```

### 设备管理命令

```bash
# 列出所有已连接设备
adb devices

# 连接远程设备
adb connect 192.168.1.100:5555

# 断开指定设备
adb disconnect 192.168.1.100:5555
```

### Python API 远程连接

```python
from phone_agent.adb import ADBConnection, list_devices

# 创建连接管理器
conn = ADBConnection()

# 连接远程设备
success, message = conn.connect("192.168.1.100:5555")
print(f"连接状态: {message}")

# 列出已连接设备
devices = list_devices()
for device in devices:
    print(f"{device.device_id} - {device.connection_type.value}")

# 在 USB 设备上启用 TCP/IP
success, message = conn.enable_tcpip(5555)
ip = conn.get_device_ip()
print(f"设备 IP: {ip}")

# 断开连接
conn.disconnect("192.168.1.100:5555")
```

### 远程连接问题排查

**连接被拒绝：**

- 确保设备和电脑在同一网络
- 检查防火墙是否阻止 5555 端口
- 确认已启用 TCP/IP 模式：`adb tcpip 5555`

**连接断开：**

- WiFi 可能断开了，使用 `adb connect` 重新连接
- 部分设备重启后会禁用 TCP/IP，需要通过 USB 重新启用

**多设备：**

- 使用 `adb devices` 查看所有已连接设备
- 服务会自动检测已连接的设备，建议用 `adb disconnect` 断开不需要的设备，只保留要控制的那台

## 配置

### 自定义SYSTEM PROMPT

系统提供中英文两套 prompt，通过 `--lang` 参数切换：

- `--lang cn` - 中文 prompt(默认)，配置文件：`phone_agent/config/prompts_zh.py`
- `--lang en` - 英文 prompt，配置文件：`phone_agent/config/prompts_en.py`

可以直接修改对应的配置文件来增强模型在特定领域的能力，或通过注入 app 名称禁用某些 app。


### 模型配置

```python
from phone_agent.model import ModelConfig

config = ModelConfig(
    base_url="http://localhost:8000/v1",
    api_key="EMPTY",  # API 密钥(如需要)
    model_name="glm-5.3-flash",  # 模型名称
    max_tokens=3000,  # 最大输出 token 数
    temperature=0.1,  # 采样温度
    frequency_penalty=0.2,  # 频率惩罚
)
```

### Agent 配置

```python
from phone_agent.agent import AgentConfig

config = AgentConfig(
    max_steps=100,  # 每个任务最大步数
    device_id=None,  # ADB 设备 ID(None 为自动检测)
    lang="cn",  # 语言选择：cn(中文)或 en(英文)
    verbose=True,  # 打印调试信息(包括思考过程和执行动作)
)
```



## 支持的应用

PhoneController 支持 50+ 款主流中文应用：

| 分类   | 应用              |
|------|-----------------|
| 社交通讯 | 微信、QQ、微博        |
| 电商购物 | 淘宝、京东、拼多多       |
| 美食外卖 | 美团、饿了么、肯德基      |
| 出行旅游 | 携程、12306、滴滴出行   |
| 视频娱乐 | bilibili、抖音、爱奇艺 |
| 音乐音频 | 网易云音乐、QQ音乐、喜马拉雅 |
| 生活服务 | 大众点评、高德地图、百度地图  |
| 内容社区 | 小红书、知乎、豆瓣       |

完整列表见 `phone_agent/config/apps.py`。

## 可用操作

Agent 可以执行以下操作：

| 操作           | 描述              |
|--------------|-----------------|
| `Launch`     | 启动应用            |  
| `Tap`        | 点击指定坐标          |
| `Type`       | 输入文本            |
| `Swipe`      | 滑动屏幕            |
| `Back`       | 返回上一页           |
| `Home`       | 返回桌面            |
| `Long Press` | 长按              |
| `Double Tap` | 双击              |
| `Wait`       | 等待页面加载          |
| `Take_over`  | 请求人工接管(登录/验证码等) |



## 二次开发


### 完整项目结构

```
PhoneController/
├── server.py                       # HTTP 服务入口（Flask，/run 接口驱动任务）
├── start_proporties.yaml           # 启动参数配置（单一配置来源）
├── ios.py                          # iOS Agent CLI（旧 WDA 方案，将由 ESP32 方案取代）
├── setup.py / requirements.txt     # 安装与依赖
│
├── phone_agent/                    # 核心 Agent 逻辑
│   ├── agent.py                    #   PhoneAgent 主类（任务循环）
│   ├── agent_ios.py                #   iOS Agent
│   ├── device_factory.py           #   设备工厂（按类型创建设备连接）
│   ├── display_switcher.py         #   显示切换
│   ├── adb/                        #   ADB 工具（Android）
│   │   ├── connection.py           #     连接管理（USB / WiFi 远程）
│   │   ├── device.py               #     设备控制（点击、滑动等）
│   │   ├── input.py                #     文本输入（ADB Keyboard）
│   │   └── screenshot.py           #     屏幕截图
│   ├── hdc/                        #   HDC 工具（鸿蒙，未适配）
│   ├── actions/                    #   操作执行器
│   │   ├── handler.py              #     Android 动作执行
│   │   └── handler_ios.py          #     iOS 动作执行
│   ├── config/                     #   配置
│   │   ├── apps.py                 #     支持的 Android 应用映射
│   │   ├── apps_ios.py             #     支持的 iOS 应用映射
│   │   ├── prompts_zh.py           #     中文系统提示词
│   │   ├── prompts_en.py           #     英文系统提示词
│   │   └── timing.py               #     时序配置
│   └── model/                      #   AI 模型客户端
│       └── client.py               #     OpenAI 兼容客户端
│
├── scrcpycontrol/                  # 屏幕镜像与虚拟桌面管理
│   ├── scrcpy_client.py            #   scrcpy 客户端
│   ├── virtualdisplay.py           #   虚拟屏（scrcpy 窗口卡片）
│   ├── multi_virtualdisplay.py     #   多虚拟屏管理
│   ├── display_server.py           #   显示服务
│   ├── display_interface.py        #   显示管理统一接口
│   ├── navigation_monitor.py       #   导航 / 锁屏监测
│   ├── screen_control_keyhot.py    #   屏幕控制快捷键
│   └── config_manager.py           #   显示配置管理
│
├── front/                          # 前端 UI 窗口
│   ├── card_window.py              #   卡片窗口（可拖拽 / 缩放）
│   ├── qt_backdrop.py              #   黑背景容器（虚拟桌面）
│   ├── qt_notification.py          #   消息通知窗口
│   ├── qt_thinking_log.py          #   思考过程日志窗口
│   ├── curved_compositor_preview.py#   弯曲合成预览（眼镜显示形态）
│   ├── window_config.py            #   窗口配置
│   └── console_tee.py              #   控制台输出分流
│
├── docs/                           # 设计文档
│   └── ios_esp32_hid_plan.md       #   iOS ESP32 HID 方案设计
└── *.md                            # 其他专题文档（虚拟桌面 / 服务模式 / 依赖说明等）
```

## 常见问题

我们列举了一些常见的问题，以及对应的解决方案：

### 设备未找到

尝试通过重启 ADB 服务来解决：

```bash
adb kill-server
adb start-server
adb devices
```

如果仍然无法识别，请检查：

1. USB 调试是否已开启
2. 数据线是否支持数据传输(部分数据线仅支持充电)
3. 手机上弹出的授权框是否已点击「允许」
4. 尝试更换 USB 接口或数据线

### 能打开应用，但无法点击

部分机型需要同时开启两个调试选项才能正常使用：

- **USB 调试**
- **USB 调试(安全设置)**

请在 `设置 → 开发者选项` 中检查这两个选项是否都已启用。

### 文本输入不工作

1. 确保设备已安装 ADB Keyboard
2. 在设置 > 系统 > 语言和输入法 > 虚拟键盘 中启用
3. Agent 会在需要输入时自动切换到 ADB Keyboard

### 截图失败(黑屏)

这通常意味着应用正在显示敏感页面(支付、密码、银行类应用)。Agent 会自动检测并请求人工接管。

### windows 编码异常问题

报错信息形如 `UnicodeEncodeError gbk code`

解决办法: 在运行代码的命令前面加上环境变量: `PYTHONIOENCODING=utf-8`
