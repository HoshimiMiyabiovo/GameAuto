"""进入原神主界面（2026-10-24，方法复刻自 BetterGI 0.66 的 GameLoading 任务）。

由 GenshinLauncher 流程的 action=genshin_enter_main 驱动。**默认不接线**：
apps.GenshinImpact 不配 prelaunch_flow，一条龙仍由 BetterGI 自己启动游戏；
要启用时在 app_config.json 的 apps.GenshinImpact 加 "prelaunch_flow": "GenshinLauncher"。

复刻的进入方法（BetterGI GameTask/GameLoading 的 Recognition.json 实证）：
直接启动 YuanShen.exe（不走米哈游启动器）→ 循环检测「点击进入」（下半屏中 1/3
ROI）与「进入游戏」（中央带 ROI）→ 点击 → 空月祝福弹窗点「领取」→ 等主界面特征
（左上角派蒙菜单图标 paimon_menu，BetterGI 全任务共用的主界面判定）连续 2 次命中。

模板复制自 BetterGI 安装目录（本地自用）：
- enter_game.png（104x29，「点击进入」白字）
- choose_enter_game.png（103x31，「进入游戏」金字）
- paimon_menu.png（38x40，主界面左上角派蒙图标）
- welkin_moon_logo.png（78x69，空月祝福弹窗标记）
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional, Tuple

import psutil

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

GAME_PROCESS = "YuanShen.exe"

MAIN_TEMPLATE = str(TEMPLATES_DIR / "原神-主界面.png")
ENTRY_TEMPLATE = str(TEMPLATES_DIR / "原神-点击进入.png")
CHOOSE_TEMPLATE = str(TEMPLATES_DIR / "原神-进入游戏.png")
WELKIN_TEMPLATE = str(TEMPLATES_DIR / "原神-空月祝福.png")

# BetterGI Recognition.json 的 ROI（比例系数，相对窗口）
MAIN_REGION = (0.0, 0.0, 0.30, 0.30)        # 派蒙图标只在左上角
ENTRY_REGION = (1 / 3, 1 / 2, 1 / 3, 1 / 2)  # rect(cw/3, ch/2, cw/3, ch/2)
CHOOSE_REGION = (0.30, 0.48, 0.40, 0.22)     # rect(685,595,549,64)@1920x1080 外扩一圈
WELKIN_REGION = (0.0, 0.5, 1.0, 0.5)         # rect(0, ch/2, cw, ch/2)

ENTRY_OCR_TEXTS = ("点击进入", "进入游戏")
CONFIRM_TEXTS = ("确认", "同意")
WELKIN_CLAIM_TEXTS = ("领取",)


def _stop(stop_check: Optional[Callable[[], bool]]) -> bool:
    return bool(stop_check and stop_check())


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl
    return impl(hwnd)


def _focus(hwnd: int) -> None:
    from .foreground_flow_runner import _focus_window
    _focus_window(hwnd)


def _hwnd_pid(hwnd: int) -> int:
    import win32process
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return 0


def _process_name(pid: int) -> str:
    try:
        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return ""


def _window_alive(hwnd: int) -> bool:
    import win32gui
    try:
        return bool(hwnd) and bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def _ocr(logger, hwnd: int, region=None):
    from .ocr_click_tester import ocr_window_content
    try:
        return ocr_window_content(logger, hwnd=hwnd, region=region) or []
    except Exception:
        logger.warning("[原神] OCR 失败", exc_info=True)
        return []


def _pick(items, texts, min_conf: float = 0.4, region=None):
    best = None
    for item in items:
        text = str(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        if region:
            x, y = item.get("center", (0, 0))
            left, top, width, height = region
            if not (left <= x <= left + width and top <= y <= top + height):
                continue
        for cand in texts:
            if cand in text:
                if best is None or conf > best[0]:
                    best = (conf, cand, item)
                break
    return best


def _click(center) -> None:
    from .foreground_flow_runner import _click_screen
    _click_screen(int(center[0]), int(center[1]))


def _subregion(rect, fx, fy, fw, fh):
    """窗口 rect 的比例子区域（BetterGI 的 rect(cw*a, ch*b, ...) 写法）。"""
    left, top, width, height = rect
    return (
        left + int(width * fx),
        top + int(height * fy),
        max(1, int(width * fw)),
        max(1, int(height * fh)),
    )


def _match_center(hwnd: int, template: str, scales, confidences, region=None):
    """在窗口（或其子区域）匹配模板，返回屏幕坐标中心或 None。"""
    from .foreground_flow_runner import _locate_template_on_screen

    rect = _window_rect(hwnd)
    if not rect:
        return None
    if region is not None:
        rect = _subregion(rect, *region)
    try:
        return _locate_template_on_screen(template, rect, scales, confidences)
    except Exception:
        return None


def _record_failure(logger, step_name: str, message: str, hwnd: int) -> None:
    try:
        from .flow_diagnostics import record_failure
        from .ocr_click_tester import ocr_window_content
        ocr_results = ocr_window_content(logger, hwnd=hwnd) if hwnd else None
        record_failure(
            logger,
            flow_key="GenshinLauncher",
            step=step_name,
            error=message,
            hwnd=hwnd,
            ocr_results=ocr_results,
            extra={"kind": "genshin_enter_main"},
        )
    except Exception as exc:
        logger.debug("[原神启动器] 记录失败诊断异常 %s: %s", step_name, exc)


def _claim_welkin(logger, hwnd: int, scales, confidences) -> None:
    """空月祝福弹窗：优先 OCR 点「领取」，兜底点弹窗下方中央。"""
    items = _ocr(logger, hwnd)
    hit = _pick(items, WELKIN_CLAIM_TEXTS, min_conf=0.5)
    if hit:
        _click(hit[2]["center"])
        logger.info("[原神] 空月祝福「%s」已点击 @ %s", hit[2].get("text"), hit[2]["center"])
        return
    rect = _window_rect(hwnd)
    if rect:
        left, top, width, height = rect
        _click((left + width // 2, top + int(height * 0.8)))
        logger.info("[原神] 空月祝福弹窗已按比例点击")


def enter_main_interface(
    logger,
    hwnd: int,
    step: dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str, Optional[int], Optional[int]]:
    """从 YuanShen 游戏窗口进入主界面。返回 (ok, message, game_hwnd, game_pid)。

    游戏本体由 flow 的 exe_path 直接启动（与 BetterGI 一致），本函数只负责
    登录页点击、空月祝福领取与主界面等待；已在主界面时首次命中即快速通过。
    """
    for tpl in (MAIN_TEMPLATE, ENTRY_TEMPLATE, CHOOSE_TEMPLATE):
        if not os.path.isfile(tpl):
            return False, f"模板不存在: {tpl}", None, None

    pid = _hwnd_pid(hwnd)
    process_name = _process_name(pid)
    if process_name != GAME_PROCESS.lower():
        return False, f"窗口进程不是 {GAME_PROCESS}（当前: {process_name or '?'}）", hwnd, pid

    timeout = int(step.get("login_timeout_sec", 600))
    end = time.time() + max(1, timeout)
    main_hits = 0
    last_ocr = 0.0
    while time.time() < end:
        if _stop(stop_check):
            return False, "用户请求停止", hwnd, pid
        if not _window_alive(hwnd):
            return False, "游戏窗口已关闭", hwnd, pid
        _focus(hwnd)

        # 1) 主界面特征连续 2 次命中（派蒙图标只在主界面左上角显示）
        if _match_center(hwnd, MAIN_TEMPLATE, scales, confidences, region=MAIN_REGION):
            main_hits += 1
            if main_hits >= 2:
                return True, "已进入主界面", hwnd, pid
            time.sleep(1.0)
            continue
        main_hits = 0

        # 2) 「点击进入」/「进入游戏」模板命中 → 点击（各自 BetterGI ROI 内匹配）
        for tpl in (ENTRY_TEMPLATE, CHOOSE_TEMPLATE):
            region = ENTRY_REGION if tpl == ENTRY_TEMPLATE else CHOOSE_REGION
            center = _match_center(hwnd, tpl, scales, confidences, region=region)
            if center:
                _click(center)
                logger.info("[原神] 入口模板已点击 @ %s", center)
                time.sleep(2.0)
                break
        else:
            # 3) 空月祝福弹窗 → 领取
            if _match_center(hwnd, WELKIN_TEMPLATE, scales, confidences, region=WELKIN_REGION):
                _claim_welkin(logger, hwnd, scales, confidences)
                time.sleep(2.0)
                continue

            # 4) OCR 兜底（3s 节流）：入口文字 / 确认
            if time.time() - last_ocr >= 3:
                last_ocr = time.time()
                items = _ocr(logger, hwnd)
                rect = _window_rect(hwnd)
                hit = None
                if rect:
                    hit = _pick(items, ENTRY_OCR_TEXTS, min_conf=0.5,
                                region=_subregion(rect, 1 / 3, 1 / 2, 2 / 3, 1 / 2))
                if hit:
                    _click(hit[2]["center"])
                    logger.info("[原神] OCR 点击「%s」@ %s", hit[2].get("text"), hit[2]["center"])
                    time.sleep(2.0)
                    continue
                hit = _pick(items, CONFIRM_TEXTS, min_conf=0.6)
                if hit:
                    _click(hit[2]["center"])
                    logger.info("[原神] 弹窗「%s」已点击 @ %s", hit[2].get("text"), hit[2]["center"])
                    time.sleep(1.0)
                    continue
        time.sleep(1.0)

    message = "等待主界面超时"
    _record_failure(logger, str(step.get("name") or "进入原神主界面"), message, hwnd)
    return False, message, hwnd, pid
