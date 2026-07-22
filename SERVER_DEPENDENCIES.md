# Server 模式依赖安装指南

## ✅ 已安装的包

### Flask 及其依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| **flask** | 3.1.3 | Web 框架核心 |
| **werkzeug** | 3.1.8 | WSGI 工具库 |
| **jinja2** | 3.1.6 | 模板引擎 |
| **click** | 8.4.1 | 命令行工具 |
| **itsdangerous** | 2.2.0 | 安全签名 |
| **markupsafe** | 3.0.3 | HTML 转义 |
| **blinker** | 1.9.0 | 信号系统 |

---

## 📦 依赖关系图

```
flask (3.1.3)
├── werkzeug (>=3.1.0) → 3.1.8
├── jinja2 (>=3.1.2) → 3.1.6
├── click (>=8.1.3) → 8.4.1
├── itsdangerous (>=2.2.0) → 2.2.0
├── markupsafe (>=2.1.1) → 3.0.3
└── blinker (>=1.9.0) → 1.9.0
```

---

## 🔍 验证安装

### 方法 1：检查 Flask 版本

```bash
python -c "import flask; print(f'✅ Flask version: {flask.__version__}')"
```

**输出**：
```
✅ Flask version: 3.1.3
```

---

### 方法 2：检查所有依赖

```bash
pip list | findstr -i "flask werkzeug jinja2 click"
```

**输出**：
```
flask                 3.1.3
werkzeug              3.1.8
jinja2                3.1.6
click                 8.4.1
```

---

### 方法 3：测试 server.py 导入

```bash
cd c:\Users\Administrator\Documents\trae_projects\myproject\Open-AutoGLM
python -c "from flask import Flask, request, jsonify; print('✅ All imports successful')"
```

**输出**：
```
✅ All imports successful
```

---

## 🚀 启动 Server

### 基本启动

```bash
cd c:\Users\Administrator\Documents\trae_projects\myproject\Open-AutoGLM
python server.py
```

**输出**：
```
🔧 Initializing Phone Agent...
✅ Agent initialized successfully
🚀 Starting server on 127.0.0.1:8080
📡 API endpoints:
   POST http://127.0.0.1:8080/run
   GET  http://127.0.0.1:8080/status
   POST http://127.0.0.1:8080/reset
   GET  http://127.0.0.1:8080/health

Press Ctrl+C to stop
```

---

### 自定义端口

```bash
python server.py --port 9000
```

**输出**：
```
🚀 Starting server on 127.0.0.1:9000
```

---

### 指定主机

```bash
python server.py --host 0.0.0.0 --port 8080
```

**说明**：
- `127.0.0.1`：仅本机访问
- `0.0.0.0`：允许局域网访问

---

### 自定义模型配置

```bash
python server.py \
  --base-url http://localhost:8000/v1 \
  --model autoglm-phone-9b \
  --apikey your-api-key
```

---

## 📡 API 端点

### 1. 执行任务

**请求**：
```bash
curl -X POST http://127.0.0.1:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task": "打开微信"}'
```

**响应**：
```json
{
  "success": true,
  "task": "打开微信",
  "result": "任务完成"
}
```

---

### 2. 查看状态

**请求**：
```bash
curl http://127.0.0.1:8080/status
```

**响应**：
```json
{
  "status": "running",
  "agent_initialized": true,
  "step_count": 5
}
```

---

### 3. 重置 Agent

**请求**：
```bash
curl -X POST http://127.0.0.1:8080/reset
```

**响应**：
```json
{
  "success": true,
  "message": "Agent reset"
}
```

---

### 4. 健康检查

**请求**：
```bash
curl http://127.0.0.1:8080/health
```

**响应**：
```json
{
  "status": "healthy"
}
```

---

## 🐍 Python 客户端示例

### 基本使用

```python
import requests

# 执行任务
response = requests.post(
    'http://127.0.0.1:8080/run',
    json={'task': '打开微信'}
)

result = response.json()
print(result)
```

---

### 完整示例

```python
import requests
import time

BASE_URL = 'http://127.0.0.1:8080'

def run_task(task: str):
    """执行任务"""
    response = requests.post(
        f'{BASE_URL}/run',
        json={'task': task}
    )
    return response.json()

def get_status():
    """获取状态"""
    response = requests.get(f'{BASE_URL}/status')
    return response.json()

def reset_agent():
    """重置 Agent"""
    response = requests.post(f'{BASE_URL}/reset')
    return response.json()

# 使用示例
if __name__ == '__main__':
    # 检查状态
    status = get_status()
    print(f"状态: {status}")
    
    # 执行任务
    result = run_task("打开微信")
    print(f"结果: {result}")
    
    # 重置 Agent
    reset = reset_agent()
    print(f"重置: {reset}")
```

