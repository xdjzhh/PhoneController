# -*- coding: utf-8 -*-
"""
virtual_desktop.py  —  Windows 10 / 11 虚拟桌面管理脚本（零额外配置）

需求规格（严格按用户两条要求实现）：
  1. ensure(N)：检查当前虚拟桌面总数，若 < N  → 自动补充创建直到 = N；若 ≥ N → 不做任何动作。
  2. switch(idx)：虚拟桌面从左→右严格编号为 0,1,2,...,N-1，可准确切换到指定编号的桌面。

桌面布局（2026-09-26 起改为「懒创建」模型，bootstrap 不再创建任何虚拟桌面）：
  · 当前桌面（通常 #0）= 唯一桌面，bootstrap 只在这里启 server.py + 全部 scrcpy
    手机桌面卡片，不新建桌面、不启动 UU；
  · UU 桌面（通常 #1，编号=创建时桌面总数-1）= 用户点主桌面 Dock「电脑」按钮时
    才由 `uu-open` 子命令按需创建，并在其中启动 UU GameViewer + 回程 Dock；
    若该桌面与 UU 均已存在，再次点击只切桌面、不重复创建/启动。
  · 手机主屏（main_display）可由 Dock「手机」按钮在两个桌面间自由嵌入，watcher
    按标题放行，不回迁。

实现：优先使用 pyvda（已在多个 Windows 项目验证）。若未装 → 自动 `pip install pyvda`
      （pyvda 是纯 Python 包，自动处理 Win10 1903 / Win11 之间 VirtualDesktop COM IID 的 GUID
       差异，避免自己手写 IServiceProvider + IVirtualDesktopManagerInternal 两套接口。）

CLI 用法（都可直接双击或命令行调用）：
  python virtual_desktop.py bootstrap [--phoneaddress HOST:PORT]
        # 不建虚拟桌面：在【当前桌面】等 ADB（最长 5min）→ 启 server.py 手机布局
  python virtual_desktop.py uu-open
        # Dock「电脑」按钮调用：有 UU 桌面只切换；没有则新建桌面→启 UU+回程Dock→切过去
  python virtual_desktop.py list                   # 打印总数 + 当前所在桌面编号
  python virtual_desktop.py create                 # 立即新建 1 个虚拟桌面（追加到最右端）
  python virtual_desktop.py remove --index 1       # 删除指定编号的桌面（窗口自动合并到前一个）

Python 模块用法：
  from virtual_desktop import VirtualDesktops
  vd = VirtualDesktops()
  vd.ensure(count=1)
  vd.switch(1)
  print(vd.count(), vd.current_index())
"""
from __future__ import annotations

import subprocess
import sys
from typing import Optional


def _force_utf8_stdio() -> None:
    """detached 子进程（CREATE_NO_WINDOW + stdout 重定向到 .vd_*.log）时，Python 默认
    按系统 ANSI 代码页(cp936/GBK) 编码 stdout，print 里的 emoji（✅/❌/⚠️）会抛
    UnicodeEncodeError 直接打断业务事务——曾导致 vd.create() 建桌成功后打印日志崩溃，
    后续 switch / state 回填全被跳过，下次点击读到 uu_desktop=null 误判"桌面不存在"
    而重复建桌。统一把 stdout/stderr 重配为 UTF-8 + errors=replace，
    使本脚本无论前台运行还是被 detached 重定向都不会因日志字符中断。
    必须在文件最顶部（任何业务 print / import 副作用之前）调用。"""
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is None:
            continue
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_force_utf8_stdio()


# ---------------------------------------------------------------------------
# 后端：pyvda 加载（缺失则自动 pip install）
# ---------------------------------------------------------------------------
_PYVDA_IMPORT_ERR: Optional[ImportError] = None

try:
    import pyvda  # type: ignore
    from pyvda import VirtualDesktop, get_virtual_desktops  # type: ignore
except ImportError as _e:
    pyvda = None  # type: ignore
    VirtualDesktop = None  # type: ignore
    get_virtual_desktops = None  # type: ignore
    _PYVDA_IMPORT_ERR = _e


