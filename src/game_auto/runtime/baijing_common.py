# -*- coding: utf-8 -*-
"""白荆回廊·共享基础设施（2026-11-14 自 baijing_daily.py 拆出）。

职责：所有白荆流程模块共用的窗口/OCR/点击/滚动/取证原语——
窗口定位与守护（_ensure_window/_abs_region）、OCR 与模板定位
（_ocr_best_match/_template_center）、列表滚动（_wheel_*/_scroll_*）、
失败取证（_record_step_failure → flow_diagnostics）、遮挡弹窗关闭
（_dismiss_overlay + 宿主面板关闭区排除）、通用比例点击（_click_ratio）。

数据流：本模块是白荆模块族的**最底层**，只依赖 baijing_flow 的窗口原语
（_resolve_baijing_window/_window_region/_focus_game_window/BAIJING_GAME_KEY）
与 ocr_click_tester / foreground_flow_runner；**禁止**反向依赖 scene/tasklist/
echo/handlers/daily（会成环）。上层模块一律 `from .baijing_common import ...`。

不变量与坑：
- OCR 一律走窗口grab（grab_screen/ocr_window_content），禁全屏回退
  （OCR 防护在 ocr_click_tester 内 fail-closed）。
- _dismiss_overlay 的「关闭」命中宿主面板右上角 X 时必须跳过
  （_in_host_panel_close_zone），2026-10-25 实锤误关宿主面板烧光领取轮次。
- _record_step_failure 内部 lazy import flow_diagnostics，失败只 debug 不抛。
"""
import ctypes
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import pyautogui
import win32api
import win32con
import win32gui
from PIL import Image

from ..settings.paths import BAIJING_VERIFY_DIR, TEMPLATES_DIR, WORKFLOW_DB_PATH
from .baijing_flow import (
    BAIJING_GAME_KEY,
    _baijing_today_key,
    _focus_game_window,
    _resolve_baijing_window,
    _window_region,
)
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, grab_screen, ocr_window_content


# ── 模板匹配参数与任务面板特征（tasklist/scene/handlers 三方共用，2026-11-14 拆分上收） ──
# 「任务」按钮模板（1920x1080 参考分辨率裁剪，红色角标已抹除）
TEMPLATE_REFERENCE_WIDTH = 1920
TEMPLATE_MATCH_THRESHOLD = 0.82

# 任务列表核心特征：只用于"确认已经从子页面回到列表"。不含「一键领取/领取奖励」
# 这类在弹窗或奖励界面也可能出现的词，避免把别的界面当成列表（2026-10-24）。
TASK_LIST_CORE_MARKERS = ("累计活跃度", "主线指引")

# 任务列表行区域（行按钮/滚动签名/研发室弹窗三方共用）
TASK_ROW_REGION = (0.42, 0.12, 1.00, 0.80)

# 场景/列表 grab 的 OCR 置信度下限（_scene_results 默认值，scene 登记表同款）
SCENE_OCR_MIN_CONF = 0.5


# ── 滚轮发送参数（2026-10-19 修复滚动失效） ──
# 根因：pyautogui.scroll(-3) 在 Windows 上等价于
# mouse_event(MOUSEEVENTF_WHEEL, dwData=-3)，而一格滚轮是 WHEEL_DELTA=120——
# 不足 1/40 格，Unreal/Slate 列表积累不到阈值直接不动（2026-10-18/19 连续两天
# 6 屏 OCR 完全相同的实锤）。ok-ef/ok-ww 的可靠做法（参考实现）：
#   1. 发送前激活游戏窗口（try_activate / active_and_send_mouse_delta）
#   2. 真实光标 SetCursorPos 落位并驻留（run_at_window_pos 驻留 0.5s）
#   3. 逐格发 ±WHEEL_DELTA（mouse_event / mouse.wheel）
#   4. PostMessage WM_MOUSEWHEEL 到窗口兜底（wParam=MAKELONG(0, 120*n),
#      lParam=MAKELONG(客户区x, 客户区y)）
#   5. 调用方用 OCR 验证列表内容真的变了，没变就升级/止损，不烧无效屏
WHEEL_NOTCH = win32con.WHEEL_DELTA           # 120，一格滚轮


