import ctypes
import glob
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil
import pyautogui
import win32gui
import win32api
import win32con
import win32process
from PIL import Image
from ..settings.paths import BASE_DIR, CACHE_DIR, ensure_flow_config_path

# OCR 可用性由 `ocr_click_tester._ocr_scan` 内部判定：onnxocr v6 是主引擎，
# 只有它返回空/异常时才尝试可选的 Tesseract 兜底。这里**不要**按
# `pytesseract` 包是否存在做门控 —— 2026-11-07 复核发现旧代码用
# `HAS_OCR = bool(pytesseract)` 卡住整个文字定位函数，可下游
# `ocr_find_any_text` 走的是 RapidOCR，等于没装这个 Python 包就把主 OCR
# 一起关掉了（正常安装时 pytesseract 是必装包，所以从未暴露）。


Rect = Tuple[int, int, int, int]
_LOGGER = logging.getLogger("GameAutomation")

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


def _match_template_cv2_once(
    screenshot: Any,
    tpl: Any,
    confidences,
    use_color: bool = False,
) -> Optional[Tuple[int, int]]:
    """单张截图上对单个尺度模板匹配一次，返回截图内中心坐标。

    同一张 matchTemplate 结果只取全局最大值，与最低阈值比一次即可
    （等价于旧高→低逐阈值循环，但少做 N-1 次 minMaxLoc）。

    use_color=True 走 BGR 三通道匹配：灰度过不了的红/蓝同形图标
    （异环质感骰子 vs 捏造骰子，灰度互匹配 0.976、彩色 0.689）靠这个分开。
    代价是单次匹配慢 2~3 倍，而且对渲染色差更敏感；**默认关闭**，
    只有 step/item 显式配 color_match 才开。
    """
    try:
        import cv2
        import numpy as np

        def _to_array(src):
            if hasattr(src, "convert"):
                return np.asarray(src.convert("RGB"))
            arr = np.asarray(src)
            # 非 PIL 输入按 OpenCV 约定当 BGR，转成 RGB 与 PIL 分支统一
            if arr.ndim == 3 and arr.shape[2] >= 3:
                return arr[:, :, 2::-1] if arr.shape[2] == 3 else cv2.cvtColor(arr, cv2.COLOR_BGRA2RGB)
            return arr

        scr_arr = _to_array(screenshot)
        tpl_arr = _to_array(tpl)
        if use_color:
            scr_img = scr_arr
            tpl_img = tpl_arr
            if scr_img.ndim != 3 or tpl_img.ndim != 3:
                return None   # 彩色匹配遇到灰度图直接放弃，不瞎猜
            if scr_img.shape[2] != tpl_img.shape[2]:
                return None
        else:
            scr_img = cv2.cvtColor(scr_arr, cv2.COLOR_RGB2GRAY) if scr_arr.ndim == 3 else scr_arr
            tpl_img = cv2.cvtColor(tpl_arr, cv2.COLOR_RGB2GRAY) if tpl_arr.ndim == 3 else tpl_arr
        if tpl_img.shape[0] > scr_img.shape[0] or tpl_img.shape[1] > scr_img.shape[1]:
            return None
        res = cv2.matchTemplate(scr_img, tpl_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        try:
            min_conf = float(min(float(c) for c in confidences))
        except Exception:
            return None
        if max_val >= min_conf:
            h, w = tpl_img.shape[:2]
            return int(max_loc[0] + w // 2), int(max_loc[1] + h // 2)
    except Exception as exc:
        _LOGGER.debug("[模板] 匹配异常: %s", exc)
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
    path = ensure_flow_config_path()
    if not os.path.exists(path):
        logging.getLogger(__name__).warning(f"配置文件不存在: {path}")
        return None
    with open(path, "r", encoding="utf-8") as f:
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


# 多个游戏共用的启动器进程名：单靠进程名无法区分是哪个游戏的启动器，
# 必须再核对窗口标题才能确认归属。
# 2026-11-02 实锤：暖暖流程把鸣潮的启动器窗口（launcher_main.exe，标题='鸣潮'）
# 当成自己的接管了 → 把鸣潮的「进入游戏」当暖暖的点了，等 5 分钟暖暖游戏窗口
# 一直没出现。xstarter.exe 是叠纸/暖暖专属，不在此列（暖暖窗口进程=xstarter.exe）。
GENERIC_LAUNCHER_PROCESS_NAMES = frozenset({
    "launcher.exe",
    "launcher_main.exe",
    "client.exe",
    "game.exe",
    "start.exe",
    "update.exe",
    "krlauncher.exe",
})


def _process_name_of(pid: Optional[int]) -> str:
    """取窗口所属进程的进程名（小写）；取不到返回空串。"""
    if not pid:
        return ""
    try:
        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        return ""


def _is_window_owned(
    pid: Optional[int],
    title_matched: bool,
    title_keywords: Optional[List[str]],
) -> bool:
    """窗口归属判定：通用启动器进程名 + 标题不命中本流程 → 不认这个窗口。

    2026-11-02 实锤：暖暖流程（title_keywords=['无限暖暖','Infinity Nikki']）
    把鸣潮启动器窗口（launcher_main.exe，标题='鸣潮'）当自己的接管了 →
    把鸣潮的「进入游戏」当暖暖的点了，等 5 分钟暖暖游戏窗口一直没出现。
    游戏专属进程名（如 xstarter.exe / X6Game-Win64-Shipping.exe）本身就是归属
    证据，不要求标题命中。
    """
    if not title_keywords or title_matched:
        return True
    return _process_name_of(pid) not in GENERIC_LAUNCHER_PROCESS_NAMES


def _find_window_by_filters(
    title_keywords: Optional[List[str]] = None,
    process_names: Optional[List[str]] = None,
    launched_after: float = 0.0,
    logger: Optional[logging.Logger] = None,
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
        # 归属校验：进程名是"多游戏通用启动器名"时，窗口标题必须命中本流程的
        # title_keywords，否则一律不认（防止把别的游戏的启动器当自己的）。
        if process_names and title_keywords and not title_matched:
            if not _is_window_owned(pid, title_matched, title_keywords):
                if logger:
                    logger.warning(
                        f"窗口归属不符已跳过: HWND={hwnd} PID={pid} "
                        f"进程={_process_name_of(pid)!r} 标题={_window_title(hwnd)!r} "
                        f"（通用启动器名需标题命中 {list(title_keywords)}）"
                    )
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
            logger=logger,
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
            logger=logger,
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


def _window_is_dead(hwnd: Optional[int]) -> bool:
    """窗口句柄是否已经失效（窗口被销毁了）。

    2026-11-04 实锤（异环 NTE_MONTHLY）：更新完游戏自己起来并重建了窗口，旧的
    HWND 直接失效，但流程还攥着它不放 → SetForegroundWindow 报「无效的窗口句柄
    (1400)」、OCR 扫 0 个区域（_ocr_scan 拒绝回退全屏）→ 一步空转 11 分钟。
    句柄真死时要重新按进程名找窗口，不能只按 pid 找（游戏窗口属于另一个进程）。
    """
    if not hwnd:
        return True
    try:
        return not win32gui.IsWindow(hwnd)
    except Exception:
        return True


def _recover_flow_window(
    pid: Optional[int],
    hwnd: Optional[int],
    title_keywords: Optional[List[str]],
    process_names: Optional[List[str]],
    logger: logging.Logger,
    flow_key: str,
) -> Tuple[Optional[int], Optional[int]]:
    """追踪的窗口句柄失效时，按流程声明的进程名/标题重新找一个能用的窗口。

    只在句柄**真的死了**时触发（不是最小化、不是窗口还在），
    避免把正常的窗口切换误判成故障。返回 (新 hwnd, 新 pid)；无需恢复时返回 (None, None)。
    """
    if not _window_is_dead(hwnd):
        return None, None
    if not process_names and not title_keywords:
        return None, None
    alt = _find_window_by_filters(
        title_keywords=title_keywords,
        process_names=process_names,
        launched_after=0.0,
        logger=logger,
    )
    if not alt or alt[0] == hwnd:
        return None, None
    logger.info(
        f"[{flow_key}] 目标窗口已失效（HWND={hwnd}），按进程名重新接管到 HWND={alt[0]} PID={alt[1]}"
    )
    return alt[0], alt[1]


def _focus_window(hwnd: int):
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        # 绕过 Windows 前台锁限制：模拟 ALT 键让系统认为用户在操作
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            _LOGGER.debug("[窗口] 模拟 Alt 键失败: %s", exc)
        win32gui.SetForegroundWindow(hwnd)
        # 先设 TOPMOST 压过可能也是 TOPMOST 的启动器窗口，再立即取消 TOPMOST
        # 这样 CJ 窗口被提到最上层，但不会一直遮挡后续其他窗口
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        except Exception as exc:
            _LOGGER.debug("[窗口] SetWindowPos 置顶失败: %s", exc)
    except Exception as exc:
        _LOGGER.debug("[窗口] SetForegroundWindow 失败: %s", exc)


_SCANCODE_VK_ALIASES = {
    "esc": 27, "escape": 27, "enter": 13, "return": 13, "tab": 9,
    "space": 32, "backspace": 8,
    # 修饰键：恶名狩猎闪避键 = shift（2026-11-07 恶名狩猎换扫描码时补，
    # 否则 press_key_scancode("shift") 直接 ValueError）
    "shift": win32con.VK_SHIFT, "ctrl": win32con.VK_CONTROL, "alt": win32con.VK_MENU,
    **{f"f{i}": 111 + i for i in range(1, 13)},  # VK_F1=112 ... VK_F12=123
}


def _scancode_vk(key: str) -> int:
    """按键名 → 虚拟键码（press_key_scancode 与需要 down/up 原语的调用方共用）。"""
    k = str(key).strip().lower()
    vk = _SCANCODE_VK_ALIASES.get(k)
    if vk is None and len(k) == 1 and k.isalnum():
        vk = ord(k.upper())
    if vk is None:
        raise ValueError(f"无法把按键 {key!r} 映射成虚拟键码")
    return vk


# 游戏内按键的按住时长：瞬时虚拟键事件会被 Unity/UE 一帧漏掉。
SCANCODE_KEY_HOLD_SEC = 0.15


def press_key_scancode(key: str, hold_sec: float = SCANCODE_KEY_HOLD_SEC) -> None:
    """发带硬件扫描码的按键事件；游戏内 UI 只认这个。

    2026-11-05 绝区零实机逐项对比：双方提权、UIPI 无阻碍、前台窗口就是游戏时，
    `pyautogui.press` / `keyDown` 的 ESC、F3 完全无效，只有
    `win32api.keybd_event(vk, MapVirtualKey(vk))` 按住 0.15s 才被 Unity 接收
    （见 `monthly_exchange._press_escape`）。绝区零是 Unity；**无限暖暖是 UE5**，
    L/H/ESC 也统一到这里只是加固，**不能据此解释暖暖日历打不开**：
    11-01 用 `pyautogui.press('l')` 第 2 下就打开了日历，真正的退化在
    `nikki_calendar._open_calendar` 的尝试顺序与顶栏连读图标坐标。
    finally 保证异常时也松开键。
    """
    vk = _scancode_vk(key)
    scan = win32api.MapVirtualKey(vk, 0)
    try:
        win32api.keybd_event(vk, scan, 0, 0)
        time.sleep(max(0.0, hold_sec))
    finally:
        win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)


_MODIFIER_VKS = (
    win32con.VK_MENU,      # Alt
    win32con.VK_CONTROL,
    win32con.VK_SHIFT,
    win32con.VK_LWIN,
    win32con.VK_RWIN,
)


def _release_modifier_keys():
    """点击前抬起 Alt/Ctrl/Shift/Win。

    流程中途被停止、键位类步骤异常退出时，修饰键可能停在按下状态；
    此时游戏/启动器会把鼠标点击当成组合键操作，表现为"点了没反应"。
    对没按下的键发送 keyup 无副作用（2026-11-03 ZZZ 标题页点击无反应排查）。
    """
    try:
        for vk in _MODIFIER_VKS:
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception as exc:  # 输入/权限异常不应影响点击本身
        _LOGGER.debug("[窗口] 释放修饰键失败: %s", exc)


def _resolve_background_click(step: Dict, cfg: Dict, attempt: int) -> bool:
    """决定本次点击用前台 pyautogui 还是后台 PostMessage。

    - background_click：显式指定基线方式（ok-script/OneDragon 对游戏多用后台）；
    - click_fallback：同一元素点击确认失败后，下一次自动换另一种方式。
      前台点击会被覆盖在游戏上的其他窗口吞掉，后台 PostMessage 又可能被
      某些游戏忽略，交替尝试比死磕一种更有效。
    """
    base = bool(step.get("background_click", cfg.get("background_click", False)))
    if attempt > 0 and bool(step.get("click_fallback", cfg.get("click_fallback", False))):
        return not base
    return base


def _window_available_for_ocr(hwnd: int) -> bool:
    """窗口当前能否被 OCR 取到有效区域（最小化/句柄失效/尺寸为 0 → False）。

    与 `ocr_click_tester._ocr_scan` 的「窗口不可用」分支同源：那种状态下 OCR
    一定返回空结果，上层却只会当「未匹配」继续轮询到 timeout_sec。
    2026-11-04 异环启动器在启动器/游戏本体间反复换窗口，就是这样空转了
    12 分钟（20:35:31→20:47:23）却连一条失败记录都没留下。

    注意只判「窗口不可用」，**不判被遮挡**：异环可后台点击，被别的窗口挡住
    不影响操作，不该因此把流程判死。
    """
    try:
        from .ocr_click_tester import _ensure_window_region

        return _ensure_window_region(hwnd) is not None
    except Exception as exc:  # noqa: BLE001 - 探测失败也按不可用，靠连续时长阈值兜底
        logging.getLogger("GameAutomation").debug("[流程] 窗口可用性探测失败: %s", exc)
        return False


def _locate_template_on_screen(
    template_path: str,
    region: Tuple[int, int, int, int],
    scales,
    confidences,
    throttle_sec: float = 0.1,
    use_color: bool = False,
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
    last_error: Optional[Exception] = None
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
        pt = _match_template_cv2_once(screenshot, tpl, confidences, use_color=use_color)
        if pt:
            return int(pt[0] + region[0]), int(pt[1] + region[1])
        if use_color:
            continue   # pyautogui.locate 内部只认灰度，彩色模式下别拿它兜底
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
                except Exception as exc:
                    last_error = exc
            time.sleep(throttle_sec)
    # 每次调用只在整轮 scale/conf 都失败后打一条：原实现按组合逐条打，
    # 一轮刷 9 行且异常 message 常为空，日志看不出是哪个模板（2026-11-03 实盘）。
    _LOGGER.debug(
        "[模板] 屏幕定位失败: %s region=%s%s",
        os.path.basename(template_path) or template_path,
        region,
        f" ({type(last_error).__name__}: {last_error})" if last_error else "",
    )
    return None


def _locate_template_in_window(
    hwnd: int,
    template_path: str,
    scales,
    confidences,
    use_color: bool = False,
) -> Optional[Tuple[int, int]]:
    if not os.path.exists(template_path):
        return None
    rect = _window_rect(hwnd)
    if not rect:
        return None
    l, t, w, h = rect
    region = (l, t, w, h)

    # 先直接在屏幕区域中匹配，避免局部截图与后续点击坐标换算产生偏差。
    try:
        screen_center = _locate_template_on_screen(
            template_path, region, scales, confidences, use_color=use_color,
        )
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
        pt = _match_template_cv2_once(screenshot, tpl, confidences, use_color=use_color)
        if pt:
            return pt
        if use_color:
            continue   # pyautogui.locate 只认灰度，彩色模式别用它兜底
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


def _ocr_locate_labeled_in_window(
    logger: logging.Logger,
    hwnd: Optional[int],
    target_text: Any,
    min_confidence: float = 0.5,
    search_region: Optional[Tuple[int, int, int, int]] = None,
    exclude_texts: Optional[List[str]] = None,
    match_mode: Optional[str] = None,
    anchor: Any = None,
    near: Optional[Dict] = None,
    ambiguous_action: str = "warn",
    debug_tag: Optional[str] = None,
    save_debug: bool = False,
    engine: Optional[object] = None,
) -> Optional[Tuple[str, Tuple[int, int]]]:
    """
    使用OCR在窗口中定位目标文字，并返回命中的候选词（_ocr_locate_in_window 的带标签版）。
    返回 (命中的候选词, 窗口局部坐标) 或 None。
    单次截图+单次 OCR 同时匹配全部候选（dismiss_texts 与目标文字共用一次扫描）。
    engine: 指定推理引擎（None 用默认 onnxocr v6；高精度步骤同引擎）。
    """
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
                exclude_texts=exclude_texts,
                match_mode=match_mode or "legacy",
                anchor=anchor,
                near=near,
                ambiguous_action=ambiguous_action,
                debug_tag=debug_tag,
                save_debug=save_debug,
                engine=engine,
            )
        else:
            result = ocr_find_any_text(
                target_texts=target_candidates,
                logger=logger,
                hwnd=hwnd,
                min_confidence=min_confidence,
                exclude_texts=exclude_texts,
                match_mode=match_mode or "legacy",
                anchor=anchor,
                near=near,
                ambiguous_action=ambiguous_action,
                debug_tag=debug_tag,
                save_debug=save_debug,
                engine=engine,
            )
        if not result:
            return None
        center = result["center"]
        matched = str(result.get("matched_target") or target_candidates[0])
        if not hwnd:
            return (matched, center)
        rect = _window_rect(hwnd)
        if not rect:
            # 目标窗口当前不可用（最小化/已关闭，rect 是 -32000 占位值或空）：
            # 无法把屏幕坐标换算成窗口局部坐标，此时给坐标必然错位。
            # 返回 None 让上层按"未匹配"重试（步骤会重新 focus/还原窗口）。
            return None
        l, t, _, _ = rect
        return (matched, (center[0] - l, center[1] - t))
    except Exception as e:
        logger.debug(f"OCR定位失败: {str(e)}")
    return None


def _ocr_locate_in_window(
    logger: logging.Logger,
    hwnd: Optional[int],
    target_text: Any,
    min_confidence: float = 0.5,
    search_region: Optional[Tuple[int, int, int, int]] = None,
    exclude_texts: Optional[List[str]] = None,
    match_mode: Optional[str] = None,
    anchor: Any = None,
    near: Optional[Dict] = None,
    ambiguous_action: str = "warn",
    debug_tag: Optional[str] = None,
    save_debug: bool = False,
    engine: Optional[object] = None,
) -> Optional[Tuple[int, int]]:
    """
    使用OCR在窗口中定位目标文字（备选方案）。
    返回窗口局部坐标或None。
    search_region: 屏幕坐标的局部搜索区域 (left, top, width, height)，限制OCR搜索范围
    match_mode / anchor / near: 结构化匹配配置（精确>前缀>子串 + 锚点几何）
    engine: 指定推理引擎（None 用默认 onnxocr v6）。
    """
    hit = _ocr_locate_labeled_in_window(
        logger,
        hwnd,
        target_text,
        min_confidence=min_confidence,
        search_region=search_region,
        exclude_texts=exclude_texts,
        match_mode=match_mode,
        anchor=anchor,
        near=near,
        ambiguous_action=ambiguous_action,
        debug_tag=debug_tag,
        save_debug=save_debug,
        engine=engine,
    )
    return hit[1] if hit else None


def _locate_with_ocr_fallback(
    logger: logging.Logger,
    hwnd: int,
    template_path: str,
    target_text: Any,
    scales,
    confidences,
    enable_ocr: bool = True,
    local_region: Optional[Tuple[int, int, int, int]] = None,
    exclude_texts: Optional[List[str]] = None,
    match_mode: Optional[str] = None,
    anchor: Any = None,
    near: Optional[Dict] = None,
    ambiguous_action: str = "warn",
    debug_tag: Optional[str] = None,
    save_debug: bool = False,
    dismiss_texts: Optional[List[str]] = None,
    engine: Optional[object] = None,
) -> Tuple[Optional[Tuple[int, int]], str]:
    """
    模板匹配+OCR联合定位。
    返回：(窗口局部坐标, 方法: "template" | "ocr" | "ocr_dismiss" | None)
    local_region: 屏幕坐标的局部搜索区域，限制OCR搜索范围防止页面其他内容干扰
    match_mode / anchor / near: 结构化匹配配置（精确>前缀>子串 + 锚点几何）
    dismiss_texts: 等待途中需要点掉的弹窗文字（如绝区零月卡「今日到账」）。
        与目标文字共用同一次 OCR 扫描；命中弹窗文字时返回方法 "ocr_dismiss"，
        由调用方点击后继续等待，不算步骤命中。
        弹窗词排在目标词前面：弹窗在场时必然先处理弹窗。
    engine: 指定推理引擎（None 用默认 onnxocr v6；高精度步骤同引擎）。
    """
    target_candidates = _resolve_target_text_candidates(template_path, target_text)
    target_label = _display_target_text(target_candidates, os.path.basename(template_path or ""))

    dismiss_candidates = _normalize_target_text_candidates(dismiss_texts)

    center = _locate_template_in_window(hwnd, template_path, scales, confidences)
    if center:
        logger.info(f"[{target_label}] 模板匹配成功 @ {center}")
        return center, "template"

    if enable_ocr and (target_candidates or dismiss_candidates):
        # 弹窗词优先：OCR 先扫弹窗再扫目标，同一次扫描内完成
        ocr_candidates = list(dismiss_candidates) + list(target_candidates)
        hit = _ocr_locate_labeled_in_window(
            logger,
            hwnd,
            ocr_candidates,
            search_region=local_region,
            exclude_texts=exclude_texts,
            match_mode=match_mode,
            anchor=anchor,
            near=near,
            ambiguous_action=ambiguous_action,
            debug_tag=debug_tag,
            save_debug=save_debug,
            engine=engine,
        )
        if hit:
            matched, center = hit
            if matched in dismiss_candidates:
                logger.info(
                    f"[{target_label}] OCR检测到弹窗「{matched}」 @ {center}（等待途中点掉）"
                )
                return center, "ocr_dismiss"
            logger.info(f"[{target_label}] OCR匹配成功 @ {center}")
            return center, "ocr"

    return None, None


def _post_click_window(hwnd: int, x_local: int, y_local: int) -> bool:
    """抄 ok-script PostMessageInteraction：后台点击，免抢焦点。

    返回 True=已投递，False=回退前台点击。发送 WM_MOUSEMOVE/LBUTTONDOWN/UP，
    并把光标临时移到按钮上再恢复（部分游戏按当前光标位置判定点击）。
    """
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        # 对齐 ok-script/OneDragon 的后台点击做法：先把光标放到按钮上再投递消息，
        # 少数游戏（含 ZZZ 标题页）只在处理点击时读当前光标位置，纯 PostMessage
        # 坐标会落空；点完恢复光标，避免影响后续动作。
        old_pos = None
        rect = _window_rect(hwnd)
        if rect:
            try:
                old_pos = win32api.GetCursorPos()
                win32api.SetCursorPos((rect[0] + int(x_local), rect[1] + int(y_local)))
                time.sleep(0.03)
            except Exception:
                old_pos = None
        lparam = (int(y_local) << 16) | (int(x_local) & 0xFFFF)
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lparam)
        time.sleep(0.02)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        time.sleep(0.02)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
        if old_pos:
            time.sleep(0.05)
            win32api.SetCursorPos(old_pos)
        return True
    except Exception:
        return False


def _click_window_local(hwnd: int, x_local: int, y_local: int, clicks: int = 1,
                        interval: float = 0.2, background: bool = False,
                        hold_sec: Optional[float] = None):
    """窗口内点击。默认前台真实输入（pyautogui），XM/CJ 等游戏只吃这个。

    background=True 才是 ok-script 式 PostMessage 后台点击，需按 step/cfg
    显式 opt-in；投递成功≠游戏响应，失败靠后续 confirm 重试兜底
    （其 DOWN/UP 之间已有 20ms 间隔，天然是按住式）。
    hold_sec 交给 `_pyautogui_click_guarded`：None=全局默认按住，
    0=显式瞬时，>0=指定时长。
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
    _release_modifier_keys()
    _focus_window(hwnd)
    sx, sy = l + int(x_local), t + int(y_local)
    _pyautogui_click_guarded(sx, sy, clicks=clicks, interval=interval, hold_sec=hold_sec)


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


# 2026-11-04 实机：Unity 启动器不认瞬时 down+up，且瞬时点击是典型脚本特征。
# 所有前台点击默认按住一小段时间；环境变量可全局调/关，步骤级 click_hold_sec
# 可单独覆盖，显式 0 = 退回瞬时（逃生口）。
try:
    DEFAULT_CLICK_HOLD_SEC = max(0.0, float(os.environ.get("GAMEAUTO_CLICK_HOLD_SEC", "0.06")))
except (TypeError, ValueError):
    DEFAULT_CLICK_HOLD_SEC = 0.06


def _pyautogui_click_guarded(x: int, y: int, clicks: int = 1, interval: float = 0.2,
                             button: str = "left", hold_sec: Optional[float] = None):
    """前台点击统一收口：默认走极短按住式点击，fail-safe 抛 FailSafeAborted。

    hold_sec=None → 用 `DEFAULT_CLICK_HOLD_SEC`（全局默认，所有调用点自动受益）；
    hold_sec=0    → 显式瞬时 `pyautogui.click`（确有需要时才用）；
    hold_sec>0    → 按住指定秒数（如绝区零启动器用 0.08）。
    """
    hold = DEFAULT_CLICK_HOLD_SEC if hold_sec is None else max(0.0, float(hold_sec))
    try:
        if hold > 0:
            _pyautogui_click_held(int(x), int(y), hold, clicks=clicks,
                                  interval=interval, button=button)
        else:
            pyautogui.click(int(x), int(y), clicks=clicks, interval=interval, button=button)
    except pyautogui.FailSafeException as exc:
        logger = logging.getLogger("GameAutomation")
        logger.warning("[fail-safe] %s", _FAILSAFE_HINT)
        raise FailSafeAborted(_FAILSAFE_HINT) from exc


def _pyautogui_click_held(
    x: int, y: int, hold_sec: float = 0.08, clicks: int = 1,
    interval: float = 0.2, button: str = "left",
):
    """按住式点击：Unity 启动器的按钮不认瞬时 `pyautogui.click`。

    2026-11-04 21:35 提权实机（用户当场开着绝区零验证）：
      - 普通权限进程 → UIPI 静默拦截，三种点击全无效；
      - 提权后瞬时 `pyautogui.click` → 「点击进入游戏」10s 仍不消失；
      - 提权 + `mouseDown` 按住 80ms 再 `mouseUp` → 3s 内出现「登录成功」。
    所以权限对齐（见 `_uipi_block_reason`）和按住这两个条件缺一不可。
    """
    count = max(1, int(clicks))
    try:
        pyautogui.moveTo(int(x), int(y))
        for i in range(count):
            pyautogui.mouseDown(button=button)
            time.sleep(max(0.0, float(hold_sec)))
            pyautogui.mouseUp(button=button)
            if i + 1 < count and interval:
                time.sleep(interval)
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


def _step_ocr_region(frac, rect) -> Optional[Tuple[int, int, int, int]]:
    """窗口相对比例 [x0,y0,x1,y1] → 屏幕绝对 (left, top, width, height)。

    每轮用当前窗口矩形实时换算：窗口移动/换屏自动跟随；窗口尺寸变化时按比例
    缩放（对固定布局的应用如 XM 脚本窗口是稳定的）。算不出返回 None（回退全窗扫描）。
    """
    try:
        fx0, fy0, fx1, fy1 = (float(v) for v in frac)
    except (TypeError, ValueError):
        return None
    if not rect or len(rect) != 4:
        return None
    l, t, w, h = rect
    if w <= 0 or h <= 0:
        return None
    x0, y0 = l + fx0 * w, t + fy0 * h
    return (
        int(x0), int(y0),
        max(1, int((fx1 - fx0) * w)), max(1, int((fy1 - fy0) * h)),
    )


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




# 自带内部循环的动作：匹配阶段直接放行，交给 _dispatch_step_action 执行
_BUILTIN_ACTIONS = (
    "zzz_shop_buy",
    "monthly_exchange",
    "hsr_resin_craft",
    "zzz_notorious_hunt",
    "nikki_enter_game",
    "nikki_calendar",
    "starrail_enter_main",
    "genshin_enter_main",
    "qoder_open_campaign",
    "qoder_verify_claim",
    "wool_dismiss_update",
    "wool_verify_log",
    "script_checklist_sync",
    "press_key",
)


def _dispatch_step_action(
    *,
    logger: logging.Logger,
    flow_key: str,
    step: dict,
    name: str,
    step_no: int,
    action: str,
    hwnd: int,
    pid: Optional[int],
    scales: List[float],
    confidences: List[float],
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str, int, Optional[int]]:
    """执行内建动作步骤（采购/兑换/进入主界面/Qoder 领取/日志核验）。

    返回 (是否成功, 失败说明, 最新 hwnd, 最新 pid)：进入游戏类动作会把
    窗口切到游戏本体，调用方必须用返回值覆盖本地 hwnd/pid。
    """
    if action == "zzz_shop_buy":
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
            return False, f"[{flow_key}] 步骤失败: {name} ({shop_msg})", hwnd, pid
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
            return False, f"[{flow_key}] 步骤失败: {name} ({exchange_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}每月兑换完成: {exchange_msg}")
    elif action == "hsr_resin_craft":
        # 崩铁：背包 → 贵重物 → 自塑尘脂 → 万能合成机 → 合成到本月上限
        from .hsr_resin_craft import craft_self_modeling_resin

        resin_ok, resin_msg = craft_self_modeling_resin(
            logger,
            hwnd,
            step,
            scales=scales,
            confidences=confidences,
            stop_check=stop_check,
        )
        if not resin_ok:
            return False, f"[{flow_key}] 步骤失败: {name} ({resin_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}自塑尘脂合成完成: {resin_msg}")
    elif action == "zzz_notorious_hunt":
        # 绝区零：F2 → 训练 → 恶名狩猎 → 库萨里库 → 出战 → 自动战斗 → 周期挑战全部领取
        from .zzz_notorious_hunt import run_notorious_hunt

        hunt_ok, hunt_msg = run_notorious_hunt(
            logger,
            hwnd,
            step,
            scales=scales,
            confidences=confidences,
            stop_check=stop_check,
        )
        if not hunt_ok:
            return False, f"[{flow_key}] 步骤失败: {name} ({hunt_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}恶名狩猎完成: {hunt_msg}")
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
            return False, f"[{flow_key}] 步骤失败: {name} ({nikki_msg})", hwnd, pid
        if game_hwnd:
            hwnd = game_hwnd
            pid = game_pid or pid
        logger.info(f"[{flow_key}] 步骤{step_no}进入主界面成功: {nikki_msg}")
    elif action == "nikki_calendar":
        # 无限暖暖：L → 奇想日历 → 奇迹之冠搭配赛领奖 → 晶石兑换（每月）
        from .nikki_calendar import run_nikki_calendar_flow

        calendar_ok, calendar_msg = run_nikki_calendar_flow(
            logger,
            hwnd,
            scales,
            confidences,
            stop_check=stop_check,
        )
        if not calendar_ok:
            return False, f"[{flow_key}] 步骤失败: {name} ({calendar_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}奇想日历完成: {calendar_msg}")
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
            return False, f"[{flow_key}] 步骤失败: {name} ({open_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}权益中心已打开: {open_msg}")
    elif action == "qoder_verify_claim":
        # Qoder 领取结果以 App 自身日志为准；UI 空跑不再算成功
        from .qoder_claim import verify_claim

        claim_ok, claim_msg = verify_claim(
            logger,
            timeout_sec=int(step.get("timeout_sec", 60)),
        )
        if not claim_ok:
            return False, f"[{flow_key}] 步骤失败: {name} ({claim_msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}领取状态确认: {claim_msg}")
    elif action == "wool_dismiss_update":
        # MiniMax：关掉「更新已来」公告弹窗。
        # 2026-11-02 实锤：公告只在**更新后第一次启动**弹（用户 17:05 重开后就没再弹，
        # 只剩左上角一个「更新」角标），但那一次恰好把自动化点「签到得」的点击吞了
        # —— api 日志有 signin/config+status、零个 signin/claim，同一按钮手动点却成功。
        # 所以在签到流程**第一步**就先把它关掉，别等末步日志核验失败才救火。
        from .wool_claim_logs import dismiss_minimax_update_popup

        dismissed = dismiss_minimax_update_popup(logger, hwnd)
        logger.info(
            f"[{flow_key}] 步骤{step_no}公告弹窗: "
            f"{'已关闭' if dismissed else '未检测到/未关闭（继续）'}"
        )
        # 非致命：关不掉也继续走（末步日志核验会再兜一次），这里只留痕
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
            return False, f"[{flow_key}] 步骤失败: {name} ({wool_msg})", hwnd, pid
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
            return False, f"[{flow_key}] 步骤失败: {name} ({sr_msg})", hwnd, pid
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
            return False, f"[{flow_key}] 步骤失败: {name} ({gi_msg})", hwnd, pid
        if game_hwnd:
            hwnd = game_hwnd
            pid = game_pid or pid
        logger.info(f"[{flow_key}] 步骤{step_no}进入主界面成功: {gi_msg}")
    elif action == "script_checklist_sync":
        # 脚本任务清单同步：点标签页 → 滚动扫描 → 勾选新出现条目（XM/CJ）
        from .script_checklist import sync_checklist

        ok, msg = sync_checklist(
            logger=logger,
            flow_key=flow_key,
            step=step,
            hwnd=hwnd,
            stop_check=stop_check,
        )
        if not ok:
            return False, f"[{flow_key}] 步骤失败: {name} ({msg})", hwnd, pid
        logger.info(f"[{flow_key}] 步骤{step_no}清单同步完成: {msg}")
    elif action == "press_key":
        # 通用扫描码按键（2026-11-07）：绝区零游戏内 UI 只吃 keybd_event+扫描码
        # （11-05 实机）；模板点右下···按钮开不出主菜单、pyautogui F2 全被吞
        # （12:09/12:15 两轮实录）。verify_texts 可选：按下后等任一文字出现才算
        # 成功，没见就重按（最多 max_attempts 轮）。
        key = str(step.get("key") or "escape")
        presses = max(1, int(step.get("presses", 1)))
        interval = float(step.get("press_interval_sec", 1.2))
        verify_texts = _normalize_target_text_candidates(step.get("verify_texts"))
        verify_timeout = float(step.get("verify_timeout_sec", 6))
        attempts = max(1, int(step.get("max_attempts", 3)))
        _focus_window(hwnd)
        for attempt in range(1, attempts + 1):
            for i in range(presses):
                press_key_scancode(key)
                if i < presses - 1:
                    time.sleep(interval)
            logger.info(
                f"[{flow_key}] 步骤{step_no}已按 {key} ×{presses}（扫描码，第 {attempt} 次）"
            )
            if not verify_texts:
                return True, f"已按 {key} ×{presses}", hwnd, pid
            deadline = time.time() + verify_timeout
            verified = False
            while time.time() < deadline:
                if _stop_requested(stop_check):
                    return False, f"[{flow_key}] 用户已请求停止", hwnd, pid
                if _ocr_locate_in_window(logger, hwnd, verify_texts):
                    logger.info(f"[{flow_key}] 步骤{step_no}按键生效（OCR 见 {verify_texts}）")
                    verified = True
                    break
                time.sleep(1.0)
            if verified:
                return True, f"已按 {key}，页面特征已出现", hwnd, pid
            logger.warning(
                f"[{flow_key}] 步骤{step_no}按 {key} 后 {verify_timeout:.0f}s "
                f"未见 {verify_texts}，重按"
            )
        return False, (
            f"[{flow_key}] 步骤失败: {name} "
            f"(按 {key} ×{attempts} 轮后未见 {verify_texts})"
        ), hwnd, pid
    else:
        return False, f"[{flow_key}] 未知动作: {action}", hwnd, pid
    return True, "", hwnd, pid




def _collect_failure_evidence(
    *,
    logger: logging.Logger,
    flow_key: str,
    step: dict,
    name: str,
    step_no: int,
    action: str,
    target_texts: List[str],
    template: str,
    hwnd: int,
    retry_count: int,
    max_attempts: int,
    step_enable_ocr: bool,
    error: str = "未匹配模板/OCR（超过重试上限）",
) -> None:
    """步骤重试耗尽时抓当帧 OCR + 截图并落库（一次识别，两处复用）。

    error 允许调用方覆盖：2026-11-03 崩铁自塑尘脂是 action 分支自己失败返回，
    以前只把原因写进日报一行，日志和 flow_failures 全空，事后查不出点歪在哪。
    """
    ocr_results = None
    try:
        from .ocr_click_tester import ocr_window_content, _get_precision_engine
        # ocr_precision 步骤的失败取证也用高精度引擎（记录更完整的当帧文字）
        prec_engine = None
        if step.get("ocr_precision"):
            try:
                prec_engine = _get_precision_engine(logger)
            except Exception as exc:
                logger.debug(f"[{flow_key}] 取证高精度引擎获取失败: {exc}")
        results = ocr_window_content(logger, hwnd=hwnd, engine=prec_engine)
        if results:
            logger.warning(f"[{flow_key}] 步骤失败时屏幕文字:")
            for r in results[:10]:
                logger.warning(f"  - \"{r['text']}\" (置信度: {r['confidence']:.2f}) @ {r['center']}")
        ocr_results = results
    except Exception as exc:
        logger.debug("[%s] 输出 OCR 失败样本异常: %s", flow_key, exc)
    # 失败取证：截图 + 当帧文字 + 结构化落库（复用上面的 OCR 结果，
    # 不重复识别），供 tools/flow_failure_report.py 事后聚合分析
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key=flow_key,
            step=name,
            error=str(error or "未匹配模板/OCR（超过重试上限）"),
            hwnd=hwnd,
            retry_count=retry_count,
            ocr_results=ocr_results,
            extra={
                "step_no": step_no,
                "step_action": action,
                "target_texts": target_texts if step_enable_ocr else [],
                "template": template or "",
                "ocr_match_mode": str(step.get("ocr_match_mode") or ""),
                "ocr_anchor": step.get("ocr_anchor"),
                "max_attempts": max_attempts,
                "kind": "flow_step",
            },
        )
    except Exception as exc:
        logger.debug("[%s] 记录失败诊断异常: %s", flow_key, exc)



def _resolve_start_guard(
    cfg: dict,
    flow_key: str,
    hwnd: int,
    scales,
    confidences,
    steps_len: int,
    logger,
) -> Tuple[int, set]:
    """统一入口守卫：优先从主界面开始（2026-11-04 用户要求，参照白荆）。

    - `start_from_main`: {"template": 主界面模板, "skip_to_step": N}
      模板命中 → 从第 N 步开始，不再做进入操作。
    - `skip_steps_if_process`: {"process_name": 进程名, "steps": [1-based...]}
      游戏进程已在运行 → 跳过这些启动器步骤（例如异环已在游戏里）。

    返回 (起始索引, 需要跳过的 0-based 步骤索引集合)。
    """
    skip_indices: set = set()
    start_index = 0
    guard = cfg.get("start_from_main")
    if isinstance(guard, dict):
        guard_tpl = str(guard.get("template") or cfg.get("exit_marker_template") or "").strip()
        try:
            skip_to = int(guard.get("skip_to_step", 1))
        except (TypeError, ValueError):
            skip_to = 1
        if guard_tpl and skip_to > 1:
            guard_rect = _window_rect(hwnd)
            if guard_rect and _locate_template_on_screen(
                guard_tpl, guard_rect, scales, confidences, throttle_sec=0.0
            ):
                start_index = max(0, min(skip_to - 1, max(0, steps_len - 1)))
                logger.info(
                    f"[{flow_key}] 已在主界面（{os.path.basename(guard_tpl)} 命中），"
                    f"跳过前 {start_index} 步"
                )
    proc_guard = cfg.get("skip_steps_if_process")
    if isinstance(proc_guard, dict) and str(proc_guard.get("process_name") or "").strip():
        proc_name = str(proc_guard["process_name"]).strip()
        try:
            running = any(
                (p.info.get("name") or "").strip().lower() == proc_name.lower()
                for p in psutil.process_iter(["name"])
            )
        except Exception:
            running = False
        if running:
            for raw_no in proc_guard.get("steps") or []:
                try:
                    skip_indices.add(int(raw_no) - 1)
                except (TypeError, ValueError):
                    continue
            if skip_indices:
                logger.info(
                    f"[{flow_key}] 进程 {proc_name} 已在运行，跳过步骤 "
                    f"{sorted(n + 1 for n in skip_indices)}（不再走启动器）"
                )
    return start_index, skip_indices


# ── 第三方脚本版本跟踪（XM/CJ 这类 exe 名带版本的流程）────────────────────────
# XM Seer脱机日常 ver 2.6.21 / 重聚脱机巅峰【2.8.7】：脚本频繁更新且老版本会被
# 服务端拒用。启动时解析版本并记录到 data/cache/flow_script_versions.json，
# 版本变化（更新后首跑）打显式日志，日报/排障一眼可见。
_SCRIPT_VERSION_PATTERNS = (
    re.compile(r"【([0-9]+(?:\.[0-9]+){1,3})】"),
    re.compile(r"\bver\s*[:：]?\s*([0-9]+(?:\.[0-9]+){1,3})", re.IGNORECASE),
)
_SCRIPT_VERSION_STATE = CACHE_DIR / "flow_script_versions.json"


def _detect_script_version(text: str) -> str:
    """从脚本 exe 路径/窗口标题解析版本号；解析不到返回空串。"""
    for pattern in _SCRIPT_VERSION_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return m.group(1)
    return ""


def _log_script_version(flow_key: str, exe_path: str, logger: logging.Logger) -> None:
    """启动时记录脚本版本；与上次相比有变化时打更新首跑提示。"""
    version = _detect_script_version(exe_path)
    if not version:
        return
    prev = ""
    state: Dict[str, Any] = {}
    try:
        state = json.loads(_SCRIPT_VERSION_STATE.read_text(encoding="utf-8"))
        prev = str(state.get(flow_key) or "")
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 状态文件损坏只影响提示，不影响流程
        logger.debug(f"[{flow_key}] 读取脚本版本状态失败: {exc}")
    if prev and prev != version:
        logger.info(
            f"[{flow_key}] 脚本版本变化: {prev} -> {version}"
            f"（更新后首跑；更新提示由前置「检查更新」步骤自动点掉）"
        )
    else:
        logger.info(f"[{flow_key}] 脚本版本: {version}")
    if prev != version:
        try:
            state[flow_key] = version
            _SCRIPT_VERSION_STATE.parent.mkdir(parents=True, exist_ok=True)
            _SCRIPT_VERSION_STATE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 同上，展示性状态，失败不影响流程
            logger.debug(f"[{flow_key}] 写脚本版本状态失败: {exc}")


def _script_ver_tuple(version: str) -> Tuple[int, ...]:
    """XM 系脚本版本按位比较：末段一位数补零成两位（2.6.5 → 2.6.50）。

    作者口径（用户 2026-11-07 说明）：2.6.5 实际是 2.6.50，小修 bug 出
    2.6.51，大改动加功能才进 2.6.6。直接按数值比 5 < 41 会把新版本判旧；
    末段补零后 50 > 41，与实测更新序列（2.6.11→2.6.2→2.6.21→2.6.41→2.6.5）
    完全单调。解析失败返回空元组。
    """
    parts: List[int] = []
    segs = version.split(".")
    for idx, seg in enumerate(segs):
        try:
            num = int(seg)
        except ValueError:
            return tuple()
        if idx == len(segs) - 1 and len(seg) == 1:
            num *= 10
        parts.append(num)
    return tuple(parts)


def _find_newer_script_exe(exe_path: str, *, since_ts: float = 0.0) -> str:
    """在同目录找「同名基础名、更新版本」的脚本 exe；没有返回空串。

    CJ/XM 的更新机制是下载新版本 exe 到同目录（新旧并存，如
    重聚脱机巅峰【2.8.7】.exe + 【2.8.8】.exe）。版本按位比较（末段补零，
    见 _script_ver_tuple）；exe 名解析不出版本时退回 mtime（更新点击之后
    新落地的算新版本）。目录里还有 HoYo.Gacha.exe 等无关程序，必须按
    基础名过滤。
    """
    path = Path(exe_path)
    base = re.sub(r"【[^】]*】|\bver\s*.*$", "", path.stem, flags=re.IGNORECASE).strip()
    if not base:
        return ""
    current_ver = _detect_script_version(exe_path)
    current_tuple = _script_ver_tuple(current_ver) if current_ver else None
    best, best_key = "", None
    try:
        for cand in path.parent.glob("*.exe"):
            if os.path.abspath(cand) == os.path.abspath(exe_path):
                continue
            if not cand.name.startswith(base):
                continue
            cand_ver = _detect_script_version(cand.name)
            cand_tuple = _script_ver_tuple(cand_ver) if cand_ver else None
            if current_tuple and cand_tuple:
                # 版本可比较：严格更大才算新
                if cand_tuple > current_tuple and (best_key is None or cand_tuple > best_key):
                    best, best_key = str(cand), cand_tuple
            elif since_ts > 0:
                # 任一侧解析不出版本 → 退回 mtime 锚点
                try:
                    m = cand.stat().st_mtime
                except OSError:
                    continue
                if m >= since_ts and (best_key is None or m > best_key):
                    best, best_key = str(cand), m
    except OSError as exc:
        logging.getLogger("GameAutomation").debug(f"扫描脚本目录失败: {exc}")
        return ""
    return best


def _wait_script_update_exe(
    exe_path: str,
    *,
    since_ts: float,
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]],
    logger: logging.Logger,
    flow_key: str,
) -> str:
    """轮询等待更新下载的新版本 exe 落地；超时/停止返回空串。"""
    deadline = time.time() + max(30, timeout_sec)
    while time.time() < deadline:
        if _stop_requested(stop_check):
            return ""
        newer = _find_newer_script_exe(exe_path, since_ts=since_ts)
        if newer:
            return newer
        time.sleep(5)
    logger.info(f"[{flow_key}] 等待新版本 exe 超时（{timeout_sec}s）")
    return ""


def _delete_old_script_exe(
    old_exe_path: str, *, new_exe_path: str, logger: logging.Logger, flow_key: str
) -> None:
    """更新换血后删除旧版本 exe（用户口径：同目录新旧并存，删旧用新）。"""
    if not old_exe_path or os.path.abspath(old_exe_path) == os.path.abspath(new_exe_path):
        return
    try:
        os.remove(old_exe_path)
        logger.info(f"[{flow_key}] 已删除旧版本脚本: {old_exe_path}")
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 旧文件可能被占用/权限不足，删除失败不影响流程
        logger.warning(f"[{flow_key}] 删除旧版本脚本失败（不影响使用）: {exc}")


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
            logger=logger,
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
    _log_script_version(flow_key, exe_path, logger)

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
    # 流程级 OCR 匹配策略：默认 legacy（旧行为）；smart 按 精确>前缀>子串 排序。
    # 步骤级 ocr_match_mode 可覆盖。anchor/near 见 ocr_click_tester.ocr_find_any_text。
    flow_ocr_match_mode = str(cfg.get("ocr_match_mode", "") or "").strip().lower()
    flow_ocr_debug = bool(cfg.get("ocr_debug_snapshot", False))
    steps = cfg.get("steps", [])
    if not steps:
        if close_on_exit:
            close_flow_processes(logger, flow_key, pid, window_process_names)
        return False, f"[{flow_key}] 配置中无步骤"

    start_index, skip_step_indices = _resolve_start_guard(
        cfg, flow_key, hwnd, scales, confidences, len(steps), logger
    )

    # OCR 备选开关只由流程配置决定（enable_ocr_fallback，未配置即关闭），
    # 执行器不再按 flow_key 硬编码默认值——新增流程的默认行为由
    # flow_configs.example.json 声明，执行器保持无偏见的插件式转发。
    enable_ocr = cfg.get("enable_ocr_fallback", False)

    step_attempts: Dict[int, int] = {}
    step_local_region: Dict[int, Optional[Tuple[int, int, int, int]]] = {}
    steps_hit: set = set()  # 本轮已成功命中的步骤名（供 self_update_swap 等联动步骤判断）
    steps_hit_ts: Dict[str, float] = {}  # 命中时刻（换血以「更新点击时刻」为 mtime 锚点）
    i = start_index
    flow_ok = False
    try:
        while i < len(steps):
            if i in skip_step_indices:
                logger.info(f"[{flow_key}] 步骤{i + 1} 因游戏进程已在运行跳过")
                i += 1
                continue
            step = steps[i]
            step_no = i + 1
            name = step.get("name", f"step{step_no}")
            action = step.get("action", "click_template")
            search_scope = str(step.get("search_scope", "window") or "window").lower()
            template = step.get("template", "")
            process_name = step.get("process_name", "")
            target_texts = _resolve_target_text_candidates(template, step.get("target_text", name), fallback_text=name)
            step_exclude_texts = _normalize_target_text_candidates(step.get("ocr_exclude_texts"))
            # 等待途中需要点掉的弹窗文字（如绝区零月卡「今日到账」）：与目标文字共用
            # 同一次 OCR 扫描，命中就点掉再继续等，不算步骤命中也不消耗重试次数
            step_dismiss_texts = _normalize_target_text_candidates(step.get("dismiss_texts"))
            step_ocr_match_mode = step.get("ocr_match_mode", step.get("match_mode"))
            if step_ocr_match_mode is None:
                step_ocr_match_mode = flow_ocr_match_mode
            step_ocr_match_mode = str(step_ocr_match_mode or "").strip().lower()
            step_ocr_anchor = step.get("ocr_anchor", step.get("anchor"))
            step_ocr_near = step.get("ocr_near", step.get("near"))
            step_ocr_ambiguous = str(step.get("ocr_ambiguous", "warn") or "warn").strip().lower()
            step_ocr_debug = bool(step.get("ocr_debug_snapshot", flow_ocr_debug))
            step_ocr_debug_tag = f"{flow_key}_s{step_no}_{name}"
            # 步骤级 OCR 搜索区域（窗口相对 [x0,y0,x1,y1]）：小字特征（如 XM 脚本
            # 的 10px 日志面板）在全窗扫描时会被检测器漏掉，紧裁剪一次命中
            step_ocr_region_frac = step.get("ocr_region_frac")
            target_text = target_texts[0] if target_texts else str(name)
            target_label = _display_target_text(target_texts, target_text)
            step_enable_ocr = bool(step.get("enable_ocr_fallback", enable_ocr or _template_has_ocr_memory(template)))
            timeout_sec = int(step.get("timeout_sec", 60))
            search_interval = float(step.get("search_interval_sec", 0.5))
            post_delay = float(step.get("post_delay_sec", 1.0))
            clicks = int(step.get("clicks", 1))
            no_click = bool(step.get("no_click", False))
            hover = bool(step.get("hover", False))
            # 后台点击需 step/cfg 显式 opt-in（XM/CJ 默认前台，PostMessage 点不上）；
            # click_fallback 开启时，同一步点击确认失败后自动换另一种点击方式
            use_background = _resolve_background_click(step, cfg, step_attempts.get(i, 0))
            max_attempts = int(step.get("max_attempts", 2))
            on_fail = step.get("on_fail", "fail")
            optional_if_missing = bool(step.get("optional_if_missing", False))
            confirm_templates = step.get("confirm_templates", [])
            confirm_timeout = int(step.get("confirm_timeout_sec", 8))

            # ── 脚本自更新换血（CJ/XM 专用语义）────────────────────────────
            # 前置「检查更新」点了更新提示后，脚本把新版本 exe 下载到同目录
            # （新旧并存）；本分支等新 exe 落地 → 关旧进程 → 启动新 exe →
            # 删旧文件，并把流程的 pid/hwnd/exe_path 切到新版本上继续。
            if action == "self_update_swap":
                require_hit = str(step.get("require_step_hit") or "")
                if require_hit and require_hit not in steps_hit:
                    # 检查更新本轮没点过任何提示（已是最新版）→ 不等新 exe，直接过。
                    # 本循环是手动索引制：跳过必须 i += 1 再 continue，否则死循环。
                    logger.info(
                        f"[{flow_key}] 本轮未点过更新提示（{require_hit} 未命中），跳过更新换血"
                    )
                    i += 1
                    continue
                new_exe = _wait_script_update_exe(
                    exe_path,
                    since_ts=steps_hit_ts.get(require_hit, time.time() - 90),
                    timeout_sec=int(step.get("update_timeout_sec", 180)),
                    stop_check=stop_check,
                    logger=logger,
                    flow_key=flow_key,
                )
                if not new_exe:
                    if optional_if_missing:
                        logger.info(f"[{flow_key}] 未检测到新版本脚本，跳过更新换血")
                        i += 1
                        continue
                    return False, f"[{flow_key}] 更新换血失败: 等待新版本 exe 超时"
                old_exe = exe_path
                logger.info(f"[{flow_key}] 检测到新版本脚本: {new_exe}，关闭旧进程并换血")
                if close_on_exit or close_on_fail:
                    close_flow_processes(logger, flow_key, pid, window_process_names)
                launch_ts = time.time()
                new_pid = _launch_maybe_elevated(logger, new_exe)
                if not new_pid:
                    return False, f"[{flow_key}] 更新换血失败: 新版本启动失败 {new_exe}"
                exe_path = new_exe
                pid = new_pid
                if not _sleep_interruptible(int(cfg.get("startup_wait_sec", 10)), stop_check):
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
                    return False, f"[{flow_key}] 更新换血失败: 未找到新版本窗口"
                if window_pid and window_pid != pid:
                    pid = window_pid
                block_reason = _uipi_block_reason(pid)
                if block_reason:
                    return False, f"[{flow_key}] 更新换血失败: {block_reason}"
                _log_script_version(flow_key, new_exe, logger)
                _delete_old_script_exe(
                    old_exe, new_exe_path=new_exe, logger=logger, flow_key=flow_key
                )
                step_local_region.clear()
                i += 1
                continue

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
            else:
                # 句柄真死了（游戏重建窗口/换进程承载）→ 按进程名重新接管
                new_hwnd, new_pid = _recover_flow_window(
                    pid, hwnd, window_title_keywords, window_process_names, logger, flow_key,
                )
                if new_hwnd:
                    hwnd = new_hwnd
                    if new_pid:
                        pid = new_pid
                    step_local_region.pop(i, None)

            local_region = step_local_region.get(i)

            ocr_strategy_note = ""
            if step_ocr_match_mode and step_ocr_match_mode != "legacy":
                ocr_strategy_note += f" [OCR模式: {step_ocr_match_mode}]"
            if step_ocr_anchor:
                ocr_strategy_note += " [锚点已配置]"
            if step_ocr_near:
                ocr_strategy_note += " [几何约束已配置]"
            logger.info(
                f"[{flow_key}] 步骤{step_no}: {name} ({action}) [目标文字: \"{target_label}\"] "
                f"[当前尝试: {step_attempts.get(i, 0) + 1}] "
                f"[搜索范围: {search_scope}] [OCR备选: {'启用' if step_enable_ocr else '禁用'}]"
                f"{ocr_strategy_note}"
            )

            end = time.time() + timeout_sec
            center = None
            match_method = None
            # 窗口连续不可用的止损时长（秒），0 = 禁用并保留旧的「空等到
            # timeout_sec」行为。真在下载/更新时按钮确实要等很久，所以不能一刀切
            # 缩短 timeout_sec；只有窗口本身已经死了才提前收场，并借既有「重试
            # 耗尽」路径落下失败截图与 flow_failures 记录。
            try:
                dead_window_timeout = max(0.0, float(step.get(
                    "window_dead_timeout_sec",
                    cfg.get("window_dead_timeout_sec", 45)) or 0))
            except (TypeError, ValueError):
                dead_window_timeout = 45.0
            window_dead_since = None

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
                    else:
                        # 句柄真死了（游戏重建窗口/换进程承载）→ 按进程名重新接管，
                        # 否则 OCR 会一直扫 0 个区域（_ocr_scan 拒绝回退全屏），空转到超时。
                        new_hwnd, new_pid = _recover_flow_window(
                            pid, hwnd, window_title_keywords, window_process_names, logger, flow_key,
                        )
                        if new_hwnd:
                            hwnd = new_hwnd
                            if new_pid:
                                pid = new_pid
                            local_region = None
                            step_local_region.pop(i, None)

                    # 2026-11-04：窗口连续「最小化/句柄失效」时 OCR 恒返回空结果，
                    # 旧逻辑会一直空转到 timeout_sec（异环 NTE_MONTHLY 是 1200s）。
                    # 提前 break 出匹配循环，交给下面的「未匹配 → 重试 → 取证」路径，
                    # 既不误伤真实更新等待，也不会出现「跑了 12 分钟却没落失败」。
                    # 只判窗口不可用，不判被遮挡：异环可后台点击，被挡住不影响操作。
                    if (
                        dead_window_timeout > 0
                        and search_scope != "screen"
                        and not _window_available_for_ocr(hwnd)
                    ):
                        now = time.time()
                        if window_dead_since is None:
                            window_dead_since = now
                        elif now - window_dead_since >= dead_window_timeout:
                            logger.warning(
                                f"[{flow_key}] 步骤{step_no}「{name}」窗口连续 "
                                f"{now - window_dead_since:.0f}s 不可用（最小化/句柄失效），"
                                f"提前结束本轮匹配，转入重试/失败取证"
                            )
                            break
                    else:
                        window_dead_since = None

                    # 每次匹配前置顶窗口，防止其他窗口（如游戏退出后弹出的启动器）遮挡
                    _focus_window(hwnd)

                    # 自带内部循环的动作跳过模板匹配，直接进分发
                    if action in _BUILTIN_ACTIONS:
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
                                exclude_texts=step_exclude_texts,
                                match_mode=step_ocr_match_mode,
                                anchor=step_ocr_anchor,
                                near=step_ocr_near,
                                ambiguous_action=step_ocr_ambiguous,
                                debug_tag=step_ocr_debug_tag,
                                save_debug=step_ocr_debug,
                            )
                            if center:
                                logger.info(f"[{target_label}] 全屏OCR匹配成功 @ {center}")
                                match_method = "ocr_screen"
                    else:
                        # 步骤级 OCR 区域（窗口相对，实时换算）：紧裁剪让小字特征
                        # 不被全窗检测漏掉（XM 日志面板 10px 字全窗扫必丢）
                        configured_region = None
                        if step_ocr_region_frac:
                            configured_region = _step_ocr_region(
                                step_ocr_region_frac, _window_rect(hwnd)
                            )
                        # 高精度引擎（onnxocr v6，GPU）：步骤显式 opt-in
                        step_engine = None
                        if step.get("ocr_precision"):
                            try:
                                from .ocr_click_tester import (
                                    _get_precision_engine,
                                )
                                step_engine = _get_precision_engine(logger)
                            except Exception as exc:
                                logger.debug(f"[{flow_key}] 高精度引擎获取失败: {exc}")
                        center, match_method = _locate_with_ocr_fallback(
                            logger=logger,
                            hwnd=hwnd,
                            template_path=template,
                            target_text=target_texts if step_enable_ocr else [],
                            scales=scales,
                            confidences=confidences,
                            enable_ocr=step_enable_ocr,
                            local_region=configured_region or local_region,
                            exclude_texts=step_exclude_texts,
                            match_mode=step_ocr_match_mode,
                            anchor=step_ocr_anchor,
                            near=step_ocr_near,
                            ambiguous_action=step_ocr_ambiguous,
                            debug_tag=step_ocr_debug_tag,
                            save_debug=step_ocr_debug,
                            dismiss_texts=step_dismiss_texts or None,
                            engine=step_engine,
                        )
                    if center and match_method == "ocr_dismiss":
                        # 等待途中出现弹窗（如绝区零月卡「今日到账」）：点掉后继续等
                        # 目标，不算步骤命中（center 复位，同一超时窗口内继续轮询）。
                        # 必须前台短按住点击：绝区零只吃前台真实点击（2026-11-04 实机）。
                        raw_dismiss_hold = step.get("click_hold_sec", cfg.get("click_hold_sec", None))
                        try:
                            dismiss_hold = (
                                None if raw_dismiss_hold is None else max(0.0, float(raw_dismiss_hold))
                            )
                        except (TypeError, ValueError):
                            dismiss_hold = None
                        _click_window_local(hwnd, center[0], center[1], clicks=1,
                                            background=False, hold_sec=dismiss_hold)
                        logger.info(
                            f"[{flow_key}] 步骤{step_no}「{name}」已点击弹窗 "
                            f"({center[0]}, {center[1]}) [方式: ocr_dismiss]，继续等待"
                        )
                        center = None
                        match_method = None
                        time.sleep(1.5)  # 等弹窗关闭动画，避免下一轮 OCR 还扫到残影重复点
                    if center:
                        break
                time.sleep(search_interval)

            matched = (
                (action == "wait_process" and match_method == "process")
                or action in _BUILTIN_ACTIONS
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
                    _collect_failure_evidence(
                        logger=logger,
                        flow_key=flow_key,
                        step=step,
                        name=name,
                        step_no=step_no,
                        action=action,
                        target_texts=target_texts,
                        template=template,
                        hwnd=hwnd,
                        retry_count=step_attempts[i],
                        max_attempts=max_attempts,
                        step_enable_ocr=step_enable_ocr,
                    )

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
            steps_hit.add(name)
            steps_hit_ts[name] = time.time()

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
                    # 2026-11-04：所有前台点击默认极短按住（见 DEFAULT_CLICK_HOLD_SEC）。
                    # 未配置 → None 用全局默认；显式 0 → 瞬时；>0 → 指定时长（绝区零 0.08）。
                    raw_hold = step.get("click_hold_sec", cfg.get("click_hold_sec", None))
                    try:
                        hold_sec = None if raw_hold is None else max(0.0, float(raw_hold))
                    except (TypeError, ValueError):
                        hold_sec = None
                    effective_hold = None if use_background else hold_sec
                    if search_scope == "screen":
                        _pyautogui_click_guarded(center[0], center[1], clicks=clicks,
                                                 hold_sec=effective_hold)
                    else:
                        _click_window_local(hwnd, center[0], center[1], clicks=clicks,
                                            background=use_background, hold_sec=effective_hold)
                    if use_background:
                        hold_tag = ""
                    else:
                        shown = DEFAULT_CLICK_HOLD_SEC if effective_hold is None else effective_hold
                        hold_tag = f"[按住: {shown:.2f}s]" if shown > 0 else "[瞬时]"
                    logger.info(f"[{flow_key}] 已点击: ({center[0]}, {center[1]}) "
                                f"[方式: {match_method}][点击: {'后台' if use_background else '前台'}]{hold_tag}")

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
            elif action in _BUILTIN_ACTIONS:
                dispatch_ok, dispatch_msg, hwnd, pid = _dispatch_step_action(
                    logger=logger,
                    flow_key=flow_key,
                    step=step,
                    name=name,
                    step_no=step_no,
                    action=action,
                    hwnd=hwnd,
                    pid=pid,
                    scales=scales,
                    confidences=confidences,
                    stop_check=stop_check,
                )
                if not dispatch_ok:
                    if "用户停止" in str(dispatch_msg):
                        # 用户主动停止不是故障：只留一行日志，不写 flow_failures
                        # （否则每次手动停止都多一条假失败记录）。
                        logger.info(f"[{flow_key}] 步骤{step_no}由用户停止: {name}")
                        return False, dispatch_msg
                    # 2026-11-03 实锤（崩铁自塑尘脂）：action 分支失败以前三层都不打日志，
                    # 只留日报一行 ❌，连 flow_failures 都没记录。这里补日志 + 统一取证
                    # （截图 + 当帧 OCR 写库），下次首跑失败能直接看图定位。
                    logger.error(
                        f"[{flow_key}] 步骤{step_no}执行失败: {name} ({dispatch_msg})"
                    )
                    _collect_failure_evidence(
                        logger=logger,
                        flow_key=flow_key,
                        step=step,
                        name=name,
                        step_no=step_no,
                        action=action,
                        target_texts=target_texts,
                        template=template,
                        hwnd=hwnd,
                        retry_count=step_attempts.get(i, 0),
                        max_attempts=max_attempts,
                        step_enable_ocr=step_enable_ocr,
                        error=str(dispatch_msg)[:500],
                    )
                    return False, dispatch_msg

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
                        # 2026-11-04 实锤：绝区零「点击进入游戏」三次「元素 10s 未消失」，
                        # 一张失败截图都没有、flow_failures 里也没有 ZZZ_MONTHLY。取证此前
                        # 只覆盖「未匹配模板/OCR」(2456) 与「内建动作失败」(2574) 两条路径，
                        # 确认失败重试耗尽直接 return，事后完全查不出当时窗口长什么样。
                        logger.error(
                            f"[{flow_key}] 步骤{step_no}确认失败且重试耗尽: {name} ({reason})"
                        )
                        _collect_failure_evidence(
                            logger=logger,
                            flow_key=flow_key,
                            step=step,
                            name=name,
                            step_no=step_no,
                            action=action,
                            target_texts=target_texts,
                            template=template,
                            hwnd=hwnd,
                            retry_count=step_attempts[i],
                            max_attempts=max_attempts,
                            step_enable_ocr=step_enable_ocr,
                            error=f"确认失败: {reason}"[:500],
                        )
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
        # 2026-11-05：复用用户已经开着的游戏窗口时，进程不是本流程拉起来的，
        # 买完直接杀掉会误伤正在玩的游戏（NTE_MONTHLY 的 skip_steps_if_process
        # 就是为接管已运行游戏设计的）。只有自己启动的才按配置关闭；确实要连
        # 复用窗口一起关，显式配 close_reused_window: true。
        if close_on_exit and reused_window and not cfg.get("close_reused_window", False):
            logger.info(f"[{flow_key}] 复用已运行窗口，保留游戏进程")
        elif close_on_exit and (flow_ok or close_on_fail):
            close_flow_processes(logger, flow_key, pid, window_process_names)
        elif close_on_exit:
            logger.info(f"[{flow_key}] 流程未成功，按配置保留进程(close_process_on_fail=false)")
    return True, f"[{flow_key}] 流程执行成功"