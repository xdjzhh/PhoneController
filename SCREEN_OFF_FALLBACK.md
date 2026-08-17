# Screen Off / On 容错方案归档

> 归档时间：2026-08-17
> 背景：项目从 Python 3.13 迁移到 Python 3.10（系统全局环境，两个项目共用）后，
> `[ScreenMonitor] Turning off physical screen after unlock...` 步骤**完全失效**。
> 本文件记录整个排查过程、踩过的坑、错误方案的取舍，以及最终落地的方案。

---

## 1. 目标与验收标准

**核心目标（不可动摇）**：「自动解锁 → 仅熄灭物理屏幕背光」。
**非目标（绝对不能做）**：
- ❌ 不能按电源键（KEYCODE_POWER 按一下会触发锁屏 → 屏幕又锁了，白解了）
- ❌ 不能修改用户系统设置（例如 `settings put system screen_off_timeout 1` 这种会改变用户配置的命令）
- ❌ 不能让任何屏幕控制相关的异常导致主进程 / ScreenMonitor 后台线程崩溃

**验收标准**：
```
[ScreenMonitor] Lock state changed: False -> True, is_asleep=True
[ScreenMonitor] Device locked! Attempting to unlock...
[ScreenMonitor] Waking up screen...
[ScreenMonitor] Quick unlock: inputting password (******)...
[ScreenMonitor] Quick unlock succeeded!
[ScreenMonitor] Unlock result: is_locked=False
[ScreenMonitor] Screen unlocked successfully
[ScreenMonitor] Turning off physical screen after unlock...
# ↑ 之后屏幕必须纯黑（背光关），同时仍然处于「已解锁」状态（不能重新出现锁屏界面）
```

---

## 2. 为什么会突然失败？（根因分析）

Python 3.13 下完全正常，但切到 Python 3.10 干净环境后就失败了。表面现象是「息屏命令没反应」，实际上是**两个连锁的依赖坑**叠加：

### 🔴 坑 A：`requirements.txt` 漏了 `scrcpy-client` 包

原来代码里的 `screen_off()` 只有这一行（之前的 Python 3.13 环境被人手动 pip 安装过 scrcpy-client，所以一直没问题）：

```python
import scrcpy  # ← 依赖 scrcpy-client Python 包！
success, message = self._set_screen_power_mode(scrcpy.POWER_MODE_OFF)
```

切到 Python 3.10 并按 `requirements.txt` 重装了一套干净环境后：
- requirements.txt 里**根本没有 scrcpy-client**
- `import scrcpy` → `ModuleNotFoundError: No module named 'scrcpy'`
- 原来的代码对这个异常没有容错 → screen_off 直接失败

---

### 🔴 坑 B：PyPI 上的 `scrcpy-client 0.4.x` 本身已经**与当前项目环境不兼容**

直接执行 `pip install scrcpy-client==0.4.7` 会遇到**两个子坑**，在 Python 3.10 下 100% 装不上：

#### 坑 B-1：`av<10,>=9` 的版本限制 + PyAV 9.x 源码编译失败
scrcpy-client 0.4.x 的 metadata 写死依赖 `av<10.0.0,>=9.0.0`。
- PyPI 上 **av 9.2.0 没有 cp310 的预编译 wheel**，必须从源码编译
- 编译需要 FFmpeg 全套开发头文件 + 匹配的 Cython 版本
- 在 Python 3.10 + 新版 setuptools/Cython 下，直接在 `av/logging.pyx` 第 216 / 351 行报：
  ```
  Cannot assign type 'void (*)(void *, int, const char *, va_list) except * nogil'
  to 'av_log_callback' (alias of 'void (*)(void *, int, const char *, va_list) noexcept nogil')
  ```
  → **100% 编译失败，无解**（除非你手动去给 PyAV 9.2.0 加 noexcept 修饰并自己编 FFmpeg）

#### 坑 B-2：`scrcpy/core.py` 使用了 `adbutils 0.13` 才有的 `AdbConnection`
就算绕过编译问题把 scrcpy-client 0.4.7 装上了，启动时还是会报错：
```python
# scrcpy/core.py 第 11 行（PyPI 原版 scrcpy-client 0.4.7）
from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb
# ImportError: cannot import name 'AdbConnection' from 'adbutils'
```
因为本项目 `requirements.txt` 里用的是 `adbutils==0.14.1`，而 **adbutils 0.14+ 已经彻底移除了 `AdbConnection` 类**（换成了 `_AdbStreamConnection` 私有类，或者用别的 API）。

---