SCROLL_CURSOR_DWELL = 0.3                    # 光标落位后驻留，等游戏注册 hover


SCROLL_NOTCH_PAUSE = 0.06                    # 逐格滚轮之间的间隔


SCROLL_POST_DELAY = 1.2                      # 滚轮发完等列表滚动动画稳定


# 打开列表的最大尝试次数（点击「任务」→等列表出现）
MAX_OPEN_ATTEMPTS = 3


# 关闭列表回到主界面的最大尝试次数
MAX_CLOSE_ATTEMPTS = 3



# 任务列表快照（压缩 JPEG，按日期覆盖，便于日常核对流程是否导航正确）
DAILY_LIST_SNAPSHOT_MAX_WIDTH = 960


DAILY_LIST_SNAPSHOT_QUALITY = 72




def _record_step_failure(logger: logging.Logger, hwnd, step: str, detail: str) -> None:
    """白荆各步骤失败时补一条结构化失败记录（截图 + 当帧文字），供事后聚合分析。"""
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="BaiJingDaily",
            step=step,
            error=detail,
            hwnd=hwnd,
            game_key=BAIJING_GAME_KEY,
            save_image=bool(hwnd),
            extra={"kind": "baijing_daily_step"},
        )
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 失败记录写入失败（已忽略）: {e}")




def _baijing_task_status_today(task_key: str) -> Optional[str]:
    """读今日 daily_progress 里白荆某任务的最终状态（读不到/异常返回 None）。

    用于"一天只能做一次"的动作（如每日免费礼包）在 force_rerun/重复运行时
    避免把"已领过导致入口消失"误报成失败（2026-10-25 17:49 实锤）。
    """
    try:
        from ..services.daily_progress_tracker import get_tracker

        snap = get_tracker(str(WORKFLOW_DB_PATH)).load_today()
        rec = snap.games.get(BAIJING_GAME_KEY)
        if not rec:
            return None
        for task in rec.tasks:
            if task.task_key == task_key:
                return task.status
    except Exception:
        return None
    return None




def _stopped(stop_check) -> bool:
    try:
        return bool(stop_check and stop_check())
    except Exception:
        return False




def _abs_region(hwnd, frac: Tuple[float, float, float, float]) -> Optional[Tuple[int, int, int, int]]:
    """窗口相对比例区域 → 屏幕绝对区域 (left, top, width, height)。窗口失效返回 None。"""
    region = _window_region(hwnd)
    if not region:
        return None
    left, top, width, height = region
    fx0, fy0, fx1, fy1 = frac
    ax0, ay0 = int(left + fx0 * width), int(top + fy0 * height)
    ax1, ay1 = int(left + fx1 * width), int(top + fy1 * height)
    return (ax0, ay0, max(0, ax1 - ax0), max(0, ay1 - ay0))




def _window_valid(hwnd) -> bool:
    try:
        import win32gui
        return bool(hwnd) and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False




def _ensure_window(logger: logging.Logger, hwnd):
    """校验窗口句柄，失效则重找；返回有效句柄或 None。"""
    if _window_valid(hwnd):
        return hwnd
    fresh = _resolve_baijing_window(logger)
    if fresh:
        logger.info(f"[白荆回廊·日常] 窗口句柄已刷新 {hwnd} -> {fresh}")
    return fresh




