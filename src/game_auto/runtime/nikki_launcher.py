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
# 副本/大世界「确认返回」弹窗特征文案（2026-11-08 用户截图实机指认：
# 标题「确认返回」、正文「是否要返回书外?」、按钮「取消/确认」）。
INSTANCE_CONFIRM_TEXTS = ("确认返回", "返回书外", "是否要返回")
# 门形退出图标模板（右上，11-09 20:19 取证帧二值化裁剪）；OCR 漏读 Esc 标签时靠它触发
INSTANCE_EXIT_TEMPLATE = str(TEMPLATES_DIR / "暖暖-副本退出图标.png")
INSTANCE_CONFIRM_BUTTON = "确认"

# 2026-11-21 实机：新版启动器（v1.3.3.1）开局弹「有新版本」模态框（立即更新/退出）。
# 底下「启动游戏」按钮还在但模态框挡住点不动，旧逻辑会当成已启动 → 300s 等不到游戏窗口。
# 只认实机文案，绝不点「退出」（点了启动器直接退，等于放弃本次任务）。
UPDATE_PROMPT_MARKERS = ("有新版本", "版本更新说明")
UPDATE_PROMPT_BUTTONS = ("立即更新",)
UPDATE_BUSY_MARKERS = ("下载中", "更新中", "校验中", "正在更新", "正在下载", "安装中", "解压中")
LAUNCHER_TITLE_KEYWORDS = ("无限暖暖", "Infinity Nikki", "InfinityNikki")

_last_prompt_warn = {"ts": 0.0, "hwnd": 0}


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
    """ESC 退回上一层：走带扫描码的真实按键，别用 pyautogui.press。

    2026-11-05 与 nikki_calendar._press_key 统一口径；瞬时虚拟键事件在游戏内
    UI 上不生效（绝区零实机逐项验证过）。
    """
    from .foreground_flow_runner import press_key_scancode

    press_key_scancode("esc")


def _compact(text) -> str:
    """去掉 OCR 文本里的空白（「立即更 新」也能命中）。"""
    return "".join(str(text or "").split())


def _confidence(item) -> float:
    try:
        return float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _update_prompt_state(items):
    """返回 (marker, button)：marker 为「有新版本」提示条目，button 为最近距离的「立即更新」。

    没有弹窗时返回 (None, None)。只做识别，不点击。
    """
    marker = None
    marker_conf = 0.0
    for item in items or []:
        text = _compact(item.get("text"))
        if not text:
            continue
        if _confidence(item) < 0.35:
            continue
        if any(key in text for key in UPDATE_PROMPT_MARKERS):
            conf = _confidence(item)
            if conf > marker_conf:
                marker, marker_conf = item, conf
    if marker is None:
        return None, None

    mx, my = marker.get("center") or (0, 0)
    button = None
    best_dist = None
    for item in items or []:
        if _compact(item.get("text")) not in UPDATE_PROMPT_BUTTONS:
            continue
        if _confidence(item) < 0.45:
            continue
        center = item.get("center")
        if not center:
            continue
        # 弹窗里「立即更新」在标题下方；限定几何范围，避免误点页面别处（广告/
        # 公告里）同名的「立即更新」。
        dx = center[0] - mx
        dy = center[1] - my
        if dy < 0 or abs(dx) > 400 or dy > 600:
            continue
        dist = abs(dx) + abs(dy)
        if button is None or dist < best_dist:
            button, best_dist = item, dist
    # 「版本更新说明」也可能出现在公告/新闻面板里；只有「有新版本」是弹窗标题。
    # 只命中正文标题而没有同行「立即更新」时，不当成弹窗，免得挡住正常启动。
    if button is None and "有新版本" not in _compact(marker.get("text")):
        return None, None
    return marker, button


def _items_busy(items) -> bool:
    for item in items or []:
        text = _compact(item.get("text"))
        if not text or _confidence(item) < 0.4:
            continue
        if any(key in text for key in UPDATE_BUSY_MARKERS):
            return True
    return False