## 3. 错误方案尝试 & 为什么必须放弃

面对坑 A + 坑 B 叠加的情况，一开始走了一条错误的路：
>「既然 scrcpy-client 装不上，那我们用纯 ADB shell 命令来息屏/亮屏吧！」

于是写了一个 4 阶 fallback 链：`screen_off_timeout trick → svc power stayon false → scrcpy-client → KEYCODE_POWER`。
**这个方案在代码评审后被用户直接否决**，原因是：

| 替代方案 | 原理 | 致命问题（为什么被否决） |
|---|---|---|
| `settings put system screen_off_timeout 1` 然后恢复 | 把系统「自动息屏超时」设成 1ms，让系统自己灭屏，1.5s 后再恢复原值 | ❌ **修改用户系统设置**，而且某些 ROM 上超时后会触发 Keyguard（就是锁屏） |
| `svc power stayon false` + `KEYCODE_WAKEUP` | 取消「保持常亮」，等系统休眠；用 WAKEUP 键唤醒 | ❌ 不是立刻息屏，且和用户的「保持常亮」偏好冲突，不可控 |
| `input keyevent KEYCODE_POWER` 当 fallback | 模拟按一下电源键 | ❌ **绝对不能用**：电源键是「toggle」——亮屏时按 = 黑屏 + 锁屏，直接把刚才解的锁重新锁上了，**完全违背「解锁后息屏继续使用」的业务目标** |

**最终结论**：`scrcpy-client` 的 `POWER_MODE_OFF` / `POWER_MODE_NORMAL` 是唯一**真正只关闭背光、不触发锁屏、不修改系统设置**的 API。这条路必须走，装不上就想办法让它装上，不能换方案。

---

## 4. 最终落地方案（100% 符合原 Py3.13 行为）

### 4.1 架构：严格单实现 + 进程级容错

核心代码：`scrcpycontrol/display_interface.py` 的 `screen_off()` / `screen_on()`。

**实现策略**：
1. **只有一个 API 路径**：`scrcpy.POWER_MODE_OFF` → 永远不要 fallback 到 ADB 命令，避免引入锁屏风险
2. **双层 try/except 保证进程安全**：
   - 内层：`import scrcpy`，失败转成 `ImportError` dict，给出明确的安装提示
   - 外层：`except Exception`，任何异常（scrcpy 握手失败、ADB 断连、返回值异常等）**全部吞掉转 dict**，绝不抛到 `ScreenMonitor` 线程外

