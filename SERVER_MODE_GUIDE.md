# Phone Agent 服务模式配置指南

## 📋 概述

Open-AutoGLM **默认是命令行工具（CLI）**，但也可以改造成**后台服务（Server）**模式。

---

## 🎯 两种运行模式对比

| 特性 | CLI 模式（默认） | Server 模式（新增） |
|------|----------------|-------------------|
| **运行方式** | 每次执行 `python main.py` | 启动服务后持续运行 |
| **进程管理** | 临时进程，执行完退出 | 后台服务，持续运行 |
| **资源占用** | 仅运行时占用 | 持续占用内存 |
| **适用场景** | 脚本调用、偶尔使用 | API 服务、频繁调用 |
| **初始化开销** | 每次都初始化 | 一次初始化，多次使用 |
| **并发支持** | ❌ 单任务 | ✅ 多任务（需配置） |

---

## 🚀 CLI 模式（默认）

### 使用方式

```bash
# 单次执行
python main.py "打开微信发消息给张三"

# 交互模式（类似 REPL）
python main.py
# 然后连续输入多个任务
```

### 特点

- ✅ **简单**：无需管理后台进程
- ✅ **安全**：每次都是独立进程
- ✅ **灵活**：随时切换配置
- ❌ **开销**：每次都重新初始化
- ❌ **不便**：不适合频繁调用

---

## 🌐 Server 模式（新增）

### 安装依赖

```bash
pip install flask
```

### 启动服务

```bash
# 基本启动
python server.py --port 8080

# 指定模型服务
python server.py \
  --port 8080 \
  --base-url http://localhost:8000/v1 \
  --model autoglm-phone-9b \
  --apikey EMPTY
```

### API 端点

#### 1. 执行任务

```bash
POST http://localhost:8080/run
Content-Type: application/json

{
  "task": "打开微信发消息给张三"
}
```

**响应**：
```json
{
  "success": true,
  "task": "打开微信发消息给张三",
  "result": "任务执行成功"
}
```

#### 2. 查看状态

```bash
GET http://localhost:8080/status
```

**响应**：
```json
{
  "status": "running",
  "agent_initialized": true,
  "step_count": 5
}
```

#### 3. 重置 Agent

```bash
POST http://localhost:8080/reset
```

#### 4. 健康检查

```bash
GET http://localhost:8080/health
```

---

## 💻 代码调用示例

### Python

```python
import requests

# 执行任务
response = requests.post(
    'http://localhost:8080/run',
    json={'task': '打开微信发消息给张三'}
)

result = response.json()
print(result['result'])

# 查看状态
response = requests.get('http://localhost:8080/status')
print(response.json())
```

### JavaScript/Node.js

```javascript
// 执行任务
fetch('http://localhost:8080/run', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ task: '打开微信发消息给张三' })
})
.then(res => res.json())
.then(data => console.log(data.result));
```

### cURL

```bash
# 执行任务
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task":"打开微信发消息给张三"}'

# 查看状态
curl http://localhost:8080/status
```

---

## 🎯 使用场景对比

### CLI 模式适合：

- ✅ 偶尔使用
- ✅ 脚本自动化
- ✅ 调试和测试
- ✅ 不同配置频繁切换

**示例**：
```bash
# 定时任务脚本
#!/bin/bash
python main.py "打开天气查看预报"
python main.py "发微信给老婆说晚上不回家吃饭"
```

---

### Server 模式适合：

- ✅ Web 应用后端
- ✅ 频繁调用
- ✅ 多用户并发访问
- ✅ 需要低延迟响应

**示例**：
```python
# Flask Web 应用
from flask import Flask, request
import requests

app = Flask(__name__)

@app.route('/voice_command', methods=['POST'])
def voice_command():
    # 语音转文本
    text = speech_to_text(request.files['audio'])
    
    # 调用 Phone Agent
    response = requests.post(
        'http://localhost:8080/run',
        json={'task': text}
    )
    
    return response.json()
```

---

## 📊 性能对比

| 指标 | CLI 模式 | Server 模式 |
|------|---------|------------|
| **首次响应** | ~2-3 秒（含初始化） | ~0.5 秒 |
| **后续响应** | ~2-3 秒（每次都初始化） | ~0.5 秒 |
| **内存占用** | 运行时 ~200MB | 持续 ~200MB |
| **CPU 占用** | 运行时高 | 空闲时低 |

---

## 🔧 高级配置

### 1. 使用 Gunicorn 生产部署

```bash
# 安装
pip install gunicorn

# 启动
gunicorn -w 4 -b 0.0.0.0:8080 server:app
```

### 2. 使用 systemd 管理（Linux）

创建服务文件 `/etc/systemd/system/phone-agent.service`：

```ini
[Unit]
Description=Phone Agent Service
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/Open-AutoGLM
ExecStart=/usr/bin/python3 /path/to/Open-AutoGLM/server.py --port 8080
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl start phone-agent
sudo systemctl enable phone-agent  # 开机自启
```

### 3. 使用 Docker 容器化

创建 `Dockerfile`：

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt flask

EXPOSE 8080

CMD ["python", "server.py", "--port", "8080"]
```

运行容器：
```bash
docker build -t phone-agent .
docker run -p 8080:8080 phone-agent
```

---

## ⚠️ 注意事项

### CLI 模式

- ✅ 无需担心进程管理
- ✅ 每次都是干净的环境
- ❌ 不适合高频调用

### Server 模式

- ⚠️ 需要管理后台进程
- ⚠️ 注意内存泄漏
- ⚠️ 需要处理并发（多线程/多进程）
- ✅ 低延迟、高性能

---

## 🎉 推荐方案

### 个人使用（偶尔）

```bash
# 直接使用 CLI
python main.py "任务描述"
```

### 团队使用（频繁）

```bash
# 启动服务
python server.py --port 8080

# 或者使用进程管理
nohup python server.py --port 8080 &
```

### 生产环境

```bash
# 使用 Gunicorn + Nginx
gunicorn -w 4 -b 127.0.0.1:8080 server:app

# Nginx 反向代理
server {
    listen 80;
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

---

## 📝 快速开始

### CLI 模式

```bash
# 1. 单次执行
python main.py "打开美团点外卖"

# 2. 交互模式
python main.py
# 输入多个任务...
```

### Server 模式

```bash
# 1. 安装依赖
pip install flask

# 2. 启动服务
python server.py --port 8080

# 3. 调用 API
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task":"打开微信"}'
```

---

## 🔗 相关文件

- [`main.py`](main.py) - CLI 入口
- [`server.py`](server.py) - HTTP 服务入口
- [`phone_agent/agent.py`](phone_agent/agent.py) - Agent 核心

---

## 💡 总结

**Open-AutoGLM 默认是 CLI 工具**，每次运行 `main.py` 执行任务。

**如果需要服务模式**：
1. 使用新增的 `server.py`
2. 启动后作为后台服务运行
3. 通过 HTTP API 调用

**选择建议**：
- 个人使用、偶尔调用 → CLI 模式
- Web 应用、频繁调用 → Server 模式
