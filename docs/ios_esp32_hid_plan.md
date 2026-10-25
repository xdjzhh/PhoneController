# iOS 控制方案：AirPlay 镜像 + ESP32 BLE HID 透传网关

> 目标：为 Open-AutoGLM 增加 iOS 系统级操控通道（锁屏/设置/任意 App 界面），
> 替代 WDA 路线（仅 App 内操作、需开发者证书）的盲区。设备实现层与 Android/ADB
> 同构，Agent 循环与动作协议复用。

## 1. 总体架构

```
┌─ PC ──────────────────────────────────────────────────────────┐
│  UxPlay 镜像窗口 ──► 抓帧 ──► Agent（模型决策，复用现有循环）    │
│       ▲ 投屏                    │ 动作指令                     │
│  Qt 覆盖层（坐标定位，同 scrcpy 卡片模式）                      │
│       │              ▼                                        │
│  HandDetecter/ASR      HidActionHandler                       │
│  （语音/手势链路不变）   │ 网关指令（USB 串口 / WiFi TCP）        │
└────────────────────────┼─────────────────────────────────────┘
                         ▼
              ESP32（纯透传网关，固件一次成型）
                         │ BLE HID（HOGP）
                         ▼
                    iPhone（AssistiveTouch + 外接键盘语义）
```

- **画面**：AirPlay 镜像 → UxPlay（开源接收端，Windows 可跑）显示 + 抓帧
- **输入**：PC → ESP32 → BLE HID 鼠标/键盘；ESP32 固件只做透传，永不迭代
- **坐标**：镜像窗口叠加 Qt 覆盖层，0-1000 归一化协议与 Android 一致

## 2. 链路对照（Android → iOS）

| 环节 | Android 现状 | iOS 方案 | 备注 |
|---|---|---|---|
| 画面 | scrcpy 镜像卡片 | AirPlay 镜像（UxPlay） | 延迟 50~150ms |
| 坐标定位 | 卡片窗口归一化坐标 | 投屏窗口 Qt 覆盖层 | 模式复用 |
| 点击 | ADB tap（绝对坐标） | HID 相对位移 + 视觉闭环伺服 | 见难点 1 |
| 拖动/滑动 | ADB swipe | HID 按住 + 移动 | iOS 映射为单指拖动 |
| 文本 | ADB text | HID 键盘拼音 + 候选确认 | 中文 IME 交互 |
| 滚动 | input swipe 模拟 | HID 滚轮 | AssistiveTouch 双指滚动 |
| 截图给模型 | ADB screencap | 镜像帧直接抓取 | 更简单 |
| 启动 App | am start | Spotlight 宏（Cmd+Space → 拼音 → Return） | 零视觉伺服 |
| Home/返回 | keyevent | Cmd+H / Cmd+Tab 等键盘快捷键 | 外接键盘语义 |

## 3. 三个关键难点与解法

### 难点 1：相对光标（最大工程量）
iOS 的 HID 鼠标是 AssistiveTouch 圆形光标，只吃 Δx/Δy 增量，且有指针加速曲线。
- **解法**：从镜像画面模板匹配光标位置，**闭环伺服**——小步逼近目标，每步校准
- 缓解：辅助功能中把指针跟踪速度调到最低（曲线近线性）
- 若重定位成本高：任何点击前先做一次光标检测

### 难点 2：安全界面遮蔽
密码输入、Apple Pay、Face ID 确认界面 iOS 会把镜像涂黑。画面不可见但 HID 可用
→ 识别"遮蔽态"后转**盲操作**（键盘盲打密码可行），或上报不可控。

### 难点 3：手势表达边界
HID 能表达：点按/拖动/滚动。表达不了：双指缩放、三指手势、边缘返回。
变通：AssistiveTouch 菜单预录"自定手势"，光标移到悬浮菜单触发——笨重但可达。
方案上限 = AssistiveTouch 能力上限，prompt 中需向模型声明该边界。

## 4. 延迟预算（端到端点击反馈）

```
光标检测 ~20ms + PC→ESP32 ~10ms + BLE connection interval 7.5~15ms
+ AssistiveTouch 处理 ~10ms + AirPlay 回显 50~150ms
≈ 100~200ms
```

对 Agent "看→想→点"节奏够用。注意 iPhone 息屏时 BLE interval 拉长（延迟劣化），
需保持亮屏策略。

## 5. 语音/手势控制：链路不变

语音在 PC 侧闭环（ASR 麦克风 → 转写 → Agent），与手机控制通道解耦：
换平台只是最后一跳从 ADB 换成 BLE HID，Agent 收到的仍是同一段文字任务。
HandDetecter 手势 → vd 事件流 → 动作映射层换目标即可。

