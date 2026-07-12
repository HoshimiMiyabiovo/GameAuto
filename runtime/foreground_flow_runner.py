import ctypes
import glob
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

import psutil
import pyautogui
import win32gui
import win32process
from PIL import Image
from ..settings.paths import FLOW_CONFIG_PATH as PROJECT_FLOW_CONFIG_PATH

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


Rect = Tuple[int, int, int, int]
FLOW_CONFIG_PATH = str(PROJECT_FLOW_CONFIG_PATH)
TEXT_TEMPLATE_OCR_MEMORY: Dict[str, Tuple[str, ...]] = {
    "异环-进入游戏.png": ("进入游戏",),
    "异环-开始游戏.png": ("开始游戏",),
    "异环-开始更新.png": ("开始更新",),
    "异环-确定.png": ("开始", "确定"),
    "进入脱机.png": ("进入脱机",),
    "XM执行完毕.png": ("选择任务已执行完毕",),
    "XM运行中.png": ("停止", "运行中"),
    "XM开始.png": ("开始",),
    "XM登录.png": ("登录",),
    "CJ进入对战-2.png": ("一键刷任务",),
    "CJ进入对战-1.png": ("进入对战",),
    "CJ登录-2.png": ("登录",),
    "CJ登录-1.png": ("登录",),
    "CJ登陆成功.png": ("退出",),
}


def _normalize_target_text_candidates(target_text: Any) -> List[str]:
    if target_text is None:
        return []
    if isinstance(target_text, str):
        values = [target_text]
    elif isinstance(target_text, (list, tuple, set)):
        values = list(target_text)
    else:
        values = [str(target_text)]

    unique_values: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique_values.append(text)
    return unique_values


def _template_has_ocr_memory(template_path: str) -> bool:
    basename = os.path.basename(template_path or "")
    return basename in TEXT_TEMPLATE_OCR_MEMORY


def _resolve_target_text_candidates(
    template_path: str,
    target_text: Any,
    fallback_text: str = "",
) -> List[str]:
    basename = os.path.basename(template_path or "")
    remembered = list(TEXT_TEMPLATE_OCR_MEMORY.get(basename, ()))
    configured = _normalize_target_text_candidates(target_text)
    fallback = _normalize_target_text_candidates(fallback_text)
    return _normalize_target_text_candidates(remembered + configured + fallback)


def _display_target_text(target_texts: Any, fallback_text: str = "") -> str:
    candidates = _normalize_target_text_candidates(target_texts)
    if not candidates and fallback_text:
        return fallback_text
    return " / ".join(candidates) if candidates else "未知目标"


def _describe_match_method(match_method: Optional[str]) -> str:
    labels = {
        "template": "模板",
        "ocr": "OCR",
        "template_screen": "全屏模板",
        "ocr_screen": "全屏OCR",
        "process": "进程",
    }
    return labels.get(match_method or "", match_method or "未知")


def _log_step_match(
    logger: logging.Logger,
    flow_key: str,
    step_no: int,
    step_name: str,
    target_label: str,
    match_method: Optional[str],
    center: Optional[Tuple[int, int]] = None,
    process_name: str = "",
):
    message = (
        f"[{flow_key}] 步骤{step_no}命中: {step_name} "
        f"[方式: {_describe_match_method(match_method)}] "
        f"[目标: \"{target_label}\"]"
    )
    if center:
        message += f" [坐标: ({center[0]}, {center[1]})]"
    elif process_name:
        message += f" [进程: {process_name}]"
    logger.info(message)


def _load_flow_config(flow_key: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(FLOW_CONFIG_PATH):
        logging.getLogger(__name__).warning(f"配置文件不存在: {FLOW_CONFIG_PATH}")
        return None
    with open(FLOW_CONFIG_PATH, "r", encoding="utf-8") as f:
        all_cfg = json.load(f)
    cfg = all_cfg.get(flow_key)
    if cfg is None:
        logging.getLogger(__name__).info(f"未找到流程key: {flow_key}, 可用keys: {list(all_cfg.keys())}")
    return cfg


def _resolve_exe_path(cfg: Dict[str, Any]) -> Optional[str]:
    if cfg.get("exe_path"):
        return cfg["exe_path"] if os.path.exists(cfg["exe_path"]) else None
    if cfg.get("exe_glob"):
        candidates = sorted(glob.glob(cfg["exe_glob"]), key=os.path.getmtime, reverse=True)
        return candidates[0] if candidates else None
    return None


def _find_newest_process_by_name(name: str, after_ts: float) -> Optional[psutil.Process]:
    candidates = []
    n = name.lower()
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if (proc.info.get("name") or "").lower() == n and proc.info.get("create_time", 0) >= after_ts - 1:
                candidates.append(proc)
        except Exception:
            continue
    return max(candidates, key=lambda p: p.create_time()) if candidates else None


def _launch_maybe_elevated(logger: logging.Logger, exe_path: str) -> Optional[int]:
    launch_ts = time.time()
    try:
        proc = subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
        return proc.pid
    except OSError as e:
        if getattr(e, "winerror", None) != 740:
            logger.error(f"启动失败: {str(e)}")
            return None
        logger.info("检测到需要管理员权限，尝试 runas 提权启动")
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe_path, None, os.path.dirname(exe_path), 1)
        if ret <= 32:
            logger.error(f"提权启动失败，错误码: {ret}")
            return None
        target_name = os.path.basename(exe_path)
        end = time.time() + 25
        while time.time() < end:
            proc = _find_newest_process_by_name(target_name, launch_ts)
            if proc:
                return proc.pid
            time.sleep(0.5)
    return None