def _handle_update_prompt(logger, hwnd: int, step, stop_check=None):
    """识别并点掉启动器「有新版本」模态框的「立即更新」。

    返回：
      "clicked" —— 弹窗已处理（点击过，或点击后弹窗已消失）；
      "present" —— 弹窗在，但 OCR 没认出「立即更新」，调用方别去点底下的启动游戏；
      None      —— 当前帧没有更新弹窗。
    """
    try:
        items = _ocr(logger, hwnd) or []
    except Exception:  # noqa: BLE001 - 识别失败按"没弹窗"处理，不拖死启动
        logger.debug("[暖暖] 检查启动器更新弹窗失败", exc_info=True)
        return None
    marker, button = _update_prompt_state(items)
    if marker is None:
        return None
    if _items_busy(items):
        # 弹窗自己已经在显示「下载中/更新中」：更新已开始，别重复点「立即更新」
        return "clicked"

    if button is None:
        now = time.time()
        if now - _last_prompt_warn["ts"] > 30 or _last_prompt_warn["hwnd"] != hwnd:
            _last_prompt_warn["ts"] = now
            _last_prompt_warn["hwnd"] = hwnd
            logger.error(
                "[暖暖] 启动器弹出更新提示「%s」，但 OCR 没找到「立即更新」按钮；"
                "不会点「退出」，继续重试识别",
                marker.get("text"),
            )
        return "present"

    center = button.get("center")
    if not center:
        logger.error("[暖暖] 「立即更新」按钮没有中心坐标，无法点击")
        return "present"
    _click(center)
    logger.warning(
        "[暖暖] 启动器提示有新版本，已点「立即更新」@ %s（不点退出）", center
    )

    wait_sec = float(step.get("update_prompt_wait_sec", 30))
    end = time.time() + max(1.0, wait_sec)
    while time.time() < end:
        if _stop(stop_check):
            return "clicked"
        time.sleep(1.5)
        try:
            latest = _ocr(logger, hwnd) or []
        except Exception:  # noqa: BLE001
            return "clicked"
        if _update_prompt_state(latest)[0] is None:
            logger.info("[暖暖] 更新提示已关闭，等待更新完成")
            return "clicked"
    logger.warning(
        "[暖暖] 点「立即更新」后 %ss 弹窗仍在（可能在校验/下载），继续等待",
        int(wait_sec),
    )
    return "clicked"


def _launcher_busy(logger, hwnd: int) -> bool:
    """启动器是否还在下载/校验/安装（点过「立即更新」后防「启动游戏」提前恢复文案）。"""
    try:
        return _items_busy(_ocr(logger, hwnd) or [])
    except Exception:  # noqa: BLE001
        return False


