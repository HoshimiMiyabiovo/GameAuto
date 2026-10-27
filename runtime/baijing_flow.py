"""白荆回廊（Seed / GateMK）启动器 + 游戏内自动化流程。

流程概览：
1. 点击启动器「开始游戏」→ 等待 GateMK-Win64-Shipping.exe 进程（游戏本体）
2. 游戏窗口内：点击「关闭公告」→ 点击「进入游戏」
3. 进入游戏后延长等待 60s（加载/过场）
4. 常态匹配阶段（动态背景，需多次匹配）：
   - 出现「点击屏幕继续」→ 点击（推进过场/对话）
   - 出现「关闭」→ 点击（关闭异常/未知弹窗）
   - 未知态 → 连续按 ESC 恢复（最多 10 次）
   - 纯模板匹配连续命中（默认 5 帧）识别到「主界面」→ 视为进入主界面，流程结束
   所有后续任务都从主界面这一页启动。

日常任务（2026-10-18 起重构）：不再回放固定坐标序列，改为视觉版流程
（见 runtime/baijing_daily.py）：主界面 → 点右侧「任务」按钮（OCR 优先+
模板兜底）→ 日常任务列表 → 一键/逐个领取 → 关闭列表回主界面。
验证 / 周常仍为固定点击序列回放（click_sequences/白荆回廊-*.json）。

说明：
- 启动器可执行文件：Seed_OB_Prod.exe（位于版本目录 1.1.1.7 下）
- 游戏本体可执行文件：GateMK-Win64-Shipping.exe
- 启动器内「开始游戏」按钮通过模板 白荆回廊-开始游戏.png 联合 OCR 识别并点击
- 游戏内各 UI 通过模板匹配（白荆回廊-*.png）识别
"""

import ctypes
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import psutil
import pyautogui
from PIL import Image
import win32api
import win32con
import win32gui
import win32process

from ..settings.paths import BAIJING_VERIFY_DIR, TEMPLATES_DIR, WORKFLOW_DB_PATH
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import (
    ocr_and_template_match,
    _find_by_template,
    grab_screen,
)

# 模板路径
_TPL = {
    "launcher_start": str(TEMPLATES_DIR / "白荆回廊-开始游戏.png"),
    "close_announce": str(TEMPLATES_DIR / "白荆回廊-关闭公告.png"),
    "enter_game": str(TEMPLATES_DIR / "白荆回廊-进入游戏.png"),
    "tap_continue": str(TEMPLATES_DIR / "白荆回廊-点击屏幕继续.png"),
    "close": str(TEMPLATES_DIR / "白荆回廊-关闭.png"),
    "main_ui": str(TEMPLATES_DIR / "白荆回廊-主界面.png"),
}

# 游戏本体进程名（启动器点击「开始游戏」后由它拉起）
GAME_PROCESS = "GateMK-Win64-Shipping.exe"
# 启动器进程名（用于日志/忽略判断）
LAUNCHER_PROCESS = "Seed_OB_Prod.exe"

# ── 一条龙固定序列名 ──
# 注意：日常已改为视觉版流程（runtime/baijing_daily.py，从主界面点「任务」进
# 日常任务列表），不再回放 BAIJING_DAILY_SEQ；该 JSON 保留作历史归档，
# 视觉流程连续失败时可人工回退使用。
BAIJING_DAILY_SEQ = "白荆回廊-日常"
BAIJING_VERIFY_SEQ = "白荆回廊-验证"
BAIJING_WEEKLY_SEQ = "白荆回廊-每周商店购买"
BAIJING_GAME_KEY = "baijing"
BAIJING_GAME_NAME = "白荆回廊"
# 验证截图：卡片内压缩预览宽（像素），原图按比例缩放后转 JPEG
BAIJING_VERIFY_MAX_WIDTH = 480
BAIJING_VERIFY_JPEG_QUALITY = 72
# 仅保留最近 N 天截图
BAIJING_VERIFY_KEEP_DAYS = 7

# 进入游戏后的固定加载/过场等待秒数（用户要求延长至 60s）
ENTER_GAME_WAIT_SEC = 60
# 常态匹配阶段最大循环次数（每轮约 1~2s，足够覆盖正常载入与过场）
MAIN_LOOP_MAX_CYCLES = 150
# 连续 ESC 恢复的最大次数
MAX_CONSECUTIVE_ESC = 10

# ════════════════ 动态主界面识别配置 ════════════════
# 单次识别预算（秒）：全速循环，最多 3 秒给出判定
MAIN_UI_DETECT_BUDGET_SEC = 3.0
# 连续命中帧数：主界面模板需连续命中这么多帧才判定为主界面，
# 用于对抗动态背景动画导致的瞬时失配（少数字帧未命中即重置计数）。
MAIN_UI_CONSECUTIVE_FRAMES = 5
# 模板匹配阶梯阈值（取最高满足者），用于判定单帧是否命中主界面
MAIN_UI_TEMPLATE_THRESHOLDS = (0.9, 0.85, 0.8)


def _find_game_window(logger: logging.Logger, process_name: str = GAME_PROCESS) -> Optional[int]:
    """通过进程名查找可见的顶级窗口句柄（取最大窗口）。"""
    found = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            if proc.name().lower() == process_name.lower():
                title = win32gui.GetWindowText(hwnd)
                if title:  # 只要有标题的顶级窗口
                    found.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception as e:
        logger.debug(f"[白荆回廊] EnumWindows 异常: {e}")

    if not found:
        return None
    # 选最大的窗口（游戏窗口通常最大）
    best = max(found, key=lambda h: win32gui.GetWindowRect(h)[2] - win32gui.GetWindowRect(h)[0])
    return best