def _find_window_by_pid(pid: int) -> Optional[int]:
    found = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid == pid:
            found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None


def _window_pid(hwnd: int) -> Optional[int]:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None


def _process_matches_filters(
    pid: Optional[int],
    process_names: Optional[List[str]] = None,
    launched_after: float = 0.0,
) -> bool:
    if not pid:
        return False

    names = {(name or "").strip().lower() for name in (process_names or []) if str(name).strip()}
    try:
        proc = psutil.Process(pid)
        if names and (proc.name() or "").lower() not in names:
            return False
        if launched_after and proc.create_time() < launched_after - 1:
            return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _find_window_by_filters(
    title_keywords: Optional[List[str]] = None,
    process_names: Optional[List[str]] = None,
    launched_after: float = 0.0,
) -> Optional[Tuple[int, int]]:
    candidates: List[int] = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not _window_rect(hwnd):
            return
        pid = _window_pid(hwnd)
        title_matched = _matches_window_keywords(hwnd, title_keywords)

        if process_names and not _process_matches_filters(pid, process_names, launched_after):
            return
        if launched_after and not process_names and not _process_matches_filters(pid, None, launched_after):
            return
        if title_keywords and not process_names and not title_matched:
            return
        candidates.append(hwnd)

    win32gui.EnumWindows(callback, None)
    if not candidates:
        return None

    foreground_hwnd = None
    try:
        foreground_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        foreground_hwnd = None

    def sort_key(hwnd: int):
        return (
            1 if hwnd == foreground_hwnd else 0,
            1 if _matches_window_keywords(hwnd, title_keywords) else 0,
            _window_area(hwnd),
        )

    best_hwnd = max(candidates, key=sort_key)
    best_pid = _window_pid(best_hwnd)
    if best_pid is None:
        return None
    return best_hwnd, best_pid


def _wait_window_by_pid(pid: int, timeout: int = 45) -> Optional[int]:
    end = time.time() + timeout
    while time.time() < end:
        hwnd = _find_window_by_pid(pid)
        if hwnd:
            return hwnd
        time.sleep(0.6)
    return None


def _wait_window_for_flow(
    pid: int,
    timeout: int = 45,
    title_keywords: Optional[List[str]] = None,
    process_names: Optional[List[str]] = None,
    launched_after: float = 0.0,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[int], Optional[int]]:
    end = time.time() + timeout
    while time.time() < end:
        hwnd = _find_window_by_pid(pid)
        if hwnd:
            if not title_keywords or _matches_window_keywords(hwnd, title_keywords):
                return hwnd, pid
            if logger:
                logger.warning(
                    f"检测到进程窗口但标题未命中关键词，按进程接管: PID={pid} HWND={hwnd} 标题={_window_title(hwnd)!r}"
                )
            return hwnd, pid

        fallback = _find_window_by_filters(
            title_keywords=title_keywords,
            process_names=process_names,
            launched_after=launched_after,
        )
        if fallback:
            return fallback
        time.sleep(0.6)
    return None, None


def _window_rect(hwnd: int) -> Optional[Rect]:
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r <= l or b <= t:
            return None
        return l, t, r - l, b - t
    except Exception:
        return None


def _visible_windows_by_pid(pid: int) -> List[int]:
    found: List[int] = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid != pid:
            return
        if _window_rect(hwnd):
            found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return found


def _window_title(hwnd: int) -> str:
    try:
        return (win32gui.GetWindowText(hwnd) or "").strip().lower()
    except Exception:
        return ""


def _window_area(hwnd: int) -> int:
    rect = _window_rect(hwnd)
    if not rect:
        return 0
    return rect[2] * rect[3]


def _matches_window_keywords(hwnd: int, title_keywords: Optional[List[str]]) -> bool:
    keywords = [str(k).lower() for k in (title_keywords or []) if str(k).strip()]
    if not keywords:
        return True
    title = _window_title(hwnd)
    return any(k in title for k in keywords)


