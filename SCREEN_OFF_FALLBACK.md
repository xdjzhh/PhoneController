# Screen Off / On 容错方案归档

> 归档时间：2026-08-17
> 背景：项目从 Python 3.13 迁移到 Python 3.10（系统全局环境，两个项目共用）后，
> `[ScreenMonitor] Turning off physical screen after unlock...` 步骤**完全失效**。
> 本文件记录整个排查过程、踩过的坑、错误方案的取舍，以及最终落地的方案。
>
> **2026-09-26 订正**：经从 PyPI 重新下载 wheel 逐字节比对，新主机安装路径
> （原 §5b）从「scrcpy-client 0.4.7 + 手工 patch」简化为
> **「scrcpy-client 0.4.1，零 patch」**。三处关键事实更正：
> 1. 0.4.1 的 wheel 上游原生就是 `_AdbStreamConnection` 新写法，元数据 pin
>    `adbutils>=0.14.1,<0.15`，在 adbutils 0.14.1 下**本来就不需要 patch**；
> 2. 本机从 Py3.13 复制来的 0.4.1 也从未被手工 patch（core.py 与 PyPI 官方 wheel
>    SHA256 完全一致），旧文「Py3.13 里的是 patch 过的版本」系误判；
> 3. 真正带旧 `AdbConnection` import、需要 patch 的是 **0.4.2+（含 0.4.7）**，
>    它们反而 pin `adbutils>=1.0` 且捆绑更旧的 scrcpy-server 1.20（0.4.1 为 1.24）。
>    安装时请**钉死 0.4.1**，见下文订正后的 §5b。

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

### 🔴 坑 B：直接按普通方式装 PyPI `scrcpy-client` 会失败（2026-09-26 订正：有一个例外版本 0.4.1）

直接执行 `pip install scrcpy-client`（当时最新为 0.4.7）会遇到**两个子坑**，在 Python 3.10 下 100% 装不上/跑不起来。注意：**av<10 的坑对全部 0.4.x（含 0.4.1）都成立，必须 --no-deps；而 adbutils 不兼容的坑只对 0.4.2+ 成立，0.4.1 可直接用**（见坑 B-2）。

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

#### 🔴 坑 B-2：新版 `scrcpy-client`（0.4.2+，含 0.4.7）与本项目 adbutils 不兼容
（2026-09-26 订正：**0.4.1 不在此列**，它是唯一与 adbutils 0.14.1 原生兼容的版本）

直接执行 `pip install --no-deps scrcpy-client==0.4.7` 会发现它的 `scrcpy/core.py` 第 11 行仍是老写法：
```python
# scrcpy/core.py 第 11 行（PyPI scrcpy-client 0.4.7）
from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb
# ImportError: cannot import name 'AdbConnection' from 'adbutils'
```
因为本项目 `requirements.txt` 用的是 `adbutils==0.14.1`，而 **adbutils 0.14+ 已经移除了 `AdbConnection` 类**（换成了 `_AdbStreamConnection` 私有类）；0.4.7 元数据还 pin 了 `adbutils>=1.0.8,<2.0.0`，与项目依赖直接冲突。

> ✅ **例外：0.4.1**。实测从 PyPI 下载的 0.4.1 官方 wheel，第 11 行原生就是
> `from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb`，
> 元数据 pin `adbutils>=0.14.1,<0.15`——在本项目环境下**直接可 import，无需任何 patch**。
> 因此最终方案钉死 0.4.1（见 §4.2 / §5b），不使用 0.4.2 及以上版本。

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
| **scrcpy / scrcpy_ui / dist-info**<br>**方式 A（快速，需有 Py3.13 环境）** | ✅ 从 Py3.13 的 `Lib\site-packages` 直接复制 3 个目录：<br>`scrcpy/`、`scrcpy_client-0.4.1.dist-info/`、`scrcpy_ui/` | 1. 0.4.1 的 `scrcpy/core.py L11` 上游原生就是新写法：<br>   `from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb`<br>   （2026-09-26 订正：这不是谁手工 patch 的，0.4.1 官方 wheel 本就如此；已用 SHA256 与 PyPI wheel 逐字节比对确认）<br>2. scrcpy 这个包是**纯 Python**，cp313 跟 cp310 的解释器对纯 Python 代码 100% 兼容<br>3. 不通过 pip 装，也就不会触发 pip 去校验 `av<10` 的依赖限制 |
| **scrcpy / scrcpy_ui / dist-info**<br>**方式 B（自包含，推荐新主机，不依赖 Py3.13）** | ✅ `pip --no-deps` 装 PyPI 官方 **scrcpy-client 0.4.1** 即可，**无需 patch**（2026-09-26 简化；旧文写的 0.4.7+patch 已废弃） | 1. **不依赖任何其他 Python 版本**：全新机器只装 Py3.10 就能用<br>2. --no-deps 绕开 scrcpy-client 元数据写死的 av<10 版本 pin，直接用已装好的 av 17.1.0<br>3. 0.4.1 原生兼容 adbutils 0.14.1（pin `adbutils>=0.14.1,<0.15`），装上即用；切勿用 0.4.2+（pin adbutils≥1.0、需手工 patch、捆绑 server 反而是更旧的 1.20；0.4.1 捆绑 1.24） |

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

