"""异环：确保游戏已离开标题页（2026-10-29）。

事故背景：ok-nte 的 LauncherTask 只在游戏进程**不存在**时才走
“官方启动器 → 开始游戏 → 进入游戏”全流程；如果玩家把游戏留在标题页
（底部有「进入游戏」按钮），ok-nte 会直接切到游戏窗口开始识别，
结果每个日常任务都跑满 10 分钟然后报 `主界面检测失败`。
2026-10-29 10:24 / 11:48 两次运行都是这个状态（失败截图
`ok-nte/.../screenshots/2026-10-29_11-59-31-627_...daily_anomaly_original.png`
就是标题页）。

本模块在启动 ok-nte 之后补一步兜底：
- 游戏窗口不存在 → 直接放行（交给 ok-nte 自己启动并进入）；
- 窗口存在但定位不到「进入游戏」→ 视为已在主界面/加载中，放行；
- 定位到按钮 → 点击并等按钮消失；一直没有消失则诚实失败，
  避免再白跑 45 分钟。

不改变已有流程：只有 apps.NevernessToEverness.post_launch_ensure_main == "nte"
时才会被调用（默认值在 settings/defaults.py）。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Tuple

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

GAME_PROCESS = "HTGame.exe"
ENTRY_TEMPLATE = str(TEMPLATES_DIR / "异环-进入游戏.png")
ENTRY_TEXTS = ("进入游戏",)
MATCH_SCALES = (1.0, 1.25, 0.8, 1.5, 0.67)
MATCH_CONFIDENCES = (0.92, 0.88, 0.84, 0.8)
OCR_MIN_CONFIDENCE = 0.6
# 「进入游戏」在画面底部；OCR 只扫下 45%，避免加载提示/公告里的同名词
ENTRY_REGION_TOP_FRAC = 0.55


def _stop(stop_check: Optional[Callable[[], bool]]) -> bool:
    return bool(stop_check and stop_check())


def find_game_window() -> int:
    """返回 HTGame.exe 的可见窗口 HWND；找不到返回 0。"""
    try:
        from .foreground_flow_runner import _find_window_by_filters

        found = _find_window_by_filters(process_names=[GAME_PROCESS], launched_after=0.0)
        return int(found[0]) if found else 0
    except Exception as exc:
        LOGGER.debug("[异环] 查找游戏窗口失败（按未运行处理）: %s", exc)
        return 0


def locate_entry(logger: logging.Logger, hwnd: int) -> Tuple[Optional[Tuple[int, int]], str]:
    """定位标题页的「进入游戏」：模板优先，OCR 兜底。返回 (窗口局部坐标, 方式)。"""
    try:
        from .foreground_flow_runner import _locate_template_in_window

        center = _locate_template_in_window(hwnd, ENTRY_TEMPLATE, MATCH_SCALES, MATCH_CONFIDENCES)
        if center:
            return center, "template"
    except Exception as exc:
        logger.debug("[异环] 「进入游戏」模板匹配异常: %s", exc)

    region = None
    try:
        from .foreground_flow_runner import _window_rect

        rect = _window_rect(hwnd)
        if rect:
            left, top, width, height = rect
            offset = int(height * ENTRY_REGION_TOP_FRAC)
            region = (left, top + offset, width, height - offset)
    except Exception:
        region = None

    try:
        from .foreground_flow_runner import _ocr_locate_in_window

        center = _ocr_locate_in_window(
            logger=logger,
            hwnd=hwnd,
            target_text=list(ENTRY_TEXTS),
            min_confidence=OCR_MIN_CONFIDENCE,
            search_region=region,
        )
        if center:
            return center, "ocr"
    except Exception as exc:
        logger.debug("[异环] 「进入游戏」OCR 定位异常: %s", exc)
    return None, ""


def ensure_game_main(
    logger: logging.Logger,
    stop_check: Optional[Callable[[], bool]] = None,
    timeout_sec: float = 90.0,
    click_wait_sec: float = 15.0,
) -> Tuple[bool, str]:
    """确保游戏不在「进入游戏」标题页。返回 (是否可继续, 说明)。"""
    hwnd = find_game_window()
    if not hwnd:
        return True, "游戏未运行，交给 ok-nte 启动并进入"

    logger.info("[异环] 检测到游戏窗口 HWND=%s，检查是否停留在「进入游戏」界面", hwnd)
    clicked = False
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        if _stop(stop_check):
            return False, "用户已请求停止"
        center, method = locate_entry(logger, hwnd)
        if not center:
            if clicked:
                logger.info("[异环] 「进入游戏」按钮已消失，游戏开始加载")
                return True, "已点击进入游戏"
            return True, "未检测到「进入游戏」按钮（可能已在主界面或加载中）"

        clicked = True
        try:
            from .foreground_flow_runner import _click_window_local, _focus_window

            _focus_window(hwnd)
            _click_window_local(hwnd, center[0], center[1], clicks=1)
        except Exception as exc:
            return False, f"点击「进入游戏」失败: {exc}"
        logger.info("[异环] 已点击「进入游戏」 @ %s [方式: %s]", center, method)

        wait_end = time.time() + max(1.0, click_wait_sec)
        while time.time() < wait_end:
            if _stop(stop_check):
                return False, "用户已请求停止"
            if not locate_entry(logger, hwnd)[0]:
                return True, "已点击进入游戏，按钮消失"
            time.sleep(0.8)
        logger.warning("[异环] 「进入游戏」按钮仍在，重试点击")

    return False, "停留在「进入游戏」界面，未能进入游戏（ok-nte 会全程识别失败，已提前终止）"