def _choose_best_window_for_flow(
    pid: int,
    title_keywords: Optional[List[str]] = None,
    current_hwnd: Optional[int] = None,
) -> Optional[int]:
    candidates = _visible_windows_by_pid(pid)
    if not candidates:
        return None

    keyword_matched = [hwnd for hwnd in candidates if _matches_window_keywords(hwnd, title_keywords)]
    if keyword_matched:
        candidates = keyword_matched

    foreground_hwnd = None
    try:
        foreground_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        foreground_hwnd = None

    def sort_key(hwnd: int):
        return (
            1 if hwnd == foreground_hwnd else 0,
            1 if _matches_window_keywords(hwnd, title_keywords) else 0,
            _window_area(hwnd),
            1 if hwnd == current_hwnd else 0,
        )

    return max(candidates, key=sort_key)


def _refresh_flow_window(
    pid: int,
    current_hwnd: Optional[int] = None,
    title_keywords: Optional[List[str]] = None,
    timeout: float = 0.0,
) -> Optional[int]:
    end = time.time() + max(0.0, float(timeout))
    latest = current_hwnd

    while True:
        candidate = _choose_best_window_for_flow(
            pid,
            title_keywords=title_keywords,
            current_hwnd=current_hwnd,
        )
        if candidate:
            latest = candidate
            if current_hwnd is None or candidate != current_hwnd or timeout <= 0:
                return candidate

        if time.time() >= end:
            return latest
        time.sleep(0.2)


def _focus_window(hwnd: int):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _locate_template_on_screen(
    template_path: str,
    region: Tuple[int, int, int, int],
    scales,
    confidences,
) -> Optional[Tuple[int, int]]:
    if not os.path.exists(template_path):
        return None

    tpl0 = Image.open(template_path)
    for scale in scales:
        tpl = tpl0
        if scale != 1.0:
            nw = max(8, int(tpl0.width * scale))
            nh = max(8, int(tpl0.height * scale))
            tpl = tpl0.resize((nw, nh))
        for conf in confidences:
            try:
                pt = pyautogui.locateCenterOnScreen(tpl, region=region, confidence=float(conf))
                if pt:
                    return int(pt.x), int(pt.y)
            except Exception:
                try:
                    pt = pyautogui.locateCenterOnScreen(tpl, region=region)
                    if pt:
                        return int(pt.x), int(pt.y)
                except Exception:
                    pass
    return None


def _locate_template_in_window(hwnd: int, template_path: str, scales, confidences) -> Optional[Tuple[int, int]]:
    if not os.path.exists(template_path):
        return None
    rect = _window_rect(hwnd)
    if not rect:
        return None
    l, t, w, h = rect
    region = (l, t, w, h)

    # 先直接在屏幕区域中匹配，避免局部截图与后续点击坐标换算产生偏差。
    screen_center = _locate_template_on_screen(template_path, region, scales, confidences)
    if screen_center:
        return screen_center[0] - l, screen_center[1] - t

    screenshot = pyautogui.screenshot(region=region)
    tpl0 = Image.open(template_path)
    for scale in scales:
        tpl = tpl0
        if scale != 1.0:
            nw = max(8, int(tpl0.width * scale))
            nh = max(8, int(tpl0.height * scale))
            tpl = tpl0.resize((nw, nh))
        for conf in confidences:
            box = None
            try:
                box = pyautogui.locate(tpl, screenshot, confidence=float(conf))
            except Exception:
                try:
                    box = pyautogui.locate(tpl, screenshot)
                except Exception:
                    box = None
            if box:
                return int(box.left + box.width / 2), int(box.top + box.height / 2)
    return None


def _ocr_locate_in_window(
    logger: logging.Logger,
    hwnd: Optional[int],
    target_text: Any,
    min_confidence: float = 0.5,
    search_region: Optional[Tuple[int, int, int, int]] = None,
) -> Optional[Tuple[int, int]]:
    """
    使用OCR在窗口中定位目标文字（备选方案）。
    返回窗口局部坐标或None。
    search_region: 屏幕坐标的局部搜索区域 (left, top, width, height)，限制OCR搜索范围
    """
    if not HAS_OCR:
        return None

    target_candidates = _normalize_target_text_candidates(target_text)
    if not target_candidates:
        return None

    try:
        from .ocr_click_tester import ocr_find_text

        for text in target_candidates:
            if search_region:
                result = ocr_find_text(
                    target_text=text,
                    logger=logger,
                    hwnd=None,
                    region=search_region,
                    min_confidence=min_confidence,
                )
            else:
                result = ocr_find_text(
                    target_text=text,
                    logger=logger,
                    hwnd=hwnd,
                    min_confidence=min_confidence,
                )

            if not result:
                continue

            center = result["center"]
            rect = _window_rect(hwnd) if hwnd else None
            if rect:
                l, t, _, _ = rect
                return (center[0] - l, center[1] - t)
            return center
    except Exception as e:
        logger.debug(f"OCR定位失败: {str(e)}")
    return None


