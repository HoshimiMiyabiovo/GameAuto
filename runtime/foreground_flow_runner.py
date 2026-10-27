import ctypes
import glob
import json
import logging
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil
import pyautogui
import win32gui
import win32api
import win32con
import win32process
from PIL import Image
from ..settings.paths import BASE_DIR, FLOW_CONFIG_PATH as PROJECT_FLOW_CONFIG_PATH

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


Rect = Tuple[int, int, int, int]
FLOW_CONFIG_PATH = str(PROJECT_FLOW_CONFIG_PATH)

# ── 模板图缓存：避免每次匹配都 Image.open 读盘（抄 ok-script FeatureSet 预加载思路）──
_TEMPLATE_IMAGE_CACHE: Dict[str, Any] = {}


def _get_cached_template(template_path: str) -> Optional[Any]:
    if not template_path or not os.path.exists(template_path):
        return None
    img = _TEMPLATE_IMAGE_CACHE.get(template_path)
    if img is not None:
        return img
    try:
        # PIL 先加载规避 OpenCV 中文路径问题，与 _find_by_template 一致
        img = Image.open(template_path)
        img.load()
        # 简单上限防内存膨胀
        if len(_TEMPLATE_IMAGE_CACHE) > 64:
            _TEMPLATE_IMAGE_CACHE.clear()
        _TEMPLATE_IMAGE_CACHE[template_path] = img
        return img
    except Exception:
        return None


