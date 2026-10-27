"""进入无限暖暖主界面（2026-10-22）。

由 NikkiLauncher 流程的 action=nikki_enter_game 驱动：
叠纸启动器「启动游戏/更新/运行中」→ 游戏登录页「点击进入游戏/确认/登录」
→ 加载与月卡/道具弹窗 → 左下角聊天气泡（主界面特征）连续出现。

主界面模板来自 whimbox 2.5.6 的 IconPageMainFeature 二值化裁剪
（GPL-3.0，本地自用）；启动器/登录页靠 RapidOCR，不依赖固定坐标。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional, Tuple

import psutil

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

GAME_PROCESS = "X6Game-Win64-Shipping.exe"
LAUNCHER_PROCESSES = {"xstarter.exe", "launcher_main.exe", "launcher.exe"}
MAIN_TEMPLATE = str(TEMPLATES_DIR / "暖暖-主界面.png")

LOGIN_HINT_TEXTS = ("点击进入游戏", "占土进入游戏", "点击进入")
CONFIRM_TEXTS = ("确认", "同意")
LOGIN_TEXTS = ("登录", "注册\\登录")


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
        logger.warning("[暖暖] OCR 失败", exc_info=True)
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


def _press_esc() -> None:
    import pyautogui
    from .foreground_flow_runner import FailSafeAborted
    try:
        pyautogui.press("escape")
    except pyautogui.FailSafeException as exc:
        raise FailSafeAborted(
            "PyAutoGUI 紧急停止已触发：鼠标位于屏幕左上角 (0,0)。"
        ) from exc


def _launcher_button(logger, hwnd: int):
    """返回 (类型, OCR条目)；类型为 启动游戏/进入游戏/启动/更新/运行中。

    2026-10-23 避坑：叠纸启动器主按钮文案是「进入游戏」（不含"启动"），
    备用按钮是「使用DX11启动（游戏异常时选择）」。若只匹配"启动"子串会点到
    DX11 备用按钮、游戏根本起不来。因此：主按钮文案「进入游戏」单独入表；
    通用"启动"命中时排除含 DX11 的文本。
    """
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    region = (
        left + int(width * 0.62),
        top + int(height * 0.72),
        int(width * 0.36),
        int(height * 0.26),
    )
    items = _ocr(logger, hwnd, region=region)
    for item in items:
        text = str(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.4:
            continue
        for kind in ("启动游戏", "进入游戏", "启动", "更新", "运行中"):
            if kind == "启动" and "DX11" in text.upper():
                # 「使用DX11启动」是游戏异常时的备用按钮，不是主启动按钮
                continue
            if kind in text:
                return kind, item
    return None


def _find_game_process():
    from .foreground_flow_runner import _find_process_by_exact_name
    return _find_process_by_exact_name(GAME_PROCESS)


def _wait_game_window(step, stop_check):
    from .foreground_flow_runner import _wait_window_by_pid

    timeout = int(step.get("game_window_timeout_sec", 300))
    end = time.time() + max(1, timeout)
    while time.time() < end:
        if _stop(stop_check):
            return None, None
        proc = _find_game_process()
        if proc:
            remain = max(5, int(end - time.time()))
            hwnd = _wait_window_by_pid(proc.pid, timeout=min(60, remain), stop_check=stop_check)
            if hwnd:
                return hwnd, proc.pid
        time.sleep(1.0)
    return None, None


def _wait_update_done(logger, hwnd: int, step, stop_check):
    timeout = int(step.get("update_timeout_sec", 3600))
    end = time.time() + max(1, timeout)
    last_log = 0.0
    while time.time() < end:
        if _stop(stop_check):
            return False, "用户请求停止"
        time.sleep(3.0)
        if not _window_alive(hwnd):
            if _find_game_process():
                return True, "更新完成，游戏已启动"
            return False, "启动器窗口已关闭"
        _focus(hwnd)
        found = _launcher_button(logger, hwnd)
        if found and found[0] in ("启动游戏", "进入游戏", "启动"):
            _click(found[1]["center"])
            logger.info("[暖暖] 更新完成，已点击启动游戏")
            return True, "更新完成并启动"
        if found and found[0] == "运行中" and _find_game_process():
            return True, "更新后游戏已自动启动"
        if time.time() - last_log > 60:
            logger.info("[暖暖] 等待游戏更新完成…")
            last_log = time.time()
    return False, "等待更新完成超时"


def _launch_game_from_launcher(logger, hwnd: int, step, stop_check):
    button_timeout = int(step.get("launcher_timeout_sec", 60))
    end = time.time() + max(1, button_timeout)
    while time.time() < end:
        if _stop(stop_check):
            return False, "用户请求停止"
        _focus(hwnd)
        found = _launcher_button(logger, hwnd)
        if not found:
            time.sleep(1.0)
            continue
        kind, item = found
        center = item.get("center")
        if kind == "运行中":
            if _find_game_process():
                return True, "游戏已在运行"
            time.sleep(5.0)
            continue
        if not center:
            time.sleep(1.0)
            continue
        _click(center)
        logger.info("[暖暖] 启动器按钮「%s」已点击 @ %s", kind, center)
        if kind == "更新":
            return _wait_update_done(logger, hwnd, step, stop_check)
        return True, f"已点击{kind}"
    return False, "未找到启动器按钮"


def _has_main(hwnd: int, scales, confidences) -> bool:
    from .foreground_flow_runner import _locate_template_on_screen

    rect = _window_rect(hwnd)
    if not rect:
        return False
    left, top, width, height = rect
    region = (
        left,
        top + int(height * 0.70),
        max(1, int(width * 0.30)),
        max(1, int(height * 0.30)),
    )
    try:
        return bool(_locate_template_on_screen(MAIN_TEMPLATE, region, scales, confidences))
    except Exception:
        return False


def _login_region(hwnd: int):
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    return (
        left + int(width * 0.15),
        top + int(height * 0.55),
        int(width * 0.70),
        int(height * 0.45),
    )


def _enter_login(logger, hwnd: int, scales, confidences, step, stop_check):
    """返回 (phase, message)，phase: main/loading/closed/stopped/timeout。"""
    timeout = int(step.get("login_timeout_sec", 180))
    end = time.time() + max(1, timeout)
    while time.time() < end:
        if _stop(stop_check):
            return "stopped", "用户请求停止"
        if not _window_alive(hwnd):
            return "closed", "游戏窗口已关闭"
        _focus(hwnd)
        if _has_main(hwnd, scales, confidences):
            return "main", "已在主界面"
        items = _ocr(logger, hwnd)
        whole = " ".join(str(item.get("text") or "") for item in items)
        hit = _pick(items, LOGIN_HINT_TEXTS, min_conf=0.35, region=_login_region(hwnd))
        if hit:
            _click(hit[2]["center"])
            logger.info("[暖暖] 点击「%s」@ %s", hit[2].get("text"), hit[2]["center"])
            return "loading", f"已点击{hit[2].get('text')}"
        hit = _pick(items, CONFIRM_TEXTS, min_conf=0.45)
        if hit and not any(k in whole for k in ("退出游戏", "账号登出")):
            _click(hit[2]["center"])
            logger.info("[暖暖] 登录页弹窗「%s」已点击", hit[2].get("text"))
            time.sleep(1.0)
            continue
        hit = _pick(items, LOGIN_TEXTS, min_conf=0.45)
        if hit:
            _click(hit[2]["center"])
            logger.info("[暖暖] 登录按钮「%s」已点击", hit[2].get("text"))
            time.sleep(1.0)
            continue
        _press_esc()
        time.sleep(1.0)
    return "timeout", "等待登录页超时"


def _wait_main(logger, hwnd: int, scales, confidences, step, stop_check):
    """返回 (phase, message)，phase: ok/closed/stopped/timeout。"""
    timeout = int(step.get("loading_timeout_sec", 240))
    end = time.time() + max(1, timeout)
    start = time.time()
    hits = 0
    last_ocr = 0.0
    while time.time() < end:
        if _stop(stop_check):
            return "stopped", "用户请求停止"
        if not _window_alive(hwnd):
            return "closed", "游戏窗口已关闭（可能资源更新后重启）"
        _focus(hwnd)
        if _has_main(hwnd, scales, confidences):
            hits += 1
            if hits >= 2:
                return "ok", "已进入主界面"
            time.sleep(1.0)
            continue
        hits = 0
        rect = _window_rect(hwnd)
        if rect:
            left, top, width, height = rect
            # 底部中央点掉小月卡/道具过期弹窗；加载早期再点顶部中央推进提示
            _click((left + width // 2, top + int(height * 0.83)))
            if time.time() - start < 60:
                _click((left + width // 2, top + int(height * 0.09)))
        if time.time() - last_ocr >= 3:
            items = _ocr(logger, hwnd)
            hit = _pick(items, LOGIN_HINT_TEXTS, min_conf=0.35)
            if hit:
                _click(hit[2]["center"])
                logger.info("[暖暖] 加载中回到登录页，已重新点击「%s」", hit[2].get("text"))
            else:
                hit = _pick(items, CONFIRM_TEXTS, min_conf=0.45)
                if hit:
                    _click(hit[2]["center"])
                    logger.info("[暖暖] 异常弹窗「%s」已点击", hit[2].get("text"))
            last_ocr = time.time()
        time.sleep(1.0)
    return "timeout", "等待主界面超时"


def _record_failure(logger, step_name: str, message: str, hwnd: int) -> None:
    try:
        from .flow_diagnostics import record_failure
        from .ocr_click_tester import ocr_window_content
        ocr_results = ocr_window_content(logger, hwnd=hwnd) if hwnd else None
        record_failure(
            logger,
            flow_key="NikkiLauncher",
            step=step_name,
            error=message,
            hwnd=hwnd,
            ocr_results=ocr_results,
            extra={"kind": "nikki_enter_game"},
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
    """从启动器窗口或游戏窗口进入主界面。返回 (ok, message, game_hwnd, game_pid)。"""
    if not os.path.isfile(MAIN_TEMPLATE):
        return False, f"主界面模板不存在: {MAIN_TEMPLATE}", None, None

    pid = _hwnd_pid(hwnd)
    process_name = _process_name(pid)
    if process_name == GAME_PROCESS.lower():
        game_hwnd, game_pid = hwnd, pid
    else:
        logger.info("[暖暖] 当前窗口进程=%s，按启动器处理", process_name or "?")
        ok, message = _launch_game_from_launcher(logger, hwnd, step, stop_check)
        if not ok:
            return False, message, None, None
        game_hwnd, game_pid = _wait_game_window(step, stop_check)
        if not game_hwnd:
            return False, "游戏窗口未出现", None, None
    logger.info("[暖暖] 游戏窗口就绪: HWND=%s PID=%s", game_hwnd, game_pid)

    for _ in range(2):
        if _stop(stop_check):
            return False, "用户请求停止", game_hwnd, game_pid
        _focus(game_hwnd)
        phase, message = _enter_login(logger, game_hwnd, scales, confidences, step, stop_check)
        if phase == "main":
            return True, message, game_hwnd, game_pid
        if phase in ("stopped", "timeout"):
            _record_failure(logger, str(step.get("name") or "进入无限暖暖主界面"), message, game_hwnd)
            return False, message, game_hwnd, game_pid
        if phase == "closed":
            game_hwnd, game_pid = _wait_game_window(step, stop_check)
            if not game_hwnd:
                return False, "游戏重启后窗口未出现", None, None
            continue

        phase, message = _wait_main(logger, game_hwnd, scales, confidences, step, stop_check)
        if phase == "ok":
            return True, message, game_hwnd, game_pid
        if phase == "stopped":
            return False, message, game_hwnd, game_pid
        if phase == "closed":
            game_hwnd, game_pid = _wait_game_window(step, stop_check)
            if not game_hwnd:
                return False, "游戏重启后窗口未出现", None, None
            continue
        _record_failure(logger, str(step.get("name") or "进入无限暖暖主界面"), message, game_hwnd)
        return False, message, game_hwnd, game_pid

    return False, "多次重试仍未进入主界面", game_hwnd, game_pid