**Step 2 — 从 Py3.13 复制 0.4.1 的 scrcpy 三件套（纯 Python，0.4.1 上游本就兼容 adbutils 0.14，无需 patch）**
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
> 最终效果与方式 A（从 Py3.13 复制 0.4.1）**完全等价**，而且是更正规的「可复现部署路径」。
>
> **2026-09-26 订正**：本节原来写的是「装 0.4.7 + 1 行手工 patch」，共 4 步。
> 经实测 PyPI 官方 **0.4.1 wheel 原生兼容 adbutils 0.14.1，无需任何 patch**，
> 现已简化为 3 步（旧的 patch 脚本对 0.4.1 不需要，也不应使用——它只适用于
> 误装了 0.4.2+ 的情况，而本项目根本不应安装那些版本）。

> 前提：Py3.10 已经装好 `adbutils==0.14.1`（requirements.txt 默认安装）。
> 下面命令中的解释器路径请按新机实际用户名调整，或直接用 `py -3.10`。

**Step 1 — 装 PyAV（预编译 wheel，永不编源码）**
```bat
py -3.10 -m pip install --user --only-binary :all: av==17.1.0
```

**Step 2 — 装 scrcpy-client 0.4.1（--no-deps 绕开 av<10 的限制，无需 patch）**
```bat
py -3.10 -m pip install --user --no-deps scrcpy-client==0.4.1
```
*说明：*
- *`--no-deps` 让 pip 忽略 scrcpy-client 元数据写死的 `av<10` 要求，直接使用 Step 1 装好的 av 17.1.0（运行时仅用 `av.codec.CodecContext`，完全兼容）；*
- ***版本必须钉死 0.4.1**：它的 `scrcpy/core.py` 第 11 行上游原生就是
  `from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb`，
  元数据 pin `adbutils>=0.14.1,<0.15`，在本项目 adbutils 0.14.1 下直接可 import；
  0.4.2+（含 0.4.7）pin `adbutils>=1.0`、第 11 行仍是已删除的 `AdbConnection`，
  必须手工 patch 才能跑，且捆绑的 scrcpy-server 反而是更旧的 1.20（0.4.1 为 1.24）。*

**Step 3 — 验证**
```bat
py -3.10 -c "import av, scrcpy, adbutils; from scrcpy.core import Client; print('av', av.__version__); print('adbutils', adbutils.__version__); print('POWER_MODE_OFF =', scrcpy.POWER_MODE_OFF); print('POWER_MODE_NORMAL =', scrcpy.POWER_MODE_NORMAL); print('scrcpy.Client OK')"
```
预期输出：
```
av 17.1.0
adbutils 0.14.1
POWER_MODE_OFF = 0
POWER_MODE_NORMAL = 2
scrcpy.Client OK
```

> 若误装成 0.4.2+ 导致 `ImportError: cannot import name 'AdbConnection'`：
> 先 `py -3.10 -m pip uninstall -y scrcpy-client`，再按 Step 2 重装 0.4.1，
> 不要走手工 patch——降级 adbutils 或改包源码都会给后续部署埋坑。

---

## 6. 代码位置索引

| 文件 | 关键位置 | 说明 |
|---|---|---|
| `scrcpycontrol/display_interface.py` | [L727-L767](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L727-L767) | `_set_screen_power_mode` — 调 scrcpy.Client 的真正实现（建立控制通道发送 POWER_MODE 控制消息） |
| `scrcpycontrol/display_interface.py` | [L770-L835](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L770-L835) | `screen_off()` / `screen_on()` — 对外 API，**严格单实现 + 进程级容错** |
| `scrcpycontrol/display_interface.py` | [L1541-L1553](file:///d:/TraeProj/Open-AutoGLM/scrcpycontrol/display_interface.py#L1541-L1553) | `ScreenMonitor` 线程里的调用点，解锁成功后调 `self.screen_off()`，打印 ✅ 成功或 ❌ 失败 |
| `requirements.txt` | [L24-L50](file:///d:/TraeProj/Open-AutoGLM/requirements.txt#L24-L50) | av==17.1.0 + scrcpy-client 0.4.1 免 patch 安装步骤的注释说明 |

---

## 7. 一句话结论

> **scrcpy-client 的 POWER_MODE_OFF 是唯一符合「只关背光不锁屏」要求的 API，放弃其他方案。依赖问题的标准解法（2026-09-26 订正）：`av` 装高版本预编译 wheel + `pip --no-deps scrcpy-client==0.4.1`（该版本原生兼容 adbutils 0.14.1，免 patch；切勿用 0.4.2+）；有现成 Py3.13 环境时也可直接复制 0.4.1 三件套。**