def _locate_with_ocr_fallback(
    logger: logging.Logger,
    hwnd: int,
    template_path: str,
    target_text: Any,
    scales,
    confidences,
    enable_ocr: bool = True,
    local_region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[Optional[Tuple[int, int]], str]:
    """
    模板匹配+OCR联合定位。
    返回：(窗口局部坐标, 方法: "template" | "ocr" | None)
    local_region: 屏幕坐标的局部搜索区域，限制OCR搜索范围防止页面其他内容干扰
    """
    target_candidates = _resolve_target_text_candidates(template_path, target_text)
    target_label = _display_target_text(target_candidates, os.path.basename(template_path or ""))

    center = _locate_template_in_window(hwnd, template_path, scales, confidences)
    if center:
        logger.info(f"[{target_label}] 模板匹配成功 @ {center}")
        return center, "template"

    if enable_ocr and target_candidates:
        center = _ocr_locate_in_window(logger, hwnd, target_candidates, search_region=local_region)
        if center:
            logger.info(f"[{target_label}] OCR匹配成功 @ {center}")
            return center, "ocr"

    return None, None


def _click_window_local(hwnd: int, x_local: int, y_local: int, clicks: int = 1, interval: float = 0.2):
    rect = _window_rect(hwnd)
    if not rect:
        return
    l, t, _, _ = rect
    _focus_window(hwnd)
    pyautogui.click(l + int(x_local), t + int(y_local), clicks=clicks, interval=interval)


def _click_screen(x: int, y: int, clicks: int = 1, interval: float = 0.2):
    pyautogui.click(int(x), int(y), clicks=clicks, interval=interval)


def _find_process_by_exact_name(name: str) -> Optional[psutil.Process]:
    target = (name or "").strip().lower()
    if not target:
        return None

    candidates = []
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                candidates.append(proc)
        except Exception:
            continue
    return max(candidates, key=lambda p: p.create_time()) if candidates else None


def _wait_any_template(hwnd: int, templates: List[str], timeout_sec: int, scales, confidences) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        for tpl in templates:
            if tpl and _locate_template_in_window(hwnd, tpl, scales, confidences):
                return True
        time.sleep(0.4)
    return False


def _check_element_exists(
    logger: logging.Logger,
    hwnd: int,
    template_path: str,
    target_text: str,
    scales,
    confidences,
    enable_ocr: bool = True,
    local_region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[bool, str]:
    """
    检查目标元素是否仍然存在。
    返回：(是否存在, 匹配方式)
    local_region: 局部搜索区域，限制OCR范围防止页面其他内容干扰
    """
    center, method = _locate_with_ocr_fallback(
        logger=logger,
        hwnd=hwnd,
        template_path=template_path,
        target_text=target_text,
        scales=scales,
        confidences=confidences,
        enable_ocr=enable_ocr,
        local_region=local_region,
    )
    exists = center is not None
    return exists, method


def _calc_local_search_region(
    center_local: Tuple[int, int],
    template_path: str,
    hwnd: int,
    scale: float = 2.0,
) -> Optional[Tuple[int, int, int, int]]:
    """
    以识别位置为中心，按模板的scale倍大小计算局部搜索区域（屏幕坐标）。
    用于限制OCR搜索范围，防止页面其他内容（如通知）干扰识别。
    返回：(left, top, width, height) 屏幕坐标，或None
    """
    if not template_path or not os.path.exists(template_path):
        return None
    try:
        tpl = Image.open(template_path)
        region_w = int(tpl.width * scale)
        region_h = int(tpl.height * scale)
        rect = _window_rect(hwnd)
        if not rect:
            return None
        l, t, _, _ = rect
        screen_x = l + center_local[0]
        screen_y = t + center_local[1]
        x1 = max(0, screen_x - region_w // 2)
        y1 = max(0, screen_y - region_h // 2)
        return (x1, y1, region_w, region_h)
    except Exception:
        return None


def confirm_disappear(
    logger: logging.Logger,
    hwnd: int,
    step_name: str,
    template: str,
    target_text: str,
    wait_time: int,
    timeout_sec: int,
    confirm_templates: List[str],
    scales,
    confidences,
    enable_ocr: bool,
    local_region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[bool, str]:
    """
    确认策略1：消失型（点击后按钮消失）

    逻辑：
    1. 等待界面响应
    2. 在局部区域内检查原元素是否消失
       - 消失 → ✅ 成功
       - 还在 → ❌ 失败
     3. 如果配置了 confirm_templates，继续确认下一状态模板已出现
         - 找到确认模板 → ✅ 成功
         - 未找到 → ❌ 失败
    local_region: 局部搜索区域，防止页面其他位置的同名文字干扰
    """
    import time as _time

    logger.debug(f"[{step_name}] [消失型确认] 等待 {wait_time}秒...")
    _time.sleep(wait_time)

    still_exists, _ = _check_element_exists(
        logger=logger,
        hwnd=hwnd,
        template_path=template,
        target_text=target_text,
        scales=scales,
        confidences=confidences,
        enable_ocr=enable_ocr,
        local_region=local_region,
    )

    if not still_exists:
        if confirm_templates:
            remaining_timeout = max(timeout_sec - wait_time, 2)
            if _wait_any_template(hwnd, confirm_templates, remaining_timeout, scales, confidences):
                logger.info(f"[{step_name}] ✅ [消失型] 原元素消失且确认模板出现 → 操作成功")
                return True, "元素消失且确认模板匹配"
            logger.warning(f"[{step_name}] ❌ [消失型] 原元素已消失，但未等到确认模板出现")
            return False, "元素已消失但确认模板未出现"

        logger.info(f"[{step_name}] ✅ [消失型] 目标元素已消失 → 操作成功")
        return True, "元素已消失"
    else:
        logger.warning(f"[{step_name}] ❌ [消失型] 目标元素仍存在 → 可能未点击成功")
        return False, "元素仍存在"


def confirm_text_change(
    logger: logging.Logger,
    hwnd: int,
    step_name: str,
    template: str,
    target_text_before: str,
    expected_text_after: str,
    confirm_templates: List[str],
    wait_time: int,
    timeout_sec: int,
    scales,
    confidences,
    enable_ocr: bool,
    local_region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[bool, str]:
    """
    确认策略2：文字变化型（点击后按钮文字改变）

    逻辑：
    1. 等待界面响应
    2. 在局部区域内检查是否出现新文字（expected_text_after）
       - 出现新文字 → ✅ 成功
    3. 如果有 confirm_templates，也检查模板匹配
       - 找到模板 → ✅ 成功
    4. 都不满足 → ❌ 失败
    local_region: 局部搜索区域，防止页面其他位置的同名文字干扰
    """
    import time as _time

    logger.debug(f"[{step_name}] [文字变化确认] 等待 {wait_time}秒...")
    logger.info(f"[{step_name}] [文字变化] 期望: \"{target_text_before}\" → \"{expected_text_after}\"")
    _time.sleep(wait_time)

    if enable_ocr and expected_text_after:
        try:
            from .ocr_click_tester import ocr_find_text
            ocr_kwargs = {"target_text": expected_text_after, "logger": logger, "min_confidence": 0.5}
            if local_region:
                ocr_kwargs["region"] = local_region
            else:
                ocr_kwargs["hwnd"] = hwnd
            new_text_found = ocr_find_text(**ocr_kwargs)
            if new_text_found:
                logger.info(f"[{step_name}] ✅ [文字变化] 检测到新文字 \"{expected_text_after}\"")
                return True, f"文字变化为:{expected_text_after}"
        except Exception as e:
            logger.debug(f"[{step_name}] OCR检测失败: {str(e)}")

    # 策略B：使用确认模板匹配
    if confirm_templates:
        remaining_timeout = max(timeout_sec - wait_time, 2)
        logger.debug(f"[{step_name}] [文字变化] 检查确认模板 ({remaining_timeout}秒)...")

        template_found = _wait_any_template(hwnd, confirm_templates, remaining_timeout, scales, confidences)

        if template_found:
            logger.info(f"[{step_name}] ✅ [文字变化] 确认模板匹配成功")
            return True, "确认模板匹配"

    # 都没找到
    logger.warning(f"[{step_name}] ❌ [文字变化] 未检测到变化（期望:\"{expected_text_after}\"）")
    return False, "未检测到文字变化"


def confirm_none(
    logger: logging.Logger,
    step_name: str,
) -> Tuple[bool, str]:
    """
    确认策略3：无确认（点击后窗口变化极小）
    
    适用场景：
    - 变化无法识别的操作
    - 辅助性点击操作
    
    逻辑：
    - 直接认为成功，不做任何验证
    """
    logger.info(f"[{step_name}] ℹ️ [无确认] 跳过验证，默认认为操作成功")
    return True, "无验证-默认成功"


def execute_confirm_strategy(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    enable_ocr: bool,
    local_region: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[bool, str]:
    """
    智能确认策略路由器：
    根据 step 配置的 confirm_type 自动选择确认策略。

    支持的三种类型：
    - "disappear":       消失型（按钮点击后消失）
    - "text_change":     文字变化型（按钮文字改变）
    - "template_match":  模板匹配型（检查确认模板出现）
    - "none":            无确认（变化极小）

    local_region: 局部搜索区域（屏幕坐标），限制OCR范围防止干扰
    返回：(是否成功, 原因描述)
    """
    step_name = step.get("name", "未知步骤")
    confirm_type = step.get("confirm_type", "auto")  # 默认自动推断
    template = step.get("template", "")
    target_text = step.get("target_text", "")
    confirm_templates = step.get("confirm_templates", [])
    confirm_timeout = int(step.get("confirm_timeout_sec", 8))
    expected_text = step.get("expected_text", "")  # 文字变化后的期望文字

    # 自动推断 confirm_type（如果未明确指定）
    if confirm_type == "auto":
        if expected_text:
            confirm_type = "text_change"
        elif not confirm_templates:
            # 无确认模板 → 使用消失检测或无确认
            confirm_type = "none"
        else:
            # 有确认模板 → 使用模板匹配（兼容旧配置）
            confirm_type = "template_match"

    logger.debug(f"[{step_name}] 确认策略: {confirm_type}")

    # 路由到对应的确认策略
    if confirm_type == "disappear":
        return confirm_disappear(
            logger=logger,
            hwnd=hwnd,
            step_name=step_name,
            template=template,
            target_text=target_text,
            wait_time=min(confirm_timeout, 3),
            timeout_sec=confirm_timeout,
            confirm_templates=confirm_templates,
            scales=scales,
            confidences=confidences,
            enable_ocr=enable_ocr,
            local_region=local_region,
        )

    elif confirm_type == "text_change":
        return confirm_text_change(
            logger=logger,
            hwnd=hwnd,
            step_name=step_name,
            template=template,
            target_text_before=target_text,
            expected_text_after=expected_text,
            confirm_templates=confirm_templates,
            wait_time=min(confirm_timeout, 3),
            timeout_sec=confirm_timeout,
            scales=scales,
            confidences=confidences,
            enable_ocr=enable_ocr,
            local_region=local_region,
        )

    elif confirm_type == "none":
        return confirm_none(logger=logger, step_name=step_name)

    elif confirm_type == "template_match":
        # 兼容旧配置：仅使用 confirm_templates
        if confirm_templates:
            found = _wait_any_template(hwnd, confirm_templates, confirm_timeout, scales, confidences)
            if found:
                logger.info(f"[{step_name}] ✅ [模板匹配] 确认模板找到")
                return True, "确认模板匹配"
            else:
                logger.warning(f"[{step_name}] ❌ [模板匹配] 未找到确认模板")
                return False, "确认模板未找到"
        else:
            # 无模板，降级为无确认
            return confirm_none(logger=logger, step_name=step_name)

    else:
        # 未知类型，降级为无确认
        logger.warning(f"[{step_name}] ⚠️ 未知的确认类型 '{confirm_type}'，降级为无确认")
        return confirm_none(logger=logger, step_name=step_name)


def run_flow(
    logger: logging.Logger,
    flow_key: str,
    stop_after_step: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    增强版流程执行器，支持OCR+模板匹配联合识别。

    特性：
    - 自动启用OCR备选（XM流程默认启用）
    - 详细的日志输出（包括识别方式和置信度）
    - 失败时输出屏幕文字快照便于调试
    - 可配置的步骤级目标文字
    - 支持在指定步骤确认成功后提前返回
    """
    cfg = _load_flow_config(flow_key)
    if not cfg:
        return False, f"[{flow_key}] 未找到流程配置"
    if not cfg.get("enabled", True):
        return False, f"[{flow_key}] 流程已禁用"

    exe_path = _resolve_exe_path(cfg)
    if not exe_path:
        return False, f"[{flow_key}] 未找到可执行文件（路径或版本匹配失败）"

    launch_ts = time.time()
    pid = _launch_maybe_elevated(logger, exe_path)
    if not pid:
        return False, f"[{flow_key}] 启动失败"
    logger.info(f"[{flow_key}] 已启动: {exe_path} PID={pid}")
    time.sleep(int(cfg.get("startup_wait_sec", 10)))

    window_title_keywords = cfg.get("window_title_keywords", [])
    window_process_names = cfg.get("window_process_names", [])

    hwnd, window_pid = _wait_window_for_flow(
        pid,
        timeout=45,
        title_keywords=window_title_keywords,
        process_names=window_process_names,
        launched_after=launch_ts,
        logger=logger,
    )
    if not hwnd:
        return False, f"[{flow_key}] 未找到窗口"
    if window_pid and window_pid != pid:
        logger.info(f"[{flow_key}] 窗口由新进程承载: PID {pid} -> {window_pid}")
        pid = window_pid

    hwnd = _refresh_flow_window(
        pid,
        current_hwnd=hwnd,
        title_keywords=window_title_keywords,
    ) or hwnd

    scales = cfg.get("match_scales", [1.0, 1.25, 0.8])
    confidences = cfg.get("match_confidences", [0.9, 0.85, 0.8])
    steps = cfg.get("steps", [])
    if not steps:
        return False, f"[{flow_key}] 配置中无步骤"

    # 判断是否启用OCR备选（XM流程默认启用，CJ流程不启用）
    enable_ocr = cfg.get("enable_ocr_fallback", flow_key == "XM")

    step_attempts: Dict[int, int] = {}
    step_local_region: Dict[int, Optional[Tuple[int, int, int, int]]] = {}
    i = 0
    while i < len(steps):
        step = steps[i]
        step_no = i + 1
        name = step.get("name", f"step{step_no}")
        action = step.get("action", "click_template")
        search_scope = str(step.get("search_scope", "window") or "window").lower()
        template = step.get("template", "")
        process_name = step.get("process_name", "")
        target_texts = _resolve_target_text_candidates(template, step.get("target_text", name), fallback_text=name)
        target_text = target_texts[0] if target_texts else str(name)
        target_label = _display_target_text(target_texts, target_text)
        step_enable_ocr = bool(step.get("enable_ocr_fallback", enable_ocr or _template_has_ocr_memory(template)))
        timeout_sec = int(step.get("timeout_sec", 60))
        post_delay = float(step.get("post_delay_sec", 1.0))
        clicks = int(step.get("clicks", 1))
        no_click = bool(step.get("no_click", False))
        max_attempts = int(step.get("max_attempts", 2))
        on_fail = step.get("on_fail", "fail")
        optional_if_missing = bool(step.get("optional_if_missing", False))
        confirm_templates = step.get("confirm_templates", [])
        confirm_timeout = int(step.get("confirm_timeout_sec", 8))

        refreshed_hwnd = _refresh_flow_window(
            pid,
            current_hwnd=hwnd,
            title_keywords=window_title_keywords,
        )
        if refreshed_hwnd and refreshed_hwnd != hwnd:
            logger.info(
                f"[{flow_key}] 检测到窗口切换: {hwnd} -> {refreshed_hwnd} "
                f"{_window_rect(hwnd)} -> {_window_rect(refreshed_hwnd)}"
            )
            hwnd = refreshed_hwnd
            step_local_region.pop(i, None)

        local_region = step_local_region.get(i)

        logger.info(
            f"[{flow_key}] 步骤{step_no}: {name} ({action}) [目标文字: \"{target_label}\"] "
            f"[当前尝试: {step_attempts.get(i, 0) + 1}] "
            f"[搜索范围: {search_scope}] [OCR备选: {'启用' if step_enable_ocr else '禁用'}]"
        )

        end = time.time() + timeout_sec
        center = None
        match_method = None

        while time.time() < end:
            if action == "wait_process":
                proc = _find_process_by_exact_name(process_name)
                if proc:
                    match_method = "process"
                    if bool(step.get("switch_window_to_process", False)):
                        switched_hwnd = _wait_window_by_pid(proc.pid, timeout=min(max(timeout_sec, 5), 45))
                        if switched_hwnd:
                            logger.info(f"[{flow_key}] 已切换到进程窗口: {process_name} PID={proc.pid} HWND={switched_hwnd}")
                            pid = proc.pid
                            hwnd = switched_hwnd
                            local_region = None
                            step_local_region.pop(i, None)
                        else:
                            logger.warning(f"[{flow_key}] 目标进程已出现，但未找到可见窗口: {process_name} PID={proc.pid}")
                    break
            else:
                refreshed_hwnd = _refresh_flow_window(
                    pid,
                    current_hwnd=hwnd,
                    title_keywords=window_title_keywords,
                )
                if refreshed_hwnd and refreshed_hwnd != hwnd:
                    logger.info(
                        f"[{flow_key}] 匹配期间窗口已切换: {hwnd} -> {refreshed_hwnd} "
                        f"{_window_rect(hwnd)} -> {_window_rect(refreshed_hwnd)}"
                    )
                    hwnd = refreshed_hwnd
                    local_region = None
                    step_local_region.pop(i, None)

                if search_scope == "screen":
                    screen_w, screen_h = pyautogui.size()
                    screen_region = (0, 0, int(screen_w), int(screen_h))
                    center = _locate_template_on_screen(
                        template,
                        region=screen_region,
                        scales=scales,
                        confidences=confidences,
                    )
                    if center:
                        logger.info(f"[{target_label}] 全屏模板匹配成功 @ {center}")
                        match_method = "template_screen"
                    elif step_enable_ocr and target_texts:
                        center = _ocr_locate_in_window(
                            logger=logger,
                            hwnd=None,
                            target_text=target_texts,
                            search_region=screen_region,
                        )
                        if center:
                            logger.info(f"[{target_label}] 全屏OCR匹配成功 @ {center}")
                            match_method = "ocr_screen"
                else:
                    center, match_method = _locate_with_ocr_fallback(
                        logger=logger,
                        hwnd=hwnd,
                        template_path=template,
                        target_text=target_texts if step_enable_ocr else [],
                        scales=scales,
                        confidences=confidences,
                        enable_ocr=step_enable_ocr,
                        local_region=local_region,
                    )
                if center:
                    break
            time.sleep(0.5)

        matched = action == "wait_process" and match_method == "process" or center is not None
        if not matched:
            if optional_if_missing:
                logger.info(f"[{flow_key}] 可选步骤{step_no}未出现，跳过: {name}")
                step_attempts[i] = 0
                step_local_region.pop(i, None)
                i += 1
                continue
            step_attempts[i] = step_attempts.get(i, 0) + 1
            if step_attempts[i] > max_attempts:
                try:
                    from .ocr_click_tester import ocr_window_content
                    results = ocr_window_content(logger, hwnd=hwnd)
                    if results:
                        logger.warning(f"[{flow_key}] 步骤失败时屏幕文字:")
                        for r in results[:10]:
                            logger.warning(f"  - \"{r['text']}\" (置信度: {r['confidence']:.2f}) @ {r['center']}")
                except Exception:
                    pass

                return False, f"[{flow_key}] 步骤失败: {name} 未匹配模板/OCR（超过重试上限）"
            if on_fail == "retry_prev" and i > 0:
                logger.warning(f"[{flow_key}] 步骤{step_no}未匹配，回退到上一步重试")
                i -= 1
            else:
                logger.warning(f"[{flow_key}] 步骤{step_no}未匹配，重试当前步骤")
            continue

        _log_step_match(
            logger=logger,
            flow_key=flow_key,
            step_no=step_no,
            step_name=name,
            target_label=target_label,
            match_method=match_method,
            center=center,
            process_name=process_name,
        )

        # 找到元素后，计算局部搜索区域（模板200%大小），用于后续确认和重试
        if center and template and search_scope != "screen":
            local_region = _calc_local_search_region(center, template, hwnd, scale=2.0)
            step_local_region[i] = local_region
            if local_region:
                logger.debug(f"[{flow_key}] 步骤{step_no} 局部搜索区域: {local_region}")

        if action == "click_template":
            if no_click:
                logger.info(f"[{flow_key}] 仅确认不点击: {name}")
            else:
                if search_scope == "screen":
                    _click_screen(center[0], center[1], clicks=clicks)
                else:
                    _click_window_local(hwnd, center[0], center[1], clicks=clicks)
                logger.info(f"[{flow_key}] 已点击: ({center[0]}, {center[1]}) [方式: {match_method}]")

                refreshed_hwnd = _refresh_flow_window(
                    pid,
                    current_hwnd=hwnd,
                    title_keywords=window_title_keywords,
                    timeout=min(confirm_timeout, 3),
                )
                if refreshed_hwnd and refreshed_hwnd != hwnd:
                    logger.info(
                        f"[{flow_key}] 点击后检测到窗口切换: {hwnd} -> {refreshed_hwnd} "
                        f"{_window_rect(hwnd)} -> {_window_rect(refreshed_hwnd)}"
                    )
                    hwnd = refreshed_hwnd
                    local_region = None
                    step_local_region.pop(i, None)
        elif action == "wait_template":
            logger.info(f"[{flow_key}] 状态确认通过: {name}")
        elif action == "wait_process":
            logger.info(f"[{flow_key}] 目标进程已出现: {process_name}")
        else:
            return False, f"[{flow_key}] 未知动作: {action}"

        if action == "click_template" and not no_click:
            ok, reason = execute_confirm_strategy(
                logger=logger,
                hwnd=hwnd,
                step=step,
                scales=scales,
                confidences=confidences,
                enable_ocr=step_enable_ocr,
                local_region=local_region,
            )

            if not ok:
                step_attempts[i] = step_attempts.get(i, 0) + 1
                if step_attempts[i] > max_attempts:
                    return False, f"[{flow_key}] 步骤失败: {name} ({reason})（超过重试上限）"
                if on_fail == "retry_prev" and i > 0:
                    logger.warning(f"[{flow_key}] 步骤{step_no}确认失败({reason})，回退上一步")
                    i -= 1
                else:
                    logger.warning(f"[{flow_key}] 步骤{step_no}确认失败({reason})，重试当前步骤")
                continue
            logger.info(f"[{flow_key}] 步骤{step_no}确认成功 [{reason}]")

        step_attempts[i] = 0
        step_local_region.pop(i, None)

        if stop_after_step and name == stop_after_step:
            time.sleep(post_delay)
            logger.info(f"[{flow_key}] 已达到提前返回步骤: {stop_after_step}，保持流程进程继续运行")
            return True, f"[{flow_key}] 已完成到步骤: {stop_after_step}"

        time.sleep(post_delay)
        i += 1

    if cfg.get("close_process_on_finish", False):
        try:
            psutil.Process(pid).terminate()
            logger.info(f"[{flow_key}] 已关闭流程进程 PID={pid}")
        except Exception:
            pass
    return True, f"[{flow_key}] 流程执行成功"