def _focus_game_window(logger: logging.Logger, hwnd: Optional[int]):
    """聚焦游戏窗口，确保点击/按键能生效。

    绕过 Windows 前台锁限制：模拟 ALT 键 + AttachThreadInput 兜底；
    即使前台焦点拿不到，也强制把窗口提到最前，避免截图/OCR 被 IDE 等窗口遮挡。
    """
    if not hwnd:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 绕过 Windows 前台锁限制：模拟 ALT 键
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
        foreground_ok = False
        try:
            win32gui.SetForegroundWindow(hwnd)
            foreground_ok = win32gui.GetForegroundWindow() == hwnd
        except Exception as e:
            logger.debug(f"[白荆回廊] SetForegroundWindow 失败: {e}，尝试 AttachThreadInput")
        if not foreground_ok:
            try:
                fg = win32gui.GetForegroundWindow()
                fg_thread = win32process.GetWindowThreadProcessId(fg)[0]
                target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
                attached = False
                if fg_thread != target_thread:
                    ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, True)
                    attached = True
                try:
                    win32gui.SetForegroundWindow(hwnd)
                    foreground_ok = win32gui.GetForegroundWindow() == hwnd
                finally:
                    if attached:
                        ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, False)
            except Exception as e:
                logger.debug(f"[白荆回廊] AttachThreadInput 聚焦失败: {e}")
        # 无论前台焦点是否成功，都置顶一次，保证截图/OCR 拿到的是游戏画面
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        except Exception as e:
            logger.debug(f"[白荆回廊] SetWindowPos 置顶失败: {e}")
        if foreground_ok:
            logger.debug(f"[白荆回廊] 已聚焦游戏窗口 HWND={hwnd}")
        else:
            logger.debug(f"[白荆回廊] 已置顶游戏窗口 HWND={hwnd}（前台焦点可能未取得）")
    except Exception as e:
        logger.debug(f"[白荆回廊] 聚焦游戏窗口失败: {e}")


def _window_region(hwnd: Optional[int]) -> Optional[Tuple[int, int, int, int]]:
    """返回 (left, top, width, height) 窗口区域，失败返回 None。"""
    if not hwnd:
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = max(0, right - left)
        height = max(0, bottom - top)
        if width == 0 or height == 0:
            return None
        return left, top, width, height
    except Exception:
        return None