def _ocr_best_match(
    logger: logging.Logger,
    texts,
    hwnd,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    min_conf: float = 0.5,
    exclude=(),
) -> Optional[Dict]:
    """全窗 OCR 一次，按候选文字 + 相对区域 + 黑名单过滤，返回置信度最高的命中（dict）。

    匹配规则：先精确匹配（OCR 文本 == 候选文字），无精确命中再子串匹配
    （候选文字 ∈ OCR 文本，或 OCR 文本 ∈ 候选文字且至少 2 字符）。
    精确优先很重要：实测「确认」按钮会被对话框正文「将离开休息室，是否确认退出?」
    以更高置信度截胡，点到正文中间等于没点按钮。
    exclude 中的字样出现在 OCR 文本里则跳过该命中。

    注意：不做「先裁剪区域再 OCR」——实测裁剪会改变检测模型的结果
    （右侧区域裁剪后「一任务」直接丢失、中部「阶段任务」只剩「4/200」），
    全窗识别 + 对结果按区域过滤才稳（与 ocr_find_any_text 单次多候选同策略）。
    """
    results = ocr_window_content(logger, hwnd=hwnd)
    if not results:
        return None
    abs_region = _abs_region(hwnd, frac_region) if frac_region else None
    targets = [_normalize_text(t) for t in texts if _normalize_text(t)]
    best_exact = None
    best_exact_conf = -1.0
    best_sub = None
    best_sub_conf = -1.0
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        if conf < min_conf:
            continue
        center = r.get("center")
        if abs_region and center:
            ax0, ay0, aw, ah = abs_region
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm:
            continue
        if any(x and _normalize_text(x) in tnorm for x in exclude):
            continue
        for target in targets:
            if tnorm == target:
                if conf > best_exact_conf:
                    best_exact, best_exact_conf = r, conf
                break
            if target in tnorm or (tnorm in target and len(tnorm) >= 2):
                if conf > best_sub_conf:
                    best_sub, best_sub_conf = r, conf
                break
    best = best_exact if best_exact is not None else best_sub
    if best is not None:
        logger.info(
            f"[白荆回廊·日常] OCR 命中 {best['text']!r} "
            f"({float(best.get('confidence') or 0):.2f}) @ {best['center']}"
        )
    return best




def _template_center(
    logger: logging.Logger,
    hwnd,
    template_path: str,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    threshold: float = TEMPLATE_MATCH_THRESHOLD,
) -> Optional[Tuple[int, int]]:
    """在窗口（可限定相对区域）内做多尺度模板匹配，返回屏幕绝对中心坐标。"""
    if not template_path or not os.path.exists(template_path):
        logger.warning(f"[白荆回廊·日常] 模板文件不存在: {template_path}")
        return None
    region = _window_region(hwnd)
    if not region:
        return None
    try:
        shot = grab_screen(region=region)
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 模板匹配截图失败: {e}")
        return None
    scr = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2GRAY)
    if frac_region:
        fx0, fy0, fx1, fy1 = frac_region
        h, w = scr.shape[:2]
        x0, y0 = int(fx0 * w), int(fy0 * h)
        x1, y1 = int(fx1 * w), int(fy1 * h)
        sub = scr[y0:y1, x0:x1]
    else:
        sub, x0, y0 = scr, 0, 0
    if sub.size == 0:
        return None
    try:
        tpl = cv2.cvtColor(
            np.asarray(Image.open(template_path).convert("RGB")), cv2.COLOR_RGB2GRAY
        )
    except Exception as e:
        logger.warning(f"[白荆回廊·日常] 模板加载失败: {template_path} ({e})")
        return None
    # 模板按 1920 宽参考分辨率裁剪；按当前窗口宽度换算缩放，±8% 容错
    base_scale = region[2] / float(TEMPLATE_REFERENCE_WIDTH)
    for scale in (base_scale, base_scale * 1.08, base_scale * 0.92):
        t = tpl
        if abs(scale - 1.0) > 1e-3:
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            t = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=interp)
        if t.shape[0] > sub.shape[0] or t.shape[1] > sub.shape[1]:
            continue
        result = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= threshold:
            th, tw = t.shape[:2]
            cx = region[0] + x0 + int(max_loc[0]) + tw // 2
            cy = region[1] + y0 + int(max_loc[1]) + th // 2
            logger.info(
                f"[白荆回廊·日常] 模板命中 {os.path.basename(template_path)} "
                f"scale={scale:.3f} conf={max_val:.3f} @ ({cx},{cy})"
            )
            return (cx, cy)
    return None




def _capture_daily_list_snapshot(
    logger: logging.Logger, hwnd, prefix: str = "daily_list"
) -> Optional[str]:
    """任务列表/商店页打开后抓一张压缩快照落盘（按日期覆盖），便于核对导航是否正确。"""
    try:
        region = _window_region(hwnd)
        img = grab_screen(region=region)
        w, h = img.size
        if w > DAILY_LIST_SNAPSHOT_MAX_WIDTH:
            img = img.resize(
                (DAILY_LIST_SNAPSHOT_MAX_WIDTH, max(1, int(h * DAILY_LIST_SNAPSHOT_MAX_WIDTH / w))),
                Image.LANCZOS,
            )
        if img.mode != "RGB":
            img = img.convert("RGB")
        BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
        out_path = BAIJING_VERIFY_DIR / f"{prefix}_{_baijing_today_key()}.jpg"
        img.save(str(out_path), "JPEG", quality=DAILY_LIST_SNAPSHOT_QUALITY, optimize=True)
        logger.info(f"[白荆回廊·日常] 快照已保存: {out_path.name}")
        return str(out_path)
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 任务列表快照保存失败: {e}")
        return None