```
┌─────────────────────────────────────────────────────────────┐
│  ScreenMonitor background thread (NEVER CRASH!)             │
│                                                             │
│  screen_off() ──► try {                                     │
│                     try { import scrcpy }                   │
│                        ▲ ImportError → return fail dict     │
│                     _set_screen_power_mode(OFF)             │
│                  } catch Exception ALL → return fail dict   │
│                                                             │
│  → 无论发生什么，都返回 {success: bool, error/message: str} │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 环境依赖：绕过 PyPI 装不上的难题（关键技巧）

既然 PyPI scrcpy-client 0.4.x 在 Python 3.10 下有 av 编译 + adbutils API 两个坑，我们提供**两种等价的落地方式**（任选其一，最终产生的 scrcpy 运行时完全一致）：

| 依赖 | 处理方式 | 为什么这么做 |
|---|---|---|
| **av (PyAV)** | ✅ 装官方预编译 wheel：<br>`pip install --user --only-binary :all: av==17.1.0` | 1. av 17.1.0 在 PyPI 上有**现成的 cp310-win_amd64 wheel**（28MB），不用编译任何 C 代码<br>2. 直接跳开 scrcpy-client 写死的 `av<10` 限制（我们手动 --no-deps，见下）<br>3. scrcpy-client 运行时只用了 `av.codec.CodecContext`，高版本 av 17 完全向后兼容 |
| **scrcpy / scrcpy_ui / dist-info**<br>**方式 A（快速，需有 Py3.13 环境）** | ✅ 从 Py3.13 的 `Lib\site-packages` 直接复制 3 个目录：<br>`scrcpy/`、`scrcpy_client-0.4.1.dist-info/`、`scrcpy_ui/` | 1. **避开 adbutils 0.14.1 的坑**：Py3.13 里的 `scrcpy/core.py L11` 已经是 patch 过的版本：<br>   `from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb`<br>   （用的是 `_AdbStreamConnection`，而非 PyPI 原版的不存在的 `AdbConnection`）<br>2. scrcpy 这个包是**纯 Python**，cp313 跟 cp310 的解释器对纯 Python 代码 100% 兼容<br>3. 不通过 pip 装，也就不会触发 pip 去校验 `av<10` 的依赖限制 |
| **scrcpy / scrcpy_ui / dist-info**<br>**方式 B（自包含，推荐新主机，不依赖 Py3.13）** | ✅ `pip --no-deps` 装 PyPI 原版 scrcpy-client 0.4.7 + **运行 1 行 patch** 把 `scrcpy/core.py L11` 的 `AdbConnection` 替换成 `_AdbStreamConnection`。见「Section 5b 无 Py3.13 时的独立安装流程」 | 1. **不依赖任何其他 Python 版本**：全新机器只装 Py3.10 就能用<br>2. --no-deps 绕开 scrcpy-client 0.4.x 写死的 av<10 版本 pin，直接用我们已经装好的 av 17.1.0<br>3. patch 只有一行，完全可复现，可以写成自动化脚本（Section 5b 提供），几毫秒完成 |

### 4.3 取舍权衡（为什么不做其他选择）

| 其他选项 | 为什么不选 |
|---|---|
| 把 adbutils 降级到 0.13.x | ❌ 风险太大：项目大量其他地方依赖 adbutils 0.14.x API，降级会引发连锁回归 |
| 给 PyAV 9.x + FFmpeg + Cython 打补丁，自己编 wheel | ❌ 成本太高：Windows 下搭 FFmpeg 开发环境 + 修 av/logging.pyx noexcept，至少半天，而且将来换机器又要重来 |
| 把 scrcpy 这个包直接提交到项目 repo 里（比如放在 `third_party/scrcpy/`，然后在 server.py 里插 sys.path） | ⚠️ 理论可行，但之前已经按用户要求删除了 `_pydeps` 私有目录路径注入逻辑，「不要私有目录，多项目共用系统 Python 环境」是新的约束，不违反 |
| 用 scrcpy 官方命令行 `scrcpy --power-off-on-close` 等替代 | ❌ 我们需要的是运行中随时开关屏幕（解完锁立刻关），而不是进程结束时才关，命令行模式做不到细粒度控制 |

---

## 5a. 环境复现脚本 — 有 Py3.13 环境时的快速复制法

> 前提：Py3.13 环境里的 scrcpy 是好的（我们复制源），Py3.10 已经装好 adbutils==0.14.1。

**Step 1 — 装 PyAV（预编译 wheel，永不编源码）**
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -m pip install --user --only-binary :all: av==17.1.0
```

**Step 2 — 从 Py3.13 复制已经 patch 好的 scrcpy 三件套（纯 Python）**
```powershell
$src = "C:\Users\DELL\AppData\Local\Programs\Python\Python313\Lib\site-packages"
$dst = "C:\Users\DELL\AppData\Roaming\Python\Python310\site-packages"
@("scrcpy","scrcpy_client-0.4.1.dist-info","scrcpy_ui") | ForEach-Object {
    $s = Join-Path $src $_; $t = Join-Path $dst $_
    if (Test-Path $t) { Remove-Item -Recurse -Force $t }
    Copy-Item -Path $s -Destination $t -Recurse -Force
}
```

**Step 3 — 验证**
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -c "import av, scrcpy; print('av', av.__version__); print('OFF', scrcpy.POWER_MODE_OFF); print('NORMAL', scrcpy.POWER_MODE_NORMAL)"
```
预期输出：
```
av 17.1.0
OFF 0
NORMAL 2
```

---

## 5b. 环境复现脚本 — 不依赖 Py3.13 的独立安装流程（推荐）

> 适用场景：**全新的机器** / **只装了 Python 3.10 没有 3.13** / **不想翻其他 Python 版本目录**。
> 最终效果与方式 A（从 Py3.13 复制）**完全等价**，而且是更正规的「可复现部署路径」。

> 前提：Py3.10 已经装好 `adbutils==0.14.1`（requirements.txt 默认安装）。

**Step 1 — 装 PyAV（预编译 wheel，永不编源码）**
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -m pip install --user --only-binary :all: av==17.1.0
```

