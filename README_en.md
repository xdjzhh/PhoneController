# PhoneController
hand detection：https://github.com/xdjzhh/HandDetecterForEye
ASR：https://github.com/xdjzhh/ASR
start framework：https://github.com/xdjzhh/VirtualDesk

## Background: What Is the Real Future of Smart Glasses?

Look at every smart glasses product on the market today: without exception, they chase flashy features and endlessly pump up display resolution, yet serve no genuine practical purpose. Before building a product, let's think through three questions:

**Question 1: Can glasses really replace the phone?**

Or, once AR glasses mature, from what angle and in what scenarios could they replace the phone? If the answer is unclear, then piling compute and display specs onto glasses is essentially replicating a "phone screen worn on your face" on a carrier that's unfit for it.

**Question 2: Do we really need yet another standalone control device and system?**

Given how many terminals already exist — PCs, phones, tablets, plus all kinds of platforms and operating systems — does the world really need a new, independent control device and system? Every additional standalone terminal ecosystem brings more fragmentation, more learning cost, and another data silo. Rather than building yet another "new phone," we should put the terminals we already own to work for us.

**Question 3: What can a phone do without a network?**

Make calls? Take photos? Set alarms? Beyond these basics, every advanced capability is completely locked down — without a network, a phone is nothing. Since advanced features fundamentally depend on network and cloud compute anyway, why not go one step further: move the compute to the cloud and drive everything through remote control?

### Conclusion: Thin Client + Remote Compute

Once you think through these three questions, this project's positioning emerges naturally:

- **Glasses/headsets keep only lightweight essentials**: built-in voice services, gesture recognition, and basic functions — meeting the strict power and size constraints of wearables
- **All advanced features are realized through remote control**: computation and control happen on the remote end (PC/cloud); the glasses are just a thin interaction portal, not a computer strapped to your head

**Use cases**: firefighting, tactical operations, motorcycle helmets, multi-screen interaction, voice/gesture interaction

**Advantage**: apart from built-in voice services, gesture recognition, and basic functions, all advanced features are delivered via remote control — dramatically reducing the device's power consumption and physical size, while perfectly reusing apps on both PCs and phones.

## Introduction

PhoneController is the practical implementation of the "thin client + remote compute" philosophy above: the remote end handles all computation and decision-making, driving existing terminals such as phones through remote control, while the glasses side only needs lightweight interaction channels like voice and gesture input.

PhoneController is a phone-side AI agent framework. It understands phone screen content multimodally and helps users complete tasks through automated operations. The system controls devices via ADB (Android Debug Bridge), perceives the screen with a vision-language model, and combines intelligent planning to generate and execute operation flows. Users simply describe their needs in natural language — for example, "open Xiaohongshu and search for food" — and the agent automatically parses the intent, understands the current interface, plans the next action, and completes the entire flow. The system also has a built-in sensitive-action confirmation mechanism and supports human takeover for login or CAPTCHA scenarios. In addition, it provides remote ADB debugging: devices can be connected over WiFi or network for flexible remote control and development.

The server runs as a Flask HTTP server, with tasks dispatched via the `/run` API — easy to integrate with other systems (speech recognition, gesture control, etc.).

> ⚠️
> This project is for research and learning purposes only. It must not be used for illegal information acquisition, system interference, or any other illegal activities.

**Current limitation**: the controller currently runs on Windows and will later be ported to a trimmed-down Linux system for extreme lightweight deployment, further reducing the power consumption and size of the remote control host.
hand detection：https://github.com/xdjzhh/HandDetecterForEye
ASR：https://github.com/xdjzhh/ASR
start framework：https://github.com/xdjzhh/VirtualDesk
<img width="685" height="501" alt="image" src="https://github.com/user-attachments/assets/53c46a5c-ae37-4899-b824-44dd5ade5e98" />
<img width="685" height="473" alt="image" src="https://github.com/user-attachments/assets/a8f6ab20-9cce-4524-8b6a-6f5d8ae951e3" />

## Android Setup

### 1. Python Environment

Python 3.10 or above is recommended.

### 2. Phone Debugging CLI Tools

No need to download ADB separately — just download scrcpy (the package ships with `adb.exe`). This project's main display window is also built on scrcpy, so one installation covers both components.