## 6. 与现有代码的对接

保留 [agent_ios.py](../phone_agent/agent_ios.py) 的 `_execute_step` 循环结构
（模型调用/上下文/动作解析），只换三样：

1. **screenshot 源**：WDA 截图 → UxPlay 镜像抓帧
2. **执行体**：`IOSActionHandler`（WDA）→ 新 `HidActionHandler`，
   同接口 `execute(action, w, h)`；launch_app/press_home 等翻译为 HID 序列
   （对模型透明，prompt 动作协议不变）
3. **get_current_app**：取消依赖或降级为截图 OCR 猜测

坐标协议（0-1000 归一化）、模型提示词结构、Agent 循环全部复用。

## 7. ESP32 网关指令集（透传协议，一次成型）

```
MOVE dx dy            # 相对移动（光标伺服的最小步进）
CLICK [left]          # 单击（默认左键）
DOWN / UP             # 按住/释放（拖动用）
WHEEL dy              # 滚动
KEY keycode [mods]    # 单键（cmd/space/h 等 + 组合键修饰）
TEXT ascii            # 英文直接打；中文由 PC 侧拆拼音 + 候选交互
CAL                   # 重新广播/回连
```

传输层：USB 串口（首选，零网络依赖）或 WiFi TCP（可后期加）。

---

## 下一步开发计划

### 阶段 0：POC 前置验证（先跑通再动工，两个实测点）
- [ ] Windows 跑通 UxPlay，iPhone AirPlay 镜像可显示
- [ ] 验证**锁屏时镜像是否保持**（iOS 版本相关，需实测）
- [ ] 从镜像帧中模板匹配 AssistiveTouch 光标，测 50 帧检出率
- 验收：三项结论落文档（镜像可用性 / 锁屏行为 / 光标检出率 ≥ 95%）

### 阶段 1：ESP32 固件（NimBLE HOGP 透传网关）
- [ ] NimBLE 实现标准 BLE 鼠标+键盘外设，与目标 iPhone 完成配对 bonding
- [ ] 串口指令解析 → HID 事件映射（上表指令集）
- [ ] 压力测试：连续 1000 次 MOVE+CLICK 无丢事件、无断连
- 验收：`python send_cmd.py` 控制手机光标移动+点击；固件定型封版

### 阶段 2：PC 镜像与坐标层
- [ ] UxPlay 集成：启动/重连管理、窗口句柄定位
- [ ] 镜像帧抓取（供 Agent 截图 + 光标检测共用）
- [ ] 横竖屏跟随：镜像分辨率变化时坐标映射自动适配
- [ ] Qt 覆盖层窗口（复用 scrcpy 卡片模式，点击拦截 + 归一化）
- 验收：覆盖层点击坐标 → 镜像画面标注点位置误差 < 1%

### 阶段 3：光标视觉闭环伺服（tap 的地基）
- [ ] 光标检测器（模板匹配，带漂移自校准）
- [ ] 伺服控制器：检测→计算 Δ→发送→再检测，小步收敛循环
- [ ] 参数调优：步进粒度、超时、失败回退（重新全局检测）
- 验收：任意两点间移动点击，100 次成功率 ≥ 95%，单次平均 < 1.5s

### 阶段 4：HidActionHandler（动作层，对齐 Android 动作集）
- [ ] `execute(action, w, h)` 接口实现：tap/swipe/type/scroll 底层原语
- [ ] launch_app → Spotlight 宏（Cmd+Space → 拼音 → 候选确认 → Return）
- [ ] type 中文 IME 交互（拼音 + 截图看候选 + 方向键选字）
- [ ] 键盘快捷键映射表（Home/切 App/通知中心）
- [ ] 安全面遮蔽检测 → 盲操作模式或上报
- 验收：Android 版 prompt 原动作集在 iOS 上逐个通过（模型协议零改动）

### 阶段 5：整合与体验
- [ ] agent_ios.py 接入 HidActionHandler（WDA 执行体退役或并存可选）
- [ ] 语音任务端到端：ASR 唤醒 → 文字任务 → iOS 执行
- [ ] HandDetecter 手势映射到 iOS 动作（tap/swipe 直接复用）
- [ ] vd 虚拟桌面纳入 iPhone 镜像卡片（窗口归属/黑背景一致）
- 验收：真实语音任务（"打开微信发消息给 X"）在 iOS 上完整跑通

### 依赖关系
```
阶段 0 ─► 阶段 1 ─┐
   │              ├─► 阶段 3 ─► 阶段 4 ─► 阶段 5
   └─► 阶段 2 ────┘
```
阶段 1 与 阶段 2 可并行；阶段 3 依赖两者（固件提供输入、镜像提供视觉反馈）。
