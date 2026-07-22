# SSLKey.log 错误解决方案

## ❌ 问题描述

调用智谱 API 时报错：
```
FileNotFoundError: [Errno 2] No such file or directory: 'sslkey.log'
```

或

```
PermissionError: [Errno 13] Permission denied: 'sslkey.log'
```

---

## 🔍 问题根源

### 原因分析

1. **OpenAI SDK 使用 httpx 作为底层 HTTP 客户端**
2. **httpx 支持 SSL 密钥日志记录**（用于 Wireshark 调试）
3. **环境变量 `SSLKEYLOGFILE` 被设置**，但路径不可写

```python
# openai/client.py 内部使用 httpx
from openai import OpenAI

client = OpenAI(base_url="...", api_key="...")
# httpx 会尝试写入 SSLKEYLOGFILE 指定的文件
```

---

## 🎯 解决方案

### 方案 1：取消 SSLKEYLOGFILE 环境变量（推荐）

#### Windows PowerShell
```powershell
# 临时取消（当前会话）
Remove-Item Env:\SSLKEYLOGFILE

# 或设置为空
$env:SSLKEYLOGFILE=""

# 永久取消（需要管理员权限）
[System.Environment]::SetEnvironmentVariable("SSLKEYLOGFILE", $null, "User")
```

#### Windows CMD
```cmd
# 临时取消
set SSLKEYLOGFILE=

# 永久取消（需要重启）
setx SSLKEYLOGFILE ""
```

#### Linux/Mac
```bash
# 临时取消
unset SSLKEYLOGFILE

# 或添加到 ~/.bashrc
echo "unset SSLKEYLOGFILE" >> ~/.bashrc
source ~/.bashrc
```

---

### 方案 2：设置可写路径

如果确实需要 SSL 密钥日志（用于网络调试）：

#### Windows
```powershell
# 设置到临时目录
$env:SSLKEYLOGFILE="$env:TEMP\sslkey.log"

# 或在代码中设置
import os
os.environ['SSLKEYLOGFILE'] = os.path.join(os.getenv('TEMP', '.'), 'sslkey.log')
```

#### Linux/Mac
```bash
# 设置到临时目录
export SSLKEYLOGFILE=/tmp/sslkey.log
```

---

### 方案 3：在代码中禁用 SSL 密钥日志

创建包装脚本 `fix_ssl.py`：

```python
import os
import sys

# 在导入 openai 之前执行
if 'SSLKEYLOGFILE' in os.environ:
    print(f"⚠️  检测到 SSLKEYLOGFILE={os.environ['SSLKEYLOGFILE']}")
    print("   这可能导致 sslkey.log 文件错误")
    print("   建议运行：unset SSLKEYLOGFILE 或删除该环境变量")
    print()

# 继续运行主程序
from openai import OpenAI

# 正常使用
client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")
```

---

### 方案 4：修改 main.py 自动处理

在 [`main.py`](main.py) 开头添加：

```python
#!/usr/bin/env python3
"""Phone Agent CLI - AI-powered phone automation."""

import os
import sys

# 修复 SSLKEYLOGFILE 问题
if 'SSLKEYLOGFILE' in os.environ:
    ssl_key_file = os.environ['SSLKEYLOGFILE']
    # 如果路径不可写，取消设置
    try:
        test_path = os.path.dirname(ssl_key_file) or '.'
        if not os.access(test_path, os.W_OK):
            del os.environ['SSLKEYLOGFILE']
            print(f"⚠️  SSLKEYLOGFILE 路径不可写，已取消设置")
    except Exception:
        del os.environ['SSLKEYLOGFILE']

# 原有代码
import argparse
import shutil
# ... 其他导入
```

---

## 🔧 验证修复

### 测试脚本

创建 `test_ssl_fix.py`：

```python
#!/usr/bin/env python3
"""测试 SSL 密钥日志问题是否解决"""

import os
import sys

print("=" * 60)
print("SSLKey.log 问题诊断工具")
print("=" * 60)
print()

# 检查环境变量
print("1. 检查环境变量:")
ssl_key_file = os.environ.get('SSLKEYLOGFILE')
if ssl_key_file:
    print(f"   ❌ SSLKEYLOGFILE 已设置：{ssl_key_file}")
    
    # 检查路径是否可写
    try:
        test_dir = os.path.dirname(ssl_key_file) or '.'
        if os.access(test_dir, os.W_OK):
            print(f"   ✅ 路径可写：{test_dir}")
        else:
            print(f"   ❌ 路径不可写：{test_dir}")
    except Exception as e:
        print(f"   ❌ 路径检查失败：{e}")
else:
    print(f"   ✅ SSLKEYLOGFILE 未设置")

print()

# 测试 OpenAI 客户端
print("2. 测试 OpenAI 客户端:")
try:
    from openai import OpenAI
    
    # 尝试创建客户端（不实际调用）
    client = OpenAI(
        base_url="http://localhost:8000/v1",
        api_key="EMPTY"
    )
    print(f"   ✅ OpenAI 客户端创建成功")
    
except Exception as e:
    print(f"   ❌ OpenAI 客户端创建失败：{e}")

print()

# 测试实际调用（可选）
print("3. 测试 API 调用（可选）:")
try:
    response = client.chat.completions.create(
        model="test",
        messages=[{"role": "user", "content": "test"}],
        max_tokens=10
    )
    print(f"   ✅ API 调用成功")
except Exception as e:
    print(f"   ⚠️  API 调用失败（正常，因为模型可能未运行）: {e}")

print()
print("=" * 60)
print("诊断完成")
print("=" * 60)
```