def _find_launcher_window(logger):
    """按进程名/标题重新找一个启动器窗口（启动器自更新会重建 HWND）。"""
    from .foreground_flow_runner import _find_window_by_filters

    try:
        found = _find_window_by_filters(
            title_keywords=list(LAUNCHER_TITLE_KEYWORDS),
            process_names=sorted(LAUNCHER_PROCESSES),
            logger=logger,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[暖暖] 重新查找启动器窗口失败", exc_info=True)
        return None
    return found[0] if found else None


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


def _wait_update_done(logger, hwnd: int, step, stop_check, after_prompt: bool = False):
    """等更新完成并点「启动游戏」。

    2026-11-21：新版启动器弹「有新版本」模态框后点「立即更新」，启动器可能自我
    重启（旧 HWND 失效），这里按进程名重新接管；after_prompt=True 表示这次更新是
    弹窗触发的，要防「资源还在下载、主按钮已提前恢复成『启动游戏』」的假完成。
    """
    timeout = int(step.get("update_timeout_sec", 3600))
    end = time.time() + max(1, timeout)
    restart_timeout = float(step.get("launcher_restart_timeout_sec", 300))
    last_log = 0.0
    missing_since = None
    while time.time() < end:
        if _stop(stop_check):
            return False, "用户请求停止"
        time.sleep(3.0)
        if not _window_alive(hwnd):
            if _find_game_process():
                return True, "更新完成，游戏已启动"
            new_hwnd = _find_launcher_window(logger)
            if new_hwnd and new_hwnd != hwnd:
                logger.info(
                    "[暖暖] 启动器在更新中重建窗口，已重新接管: HWND=%s → %s",
                    hwnd, new_hwnd,
                )
                hwnd = new_hwnd
                missing_since = None
                continue
            if missing_since is None:
                missing_since = time.time()
            if time.time() - missing_since >= restart_timeout:
                return False, "启动器窗口已关闭且未重新出现"
            if time.time() - last_log > 30:
                logger.info("[暖暖] 启动器窗口暂不可见（更新/重启中），继续等待…")
                last_log = time.time()
            continue
        missing_since = None
        _focus(hwnd)
        prompt = _handle_update_prompt(logger, hwnd, step, stop_check)
        if prompt == "clicked":
            after_prompt = True
            last_log = time.time()
            continue
        if prompt == "present":
            continue
        found = _launcher_button(logger, hwnd)
        if found and found[0] in ("启动游戏", "进入游戏", "启动"):
            if after_prompt and _launcher_busy(logger, hwnd):
                if time.time() - last_log > 30:
                    logger.info("[暖暖] 更新资源仍在下载/校验，等主按钮恢复")
                    last_log = time.time()
                continue
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
    prompt_seen = False
    while time.time() < end:
        if _stop(stop_check):
            return False, "用户请求停止"
        if not _window_alive(hwnd):
            new_hwnd = _find_launcher_window(logger)
            if new_hwnd and new_hwnd != hwnd:
                logger.info("[暖暖] 启动器窗口已重建，重新接管: HWND=%s → %s", hwnd, new_hwnd)
                hwnd = new_hwnd
            else:
                time.sleep(2.0)
                continue
        _focus(hwnd)
        # 2026-11-21：新版启动器开局弹「有新版本」模态框，必须先点「立即更新」，
        # 否则底下的「启动游戏」点不动（点完还会误判成功）。
        prompt = _handle_update_prompt(logger, hwnd, step, stop_check)
        if prompt == "clicked":
            return _wait_update_done(logger, hwnd, step, stop_check, after_prompt=True)
        if prompt == "present":
            prompt_seen = True
            time.sleep(2.0)
            continue
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
    if prompt_seen:
        return False, "启动器仍停在「有新版本」弹窗（未识别到「立即更新」）"
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


def _instance_exit_icon(hwnd: int, scales, confidences) -> bool:
    from .foreground_flow_runner import _locate_template_on_screen

    rect = _window_rect(hwnd)
    if not rect:
        return False
    left, top, width, height = rect
    region = (
        left + int(width * 0.90),
        top,
        max(1, int(width * 0.10)),
        max(1, int(height * 0.10)),
    )
    try:
        return bool(_locate_template_on_screen(INSTANCE_EXIT_TEMPLATE, region, scales, confidences))
    except Exception:
        return False


def _pick_exact(items, text: str, min_conf: float = 0.45):
    """整行精确匹配（去空白）；「确认返回」标题含「确认」子串，子串匹配会点中标题。"""
    for item in items or []:
        if "".join(str(item.get("text") or "").split()) != text:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= min_conf and item.get("center"):
            return item
    return None


def _exit_instance_dialog(logger, hwnd: int) -> bool:
    """ESC 后等「确认返回」弹窗点「确认」；按钮精确匹配，标题也含「确认」不能点。"""
    _press_esc()
    end = time.time() + 8
    while time.time() < end:
        time.sleep(0.5)
        items = _ocr(logger, hwnd)
        if not any(t in str(i.get("text") or "") for i in items for t in INSTANCE_CONFIRM_TEXTS):
            continue
        btn = _pick_exact(items, INSTANCE_CONFIRM_BUTTON, min_conf=0.5)
        if btn:
            _click(btn["center"])
            logger.info("[暖暖] 副本退出：「确认返回」弹窗已点「确认」@ %s", btn["center"])
            return True
    logger.warning("[暖暖] 副本退出：按 ESC 后 8s 未见「确认返回」弹窗，继续等主界面")
    return False


def _wait_main(logger, hwnd: int, scales, confidences, step, stop_check):
    """返回 (phase, message)，phase: ok/closed/stopped/timeout。"""
    timeout = int(step.get("loading_timeout_sec", 240))
    end = time.time() + max(1, timeout)
    start = time.time()
    hits = 0
    last_ocr = 0.0
    last_exit = 0.0
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
        # 门形退出图标命中 → 完整退出序列；冷却 6s 防连按 ESC
        if rect and time.time() - last_exit >= 6 and _instance_exit_icon(hwnd, scales, confidences):
            last_exit = time.time()
            logger.info("[暖暖] 右上门形退出图标命中，执行 ESC → 确认返回 → 确认")
            _exit_instance_dialog(logger, hwnd)
            time.sleep(1.0)
            continue
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
                # 副本退出链路（11-08/11-09 实机）：右上门形图标/Esc 标签 → ESC →
                # 「确认返回/是否要返回书外?」→ 点「确认」→ 回主界面。
                esc_hit = None
                if rect:
                    esc_region = (left + int(width * 0.6), top, int(width * 0.4), int(height * 0.2))
                    esc_hit = _pick(items, ("Esc", "ESC", "esc"), min_conf=0.5, region=esc_region)
                confirm_dialog = _pick(items, INSTANCE_CONFIRM_TEXTS, min_conf=0.4)
                if confirm_dialog:
                    # 弹窗已在：精确点「确认」按钮（标题含子串，不能子串匹配）
                    btn = _pick_exact(items, INSTANCE_CONFIRM_BUTTON, min_conf=0.5)
                    if btn:
                        _click(btn["center"])
                        logger.info("[暖暖] 「确认返回」弹窗已点确认（@ %s），等回主界面",
                                    btn["center"])
                        time.sleep(1.5)
                elif esc_hit and time.time() - last_exit >= 6:
                    last_exit = time.time()
                    logger.info("[暖暖] 右上角检测到「Esc」退出标志，执行 ESC → 确认返回 → 确认")
                    _exit_instance_dialog(logger, hwnd)
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
    except Exception as exc:
        logger.debug("[暖暖启动器] 记录失败诊断异常 %s: %s", step_name, exc)


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
            # 2026-11-21：窗口没出现时留证据。若是「有新版本」弹窗还在，明确写出来，
            # 别再只报「游戏窗口未出现」让人猜。
            message = "游戏窗口未出现"
            try:
                if _update_prompt_state(_ocr(logger, hwnd) or [])[0] is not None:
                    message = "游戏窗口未出现：启动器仍显示「有新版本」更新弹窗"
            except Exception:  # noqa: BLE001
                pass
            _record_failure(
                logger,
                str(step.get("name") or "进入无限暖暖主界面"),
                message,
                hwnd,
            )
            return False, message, None, None
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