def _cursor_pos() -> Tuple[int, int]:
    """当前鼠标屏幕坐标。"""
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))




def _set_cursor_pos(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))




def _wheel_real(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """真实光标落位后逐格发滚轮（mouse_event ±WHEEL_DELTA）。

    notches 为正向上、负向下；按格数循环发送，每格间隔 SCROLL_NOTCH_PAUSE。
    发送前聚焦游戏窗口，光标落位后驻留 SCROLL_CURSOR_DWELL 让游戏注册 hover，
    结束后恢复原光标位置（不把鼠标留在游戏画面中央）。
    """
    if notches == 0:
        return True
    try:
        _focus_game_window(logger, hwnd)
        original = _cursor_pos()
        try:
            _set_cursor_pos(x, y)
            time.sleep(SCROLL_CURSOR_DWELL)
            sign = 1 if notches > 0 else -1
            for _ in range(abs(int(notches))):
                ctypes.windll.user32.mouse_event(
                    win32con.MOUSEEVENTF_WHEEL, 0, 0, sign * WHEEL_NOTCH, 0
                )
                time.sleep(SCROLL_NOTCH_PAUSE)
        finally:
            _set_cursor_pos(*original)
        return True
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] 真实滚轮发送失败 @ ({x},{y}) {notches} 格: {exc}")
        return False




def _wheel_post(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """PostMessage WM_MOUSEWHEEL 兜底（ok-ef/ok-ww post_message 后端同款）。

    lParam 用窗口客户区坐标；ok-ef 实测 PostMessage 鼠标消息在窗口未真实
    激活时可能被游戏丢弃，因此发送前同样先聚焦。
    """
    if not hwnd or notches == 0:
        return False
    try:
        _focus_game_window(logger, hwnd)
        client_x, client_y = win32gui.ScreenToClient(hwnd, (int(x), int(y)))
        wparam = win32api.MAKELONG(0, int(notches) * WHEEL_NOTCH)
        lparam = win32api.MAKELONG(client_x, client_y)
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        logger.debug(
            f"[白荆回廊·日常] PostMessage 滚轮 hwnd={hwnd} "
            f"客户区坐标=({client_x},{client_y}) 格数={notches}"
        )
        return True
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] PostMessage 滚轮失败: {exc}")
        return False




def _row_region_signature(logger: logging.Logger, hwnd) -> str:
    """任务行区域 OCR 文本签名，用于判断滚动是否真的改变了列表内容。"""
    try:
        results = ocr_window_content(logger, hwnd=hwnd)
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            ax0, ay0, aw, ah = region
            results = [
                r for r in results
                if r.get("center")
                and ax0 <= r["center"][0] <= ax0 + aw
                and ay0 <= r["center"][1] <= ay0 + ah
            ]
        texts = sorted(
            _normalize_text(str(r.get("text") or ""))
            for r in results
            if _normalize_text(str(r.get("text") or ""))
        )
        return "|".join(texts)
    except Exception:
        return ""




def _scroll_list_at(logger: logging.Logger, hwnd, x: int, y: int, notches: int) -> bool:
    """在 (x, y) 滚动任务列表，真实滚轮优先、PostMessage 兜底。

    返回 True = 至少一种方式发送成功（不保证列表真的动了，调用方需要
    用 OCR 验证；见 _scroll_list_with_verify）。
    """
    if _wheel_real(logger, hwnd, x, y, notches):
        return True
    logger.info("[白荆回廊·日常] 真实滚轮发送失败，改用 PostMessage 兜底")
    return _wheel_post(logger, hwnd, x, y, notches)