def _ensure_pyvda_installed() -> None:
    """pyvda 未装时自动 pip install 并 reload；再失败则给用户清晰指引。"""
    global pyvda, VirtualDesktop, get_virtual_desktops, _PYVDA_IMPORT_ERR
    if pyvda is not None:
        return
    print("[virtual_desktop] pyvda 未安装，正在执行自动 pip install pyvda ...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "pyvda"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as pip_e:
        raise RuntimeError(
            "[virtual_desktop] 自动安装 pyvda 失败，请手动执行：\n"
            f"    {sys.executable} -m pip install pyvda\n"
            f"（pip 错误：{pip_e}）"
        ) from pip_e

    # reload
    import importlib
    try:
        pyvda = importlib.import_module("pyvda")
        VirtualDesktop = pyvda.VirtualDesktop
        get_virtual_desktops = pyvda.get_virtual_desktops
        _PYVDA_IMPORT_ERR = None
    except ImportError as reload_e:
        raise RuntimeError(
            "[virtual_desktop] 已 pip install pyvda 但 import 仍失败，请重启脚本。\n"
            f"import 错误：{reload_e}"
        ) from reload_e


# ---------------------------------------------------------------------------
# 主类：VirtualDesktops
# ---------------------------------------------------------------------------
class VirtualDesktops:
    """
    虚拟桌面对象。所有桌面编号严格按「左→右」顺序 0,1,2,...
    """

    def __init__(self, auto_install_pyvda: bool = True) -> None:
        if auto_install_pyvda:
            _ensure_pyvda_installed()
        elif pyvda is None:
            raise RuntimeError(
                f"pyvda 未安装：{_PYVDA_IMPORT_ERR}\n"
                f"请先运行：{sys.executable} -m pip install pyvda"
            )

    # ---------- 核心查询 ----------
    def count(self) -> int:
        """当前虚拟桌面总数。"""
        return len(get_virtual_desktops())

    def current_index(self) -> int:
        """当前位于第几号桌面（左→右 0-based）。"""
        cur = VirtualDesktop.current()
        for idx, d in enumerate(get_virtual_desktops()):
            if d.id == cur.id:
                return idx
        # 兜底（不应触发）：当前桌面不在列表里就返回 0
        return 0

    # ---------- 规格 1：ensure(count=N) ----------
    def ensure(self, count: int = 1) -> int:
        """
        保证至少有 `count` 个虚拟桌面。
          - 已有数 < count → 自动创建补充到 count，返回本次新建个数
          - 已有数 ≥ count → 不做任何动作，返回 0
        懒创建模型下 bootstrap 不再调用本方法（默认 1 = 保持系统现状，不建桌面）；
        UU 桌面由 `uu-open` 子命令在用户点「电脑」按钮时用 create() 按需新建。
        """
        if count < 1:
            raise ValueError(f"ensure count must be >= 1, got {count}")
        current = self.count()
        need = count - current
        if need <= 0:
            print(f"[ensure] 当前虚拟桌面数量={current}，已 ≥ {count} 个，不做任何动作。")
            return 0
        created = 0
        for i in range(need):
            try:
                VirtualDesktop.create()
                created += 1
                new_total = current + created
                print(f"[ensure] 新建第 {new_total - 1} 号虚拟桌面（当前总数 {new_total} / 目标 {count}）")
            except Exception as e:
                print(f"[ensure] 创建第 {i} 个虚拟桌面失败：{e}")
                break
        final = self.count()
        print(f"[ensure] 完成：当前虚拟桌面 {final} 个（本轮新建 {created} 个），编号 0..{final - 1}。")
        return created

    # ---------- 规格 2：switch(idx) ----------
    def switch(self, index: int) -> bool:
        """
        切换到指定编号（0-based，左→右顺序）的虚拟桌面。
          - index 在 [0, count-1] 范围内 → 切换，返回 True
          - index 越界 → 打印错误 + 不动作，返回 False
        """
        total = self.count()
        if index < 0 or index >= total:
            print(
                f"[switch] ❌ 编号越界：index={index} 超出范围 [0, {total - 1}] "
                f"（当前共 {total} 个虚拟桌面）"
            )
            return False
        # 已在目标桌面时绝不再调 go()：Windows 的 SwitchDesktop（pyvda go()）
        # 即使目标就是当前桌面，也会在屏幕中央弹出「桌面N」切换浮窗。
        # bootstrap 懒创建模型下 server 直接在当前桌面启动，launch 强隔离
        # 分支仍会无条件 switch(当前桌面)——启动时误弹"桌面1"即源于此。
        # 无实际切换时直接成功返回（桌面归属本就是当前桌面，隔离语义不变）。
        if self.current_index() == index:
            print(f"[switch] ℹ️  当前已在 {index} 号虚拟桌面，无需切换"
                  f"（跳过 go()，不弹桌面浮窗）。")
            return True
        desktops = get_virtual_desktops()
        target = desktops[index]
        target.go()
        print(f"[switch] ✅ 已切换到 {index} 号虚拟桌面（共 {total} 个，0..{total - 1}）。")
        return True

    # ---------- 附加：create() / remove() 友好工具 ----------
    def create(self) -> int:
        """立即新建 1 个虚拟桌面（追加到最右端），返回新桌面编号（= count-1）。"""
        VirtualDesktop.create()
        new_idx = self.count() - 1
        print(f"[create] ✅ 新建虚拟桌面，最新编号 = {new_idx}，总数 = {self.count()}。")
        return new_idx

    def remove(self, index: int) -> bool:
        """
        删除指定编号的桌面；被删桌面里的所有窗口会被系统自动合并到左侧相邻桌面。
        index 越界 / 只剩 1 个桌面（不允许删除）时不动作，返回 False。
        """
        total = self.count()
        if total <= 1:
            print("[remove] ❌ 只剩 1 个虚拟桌面，Windows 不允许删除最后一个桌面。")
            return False
        if index < 0 or index >= total:
            print(f"[remove] ❌ 编号越界：index={index} 不在 [0, {total - 1}]。")
            return False
        desktops = get_virtual_desktops()
        target = desktops[index]
        # Windows API 要求提供 fallback desktop（窗口移过去），取左侧邻居；若删 0 号则 fallback 到原 1 号
        fallback_idx = index - 1 if index > 0 else 1
        fallback = desktops[fallback_idx]
        try:
            target.remove(fallback)
        except TypeError:
            # 老版 pyvda remove 不带参数：兼容兜底（直接 remove，让系统自己选 fallback）
            target.remove()  # type: ignore[call-arg]
        new_total = self.count()
        print(
            f"[remove] ✅ 已删除 {index} 号虚拟桌面，剩余 {new_total} 个，"
            f"窗口已合并到 {fallback_idx if fallback_idx < new_total else new_total - 1} 号。"
        )
        return True

    def info(self) -> str:
        """可读状态字符串（list 命令用）。"""
        total = self.count()
        cur = self.current_index()
        lines = [
            f"虚拟桌面总数：{total}",
            f"当前所在桌面编号：{cur}（严格 0-based，左→右顺序）",
            f"可用编号范围：[0, {total - 1}]",
        ]
        if total < 2:
            lines.append("当前只有 1 个桌面（手机布局）；点 Dock「电脑」按钮会按需新建 UU 桌面，"
                         "或手动执行：python virtual_desktop.py create")
        return "\n".join(lines)

    def launch_on_desktop(self, index: int, command: list[str], *,
                          cwd: Optional[str] = None,
                          wait_window_ms: int = 16000,
                          label: str = "",
                          no_window: bool = False,
                          mode: str = "auto",
                          min_hwnd_count: int = 1,
                          ) -> tuple[Optional[subprocess.Popen],
                                     Optional[_threading.Thread],
                                     str]:
        r"""
        在指定编号的虚拟桌面上启动进程，**四种模式**（本轮修复后默认强隔离模式，100% 不会再出现
        「窗口仍在桌面 0 / 两个桌面散落」的老问题）：

          mode="auto"                  → 默认值。直接选 switch_wait_switchback（强隔离最可靠）。
          mode="switch_wait_switchback" → 【✅ 推荐 / 本轮修复方案】
                                         Phase 1 : switch(#N) 切到目标桌面
                                         Phase 2 : Popen 立即启动
                                         Phase 3 : **轮询等 HWND 真正出现 >= min_hwnd_count 个**
                                                   （不是死 sleep！scrcpy 慢慢起来就一直等）
                                         Phase 4 : **仍留在桌面 #N，不切回**（调用方切下一个或切回 0）
                                         → 100% 隔离：进程初始桌面就是 #N，HWND 默认落到 #N。
          mode="switch_then_sleep"     → 老模式（和上一条同样先切桌面，但等 N 毫秒而非条件等待，
                                         保留作兼容兜底）。
          mode="move_after"            → 事后迁移（上次失败的模式），保留作：当用户要"完全不切桌面"
                                         时的降级，不推荐默认用（HWND 会暂时出现在桌面 0 再被迁走）。

        Parameters
        ----------
        wait_window_ms : 每种模式里"最长等待/最长扫尾"的时间窗口（默认 16000ms，足够 scrcpy 起 5 张卡片）。
        min_hwnd_count : switch_wait_switchback 专用：条件等待的 HWND 阈值（默认 1）。
        """
        import os as _os_local

        if index < 0 or index >= self.count():
            print(f"[launch] ❌ 桌面编号 index={index} 越界。")
            return None, None, mode

        # --- auto 模式选择：本轮改为默认强隔离 switch_wait_switchback ---
        if mode == "auto":
            mode = "switch_wait_switchback"
        valid_modes = ("switch_wait_switchback", "switch_then_sleep", "move_after")
        if mode not in valid_modes:
            raise ValueError(f"未知 mode={mode}，可选 auto/{'/'.join(valid_modes)}")

        label_suffix = f" [{label}]" if label else ""
        creationflags = 0
        if no_window and _os_local.name == "nt":
            creationflags |= 0x08000000  # CREATE_NO_WINDOW

        # =================================================================
        # 模式 A：switch_wait_switchback（强隔离，本轮默认 & 推荐）
        # =================================================================
        if mode == "switch_wait_switchback":
            print(
                f"[launch{label_suffix}] 🛡️  强隔离模式（switch_wait_switchback）："
                f"切到 desktop#{index} → Popen → 等 >= {min_hwnd_count} 个顶层 HWND 出现"
                f"后再离开（**不是死 sleep**）。"
            )
            # Phase 1: 切桌面
            if not self.switch(index):
                return None, None, mode
            # Phase 2: Popen 立即启动（当前活跃桌面就是 #N → HWND 初始归属 = #N）
            try:
                proc = subprocess.Popen(command, cwd=cwd, creationflags=creationflags)
            except FileNotFoundError as e:
                print(f"[launch{label_suffix}] ❌ 找不到可执行文件：{e}")
                return None, None, mode
            except Exception as e:
                print(f"[launch{label_suffix}] ❌ 启动失败：{e}")
                return None, None, mode
            print(f"[launch{label_suffix}] ✅ PID={proc.pid} 已在 desktop#{index} 创建。")

            # Phase 3: 条件等待 HWND 出现（不是死 sleep）
            wait_ms = max(3000, int(wait_window_ms))
            _wait_until_hwnds_ready(
                proc.pid, index,
                total_ms=wait_ms,
                min_hwnd_count=min_hwnd_count,
                label=f"{label}: desktop#{index}",
            )

            # Phase 4: 仍留在 desktop#N（调用方 do_bootstrap 决定下一步），
            #         但同时立刻开一个 10s 的扫尾 poller，抓那些"16s 超时后才冒出来"的残余卡片
            sweeper = _start_hwnd_assignment_poller(
                proc.pid, index,
                total_ms=max(8000, wait_ms // 2),
                label=f"sweeper {label or ''}: D{index}",
            )
            return proc, sweeper, mode

        # =================================================================
        # 模式 B：move_after（事后迁移，不切桌面但隔离不稳定 —— 保留作用户显式请求）
        # =================================================================
        if mode == "move_after":
            print(
                f"[launch{label_suffix}] 🪞  事后迁移模式（move_after）："
                f"当前桌面 Popen，再把 HWND 迁到 desktop#{index}（隔离不稳定，窗口会暂时出现在"
                f"桌面 0，仅在显式请求时使用）。"
            )
            try:
                proc = subprocess.Popen(command, cwd=cwd, creationflags=creationflags)
            except FileNotFoundError as e:
                print(f"[launch{label_suffix}] ❌ 找不到可执行文件：{e}")
                return None, None, mode
            except Exception as e:
                print(f"[launch{label_suffix}] ❌ 启动失败：{e}")
                return None, None, mode
            print(
                f"[launch{label_suffix}] ✅ PID={proc.pid} 已启动（随后 HWND 将迁到 desktop#{index}）。"
            )
            poller = _start_hwnd_assignment_poller(
                proc.pid, index,
                total_ms=max(3000, int(wait_window_ms)),
                label=label or "",
            )
            return proc, poller, mode

        # =================================================================
        # 模式 C：switch_then_sleep（旧兼容模式：死 sleep）
        # =================================================================
        assert mode == "switch_then_sleep"
        print(
            f"[launch{label_suffix}] 💤  旧兼容模式（switch_then_sleep）：切 desktop#{index} →"
            f" Popen → sleep {wait_window_ms}ms。"
        )
        if not self.switch(index):
            return None, None, mode
        try:
            proc = subprocess.Popen(command, cwd=cwd, creationflags=creationflags)
        except FileNotFoundError as e:
            print(f"[launch{label_suffix}] ❌ 找不到可执行文件：{e}")
            return None, None, mode
        except Exception as e:
            print(f"[launch{label_suffix}] ❌ 启动失败：{e}")
            return None, None, mode
        wait_s = max(0.0, wait_window_ms / 1000.0)
        if wait_s > 0:
            print(
                f"[launch{label_suffix}] ✅ PID={proc.pid} 已在 desktop#{index} 启动，"
                f"停留 {wait_s:.1f}s ..."
            )
            _time.sleep(wait_s)
        # 也开一个扫尾 poller（否则死 sleep 可能漏慢 scrcpy）
        sweeper = _start_hwnd_assignment_poller(
            proc.pid, index, total_ms=8000,
            label=f"sweeper {label or ''}: D{index}",
        )
        print(f"[launch{label_suffix}] 🟢 sleep 结束；启动 8s 扫尾 poller 继续抓慢窗口。")
        return proc, sweeper, mode


# ---------------------------------------------------------------------------
# 编排（2026-09-26 懒创建模型）：
#   bootstrap → 在【当前桌面】等 ADB → 启 server.py（手机布局），不建虚拟桌面、不启 UU；
#   uu-open   → Dock「电脑」按钮调用：有 UU 桌面只切换，没有则新建桌面并启 UU+回程Dock。
# ---------------------------------------------------------------------------
import os as _os
import pathlib as _pathlib
import shutil as _shutil

_SCRIPT_DIR = _pathlib.Path(__file__).resolve().parent
_DEFAULT_PROJECT_ROOT = _SCRIPT_DIR              # 默认把 virtual_desktop.py 同目录当项目根
_DEFAULT_SERVER_SCRIPT = "server.py"              # 默认要启动的 server 脚本

# UU远程 / 向日葵 / ToDesk 的常见默认安装位（按命中概率从高到低排）
#   第 1 条来自用户显式指定：UU远程 GameViewer 默认安装位
_UU_REMOTE_CANDIDATES: list[str] = [
    # —— 用户指定默认：UU远程 / GameViewer
    r"D:\UUdesk\GameViewer\GameViewer.exe",
    # —— 正主：UU远程桌面 UURemote
    r"C:\Program Files\UU Remote\UURemote.exe",
    r"C:\Program Files (x86)\UU Remote\UURemote.exe",
    r"C:\Program Files\uu-remote\uu-remote.exe",
    r"C:\Users\DELL\AppData\Local\UURemote\UURemote.exe",
    r"C:\Users\DELL\AppData\Local\Programs\uu-remote\uu-remote.exe",
    # —— 兜底：如果用户口误"UU远程"实为 向日葵 / ToDesk
    r"C:\Program Files (x86)\Oray\Sunlogin\SunloginClient\SunloginClient.exe",
    r"C:\Program Files\Oray\Sunlogin\SunloginClient\SunloginClient.exe",
    r"C:\Users\DELL\AppData\Local\Oray\SunloginClient\SunloginClient.exe",
    r"C:\Program Files\ToDesk\ToDesk.exe",
    r"C:\Users\DELL\AppData\Local\Programs\ToDesk\ToDesk.exe",
]


# ---------------------------------------------------------------------------
# Win32 辅助（纯 ctypes，零 pip 依赖）：
#   枚举进程树（子孙 PID）、枚举所有属于这些 PID 的顶层 HWND、
#   把 HWND 通过 pyvda 移动到目标虚拟桌面。
# 目的：把 HWND 一个个"事后迁移"到目标虚拟桌面（move_after 降级模式用）。
#       强隔离模式下直接在目标桌面 Popen；注意本项目手机布局就放在主桌面 #0，
#       UU 远程放 #1，只有切去 #1 启 UU 时窗口才可能在切换间隙散到两个桌面。
# ---------------------------------------------------------------------------
import ctypes as _ctypes
from ctypes import wintypes as _wintypes
import threading as _threading
import time as _time

_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

_kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
_user32   = _ctypes.WinDLL("user32",   use_last_error=True)


class _PROCESSENTRY32W(_ctypes.Structure):
    _fields_ = [
        ("dwSize",              _wintypes.DWORD),
        ("cntUsage",            _wintypes.DWORD),
        ("th32ProcessID",       _wintypes.DWORD),
        ("th32DefaultHeapID",   _ctypes.c_size_t),
        ("th32ModuleID",        _wintypes.DWORD),
        ("cntThreads",          _wintypes.DWORD),
        ("th32ParentProcessID", _wintypes.DWORD),
        ("pcPriClassBase",      _ctypes.c_long),
        ("dwFlags",             _wintypes.DWORD),
        ("szExeFile",           _wintypes.WCHAR * _MAX_PATH),
    ]


def _collect_descendant_pids(root_pid: int, max_depth: int = 6) -> set[int]:
    """
    通过 CreateToolhelp32Snapshot 进程快照 + BFS 拿到 root_pid 的所有
    子/孙/曾孙... PID（含 root 自身）。
    用于：server.py (python) → 起了很多 scrcpy.exe 子进程 → 这些子进程的
          HWND 也必须一起归拢 move 到同一个虚拟桌面。
    """
    if root_pid <= 0:
        return set()
    # 1) 拍快照：(ppid -> set(child_pids))
    children: dict[int, set[int]] = {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _wintypes.HANDLE(-1).value:  # INVALID_HANDLE_VALUE
        return {root_pid}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, _ctypes.byref(entry)):
            return {root_pid}
        while True:
            ppid = int(entry.th32ParentProcessID)
            cpid = int(entry.th32ProcessID)
            children.setdefault(ppid, set()).add(cpid)
            entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
            if not _kernel32.Process32NextW(snapshot, _ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)

    # 2) BFS 从 root_pid 展开
    result: set[int] = {root_pid}
    frontier: set[int] = {root_pid}
    for _ in range(max_depth):
        nxt: set[int] = set()
        for pid in frontier:
            for cpid in children.get(pid, ()):
                if cpid not in result:
                    result.add(cpid)
                    nxt.add(cpid)
        if not nxt:
            break
        frontier = nxt
    return result


# --- HWND 枚举 -----------------------------------------------------------
_EnumWindowsProc = _ctypes.WINFUNCTYPE(
    _wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM
)
_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
_GetWindowThreadProcessId.argtypes = [_wintypes.HWND, _ctypes.POINTER(_wintypes.DWORD)]
_GetWindowThreadProcessId.restype  = _wintypes.DWORD

_IsWindowVisible = _user32.IsWindowVisible
_IsWindowVisible.argtypes = [_wintypes.HWND]
_IsWindowVisible.restype  = _wintypes.BOOL

_GetAncestor = _user32.GetAncestor
_GetAncestor.argtypes = [_wintypes.HWND, _wintypes.UINT]
_GetAncestor.restype  = _wintypes.HWND
_GA_ROOT       = 0x0002
_GA_ROOTOWNER  = 0x0003
_GA_PARENT     = 0x0001

_GetWindow = _user32.GetWindow
_GetWindow.argtypes = [_wintypes.HWND, _wintypes.UINT]
_GetWindow.restype  = _wintypes.HWND
_GW_OWNER = 0x0004

# 读 HWND 标题（debug 用，日志一眼知道是哪个窗口）
_GetWindowTextW = _user32.GetWindowTextW
_GetWindowTextW.argtypes = [_wintypes.HWND, _wintypes.LPWSTR, _ctypes.c_int]
_GetWindowTextW.restype  = _ctypes.c_int


def _get_window_title(hwnd: int, limit: int = 80) -> str:
    buf = _ctypes.create_unicode_buffer(limit)
    n = _GetWindowTextW(hwnd, buf, limit)
    return buf.value[:n] if n > 0 else "(无标题)"


def _enum_top_level_hwnds_for_pids(pid_set: set[int]) -> list[int]:
    """
    返回所有「属于 pid_set 里的进程」且「可见 + 真正顶层」的 HWND 列表。
    【相对旧版本的关键修复】：
      * 顶层过滤使用 GA_ROOT == hwnd（不是 GA_PARENT in (0, hwnd)）。
        实测 Windows 下 Qt/scrcpy/普通 Frame 窗口的 GA_PARENT 是全局
        Desktop HWND（一个 0x100xx 常量，≠0 也≠窗口自己），所以用
        GA_PARENT 过滤会把所有真实应用窗口都误排除（14 个目标窗口
        全部 GA_PARENT_ok=False，结果 audit 报告"0 个窗口"假阳性）。
        正确判断顶层：GetAncestor(hwnd, GA_ROOT) == hwnd 自身，
        即"再往上找祖先就没有了"——实测 14/14 个目标窗口都满足。
      * 同时用「IsWindowVisible + 桌面关联进程 PID」双条件。
    """
    found: list[int] = []

    @_EnumWindowsProc
    def _cb(hwnd, _lparam):
        if not _IsWindowVisible(hwnd):
            return True
        # 真正顶层："顶层根祖先就是自己"
        root = _GetAncestor(hwnd, _GA_ROOT)
        if int(root or 0) != int(hwnd):
            return True
        pid_ref = _wintypes.DWORD()
        _GetWindowThreadProcessId(hwnd, _ctypes.byref(pid_ref))
        if pid_ref.value in pid_set:
            found.append(int(hwnd))
        return True

    _user32.EnumWindows(_cb, 0)
    return found


def _get_hwnd_desktop_index(hwnd: int) -> Optional[int]:
    """
    通过 pyvda 官方支持的接口读「HWND 实际在哪个虚拟桌面」。
    pyvda 0.6.0 实测支持 2 种：
      A) 快速：pyvda.AppView(hwnd).desktop_id 和 pyvda.get_virtual_desktops()
         每个桌面对象的 .id 直接字符串比对（忽略 {} 大小写），命中就返回；
      B) 兜底（A 命中不到时才跑，代价略高）：对每个桌面调用 apps_by_z_order()
         拿 HWND 列表，反建 hwnd→desktop_idx 映射。两种都试过失败才返回 None。
    这是**唯一可信**的归属判断依据。
    """
    if pyvda is None:
        return None
    try:
        desktops = get_virtual_desktops()
    except Exception:
        return None
    if not desktops:
        return None

    # ---- 链路 A：AppView.desktop_id 快速匹配 ----
    try:
        AppViewCls = getattr(pyvda, "AppView", None)
        if AppViewCls is not None:
            av = AppViewCls(int(hwnd))
            av_desktop_id = getattr(av, "desktop_id", None)
            if av_desktop_id is not None:
                needle = str(av_desktop_id).strip("{}").lower()
                for idx, d in enumerate(desktops):
                    if str(d.id).strip("{}").lower() == needle:
                        return idx
    except Exception:
        pass

    # ---- 链路 B：apps_by_z_order() 反查（最彻底） ----
    try:
        hwnd_int = int(hwnd)
        for idx, d in enumerate(desktops):
            for app in d.apps_by_z_order():
                if int(getattr(app, "hwnd", 0) or 0) == hwnd_int:
                    return idx
    except Exception:
        pass
    return None


def _raw_vdm_move_window(hwnd: int, target_idx: int) -> bool:
    """
    pyvda.AppView(hwnd).move(target_desktop) 迁移。
    迁移后调用 _get_hwnd_desktop_index 二次验证才叫成功。
    """
    if pyvda is None:
        return False
    try:
        desktops = get_virtual_desktops()
    except Exception:
        return False
    if target_idx < 0 or target_idx >= len(desktops):
        return False
    try:
        AppViewCls = getattr(pyvda, "AppView", None)
        if AppViewCls is None:
            return False
        av = AppViewCls(int(hwnd))
        if not (hasattr(av, "move") and callable(av.move)):
            return False
        av.move(desktops[target_idx])
    except Exception:
        return False
    # 验证：真正移过去了吗？
    after_idx = _get_hwnd_desktop_index(int(hwnd))
    return after_idx == target_idx


# --- HWND → 虚拟桌面 move（校验真实归属） ---------------------------------
def _pyvda_move_hwnd(hwnd: int, desktop_index: int) -> tuple[bool, str]:
    """
    把一个 HWND 迁移到 desktop_index 号虚拟桌面，并**真实校验迁移后所在**。
    返回 (success, reason_or_method)。
    主链路：pyvda.AppView(hwnd).move(target_desktop)（pyvda 0.6.0 实测 100% 生效，
    由内部 IVirtualDesktopManager.MoveWindowToDesktop 完成），
    再调用 _get_hwnd_desktop_index（apps_by_z_order + desktop_id 双校验）确认真实归属。
    """
    if pyvda is None:
        return False, "pyvda 未导入"
    try:
        desktops = get_virtual_desktops()
        if desktop_index < 0 or desktop_index >= len(desktops):
            return False, f"desktop_index={desktop_index} 越界 [0,{len(desktops)-1}]"
    except Exception as e:
        return False, f"取虚拟桌面列表失败: {e!r}"

    # 快速通道：本来就已经在目标桌面了 → 直接成功
    real_idx = _get_hwnd_desktop_index(hwnd)
    if real_idx is not None and real_idx == desktop_index:
        return True, "already-on-target"

    last_msg = f"real_idx=#{real_idx} 期望=#{desktop_index}"

    # 链 1（主）：pyvda.AppView(hwnd).move(target_desktop)
    try:
        ok = _raw_vdm_move_window(int(hwnd), int(desktop_index))
        after_idx = _get_hwnd_desktop_index(hwnd)
        if after_idx == desktop_index:
            return True, f"pyvda.AppView.move(called_ok={ok})"
        last_msg = f"AppView.move called_ok={ok} 但 HWND 仍在 #{after_idx}"
    except Exception as e1:
        last_msg = f"AppView.move raised {e1!r}"

    # 链 2（兜底，老版本 pyvda）：VirtualDesktop.move(hwnd)
    try:
        target_desktop = desktops[desktop_index]
        if hasattr(target_desktop, "move") and callable(getattr(target_desktop, "move")):
            target_desktop.move(hwnd)
            after_idx = _get_hwnd_desktop_index(hwnd)
            if after_idx == desktop_index:
                return True, "VirtualDesktop.move"
            last_msg += f"; VirtualDesktop.move called but HWND still on #{after_idx}"
    except Exception as e2:
        last_msg += f"; VirtualDesktop.move raised {e2!r}"

    return False, (
        f"无法将 HWND={hex(hwnd)} 移到 #{desktop_index}（现在在 #{real_idx}）：{last_msg}"
    )


# --- 两种辅助：扫尾 poller / 条件等待 HWND 真出现 -------------------------
def _start_hwnd_assignment_poller(
    root_pid: int,
    desktop_index: int,
    *,
    total_ms: int = 10000,
    tick_ms: int = 350,
    label: str = "",
) -> _threading.Thread:
    """
    后台轮询线程（daemon）：在 total_ms 窗口内持续把 root_pid 子孙进程的
    顶层 HWND 一个个 move 到目标桌面。
    **主要作为切完桌面后的"扫尾"**——强隔离模式下 99% 的 HWND 已经在 Popen 时
    归属正确了，这里只抓那些"迟了 N 秒才冒出来"的残余卡片。
    """
    label_suffix = f" [{label}]" if label else ""
    seen: set[int] = set()
    failed_counter: dict[int, int] = {}

    def _worker():
        nonlocal seen, failed_counter
        end_at = _time.monotonic() + (total_ms / 1000.0)
        total_moved = 0
        tick = 0
        while _time.monotonic() < end_at:
            tick += 1
            pids = _collect_descendant_pids(root_pid)
            hwnds = _enum_top_level_hwnds_for_pids(pids)
            newly_done_this_tick = 0
            for hwnd in hwnds:
                if hwnd in seen:
                    continue
                title = _get_window_title(hwnd, 60)
                # 先查真实桌面：已对的话直接记为 seen
                real_idx = _get_hwnd_desktop_index(hwnd)
                if real_idx == desktop_index:
                    seen.add(hwnd)
                    total_moved += 1
                    newly_done_this_tick += 1
                    print(
                        f"[poller{label_suffix}] ✅ HWND={hex(hwnd)} '{title[:40]}' → "
                        f"已正确在 desktop#{desktop_index}（无需 move）"
                    )
                    continue
                ok, how = _pyvda_move_hwnd(hwnd, desktop_index)
                if ok:
                    total_moved += 1
                    newly_done_this_tick += 1
                    seen.add(hwnd)
                    print(
                        f"[poller{label_suffix}] ✅ HWND={hex(hwnd)} '{title[:40]}' → "
                        f"desktop#{desktop_index} ({how})"
                    )
                    failed_counter.pop(hwnd, None)
                else:
                    failed_counter[hwnd] = failed_counter.get(hwnd, 0) + 1
                    if failed_counter[hwnd] == 4:
                        print(
                            f"[poller{label_suffix}] ⚠️  HWND={hex(hwnd)} '{title[:40]}' "
                            f"4 次无法迁移到 desktop#{desktop_index}: {how}"
                        )
            if newly_done_this_tick:
                print(
                    f"[poller{label_suffix}] tick#{tick}: 本轮确认 {newly_done_this_tick} 个"
                    f" HWND 归位；累计 {total_moved} 个。"
                )
            _time.sleep(tick_ms / 1000.0)
        print(
            f"[poller{label_suffix}] 🏁 扫尾结束。时间窗口={total_ms}ms，"
            f"累计 {total_moved} 个 HWND 确认在 desktop#{desktop_index}。"
        )

    t = _threading.Thread(
        target=_worker,
        name=f"VD-Poller-D{desktop_index}-PID{root_pid}",
        daemon=True,
    )
    t.start()
    return t


def _wait_until_hwnds_ready(
    root_pid: int,
    target_desktop_index: int,
    *,
    total_ms: int = 16000,
    tick_ms: int = 250,
    min_hwnd_count: int = 1,
    label: str = "",
) -> tuple[int, int]:
    """
    **强隔离模式（switch_wait_switchback）的核心等待函数**：
    Phase 2 期间，Popen 已经在 target_desktop_index 桌面上启动成功（当前活跃桌面
    就是目标桌面），轮询「root_pid 子孙进程的顶层 HWND 至少出现 min_hwnd_count 个」
    作为"可以切回桌面 0"的判据。**绝对不是 sleep N ms**。

    返回: (pid_tree_hwnd_count, elapsed_ms)
        - hwnd_count >= min_hwnd_count → 满足条件
        - hwnd_count <  min_hwnd_count → 超时
    同时日志每 3s 打印一次当前进展，用户能看到"哪一个窗口已经出来"。
    """
    label_suffix = f" [{label}]" if label else ""
    start_t = _time.monotonic()
    deadline = start_t + (total_ms / 1000.0)
    last_progress = start_t
    best_count = 0
    seen_titles: set[str] = set()
    tick = 0

    while _time.monotonic() < deadline:
        tick += 1
        pids = _collect_descendant_pids(root_pid)
        hwnds = _enum_top_level_hwnds_for_pids(pids)
        best_count = max(best_count, len(hwnds))

        # 检测到新窗口就立即打印（给用户可见进展）
        for hwnd in hwnds:
            t = _get_window_title(hwnd, 80)
            key = f"{hex(hwnd)}|{t}"
            if key not in seen_titles:
                seen_titles.add(key)
                real = _get_hwnd_desktop_index(hwnd)
                print(
                    f"[wait-hwnd{label_suffix}] 🪟 新窗口: HWND={hex(hwnd)} "
                    f"on desktop#{real} '{t[:55]}'"
                )

        if len(hwnds) >= min_hwnd_count:
            elapsed_ms = int((_time.monotonic() - start_t) * 1000)
            print(
                f"[wait-hwnd{label_suffix}] ✅ 就绪：已出现 {len(hwnds)} 个顶层窗口（阈值"
                f" {min_hwnd_count}），耗时 {elapsed_ms}ms。"
            )
            return len(hwnds), elapsed_ms

        now = _time.monotonic()
        if (now - last_progress) >= 3.0:
            elapsed_ms = int((now - start_t) * 1000)
            print(
                f"[wait-hwnd{label_suffix}] ⏳ {elapsed_ms}ms：当前 {len(hwnds)}/"
                f"min={min_hwnd_count} 个顶层窗口（子孙 PID 共 {len(pids)} 个），继续等…"
            )
            last_progress = now

        _time.sleep(tick_ms / 1000.0)

    elapsed_ms = int((_time.monotonic() - start_t) * 1000)
    print(
        f"[wait-hwnd{label_suffix}] ⚠️  超时 {total_ms}ms：只出现 {best_count}/"
        f"min={min_hwnd_count} 个顶层窗口，停止等待（允许切回，但后续仍由扫尾 poller 跟进）。"
    )
    return best_count, elapsed_ms


def _locate_python(explicit: Optional[str]) -> str:
    """选 python 解释器：用户显式传入 > 当前 sys.executable > PATH 里的 python。"""
    if explicit:
        p = _pathlib.Path(explicit)
        if p.is_file():
            return str(p)
        in_path = _shutil.which(explicit)
        if in_path:
            return in_path
        raise FileNotFoundError(f"找不到指定的 Python 解释器：{explicit}")
    return sys.executable


def _locate_server_py(project_root: Optional[str], script_name: str) -> _pathlib.Path:
    """定位 server.py 绝对路径。默认使用 virtual_desktop.py 同目录作项目根。"""
    root = _pathlib.Path(project_root).expanduser().resolve() if project_root else _DEFAULT_PROJECT_ROOT
    server_py = root / script_name
    if not server_py.is_file():
        raise FileNotFoundError(
            f"找不到 server 脚本：\n    候选路径 = {server_py}\n"
            f"    请通过 --project-root / --server-script 指定，或将脚本放入 {_DEFAULT_PROJECT_ROOT}"
        )
    return server_py


def _find_uu_remote(explicit: Optional[str]) -> str:
    """找到 UU远程 / 向日葵 / ToDesk 任意一个。显式传入优先级最高；否则遍历常见安装位。"""
    if explicit:
        p = _pathlib.Path(explicit).expanduser()
        if p.is_file():
            return str(p)
        raise FileNotFoundError(f"指定的远程软件 exe 不存在：{explicit}")

    for cand in _UU_REMOTE_CANDIDATES:
        if _pathlib.Path(cand).is_file():
            print(f"[uu-open] 🎯 自动探测到远程客户端：{cand}")
            return cand
    for name in ("UURemote.exe", "uu-remote.exe", "SunloginClient.exe", "ToDesk.exe"):
        found = _shutil.which(name)
        if found:
            print(f"[uu-open] 🎯 从 PATH 中探测到远程客户端：{found}")
            return found

    raise FileNotFoundError(
        "[uu-open] ❌ 无法自动定位 UU远程 / 向日葵 / ToDesk exe。\n"
        "    请显式传入：\n"
        "        python virtual_desktop.py uu-open --uu-remote-path \"C:/你的完整路径/UURemote.exe\""
    )


# ---------------------------------------------------------------------------
# ADB 设备检查：bootstrap 启动 server.py 前确认手机可达
#   · 优先识别 USB 有线设备（serial 非 host:port 形态）；
#   · 没有有线设备时，若给了 --phoneaddress（host:port）则执行 `adb connect`。
# ---------------------------------------------------------------------------
import re as _re

_ADB_CANDIDATE_PATHS: list[str] = [
    r"D:\scrcpy-win64-v4.1\adb.exe",
    r"C:\Program Files\scrcpy\adb.exe",
    r"C:\Program Files (x86)\scrcpy\adb.exe",
    _os.path.expanduser(r"~\AppData\Local\Programs\scrcpy\adb.exe"),
]

# 无线 ADB 设备的 serial 形如 192.168.1.100:5555；USB 设备 serial 不含该形态
_WIFI_SERIAL_RE = _re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


def _find_adb_exe() -> Optional[str]:
    """定位 adb.exe：PATH 优先，其次 scrcpy 同目录自带 adb 与 Android SDK platform-tools。"""
    found = _shutil.which("adb")
    if found:
        return found
    for cand in _ADB_CANDIDATE_PATHS:
        if cand and _os.path.isfile(cand):
            return cand
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = _os.environ.get(env_key)
        if root:
            cand = str(_pathlib.Path(root) / "platform-tools" / "adb.exe")
            if _os.path.isfile(cand):
                return cand
    return None


def _adb_run(adb_exe: str, args: list[str], timeout: float = 15.0) -> str:
    """执行 adb 子命令并返回 stdout+stderr 合并文本（Windows 下不弹黑窗）。"""
    creationflags = 0x08000000 if _os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run(
        [adb_exe, *args],
        capture_output=True, text=True, timeout=timeout,
        creationflags=creationflags,
    )
    return ((r.stdout or "") + (r.stderr or "")).strip()


def _adb_devices(adb_exe: str) -> list[tuple[str, str]]:
    """解析 `adb devices`，返回 [(serial, state), ...]（仅设备行）。"""
    text = _adb_run(adb_exe, ["devices"])
    devices: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def _ensure_phone_connected(phoneaddress: Optional[str], *,
                            verbose: bool = True) -> tuple[bool, str]:
    """启动 server.py 前确认手机可达：
       1) 存在状态为 device 的 USB/有线设备（serial 非 host:port）→ 直接通过；
       2) 无有线设备但给了 phoneaddress（必须含端口）→ `adb connect <phoneaddress>`，
          已在线则跳过，连接后以 `adb devices` 真实状态为准；
       3) 无有线设备也没给 phoneaddress → 已有任意无线设备在线则警告放行，否则失败。
    verbose=False 时只返回结论、不打印中间过程（供外层重试循环使用）。
    返回 (ok, message)。"""
    def _say(*parts: object) -> None:
        if verbose:
            print(*parts)

    adb_exe = _find_adb_exe()
    if not adb_exe:
        return False, "找不到 adb.exe（PATH / scrcpy 安装目录 / ANDROID_HOME 下均未找到）"

    try:
        devices = _adb_devices(adb_exe)
    except Exception as e:
        return False, f"执行 adb devices 失败：{e!r}"

    usb = [s for s, st in devices if st == "device" and not _WIFI_SERIAL_RE.match(s)]
    wifi = [s for s, st in devices if st == "device" and _WIFI_SERIAL_RE.match(s)]

    if usb:
        return True, f"检测到 ADB 有线(USB)设备：{', '.join(usb)}"

    _say("[bootstrap] ⚠️ 未检测到 ADB 有线(USB)设备。")
    if devices:
        _say(f"[bootstrap]    当前 adb devices：{devices}")

    if phoneaddress:
        addr = phoneaddress.strip()
        if ":" not in addr:
            return False, f"phoneaddress 必须包含端口（host:port），收到：{addr}"
        if addr in wifi:
            return True, f"手机已通过无线 ADB 在线：{addr}（无需重复 connect）"
        _say(f"[bootstrap] 尝试无线连接：adb connect {addr} ...")
        try:
            msg = _adb_run(adb_exe, ["connect", addr], timeout=8.0)
        except Exception as e:
            return False, f"adb connect {addr} 执行失败：{e!r}"
        if msg:
            _say(f"[bootstrap] adb connect 输出：{msg}")
        # connect 后设备可能短暂 unauthorized/offline（adb server 刚建立时的鉴权竞态），
        # 轮询最多 ~6s 等状态稳定为 device，再下结论。
        final_state = None
        after: list[tuple[str, str]] = []
        deadline = _time.monotonic() + 6.0
        while _time.monotonic() < deadline:
            try:
                after = _adb_devices(adb_exe)
            except Exception as e:
                return False, f"adb connect 后复查设备列表失败：{e!r}"
            final_state = dict(after).get(addr)
            if final_state == "device":
                return True, f"无线 ADB 连接成功：{addr}"
            _time.sleep(0.8)
        return False, (
            f"adb connect {addr} 后设备未就绪（最终状态={final_state or '未出现在设备列表'}；"
            f"当前设备={after}）。请确认手机与电脑在同一局域网、已开启无线调试、IP:端口正确，"
            f"并留意手机上的 USB/无线调试授权弹窗。"
        )

    if wifi:
        return True, f"未提供 --phoneaddress，但已有无线设备在线，继续启动：{', '.join(wifi)}"
    return False, (
        "没有 ADB 有线(USB)设备，且未提供 --phoneaddress，无法连接手机。"
        "请插入 USB 线并在手机上授权调试，或用 --phoneaddress 192.168.x.x:5555 指定手机地址。"
    )


# ---------------------------------------------------------------------------
# 状态文件 / 进程工具 / 常驻归位 watcher / stop
#   bootstrap 把 server.py / UU / watcher 的 PID 落盘到 .vd_state.json，
#   `python virtual_desktop.py stop` 凭此优雅终止整条链路（CTRL_C →
#   server.py 的 atexit stop_all 清理全部 scrcpy），无状态文件也不工作。
# ---------------------------------------------------------------------------
import json as _json
from datetime import datetime as _datetime

_STATE_FILE = _SCRIPT_DIR / ".vd_state.json"
# 主桌面 toolbar（front/qt_backdrop.py 的 multiprocessing spawn 子进程）自报 PID 文件
_BACKDROP_PID_FILE = _SCRIPT_DIR / ".vd_backdrop.pid"
_WATCHER_LOG = _SCRIPT_DIR / ".vd_watcher.log"
# uu-open 懒创建的串行锁（防止连点电脑按钮导致 UU/回程 Dock 被启动两份）
_UU_LOCK_FILE = _SCRIPT_DIR / ".vd_uu.lock"
_UU_LOG = _SCRIPT_DIR / ".vd_uu.log"
# UU 桌面上的回程 Dock：front/uu_dock.py，只由 uu-open 在 UU 桌面启动
_UU_DOCK_SCRIPT = _SCRIPT_DIR / "front" / "uu_dock.py"
_UU_DOCK_LOG = _SCRIPT_DIR / ".vd_uudock.log"

# 需要归位到"远程桌面"的客户端主进程名（小写）。必须按**进程名**全局识别，
# 因为"早已运行"的 UU 不是 bootstrap 的子孙，按进程树枚举永远找不到它。
_REMOTE_PROCESS_NAMES = {
    "gameviewer.exe", "uuremote.exe", "uu-remote.exe",
    "sunloginclient.exe", "todesk.exe",
}
# --kill-uu 时只关主程序，保留后台服务（避免误杀 UU 的更新/守护服务）
_REMOTE_SERVICE_NAMES = {
    "gameviewerservice.exe", "gameviewerserver.exe", "gameviewerhealthd.exe",
}

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_kernel32.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
_kernel32.OpenProcess.restype = _wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [_wintypes.HANDLE, _ctypes.POINTER(_wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = _wintypes.BOOL
_kernel32.AttachConsole.argtypes = [_wintypes.DWORD]
_kernel32.AttachConsole.restype = _wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = _wintypes.BOOL
_kernel32.GenerateConsoleCtrlEvent.argtypes = [_wintypes.DWORD, _wintypes.DWORD]
_kernel32.GenerateConsoleCtrlEvent.restype = _wintypes.BOOL
# PHANDLER_ROUTINE 此处只传 NULL（=切换"本进程忽略 CTRL 事件"属性），无需原型


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or int(pid) <= 0:
        return False
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = _wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(h, _ctypes.byref(code)):
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(h)


def _find_pids_by_names(name_set: set[str]) -> dict[str, list[int]]:
    """进程名（小写）→ PID 列表。用于识别"非本脚本启动、早已运行"的 UU。"""
    wanted = {n.lower() for n in name_set}
    result: dict[str, list[int]] = {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _wintypes.HANDLE(-1).value:
        return result
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, _ctypes.byref(entry)):
            return result
        while True:
            nm = entry.szExeFile.lower()
            if nm in wanted:
                result.setdefault(nm, []).append(int(entry.th32ProcessID))
            entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
            if not _kernel32.Process32NextW(snapshot, _ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return result


def _remote_pids() -> set[int]:
    """所有远程客户端进程及其子孙 PID（托盘态服务无窗口，枚举到也无害）。"""
    pids: set[int] = set()
    for pid_list in _find_pids_by_names(_REMOTE_PROCESS_NAMES).values():
        for pid in pid_list:
            pids |= _collect_descendant_pids(pid)
    return pids


# ---------------------------------------------------------------------------
# stop 兜底清理：state 里单个 PID 可能失配（dock 被 uu-open 补启、state 已清、
# 用户手动操作过），graceful 退出也可能留下 daemon 子孙/孤儿。下列 helper
# 不依赖 state，按命令行特征做最终核验清扫。
# ---------------------------------------------------------------------------
def _read_pid_file(path: "_pathlib.Path") -> Optional[int]:
    """读纯文本 PID 文件；不存在/内容非法返回 None（不抛异常）。"""
    try:
        txt = path.read_text(encoding="utf-8").strip()
        return int(txt) if txt else None
    except Exception:
        return None


def _list_cmdlines_for_names(exe_names: set[str]) -> dict[int, str]:
    """{PID: 命令行}，只查指定进程名（小写）集合。

    Toolhelp 快照拿不到命令行，这里走一次 PowerShell CIM（无新依赖；
    stop 不是热路径，约 0.5s 可接受）。失败返回空 dict（调用方仍有
    state PID / PID 文件 / 进程名主路径）。"""
    names_lc = {n.lower() for n in exe_names}
    name_filter = " OR ".join(f"Name='{n}'" for n in sorted(names_lc))
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"Get-CimInstance Win32_Process -Filter \"{name_filter}\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        creationflags = 0x08000000  # CREATE_NO_WINDOW，避免 stop 时黑框一闪
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=20,
            creationflags=creationflags,
        )
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    except Exception:
        return {}
    result: dict[int, str] = {}
    self_pid = _os.getpid()
    for line in out.splitlines():
        line = line.rstrip("\r")
        if "\t" not in line:
            continue
        pid_txt, _, cmdline = line.partition("\t")
        try:
            pid = int(pid_txt.strip())
        except ValueError:
            continue
        if pid == self_pid:
            continue  # 绝不误杀 stop 自己
        result[pid] = cmdline or ""
    return result


_PYTHON_EXE_NAMES = {"python.exe", "pythonw.exe", "py.exe"}


def _find_project_pids_by_cmdline(patterns: dict[str, "_re.Pattern"]) -> dict[str, list[int]]:
    """按命令行正则枚举本项目残留 python 进程：{label: [PID, ...]}。"""
    cmds = _list_cmdlines_for_names(_PYTHON_EXE_NAMES)
    hits: dict[str, list[int]] = {label: [] for label in patterns}
    for pid, cmdline in cmds.items():
        for label, pat in patterns.items():
            if pat.search(cmdline):
                hits[label].append(pid)
                break
    return hits


# 本项目 detached / 独立 python 进程的命令行特征（一个命令行只会命中一个 label）
_STOP_CMDLINE_PATTERNS = {
    "watcher": _re.compile(r"virtual_desktop\.py[\"']?\s+watch\b"),
    "uu-dock": _re.compile(r"front[/\\]uu_dock\.py|[\"']?uu_dock\.py"),
    "server": _re.compile(r"[\\/\s]server\.py(?:\s|$|[\"'])"),
}


def _sweep_scrcpy(*, label: str = "residual-scrcpy") -> int:
    """无条件清扫全部 scrcpy.exe（server 死后任何残留 scrcpy 都是孤儿）。"""
    n = 0
    for pids in _find_pids_by_names({"scrcpy.exe"}).values():
        for pid in pids:
            if _taskkill_tree(pid, label=label):
                n += 1
    return n


# ===========================================================================
# [stop-diag] 临时诊断块（只加日志，不改变任何停止语义；验证权限/孤儿进程
#   假设后可整体删除）。排查两个问题：
#   A) stop 终端未提权(Medium IL)，server/scrcpy 以 High IL 运行 →
#      taskkill 报 Access is denied，但旧代码 capture_output 把错误吞了；
#   B) CTRL_C 或强杀只干掉父进程，python/scrcpy/adb 子孙变孤儿继续跑。
# ===========================================================================
def _is_elevated() -> Optional[bool]:
    """当前 stop 进程是否持有提升令牌（管理员 High IL）。检测失败返回 None。"""
    try:
        return bool(_ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _diag_snapshot_table() -> dict[int, tuple[int, str]]:
    """一次 Toolhelp 进程快照 → {pid: (ppid, exe名)}。"""
    table: dict[int, tuple[int, str]] = {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _wintypes.HANDLE(-1).value:
        return table
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, _ctypes.byref(entry)):
            return table
        while True:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID), str(entry.szExeFile))
            entry.dwSize = _ctypes.sizeof(_PROCESSENTRY32W)
            if not _kernel32.Process32NextW(snapshot, _ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return table


def _diag_dump_tree(root_pid: Optional[int]) -> None:
    """打印 root_pid 及其子孙树（PID/PPID/exe）；父进程已死的行标 [ORPHAN]。"""
    if not root_pid:
        print("[stop-diag]   （无 PID，跳过进程树）", flush=True)
        return
    root_pid = int(root_pid)
    table = _diag_snapshot_table()
    if root_pid not in table:
        print(f"[stop-diag]   PID={root_pid} 已不在进程快照中（已退出）。", flush=True)
        return
    children: dict[int, list[int]] = {}
    for pid, (ppid, _exe) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()

    def _walk(pid: int, depth: int) -> None:
        if pid in seen or pid not in table:
            return
        seen.add(pid)
        ppid, exe = table[pid]
        orphan = "" if (depth == 0 or ppid in table or ppid == 0) \
            else "   [ORPHAN 父进程已死]"
        print(f"[stop-diag]   {'  ' * depth}PID={pid:<7} PPID={ppid:<7} "
              f"{exe}{orphan}", flush=True)
        for ch in sorted(children.get(pid, ())):
            _walk(ch, depth + 1)

    _walk(root_pid, 0)


# 只聚焦本链路可能出现的进程名，避免全量进程表刷屏
_DIAG_PROC_NAMES = {
    "python.exe", "py.exe", "pythonw.exe",
    "scrcpy.exe", "scrcpy_32.exe", "adb.exe", "ffmpeg.exe",
}


def _diag_dump_related() -> None:
    """打印当前所有 python/scrcpy/adb 相关进程及父子存活状态（聚焦子集）。"""
    table = _diag_snapshot_table()
    rows = [(pid, ppid, exe) for pid, (ppid, exe) in table.items()
            if exe.lower() in _DIAG_PROC_NAMES]
    if not rows:
        print("[stop-diag]   当前无 python/scrcpy/adb 相关进程。", flush=True)
        return
    for pid, ppid, exe in sorted(rows, key=lambda r: (r[2].lower(), r[0])):
        parent_state = "父活" if ppid in table or ppid == 0 else "父死"
        orphan = "  [ORPHAN 孤儿进程]" if parent_state == "父死" else ""
        print(f"[stop-diag]   {exe:<16} PID={pid:<7} PPID={ppid:<7}"
              f"({parent_state}){orphan}", flush=True)


def _save_state(state: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(state, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, _STATE_FILE)


def _load_state() -> Optional[dict]:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _clear_state() -> None:
    try:
        _STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _update_state(**changes) -> Optional[dict]:
    """读取-合并-原子写回 .vd_state.json；返回合并后的最新 state。
    供 uu-open 在 watcher 运行期间并发更新 UU 桌面字段（写是原子替换）。"""
    st = _load_state() or {}
    st.update(changes)
    _save_state(st)
    return st


# ---------------------------------------------------------------------------
# uu-open 懒创建辅助
# ---------------------------------------------------------------------------
def _acquire_uu_lock() -> Optional[object]:
    """串行化 uu-open（用户连点电脑按钮时只允许一个实例创建/启动）。
    返回文件句柄（结束时需 _release_uu_lock 释放）；已有实例在跑则返回 None。
    锁文件中记录持有者 PID；持有者已死（异常退出留下的陈旧锁）自动接管。"""
    try:
        if _UU_LOCK_FILE.exists():
            try:
                holder = int(_UU_LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                holder = 0
            if holder and holder != _os.getpid() and _pid_alive(holder):
                return None
            # 陈旧锁（持有者已死/无法解析）→ 删掉接管
            try:
                _UU_LOCK_FILE.unlink()
            except OSError:
                pass
        fp = open(_UU_LOCK_FILE, "x", encoding="utf-8")
        fp.write(str(_os.getpid()))
        fp.flush()
        return fp
    except FileExistsError:
        return None
    except OSError:
        # 锁文件本身不可写时不阻塞主流程（并发保护是增强而非硬依赖）
        return object()  # 哨兵：未持锁但允许继续


def _release_uu_lock(fp) -> None:
    try:
        if fp is not None and hasattr(fp, "close"):
            fp.close()
    except OSError:
        pass
    try:
        # 只删除自己持有的锁
        if _UU_LOCK_FILE.exists():
            holder = int(_UU_LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            if holder == _os.getpid():
                _UU_LOCK_FILE.unlink()
    except (ValueError, OSError):
        pass


def _switch_desktop_verified(vd: "VirtualDesktops", idx: int, *,
                             tag: str = "uu-open", attempts: int = 4) -> bool:
    """切到 idx 并校验用户真的被切过去了。

    pyvda go() 在 detached/刚建桌的瞬间偶发不生效或有延迟，单次 switch 后立即
    读 current_index 可能仍是旧桌面——这正是「建了桌面却没切过去」的根因之一。
    每次 go() 后轮询最多 1s 确认 current_index==idx，不等则重试，共 attempts 轮。
    返回最终是否确认停留在 idx。"""
    last_seen = vd.current_index()
    for i in range(max(1, attempts)):
        vd.switch(idx)
        for _ in range(4):
            _time.sleep(0.25)
            last_seen = vd.current_index()
            if last_seen == idx:
                if i > 0:
                    print(f"[{tag}] 第 {i + 1} 次尝试后确认已切到桌面 #{idx}。", flush=True)
                return True
        print(f"[{tag}] ⚠️ go(#{idx}) 后当前桌面仍为 #{last_seen}，重试切换…", flush=True)
    print(f"[{tag}] ❌ {attempts} 次尝试后仍未切到桌面 #{idx}"
          f"（当前 #{last_seen}）。", flush=True)
    return False


def _start_uu_dock(uu_idx: int) -> Optional[int]:
    """在 UU 桌面启动回程 Dock（front/uu_dock.py）。
    必须【先切到 uu_idx 再 Popen】，窗口初始桌面才绑定该桌面。
    返回 dock PID；脚本缺失/启动失败返回 None。"""
    if not _UU_DOCK_SCRIPT.exists():
        print(f"[uu-open] ⚠️ 未找到 {_UU_DOCK_SCRIPT}，跳过回程 Dock。", flush=True)
        return None
    try:
        dock_fp = open(_UU_DOCK_LOG, "a", encoding="utf-8")
        p = subprocess.Popen(
            [sys.executable, str(_UU_DOCK_SCRIPT)],
            cwd=str(_SCRIPT_DIR),
            stdout=dock_fp, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        )
        print(f"[uu-open] 🖥️  UU 回程 Dock 已在桌面 #{uu_idx} 启动 PID={p.pid}"
              f"（日志 .vd_uudock.log）", flush=True)
        return p.pid
    except Exception as e:
        print(f"[uu-open] ⚠️  UU 回程 Dock 启动失败（不影响主链路）: {e!r}", flush=True)
        return None


def _launch_uu_on_desktop(vd: "VirtualDesktops", uu_idx: int, uu_cmd: list[str],
                          uu_exe: str, *, wait_ms: int = 10000, min_hwnds: int = 1,
                          force_new: bool = False) -> tuple[set[int], bool, str]:
    """在 uu_idx 桌面启动/唤出 UU GameViewer，返回 (记录用PID集合, 是否早已运行, 模式)。
    逻辑与旧 bootstrap Step3 完全一致（单例应用三分支）：
      A) 已运行且主窗口可见 → 归位 uu_idx；
      B) 已运行但缩托盘 → 切到 uu_idx 再 Popen 唤醒旧实例；
      C) 未运行/强制重启 → launch_on_desktop 强隔离启动。"""
    existing = _find_pids_by_names(_REMOTE_PROCESS_NAMES)
    record_pids: set[int] = set()
    for _pl in existing.values():
        for _pid in _pl:
            record_pids |= _collect_descendant_pids(_pid)
    preexisting = bool(record_pids) and not force_new
    if preexisting:
        visible = _enum_top_level_hwnds_for_pids(record_pids)
        if visible:
            mode = "preexisting+watcher"
            print(f"[uu-open] ℹ️  UU 已在运行且主窗口已显示：{existing}", flush=True)
            _move_wrong_hwnds(record_pids, uu_idx, {}, "uu-open-init")
        else:
            mode = "preexisting-wakeup+watcher"
            print(f"[uu-open] ℹ️  UU 已在运行但主窗口缩在系统托盘：{existing}", flush=True)
            print("[uu-open] 单例应用：切到 UU 桌面后再启动一次 exe = 唤出旧实例主窗口…",
                  flush=True)
            vd.switch(uu_idx)
            try:
                subprocess.Popen(uu_cmd)
            except Exception as e:
                print(f"[uu-open] ⚠️  唤醒 UU 失败：{e}；可手动从托盘点开，watcher 会自动归位。",
                      flush=True)
            else:
                deadline = _time.time() + max(3.0, wait_ms / 1000.0)
                while _time.time() < deadline:
                    _time.sleep(0.5)
                    record_pids = _remote_pids()
                    if _enum_top_level_hwnds_for_pids(record_pids):
                        break
                _move_wrong_hwnds(record_pids, uu_idx, {}, "uu-open-init")
                n_vis = len(_enum_top_level_hwnds_for_pids(record_pids))
                print(f"[uu-open] {'✅' if n_vis else '⚠️'} 唤出后可见窗口 {n_vis} 个。",
                      flush=True)
    else:
        p_uu, _poller, mode = vd.launch_on_desktop(
            uu_idx, uu_cmd,
            wait_window_ms=wait_ms,
            label=f"{_pathlib.Path(uu_exe).name} → #{uu_idx}",
            no_window=False,
            mode="switch_wait_switchback",
            min_hwnd_count=max(1, min_hwnds),
        )
        if p_uu is None:
            print("[uu-open] ⚠️  UU 启动失败。", flush=True)
        else:
            record_pids = _collect_descendant_pids(p_uu.pid)
    return record_pids, preexisting, mode


def _move_wrong_hwnds(pids: set[int], target_idx: int,
                      fail_counts: dict[int, int], tag: str,
                      skip_titles: Optional[set[str]] = None) -> int:
    """一轮归位：pids 进程树中所有可见顶层窗，**每轮重新查真实桌面**，
    凡不在 target_idx 的立刻迁走。返回本轮迁移窗口数。
    注意：不能用"曾经正确"的 HWND 缓存跳过——窗口随时可能被用户拖拽/其他
    程序/系统操作移出目标桌面，缓存会导致移走后再也搬不回来。
    skip_titles：命中这些精确标题的窗口跳过（手机主屏 main_display 由
    「手机」Dock 按钮按用户意愿在两个桌面间嵌入，watcher 不得回迁）。"""
    skip_titles = skip_titles or set()
    moved = 0
    for hwnd in _enum_top_level_hwnds_for_pids(pids):
        if skip_titles and _get_window_title(hwnd, 80) in skip_titles:
            fail_counts.pop(hwnd, None)
            continue
        real = _get_hwnd_desktop_index(hwnd)
        if real == target_idx:
            fail_counts.pop(hwnd, None)
            continue
        ok, how = _pyvda_move_hwnd(hwnd, target_idx)
        title = _get_window_title(hwnd, 40)
        if ok:
            fail_counts.pop(hwnd, None)
            moved += 1
            print(f"[watcher:{tag}] ✅ '{title}' #{real}→#{target_idx} ({how})", flush=True)
        else:
            fail_counts[hwnd] = fail_counts.get(hwnd, 0) + 1
            if fail_counts[hwnd] == 3:
                print(f"[watcher:{tag}] ⚠️ '{title}' 连续 3 次迁移失败: {how}", flush=True)
    return moved


def do_watch(args) -> int:
    """常驻归位守护（独立 detached 进程）：
      · server.py 子孙进程的窗口（含 AI 运行中**动态新开**的抖音/微信等 scrcpy
        卡片）一旦不在 server 桌面，立刻迁回；
      · 远程客户端（UU/向日葵/ToDesk，**含早已运行的实例**）与回程 Dock 的窗口
        只在 UU 桌面已由 uu-open 懒创建（state.uu_active=True）后才归到该桌面；
        未创建时 UU 尚未启动，保持空闲，绝不向不存在的桌面搬窗；
      · 每轮重新读取 state 文件——用户运行中点「电脑」按钮创建 UU 桌面后，
        无需重启 watcher 即可获得新的 uu_desktop / uu_dock_pid；
      · server.py 连续 3 轮确认退出后本守护自动退出。
    日志追加写入 .vd_watcher.log。"""
    state_file = _pathlib.Path(getattr(args, "state_file", None) or _STATE_FILE)
    interval = max(0.5, float(getattr(args, "interval", 1.5)))
    # 独立进程必须在本线程先初始化 COM，pyvda 的 COM 调用才能工作
    try:
        _ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        try:
            _ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            st = _json.load(f)
    except Exception as e:
        print(f"[watcher] 无法读取状态文件 {state_file}: {e!r}", flush=True)
        return 1

    # 手机主屏（main_display）标题：它由 Dock「手机」按钮按用户意愿在两个
    # 桌面间嵌入，watcher 必须放行，不能按普通 server 窗口回迁。
    def _main_display_title(st_now: dict) -> str:
        title = "手机桌面"
        proj_root = st_now.get("project_root")
        if proj_root:
            cfg_path = _pathlib.Path(proj_root) / "scrcpycontrol" / "display_config.json"
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    t = _json.load(f).get("main_display", {}).get("window_title")
                    if t:
                        title = str(t)
            except Exception:
                pass
        return title

    server_pid = int(st["server_pid"])
    skip_server_titles = {_main_display_title(st)}
    print(f"[watcher] 启动：server PID={server_pid}→#{st.get('server_desktop', 0)}，"
          f"轮询间隔 {interval}s，手机主屏 {next(iter(skip_server_titles))!r} 跟随用户不回迁；"
          f"UU 桌面尚未懒创建，UU 归位待机中。",
          flush=True)

    fail_s: dict[int, int] = {}
    fail_u: dict[int, int] = {}
    fail_d: dict[int, int] = {}
    missed = 0
    last_uu_active = None
    while True:
        # 每轮重读 state：捕获 uu-open 写入的 uu_active/uu_desktop/uu_dock_pid
        fresh = _load_state()
        if fresh is not None:
            st = fresh
        server_desk = int(st.get("server_desktop", 0))

        if not _pid_alive(server_pid):
            missed += 1
            if missed >= 3:
                print("[watcher] server.py 已退出，watcher 结束。", flush=True)
                return 0
        else:
            missed = 0
            _move_wrong_hwnds(_collect_descendant_pids(server_pid),
                              server_desk, fail_s, "server",
                              skip_titles=skip_server_titles)

        # UU 归位仅在懒创建完成后生效；且目标桌面当前必须真实存在
        uu_active = bool(st.get("uu_active")) and st.get("uu_desktop") is not None
        if uu_active != last_uu_active:
            print(f"[watcher] UU 归位状态切换：{'启用' if uu_active else '停用'}"
                  f"（uu_desktop={st.get('uu_desktop')}）", flush=True)
            last_uu_active = uu_active
        if uu_active:
            try:
                total_desktops = len(get_virtual_desktops())
            except Exception:
                total_desktops = 0
            uu_desk = int(st.get("uu_desktop"))
            if 0 <= uu_desk < total_desktops:
                # UU 无论 server 死活都持续归位（用户可能在停服后仍使用远程桌面）
                _move_wrong_hwnds(_remote_pids(), uu_desk, fail_u, "uu")
                # UU 桌面上的回程 Dock（front/uu_dock.py）始终钉在 UU 桌面
                uu_dock_pid = st.get("uu_dock_pid")
                if uu_dock_pid and _pid_alive(uu_dock_pid):
                    _move_wrong_hwnds(_collect_descendant_pids(int(uu_dock_pid)),
                                      uu_desk, fail_d, "uu-dock")
        _time.sleep(interval)


def do_signal_pid(args) -> int:
    """【内部】一次性信号助手：AttachConsole 到目标控制台并广播 CTRL_C 后退出。
    必须以 CREATE_NEW_PROCESS_GROUP 启动：助手自己的进程组=其 PID≠0，
    而 CTRL_C 只能投递给进程组 0，所以助手发出的信号永远不会杀死自己。
    返回码：0=已发送；2=AttachConsole 失败（目标无控制台）；3=发送失败。"""
    pid = int(args.pid)
    try:
        _kernel32.FreeConsole()
    except Exception:
        pass
    if not _kernel32.AttachConsole(pid):
        # [stop-diag] 常见：目标以 CREATE_NO_WINDOW/无控制台启动，或已退出
        print(f"[signal-pid] AttachConsole({pid}) 失败 "
              f"GetLastError={_ctypes.get_last_error()}", file=sys.stderr, flush=True)
        return 2
    try:
        _kernel32.SetConsoleCtrlHandler(None, True)
        ok = bool(_kernel32.GenerateConsoleCtrlEvent(0, 0))
        if not ok:
            print(f"[signal-pid] GenerateConsoleCtrlEvent(CTRL_C, 0) 失败 "
                  f"GetLastError={_ctypes.get_last_error()}", file=sys.stderr, flush=True)
        _time.sleep(0.3)
    finally:
        try:
            _kernel32.FreeConsole()
        except Exception:
            pass
        _kernel32.SetConsoleCtrlHandler(None, False)
    return 0 if ok else 3


def _send_ctrl_c_and_wait(pid: int, timeout_s: float = 12.0) -> bool:
    """派生独立进程组的信号助手给 pid 的控制台发 CTRL_C，然后等待其优雅退出。
    不在 stop 主进程里直接发——Python 自带 CTRL handler，自屏蔽时序不可靠，
    会把 stop 自己也打断（state 来不及清理）。
    返回 True=进程已退出。目标无控制台（--server-no-window）→ False，调用方回退强杀。"""
    try:
        helper = subprocess.run(
            [sys.executable, str(_SCRIPT_DIR / "virtual_desktop.py"),
             "signal-pid", "--pid", str(pid)],
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP：与目标控制台组 0 隔离
        )
    except Exception as e:
        print(f"[stop-diag] signal-pid 助手启动/执行异常：{e!r}", flush=True)
        return False
    print(f"[stop-diag] signal-pid 助手 rc={helper.returncode} "
          f"stdout={helper.stdout.strip()!r} stderr={helper.stderr.strip()!r}",
          flush=True)
    if helper.returncode != 0:
        return False
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if not _pid_alive(pid):
            return True
        _time.sleep(0.3)
    return not _pid_alive(pid)


def _wait_pid_exit(pid: int, timeout_s: float) -> bool:
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if not _pid_alive(pid):
            return True
        _time.sleep(0.3)
    return not _pid_alive(pid)


def _taskkill_tree(pid: int, *, label: str = "") -> bool:
    """taskkill /PID /T /F 强杀进程树。返回 rc==0；诊断期打印真实报错
    （旧代码 capture_output 把 'Access is denied' 吞掉，导致只看到仍存活）。"""
    r = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True, errors="replace",
    )
    tag = f"[{label}] " if label else ""
    detail = " ".join((r.stderr or r.stdout or "").split())
    if r.returncode == 0:
        print(f"[stop-diag]   {tag}taskkill /T /F PID={pid} ✅ rc=0 "
              f"{detail}", flush=True)
        return True
    print(f"[stop-diag]   {tag}taskkill /T /F PID={pid} ❌ rc={r.returncode} "
          f"报错={detail or '(无输出)'}", flush=True)
    return False


def do_stop(args) -> int:
    """全量停止整条链路并核验「零项目进程残留」（2026-09-26）：

       进程拓扑与各自归宿：
         · watcher（virtual_desktop.py watch，detached）… state PID + 命令行兜底；
         · UU 桌面 toolbar（front/uu_dock.py，detached）… state PID + 命令行兜底；
         · server.py（py launcher + python 树）…… CTRL_C 优雅退出，超时强杀 /T；
         · 主桌面 toolbar（qt_backdrop.py 的 multiprocessing spawn 子进程，内含
           Backdrop/Dock/通知/思考 4 个窗口）… server finally 会 stop，再用
           .vd_backdrop.pid 核验补刀（强杀 server 时 daemon 回收机制会失效）；
         · scrcpy.exe × N …… atexit stop_all 后仍无条件按进程名扫尾；
         · UU 主程序（GameViewer.exe 及其 webview2/crashpad 子孙）… 默认 /T 整棵
           关闭（--no-kill-uu 保留）；后台服务无窗口默认不动；
         · UU 虚拟桌面 …… 默认删除（--no-remove-desktops 保留）。

       无 state 也执行「孤儿清扫模式」：state 失配/被清后仍能按命令行特征、
       PID 文件、进程名把残留清干净。
       注：adb fork-server（5037）是全机共享服务，默认不动（结尾给出提示）。"""
    st = _load_state() or {}
    forced = getattr(args, "server_pid", None)
    if not st:
        if forced:
            print(f"[stop] 未找到 {_STATE_FILE.name}，使用命令行指定的 server PID={forced}。",
                  flush=True)
            st = {"server_pid": int(forced)}
        else:
            print(f"[stop] 未找到 {_STATE_FILE.name}：进入【孤儿清扫模式】——不发 "
                  f"CTRL_C，但仍按命令行特征 / PID 文件 / 进程名清理 watcher、"
                  f"两个 toolbar、scrcpy、UU。", flush=True)
            st = {}

    watcher_pid = st.get("watcher_pid")
    server_pid = st.get("server_pid")
    uu_dock_pid = st.get("uu_dock_pid")
    kill_uu = bool(getattr(args, "kill_uu", True))
    remove_desktops = bool(getattr(args, "remove_desktops", True))

    # 从这里起本进程忽略 CTRL_C：助手把 CTRL_C 广播到 server 所在控制台的
    # 进程组 0，若调用方恰好共享该控制台（单控制台宿主/同窗口调试），
    # 不屏蔽的话 stop 自己会被 KeyboardInterrupt 打断，清理会半途而废。
    _kernel32.SetConsoleCtrlHandler(None, True)
    try:
        print(f"[stop-diag] state 记录的 PID：server={server_pid} "
              f"watcher={watcher_pid} uu_dock={uu_dock_pid}；"
              f"kill_uu={kill_uu} remove_desktops={remove_desktops}", flush=True)
        print("[stop-diag] 停止前相关进程快照（python/scrcpy/adb/uu）：", flush=True)
        _diag_dump_related()

        # ---- 1) 先杀 watcher：防止后续窗口消失/桌面删除时它来回搬窗 ----
        if _pid_alive(watcher_pid):
            _taskkill_tree(int(watcher_pid), label="watcher")
            print(f"[stop] watcher PID={watcher_pid} 已停止。", flush=True)

        # ---- 2) 先杀 UU 桌面 toolbar（纯 UI，强杀无副作用）----
        if uu_dock_pid and _pid_alive(uu_dock_pid):
            _taskkill_tree(int(uu_dock_pid), label="uu-dock")
            print(f"[stop] UU 桌面 toolbar（uu_dock.py）PID={uu_dock_pid} 已停止。",
                  flush=True)

        # ---- 3) CTRL_C 优雅停 server.py；失败/超时回退整树强杀 ----
        if _pid_alive(server_pid):
            print(f"[stop] 向 server.py PID={server_pid} 发送 CTRL_C"
                  f"（优雅退出，最多等 12s）...", flush=True)
            print(f"[stop-diag] CTRL_C 前 server PID={server_pid} 进程树：", flush=True)
            _diag_dump_tree(int(server_pid))
            try:
                graceful = _send_ctrl_c_and_wait(int(server_pid))
            except KeyboardInterrupt:
                print("[stop] （本进程收到同源 CTRL_C，已忽略，继续等待 server 退出…）",
                      flush=True)
                graceful = _wait_pid_exit(int(server_pid), 8.0)
            print(f"[stop-diag] CTRL_C 等待结束 graceful={graceful}，"
                  f"此刻 server PID={server_pid} 进程树：", flush=True)
            _diag_dump_tree(int(server_pid))
            if graceful:
                print("[stop] ✅ server.py 已优雅退出（finally 停 toolbar + "
                      "atexit stop_all 停 scrcpy）。", flush=True)
                _time.sleep(1.0)  # 给 daemon 子进程回收 / atexit 留点收尾时间
            else:
                print("[stop] ⚠️ CTRL_C 未送达或超时，回退 taskkill /PID /T /F"
                      "（整棵 server 树一起清）...", flush=True)
                _taskkill_tree(int(server_pid), label="server")
                _time.sleep(1.5)
                print("[stop-diag] 强杀后 server 进程树：", flush=True)
                _diag_dump_tree(int(server_pid))
                if _pid_alive(server_pid):
                    print(f"[stop] ❌ server PID={server_pid} 仍存活，"
                          f"稍后命令行兜底会再试一次。", flush=True)
        elif server_pid:
            print(f"[stop] server.py PID={server_pid} 已不在运行。", flush=True)

        # ---- 4) 无条件兜底清扫（不信赖任何隐式回收，只认真实存活）----
        # 4a. 命令行特征：watcher / uu_dock / server（覆盖 state PID 失配、
        #     被 uu-open 补启、强杀后子孙变孤儿等全部场景）
        hits = _find_project_pids_by_cmdline(_STOP_CMDLINE_PATTERNS)
        for label in ("watcher", "uu-dock", "server"):
            for pid in hits.get(label, []):
                if _pid_alive(pid) and _taskkill_tree(pid, label=f"residual-{label}"):
                    print(f"[stop] 兜底结束残留 {label} PID={pid}。", flush=True)

        # 4b. 主桌面 toolbar：.vd_backdrop.pid（spawn 子进程命令行无特征，只能靠它）
        bd_pid = _read_pid_file(_BACKDROP_PID_FILE)
        if bd_pid is not None:
            if _pid_alive(bd_pid):
                _taskkill_tree(bd_pid, label="main-toolbar(qt_backdrop)")
                print(f"[stop] 主桌面 toolbar（qt_backdrop.py）PID={bd_pid} 已停止。",
                      flush=True)
            else:
                print(f"[stop] 主桌面 toolbar PID={bd_pid} 已自行退出。", flush=True)

        # 4c. scrcpy：server 死后任何残留 scrcpy 都是孤儿，无条件全部清扫
        n_scrcpy = _sweep_scrcpy()
        if n_scrcpy:
            print(f"[stop] 已清扫残留 scrcpy.exe {n_scrcpy} 个。", flush=True)

        print("[stop-diag] 主体清理后残留快照：", flush=True)
        _diag_dump_related()

        # ---- 5) UU 主程序：默认整棵关闭（/T 带走 webview2/crashpad 子孙）----
        if kill_uu:
            killed_uu = 0
            for name, pids in _find_pids_by_names(_REMOTE_PROCESS_NAMES).items():
                for pid in pids:
                    if _taskkill_tree(pid, label=f"uu-{name.rstrip('.exe')}"):
                        killed_uu += 1
            if killed_uu:
                print(f"[stop] 已关闭 UU 远程主程序 {killed_uu} 个（整棵进程树，"
                      f"含 WebView2/crashpad 子孙）。", flush=True)
                _time.sleep(1.5)  # 等服务可能的守护重启/子进程退出
                # 二次核验：webview2/crashpad 若成孤儿，按命令行路径补杀
                for pid, cmdline in _list_cmdlines_for_names(
                        {"msedgewebview2.exe", "crashpad_handler.exe"}).items():
                    if ("GameViewer" in cmdline or "UUdesk" in cmdline) and _pid_alive(pid):
                        _taskkill_tree(pid, label="uu-webview-orphan")
                        print(f"[stop] 兜底结束 UU 孤儿子孙 PID={pid}。", flush=True)
                # 主程序被后台服务自动拉起的检测
                if _find_pids_by_names(_REMOTE_PROCESS_NAMES):
                    print("[stop] ⚠️ UU 主程序被后台服务（GameViewerService 等）"
                          "重新拉起。服务无窗口且可能需管理员权限，未自动停止；"
                          "如需彻底关闭，请在「服务」管理器中停止 NetEase UU 相关服务。",
                          flush=True)
            else:
                print("[stop] 未发现运行中的 UU 远程主程序。", flush=True)
            if _find_pids_by_names(_REMOTE_SERVICE_NAMES):
                print("[stop] ℹ️  UU 后台服务（GameViewerService/Server/Healthd，"
                      "无窗口）按策略保留。", flush=True)

        # ---- 6) 删除 UU 虚拟桌面（窗口已关闭，桌面变空再删）----
        if remove_desktops:
            vd = VirtualDesktops()
            uu_idx = st.get("uu_desktop")
            server_idx = st.get("server_desktop", 0)
            if uu_idx is None:
                print("[stop] 无 UU 桌面记录（state.uu_desktop 为空）；"
                      "若确有多余空桌面，请手动：python virtual_desktop.py list "
                      "→ remove --index N", flush=True)
            elif int(uu_idx) == int(server_idx):
                print(f"[stop] UU 桌面 #{uu_idx} 与手机桌面相同，不删。", flush=True)
            elif int(uu_idx) < vd.count():
                vd.remove(int(uu_idx))
            else:
                print(f"[stop] UU 桌面 #{uu_idx} 已不存在（当前共 {vd.count()} 个），跳过。",
                      flush=True)

        # ---- 7) 最终残留核验 ----
        leftovers = _find_project_pids_by_cmdline(_STOP_CMDLINE_PATTERNS)
        remain_py = sorted(pid for pids in leftovers.values() for pid in pids
                           if _pid_alive(pid))
        remain_bd = [bd] if (bd := _read_pid_file(_BACKDROP_PID_FILE)) and _pid_alive(bd) else []
        remain_scrcpy = [pid for pids in _find_pids_by_names({"scrcpy.exe"}).values()
                         for pid in pids if _pid_alive(pid)]
        remain_uu = ([pid for pids in _find_pids_by_names(_REMOTE_PROCESS_NAMES).values()
                      for pid in pids if _pid_alive(pid)] if kill_uu else [])
        # adb fork-server（5037）：全机共享服务（IDE/其他工具可能在用），默认不动
        adb_pids = [pid for pids in _find_pids_by_names({"adb.exe"}).values()
                    for pid in pids if _pid_alive(pid)]

        # ---- 8) 清理运行态文件 ----
        if not getattr(args, "keep_state", False):
            _clear_state()
            print(f"[stop] 已清理状态文件 {_STATE_FILE.name}。", flush=True)
        try:
            _BACKDROP_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        if remain_py or remain_bd or remain_scrcpy or remain_uu:
            print("[stop] ❌ 仍有项目进程残留（可能权限不足，请任务管理器手动结束）：",
                  flush=True)
            if remain_py:
                print(f"        python(server/watcher/uu_dock) PIDs={remain_py}", flush=True)
            if remain_bd:
                print(f"        主桌面 toolbar qt_backdrop PIDs={remain_bd}", flush=True)
            if remain_scrcpy:
                print(f"        scrcpy.exe PIDs={remain_scrcpy}", flush=True)
            if remain_uu:
                print(f"        UU 主程序 PIDs={remain_uu}", flush=True)
        else:
            print("[stop] ✅ 项目进程零残留：server / watcher / 两个 toolbar / "
                  "scrcpy 全部结束" + ("，UU 已关闭" if kill_uu else "（UU 按要求保留）")
                  + "。", flush=True)
        if adb_pids:
            print(f"[stop] ℹ️  adb 服务（5037 端口，全机共享）PID={adb_pids} 未停止；"
                  f"它不占桌面/窗口，如确需关闭请执行：adb kill-server", flush=True)
    finally:
        _kernel32.SetConsoleCtrlHandler(None, False)
    return 0


def do_status(args) -> int:
    st = _load_state()
    vd = VirtualDesktops()
    if not st:
        print(f"无活动 bootstrap 会话（{_STATE_FILE.name} 不存在）。")
        print(vd.info())
        return 1
    print(f"活动 bootstrap 会话（{_STATE_FILE.name}）：")
    print(f"  启动时间      : {st.get('started_at')}")
    print(f"  server.py     : PID={st.get('server_pid')} "
          f"{'🟢 运行中' if _pid_alive(st.get('server_pid')) else '⚪ 已退出'} → 桌面 #{st.get('server_desktop')}")
    print(f"  watcher       : PID={st.get('watcher_pid')} "
          f"{'🟢 运行中' if _pid_alive(st.get('watcher_pid')) else '⚪ 已退出'}")
    uu_idx = st.get("uu_desktop")
    uu_active = bool(st.get("uu_active"))
    if not uu_active or uu_idx is None:
        print("  UU 远程桌面   : ⚪ 未创建（点 toolbar 电脑钮 / 运行 uu-open 按需创建）")
    else:
        dock_pid = st.get('uu_dock_pid')
        if dock_pid:
            print(f"  UU回程Dock    : PID={dock_pid} "
                  f"{'🟢 运行中' if _pid_alive(dock_pid) else '⚪ 已退出'} → 桌面 #{uu_idx}")
        print(f"  UU 远程       : {'早已运行(按进程名归位)' if st.get('uu_preexisting') else 'uu-open 启动'} "
              f"→ 桌面 #{uu_idx}，PID={st.get('uu_pids')}")
    print(vd.info())
    return 0



def do_bootstrap(args) -> int:
    r"""
    编排主流程（2026-09-26 懒创建模型：**不创建任何虚拟桌面、不启动 UU**）：

      布局：当前桌面（通常 #0）= 唯一桌面，手机桌面布局（server.py + 全部
            scrcpy 卡片）直接出现在用户当前所在的桌面上；
            UU 桌面改为运行期由主桌面 Dock「电脑」按钮触发 `uu-open` 按需创建。

      1) 记录当前桌面编号（不切换、不新建）；
      2) 循环确认手机 ADB 可达（每 3s 一次，最长 5min）；
      3) 在当前桌面 Popen server.py，轮询条件等待 ≥N 个顶层 HWND 真正出现；
      4) join sweeper poller（8~10s 扫尾迟来的 scrcpy）；
      5) 审计报告：枚举 server 子孙 PID 的全部 HWND 与真实桌面归属；
      6) state 落盘（uu_active=False，UU 相关字段为空）+ 启动常驻 watcher。
    """
    vd = VirtualDesktops()
    server_desk = vd.current_index()
    launch_mode = getattr(args, "launch_mode", "auto") or "auto"

    print(f"[bootstrap] 开始：手机布局将直接启动在【当前桌面 #{server_desk}】")
    print(f"[bootstrap] launch_mode = {launch_mode}")
    print(
        "[bootstrap] 🛡️  懒创建模式：不新建虚拟桌面、不启动 UU。\n"
        "           server.py + 全部 scrcpy 手机卡片都在当前桌面启动；\n"
        "           需要 UU 远程时，点桌面底部 Dock 的「电脑」按钮，系统会按需\n"
        "           新建 UU 桌面并启动 GameViewer（已存在则只切换，不重复创建）。\n"
    )
    print("-" * 64)

    # Step 1.5: 启动 server.py 前确认手机 ADB 可达
    #   每 3s 检测一次（有 USB 设备直接通过；否则用 --phoneaddress 执行 adb connect），
    #   持续 5 分钟仍未连上才中止。
    phoneaddress = getattr(args, "phoneaddress", None)
    phone_retry_s = 3
    phone_timeout_s = 300
    print(
        f"[bootstrap] 检查手机 ADB 连接（每 {phone_retry_s}s 重试一次，"
        f"最长等待 {phone_timeout_s}s；可插入 USB 或在手机上确认无线调试授权）..."
    )
    phone_ok, phone_msg = False, ""
    phone_deadline = _time.monotonic() + phone_timeout_s
    phone_attempt = 0
    while True:
        phone_attempt += 1
        elapsed_s = int(_time.monotonic() - (phone_deadline - phone_timeout_s))
        phone_ok, phone_msg = _ensure_phone_connected(phoneaddress, verbose=False)
        if phone_ok:
            print(f"[bootstrap] ✅ 第 {phone_attempt} 次检测（已等待 {elapsed_s}s）：{phone_msg}")
            break
        first_line = str(phone_msg).splitlines()[0]
        remain_s = max(0, int(phone_deadline - _time.monotonic()))
        print(
            f"[bootstrap] ⏳ 第 {phone_attempt} 次检测未连接（已等待 {elapsed_s}s，"
            f"剩余 {remain_s}s）：{first_line}"
        )
        if _time.monotonic() >= phone_deadline:
            print(
                f"[bootstrap] ❌ 等待 {phone_timeout_s}s 手机仍未连接，"
                f"中止 bootstrap（server.py 未启动，桌面已就绪）。最后原因：{first_line}"
            )
            return 1
        _time.sleep(phone_retry_s)

    # —— Step 2 命令组装：server.py ——
    py = _locate_python(getattr(args, "python", None))
    server_py = _locate_server_py(
        getattr(args, "project_root", None),
        getattr(args, "server_script", _DEFAULT_SERVER_SCRIPT),
    )
    server_cmd: list[str] = [py, str(server_py)]
    if getattr(args, "model", None):
        server_cmd += ["--model", str(args.model)]
    if getattr(args, "host", None):
        server_cmd += ["--host", str(args.host)]
    if getattr(args, "port", None):
        server_cmd += ["--port", str(args.port)]
    if getattr(args, "device_id", None):
        server_cmd += ["--device-id", str(args.device_id)]

    # —— 参数整理（懒创建模型下 bootstrap 只关心 server.py；
    #    UU 的 exe 路径/等待参数归 uu-open 子命令所有）——
    server_wait_ms = max(6000, int(getattr(args, "server_wait_ms", 18000)))
    server_no_win  = bool(getattr(args, "server_no_window", False))
    server_min_hw  = max(1, int(getattr(args, "server_min_hwnds", 2)))   # Qt backdrop 至少有 2 窗

    pollers_to_wait: list[_threading.Thread] = []
    srv_mode_used = launch_mode if launch_mode != "auto" else "switch_wait_switchback"

    # =====================================================================
    # Step 2：在【当前桌面】启 server.py（不切桌面 → 条件等 HWND ≥N 个出现）
    #   手机桌面布局直接显示在用户当前所在的桌面上。
    # =====================================================================
    print(f"\n=== Step 2 / 2 ：当前桌面 #{server_desk} → server.py ===")
    p_server, srv_poller, srv_mode_used = vd.launch_on_desktop(
        server_desk, server_cmd,
        cwd=str(server_py.parent),
        wait_window_ms=server_wait_ms,
        label=f"{server_py.name} → #{server_desk}",
        no_window=server_no_win,
        mode=launch_mode,
        min_hwnd_count=server_min_hw,
    )
    if p_server is None:
        print("[bootstrap] ⚠️  server.py 启动失败。")
    else:
        if srv_poller is not None:
            pollers_to_wait.append(srv_poller)

    # =====================================================================
    # Step 3：join sweeper poller（~10s，扫尾迟来的 scrcpy 卡片）
    #   UU 远程与回程 Dock 已不在 bootstrap 启动——改由 Dock「电脑」按钮
    #   触发 `uu-open` 按需创建，见 do_uu_open()。
    # =====================================================================
    if pollers_to_wait:
        wait_join_s = (server_wait_ms / 1000.0) * 0.8 + 2.0
        print(
            f"\n[bootstrap] 🧹 扫尾阶段：等 {len(pollers_to_wait)} 个 sweeper poller 结束"
            f"（最多 {wait_join_s:.1f}s，抓那些 18s 后才冒出来的 scrcpy 卡片）..."
        )
        for th in pollers_to_wait:
            th.join(timeout=wait_join_s)
        print("[bootstrap] ✅ Sweeper poller 结束。\n")

    # =====================================================================
    # Step 4：审计报告 —— 逐条打印 server PID 树的 HWND + 真实桌面编号
    #         一眼确认手机布局窗口都在【当前桌面 server_desk】。
    # =====================================================================
    print()
    print("=" * 80)
    print("[bootstrap] 🧾 窗口归属审计：逐个窗口核对「真实桌面归属」")
    print("=" * 80)
    print(f"  launch_mode: server={srv_mode_used}（懒创建：无 UU 桌面）")
    print(f"  虚拟桌面范围: [0, {vd.count() - 1}]（bootstrap 未创建任何桌面）")
    print(f"  脚本结束当前桌面: #{vd.current_index()}")
    print("-" * 80)

    def _audit_one(heading: str, root_pid: Optional[int], expected_idx: int):
        print(f"\n{heading} （期望全部落在 desktop#{expected_idx}）：")
        if not root_pid:
            print("    进程未启动，跳过。")
            return (0, 0)
        pids = _collect_descendant_pids(root_pid)
        hwnds = _enum_top_level_hwnds_for_pids(pids)
        correct, wrong = 0, 0
        if not hwnds:
            print(f"    （没有检测到任何顶层窗口） 子孙 PID 共 {len(pids)} 个。")
            return (0, 0)
        for hwnd in hwnds:
            title = _get_window_title(hwnd, 60)
            real = _get_hwnd_desktop_index(hwnd)
            ok = (real == expected_idx)
            if real is None:
                marker = "❔"
                diag = (
                    "  ← **严重：该窗未被 Windows VirtualDesktopManager 登记**"
                    "（IApplicationView 找不到）。典型根因：窗口 ex_style 有"
                    " WS_EX_TOOLWINDOW 或无 WS_EX_APPWINDOW，导致 VDM 不分配"
                    " desktop GUID，结果是窗口显示在每一个虚拟桌面上（看似"
                    "复制了 N 份，实际只 1 份 / N 个桌面都可见）。请检查对应"
                    "模块的 SetWindowLongW(GWL_EXSTYLE)：必须 |= WS_EX_APPWINDOW"
                    " 且 &= ~WS_EX_TOOLWINDOW。"
                )
                wrong += 1
            elif ok:
                marker = "✅"
                correct += 1
                diag = ""
            else:
                marker = "❌"
                wrong += 1
                diag = (
                    f"  ← **隔离失败！期望 #{expected_idx}，"
                    f"实际在 #{real}（请检查这个窗口是否创建于切走后仍在执行）**"
                )
            line = (
                f"    {marker} HWND={hex(hwnd)}  真实桌面=#{real}  "
                f"标题='{title[:50]}'{diag}"
            )
            print(line)
        print(f"    汇总: {correct} 个在 #{expected_idx}，{wrong} 个不在（共 {len(hwnds)} 个窗口）。")
        return correct, wrong

    total_ok, total_ng = _audit_one(
        f"桌面 #{server_desk} / server.py 的子孙窗口（手机布局）",
        p_server.pid if p_server else None, server_desk,
    )

    print()
    print("-" * 80)
    srv_state = ("OK  PID=" + str(p_server.pid)) if p_server else "FAILED"
    print(f"  当前桌面 #{server_desk} / server.py（手机布局）→ {srv_state}  |  cwd = {server_py.parent}")
    print(f"  UU 远程桌面 → 未创建（点 Dock「电脑」按钮按需创建）")
    if total_ng == 0:
        print(f"\n  🎯 审计通过：共 {total_ok} 个窗口全部在当前桌面 #{server_desk}。")
    else:
        print(
            f"\n  ⚠️  仍有 {total_ng} 个窗口不在当前桌面（watcher 会在运行期间持续补迁）。\n"
            f"     建议：\n"
            f"       · 如果是 scrcpy 还没出完：把 --server-wait-ms 加到 25000；\n"
            f"       · 如果是 Qt notification_panel：把 --server-min-hwnds 调到 3；\n"
            f"       · 动态新开的卡片：常驻 watcher 每 1.5s 巡检归位。"
        )
    print("=" * 80)

    # =====================================================================
    # Step 5：状态落盘 + 启动常驻归位 watcher
    #   懒创建模型：state 里 UU 字段为空/未激活；uu-open 运行期回填
    #   （watcher 每轮重读 state，自动接续 UU/回程Dock 的归位）。
    #   watcher 解决 bootstrap 进程退出后无人管的动态窗口：
    #     a) server 运行期间 AI 动态新开的 scrcpy 卡片（旧 sweeper 只活十几秒）；
    #     b) uu-open 创建 UU 桌面后，托盘点开的 UU 窗口归位。
    #   stop 子命令凭状态文件先杀 watcher，再 CTRL_C 优雅停 server。
    # =====================================================================
    watcher_pid = None
    if p_server is not None:
        state = {
            "started_at": _datetime.now().isoformat(timespec="seconds"),
            "project_root": str(_SCRIPT_DIR),
            "server_pid": p_server.pid,
            # [stop-diag] 记录启动时权限：server 子进程继承该完整性级别，
            # stop 端若级别更低，taskkill 会 Access is denied
            "bootstrap_elevated": _is_elevated(),
            "server_desktop": server_desk,
            # —— UU 懒创建字段：bootstrap 阶段全部为空，由 uu-open 回填 ——
            "uu_active": False,
            "uu_desktop": None,
            "uu_pids": [],
            "uu_preexisting": False,
            "uu_dock_pid": None,
            "watcher_pid": None,
        }
        _save_state(state)  # watcher 启动即读，必须先落盘
        if not bool(getattr(args, "no_watcher", False)):
            try:
                log_fp = open(_WATCHER_LOG, "a", encoding="utf-8")
                wcmd = [
                    sys.executable, str(_SCRIPT_DIR / "virtual_desktop.py"), "watch",
                    "--state-file", str(_STATE_FILE), "--interval", "1.5",
                ]
                wp = subprocess.Popen(
                    wcmd, cwd=str(_SCRIPT_DIR),
                    stdout=log_fp, stderr=subprocess.STDOUT, close_fds=True,
                    creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                )
                watcher_pid = wp.pid
                print(f"[bootstrap] 🛰️  常驻归位 watcher 已启动 PID={watcher_pid}（日志 .vd_watcher.log）")
            except Exception as e:
                print(f"[bootstrap] ⚠️  watcher 启动失败（不影响 server）: {e!r}")
        state["watcher_pid"] = watcher_pid
        _save_state(state)
        print(f"[bootstrap] 💾 会话状态已写入 {_STATE_FILE.name}；"
              f"停止整条链路请执行：python virtual_desktop.py stop")

    return 0 if p_server is not None else 1


def do_uu_open(args) -> int:
    """Dock「电脑」按钮调用的懒创建入口（detached 独立进程，日志 .vd_uu.log）：

      决策表：
        A) state.uu_active 且 UU 桌面仍存在 且 UU 进程存活
             → 只切换到该桌面（回程 Dock 死了顺手补启），【不创建/不启动】；
        B) UU 桌面存在但 UU 已退出 / 从未启动成功
             → 切过去，在该桌面启动（或唤醒）UU，补启回程 Dock；
        C) UU 桌面不存在（从未创建，或被用户手动删掉）
             → 新建桌面（编号=当前总数-1）→ 切过去 → 启 UU + 回程 Dock；
      全程结束后停留在 UU 桌面（这正是点「电脑」的目的，无 switchback）。
    """
    # 与 watcher 相同：detached 新进程先初始化 COM，pyvda 调用才可靠
    try:
        _ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        try:
            _ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass

    lock_fp = _acquire_uu_lock()
    if lock_fp is None:
        print("[uu-open] 另一个 uu-open 正在创建/启动（锁占用）；"
              "它会先切到 UU 桌面，本次直接退出。", flush=True)
        return 0

    try:
        st = _load_state()
        if not st:
            print(f"[uu-open] ❌ 状态文件 {_STATE_FILE.name} 不存在——server 尚未 bootstrap？",
                  flush=True)
            return 1

        vd = VirtualDesktops()
        total = vd.count()
        raw_uu_idx = st.get("uu_desktop")
        uu_idx = int(raw_uu_idx) if raw_uu_idx is not None else None
        uu_pids = _remote_pids()
        uu_alive = bool(uu_pids)
        desktop_exists = uu_idx is not None and 0 <= uu_idx < total

        # —— 情况 A：桌面 + UU 都在 → 只切换（需求 4：建好后电脑钮仅负责桌面来回切换）——
        if st.get("uu_active") and desktop_exists and uu_alive:
            print(f"[uu-open] ℹ️  UU 桌面 #{uu_idx} 与 UU 进程均已存在，只切换，"
                  f"不重复创建、不重复启动。", flush=True)
            _switch_desktop_verified(vd, uu_idx, tag="uu-open")
            # 回程 Dock 若已退出则顺手补启（不影响「不重复启动 UU」语义；
            # 必须在确认已切到 uu_idx 后再 Popen，窗口才绑定该桌面）
            dock_pid = st.get("uu_dock_pid")
            if not (dock_pid and _pid_alive(dock_pid)) and not bool(getattr(args, "no_uu_dock", False)):
                dock_pid = _start_uu_dock(uu_idx)
                _update_state(uu_dock_pid=dock_pid)
            return 0

        # —— 情况 C：桌面不存在 → 新建（编号=当前总数，通常即 1）——
        #   关键事务顺序：create() 一成功就【立刻】把 uu_desktop 落盘（早于切换/启动），
        #   这样即便后续切换、启 UU 任何一步失败，下次点电脑钮读到的也是「桌面已存在」，
        #   只会走情况 B 补启动 UU，绝不再重复创建桌面（全程至多 1 个 UU 桌面）。
        if not desktop_exists:
            uu_idx = vd.create()
            print(f"[uu-open] 🆕 已按需新建 UU 桌面 #{uu_idx}（当前共 {vd.count()} 个桌面）；"
                  f"立即登记到 state，今后只复用、不再重复创建。", flush=True)
            st = _update_state(
                uu_desktop=uu_idx, uu_active=False,
                uu_pids=[], uu_preexisting=False, uu_dock_pid=None,
            )
            action = "创建桌面 + 启动 UU"
        else:
            # —— 情况 B：UU 桌面已存在但 UU 没起来（上次创建后启动失败/被关闭）——
            #   需求 3：只启动/唤醒 UU，绝不重新创建虚拟桌面。
            action = "桌面已存在，仅启动/唤醒 UU（不重建桌面）"
            print(f"[uu-open] ℹ️  UU 桌面 #{uu_idx} 已存在但 UU 未运行（uu_alive={uu_alive}），"
                  f"只启动 UU、不重建桌面。", flush=True)

        # 组装 UU 启动命令（第 1 候选 = D:\UUdesk\GameViewer\GameViewer.exe）
        uu_exe = _find_uu_remote(getattr(args, "uu_remote_path", None))
        uu_cmd = [uu_exe]
        extra = getattr(args, "uu_remote_args", "")
        if isinstance(extra, str) and extra.strip():
            uu_cmd += extra.strip().split()
        uu_wait_ms = max(2000, int(getattr(args, "uu_wait_ms", 10000)))
        uu_min_hw = max(1, int(getattr(args, "uu_min_hwnds", 1)))
        force_new = bool(getattr(args, "force_new_uu", False))

        # 先切到 UU 桌面再启动（强隔离：Popen 时活跃桌面=UU 桌面，窗口直接生于此）。
        # 需求 1：切换后轮询校验真的切过去了，失败重试，不能「建了却停在原桌面」。
        switched = _switch_desktop_verified(vd, uu_idx, tag="uu-open")
        if not switched:
            print(f"[uu-open] ⚠️ 未能确认切换到 #{uu_idx}，仍尝试启动；"
                  f"若窗口落在别的桌面，watcher 会归位到 #{uu_idx}。", flush=True)
        print(f"[uu-open] 🚀 {action}：{uu_exe}", flush=True)
        record_pids, preexisting, mode = _launch_uu_on_desktop(
            vd, uu_idx, uu_cmd, uu_exe,
            wait_ms=uu_wait_ms, min_hwnds=uu_min_hw, force_new=force_new,
        )

        # 回程 Dock：已存活则复用，否则在 UU 桌面启动
        dock_pid = st.get("uu_dock_pid")
        if dock_pid and _pid_alive(dock_pid):
            print(f"[uu-open] ℹ️  回程 Dock PID={dock_pid} 已存活，复用。", flush=True)
            _move_wrong_hwnds(_collect_descendant_pids(int(dock_pid)),
                              uu_idx, {}, "uu-open-dock")
        elif not bool(getattr(args, "no_uu_dock", False)):
            dock_pid = _start_uu_dock(uu_idx)

        # 回填最终 state（watcher 下一轮（≤1.5s）即读到并开始 UU 归位）
        _update_state(
            uu_active=True,
            uu_desktop=uu_idx,
            uu_pids=sorted(record_pids),
            uu_preexisting=preexisting,
            uu_dock_pid=dock_pid,
        )

        # 终态确认：必须停留在 UU 桌面（点「电脑」的目的）。再校验切换一次。
        if vd.current_index() != uu_idx:
            _switch_desktop_verified(vd, uu_idx, tag="uu-open-end")
        n_vis = len(_enum_top_level_hwnds_for_pids(record_pids))
        print(f"[uu-open] ✅ 完成（mode={mode}）：UU 桌面 #{uu_idx}，"
              f"可见 UU 窗口 {n_vis} 个，回程Dock PID={dock_pid}，当前桌面 #{vd.current_index()}。",
              flush=True)
        return 0
    finally:
        _release_uu_lock(lock_fp)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Windows 10/11 虚拟桌面管理 + 懒创建编排：bootstrap 只在当前桌面启 server(手机布局)，"
                    "不建虚拟桌面；点 toolbar 电脑钮触发 uu-open 时才按需创建 UU 桌面并启动 UU 远程"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ensure = sub.add_parser("ensure", help="确保虚拟桌面数量 ≥ N（不足自动创建，超过则无动作）")
    p_ensure.add_argument("--count", type=int, default=1, help="最少个数（默认=1：仅主桌面；UU 桌面由 uu-open 按需创建）")

    p_switch = sub.add_parser("switch", help="切换到指定编号（0-based，左→右）的桌面")
    p_switch.add_argument("--index", type=int, required=True, help="桌面编号 0 / 1 / 2 ...")

    sub.add_parser("list", help="打印桌面总数、当前编号、可用范围")
    sub.add_parser("create", help="立即新建 1 个虚拟桌面（追加到最右端）")

    p_remove = sub.add_parser("remove", help="删除指定编号的桌面（窗口合并到左侧邻居）")
    p_remove.add_argument("--index", type=int, required=True)

    # 一键编排 bootstrap：当前桌面启 server（不建虚拟桌面、不启 UU）
    p_boot = sub.add_parser(
        "bootstrap",
        help="在【当前所在桌面】启动 server.py（手机布局），不创建虚拟桌面、不启动 UU 远程；\n"
             "UU 桌面/UU 远程由 toolbar 电脑钮触发 uu-open 按需懒创建",
    )
    # ===== 模式 =====
    p_boot.add_argument("--launch-mode",
                        choices=["auto", "switch_wait_switchback", "switch_then_sleep", "move_after"],
                        default="auto",
                        help=("server 启动模式（默认=auto → switch_wait_switchback）：\n"
                              "  · switch_wait_switchback（推荐 ✅）：Popen→等≥N个窗口真出现；100%%落在当前桌面\n"
                              "  · switch_then_sleep：死sleep毫秒（老兼容模式）\n"
                              "  · move_after：事后迁移（隔离不稳定）"))
    # server 相关
    p_boot.add_argument("--python",
                        help="server.py 使用的 Python 解释器路径（默认=当前 sys.executable）")
    p_boot.add_argument("--project-root",
                        help=f"项目根目录（默认=脚本同目录：{_DEFAULT_PROJECT_ROOT}）")
    p_boot.add_argument("--server-script", default=_DEFAULT_SERVER_SCRIPT,
                        help=f"要在主桌面 #0 启动的脚本名（默认={_DEFAULT_SERVER_SCRIPT}）")
    p_boot.add_argument("--server-no-window", action="store_true",
                        help="隐藏 server.py 的控制台黑框（CREATE_NO_WINDOW）")
    p_boot.add_argument("--server-wait-ms", type=int, default=18000,
                        help="switch_wait_switchback 模式：server 在主桌面0 条件等待的最长窗口（默认 18000ms，\n"
                             "足以等 Qt backdrop 出来 + 多 stagger 2s 的 scrcpy 卡片；app 卡片特别多可调到 25000）。\n"
                             "同时作为 poller 扫尾和 move_after 的时间窗口上限。")
    p_boot.add_argument("--server-min-hwnds", type=int, default=2,
                        help="switch_wait_switchback 模式 server 提前离开的条件：至少出现几个顶层 HWND（默认=2，\n"
                             "对应 Qt notification_panel + thinking_log；app 卡片多时调到 3~5）。")
    p_boot.add_argument("--model", help="传给 server.py 的 --model 参数（如 autoglm-phone）")
    p_boot.add_argument("--host", help="传给 server.py 的 --host 参数")
    p_boot.add_argument("--port", type=int, help="传给 server.py 的 --port 参数")
    p_boot.add_argument("--device-id", help="传给 server.py 的 --device-id 参数")
    p_boot.add_argument("--phoneaddress", default=None,
                        help="启动 server 前若无 ADB 有线(USB)设备，则对该地址（必须含端口）"
                             "执行 `adb connect`，如 --phoneaddress 192.168.1.100:5555；"
                             "该参数不会传给 server.py")
    p_boot.add_argument("--no-watcher", action="store_true",
                        help="不启动常驻归位 watcher（动态新开的 scrcpy 卡片/uu-open 后托盘点开的 UU 将无人归位）")

    # uu-open（toolbar 电脑钮触发）：按需创建 UU 桌面 + 启动/唤醒 UU 远程；已存在则只切桌面
    p_uu = sub.add_parser(
        "uu-open",
        help="按需打开 UU 远程桌面：UU 桌面不存在→创建并启动 UU；桌面在但 UU 已退出→重新启动；\n"
             "桌面与 UU 都在→只切换桌面（不重复创建/启动）",
    )
    p_uu.add_argument("--uu-remote-path",
                      help=f"UU远程 GameViewer / 向日葵 / ToDesk 的 exe 完整路径；\n"
                           f"（默认自动探测，第 1 候选 = D:\\UUdesk\\GameViewer\\GameViewer.exe，共 {len(_UU_REMOTE_CANDIDATES)} 条常见路径）")
    p_uu.add_argument("--uu-remote-args", default="",
                      help="传给远程客户端的额外参数（如向日葵的 -silent）")
    p_uu.add_argument("--uu-wait-ms", type=int, default=10000,
                      help="启动 UU 后条件等待窗口出现的最长时间（默认 10000ms）")
    p_uu.add_argument("--uu-min-hwnds", type=int, default=1,
                      help="提前结束等待的条件：出现 >= 多少个顶层 HWND（默认=1）")
    p_uu.add_argument("--force-new-uu", action="store_true",
                      help="即使检测到 UU 远程已在运行，也强制重新 Popen（默认复用旧实例）")
    p_uu.add_argument("--no-uu-dock", action="store_true",
                      help="不在 UU 桌面启动回程 Dock（front/uu_dock.py：电脑按钮切回手机桌面）")

    # watcher（内部命令，由 bootstrap 以 detached 方式自启，也可手动运行排障）
    p_watch = sub.add_parser("watch", help=argparse.SUPPRESS)
    p_watch.add_argument("--state-file", default=None)
    p_watch.add_argument("--interval", type=float, default=1.5)

    # signal-pid（内部命令：stop 的一次性 CTRL_C 信号助手）
    p_sig = sub.add_parser("signal-pid", help=argparse.SUPPRESS)
    p_sig.add_argument("--pid", type=int, required=True)

    # stop：全量停止——默认连 UU 主程序、UU 虚拟桌面一起清理，做到零项目进程残留
    p_stop = sub.add_parser(
        "stop",
        help="全量停止：CTRL_C 优雅停 server（超时强杀）+ 两个 toolbar + watcher + "
             "全部 scrcpy；默认同时关闭 UU 主程序并删除 UU 虚拟桌面；无 state 也清扫孤儿",
    )
    p_stop.set_defaults(kill_uu=True, remove_desktops=True)
    p_stop.add_argument("--server-pid", type=int, default=None,
                        help="无 .vd_state.json 时直接指定 server.py 的 PID")
    p_stop.add_argument("--kill-uu", action="store_true",
                        help="（默认行为，保留仅为兼容）关闭 UU 远程主程序整棵进程树")
    p_stop.add_argument("--no-kill-uu", dest="kill_uu", action="store_false",
                        help="保留 UU 远程主程序（默认 stop 会关闭它）")
    p_stop.add_argument("--remove-desktops", action="store_true",
                        help="（默认行为，保留仅为兼容）删除 state 记录的 UU 桌面")
    p_stop.add_argument("--no-remove-desktops", dest="remove_desktops", action="store_false",
                        help="保留 UU 虚拟桌面（默认 stop 会删除它；手机桌面永不删）")
    p_stop.add_argument("--keep-state", action="store_true",
                        help="停止后保留 .vd_state.json（默认删除）")

    sub.add_parser("status", help="查看 bootstrap 会话（server/watcher/UU 存活状态与桌面编号）")

    args = parser.parse_args()
    # 虚拟桌面相关命令都需要 vd；bootstrap/watch/stop/status 在各自 do_* 里实例化
    if args.cmd == "bootstrap":
        try:
            return do_bootstrap(args)
        except (FileNotFoundError, ValueError) as e:
            print(f"[bootstrap] ❌ {e}")
            return 1
    if args.cmd == "uu-open":
        try:
            return do_uu_open(args)
        except (FileNotFoundError, ValueError) as e:
            print(f"[uu-open] ❌ {e}")
            return 1
    if args.cmd == "watch":
        return do_watch(args)
    if args.cmd == "signal-pid":
        return do_signal_pid(args)
    if args.cmd == "stop":
        return do_stop(args)
    if args.cmd == "status":
        return do_status(args)

    vd = VirtualDesktops()

    if args.cmd == "ensure":
        vd.ensure(count=args.count)
        print()
        print(vd.info())
    elif args.cmd == "switch":
        if not vd.switch(args.index):
            return 1
    elif args.cmd == "list":
        print(vd.info())
    elif args.cmd == "create":
        vd.create()
    elif args.cmd == "remove":
        if not vd.remove(args.index):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
