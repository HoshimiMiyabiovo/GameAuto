"""1920x1080 基准坐标 → 当前窗口屏幕坐标（分辨率无关）。

与 `zzz_monthly_shop._frac_point` / `monthly_exchange._region_frac` 同一口径：
坐标一律按窗口 rect 换算，不写死像素。本模块额外服务于「以 1920x1080 参考图
定标」的场景——实机探针（data/artifacts/*_stage*.py）、参考图标注、一次性
调试脚本常直接写参考图上的像素，经这里换算后换 4K / 改渲染倍率都不会点空。

- ``screen(win, x1080, y1080)``：1080p 基准的硬编码数字 → 当前屏幕坐标。
- ``offset(win, dx, dy)``：相对微调（如 ±40px）→ 当前窗口下的像素偏移。
- ``min_confirm_y(win)``：弹窗底部确认按钮的屏幕 y 下界，替代写死的 ``y>700``。

**别拿它换算 OCR 返回的 center**：`ocr_click_tester._ocr_scan` 的 center 已经是
屏幕绝对坐标（box 才是截图局部坐标），再缩放会二次换算点飞。OCR 坐标直接喂
点击函数，只有硬编码常量走 screen()。窗口宽高独立缩放，16:9 / 16:10 / 任意
客户区尺寸都成立；游戏内部渲染倍率（NVIDIA DSR / 渲染精度）不改变桌面合成后
的窗口坐标系，无需额外适配。
"""

from __future__ import annotations

BASE_W = 1920.0
BASE_H = 1080.0

# 「确认 / 购买」按钮在弹窗底部，标题栏同名文字在上方；1080p 基准阈值 700。
# 绝区零弹窗标题「兑换确认」@y≈316、异环标题「购买」@y≈280，底部按钮 @y≈770/800。
CONFIRM_MIN_Y_RATIO = 700.0 / BASE_H  # 0.648


def rect_size(win):
    """返回窗口 (left, top, width, height)；win 可为 dict（带 rect）或四元组。"""
    rect = win["rect"] if isinstance(win, dict) else win
    left, top, right, bottom = rect
    return int(left), int(top), max(1, int(right) - int(left)), max(1, int(bottom) - int(top))


def scale(win):
    """返回 (sx, sy)：1080p 基准像素 → 当前窗口像素的缩放系数。"""
    _, _, width, height = rect_size(win)
    return width / BASE_W, height / BASE_H


def screen(win, x1080, y1080):
    """1080p 基准的绝对坐标 → 当前屏幕绝对坐标。"""
    left, top, width, height = rect_size(win)
    return (int(round(left + float(x1080) * width / BASE_W)),
            int(round(top + float(y1080) * height / BASE_H)))


def offset(win, dx1080, dy1080):
    """1080p 基准的相对偏移 → 当前窗口下的像素偏移（OCR 命中点附近微调用）。"""
    sx, sy = scale(win)
    return int(round(float(dx1080) * sx)), int(round(float(dy1080) * sy))


def min_confirm_y(win):
    """当前窗口下「底部确认按钮」的屏幕 y 下界（OCR center 是屏幕坐标）。"""
    _left, top, _width, height = rect_size(win)
    return top + height * CONFIRM_MIN_Y_RATIO


def y_ratio(y1080):
    """1080p 基准 y → 窗口高度比例，用于 region_frac 之类的配置。"""
    return float(y1080) / BASE_H