1. Download the scrcpy [package](https://github.com/Genymobile/scrcpy/releases) (choose `scrcpy-win64-*.zip` on Windows) and extract it to a custom path
2. Add the extracted directory (containing both `scrcpy.exe` and `adb.exe`) to your PATH



### 3. An Android 7.0+ Device with `Developer Mode` and `USB Debugging` Enabled

1. Enabling Developer Mode: usually done via `Settings > About Phone > Build Number` — tap it quickly about 10 times until a popup says "You are now in developer mode". This varies slightly across devices; search for a tutorial if you can't find it.
2. Enabling USB Debugging: once Developer Mode is on, go to `Settings > Developer Options > USB Debugging` and check it
3. On some models, the developer options may require a device reboot to take effect. You can test: connect the phone to the PC with a USB cable, then run `adb devices` — if no device shows up, the connection failed.

**Please double-check the relevant permissions**

### 4. Install ADB Keyboard (for text input)

Download the [APK](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk) and install it on your Android device.
Note: after installation, you must also enable `ADB Keyboard` under `Settings > Input Methods` or `Settings > Keyboard List` for it to work (or run `adb shell ime enable com.android.adbkeyboard/.AdbIME` — see [How-to-use](https://github.com/senzhk/ADBKeyBoard/blob/master/README.md#how-to-use)).

## iPhone Control (In Development)

iPhone control uses an **ESP32 hardware approach** that bypasses intrusive control:

- The WebDriverAgent (WDA) route is abandoned — WDA requires a developer certificate, only supports in-app operations, and is an intrusive solution
- The new approach captures the screen via **AirPlay mirroring (UxPlay)** and controls the device through an **ESP32 BLE HID passthrough gateway** that emulates an external mouse/keyboard
- No app needs to be installed on the iPhone, no signing certificate is required; system-level control is achieved via AssistiveTouch and external keyboard semantics

See [iOS ESP32 HID Plan](docs/ios_esp32_hid_plan.md) for the detailed design and development plan.

## Deployment Preparation

### 1. Install Dependencies

```bash
pip install -r requirements.txt 
pip install -e .
```

### 2. Configure ADB

Make sure your **USB cable supports data transfer**, not charging only.

Ensure ADB is installed and connect your device with a **USB cable**:

```bash
# Check connected devices
adb devices

# The output should show your device, e.g.:
# List of devices attached
# emulator-5554   device
```

#### Enabling Wireless ADB

Once enabled, no USB cable is needed to control the device. The first time, you must connect via USB once:

```bash
# 1. With the phone connected via USB, make adbd listen on port 5555
adb tcpip 5555
# Output: restarting in TCP mode port: 5555

# 2. Find the phone's IP (also visible under Settings > Wi-Fi > current network details)
adb shell ip route
# Locate the phone IP in the output, e.g. 192.168.1.100

# 3. Unplug the USB cable and connect over IP (make sure the phone and PC are on the same WiFi)
adb connect 192.168.1.100:5555
# Output: connected to 192.168.1.100:5555

# 4. Verify the connection
adb devices
# Should show: 192.168.1.100:5555    device
```

#### Starting the Service with a Wireless ADB IP

Once the device is connected wirelessly, just start the service (devices are auto-detected):

```bash
adb connect 192.168.1.100:5555
python server.py
```

The service automatically detects connected devices (whichever is connected is the one being controlled) and does not depend on any environment variables. If multiple devices are connected to your PC, it is recommended to `adb disconnect` the ones you don't need and keep only the one you want to control.

> Note: some phones automatically turn wireless debugging off after a reboot; re-run `adb tcpip 5555` over USB once and reconnect.

### 3. Start a Model Service

You can deploy a model service yourself, or use a third-party model provider.

#### Option A: Use a Third-Party Model Service

If you don't want to deploy a model yourself, use a third-party service:

**1. Zhipu BigModel**

- Docs: https://docs.bigmodel.cn/cn/api/introduction (Chinese)
- `--base-url`: `https://open.bigmodel.cn/api/paas/v4`
- `--model`: `glm-5.3-flash`
- `--apikey`: apply for your API key on the BigModel platform

#### Option B: Deploy a Model Yourself

**Local deployment is not recommended** — it requires a GPU, inference engine configuration is cumbersome, and it consumes substantial VRAM and disk space. Using the third-party service in Option A is recommended.

If you insist on a local setup, [Ollama](https://ollama.com) offers one-command deployment:

```bash
# 1. After installing Ollama, pull a model (qwen2.5 here as an example; use any model you like)
ollama pull qwen2.5:7b

# 2. Ollama ships with an OpenAI-compatible API listening on http://localhost:11434 by default — no extra configuration needed
```

Then edit the three parameters in `start_proporties.yaml` to point to Ollama:

```yaml
base_url: http://localhost:11434/v1
model: qwen2.5:7b
apikey: "ollama"    # Ollama doesn't validate the key; any non-empty value works
```

## Usage

### Starting the Service

```bash
python server.py
```

The service listens on `127.0.0.1:8080` by default. Parameter priority: **CLI arguments > start_proporties.yaml > built-in defaults**.

Available options:

| Option             | Description                                                        |
|--------------------|--------------------------------------------------------------------|
| `--config`         | Path to a YAML config file (defaults to `start_proporties.yaml` in the same directory) |
| `--host`           | Listen address (default `127.0.0.1`)                               |
| `--port`           | Listen port (default `8080`)                                       |
| `--base-url`       | Model API URL (default `http://localhost:8000/v1`)                  |
| `--model`          | Model name (default `autoglm-phone`)                               |
| `--apikey`         | API key for model authentication                                   |
| `--no-main-display`| Skip launching the main display window                             |
| `--no-screen-monitor` | Skip launching the screen monitor                               |
| `--no-backdrop`    | Skip launching the black backdrop container                        |



### Running Tasks via the HTTP API

Tasks are dispatched with `POST /run`:

```bash
curl -X POST http://127.0.0.1:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Open Meituan and search for nearby hotpot restaurants"}'
```

- Dispatching a new task while one is running is rejected (returns 409)
- Sending the stop command terminates the running task


## Remote Debugging

PhoneController supports remote ADB debugging over WiFi/network, so devices can be controlled without a USB connection.

### Configuring Remote Debugging

#### Enable Wireless Debugging on the Phone

1. Make sure the phone and the PC are on the same WiFi
2. Go to `Settings > Developer Options > Wireless Debugging` and enable it
3. Note the displayed IP address and port

#### Use Standard ADB Commands on the PC

```bash
# Connect over WiFi; substitute the IP and port shown on the phone
adb connect 192.168.1.100:5555

# Verify the connection
adb devices
# Should show: 192.168.1.100:5555    device
```

### Device Management Commands

```bash
# List all connected devices
adb devices

# Connect to a remote device
adb connect 192.168.1.100:5555

# Disconnect a specific device
adb disconnect 192.168.1.100:5555
```

### Remote Connection via the Python API

```python
from phone_agent.adb import ADBConnection, list_devices

# Create a connection manager
conn = ADBConnection()

# Connect to a remote device
success, message = conn.connect("192.168.1.100:5555")
print(f"Connection status: {message}")

# List connected devices
devices = list_devices()
for device in devices:
    print(f"{device.device_id} - {device.connection_type.value}")

# Enable TCP/IP on a USB-connected device
success, message = conn.enable_tcpip(5555)
ip = conn.get_device_ip()
print(f"Device IP: {ip}")

# Disconnect
conn.disconnect("192.168.1.100:5555")
```

### Troubleshooting Remote Connections

**Connection refused:**

- Make sure the device and the PC are on the same network
- Check whether the firewall blocks port 5555
- Confirm TCP/IP mode is enabled: `adb tcpip 5555`

**Connection dropped:**

- WiFi may have disconnected; reconnect with `adb connect`
- Some devices disable TCP/IP after a reboot; re-enable it over USB

**Multiple devices:**

- Use `adb devices` to list all connected devices
- The service auto-detects connected devices; it's recommended to `adb disconnect` the ones you don't need and keep only the one you want to control

## Configuration

### Customizing the System Prompt

The system ships with two prompt sets, Chinese and English, switchable via `--lang`:

- `--lang cn` - Chinese prompt, config file: `phone_agent/config/prompts_zh.py`
- `--lang en` - English prompt (default), config file: `phone_agent/config/prompts_en.py`

You can edit these config files directly to strengthen the model's capabilities in specific domains, or inject app names to disable certain apps.


### Model Configuration

```python
from phone_agent.model import ModelConfig

config = ModelConfig(
    base_url="http://localhost:8000/v1",
    api_key="EMPTY",  # API key (if required)
    model_name="glm-5.3-flash",  # Model name
    max_tokens=3000,  # Max output tokens
    temperature=0.1,  # Sampling temperature
    frequency_penalty=0.2,  # Frequency penalty
)
```

### Agent Configuration

```python
from phone_agent.agent import AgentConfig

config = AgentConfig(
    max_steps=100,  # Max steps per task
    device_id=None,  # ADB device ID (None = auto-detect)
    lang="en",  # Language: cn (Chinese) or en (English)
    verbose=True,  # Print debug info (including reasoning and executed actions)
)
```



## Supported Apps

PhoneController supports 50+ mainstream Chinese apps:

| Category      | Apps                                                    |
|---------------|---------------------------------------------------------|
| Social        | WeChat, QQ, Weibo                                        |
| E-commerce    | Taobao, JD.com, Pinduoduo                                |
| Food & Delivery | Meituan, Ele.me, KFC                                   |
| Travel        | Ctrip, 12306, Didi                                       |
| Video         | bilibili, Douyin, iQIYI                                  |
| Music & Audio | NetEase Cloud Music, QQ Music, Ximalaya                  |
| Lifestyle     | Dianping, Amap, Baidu Maps                               |
| Communities   | Xiaohongshu (RED), Zhihu, Douban                         |

See `phone_agent/config/apps.py` for the full list.

## Available Actions

The agent can perform the following actions:

| Action       | Description                                |
|--------------|--------------------------------------------|
| `Launch`     | Launch an app                              |  
| `Tap`        | Tap the specified coordinates              |
| `Type`       | Type text                                  |
| `Swipe`      | Swipe the screen                           |
| `Back`       | Go back to the previous page               |
| `Home`       | Return to the home screen                  |
| `Long Press` | Long press                                 |
| `Double Tap` | Double tap                                 |
| `Wait`       | Wait for the page to load                  |
| `Take_over`  | Request human takeover (login/CAPTCHA etc.)|



## Secondary Development


### Full Project Structure

```
PhoneController/
├── server.py                       # HTTP server entry (Flask, tasks via /run API)
├── start_proporties.yaml           # Startup configuration (single source of truth)
├── ios.py                          # iOS Agent CLI (legacy WDA approach, to be replaced by ESP32)
├── setup.py / requirements.txt     # Installation and dependencies
│
├── phone_agent/                    # Core agent logic
│   ├── agent.py                    #   PhoneAgent main class (task loop)
│   ├── agent_ios.py                #   iOS Agent
│   ├── device_factory.py           #   Device factory (creates device connections by type)
│   ├── display_switcher.py         #   Display switching
│   ├── adb/                        #   ADB tools (Android)
│   │   ├── connection.py           #     Connection management (USB / WiFi remote)
│   │   ├── device.py               #     Device control (tap, swipe, etc.)
│   │   ├── input.py                #     Text input (ADB Keyboard)
│   │   └── screenshot.py           #     Screenshots
│   ├── hdc/                        #   HDC tools (HarmonyOS, not adapted yet)
│   ├── actions/                    #   Action executors
│   │   ├── handler.py              #     Android action execution
│   │   └── handler_ios.py          #     iOS action execution
│   ├── config/                     #   Configuration
│   │   ├── apps.py                 #     Supported Android app mapping
│   │   ├── apps_ios.py             #     Supported iOS app mapping
│   │   ├── prompts_zh.py           #     Chinese system prompts
│   │   ├── prompts_en.py           #     English system prompts
│   │   └── timing.py               #     Timing configuration
│   └── model/                      #   AI model client
│       └── client.py               #     OpenAI-compatible client
│
├── scrcpycontrol/                  # Screen mirroring and virtual desktop management
│   ├── scrcpy_client.py            #   scrcpy client
│   ├── virtualdisplay.py           #   Virtual display (scrcpy window card)
│   ├── multi_virtualdisplay.py     #   Multi virtual display management
│   ├── display_server.py           #   Display service
│   ├── display_interface.py        #   Unified display management interface
│   ├── navigation_monitor.py       #   Navigation / lock screen monitoring
│   ├── screen_control_keyhot.py    #   Screen control hotkeys
│   └── config_manager.py           #   Display configuration management
│
├── front/                          # Frontend UI windows
│   ├── card_window.py              #   Card window (draggable / resizable)
│   ├── qt_backdrop.py              #   Black backdrop container (virtual desktop)
│   ├── qt_notification.py          #   Message notification window
│   ├── qt_thinking_log.py          #   Thinking-process log window
│   ├── curved_compositor_preview.py#   Curved compositor preview (glasses display form factor)
│   ├── window_config.py            #   Window configuration
│   └── console_tee.py              #   Console output tee
│
├── docs/                           # Design documents
│   └── ios_esp32_hid_plan.md       #   iOS ESP32 HID approach design
└── *.md                            # Other topic docs (virtual desktop / server mode / dependencies, etc.)
```

## FAQ

Here are some common issues and their solutions:

### Device Not Found

Try restarting the ADB server:

```bash
adb kill-server
adb start-server
adb devices
```

If the device still isn't recognized, check:

1. Whether USB debugging is enabled
2. Whether the cable supports data transfer (some cables are charge-only)
3. Whether the authorization popup on the phone has been accepted ("Allow")
4. Try a different USB port or cable

### Apps Open But Cannot Be Clicked

On some models, two debugging options must both be enabled:

- **USB Debugging**
- **USB Debugging (Security Settings)**

Check that both are enabled under `Settings > Developer Options`.

### Text Input Not Working

1. Make sure ADB Keyboard is installed on the device
2. Enable it under Settings > System > Languages & Input > Virtual Keyboard
3. The agent automatically switches to ADB Keyboard when input is needed

### Screenshots Fail (Black Screen)

This usually means the app is showing a sensitive page (payments, passwords, banking apps). The agent detects this automatically and requests human takeover.

### Windows Encoding Errors

Error messages like `UnicodeEncodeError gbk code`

Solution: prefix the command with the environment variable `PYTHONIOENCODING=utf-8`