运行测试：
```bash
python test_ssl_fix.py
```

---

## 📊 常见场景

### 场景 1：开发环境

```bash
# 可能之前设置了用于调试
export SSLKEYLOGFILE=./sslkey.log

# 后来删除了文件但环境变量还在
# 导致报错

# 解决：
unset SSLKEYLOGFILE
```

---

### 场景 2：IDE 配置

某些 IDE（如 PyCharm、VSCode）可能在运行配置中设置了：

**PyCharm**:
```
Run → Edit Configurations → Environment variables
SSLKEYLOGFILE=./sslkey.log
```

**解决**：删除该环境变量

---

### 场景 3：全局配置

可能在 `~/.bashrc`、`~/.zshrc` 或系统环境变量中设置：

```bash
# 检查
grep -r "SSLKEYLOGFILE" ~/.bashrc ~/.zshrc /etc/environment

# 删除相关行
```

---

## 🎯 预防措施

### 1. 项目级 `.env` 文件

在项目根目录创建 `.env`：

```ini
# 明确取消 SSL 密钥日志
SSLKEYLOGFILE=
```

使用 python-dotenv 自动加载：

```python
# main.py
from dotenv import load_dotenv
load_dotenv()  # 会覆盖系统环境变量
```

---

### 2. 启动脚本

创建 `run.bat`（Windows）：

```batch
@echo off
set SSLKEYLOGFILE=
python main.py %*
```

或 `run.sh`（Linux/Mac）：

```bash
#!/bin/bash
unset SSLKEYLOGFILE
python main.py "$@"
```

---

### 3. 文档说明

在 README 中添加：

```markdown
## 常见问题

### Q: 报错 "sslkey.log" 找不到？

**解决**：
```bash
# Windows PowerShell
Remove-Item Env:\SSLKEYLOGFILE

# Linux/Mac
unset SSLKEYLOGFILE
```
```

---

## 🔍 深入理解

### 为什么需要 SSLKEYLOGFILE？

**用途**：调试 HTTPS 流量

```bash
# 1. 设置环境变量
export SSLKEYLOGFILE=./sslkey.log

# 2. 运行程序
python script.py

# 3. Wireshark 捕获流量时加载 sslkey.log
# 可以解密 HTTPS 流量，查看明文内容
```

**对于 Open-AutoGLM**：
- ❌ **不需要** SSL 密钥日志
- ✅ **应该取消**该环境变量

---

## 📝 完整修复流程

### Windows 用户

```powershell
# 步骤 1：检查当前设置
echo $env:SSLKEYLOGFILE

# 步骤 2：临时取消
Remove-Item Env:\SSLKEYLOGFILE

# 步骤 3：运行程序
python main.py --base-url http://localhost:8000/v1 "任务"

# 步骤 4：永久取消（如果需要）
[System.Environment]::SetEnvironmentVariable("SSLKEYLOGFILE", $null, "User")
```

---

### Linux/Mac 用户

```bash
# 步骤 1：检查当前设置
echo $SSLKEYLOGFILE

# 步骤 2：临时取消
unset SSLKEYLOGFILE

# 步骤 3：运行程序
python main.py --base-url http://localhost:8000/v1 "任务"

# 步骤 4：永久取消
echo "unset SSLKEYLOGFILE" >> ~/.bashrc
source ~/.bashrc
```

---

## ✅ 验证清单

- [ ] 环境变量 `SSLKEYLOGFILE` 已取消设置
- [ ] 运行 `python main.py` 不再报错
- [ ] API 调用正常工作
- [ ] 没有 `sslkey.log` 相关文件错误

---

## 🎉 总结

**问题**：环境变量 `SSLKEYLOGFILE` 指向不可写路径

**解决**：取消设置该环境变量

```bash
# 一行命令搞定
unset SSLKEYLOGFILE  # Linux/Mac
Remove-Item Env:\SSLKEYLOGFILE  # Windows PowerShell
```

**预防**：
- 检查 IDE 运行配置
- 检查 shell 配置文件
- 使用启动脚本自动处理

---

## 🔗 相关资源

- [httpx SSL 密钥日志文档](https://www.python-httpx.org/advanced/logging/)
- [Wireshark TLS 解密](https://wiki.wireshark.org/TLS#tls-decryption)
- [OpenAI SDK 文档](https://platform.openai.com/docs/quickstart)