def _match_template_in_screenshot(
    template_img: Image.Image,
    screenshot: Image.Image,
    thresholds: Tuple[float, ...] = MAIN_UI_TEMPLATE_THRESHOLDS,
) -> Tuple[Optional[Tuple[int, int]], float]:
    """在已抓取的截图上直接做模板匹配，避免重复截图。

    返回 (中心坐标, 最高置信度)，未命中返回 (None, 0.0)。
    """
    try:
        import cv2
        tpl_gray = cv2.cvtColor(np.asarray(template_img), cv2.COLOR_RGB2GRAY)
        scr_gray = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2GRAY)
        if tpl_gray.shape[0] > scr_gray.shape[0] or tpl_gray.shape[1] > scr_gray.shape[1]:
            return None, 0.0
        match_result = cv2.matchTemplate(scr_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
        for thresh in thresholds:
            _, max_val, _, max_loc = cv2.minMaxLoc(match_result)
            if max_val >= thresh:
                h, w = tpl_gray.shape[:2]
                return (max_loc[0] + w // 2, max_loc[1] + h // 2), max_val
    except Exception:
        pass
    return None, 0.0


def detect_baijing_main_ui(
    logger: logging.Logger,
    hwnd: Optional[int],
    budget_sec: float = MAIN_UI_DETECT_BUDGET_SEC,
) -> Tuple[bool, Dict]:
    """纯模板匹配，全速连续判定是否处于主界面（本次不使用 OCR）。

    在 budget_sec 内连续抓取屏幕（WGC GPU 直采），对每帧做
    白荆回廊-主界面.png 模板匹配：
      - 某帧命中 → 连续命中计数 +1；
      - 某帧未命中 → 连续命中计数清零（应对动态背景动画瞬时失配）；
      - 连续命中达到 MAIN_UI_CONSECUTIVE_FRAMES（默认 5）帧即判定为主界面。
    超时未达连续命中数则返回 False。
    """
    region = _window_region(hwnd)

    # 加载主界面模板图（PIL，避免 OpenCV 中文路径问题）
    template_path = _TPL.get("main_ui", "")
    template_img: Optional[Image.Image] = None
    if template_path and os.path.exists(template_path):
        try:
            template_img = Image.open(template_path)
        except Exception as e:
            logger.debug(f"[白荆回廊] 主界面模板加载失败: {e}")

    if template_img is None:
        logger.warning(f"[白荆回廊] 主界面模板缺失，无法做模板匹配: {template_path}")
        return False, {"reason": "template_missing", "frames": 0, "elapsed_sec": 0.0}

    start = time.perf_counter()
    deadline = start + budget_sec

    frame_count = 0
    consecutive = 0
    last_center: Optional[Tuple[int, int]] = None
    last_conf = 0.0
    last_log = start

    while time.perf_counter() < deadline:
        frame_count += 1
        current_region = _window_region(hwnd) if hwnd else region
        if current_region:
            region = current_region

        # ── 抓取一帧（WGC GPU 直采，优先窗口区域）──
        try:
            screenshot = grab_screen(region=region)
        except Exception as e:
            logger.debug(f"[白荆回廊] 主界面截图失败: {e}")
            continue

        # ── 模板匹配（在已抓取的截图上直接匹配，避免重复 GDI 截图）──
        try:
            center, tpl_conf = _match_template_in_screenshot(template_img, screenshot)
        except Exception:
            center, tpl_conf = None, 0.0

        if center:
            consecutive += 1
            last_center = center
            last_conf = tpl_conf
            logger.info(
                f"[白荆回廊] 主界面模板命中（第 {consecutive} 帧连续命中）@ {center} "
                f"(置信度 {tpl_conf:.2f})"
            )
            if consecutive >= MAIN_UI_CONSECUTIVE_FRAMES:
                elapsed = time.perf_counter() - start
                evidence = {
                    "reason": "consecutive_template_matched",
                    "consecutive": consecutive,
                    "template_center": last_center,
                    "template_conf": last_conf,
                    "frames": frame_count,
                    "elapsed_sec": elapsed,
                }
                logger.info(
                    f"[白荆回廊] 连续 {consecutive} 帧命中主界面模板，判定已处于主界面"
                    f"（共 {frame_count} 帧，耗时 {elapsed:.2f}s）"
                )
                return True, evidence
        else:
            if consecutive > 0:
                logger.debug(
                    f"[白荆回廊] 主界面模板本帧未命中，连续命中计数重置（之前 {consecutive}）"
                )
            consecutive = 0

        # 全速循环，不主动 sleep；每 1 秒输出一次调试心跳
        now = time.perf_counter()
        if now - last_log >= 1.0:
            logger.debug(
                f"[白荆回廊] 主界面识别中… 已识别 {frame_count} 帧，"
                f"当前连续命中 {consecutive}"
            )
            last_log = now

    elapsed = time.perf_counter() - start
    evidence = {
        "reason": "budget_exhausted",
        "consecutive": consecutive,
        "template_center": last_center,
        "template_conf": last_conf,
        "frames": frame_count,
        "elapsed_sec": elapsed,
    }
    logger.info(
        f"[白荆回廊] {budget_sec:.1f}s 内未连续 {MAIN_UI_CONSECUTIVE_FRAMES} 帧命中主界面模板"
        f"（最多连续 {consecutive} 帧，共 {frame_count} 帧），继续常态匹配"
    )
    return False, evidence


def _match_template(
    logger: logging.Logger,
    template_path: str,
    hwnd: Optional[int],
    tries: int = 3,
    interval: float = 0.4,
) -> Optional[Tuple[int, int]]:
    """模板匹配（带重试以对抗动态背景动画），返回中心坐标或 None。

    先按游戏窗口区域匹配，失败再退全屏；confidence 由 ocr_click_tester 内部
    阶梯下降（0.9/0.85/0.8），tries 次重试应对动画帧导致的瞬时失配。
    """
    if not template_path or not os.path.exists(template_path):
        logger.warning(f"[白荆回廊] 模板文件不存在: {template_path}")
        return None
    region = _window_region(hwnd)
    for _ in range(max(1, tries)):
        center = _find_by_template(logger, template_path, region=region)
        if not center and region:
            center = _find_by_template(logger, template_path, region=None)
        if center:
            return center
        time.sleep(interval)
    return None


def _wait_process(logger: logging.Logger, process_name: str, timeout: int = 300, stop_check=None) -> Optional[int]:
    """等待指定进程启动，返回 PID。stop_check 命中则立即返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_check and stop_check():
            logger.info(f"[白荆回廊] 等待 {process_name} 期间收到停止信号，中止等待")
            return None
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == process_name.lower():
                    logger.info(f"[白荆回廊] 游戏本体进程已启动: {process_name} PID={proc.info['pid']}")
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(2)
    logger.warning(f"[白荆回廊] 等待游戏本体进程超时: {process_name} ({timeout}s)")
    return None


def _find_and_click(
    logger: logging.Logger,
    template_key: str,
    target_text: str,
    timeout: int = 60,
    poll_interval: float = 1.5,
    stop_check=None,
) -> bool:
    """联合模板+OCR 定位启动器「开始游戏」并点击，返回是否成功。"""
    tpl = _TPL.get(template_key, "")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_check and stop_check():
            logger.info("[白荆回廊] 点击等待期间收到停止信号，中止")
            return False
        center, method = ocr_and_template_match(target_text, tpl, logger, hwnd=None)
        if center:
            _pyautogui_click_guarded(center[0], center[1])
            logger.info(
                f"[白荆回廊] 已点击启动器\"{target_text}\" [方式: {method}] @ ({center[0]}, {center[1]})"
            )
            return True
        time.sleep(poll_interval)
    logger.debug(f"[白荆回廊] 未找到启动器\"{target_text}\" (超时 {timeout}s)")
    return False


def _click_template_until(
    logger: logging.Logger,
    template_key: str,
    label: str,
    hwnd: Optional[int],
    timeout: int = 90,
    poll_interval: float = 1.5,
    stop_check=None,
) -> bool:
    """在 timeout 内持续匹配并点击指定模板（用于进入阶段的确定性步骤）。"""
    tpl = _TPL.get(template_key, "")
    if not tpl:
        logger.error(f"[白荆回廊] 模板键不存在: {template_key}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_check and stop_check():
            logger.info(f"[白荆回廊] 等待「{label}」期间收到停止信号，中止")
            return False
        center = _match_template(logger, tpl, hwnd, tries=2, interval=0.3)
        if center:
            _pyautogui_click_guarded(center[0], center[1])
            logger.info(f"[白荆回廊] 已点击\"{label}\" @ {center}")
            return True
        time.sleep(poll_interval)
    logger.debug(f"[白荆回廊] 未找到\"{label}\" (超时 {timeout}s)")
    return False


def run_baijing_ingame_flow(config: dict, logger: logging.Logger, stop_check=None) -> bool:
    """白荆回廊启动器 + 游戏内自动化主流程。

    1) 点启动器开始游戏 → 等待游戏本体进程
    2) 游戏窗口内：关闭公告 → 进入游戏 → 延长等待 60s
    3) 常态匹配直到识别主界面（点击屏幕继续 / 关闭异常 / 连续 ESC 恢复）

    返回值由 automation_controller 忽略（调用后直接进入 _monitor_game），
    这里返回 True 表示流程已尽力执行完进入步骤。
    stop_check: 无参回调，返回 True 即尽快中止并返回 False。
    """
    def _stopped() -> bool:
        try:
            return bool(stop_check and stop_check())
        except Exception:
            return False

    logger.info("=" * 50 + "\n[白荆回廊] 开始启动器 + 游戏内流程\n" + "=" * 50)

    # ════════════ 启动阶段（线性，状态确定） ════════════
    logger.info("[白荆回廊] 启动: 点击启动器「开始游戏」按钮")
    if not _find_and_click(logger, "launcher_start", "开始游戏", timeout=60, stop_check=stop_check):
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
        else:
            logger.error("[白荆回廊] 启动器「开始游戏」按钮未找到")
        return False
    for _ in range(6):
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
            return False
        time.sleep(0.5)

    logger.info(f"[白荆回廊] 启动: 等待游戏本体进程 {GAME_PROCESS}")
    game_pid = _wait_process(logger, GAME_PROCESS, timeout=300, stop_check=stop_check)
    if not game_pid:
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
        else:
            logger.error(f"[白荆回廊] 游戏本体进程 {GAME_PROCESS} 未启动")
        return False

    # 查找游戏窗口句柄
    hwnd = None
    for _ in range(30):
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
            return False
        hwnd = _find_game_window(logger, GAME_PROCESS)
        if hwnd:
            logger.info(f"[白荆回廊] 游戏窗口已就绪 HWND={hwnd}")
            break
        time.sleep(2)
    if not hwnd:
        logger.warning("[白荆回廊] 未找到游戏窗口句柄，后续步骤将按全屏匹配")

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    # ════════════ 进入阶段（关闭公告 → 进入游戏 → 延长等待） ════════════
    logger.info("[白荆回廊] 进入阶段: 关闭公告")
    _focus_game_window(logger, hwnd)
    _click_template_until(logger, "close_announce", "关闭公告", hwnd, timeout=45, stop_check=stop_check)
    if _stopped():
        logger.info("[白荆回廊] 用户停止，退出进入流程")
        return False

    logger.info("[白荆回廊] 进入阶段: 点击「进入游戏」")
    _focus_game_window(logger, hwnd)
    if not _click_template_until(logger, "enter_game", "进入游戏", hwnd, timeout=120, stop_check=stop_check):
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
            return False
        logger.warning("[白荆回廊] 未找到「进入游戏」按钮，可能已在游戏中，继续后续流程")

    logger.info(f"[白荆回廊] 进入游戏，延长等待 {ENTER_GAME_WAIT_SEC}s（加载/过场）...")
    waited = 0.0
    while waited < ENTER_GAME_WAIT_SEC:
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
            return False
        time.sleep(min(1.0, ENTER_GAME_WAIT_SEC - waited))
        waited += 1.0

    # 预热 WGC 截图（首帧初始化较慢），让 detect_baijing_main_ui 内第一帧更快；
    # 主界面判定已改为纯模板匹配，不再预热 OCR 引擎。
    logger.info("[白荆回廊] 预热 WGC 截图…")
    try:
        grab_screen(region=_window_region(hwnd))
        logger.info("[白荆回廊] WGC 截图已预热")
    except Exception as e:
        logger.debug(f"[白荆回廊] WGC 截图预热失败，将回退 GDI: {e}")

    # ════════════ 常态匹配阶段（动态背景，多次匹配直到主界面） ════════════
    logger.info(
        "[白荆回廊] 进入常态匹配：点击屏幕继续 / 关闭异常界面 / 连续ESC恢复，"
        f"直至识别「主界面」（ESC 最多连续 {MAX_CONSECUTIVE_ESC} 次）"
    )
    reached_main = False
    esc_count = 0
    for cycle in range(MAIN_LOOP_MAX_CYCLES):
        if _stopped():
            logger.info("[白荆回廊] 用户停止，退出进入流程")
            return False
        # 窗口句柄可能中途变化，重试查找
        if hwnd is None:
            hwnd = _find_game_window(logger, GAME_PROCESS)
        _focus_game_window(logger, hwnd)

        # 1) 主界面（目标态，优先判定）
        # 使用 GPU 加速 OCR + 模板匹配综合判定，每次最多 3 秒全速识别
        is_main, evidence = detect_baijing_main_ui(logger, hwnd)
        if is_main:
            logger.info(
                f"[白荆回廊] 综合判定已进入主界面（证据: {evidence}）"
            )
            reached_main = True
            break

        # 2) 点击屏幕继续（出现即点击，推进过场/对话）
        cont_c = _match_template(logger, _TPL["tap_continue"], hwnd, tries=2, interval=0.3)
        if cont_c:
            _pyautogui_click_guarded(cont_c[0], cont_c[1])
            logger.info(f"[白荆回廊] 点击「点击屏幕继续」 @ {cont_c}（推进过场）")
            esc_count = 0  # 出现了有效操作，重置连续 ESC 计数
            time.sleep(1.0)
            continue

        # 3) 关闭异常/未知弹窗
        close_c = _match_template(logger, _TPL["close"], hwnd, tries=2, interval=0.3)
        if close_c:
            _pyautogui_click_guarded(close_c[0], close_c[1])
            logger.info(f"[白荆回廊] 点击「关闭」关闭异常界面 @ {close_c}")
            esc_count = 0
            time.sleep(1.0)
            continue

        # 4) 未知态：连续按 ESC 恢复（最多 MAX_CONSECUTIVE_ESC 次）
        if esc_count < MAX_CONSECUTIVE_ESC:
            pyautogui.press("escape")
            esc_count += 1
            logger.info(f"[白荆回廊] 未知态，按 ESC 恢复 (#{esc_count}/{MAX_CONSECUTIVE_ESC})")
            time.sleep(1.0)
            continue

        # 5) ESC 已达上限仍未知：保持对主界面/点击屏幕继续/关闭 的常态匹配，空转等待
        logger.debug(f"[白荆回廊] 未匹配任何态且 ESC 达上限，空转等待 (cycle #{cycle + 1})")
        time.sleep(2.0)

    if reached_main:
        logger.info("=" * 50 + "\n[白荆回廊] 已进入主界面，游戏内流程完成\n" + "=" * 50)
    else:
        logger.warning(
            "[白荆回廊] 未识别到「主界面」（循环结束/ESC 达上限），"
            "已尽力进入；后续交由监控流程与任务调度"
        )
    return True


# ════════════════════════════════════════════════════════════════════
#  一条龙扩展：日常 / 验证(WGC截图) / 周常 / 关闭 + 进度落盘
# ════════════════════════════════════════════════════════════════════

def _baijing_today_key() -> str:
    """与 daily_progress_tracker._today_key 同步：04:00 为日界。"""
    try:
        from ..services.daily_progress_tracker import _today_key as _dp_today
        return _dp_today()
    except Exception:
        import time as _t
        tm = _t.localtime()
        if tm.tm_hour < 4:
            tm_y = _t.localtime(_t.time() - 86400)
            return _t.strftime("%Y-%m-%d", tm_y)
        return _t.strftime("%Y-%m-%d", tm)


def _resolve_baijing_window(logger: logging.Logger) -> Optional[int]:
    """优先按进程名定位白荆窗口，失败回退标题前缀 '白荆回廊'。"""
    hwnd = _find_game_window(logger, GAME_PROCESS)
    if hwnd:
        return hwnd
    # 版本号会变，用前缀匹配
    try:
        import win32gui as _wg
        found = []
        def _cb(h, _):
            if not _wg.IsWindowVisible(h):
                return
            t = _wg.GetWindowText(h) or ""
            if "白荆回廊" in t:
                found.append(h)
        _wg.EnumWindows(_cb, None)
        if found:
            best = max(found, key=lambda h: _wg.GetWindowRect(h)[2] - _wg.GetWindowRect(h)[0])
            logger.info(f"[白荆回廊] 通过标题回退定位窗口 HWND={best}")
            return best
    except Exception:
        pass
    return None


def _prune_old_verify_images(logger: logging.Logger):
    """保留最近 N 天截图，其余删除。"""
    try:
        import glob as _glob
        import time as _t
        dir_p = BAIJING_VERIFY_DIR
        if not dir_p.exists():
            return
        now = _t.time()
        cutoff = now - BAIJING_VERIFY_KEEP_DAYS * 86400
        for fp in dir_p.glob("*.jpg"):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink()
                    logger.debug(f"[白荆回廊] 清理过期验证图: {fp.name}")
            except Exception:
                pass
        for fp in dir_p.glob("*.png"):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink()
                    logger.debug(f"[白荆回廊] 清理过期验证图: {fp.name}")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[白荆回廊] 清理验证图失败: {e}")


def _capture_and_compress_verify(
    logger: logging.Logger, hwnd: Optional[int]
) -> Optional[str]:
    """WGC 抓取验证画面→压缩→落盘，返回相对路径（供 extra_json）。"""
    try:
        from pathlib import Path as _P
        import time as _t
        region = _window_region(hwnd)
        try:
            img = grab_screen(region=region)
        except Exception as e:
            logger.warning(f"[白荆回廊] 截图失败，回退 GDI: {e}")
            img = grab_screen(region=region)
        # 压缩：按宽度等比缩放至 MAX_WIDTH，转 JPEG
        w, h = img.size
        if w > BAIJING_VERIFY_MAX_WIDTH:
            ratio = BAIJING_VERIFY_MAX_WIDTH / float(w)
            nh = max(1, int(h * ratio))
            img = img.resize((BAIJING_VERIFY_MAX_WIDTH, nh), Image.LANCZOS)
        BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
        date_key = _baijing_today_key()
        # 文件名：YYYY-MM-DD.jpg；同一天覆盖，保证卡片只看最新
        out_path = BAIJING_VERIFY_DIR / f"{date_key}.jpg"
        # JPEG 压缩
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (0, 0, 0))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(str(out_path), "JPEG", quality=BAIJING_VERIFY_JPEG_QUALITY, optimize=True)
        # 清理旧图
        _prune_old_verify_images(logger)
        # 返回相对 artifacts 的路径，便于 bridge 转 file://
        try:
            from ..settings.paths import BASE_DIR
            rel = out_path.relative_to(BASE_DIR)
            return str(rel).replace("\\", "/")
        except Exception:
            return str(out_path)
    except Exception as e:
        logger.warning(f"[白荆回廊] 验证截图保存失败: {e}", exc_info=True)
        return None


def _record_baijing_task(
    logger: logging.Logger,
    task_key: str,
    task_name: str,
    status: str,
    message: str = "",
    extra: Optional[Dict] = None,
):
    """写 daily_progress，失败不抛异常。"""
    try:
        from ..services.daily_progress_tracker import get_tracker
        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        tracker.record_task(
            game_key=BAIJING_GAME_KEY,
            game_name=BAIJING_GAME_NAME,
            task_key=task_key,
            task_name=task_name,
            status=status,
            source="script",
            message=message,
            extra=extra or {},
        )
    except Exception as e:
        logger.warning(f"[白荆回廊] 写入进度失败 {task_key}: {e}")


def _ensure_baijing_window_valid(logger: logging.Logger, hwnd: Optional[int]) -> Optional[int]:
    """校验句柄有效性，无效则重找；返回有效句柄或 None。"""
    try:
        import win32gui as _wg
        if hwnd and _wg.IsWindow(hwnd) and _wg.IsWindowVisible(hwnd):
            return hwnd
    except Exception:
        pass
    # 重找
    nh = _resolve_baijing_window(logger)
    if nh:
        logger.info(f"[白荆回廊] 窗口句柄已刷新 {hwnd} -> {nh}")
    return nh


def _play_sequence_with_verify_capture(
    logger: logging.Logger,
    seq_name: str,
    hwnd: Optional[int],
    stop_check=None,
) -> Tuple[bool, Optional[str]]:
    """回放固定序列；在 kind==mark 时做 WGC 截图（压缩）。返回 (是否成功, 截图相对路径)。

    安全策略：窗口匹配失败则直接失败，绝不回退绝对坐标盲点，避免点到桌面/其他窗口。
    """
    verify_path: Optional[str] = None
    try:
        from .click_sequence import ClickSequencePlayer
        player = ClickSequencePlayer(logger)
        seq = player.load(seq_name)

        # 窗口句柄获取与校验（带重试，避免启动瞬间窗口未就绪）
        if hwnd is None or not _ensure_baijing_window_valid(logger, hwnd):
            hwnd = _resolve_baijing_window(logger)
        if hwnd is None:
            for _ in range(5):
                time.sleep(1.0)
                hwnd = _resolve_baijing_window(logger)
                if hwnd:
                    break
        if hwnd is None:
            logger.error(f"[白荆回廊] 序列 {seq_name} 窗口匹配失败（未找到白荆回廊窗口），已中止回放避免盲点")
            _record_baijing_task(logger, "window_match", "窗口匹配", "failed", f"{seq_name} 未找到窗口")
            return False, None
        # 聚焦
        _focus_game_window(logger, hwnd)
        time.sleep(0.6)

        # 适配 stop_check -> threading.Event
        stop_event = None
        if stop_check is not None:
            class _Adapter:
                def is_set(self):  # noqa
                    try:
                        return bool(stop_check())
                    except Exception:
                        return False
            stop_event = _Adapter()

        # 手动逐事件回放以便在 mark 处截图；窗口 rect 仅取自 hwnd，不依赖旧版标题关键字
        total = len(seq.events)
        if total == 0:
            logger.warning(f"[白荆回廊] 序列为空: {seq_name}")
            return False, None
        logger.info(f"[白荆回廊] 开始回放: {seq_name} 共 {total} 步 HWND={hwnd}")
        rect = _window_region(hwnd)
        if not rect:
            logger.error(f"[白荆回廊] 序列 {seq_name} 无法获取窗口区域，HWND={hwnd} 已失效")
            return False, None

        for idx, ev in enumerate(seq.events, start=1):
            if stop_event and stop_event.is_set():
                logger.info("[白荆回廊] 收到停止信号，序列中断")
                return False, verify_path
            gap = ev.gap_after if ev.gap_after is not None else 0.5
            kind = getattr(ev, "kind", "click")
            if kind == "mark":
                logger.info(f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: ✚ 标记，触发截图")
                hwnd = _ensure_baijing_window_valid(logger, hwnd)
                if hwnd is None:
                    logger.error(f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 窗口丢失，截图跳过")
                    continue
                # 稍等 UI 稳定 0.5s 再抓
                time.sleep(0.5)
                _focus_game_window(logger, hwnd)
                p = _capture_and_compress_verify(logger, hwnd)
                if p:
                    verify_path = p
                    logger.info(f"[白荆回廊] 验证截图已保存: {p}")
                else:
                    logger.warning("[白荆回廊] 标记处截图失败")
            elif kind == "key":
                # 按键前确保窗口仍在前台（避免按键发到别的窗口）
                hwnd = _ensure_baijing_window_valid(logger, hwnd) or hwnd
                if hwnd:
                    _focus_game_window(logger, hwnd)
                try:
                    from .click_sequence import _press_recorded_key
                    if not _press_recorded_key(getattr(ev, "key", "")):
                        logger.warning(f"[白荆回廊] 按键无法识别: {ev.key}")
                    else:
                        logger.info(f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 按键 {ev.key}")
                except Exception as e:
                    logger.warning(f"[白荆回廊] 按键回放异常 {ev.key}: {e}")
            else:
                # click - 必须基于窗口相对坐标，窗口失效/无相对坐标则直接失败，绝不盲点绝对坐标
                hwnd = _ensure_baijing_window_valid(logger, hwnd)
                if hwnd is None:
                    logger.error(f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 窗口丢失，停止回放避免误触桌面")
                    return False, verify_path
                rect = _window_region(hwnd)
                if not rect:
                    logger.error(f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 无法获取窗口区域 HWND={hwnd}")
                    return False, verify_path
                if ev.rel_x is None or ev.rel_y is None:
                    logger.error(
                        f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 缺少相对坐标，拒绝盲点绝对坐标 ({ev.x},{ev.y})"
                    )
                    return False, verify_path
                sx = int(rect[0] + ev.rel_x)
                sy = int(rect[1] + ev.rel_y)
                # 越界校验：点击点必须在当前窗口内
                if not (rect[0] <= sx <= rect[0] + rect[2] and rect[1] <= sy <= rect[1] + rect[3]):
                    logger.warning(
                        f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 坐标越界 ({sx},{sy}) 不在窗口 {rect} 内，已跳过避免误触"
                    )
                    return False, verify_path
                try:
                    _focus_game_window(logger, hwnd)
                except Exception:
                    pass
                try:
                    _pyautogui_click_guarded(sx, sy, button=getattr(ev, "button", "left") or "left")
                    logger.info(
                        f"[白荆回廊] 序列 {seq_name} 步骤{idx}/{total}: 点击 ({sx},{sy}) rel=({ev.rel_x},{ev.rel_y}) btn={ev.button} HWND={hwnd}"
                    )
                except Exception as e:
                    logger.warning(f"[白荆回廊] 点击失败 ({sx},{sy}): {e}")
                    return False, verify_path
            if gap and gap > 0 and idx < total:
                # 中断可响应
                slept = 0.0
                while slept < gap:
                    if stop_event and stop_event.is_set():
                        return False, verify_path
                    step = min(0.2, gap - slept)
                    time.sleep(step)
                    slept += step
        logger.info(f"[白荆回廊] 序列完成: {seq_name}")
        return True, verify_path
    except FileNotFoundError as e:
        logger.error(f"[白荆回廊] 序列不存在: {seq_name} ({e})")
        return False, None
    except Exception as e:
        logger.error(f"[白荆回廊] 序列回放异常 {seq_name}: {e}", exc_info=True)
        return False, verify_path


def _maybe_run_weekly(logger: logging.Logger, hwnd: Optional[int], stop_check=None) -> Optional[str]:
    """每周执行一次周常：俱乐部商店「全量购买」（2026-10-18 起视觉版）。

    视觉版跑不通（未处于主界面 / 商店页没扫到商品且状态异常）时回退旧的固定坐标
    序列 click_sequences/白荆回廊-每周商店购买.json，保证每周至少还能买一次。
    """
    try:
        from ..storage.workflow_db import WorkflowDB
        import time as _t
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        week_key = _t.strftime("%G-W%V")
        meta_key = f"weekly_sequence:{BAIJING_WEEKLY_SEQ}"
        if db.get_task_meta(meta_key) == week_key:
            logger.info(f"[白荆回廊] 周常 {BAIJING_WEEKLY_SEQ} 本周已执行，跳过")
            _record_baijing_task(logger, "weekly", "每周商店购买", "already", "本周已完成", extra={"week": week_key})
            return None
        # 1) 视觉版：俱乐部商店逐个扫商品购买
        vision = None
        try:
            from .baijing_shop import run_club_store_weekly
            shop_hwnd = _ensure_baijing_window_valid(logger, hwnd) or _resolve_baijing_window(logger)
            vision = run_club_store_weekly(logger, shop_hwnd, stop_check=stop_check)
        except Exception as e:
            logger.warning(f"[白荆回廊] 俱乐部商店视觉购买异常: {e}", exc_info=True)
            vision = None
        if vision and (vision.get("purchased") or vision.get("status") == "already"):
            try:
                db.set_task_meta(meta_key, week_key)
            except Exception as e:
                logger.warning(f"[白荆回廊] 写入周常 meta 失败: {e}")
            detail = vision.get("detail") or ""
            _record_baijing_task(
                logger,
                "weekly",
                "每周商店购买",
                "success" if vision.get("purchased") else "already",
                detail,
                extra={"week": week_key, "engine": "vision", **vision},
            )
            icon = "✅" if vision.get("purchased") else "☑️"
            return f"  🛒 白荆回廊每周商店购买: {icon} {detail}"
        logger.warning(
            f"[白荆回廊] 视觉版俱乐部商店购买未完成（{(vision or {}).get('detail') or '无结果'}），"
            "回退旧坐标序列"
        )
        # 2) 回退：旧固定坐标序列
        ok, _ = _play_sequence_with_verify_capture(logger, BAIJING_WEEKLY_SEQ, hwnd, stop_check=stop_check)
        if ok:
            try:
                db.set_task_meta(meta_key, week_key)
            except Exception as e:
                logger.warning(f"[白荆回廊] 写入周常 meta 失败: {e}")
            _record_baijing_task(logger, "weekly", "每周商店购买", "success", "已完成", extra={"week": week_key})
            logger.info(f"[白荆回廊] 周常 {BAIJING_WEEKLY_SEQ} 执行成功，本周不再重复")
            return "  🛒 白荆回廊每周商店购买: ✅ 已完成"
        _record_baijing_task(logger, "weekly", "每周商店购买", "failed", "执行失败，下次重试", extra={"week": week_key})
        logger.warning(f"[白荆回廊] 周常 {BAIJING_WEEKLY_SEQ} 执行失败，下次重试")
        return "  🛒 白荆回廊每周商店购买: ❌ 失败（下次运行重试）"
    except Exception as e:
        logger.warning(f"[白荆回廊] 周常执行异常: {e}", exc_info=True)
        _record_baijing_task(logger, "weekly", "每周商店购买", "failed", f"异常: {e}")
        return f"  🛒 白荆回廊每周商店购买: ❌ 异常: {e}"


def _maybe_run_monthly(logger: logging.Logger, hwnd: Optional[int], stop_check=None) -> Optional[str]:
    """每月执行一次：有猫零售「有猫兑换」按清单购买（2026-10-25 新增）。

    购买清单来自 app_config.json → baijing.cat_exchange.items（当月还没定买什么，
    所以清单默认为空）。**清单为空时只报「待配置」，不进商店、不买任何东西，
    也不写月度 meta**——等清单补上后本月还能补跑。
    """
    try:
        from ..storage.workflow_db import WorkflowDB
        import time as _t
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        month_key = _t.strftime("%Y-%m")
        meta_key = "monthly_sequence:baijing_cat_exchange"
        if db.get_task_meta(meta_key) == month_key:
            logger.info("[白荆回廊] 每月有猫兑换 本月已执行，跳过")
            _record_baijing_task(
                logger, "monthly", "每月有猫兑换", "already", "本月已完成",
                extra={"month": month_key},
            )
            return "  🛒 白荆回廊每月有猫兑换: ☑️ 本月已执行"
        # 1) 读清单（空 → 诚实跳过，不碰游戏）
        try:
            from .baijing_cat_exchange import load_cat_exchange_plan
            plan = load_cat_exchange_plan(logger)
        except Exception as e:
            logger.warning(f"[白荆回廊] 读取有猫兑换清单失败: {e}", exc_info=True)
            plan = {"enabled": True, "tab": "", "items": []}
        if not plan.get("enabled"):
            _record_baijing_task(
                logger, "monthly", "每月有猫兑换", "skipped", "配置已关闭",
                extra={"month": month_key},
            )
            return "  🛒 白荆回廊每月有猫兑换: ⏸ 已关闭"
        if not plan.get("items"):
            _record_baijing_task(
                logger, "monthly", "每月有猫兑换", "skipped", "购买清单为空（待定）",
                extra={"month": month_key},
            )
            logger.info("[白荆回廊] 每月有猫兑换：购买清单为空（待定），本次不进入商店")
            return "  🛒 白荆回廊每月有猫兑换: ⏸ 购买清单待定"
        # 2) 进商店按清单买
        try:
            from .baijing_cat_exchange import run_cat_exchange_monthly
            shop_hwnd = _ensure_baijing_window_valid(logger, hwnd) or _resolve_baijing_window(logger)
            result = run_cat_exchange_monthly(logger, shop_hwnd, stop_check=stop_check)
        except Exception as e:
            logger.warning(f"[白荆回廊] 有猫兑换购买异常: {e}", exc_info=True)
            _record_baijing_task(
                logger, "monthly", "每月有猫兑换", "failed", f"异常: {e}",
                extra={"month": month_key},
            )
            return f"  🛒 白荆回廊每月有猫兑换: ❌ 异常: {e}"
        status = result.get("status")
        detail = result.get("detail") or ""
        if status in ("success", "already"):
            try:
                db.set_task_meta(meta_key, month_key)
            except Exception as e:
                logger.warning(f"[白荆回廊] 写入月度 meta 失败: {e}")
            _record_baijing_task(
                logger, "monthly", "每月有猫兑换",
                "success" if status == "success" else "already",
                detail, extra={"month": month_key, **result},
            )
            icon = "✅" if status == "success" else "☑️"
            return f"  🛒 白荆回廊每月有猫兑换: {icon} {detail}"
        _record_baijing_task(
            logger, "monthly", "每月有猫兑换", "failed", detail or "执行失败",
            extra={"month": month_key, **result},
        )
        logger.warning(f"[白荆回廊] 每月有猫兑换失败：{detail}")
        return f"  🛒 白荆回廊每月有猫兑换: ❌ {detail or '失败（下次运行重试）'}"
    except Exception as e:
        logger.warning(f"[白荆回廊] 月度任务执行异常: {e}", exc_info=True)
        _record_baijing_task(logger, "monthly", "每月有猫兑换", "failed", f"异常: {e}")
        return f"  🛒 白荆回廊每月有猫兑换: ❌ 异常: {e}"


def _close_baijing_game(logger: logging.Logger):
    """关闭白荆游戏与启动器进程树。"""
    try:
        from .foreground_flow_runner import close_flow_processes
        hwnd = _resolve_baijing_window(logger)
        pid = None
        if hwnd:
            try:
                import win32process as _wp
                _, pid = _wp.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = None
        close_flow_processes(logger, "BaiJing", pid=pid, process_names=[GAME_PROCESS, LAUNCHER_PROCESS])
        logger.info("[白荆回廊] 已请求关闭游戏进程")
    except Exception as e:
        logger.warning(f"[白荆回廊] 关闭游戏失败: {e}", exc_info=True)


def _make_stop_check_with_heartbeat(controller):
    """构造白荆一条龙的停止回调：每次轮询停止标志的同时上报任务线程心跳。

    execute_task 在任务一开始就启动 task_heartbeat_watchdog，但 beat() 原本只在
    _monitor_game 循环里调用——白荆走 run_baijing_full_flow，全程零心跳，整条
    流程超过 600 秒就被看门狗按"任务线程卡死"误杀（2026-10-25 冷启动实例：光等
    游戏本体进程就 4 分钟，09:38:41 在进入阶段被 600s 无心跳腰斩，连日常都没跑到）。
    白荆流程所有长循环本来就轮询 stop_check（见 AGENTS「长等待必须可中断」），
    借这个既有节拍捎带心跳：线程真卡死时停止轮询、心跳随之停止，看门狗保护不失效。
    """
    if controller is None:
        return None
    heartbeat = getattr(controller, "_heartbeat", None)

    def _stop_and_beat() -> bool:
        if heartbeat is not None:
            try:
                heartbeat.beat()
            except Exception:
                # 心跳上报失败不能影响停止判定（beat 只是时间戳写入，实际不会抛）
                pass
        return bool(getattr(controller, "stop_current_task", False))

    return _stop_and_beat


def run_baijing_full_flow(config: dict, logger: logging.Logger, controller=None) -> bool:
    """白荆回廊一条龙：进入游戏 → 日常(视觉版) → 验证(WGC截图) → 周常(视觉版) → 月度(有猫兑换) → 关闭。

    - 日常为视觉版流程（baijing_daily.run_baijing_daily_vision_flow）：
      主界面 → 点「任务」→ 日常任务列表 → 领取 → 关闭回主界面，不依赖固定坐标。
    - 验证仍为固定点击序列回放（验证的 mark 处做 WGC 截图并压缩落盘）；
      周常（俱乐部商店全量购买）为视觉版 baijing_shop.buy_club_store_all，
      跑不通时回退旧坐标序列。
    - 月度（2026-10-25 新增）：有猫零售「有猫兑换」按配置清单购买
      （baijing_cat_exchange.run_cat_exchange_monthly），每月一次；
      清单为空（买什么还没定）时只报「待定」，不进商店。
    - 结果写入 daily_progress（game_key=baijing），验证图路径在 extra.verify_image。
    - 无论成功失败，最终关闭游戏（除非被 stop 提前中断）。
    - 返回 False 仅代表进入阶段失败，其余步骤失败仍返回 True 让上层按脚本时长判定。
    """
    stop_check = _make_stop_check_with_heartbeat(controller)
    try:
        logger.info("=" * 50 + "\n[白荆回廊] 一条龙开始\n" + "=" * 50)
        # 1) 进入游戏（视觉方案）
        ok_entry = run_baijing_ingame_flow(config, logger, stop_check=stop_check)
        if not ok_entry:
            _record_baijing_task(logger, "enter_game", "进入游戏", "failed", "视觉方案未完成")
            logger.error("[白荆回廊] 进入游戏失败，跳过后续任务并尝试关闭")
            _record_baijing_task(logger, "daily", "日常任务", "skipped", "进入失败跳过")
            _record_baijing_task(logger, "verify", "验证", "skipped", "进入失败跳过")
            _close_baijing_game(logger)
            return False
        _record_baijing_task(logger, "enter_game", "进入游戏", "success", "已进入主界面")
        # 拿到窗口
        hwnd = _resolve_baijing_window(logger)
        if hwnd:
            _focus_game_window(logger, hwnd)
            time.sleep(1.0)
        else:
            logger.warning("[白荆回廊] 未能定位游戏窗口，日常视觉流程将自行重试定位")

        if stop_check and stop_check():
            logger.info("[白荆回廊] 收到停止信号，跳过日常/验证/周常")
            _close_baijing_game(logger)
            return False

        # 2) 日常（视觉版：主界面 → 任务 → 日常任务列表 → 领取，2026-10-18 重构；
        #    旧固定坐标序列 BAIJING_DAILY_SEQ 保留作历史归档，不再默认回放）
        try:
            from .baijing_daily import run_baijing_daily_vision_flow
            ok_daily = run_baijing_daily_vision_flow(logger, hwnd, stop_check=stop_check)
            if ok_daily:
                logger.info("[白荆回廊] 日常完成")
            else:
                if stop_check and stop_check():
                    _record_baijing_task(logger, "daily", "日常任务", "skipped", "用户停止")
                    _close_baijing_game(logger)
                    return False
                logger.warning("[白荆回廊] 日常未完成，仍继续验证")
        except Exception as e:
            _record_baijing_task(logger, "daily", "日常任务", "failed", f"异常: {e}")
            logger.error(f"[白荆回廊] 日常异常: {e}", exc_info=True)

        if stop_check and stop_check():
            _close_baijing_game(logger)
            return False

        # 3) 验证（含截图）
        verify_path = None
        try:
            ok_verify, verify_path = _play_sequence_with_verify_capture(logger, BAIJING_VERIFY_SEQ, hwnd, stop_check=stop_check)
            if ok_verify:
                extra = {"verify_image": verify_path} if verify_path else {}
                _record_baijing_task(logger, "verify", "验证", "success", "已完成", extra=extra)
                logger.info(f"[白荆回廊] 验证完成，截图: {verify_path}")
            else:
                if stop_check and stop_check():
                    _record_baijing_task(logger, "verify", "验证", "skipped", "用户停止")
                    _close_baijing_game(logger)
                    return False
                # 即使回放失败，也尝试补拍一张窗口图保证卡片有图
                if not verify_path:
                    verify_path = _capture_and_compress_verify(logger, hwnd)
                extra = {"verify_image": verify_path} if verify_path else {}
                _record_baijing_task(logger, "verify", "验证", "failed", "回放失败或截图缺失", extra=extra)
                logger.warning("[白荆回廊] 验证失败")
        except Exception as e:
            _record_baijing_task(logger, "verify", "验证", "failed", f"异常: {e}")
            logger.error(f"[白荆回廊] 验证异常: {e}", exc_info=True)

        if stop_check and stop_check():
            _close_baijing_game(logger)
            return False

        # 4) 周常（每周一次）
        try:
            weekly_line = _maybe_run_weekly(logger, hwnd, stop_check=stop_check)
            if weekly_line:
                # 额外写一条便于 task_batch_runner 汇总？这里只打日志
                logger.info(weekly_line)
        except Exception as e:
            logger.warning(f"[白荆回廊] 周常阶段异常: {e}")

        if stop_check and stop_check():
            _close_baijing_game(logger)
            return False

        # 5) 月度（有猫兑换按清单购买，每月一次；清单为空则只报「待定」）
        try:
            monthly_line = _maybe_run_monthly(logger, hwnd, stop_check=stop_check)
            if monthly_line:
                logger.info(monthly_line)
        except Exception as e:
            logger.warning(f"[白荆回廊] 月度阶段异常: {e}")

        # 6) 关闭游戏
        logger.info("[白荆回廊] 一条龙任务结束，准备关闭游戏")
        _close_baijing_game(logger)
        logger.info("=" * 50 + "\n[白荆回廊] 一条龙结束\n" + "=" * 50)
        return True
    except Exception as e:
        logger.error(f"[白荆回廊] 一条龙异常: {e}", exc_info=True)
        try:
            _close_baijing_game(logger)
        except Exception:
            pass
        return False
