# -*- coding: utf-8 -*-
"""
===========================================================================
  Curved Surface Compositor Preview (方案 A · 电脑端整帧 Warp LUT 合成 POC)
===========================================================================

  目标：在「电脑这一侧」就把整屏像素按柱面 Warp LUT 做实时弯曲重采样，
       最终输出给 AR 眼镜的只需要是"已经弯好的一帧"，眼镜纯面板点亮即可。

  这完全等价于 AR 眼镜固件里的 Warp Engine 做的事（LUT 公式完全一样），
  只是这里用 Python + Numpy 在 CPU 端跑，用于快速验证视觉/延迟/弯曲参数。
  产品级做法见脚本底部的「迁移到 GPU 路径」章节。

  用法：
      python front\\curved_compositor_preview.py
      python front\\curved_compositor_preview.py --w 1600 --h 900
      python front\\curved_compositor_preview.py --sbs           # 双目 SBS 左右分屏（模拟 AR 眼镜双目输出）
      python front\\curved_compositor_preview.py --deg 70        # 初始水平弯曲最大 70°

  键盘（焦点在预览窗口内时）：
      = / +  : 增大弯曲度（max_deg +5°）
      - / _  : 减小弯曲度（max_deg -5°）
      [ / ]  : 减小/增大 源图可视范围（scale_k 0.05 步进）
      9 / 0  : 减小/增大 纵向翘曲（lift_k）
      S      : 切换 单目 / 双目 SBS 左右分屏
      R      : 恢复默认参数
      Q / Esc: 退出

  依赖（当前项目 requirements 已覆盖前两项）：
      Pillow>=12       ✅（已在 requirements.txt 里）
      numpy            ✅（scrcpy_client 依赖 opencv，自然带了 numpy）
      mss（可选）       pip install mss   → 抓屏速度从 ~30ms 降到 ~8ms
===========================================================================
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

# 注意：之前用的 mss 已卸载（GDI BitBlt 对硬件加速表面会花屏/失败/延迟高，不再作为主路径）
# 现在临时抓屏仅保留 PIL.ImageGrab 兜底，后续会切换到"虚拟显示器 + D3D11/WGC 共享纹理"。
_HAS_MSS = False

from PIL import Image, ImageGrab, ImageTk

import tkinter as tk


# ═══════════════════════════════════════════════════════════════════════════
#  Warp 数学核心：反向柱面 + 双线性采样
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WarpParams:
    max_deg: float = 65.0        # 水平最大弯曲角度（±max_deg），越大弯得越明显
    scale_k: float = 1.15        # 源图可视范围缩放（>1 → 看源图更多内容；<1 → 只看到中心部分）
    lift_k: float = 0.25         # 纵向翘曲系数（cosθ 补偿）。0=完全不翘，0.25~0.4 比较自然
    sbs: bool = False            # True=输出双目 SBS 左右分屏（单目宽=输出宽//2，两眼看同一张弯好的图）
    interp: str = "nearest"      # "nearest"（默认，3~5× 更快/足够看效果） | "bilinear"（高质量）
    # 内部几何（由输出尺寸决定）；默认 1440x810 非全屏，避免抓屏时盖住自己导致 mss BitBlt 失败
    out_w: int = 1440
    out_h: int = 810


def _build_warp_maps(p: WarpParams) -> Tuple[np.ndarray, np.ndarray]:
    """生成 src_x_map[y, x], src_y_map[y, x]：每个输出像素对应在源图里的浮点采样坐标。

    注意：因为是"反向 Warp"（输出像素 → 找源像素的位置），和 AR 眼镜固件 Warp LUT
    的逻辑 100% 一致——这样做不会出现空洞（每个输出像素一定能采到值）。
    """
    ow, oh = p.out_w, p.out_h
    max_rad = math.radians(max(5.0, min(170.0, p.max_deg)))
    scale_k = float(max(0.5, min(3.0, p.scale_k)))
    lift_k = float(max(0.0, min(1.0, p.lift_k)))

    # 输出坐标归一化到 [-1, +1] × [-1, +1]（注意 y 屏幕朝下，观察空间 y 朝上）
    xs = np.linspace(-1.0, 1.0, ow, dtype=np.float32)
    ys = np.linspace(1.0, -1.0, oh, dtype=np.float32)
    u, v = np.meshgrid(xs, ys)  # shape: (oh, ow)

    # 柱面展开：u 是归一化视角 → θ = u * max_rad
    theta = u * max_rad  # (oh, ow)
    cos_t = np.cos(theta)
    sin_t = np.sin(theta)

    # ───── 透视投影到 z=1 平面，回到平面投影坐标 proj_u, proj_v ─────
    # 柱面点 P = (sinθ, y_v / lift_k? 不，直接：)
    # 简化但视觉上正确的柱面投影：
    #   proj_u = sinθ / cosθ = tanθ  自然越远越扩散
    #   proj_v = v / cosθ            边缘 cosθ 小 → proj_v 放大 → 画面自然"两侧向外拉翘"，
    #                                再乘 (1-lift_k) 可控是否补偿
    proj_u = sin_t / np.clip(cos_t, 1e-3, None)  # tanθ
    # lift_k=0 → 完全按 cos 补偿翘曲；lift_k=1 → 完全不补偿，边缘竖向被强拉伸
    v_div = cos_t * (1.0 - lift_k) + 1.0 * lift_k
    proj_v = v / np.clip(v_div, 1e-3, None)

    # proj_u / proj_v ∈ ???：理论上当 θ→±90° proj_u→±∞，我们按 scale_k 归一化到 [-1,1]
    # 实际 70° 时 tan(70°)≈2.75；我们以 "tan(max_rad)" 作为归一化分母（scale_k 会再乘一次）
    denom_u = max(math.tan(max_rad), 1e-3) * scale_k
    denom_v = 1.0 / max(math.cos(max_rad) * (1 - lift_k) + 1.0 * lift_k, 1e-3)
    denom_v = max(denom_v, 1.0) * scale_k

    # → 把 proj_u/proj_v 重新归一化到 [-1, +1]（对应源图完整宽高）
    src_u = proj_u / max(denom_u, 1e-3)
    src_v = proj_v / max(denom_v, 1e-3)

    # → 映射到源图像素坐标（源图尺寸我们先不知道；先给一个相对 0..1 坐标，采样时再乘）
    # 但是 src_x_map / src_y_map 要返回像素坐标，所以先输出 [0,1] 相对，由采样器乘源图尺寸。
    # 为了少一次乘法，我们在这里输出 [-1,+1]；调用方拿到后做 (x+1)/2 * W-1 即可。
    return src_u.astype(np.float32), src_v.astype(np.float32)


def _apply_warp(
    src_rgb: np.ndarray,
    rel_u: np.ndarray,
    rel_v: np.ndarray,
    out_w: int,
    out_h: int,
    sbs: bool,
    interp: str = "nearest",
) -> np.ndarray:
    """把一张 H×W×3 的源图，按 rel_u/rel_v（[-1,1]）弯成 out_h × out_w × 3 的输出图。

    interp="nearest"（默认）：只做整数取整，速度比双线性快 3~5×，对"看效果"完全够用。
    interp="bilinear"：4 点加权插值，质量高但 CPU 更重。
    """
    H, W = src_rgb.shape[:2]
    if H <= 1 or W <= 1:
        return np.zeros((out_h, out_w, 3), dtype=np.uint8)

    # rel_u/rel_v 先 clamp 到 [-2,+2] 防止极端值溢出（后续再判断是否在 [0,W-1] 内）
    u = np.clip(rel_u, -2.0, 2.0)
    v = np.clip(rel_v, -2.0, 2.0)
    sx = (u + 1.0) * 0.5 * (W - 1)  # (oh, ow)
    sy = (v + 1.0) * 0.5 * (H - 1)

    # 越界：sx/sy 落在源图外 → 填 0（黑边代表虚拟曲面外部）
    valid = (sx >= 0.0) & (sx <= (W - 1)) & (sy >= 0.0) & (sy <= (H - 1))  # (oh, ow)

    if interp == "bilinear":
        # 双线性采样（4 点加权）
        x0 = np.floor(sx).astype(np.int32)
        y0 = np.floor(sy).astype(np.int32)
        x1 = x0 + 1
        y1 = y0 + 1
        ax = (sx - x0).astype(np.float32)[..., None]   # (oh, ow, 1)
        ay = (sy - y0).astype(np.float32)[..., None]
        x0c = np.clip(x0, 0, W - 1)
        x1c = np.clip(x1, 0, W - 1)
        y0c = np.clip(y0, 0, H - 1)
        y1c = np.clip(y1, 0, H - 1)
        Ia = src_rgb[y0c, x0c, :]
        Ib = src_rgb[y0c, x1c, :]
        Ic = src_rgb[y1c, x0c, :]
        Id = src_rgb[y1c, x1c, :]
        top = Ia * (1 - ax) + Ib * ax
        bot = Ic * (1 - ax) + Id * ax
        out = top * (1 - ay) + bot * ay
        out = np.where(valid[..., None], out, 0.0)
        out8 = np.clip(out, 0.0, 255.0).astype(np.uint8)
    else:
        # nearest：round 后直接索引（最快）
        xi = np.clip(np.round(sx).astype(np.int32), 0, W - 1)
        yi = np.clip(np.round(sy).astype(np.int32), 0, H - 1)
        out8 = src_rgb[yi, xi, :].copy()
        out8[~valid, :] = 0

    if sbs:
        # 双目 SBS：左右两半各放一张相同的弯好的图（真正 AR 眼镜会在这里加 IPD 偏移 + 视差）
        half_w = out_w // 2
        if out8.shape[1] != half_w:
            out8_small = np.array(
                Image.fromarray(out8).resize((half_w, out_h), Image.NEAREST if interp != "bilinear" else Image.BILINEAR),
                dtype=np.uint8,
            )
            sbs_frame = np.zeros((out_h, half_w * 2, 3), dtype=np.uint8)
            sbs_frame[:, :half_w, :] = out8_small
            sbs_frame[:, half_w:, :] = out8_small
            return sbs_frame
        return out8
    return out8


# ═══════════════════════════════════════════════════════════════════════════
#  抓屏：mss 优先，退化到 PIL.ImageGrab
# ═══════════════════════════════════════════════════════════════════════════

class ScreenGrabber:
    """抓屏兜底：目前只用 PIL.ImageGrab（GDI 级别，可能慢/花屏/抓不到硬件加速表面）。
    后续"虚拟显示器合成"主链路会替换成：
        - 要么 Windows Graphics Capture API（D3D11 共享纹理，不走 GDI）
        - 要么 DXGI Desktop Duplication（同一会话抓整张桌面的共享 SwapChain）
    届时 grab_rgb 直接返回 D3D11 Texture 的 CPU 读回 / 或根本不读回，直接 GPU→GPU warp。
    """

    def __init__(self):
        pass  # 不再持有 mss 实例

    def _fallback_grab(self) -> np.ndarray:
        img = ImageGrab.grab(all_screens=True)
        return np.array(img.convert("RGB"), dtype=np.uint8)

    def grab_rgb(self, monitor_idx: int = 0) -> np.ndarray:
        # monitor_idx 参数先保留给未来"指定虚拟显示器编号时"做单独裁剪；
        # 目前 ImageGrab 抓所有屏合并帧，等接 WGC/DXGI 时再分屏。
        _ = monitor_idx
        return self._fallback_grab()


# ═══════════════════════════════════════════════════════════════════════════
#  Tkinter 全屏预览窗口 + 键盘调参
# ═══════════════════════════════════════════════════════════════════════════

class PreviewApp:
    def __init__(self, args: argparse.Namespace):
        interp = "bilinear" if args.bilinear else "nearest"
        self.params = WarpParams(
            max_deg=args.deg,
            scale_k=args.scale,
            lift_k=args.lift,
            sbs=args.sbs,
            out_w=args.w,
            out_h=args.h,
            interp=interp,
        )
        self._last_maps_key: Optional[Tuple] = None
        self._map_u: Optional[np.ndarray] = None
        self._map_v: Optional[np.ndarray] = None
        self._grabber = ScreenGrabber()
        self._warned_tick_error_once = False

        self._root = tk.Tk()
        self._root.title("Curved Compositor Preview - Q/Esc 退出")
        # 无边框 + TopMost，但默认尺寸 1440x810（非全屏），避免盖满全屏导致抓屏循环失败
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        # 居中到屏幕，尺寸按 out_w x out_h
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = max(0, (sw - self.params.out_w) // 2)
        y = max(0, (sh - self.params.out_h) // 2)
        self._root.geometry(f"{self.params.out_w}x{self.params.out_h}+{x}+{y}")

        self._canvas = tk.Label(self._root, bd=0, bg="black")
        self._canvas.pack(fill="both", expand=True)
        self._photo: Optional[ImageTk.PhotoImage] = None

        # 键盘事件
        self._root.bind("<KeyPress>", self._on_key)
        self._root.bind("<Escape>", lambda e: self._quit())
        self._root.focus_set()

        self._frame = 0
        self._t0 = time.time()

        # 启动提示
        print(
            "══════════════════════════════════════════════════════════\n"
            "  Curved Compositor Preview 已启动\n"
            f"    输出尺寸：{self.params.out_w}x{self.params.out_h}   "
            f"SBS={self.params.sbs}   采样={self.params.interp}   "
            f"抓屏库={'mss（失败自动退化 PIL）' if _HAS_MSS else 'PIL.ImageGrab'}\n"
            "  键盘：\n"
            "    = / +   增大弯曲度（+5°）         - / _   减小弯曲度（-5°）\n"
            "    ]  增大源图可视范围（+0.05）       [  减小源图可视范围（-0.05）\n"
            "    0  增大纵向翘曲 lift（+0.05）      9  减小纵向翘曲 lift（-0.05）\n"
            "    S  切换 单目 / 双目 SBS 左右分屏\n"
            "    B  切换 最近邻(默认/快) / 双线性(慢/高质量)\n"
            "    R  恢复默认参数        Q / Esc  退出\n"
            "══════════════════════════════════════════════════════════",
            flush=True,
        )

    # ──────────── 键盘 ────────────
    def _on_key(self, ev: tk.Event):
        k = ev.keysym
        changed = True
        p = self.params
        if k in ("plus", "equal", "KP_Add"):
            p.max_deg = min(150.0, p.max_deg + 5.0)
        elif k in ("minus", "KP_Subtract", "underscore"):
            p.max_deg = max(10.0, p.max_deg - 5.0)
        elif k == "bracketright":
            p.scale_k = round(min(2.5, p.scale_k + 0.05), 2)
        elif k == "bracketleft":
            p.scale_k = round(max(0.7, p.scale_k - 0.05), 2)
        elif k == "0":
            p.lift_k = round(min(0.6, p.lift_k + 0.05), 2)
        elif k == "9":
            p.lift_k = round(max(0.0, p.lift_k - 0.05), 2)
        elif k.lower() == "s":
            p.sbs = not p.sbs
        elif k.lower() == "b":
            p.interp = "bilinear" if p.interp != "bilinear" else "nearest"
        elif k.lower() == "r":
            p.max_deg, p.scale_k, p.lift_k = 65.0, 1.15, 0.25
            p.interp = "nearest"
        elif k.lower() == "q":
            self._quit()
        else:
            changed = False
        if changed:
            print(
                f"  ⚙ 参数变更：max_deg={p.max_deg:5.1f}°  "
                f"scale_k={p.scale_k:.2f}  lift_k={p.lift_k:.2f}  "
                f"SBS={p.sbs}  采样={p.interp}",
                flush=True,
            )
            # 下次 tick 会重建 LUT

    def _quit(self):
        try:
            self._root.destroy()
        finally:
            sys.exit(0)

    # ──────────── 主循环 tick ────────────
    def _rebuild_maps_if_needed(self):
        p = self.params
        key = (p.max_deg, p.scale_k, p.lift_k, p.sbs, p.out_w, p.out_h, p.interp)
        if key != self._last_maps_key:
            # SBS 时：单目宽度 = out_w // 2（左右各一），LUT 按单目尺寸算，之后再在采样后拼接
            if p.sbs:
                w_one = max(320, p.out_w // 2)
                sub_p = WarpParams(
                    max_deg=p.max_deg, scale_k=p.scale_k, lift_k=p.lift_k,
                    sbs=False, out_w=w_one, out_h=p.out_h, interp=p.interp,
                )
                self._map_u, self._map_v = _build_warp_maps(sub_p)
            else:
                self._map_u, self._map_v = _build_warp_maps(p)
            self._last_maps_key = key

    def tick(self):
        t_grab = 0.0
        t_warp = 0.0
        t_show = 0.0
        try:
            # 1) 抓屏
            t = time.perf_counter()
            src = self._grabber.grab_rgb(0)
            t_grab = time.perf_counter() - t

            # 2) Warp（LUT 缓存不变，参数变就重建）
            t = time.perf_counter()
            self._rebuild_maps_if_needed()
            p = self.params
            out_warped = _apply_warp(
                src, self._map_u, self._map_v,
                out_w=(p.out_w // 2 if p.sbs else p.out_w),
                out_h=p.out_h,
                sbs=p.sbs,
                interp=p.interp,
            )
            # 强制按 out_w × out_h 对齐（SBS 可能产生 2*half_w 不是 out_w 的情况四舍五入）
            if out_warped.shape[1] != p.out_w or out_warped.shape[0] != p.out_h:
                resample = Image.BILINEAR if p.interp == "bilinear" else Image.NEAREST
                img = Image.fromarray(out_warped).resize((p.out_w, p.out_h), resample)
                out_warped = np.array(img, dtype=np.uint8)
            t_warp = time.perf_counter() - t

            # 3) 显示
            t = time.perf_counter()
            pil = Image.fromarray(out_warped, mode="RGB")
            self._photo = ImageTk.PhotoImage(image=pil)
            self._canvas.config(image=self._photo)  # type: ignore[call-overload]
            t_show = time.perf_counter() - t

            # 每 30 帧打一次性能统计
            self._frame += 1
            if self._frame % 30 == 0:
                fps = 30.0 / max(1e-6, time.time() - self._t0)
                self._t0 = time.time()
                print(
                    f"  📊 FPS={fps:5.1f}   "
                    f"抓屏={t_grab*1000:5.1f}ms   "
                    f"Warp={t_warp*1000:5.1f}ms   "
                    f"显示={t_show*1000:5.1f}ms   "
                    f"源图 {src.shape[1]}x{src.shape[0]}",
                    flush=True,
                )
        except KeyboardInterrupt:
            self._quit()
            return
        except Exception as e:  # pragma: no cover - runtime safety
            if not self._warned_tick_error_once:
                self._warned_tick_error_once = True
                print(f"  ⚠  渲染 tick 异常（之后不再重复打印）：{type(e).__name__}: {e}", flush=True)

        # 下一帧：~30fps 足够 POC（想更高帧率就把 33 改 16）
        self._root.after(33, self.tick)

    def run(self):
        self._root.after(20, self.tick)
        self._root.mainloop()


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="Curved Surface Compositor Preview (CPU/Numpy 整帧 Warp LUT POC)")
    ap.add_argument("--w", type=int, default=1440, help="预览窗口宽度（默认 1440，非全屏避免抓屏盖住自己失败）")
    ap.add_argument("--h", type=int, default=810, help="预览窗口高度（默认 810）")
    ap.add_argument("--deg", type=float, default=65.0, help="初始水平最大弯曲角度（默认 65°，推荐 45~85）")
    ap.add_argument("--scale", type=float, default=1.15, help="初始源图可视范围缩放（默认 1.15，越大弯后能看到越多源图内容，黑边越小）")
    ap.add_argument("--lift", type=float, default=0.25, help="初始纵向翘曲补偿（默认 0.25，0=强翘，0.5=平）")
    ap.add_argument("--sbs", action="store_true", help="启动时直接用双目 SBS 左右分屏输出（模拟 AR 眼镜双目）")
    ap.add_argument("--fullhd", action="store_true", help="等价于 --w 1920 --h 1080（可能导致 mss BitBlt 失败，会自动退化 PIL）")
    ap.add_argument("--nearest", action="store_true", help="采样用最近邻（默认，3~5× 更快/足够看效果）")
    ap.add_argument("--fast", action="store_true", help="等价于 --nearest")
    ap.add_argument("--bilinear", action="store_true", help="采样用双线性（质量高但更慢）")
    ns = ap.parse_args(argv)
    if ns.fullhd:
        ns.w, ns.h = 1920, 1080
    # 采样默认 nearest；显式 --bilinear 时使用
    return ns


if __name__ == "__main__":
    if not sys.platform.startswith("win"):
        print("⚠  当前 POC 只保证 Windows 可用（Magnification API / Win 全屏窗口）。", file=sys.stderr)
    try:
        PreviewApp(_parse_args(sys.argv[1:])).run()
    except KeyboardInterrupt:
        pass
