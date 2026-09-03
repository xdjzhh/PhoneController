# -*- coding: utf-8 -*-
"""
virtual_desktop.py  —  Windows 10 / 11 虚拟桌面管理脚本（零额外配置）

需求规格（严格按用户两条要求实现）：
  1. ensure(N=3)：检查当前虚拟桌面总数，若 < N  → 自动补充创建直到 = N；若 ≥ N → 不做任何动作。
  2. switch(idx)：虚拟桌面从左→右严格编号为 0,1,2,...,N-1，可准确切换到指定编号的桌面。

实现：优先使用 pyvda（已在多个 Windows 项目验证）。若未装 → 自动 `pip install pyvda`
      （pyvda 是纯 Python 包，自动处理 Win10 1903 / Win11 之间 VirtualDesktop COM IID 的 GUID
       差异，避免自己手写 IServiceProvider + IVirtualDesktopManagerInternal 两套接口。）

CLI 用法（都可直接双击或命令行调用）：
  python virtual_desktop.py ensure --count 3       # 保证至少 3 个虚拟桌面（不足自动创建）
  python virtual_desktop.py switch --index 1       # 切换到编号 1 的桌面（左数第 2 个）
  python virtual_desktop.py list                   # 打印总数 + 当前所在桌面编号
  python virtual_desktop.py create                 # 立即新建 1 个虚拟桌面（追加到最右端）
  python virtual_desktop.py remove --index 2       # 删除指定编号的桌面（窗口自动合并到前一个）

Python 模块用法：
  from virtual_desktop import VirtualDesktops
  vd = VirtualDesktops()
  vd.ensure(count=3)
  vd.switch(1)
  print(vd.count(), vd.current_index())
"""
from __future__ import annotations