def _match_template_cv2_once(screenshot: Any, tpl: Any, confidences) -> Optional[Tuple[int, int]]:
    """单张截图上对单个尺度模板匹配一次，返回截图内中心坐标。

    同一张 matchTemplate 结果只取全局最大值，与最低阈值比一次即可
    （等价于旧高→低逐阈值循环，但少做 N-1 次 minMaxLoc）。
    """
    try:
        import cv2
        import numpy as np
        scr = screenshot
        if hasattr(scr, "convert"):
            scr_gray = cv2.cvtColor(np.asarray(scr.convert("RGB")), cv2.COLOR_RGB2GRAY)
        else:
            arr = np.asarray(scr)
            scr_gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr
        if hasattr(tpl, "convert"):
            tpl_gray = cv2.cvtColor(np.asarray(tpl.convert("RGB")), cv2.COLOR_RGB2GRAY)
        else:
            tpl_gray = np.asarray(tpl)
        if tpl_gray.shape[0] > scr_gray.shape[0] or tpl_gray.shape[1] > scr_gray.shape[1]:
            return None
        res = cv2.matchTemplate(scr_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        try:
            min_conf = float(min(float(c) for c in confidences))
        except Exception:
            return None
        if max_val >= min_conf:
            h, w = tpl_gray.shape[:2]
            return int(max_loc[0] + w // 2), int(max_loc[1] + h // 2)
    except Exception:
        pass
    return None
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
    "归龙潮启动器-开始游戏.png": ("开始游戏",),
    "归龙潮-开始游戏.png": ("开始游戏",),
    "归龙潮-关闭公告.png": ("关闭", "确定", "知道了"),
    "归龙潮-esc.png": ("红叶丹歌",),
    "归龙潮-每日任务.png": ("每日任务",),
    "归龙潮-挑战任务.png": ("挑战任务",),
    "归龙潮-每日任务-边界.png": ("边界",),
    "归龙潮-每日任务-前往.png": ("前往",),
    "归龙潮-每日任务-刷新.png": ("刷新",),
    "白荆回廊-开始游戏.png": ("开始游戏",),
    "trae-签到.png": ("签到",),
    "trae-签到弹窗.png": ("签到参与抽奖",),
    "trae-用户头像.png": ("用户",),
    "trae-积分入口.png": ("3,000", "积分"),
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


def _normalize_flow_paths(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把流程配置里的相对模板路径锚定到项目根目录，项目整体迁移不失效。

    只处理相对路径且项目内确实存在的文件；绝对路径与外部程序路径不动。
    覆盖：step.template / step.confirm_templates / step.confirm_template /
    step.shop_entry_template / step.tab_template / step.exit_marker_template /
    step.shop_groups[].tab_template / tab_selected_template / items[].template /
    step.items[].template。
    """
    base = str(BASE_DIR)

    def _anchor(path: Any) -> Any:
        if isinstance(path, str) and path and not os.path.isabs(path):
            cand = os.path.join(base, path)
            if os.path.exists(cand):
                return cand
        return path

    for step in cfg.get("steps", []) if isinstance(cfg, dict) else []:
        if not isinstance(step, dict):
            continue
        if "template" in step:
            step["template"] = _anchor(step.get("template"))
        if "confirm_template" in step:
            step["confirm_template"] = _anchor(step.get("confirm_template"))
        cts = step.get("confirm_templates")
        if isinstance(cts, list):
            step["confirm_templates"] = [_anchor(p) for p in cts]
        for key in ("shop_entry_template", "tab_template", "exit_marker_template"):
            if key in step:
                step[key] = _anchor(step.get(key))
        for item in step.get("items", []) or []:
            if isinstance(item, dict) and "template" in item:
                item["template"] = _anchor(item.get("template"))
        for group in step.get("shop_groups", []) or []:
            if not isinstance(group, dict):
                continue
            if "tab_template" in group:
                group["tab_template"] = _anchor(group.get("tab_template"))
            if "tab_selected_template" in group:
                group["tab_selected_template"] = _anchor(group.get("tab_selected_template"))
            for item in group.get("items", []) or []:
                if isinstance(item, dict) and "template" in item:
                    item["template"] = _anchor(item.get("template"))
    return cfg


# 会被当成列表用的流程字段。手写 JSON 时容易把 Python repr
# 当字符串塞进去（如 "['a', 'b']"），此时按字符迭代会导致窗口永远匹配不上、
# 模板 scale 取到单个字符。加载时统一纠正，解析失败则保留原值。
_LIST_FLOW_FIELDS = (
    "window_title_keywords",
    "window_process_names",
    "match_scales",
    "match_confidences",
)


def _coerce_list_flow_fields(cfg: Dict[str, Any], flow_key: str = "") -> Dict[str, Any]:
    import ast

    if not isinstance(cfg, dict):
        return cfg
    for field in _LIST_FLOW_FIELDS:
        value = cfg.get(field)
        if isinstance(value, str):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple)):
                cfg[field] = list(parsed)
                logging.getLogger(__name__).warning(
                    f"[{flow_key}] 配置字段 {field} 是字符串，已按列表解析；"
                    "请改成 JSON 数组写法"
                )
    return cfg


def _load_flow_config(flow_key: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(FLOW_CONFIG_PATH):
        logging.getLogger(__name__).warning(f"配置文件不存在: {FLOW_CONFIG_PATH}")
        return None
    with open(FLOW_CONFIG_PATH, "r", encoding="utf-8") as f:
        all_cfg = json.load(f)
    cfg = all_cfg.get(flow_key)
    if cfg is None:
        logging.getLogger(__name__).info(f"未找到流程key: {flow_key}, 可用keys: {list(all_cfg.keys())}")
    else:
        cfg = _coerce_list_flow_fields(cfg, flow_key)
        cfg = _normalize_flow_paths(cfg)
    return cfg


def _read_flow_exe_override(flow_key: str) -> str:
    """读取 GUI 里配置的流程 exe 路径覆盖（app_config.json → flow_exe_paths）。"""
    if not flow_key:
        return ""
    try:
        from ..settings import manager as _cfg_manager
        # force=True：每次流程执行都读盘上的最新配置，外部/GUI 修改即时生效
        paths = _cfg_manager.load(force=True).get("flow_exe_paths") or {}
        return str(paths.get(flow_key) or "").strip()
    except Exception:
        return ""


def _resolve_exe_path(cfg: Dict[str, Any], flow_key: str = "", logger: Optional[logging.Logger] = None) -> Optional[str]:
    """解析流程 exe 路径。

    顺序：exe_from_game（apps 里该游戏"本体"路径 _discovery.game_exe_path）
    → exe_from_app（apps 里该游戏的启动路径，GUI 可改）
    → flow_exe_paths 覆盖（独立程序如 MiniMax，GUI 可改，无默认值）
    → exe_path / exe_glob（flow_configs 遗留机制，其他流程仍在用）。

    exe_from_game 与 exe_from_app 分开的原因：原神的 apps.launcher 是 BetterGI（一条龙
    脚本），而每月流程需要直接启动游戏本体 YuanShen.exe；发现模块会把游戏本体路径写在
    _discovery.game_exe_path，这里直接复用，避免在 flow_configs 里硬编码绝对路径。
    """
    game_app = str(cfg.get("exe_from_game") or "").strip()
    if game_app:
        try:
            from ..settings import APPS
            game_path = str(
                ((APPS.get(game_app) or {}).get("_discovery") or {}).get("game_exe_path") or ""
            ).strip()
        except Exception:
            game_path = ""
        if game_path and os.path.exists(game_path):
            return game_path
        if logger:
            logger.warning(
                f"[{flow_key}] apps.{game_app}._discovery.game_exe_path 未配置或路径不存在"
                f"（当前: {game_path or '空'}），请先在设置里重新发现该游戏本体路径"
            )

    app_key = str(cfg.get("exe_from_app") or "").strip()
    if app_key:
        try:
            from ..settings import APPS
            app_path = str((APPS.get(app_key) or {}).get("launcher") or "").strip()
        except Exception:
            app_path = ""
        if app_path and os.path.exists(app_path):
            return app_path
        if logger:
            logger.warning(
                f"[{flow_key}] apps.{app_key}.launcher 未配置或路径不存在（当前: {app_path or '空'}），"
                "请在「路径配置」页填写该游戏程序路径"
            )
    override = _read_flow_exe_override(flow_key)
    if override:
        # 支持精确路径与通配符（如带版本号的程序名，自动取最新匹配）
        candidates = sorted(glob.glob(override), key=os.path.getmtime, reverse=True)
        if candidates:
            return candidates[0]
        if logger:
            logger.warning(f"[{flow_key}] GUI 配置的 exe 路径无效: {override}")
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


def _stop_requested(stop_check: Optional[Callable[[], bool]]) -> bool:
    """流程内统一的停止标志检查。stop_check 为 None 时永不停止（兼容旧调用方）。"""
    if stop_check is None:
        return False
    try:
        return bool(stop_check())
    except Exception:
        return False


def _sleep_interruptible(
    seconds: float,
    stop_check: Optional[Callable[[], bool]] = None,
    step: float = 0.25,
) -> bool:
    """可被停止标志打断的睡眠。返回 False 表示期间收到了停止请求。

    用于替换流程启动阶段的长 time.sleep，使 GUI「停止」能在约 step 秒内生效，
    而不是等整个等待跑完（历史问题：45 秒窗口轮询期间点停止完全无响应）。
    """
    remaining = float(seconds)
    while remaining > 0:
        if _stop_requested(stop_check):
            return False
        time.sleep(min(step, remaining))
        remaining -= step
    return not _stop_requested(stop_check)


def _wait_window_by_pid(
    pid: int,
    timeout: int = 45,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Optional[int]:
    end = time.time() + timeout
    while time.time() < end:
        if _stop_requested(stop_check):
            return None
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
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[Optional[int], Optional[int]]:
    end = time.time() + timeout
    while time.time() < end:
        if _stop_requested(stop_check):
            return None, None
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
    # 兜底：只找到陈旧窗口（如上轮失败残留的启动器，单实例新进程直接退出了）。
    # 不复用就会"每次都起新进程、每次都因 launched_after 错过"，无限未找到窗口。
    if not _stop_requested(stop_check) and (title_keywords or process_names):
        stale = _find_window_by_filters(
            title_keywords=title_keywords,
            process_names=process_names,
            launched_after=0.0,
        )
        if stale:
            if logger:
                logger.warning(
                    f"未找到本轮新窗口，复用已存在的陈旧窗口 HWND={stale[0]} PID={stale[1]}"
                )
            return stale
    return None, None


def _window_rect(hwnd: int) -> Optional[Rect]:
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r <= l or b <= t:
            return None
        # 最小化窗口的 GetWindowRect 返回 Windows 占位值 (-32000,-32000)，
        # 不拦住会让"屏幕坐标↔窗口局部坐标"换算凭空多出 32000 偏移，
        # 点击会飞到屏幕外（2026-10-26 实测 MiniMax Code 最小化时定位到 (32174,32948)）。
        if l <= -30000 and t <= -30000:
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
        # 最小化窗口的 rect 是占位值（_window_rect 判为 None），但它仍是可还原的
        # 目标，不能因此丢掉，否则流程会重复拉起新实例。
        if _window_rect(hwnd) or win32gui.IsIconic(hwnd):
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
        # 绕过 Windows 前台锁限制：模拟 ALT 键让系统认为用户在操作
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
        win32gui.SetForegroundWindow(hwnd)
        # 先设 TOPMOST 压过可能也是 TOPMOST 的启动器窗口，再立即取消 TOPMOST
        # 这样 CJ 窗口被提到最上层，但不会一直遮挡后续其他窗口
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        except Exception:
            pass
    except Exception:
        pass


def _locate_template_on_screen(
    template_path: str,
    region: Tuple[int, int, int, int],
    scales,
    confidences,
    throttle_sec: float = 0.1,
) -> Optional[Tuple[int, int]]:
    tpl0 = _get_cached_template(template_path)
    if tpl0 is None:
        return None

    # 单次截图复用于全部 scale/conf 组合（抄 ok-script frame 复用，避免每次都全屏截图）
    try:
        screenshot = pyautogui.screenshot(region=region)
    except OSError:
        return None
    except Exception:
        screenshot = None
    if screenshot is None:
        return None
    for scale in scales:
        tpl = tpl0
        if scale != 1.0:
            nw = max(8, int(tpl0.width * scale))
            nh = max(8, int(tpl0.height * scale))
            try:
                tpl = tpl0.resize((nw, nh))
            except Exception:
                continue
        # 优先 cv2 单次匹配（返回截图内坐标，需加 region 偏移回屏幕坐标）
        pt = _match_template_cv2_once(screenshot, tpl, confidences)
        if pt:
            return int(pt[0] + region[0]), int(pt[1] + region[1])
        for conf in confidences:
            try:
                box = pyautogui.locate(tpl, screenshot, confidence=float(conf))
                if box:
                    return int(box.left + box.width / 2 + region[0]), int(box.top + box.height / 2 + region[1])
            except Exception:
                try:
                    box = pyautogui.locate(tpl, screenshot)
                    if box:
                        return int(box.left + box.width / 2 + region[0]), int(box.top + box.height / 2 + region[1])
                except Exception:
                    pass
            time.sleep(throttle_sec)
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
    try:
        screen_center = _locate_template_on_screen(template_path, region, scales, confidences)
    except OSError as e:
        # 桌面临时不可用（锁屏/RDP/窗口刚关闭），视为未命中而非崩溃
        return None
    except Exception:
        screen_center = None
    if screen_center:
        return screen_center[0] - l, screen_center[1] - t

    try:
        screenshot = pyautogui.screenshot(region=region)
    except OSError:
        return None
    tpl0 = _get_cached_template(template_path)
    if tpl0 is None:
        return None
    for scale in scales:
        tpl = tpl0
        if scale != 1.0:
            nw = max(8, int(tpl0.width * scale))
            nh = max(8, int(tpl0.height * scale))
            tpl = tpl0.resize((nw, nh))
        pt = _match_template_cv2_once(screenshot, tpl, confidences)
        if pt:
            return pt
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
        from .ocr_click_tester import ocr_find_any_text

        # 单次截图+单次 OCR 匹配全部候选（抄 ok-script match=[...] 一次过滤）
        if search_region:
            result = ocr_find_any_text(
                target_texts=target_candidates,
                logger=logger,
                hwnd=None,
                region=search_region,
                min_confidence=min_confidence,
            )
        else:
            result = ocr_find_any_text(
                target_texts=target_candidates,
                logger=logger,
                hwnd=hwnd,
                min_confidence=min_confidence,
            )
        if not result:
            return None
        center = result["center"]
        if not hwnd:
            return center
        rect = _window_rect(hwnd)
        if not rect:
            # 目标窗口当前不可用（最小化/已关闭，rect 是 -32000 占位值或空）：
            # 无法把屏幕坐标换算成窗口局部坐标，此时给坐标必然错位。
            # 返回 None 让上层按"未匹配"重试（步骤会重新 focus/还原窗口）。
            return None
        l, t, _, _ = rect
        return (center[0] - l, center[1] - t)
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


def _post_click_window(hwnd: int, x_local: int, y_local: int) -> bool:
    """抄 ok-script PostMessageInteraction：后台点击，免抢焦点。

    返回 True=已投递，False=回退前台点击。只做最小实现：WM_LBUTTONDOWN/UP。
    """
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        lparam = (int(y_local) << 16) | (int(x_local) & 0xFFFF)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        time.sleep(0.02)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
        return True
    except Exception:
        return False


def _click_window_local(hwnd: int, x_local: int, y_local: int, clicks: int = 1, interval: float = 0.2, background: bool = False):
    """窗口内点击。默认前台真实输入（pyautogui），XM/CJ 等游戏只吃这个。

    background=True 才是 ok-script 式 PostMessage 后台点击，需按 step/cfg
    显式 opt-in；投递成功≠游戏响应，失败靠后续 confirm 重试兜底。
    """
    rect = _window_rect(hwnd)
    if not rect:
        return
    l, t, _, _ = rect
    if background:
        ok_all = True
        for _ in range(max(1, clicks)):
            if not _post_click_window(hwnd, int(x_local), int(y_local)):
                ok_all = False
                break
            if interval:
                time.sleep(interval)
        if ok_all:
            return
    _focus_window(hwnd)
    _pyautogui_click_guarded(l + int(x_local), t + int(y_local), clicks=clicks, interval=interval)


def _hover_window_local(hwnd: int, x_local: int, y_local: int, duration: float = 0.2):
    """把鼠标移到窗口内指定位置并停留（不点击）。

    用于只有悬停才展开的浮层菜单（MiniMax「每日签到」卡片）。pyautogui.moveTo
    同样受 fail-safe 保护，这里与点击一样转成 FailSafeAborted。
    """
    rect = _window_rect(hwnd)
    if not rect:
        return
    l, t, _, _ = rect
    _focus_window(hwnd)
    try:
        pyautogui.moveTo(l + int(x_local), t + int(y_local), duration=duration)
    except pyautogui.FailSafeException:
        raise FailSafeAborted("鼠标位于屏幕左上角(0,0)，已按用户紧急停止处理")


class FailSafeAborted(Exception):
    """pyautogui fail-safe 触发（鼠标精确位于 (0,0)）。

    pyautogui 把"鼠标顶在屏幕左上角"当作用户紧急停止信号。本项目的挂机流程
    窗口常贴屏幕角摆放，用户点完启动按钮随手把鼠标放到左上角就会误触发，
    表现为整个任务 traceback 崩溃、日志只有看不懂的堆栈（2026-09-29 CJ 实例）。
    转成此异常后在 task_batch_runner 收口成一条人话日志 + 优雅失败。
    """


_FAILSAFE_HINT = (
    "PyAutoGUI 紧急停止已触发：鼠标位于屏幕左上角 (0,0)。"
    "这是 pyautogui 的防失控保护（鼠标顶到左上角 = 请求停止自动化）。"
    "若是误触发（如点完启动按钮后鼠标恰好停在角落），请把鼠标移开屏幕角落后重新运行任务；"
    "若是有意停止，无需处理。"
)


def _pyautogui_click_guarded(x: int, y: int, clicks: int = 1, interval: float = 0.2, button: str = "left"):
    """pyautogui.click 的包装：fail-safe 触发时抛带说明的 FailSafeAborted。"""
    try:
        pyautogui.click(int(x), int(y), clicks=clicks, interval=interval, button=button)
    except pyautogui.FailSafeException as exc:
        logger = logging.getLogger("GameAutomation")
        logger.warning("[fail-safe] %s", _FAILSAFE_HINT)
        raise FailSafeAborted(_FAILSAFE_HINT) from exc


def _click_screen(x: int, y: int, clicks: int = 1, interval: float = 0.2):
    _pyautogui_click_guarded(int(x), int(y), clicks=clicks, interval=interval)


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
    1. 每 2 秒轮询检查原元素是否消失（直到 timeout_sec，不是单次检查）
       - 消失 → ✅ 成功
       - 超时仍在 → ❌ 失败
     2. 如果配置了 confirm_templates，继续确认下一状态模板已出现
         - 找到确认模板 → ✅ 成功
         - 未找到 → ❌ 失败
    local_region: 局部搜索区域，防止页面其他位置的同名文字干扰
    """
    import time as _time

    logger.debug(f"[{step_name}] [消失型确认] 最长等待 {timeout_sec}秒（每 2 秒轮询）...")

    # 轮询直到元素消失或超时。早期实现只在 +3s 单次检查——Qoder 领取成功后的
    # 弹窗要 ~30s 才关闭，单次检查必误判"元素仍存在"→ 假失败（2026-10-21 事故：
    # 服务端 7.5s 前就 CLAIMED，确认却连报 4 次"按钮还在"）。
    deadline = _time.time() + max(int(timeout_sec), 1)
    still_exists = True
    while True:
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
            break
        if _time.time() >= deadline:
            logger.warning(
                f"[{step_name}] ❌ [消失型] 目标元素仍存在（已等待 {timeout_sec}s）→ 可能未点击成功")
            return False, f"元素仍存在（{timeout_sec}s 未消失）"
        _time.sleep(2.0)

    if confirm_templates:
        remaining_timeout = max(int(deadline - _time.time()), 2)
        if _wait_any_template(hwnd, confirm_templates, remaining_timeout, scales, confidences):
            logger.info(f"[{step_name}] ✅ [消失型] 原元素消失且确认模板出现 → 操作成功")
            return True, "元素消失且确认模板匹配"
        logger.warning(f"[{step_name}] ❌ [消失型] 原元素已消失，但未等到确认模板出现")
        return False, "元素已消失但确认模板未出现"

    logger.info(f"[{step_name}] ✅ [消失型] 目标元素已消失 → 操作成功")
    return True, "元素已消失"


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
    1. 等待界面响应（wait_time 为首次检查延迟）
    2. 每 2 秒轮询局部区域，直到 timeout_sec（不是单次检查）
       - 出现新文字 → ✅ 成功
    3. 如果有 confirm_templates，也检查模板匹配
       - 找到模板 → ✅ 成功
    4. 都不满足 → ❌ 失败
    local_region: 局部搜索区域，防止页面其他位置的同名文字干扰

    2026-10-25 修正：原实现只在 +3s 检查一次 OCR，timeout_sec 只作用于模板，
    而 WorkBuddy 领取接口要 ~6s 才回 → 连续误报"未检测到文字变化"。
    """
    import time as _time

    logger.debug(f"[{step_name}] [文字变化确认] 首次等待 {wait_time}秒，最长 {timeout_sec}秒...")
    logger.info(f"[{step_name}] [文字变化] 期望: \"{target_text_before}\" → \"{expected_text_after}\"")
    deadline = _time.time() + max(int(timeout_sec), 1)
    _time.sleep(max(0, min(int(wait_time), max(int(timeout_sec), 1))))

    while True:
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
        if _time.time() >= deadline:
            break
        _time.sleep(2.0)

    # 策略B：使用确认模板匹配（OCR 轮询耗尽剩余时间后再查）
    if confirm_templates:
        remaining_timeout = max(int(deadline - _time.time()), 2)
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


def _process_is_elevated(pid: int) -> Optional[bool]:
    """读进程令牌 TokenElevation 判断是否管理员运行；读不到返回 None（不阻断）。"""
    if not pid or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        TOKEN_QUERY = 0x0008
        TokenElevation = 20
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            tok = wintypes.HANDLE()
            if not ctypes.windll.advapi32.OpenProcessToken(
                    h, TOKEN_QUERY, ctypes.byref(tok)):
                return None
            try:
                elev = wintypes.DWORD()
                ok = ctypes.windll.advapi32.GetTokenInformation(
                    tok, TokenElevation, ctypes.byref(elev),
                    ctypes.sizeof(elev), ctypes.byref(wintypes.DWORD()))
                return bool(elev.value) if ok else None
            finally:
                ctypes.windll.kernel32.CloseHandle(tok)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


def _uipi_block_reason(pid: int) -> str:
    """本程序权限低于目标进程时的可行动报错；不会拦则返回空串。

    UIPI：普通完整性进程向管理员进程窗口发合成输入（SendInput/mouse_event）
    会被 Windows 静默拦截——现象是点击无反应、模板匹配超时，极易误判成
    "UI 改版/坐标失效"。2026-10-20 实测：白荆游戏提权运行，普通进程点击全被吞，
    提权后同坐标点击立刻生效。
    """
    self_elev = _process_is_elevated(os.getpid())
    target_elev = _process_is_elevated(pid)
    if self_elev is False and target_elev is True:
        return (
            "目标程序正以管理员身份运行，而本程序是普通权限——Windows(UIPI) 会"
            "静默拦截所有合成点击（表现为点击无反应/匹配超时）。请用管理员身份"
            "重启本程序（start_gui_admin.bat / 00_START_HERE_GUI_ADMIN.bat）"
        )
    return ""


def _protected_pids() -> set:
    """当前进程及其祖先 PID——清理时绝不误杀自己所在的进程树。"""
    pids = set()
    try:
        proc = psutil.Process(os.getpid())
    except Exception:
        return pids
    while proc is not None:
        if proc.pid in pids:
            break
        pids.add(proc.pid)
        try:
            proc = proc.parent()
        except Exception:
            break
    return pids


def _iter_processes_by_exact_name(name: str) -> List[psutil.Process]:
    target = str(name or "").strip().lower()
    if not target:
        return []
    found: List[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        try:
            proc_name = str(proc.info.get("name") or "").strip().lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if proc_name == target:
            found.append(proc)
    return found


def close_flow_processes(
    logger: logging.Logger,
    flow_key: str,
    pid: Optional[int] = None,
    process_names: Optional[List[str]] = None,
) -> None:
    """关闭流程相关进程：先按 PID 杀整棵进程树，再按进程名兜底清理残留。

    Electron 类应用（TRAE / MiniMax Code / WorkBuddy）常由启动器拉起子进程，
    只 terminate 启动 PID 会留下真实窗口进程，因此必须按 window_process_names 兜底。
    """
    from .process_supervisor import ProcessSupervisor

    protected = _protected_pids()

    if pid and int(pid) not in protected:
        try:
            ProcessSupervisor.safe_kill_process(int(pid), timeout=3)
            logger.info(f"[{flow_key}] 已关闭流程进程树 PID={pid}")
        except Exception as exc:
            logger.warning(f"[{flow_key}] 关闭流程进程 PID={pid} 失败: {exc}")

    for name in process_names or []:
        for proc in _iter_processes_by_exact_name(name):
            if proc.pid in protected:
                logger.info(f"[{flow_key}] 跳过受保护进程 {name} PID={proc.pid}（当前进程树）")
                continue
            try:
                ProcessSupervisor.safe_kill_process(proc.pid, timeout=3)
                logger.info(f"[{flow_key}] 已清理残留进程 {name} PID={proc.pid}")
            except Exception as exc:
                logger.warning(f"[{flow_key}] 清理残留进程 {name} PID={proc.pid} 失败: {exc}")


def run_flow(
    logger: logging.Logger,
    flow_key: str,
    stop_after_step: Optional[str] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """
    增强版流程执行器，支持OCR+模板匹配联合识别。

    特性：
    - 自动启用OCR备选（XM流程默认启用）
    - 详细的日志输出（包括识别方式和置信度）
    - 失败时输出屏幕文字快照便于调试
    - 可配置的步骤级目标文字
    - 支持在指定步骤确认成功后提前返回
    - stop_check：可选停止标志回调。传入后启动等待 / 窗口等待 / 步骤搜索
      均可被中断，GUI「停止」不再需要等 45 秒轮询超时。传 None 则行为与旧版一致。
    """
    if _stop_requested(stop_check):
        return False, f"[{flow_key}] 用户已请求停止，未启动流程"

    cfg = _load_flow_config(flow_key)
    if not cfg:
        return False, f"[{flow_key}] 未找到流程配置"
    if not cfg.get("enabled", True):
        return False, f"[{flow_key}] 流程已禁用"

    close_on_exit = bool(cfg.get("close_process_on_finish", False))
    close_on_fail = bool(cfg.get("close_process_on_fail", True))

    exe_path = _resolve_exe_path(cfg, flow_key, logger)
    if not exe_path:
        return False, f"[{flow_key}] 未找到可执行文件——请在「路径配置」页填写该流程的程序路径（游戏类流程请填游戏本体 exe）"

    window_title_keywords = cfg.get("window_title_keywords", []) or []
    window_process_names = cfg.get("window_process_names", []) or []

    # 先复用已存在的窗口（如残留/用户已开的启动器），没有再拉新进程。
    # 默认关闭（游戏类流程复用陈旧窗口状态不可预期）；启动器类流程可配 reuse_existing_window。
    launch_ts = time.time()
    pid = None
    reused_window = False
    if cfg.get("reuse_existing_window", False) and (window_title_keywords or window_process_names):
        existing = _find_window_by_filters(
            title_keywords=window_title_keywords,
            process_names=window_process_names,
            launched_after=0.0,
        )
        if existing:
            pid = existing[1]
            launch_ts = 0.0
            reused_window = True
            logger.info(f"[{flow_key}] 发现已存在的窗口，直接复用 HWND={existing[0]} PID={pid}，跳过启动")
    if pid is None:
        launch_ts = time.time()
        pid = _launch_maybe_elevated(logger, exe_path)
        if not pid:
            return False, f"[{flow_key}] 启动失败"
        logger.info(f"[{flow_key}] 已启动: {exe_path} PID={pid}")

    def _abort_for_stop() -> Tuple[bool, str]:
        """启动阶段收到停止请求：按既有 close_on_exit 规则收尾，避免残留进程。"""
        if close_on_exit:
            close_flow_processes(logger, flow_key, pid, window_process_names)
        logger.info(f"[{flow_key}] 用户已请求停止，终止流程")
        return False, f"[{flow_key}] 用户已请求停止"

    # 复用已存在窗口时跳过启动等待（窗口已经在那了，步骤自带超时）
    if not reused_window and not _sleep_interruptible(int(cfg.get("startup_wait_sec", 10)), stop_check):
        return _abort_for_stop()

    hwnd, window_pid = _wait_window_for_flow(
        pid,
        timeout=45,
        title_keywords=window_title_keywords,
        process_names=window_process_names,
        launched_after=launch_ts,
        logger=logger,
        stop_check=stop_check,
    )
    if not hwnd:
        if _stop_requested(stop_check):
            return _abort_for_stop()
        # 失败也按 close_on_fail 清理（WuwaLauncher finish=false 但 fail 默认 true）。
        # 否则残留启动器进程导致下一轮 launched_after 永远错过，变成无限未找到窗口。
        if close_on_exit or close_on_fail:
            close_flow_processes(logger, flow_key, pid, window_process_names)
        return False, f"[{flow_key}] 未找到窗口"
    if window_pid and window_pid != pid:
        logger.info(f"[{flow_key}] 窗口由新进程承载: PID {pid} -> {window_pid}")
        pid = window_pid

    # UIPI 护栏：目标进程管理员运行而本程序普通权限时，合成点击会被 Windows
    # 静默拦截（2026-10-20 白荆实测）。不提前拦的话每一步都匹配超时，报错信息
    # 全是"未匹配模板"，会把权限问题误导成 UI 改版。
    block_reason = _uipi_block_reason(pid)
    if block_reason:
        logger.error(f"[{flow_key}] {block_reason}")
        if close_on_exit or close_on_fail:
            close_flow_processes(logger, flow_key, pid, window_process_names)
        return False, f"[{flow_key}] {block_reason}"

    hwnd = _refresh_flow_window(
        pid,
        current_hwnd=hwnd,
        title_keywords=window_title_keywords,
    ) or hwnd

    # 启动后按配置最大化窗口：某些应用（如 Trae）在小窗口下侧边栏不渲染
    # 目标卡片（签到入口），需放大后才可见。默认关闭，不影响其他流程。
    if cfg.get("maximize_window_on_start", False):
        try:
            win32gui.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            time.sleep(0.5)
            hwnd = _refresh_flow_window(
                pid,
                current_hwnd=hwnd,
                title_keywords=window_title_keywords,
            ) or hwnd
            logger.info(f"[{flow_key}] 已按配置最大化窗口 HWND={hwnd}")
        except Exception as exc:
            logger.warning(f"[{flow_key}] 最大化窗口失败: {exc}")

    scales = cfg.get("match_scales", [1.0, 1.25, 0.8])
    confidences = cfg.get("match_confidences", [0.9, 0.85, 0.8])
    steps = cfg.get("steps", [])
    if not steps:
        if close_on_exit:
            close_flow_processes(logger, flow_key, pid, window_process_names)
        return False, f"[{flow_key}] 配置中无步骤"

    # 判断是否启用OCR备选（XM流程默认启用，CJ流程不启用）
    enable_ocr = cfg.get("enable_ocr_fallback", flow_key == "XM")

    step_attempts: Dict[int, int] = {}
    step_local_region: Dict[int, Optional[Tuple[int, int, int, int]]] = {}
    i = 0
    flow_ok = False
    try:
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
            search_interval = float(step.get("search_interval_sec", 0.5))
            post_delay = float(step.get("post_delay_sec", 1.0))
            clicks = int(step.get("clicks", 1))
            no_click = bool(step.get("no_click", False))
            hover = bool(step.get("hover", False))
            # 后台点击需 step/cfg 显式 opt-in（XM/CJ 默认前台，PostMessage 点不上）
            use_background = bool(step.get("background_click", cfg.get("background_click", False)))
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
                # 步骤搜索期间响应停止请求：直接退出，由 finally 按既有 close_on_exit 规则收尾
                if _stop_requested(stop_check):
                    logger.info(f"[{flow_key}] 用户已请求停止，中断步骤{step_no}「{name}」")
                    return False, f"[{flow_key}] 用户已请求停止"
                if action == "wait_process":
                    proc = _find_process_by_exact_name(process_name)
                    if proc:
                        match_method = "process"
                        if bool(step.get("switch_window_to_process", False)):
                            switched_hwnd = _wait_window_by_pid(
                                proc.pid,
                                timeout=min(max(timeout_sec, 5), 45),
                                stop_check=stop_check,
                            )
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

                    # 每次匹配前置顶窗口，防止其他窗口（如游戏退出后弹出的启动器）遮挡
                    _focus_window(hwnd)

                    # 自带内部循环的动作跳过模板匹配，直接进分发
                    if action in ("zzz_shop_buy", "monthly_exchange", "nikki_enter_game",
                                  "starrail_enter_main", "genshin_enter_main",
                                  "qoder_open_campaign", "qoder_verify_claim", "wool_verify_log"):
                        break

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
                time.sleep(search_interval)

            matched = (
                (action == "wait_process" and match_method == "process")
                or action in ("zzz_shop_buy", "monthly_exchange", "nikki_enter_game",
                              "starrail_enter_main", "genshin_enter_main",
                              "qoder_open_campaign", "qoder_verify_claim", "wool_verify_log")
                or center is not None
            )
            if not matched:
                if optional_if_missing:
                    logger.info(f"[{flow_key}] 可选步骤{step_no}未出现，跳过: {name}")
                    step_attempts[i] = 0
                    step_local_region.pop(i, None)
                    i += 1
                    continue
                step_attempts[i] = step_attempts.get(i, 0) + 1
                if step_attempts[i] > max_attempts:
                    ocr_failed = None
                    try:
                        from .ocr_click_tester import ocr_window_content
                        results = ocr_window_content(logger, hwnd=hwnd)
                        if results:
                            logger.warning(f"[{flow_key}] 步骤失败时屏幕文字:")
                            for r in results[:10]:
                                logger.warning(f"  - \"{r['text']}\" (置信度: {r['confidence']:.2f}) @ {r['center']}")
                        ocr_failed = results
                    except Exception:
                        pass
                    # 失败取证：截图 + 当帧文字 + 结构化落库（复用上面的 OCR 结果，
                    # 不重复识别），供 tools/flow_failure_report.py 事后聚合分析
                    try:
                        from .flow_diagnostics import record_failure

                        record_failure(
                            logger,
                            flow_key=flow_key,
                            step=name,
                            error="未匹配模板/OCR（超过重试上限）",
                            hwnd=hwnd,
                            retry_count=step_attempts[i],
                            ocr_results=ocr_failed,
                            extra={
                                "step_no": step_no,
                                "step_action": action,
                                "target_texts": target_texts if step_enable_ocr else [],
                                "template": template or "",
                                "max_attempts": max_attempts,
                                "kind": "flow_step",
                            },
                        )
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
                if hover:
                    # 悬停型入口：只移动鼠标不点击（浮层菜单/预览卡片）
                    if search_scope == "screen":
                        pyautogui.moveTo(center[0], center[1], duration=0.2)
                    else:
                        _hover_window_local(hwnd, center[0], center[1])
                    logger.info(f"[{flow_key}] 已悬停: ({center[0]}, {center[1]}) [方式: {match_method}]")
                elif no_click:
                    logger.info(f"[{flow_key}] 仅确认不点击: {name}")
                else:
                    if search_scope == "screen":
                        _click_screen(center[0], center[1], clicks=clicks)
                    else:
                        _click_window_local(hwnd, center[0], center[1], clicks=clicks, background=use_background)
                    logger.info(f"[{flow_key}] 已点击: ({center[0]}, {center[1]}) [方式: {match_method}][点击: {'后台' if use_background else '前台'}]")

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
            elif action == "zzz_shop_buy":
                # 绝区零后勤商店采购：内部完成页签切换 + 逐商品「点商品→确认」循环
                from .zzz_monthly_shop import buy_shop_groups

                shop_ok, shop_msg = buy_shop_groups(
                    logger,
                    hwnd,
                    step,
                    scales=scales,
                    confidences=confidences,
                    stop_check=stop_check,
                )
                if not shop_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({shop_msg})"
                logger.info(f"[{flow_key}] 步骤{step_no}采购完成: {shop_msg}")
            elif action == "monthly_exchange":
                # 商店 → 兑换商店 → 目标页签 每月兑换（绝区零信号残响 / 崩铁余烬兑换共用）
                from .monthly_exchange import buy_monthly_exchange

                exchange_ok, exchange_msg = buy_monthly_exchange(
                    logger,
                    hwnd,
                    step,
                    scales=scales,
                    confidences=confidences,
                    stop_check=stop_check,
                )
                if not exchange_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({exchange_msg})"
                logger.info(f"[{flow_key}] 步骤{step_no}每月兑换完成: {exchange_msg}")
            elif action == "nikki_enter_game":
                # 无限暖暖：启动器 → 登录页 → 加载 → 主界面（聊天气泡特征）
                from .nikki_launcher import enter_main_interface

                nikki_ok, nikki_msg, game_hwnd, game_pid = enter_main_interface(
                    logger,
                    hwnd,
                    step,
                    scales,
                    confidences,
                    stop_check=stop_check,
                )
                if not nikki_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({nikki_msg})"
                if game_hwnd:
                    hwnd = game_hwnd
                    pid = game_pid or pid
                logger.info(f"[{flow_key}] 步骤{step_no}进入主界面成功: {nikki_msg}")
            elif action == "qoder_open_campaign":
                # 头像 → 权益中心：活动弹窗不会自动出现，必须走常驻入口
                from .qoder_claim import open_campaign

                open_ok, open_msg = open_campaign(
                    logger,
                    hwnd,
                    scales,
                    confidences,
                    timeout_sec=int(step.get("timeout_sec", 20)),
                    stop_check=stop_check,
                )
                if not open_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({open_msg})"
                logger.info(f"[{flow_key}] 步骤{step_no}权益中心已打开: {open_msg}")
            elif action == "qoder_verify_claim":
                # Qoder 领取结果以 App 自身日志为准；UI 空跑不再算成功
                from .qoder_claim import verify_claim

                claim_ok, claim_msg = verify_claim(
                    logger,
                    timeout_sec=int(step.get("timeout_sec", 60)),
                )
                if not claim_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({claim_msg})"
                logger.info(f"[{flow_key}] 步骤{step_no}领取状态确认: {claim_msg}")
            elif action == "wool_verify_log":
                # MiniMax/TRAE 以接口日志为准，避免 UI 确认误报
                from .wool_claim_logs import verify as verify_wool_log

                wool_ok, wool_msg = verify_wool_log(
                    logger,
                    str(step.get("source") or ""),
                    hwnd,
                    scales,
                    confidences,
                    step,
                    int(step.get("timeout_sec", 30)),
                )
                if not wool_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({wool_msg})"
                logger.info(f"[{flow_key}] 步骤{step_no}日志确认成功: {wool_msg}")
            elif action == "starrail_enter_main":
                # 崩铁：复刻 March7th 的进入方法（点击进入 → 主界面特征连续命中）
                from .starrail_launcher import enter_main_interface

                sr_ok, sr_msg, game_hwnd, game_pid = enter_main_interface(
                    logger,
                    hwnd,
                    step,
                    scales,
                    confidences,
                    stop_check=stop_check,
                )
                if not sr_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({sr_msg})"
                if game_hwnd:
                    hwnd = game_hwnd
                    pid = game_pid or pid
                logger.info(f"[{flow_key}] 步骤{step_no}进入主界面成功: {sr_msg}")
            elif action == "genshin_enter_main":
                # 原神：复刻 BetterGI GameLoading 的进入方法（点击进入 → 空月祝福 → 派蒙图标）
                from .genshin_launcher import enter_main_interface

                gi_ok, gi_msg, game_hwnd, game_pid = enter_main_interface(
                    logger,
                    hwnd,
                    step,
                    scales,
                    confidences,
                    stop_check=stop_check,
                )
                if not gi_ok:
                    return False, f"[{flow_key}] 步骤失败: {name} ({gi_msg})"
                if game_hwnd:
                    hwnd = game_hwnd
                    pid = game_pid or pid
                logger.info(f"[{flow_key}] 步骤{step_no}进入主界面成功: {gi_msg}")
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
                close_on_exit = False
                logger.info(f"[{flow_key}] 已达到提前返回步骤: {stop_after_step}，保持流程进程继续运行")
                return True, f"[{flow_key}] 已完成到步骤: {stop_after_step}"

            time.sleep(post_delay)
            i += 1
        flow_ok = True
    finally:
        if close_on_exit and (flow_ok or close_on_fail):
            close_flow_processes(logger, flow_key, pid, window_process_names)
        elif close_on_exit:
            logger.info(f"[{flow_key}] 流程未成功，按配置保留进程(close_process_on_fail=false)")
    return True, f"[{flow_key}] 流程执行成功"