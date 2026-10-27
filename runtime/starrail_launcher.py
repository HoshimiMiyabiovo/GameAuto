"""进入崩铁主界面（2026-10-24，方法复刻自 March7th Assistant v2.2.0.3）。

由 StarRailLauncher 流程的 action=starrail_enter_main 驱动。**默认不接线**：
apps.HonkaiStarRail 不配 prelaunch_flow，一条龙仍由 March7th 自己启动游戏；
要启用时在 app_config.json 的 apps.HonkaiStarRail 加 "prelaunch_flow": "StarRailLauncher"。

复刻的进入方法（依据 March7th 本地日志 2026-10-19 / 10-23 实证）：
直接启动 StarRail.exe（不走官方启动器）→ 循环检测「点击进入」屏幕
（click_enter 模板，实测命中 0.98+，加载界面 ≤0.31）→ 点击 → 等主界面特征
（main 模板，实测主界面 0.96-1.00、其他界面 ≤0.55）连续 2 次命中。
途中顺带点掉 开始游戏/月卡/欢迎/消息 弹窗；OCR 见「前往启动器」字样时
快速失败（客户端未更新，继续等只会超时）。

模板复制自 March7th Assistant assets/images/screen（本地自用）：
- click_enter.png（101x27，「点击进入」文字）
- main.png（32x29，主界面特征图标）
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional, Tuple

import psutil

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

GAME_PROCESS = "StarRail.exe"

MAIN_TEMPLATE = str(TEMPLATES_DIR / "崩铁-主界面.png")
ENTRY_TEMPLATE = str(TEMPLATES_DIR / "崩铁-点击进入.png")
# 可选点击模板：匹配到才点，不出现跳过（顺序即尝试顺序）
POPUP_TEMPLATES = (
    ("开始游戏", str(TEMPLATES_DIR / "崩铁-开始游戏.png")),
    ("月卡奖励", str(TEMPLATES_DIR / "崩铁-月卡.png")),
    ("欢迎弹窗", str(TEMPLATES_DIR / "崩铁-欢迎.png")),
    ("消息通知", str(TEMPLATES_DIR / "崩铁-消息通知.png")),
)

# OCR 兜底只认「点击进入」——不能用"进入游戏"：加载页问候语
# 「156*****68，欢迎进入游戏」含"进入游戏"，会点到问候语上
ENTRY_OCR_TEXTS = ("点击进入",)
CONFIRM_TEXTS = ("确认", "同意")
# 游戏提示该文案 = 客户端版本落后，等下去只有超时
LAUNCHER_REQUIRED_TEXT = "前往启动器"


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
        logger.warning("[星铁] OCR 失败", exc_info=True)
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


def _match_center(hwnd: int, template: str, scales, confidences):
    """在游戏窗口区域匹配模板，返回屏幕坐标中心或 None。"""
    from .foreground_flow_runner import _locate_template_on_screen

    rect = _window_rect(hwnd)
    if not rect:
        return None
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
            flow_key="StarRailLauncher",
            step=step_name,
            error=message,
            hwnd=hwnd,
            ocr_results=ocr_results,
            extra={"kind": "starrail_enter_main"},
        )
    except Exception:
        pass


def enter_main_interface(
    logger,
    hwnd: int,
    step: dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str, Optional[int], Optional[int]]:
    """从 StarRail 游戏窗口进入主界面。返回 (ok, message, game_hwnd, game_pid)。

    游戏本体由 flow 的 exe_path 直接启动（与三月七一致），本函数只负责
    登录页点击与主界面等待；游戏已在主界面时首次 main 命中即快速通过。
    """
    for tpl in (MAIN_TEMPLATE, ENTRY_TEMPLATE):
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

        # 1) 主界面特征连续 2 次命中（与三月七一致，防加载动画/过渡帧误判）
        if _match_center(hwnd, MAIN_TEMPLATE, scales, confidences):
            main_hits += 1
            if main_hits >= 2:
                return True, "已进入主界面", hwnd, pid
            time.sleep(1.0)
            continue
        main_hits = 0

        # 2) 「点击进入」模板命中 → 点击
        center = _match_center(hwnd, ENTRY_TEMPLATE, scales, confidences)
        if center:
            _click(center)
            logger.info("[星铁] 点击「点击进入」@ %s", center)
            time.sleep(2.0)
            continue

        # 3) 可选弹窗模板 → 点掉
        popup_hit = False
        for label, tpl in POPUP_TEMPLATES:
            if not os.path.isfile(tpl):
                continue
            center = _match_center(hwnd, tpl, scales, confidences)
            if center:
                _click(center)
                logger.info("[星铁] 弹窗「%s」已点击 @ %s", label, center)
                popup_hit = True
                time.sleep(2.0)
                break
        if popup_hit:
            continue

        # 4) OCR 兜底（3s 节流）：点击进入 / 确认；见到「前往启动器」快速失败
        if time.time() - last_ocr >= 3:
            last_ocr = time.time()
            items = _ocr(logger, hwnd)
            whole = " ".join(str(item.get("text") or "") for item in items)
            if LAUNCHER_REQUIRED_TEXT in whole:
                message = "客户端未更新：游戏提示「前往启动器下载最新客户端」"
                _record_failure(logger, str(step.get("name") or "进入崩铁主界面"), message, hwnd)
                return False, message, hwnd, pid
            hit = _pick(items, ENTRY_OCR_TEXTS, min_conf=0.5)
            if hit:
                _click(hit[2]["center"])
                logger.info("[星铁] OCR 点击「%s」@ %s", hit[2].get("text"), hit[2]["center"])
                time.sleep(2.0)
                continue
            hit = _pick(items, CONFIRM_TEXTS, min_conf=0.6)
            if hit:
                _click(hit[2]["center"])
                logger.info("[星铁] 弹窗「%s」已点击 @ %s", hit[2].get("text"), hit[2]["center"])
                time.sleep(1.0)
                continue
        time.sleep(1.0)

    message = "等待主界面超时"
    _record_failure(logger, str(step.get("name") or "进入崩铁主界面"), message, hwnd)
    return False, message, hwnd, pid