def _scroll_list_with_verify(logger: logging.Logger, hwnd, notches: int) -> bool:
    """滚动任务列表并用 OCR 验证内容真的变了。

    返回 True = 列表内容已变化（滚动生效）；
    返回 False = 真实滚轮 + PostMessage 都发过，内容仍无变化
    （已滚到列表底部，或滚动未生效——调用方按需止损）。
    """
    region = _abs_region(hwnd, TASK_ROW_REGION)
    if not region:
        return False
    sx, sy = region[0] + region[2] // 2, region[1] + region[3] // 2
    before = _row_region_signature(logger, hwnd)
    if _wheel_real(logger, hwnd, sx, sy, notches):
        time.sleep(SCROLL_POST_DELAY)
        if _row_region_signature(logger, hwnd) != before:
            return True
        logger.info("[白荆回廊·日常] 真实滚轮后列表内容无变化，尝试 PostMessage 兜底")
        if _wheel_post(logger, hwnd, sx, sy, notches):
            time.sleep(SCROLL_POST_DELAY)
            if _row_region_signature(logger, hwnd) != before:
                logger.info("[白荆回廊·日常] PostMessage 滚轮生效")
                return True
    return False




def _wait_page_markers(
    logger: logging.Logger,
    hwnd,
    markers,
    timeout: float = 6.0,
    stop_check=None,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
) -> bool:
    """轮询等待页面特征文字出现（任一命中）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        if _ocr_best_match(logger, markers, hwnd, min_conf=0.55, frac_region=frac_region):
            return True
        time.sleep(1.0)
    return False




def _scene_results(logger: logging.Logger, hwnd) -> List[Dict]:
    """一轮恢复只截一次屏：所有场景判定共用这份 OCR 结果。

    旧实现里 `_in_rest_room` / `_in_focus_mode` / `_ocr_best_match` 每个判定都
    各自截屏 + OCR，恢复一轮要跑 4-6 次识别（十几秒）。
    """
    try:
        return ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:
        logger.debug(f"[白荆回廊·日常] 场景识别 OCR 失败: {exc}")
        return []




def _pick_result(
    logger: logging.Logger,
    hwnd,
    results: List[Dict],
    markers,
    frac_region: Optional[Tuple[float, float, float, float]] = None,
    min_conf: float = SCENE_OCR_MIN_CONF,
    exclude=(),
    exact_only: bool = False,
) -> Optional[Dict]:
    """在已取到的 OCR 结果里挑一条命中（规则同 `_ocr_best_match`，但不再截屏）。

    精确匹配优先于子串匹配：实测「确认」按钮会被对话框正文以更高置信度截胡。
    区域过滤用屏幕绝对坐标（`ocr_window_content` 返回的 center 已加过窗口原点），
    所以 frac_region 要先经 `_abs_region` 换算。
    """
    if not results:
        return None
    abs_region = _abs_region(hwnd, frac_region) if frac_region else None
    targets = [_normalize_text(t) for t in markers if _normalize_text(t)]
    best_exact = None
    best_exact_conf = -1.0
    best_sub = None
    best_sub_conf = -1.0
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        if conf < min_conf:
            continue
        center = r.get("center")
        if abs_region and center:
            ax0, ay0, aw, ah = abs_region
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm:
            continue
        if any(x and _normalize_text(x) in tnorm for x in exclude):
            continue
        for target in targets:
            if tnorm == target:
                if conf > best_exact_conf:
                    best_exact, best_exact_conf = r, conf
                break
            # exact_only：只认「候选文字 ⊆ OCR 文本」，禁止反向包含。
            # 用于「计划等级提升」这类长特征——普通子串匹配会让旧页面的
            # 「等级提升」短文字误命中，进而点错屏幕。
            if target in tnorm or (not exact_only and tnorm in target and len(tnorm) >= 2):
                if conf > best_sub_conf:
                    best_sub, best_sub_conf = r, conf
                break
    return best_exact if best_exact is not None else best_sub


def _same_click_target(a, b, tol: int = 25) -> bool:
    """两个点击坐标是否指同一目标（容差 tol 像素）——领取循环防重复点击用。"""
    if not a or not b:
        return False
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _ratio_point(logger: logging.Logger, hwnd, ratio: Tuple[float, float]) -> Optional[Tuple[int, int]]:
    """窗口比例 → 屏幕绝对坐标（只换算不点击；窗口取不到返回 None）。"""
    region = _window_region(hwnd)
    if not region:
        return None
    return (
        int(region[0] + ratio[0] * region[2]),
        int(region[1] + ratio[1] * region[3]),
    )




def _pixel_is_blue(logger: logging.Logger, hwnd, rx: float, ry: float) -> bool:
    """窗口比例坐标附近取色判蓝（突破确认按钮可点=蓝色底；置灰/抓屏异常=不可点）。

    2026-11-08 取证帧确认按钮为高饱和蓝底白字；材料不足时应为置灰（未取证，
    按保守方向处理：判不出可点就当不能突破，取消换一张——误判方向安全）。
    """
    region = _window_region(hwnd)
    if not region:
        return False
    px = int(region[0] + rx * region[2])
    py = int(region[1] + ry * region[3])
    try:
        img = grab_screen(region=(px - 6, py - 6, 13, 13)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 抓屏失败按不可点处理
        logger.debug(f"[白荆回廊·日常] 突破按钮取色失败: {exc}")
        return False
    blue = sum(1 for r, g, b in img.getdata() if b > 140 and b - r > 40)
    return blue >= 3




def _click_ratio(logger: logging.Logger, hwnd, ratio: Tuple[float, float], label: str = ""):
    """按窗口相对比例点击。"""
    region = _window_region(hwnd)
    if not region:
        return None
    cx = int(region[0] + ratio[0] * region[2])
    cy = int(region[1] + ratio[1] * region[3])
    _pyautogui_click_guarded(cx, cy)
    logger.info(f"[白荆回廊·日常] 点击{label or '位置'} @ ({cx},{cy}) [{ratio[0]:.2f},{ratio[1]:.2f}]")
    return (cx, cy)




def _dismiss_overlay(logger: logging.Logger, hwnd) -> bool:
    """关闭可能遮挡任务列表的弹窗（「点击屏幕继续」/「关闭」模板）。

    「关闭」模板有一个致命撞车（2026-10-25 实锤）：任务列表面板/俱乐部页/
    零售页这些**宿主面板自己的 X** 也在窗口右上角（(1847,159)@1919x1079，
    conf 0.968 稳定命中），纹样与弹窗 X 同款。没有弹窗时 dismiss 一样会命中
    它、一点就把宿主面板整个关掉——领取循环因此每轮"误关面板→重开"空转
    （12 轮烧完），重扫前奏也因此在"列表已确认在屏"的 1 秒后把面板点没了，
    末轮重扫 6 屏 0 行。弹窗的 X 都在弹窗框内（窗口中央区），永远不会跑到
    窗口极右上角，所以这里把右上角宿主关闭区排除掉，命中即跳过不点。
    """
    for name, tpl in (
        ("点击屏幕继续", str(TEMPLATES_DIR / "白荆回廊-点击屏幕继续.png")),
        ("关闭", str(TEMPLATES_DIR / "白荆回廊-关闭.png")),
    ):
        center = _template_center(logger, hwnd, tpl, threshold=0.85)
        if center:
            if name == "关闭" and _in_host_panel_close_zone(hwnd, center):
                logger.info(
                    f"[白荆回廊·日常] 「关闭」命中宿主面板右上角 X @ {center}，跳过"
                    f"（那是列表面板自己的关闭钮，点了会把面板关掉）"
                )
                continue
            _pyautogui_click_guarded(center[0], center[1])
            logger.info(f"[白荆回廊·日常] 关闭遮挡弹窗（{name}）@ {center}")
            time.sleep(0.8)
            return True
    return False




def _in_host_panel_close_zone(hwnd, center) -> bool:
    """命中点是否落在宿主面板自己的右上角关闭区（窗口相对 fx≥0.88, fy≤0.25）。

    标定：(1847,159)@1919x1079 → fx=0.962/fy=0.147。中央弹窗的 X 最右也就到
    fx≈0.81（1200px 宽弹窗贴右边），与 0.88 之间留了安全带；俱乐部入口图标
    (351,51) fx=0.18 不受影响。
    """
    region = _window_region(hwnd)
    if not region:
        return False
    left, top, width, height = region
    cx, cy = center[0], center[1]
    fx = (cx - left) / width if width else 0.0
    fy = (cy - top) / height if height else 0.0
    return fx >= 0.88 and fy <= 0.25