---

## 🔧 故障排查

### 问题 1：ModuleNotFoundError: No module named 'flask'

**原因**：Flask 未安装

**解决**：
```bash
pip install flask
```

---

### 问题 2：端口被占用

**错误信息**：
```
OSError: [WinError 10048] Address already in use
```

**解决**：
```bash
# 方法 1：更换端口
python server.py --port 9000

# 方法 2：查找并关闭占用进程
netstat -ano | findstr :8080
taskkill /PID <进程ID> /F
```

---

### 问题 3：Agent 初始化失败

**错误信息**：
```
Error: Agent not initialized
```

**解决**：
```bash
# 检查模型配置
python server.py \
  --base-url http://localhost:8000/v1 \
  --model autoglm-phone-9b \
  --apikey your-api-key
```

---

### 问题 4：请求超时

**错误信息**：
```
ReadTimeoutError: HTTPConnectionPool
```

**解决**：
```python
import requests

# 增加超时时间
response = requests.post(
    'http://127.0.0.1:8080/run',
    json={'task': '打开微信'},
    timeout=300  # 5 分钟
)
```

---

## 📊 性能优化

### 1. 使用多线程

```python
# server.py 中已启用
app.run(host=args.host, port=args.port, debug=False, threaded=True)
```

---

### 2. 使用 Gunicorn（Linux/Mac）

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8080 server:app
```

**说明**：
- `-w 4`：4 个 worker 进程
- `-b 0.0.0.0:8080`：绑定地址和端口

---

### 3. 使用 Waitress（Windows）

```bash
pip install waitress
waitress-serve --port=8080 --threads=4 server:app
```

---

## 🔒 安全建议

### 1. 使用 HTTPS

```python
from flask_sslify import SSLify

if __name__ == '__main__':
    sslify = SSLify(app)
    app.run(host='0.0.0.0', port=443, ssl_context='adhoc')
```

---

### 2. 添加 API Key 认证

```python
API_KEY = "your-secret-key"

@app.before_request
def check_api_key():
    if request.path == '/health':
        return
    
    api_key = request.headers.get('X-API-Key')
    if api_key != API_KEY:
        return jsonify({'error': 'Invalid API key'}), 401
```

**客户端调用**：
```python
response = requests.post(
    'http://127.0.0.1:8080/run',
    json={'task': '打开微信'},
    headers={'X-API-Key': 'your-secret-key'}
)
```

---

### 3. 限制访问 IP

```python
from flask import request

ALLOWED_IPS = ['127.0.0.1', '192.168.1.100']

@app.before_request
def limit_remote_addr():
    client_ip = request.remote_addr
    if client_ip not in ALLOWED_IPS:
        return jsonify({'error': 'Access denied'}), 403
```

---

## 📝 requirements.txt 更新

已更新 [`requirements.txt`](file:///c:/Users/Administrator/Documents/trae_projects/myproject/open-autoglm/requirements.txt)：

```txt
Pillow>=12.0.0
openai>=2.9.0

# For iOS Support
requests>=2.31.0

# For Server Mode
flask>=3.0.0

# For Model Deployment
# sglang>=0.5.6.post1
# vllm>=0.12.0
# transformers>=5.0.0rc0
```

---

## 🎉 总结

### ✅ 已完成

1. ✅ 安装 Flask 3.1.3 及所有依赖
2. ✅ 验证所有包导入正常
3. ✅ 更新 requirements.txt
4. ✅ 创建完整使用文档

### 🚀 快速开始

```bash
# 1. 启动服务
python server.py

# 2. 测试 API
curl http://127.0.0.1:8080/health

# 3. 执行任务
curl -X POST http://127.0.0.1:8080/run \
  -H "Content-Type: application/json" \
  -d '{"task": "打开微信"}'
```

### 📚 相关文档

- [`server.py`](file:///c:/Users/Administrator/Documents/trae_projects/myproject/Open-AutoGLM/server.py) - Server 源码
- [`requirements.txt`](file:///c:/Users/Administrator/Documents/trae_projects/myproject/open-autoglm/requirements.txt) - 依赖列表
- [`SERVER_MODE_GUIDE.md`](file:///c:/Users/Administrator/Documents/trae_projects/myproject/Open-AutoGLM/SERVER_MODE_GUIDE.md) - Server 模式完整指南

---

**恭喜！Server 模式依赖已全部安装完成！** 🎉