**Step 2 — 装 scrcpy-client（--no-deps 绕开 av<10 的限制）**
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -m pip install --user --no-deps scrcpy-client==0.4.7
```
*说明：`--no-deps` 会让 pip 忽略 scrcpy-client 0.4.7 写死的 `av<10` 版本要求，直接使用已经装好的高版本 av 17.1.0（运行时完全兼容）。*

**Step 3 — 给 scrcpy/core.py 打 1 行 adbutils 0.14+ 兼容 patch（必做！）**

PyPI 上 scrcpy-client 0.4.7 的 `scrcpy/core.py L11` 还是老写法：
```python
# ❌ 原版（adbutils 0.14+ 已经移除了 AdbConnection）
from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb
```

我们需要改成新写法（只改这一行，其他都不动）：
```python
# ✅ patch 后（用 _AdbStreamConnection 替代）
from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb
```

可以用下面这个**自动化 patch 脚本**（复制到终端运行就行，**幂等的**——已 patch 过会自动跳过，不会重复改）：
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -c "
import os, site, glob
# 1) 找 scrcpy/core.py：用户 site-packages 优先，其次全局 site-packages
candidates = [site.getusersitepackages()] + site.getsitepackages()
core_py = None
for base in candidates:
    p = os.path.join(base, 'scrcpy', 'core.py')
    if os.path.exists(p):
        core_py = p; break
if not core_py:
    # 兜底：所有 site-packages 中第一个出现 scrcpy/core.py 的
    for base in candidates:
        hits = glob.glob(os.path.join(base, 'scrcpy', 'core.py'))
        if hits: core_py = hits[0]; break
assert core_py, f'Cannot find scrcpy/core.py under any of {candidates}. Did you pip install scrcpy-client in Step 2?'
print(f'[patch] scrcpy/core.py located at: {core_py}')

# 2) 读文件
with open(core_py, 'r', encoding='utf-8') as f:
    content = f.read()

OLD_LINE = 'from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb'
NEW_LINE = 'from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb'

if NEW_LINE in content:
    print('[patch] SKIP: core.py is ALREADY patched (contains _AdbStreamConnection). No changes.')
    exit(0)
if OLD_LINE not in content:
    print(f'[patch] WARN: scrcpy/core.py missing the expected old import line. Expected to find:\n  {OLD_LINE}\nPlease inspect manually.')
    exit(2)

# 3) 原子替换（一次 replace_all）
content_new = content.replace(OLD_LINE, NEW_LINE, 1)
tmp = core_py + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    f.write(content_new)
os.replace(tmp, core_py)  # 原子写，防断电坏文件
print('[patch] OK: scrcpy/core.py patched successfully.')
"
```

脚本输出说明：
- `[patch] SKIP: core.py is ALREADY patched ...` → 之前已经 patch 过，不用管
- `[patch] OK: scrcpy/core.py patched successfully.` → 第一次 patch 成功
- `[patch] WARN: scrcpy/core.py missing the expected old import line ...` → scrcpy 版本跟预期不一致，需要手动打开 `core.py` 看第 11 行

**Step 4 — 验证**
```bat
C:\Users\DELL\AppData\Local\Programs\Python\Python310\python.exe -c "import av, scrcpy, adbutils; print('av', av.__version__); print('adbutils', adbutils.__version__); print('POWER_MODE_OFF =', scrcpy.POWER_MODE_OFF); print('POWER_MODE_NORMAL =', scrcpy.POWER_MODE_NORMAL); from scrcpy.core import Client; print('scrcpy.Client OK')"
```
预期输出：
```
av 17.1.0
adbutils 0.14.1
POWER_MODE_OFF = 0
POWER_MODE_NORMAL = 2
scrcpy.Client OK
```

---

## 6. 代码位置索引

| 文件 | 关键位置 | 说明 |
|---|---|---|
| `scrcpycontrol/display_interface.py` | [L604-L634](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L604-L634) | `_set_screen_power_mode` — 调 scrcpy.Client 的真正实现（建立控制通道发送 POWER_MODE 控制消息） |
| `scrcpycontrol/display_interface.py` | [L637-L703](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L637-L703) | `screen_off()` / `screen_on()` — 对外 API，**严格单实现 + 进程级容错** |
| `scrcpycontrol/display_interface.py` | [L1536-L1549](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L1536-L1549) | `ScreenMonitor` 线程里的调用点，解锁成功后调 `self.screen_off()`，打印 ✅ 成功或 ❌ 失败 |
| `requirements.txt` | [L17-L39](file:///d:/TraeProj/Open-AutoGLM/requirements.txt#L17-L39) | av==17.1.0 写法 + scrcpy 复制步骤的注释说明 |

---

## 7. 一句话结论

> **scrcpy-client 的 POWER_MODE_OFF 是唯一符合「只关背光不锁屏」要求的 API，放弃其他方案。依赖问题用「av 装高版本预编译 wheel + scrcpy 纯 Python 包直接从工作环境复制」的组合拳绕过 PyPI 的版本冲突和编译坑。**