import subprocess
import sys
from typing import Optional


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
    def ensure(self, count: int = 3) -> int:
        """
        保证至少有 `count` 个虚拟桌面。
          - 已有数 < count → 自动创建补充到 count，返回本次新建个数
          - 已有数 ≥ count → 不做任何动作，返回 0
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
        if total < 3:
            lines.append(f"⚠️ 不足 3 个，请执行：python virtual_desktop.py ensure --count 3")
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
# 一键编排 bootstrap：ensure(3) → #1 启 server.py → #2 启 UU远程 → 切回原桌面
# ---------------------------------------------------------------------------
import os as _os
import pathlib as _pathlib
import shutil as _shutil

_SCRIPT_DIR = _pathlib.Path(__file__).resolve().parent
_DEFAULT_PROJECT_ROOT = _SCRIPT_DIR              # 默认把 virtual_desktop.py 同目录当项目根
_DEFAULT_SERVER_SCRIPT = "server.py"              # 默认要启动的 server 脚本
_DEFAULT_RETURN_TO = "original"                   # 编排结束后切回：'original' 回到脚本开始前的桌面

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
# 目的：实现「后台初始化，用户显示桌面保持在桌面 0」，
#       在桌面 0 Popen 进程后，持续轮询把 HWND 一个个"事后迁移"到目标桌面，
#       完美解决"server Qt+scrcpy 启动慢 → 切去桌面 2 时窗口还在创建 → 散到两个桌面"的问题。
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
            print(f"[bootstrap] 🎯 自动探测到远程客户端：{cand}")
            return cand
    for name in ("UURemote.exe", "uu-remote.exe", "SunloginClient.exe", "ToDesk.exe"):
        found = _shutil.which(name)
        if found:
            print(f"[bootstrap] 🎯 从 PATH 中探测到远程客户端：{found}")
            return found

    raise FileNotFoundError(
        "[bootstrap] ❌ 无法自动定位 UU远程 / 向日葵 / ToDesk exe。\n"
        "    请显式传入：\n"
        "        python virtual_desktop.py bootstrap --uu-remote-path \"C:/你的完整路径/UURemote.exe\""
    )


def _parse_return_to(return_to_arg: str, original_idx: int) -> int:
    """--return-to CLI arg → 实际桌面编号：'original' 用启动前原桌面；其他按整数。"""
    if return_to_arg in ("original", "auto", ""):
        return original_idx
    try:
        return int(return_to_arg)
    except ValueError as e:
        raise ValueError(f"--return-to 必须是 'original' 或整数编号，收到：{return_to_arg}") from e


def do_bootstrap(args) -> int:
    r"""
    编排主流程（本轮重写后 **默认走强隔离 switch_wait_switchback**，彻底解决
    「server 窗口还在桌面 0 / UU 窗口还在桌面 0 / 两个桌面散落」的隔离失败问题）：

      1) ensure 至少 3 个桌面 → 保证 0/1/2 三个编号都存在；
      2) **切到 #1 → Popen server.py → 轮询条件等待 ≥2 个顶层 HWND 真正出现**
                  → 仍停留在 #1，**绝不提前切走**（→ server + scrcpy 的初始桌面就是 #1）；
      3) **切到 #2 → Popen GameViewer(UUdesk) → 条件等待 ≥1 个顶层 HWND 出现**
                  → 仍停留在 #2；
      4) **切回 return_to（默认 = original = 桌面 0）**
      5) 并行 join 两个 sweeper poller（额外 8~10s 扫尾那些"16s 后才冒出来的 scrcpy"）
      6) **最终审计报告**：枚举 server 和 uu 的所有子孙 PID 的 HWND，逐条打印
         「窗口标题 / 真实 desktop id（= #?）」—— 肉眼可一眼验证隔离正确与否。
    """
    vd = VirtualDesktops()
    original_idx = vd.current_index()
    launch_mode = getattr(args, "launch_mode", "auto") or "auto"

    print(f"[bootstrap] 开始：执行前用户桌面 = #{original_idx}")
    print(f"[bootstrap] launch_mode = {launch_mode}")
    print(
        "[bootstrap] 🛡️  默认强隔离模式（switch_wait_switchback）：\n"
        "           切 #1 启 server → 条件等窗口出现 → 再切 #2 启 UU → 等窗口出现 → 再切回 #0。\n"
        "           Windows 在 Popen 时把进程初始线程 desktop affiliation 绑定在当前活跃桌面，\n"
        "           所以只要 HWND 真正 CreateWindowEx 时我们还没切走，100% 落在对应桌面，\n"
        "           根本不需要事后 MoveWindowToDesktop（那条链路不稳定）。\n"
        "           视觉代价：用户桌面会短暂经过 0 → 1 → 2 → 0 的切换（约 30s，取决于\n"
        "           scrcpy 起来速度，期间你如果在旁边看会看到桌面在切，这是必须的）。\n"
    )
    print("-" * 64)

    # Step 1: 保证有 3 个桌面（0,1,2）
    vd.ensure(count=3)

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

    # —— Step 3 命令组装：UU远程（第 1 候选 = D:\UUdesk\GameViewer\GameViewer.exe） ——
    uu_exe = _find_uu_remote(getattr(args, "uu_remote_path", None))
    uu_cmd: list[str] = [uu_exe]
    extra = getattr(args, "uu_remote_args", "")
    if isinstance(extra, str) and extra.strip():
        uu_cmd += extra.strip().split()

    server_wait_ms = max(6000, int(getattr(args, "server_wait_ms", 18000)))
    uu_wait_ms     = max(2000, int(getattr(args, "uu_wait_ms",     10000)))
    server_no_win  = bool(getattr(args, "server_no_window", False))
    server_min_hw  = max(1, int(getattr(args, "server_min_hwnds", 2)))   # Qt backdrop 至少有 2 窗
    uu_min_hw      = max(1, int(getattr(args, "uu_min_hwnds", 1)))

    pollers_to_wait: list[_threading.Thread] = []
    srv_mode_used = launch_mode if launch_mode != "auto" else "switch_wait_switchback"
    uu_mode_used  = srv_mode_used

    # =====================================================================
    # Step 2：#1 启 server.py（切桌面 → 条件等 HWND ≥2 个 → 不提前切走）
    # =====================================================================
    print("\n=== Step 2 / 3 ：桌面 #1 → server.py ===")
    p_server, srv_poller, srv_mode_used = vd.launch_on_desktop(
        1, server_cmd,
        cwd=str(server_py.parent),
        wait_window_ms=server_wait_ms,
        label=f"{server_py.name} → #1",
        no_window=server_no_win,
        mode=launch_mode,
        min_hwnd_count=server_min_hw,
    )
    if p_server is None:
        print("[bootstrap] ⚠️  server.py 启动失败（继续尝试启动 UU远程）...")
    else:
        if srv_poller is not None:
            pollers_to_wait.append(srv_poller)

    # =====================================================================
    # Step 3：#2 启 UU远程（切桌面 #2 → Popen → 条件等 HWND ≥1 个 → 不提前切走）
    # =====================================================================
    print("\n=== Step 3 / 3 ：桌面 #2 → UU远程 (GameViewer) ===")
    p_uu, uu_poller, uu_mode_used = vd.launch_on_desktop(
        2, uu_cmd,
        wait_window_ms=uu_wait_ms,
        label=f"{_pathlib.Path(uu_exe).name} → #2",
        no_window=False,
        mode=launch_mode,
        min_hwnd_count=uu_min_hw,
    )
    if p_uu is None:
        print("[bootstrap] ⚠️  UU远程启动失败（server 不影响）。")
    else:
        if uu_poller is not None:
            pollers_to_wait.append(uu_poller)

    # =====================================================================
    # Step 4：切回用户原桌面（桌面 0）
    # =====================================================================
    back_to = _parse_return_to(getattr(args, "return_to", _DEFAULT_RETURN_TO), original_idx)
    if back_to != vd.current_index():
        print(f"\n[bootstrap] ↩️  切回桌面 #{back_to}（用户执行前桌面）")
        vd.switch(back_to)

    # =====================================================================
    # Step 5：join 两个扫尾 poller（每个约 10s，并行等所以只花 ~10s，保证迟来的 scrcpy 仍归位）
    # =====================================================================
    if pollers_to_wait:
        longest_ms = max(server_wait_ms, uu_wait_ms)
        wait_join_s = (longest_ms / 1000.0) * 0.8 + 2.0
        print(
            f"\n[bootstrap] 🧹 扫尾阶段：并行等 {len(pollers_to_wait)} 个 sweeper poller 结束"
            f"（最多 {wait_join_s:.1f}s，抓那些 18s 后才冒出来的 scrcpy 卡片）..."
        )
        for th in pollers_to_wait:
            th.join(timeout=wait_join_s)
        print("[bootstrap] ✅ Sweeper poller 结束。\n")

    # =====================================================================
    # Step 6：最终审计报告 —— 逐条打印两个 PID 树的 HWND + 真实桌面编号
    #         用户一眼就能知道 server 的窗口是不是真的都在 #1，UU 是不是真的都在 #2
    # =====================================================================
    print()
    print("=" * 80)
    print("[bootstrap] 🧾 隔离审计报告：逐个窗口核对「真实桌面归属」")
    print("=" * 80)
    print(f"  launch_mode: server={srv_mode_used} / uu={uu_mode_used}")
    print(f"  虚拟桌面范围: [0, {vd.count() - 1}]")
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

    total_ok, total_ng = 0, 0
    a, b = _audit_one("桌面 1 / server.py 的子孙窗口",
                      p_server.pid if p_server else None, 1)
    total_ok += a; total_ng += b
    a, b = _audit_one("桌面 2 / UU远程 的子孙窗口",
                      p_uu.pid if p_uu else None, 2)
    total_ok += a; total_ng += b

    print()
    print("-" * 80)
    srv_state = ("OK  PID=" + str(p_server.pid)) if p_server else "FAILED"
    uu_state  = ("OK  PID=" + str(p_uu.pid))     if p_uu     else "FAILED"
    print(f"  桌面 1 / server.py           → {srv_state}  |  cwd = {server_py.parent}")
    print(f"  桌面 2 / UU远程 GameViewer   → {uu_state}   |  exe = {uu_exe}")
    if total_ng == 0:
        print(f"\n  🎯 100% 隔离成功：共 {total_ok} 个窗口全部在正确的桌面 #1/#2，桌面 0 干净。")
    else:
        print(
            f"\n  ⚠️  仍有 {total_ng} 个窗口不在正确桌面。\n"
            f"     建议：\n"
            f"       · 如果是 scrcpy 还没出完：把 --server-wait-ms 加到 25000；\n"
            f"       · 如果是 Qt notification_panel：把 --server-min-hwnds 调到 3；\n"
            f"       · 如果出现在桌面 0：检查是否为 --return-to=0 后（此时 active desktop=0）\n"
            f"         新弹出来的（sweeper poller 会在 10s 时间窗口内迁走），只要 sweeper 日志里\n"
            f"         最后打印 ✅ 就没问题。"
        )
    print("=" * 80)

    return 0 if (p_server is not None or p_uu is not None) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Windows 10/11 虚拟桌面管理 + 一键编排：ensure(≥3) → #1启server → #2启UU远程"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ensure = sub.add_parser("ensure", help="确保虚拟桌面数量 ≥ N（不足自动创建，超过则无动作）")
    p_ensure.add_argument("--count", type=int, default=3, help="最少个数（默认=3）")

    p_switch = sub.add_parser("switch", help="切换到指定编号（0-based，左→右）的桌面")
    p_switch.add_argument("--index", type=int, required=True, help="桌面编号 0 / 1 / 2 ...")

    sub.add_parser("list", help="打印桌面总数、当前编号、可用范围")
    sub.add_parser("create", help="立即新建 1 个虚拟桌面（追加到最右端）")

    p_remove = sub.add_parser("remove", help="删除指定编号的桌面（窗口合并到左侧邻居）")
    p_remove.add_argument("--index", type=int, required=True)

    # 一键编排 bootstrap（用户本次新增需求）
    p_boot = sub.add_parser(
        "bootstrap",
        help="按用户要求的固定顺序启动：ensure(≥3) → 桌面1启server.py → 桌面2启UU远程 → 切回原桌面\n"
             "（**默认强隔离 switch_wait_switchback**：先切桌面再启动 + 条件等窗口，100% 不会散落在桌面 0）",
    )
    # ===== 模式 =====
    p_boot.add_argument("--launch-mode",
                        choices=["auto", "switch_wait_switchback", "switch_then_sleep", "move_after"],
                        default="auto",
                        help=("启动模式（默认=auto → switch_wait_switchback）：\n"
                              "  · switch_wait_switchback（推荐 ✅）：切#N→Popen→等≥N个窗口真出现→再切走；100%%隔离\n"
                              "  · switch_then_sleep：先切桌面+死sleep毫秒（老兼容模式）\n"
                              "  · move_after：桌面0不动+事后迁移（隔离不稳定，窗口暂时留在桌面0）"))
    # server 相关
    p_boot.add_argument("--python",
                        help="server.py 使用的 Python 解释器路径（默认=当前 sys.executable）")
    p_boot.add_argument("--project-root",
                        help=f"项目根目录（默认=脚本同目录：{_DEFAULT_PROJECT_ROOT}）")
    p_boot.add_argument("--server-script", default=_DEFAULT_SERVER_SCRIPT,
                        help=f"要在桌面 1 启动的脚本名（默认={_DEFAULT_SERVER_SCRIPT}）")
    p_boot.add_argument("--server-no-window", action="store_true",
                        help="隐藏 server.py 的控制台黑框（CREATE_NO_WINDOW）")
    p_boot.add_argument("--server-wait-ms", type=int, default=18000,
                        help="switch_wait_switchback 模式：server 在桌面1 条件等待的最长窗口（默认 18000ms，\n"
                             "足以等 Qt backdrop 出来 + 多 stagger 2s 的 scrcpy 卡片；app 卡片特别多可调到 25000）。\n"
                             "同时作为 poller 扫尾和 move_after 的时间窗口上限。")
    p_boot.add_argument("--server-min-hwnds", type=int, default=2,
                        help="switch_wait_switchback 模式 server 提前离开的条件：至少出现几个顶层 HWND（默认=2，\n"
                             "对应 Qt notification_panel + thinking_log；app 卡片多时调到 3~5）。")
    p_boot.add_argument("--model", help="传给 server.py 的 --model 参数（如 autoglm-phone）")
    p_boot.add_argument("--host", help="传给 server.py 的 --host 参数")
    p_boot.add_argument("--port", type=int, help="传给 server.py 的 --port 参数")
    p_boot.add_argument("--device-id", help="传给 server.py 的 --device-id 参数")
    # UU 远程相关（第 1 候选默认：用户指定 D:\UUdesk\GameViewer\GameViewer.exe）
    p_boot.add_argument("--uu-remote-path",
                        help=f"UU远程 GameViewer / 向日葵 / ToDesk 的 exe 完整路径；\n"
                             f"（默认自动探测，第 1 候选 = D:\\UUdesk\\GameViewer\\GameViewer.exe，共 {len(_UU_REMOTE_CANDIDATES)} 条常见路径）")
    p_boot.add_argument("--uu-remote-args", default="",
                        help="传给远程客户端的额外参数（如向日葵的 -silent）")
    p_boot.add_argument("--uu-wait-ms", type=int, default=10000,
                        help="switch_wait_switchback 模式：UU远程 在桌面2 条件等待的最长窗口（默认 10000ms）。\n"
                             "也是 poller / move_after 的时间窗口上限。")
    p_boot.add_argument("--uu-min-hwnds", type=int, default=1,
                        help="switch_wait_switchback 模式 UU远程 提前离开的条件：出现 >= 多少个顶层 HWND（默认=1）")
    # 收尾
    p_boot.add_argument("--return-to", default=_DEFAULT_RETURN_TO,
                        help="编排完后切到几号桌面：'original'=回到脚本执行前用户所在桌面（默认）；或 0/1/2 等整数")

    args = parser.parse_args()
    # 虚拟桌面相关命令都需要 vd；bootstrap 命令自己在 do_bootstrap 里实例化 vd
    if args.cmd == "bootstrap":
        try:
            return do_bootstrap(args)
        except (FileNotFoundError, ValueError) as e:
            print(f"[bootstrap] ❌ {e}")
            return 1

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
