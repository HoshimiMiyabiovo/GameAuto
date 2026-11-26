"""百度搭子每日签到（2026-11-26）。

兼容两代 UI：
- 旧版首页直接显示「双端签到积分+500」卡片与「立即签到」按钮；
- 1.0.97 新版首页取消卡片，入口变为左下角礼盒 → 账户菜单「可用积分」
  → 积分页签到卡片。

所有点击都有独立页面/状态校验：礼盒点完必须看到「可用积分」，积分页点完
必须看到「立即签到」或已签到文案；「立即签到」点完只认「今日已签到 / 已签到 /
签到成功 / 明日再来」。拿不到明确已签到证据一律失败，不把“点过了”当成功。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("GameAutomation")

CLAIM_BUTTON_TEXT = "立即签到"
CLAIMED_TEXTS = ("今日已签到", "已签到", "签到成功", "明日再来")
POINTS_ENTRY_TEXT = "可用积分"
SIGNIN_PAGE_TEXTS = ("积分账单", "赠送积分", "每日签到", "快来签到领积分啦")

MENU_WAIT_SEC = 10.0
POINTS_PAGE_WAIT_SEC = 20.0
VERIFY_POLL_SEC = 25.0
POLL_SEC = 1.0
GIFT_X_LOCAL = 295
GIFT_BOTTOM_INSET = 40


def _norm_text(text) -> str:
    return "".join(str(text or "").split()).replace("…", "").lower()


def find_block(results: Sequence[dict], keyword: str, *, exact: bool = False) -> Optional[dict]:
    """找 OCR 文字块；exact=True 时只接受归一化后完全相等。"""
    target = _norm_text(keyword)
    for item in results or []:
        text = _norm_text((item or {}).get("text"))
        if text and ((text == target) if exact else (target in text)):
            return item
    return None


def claim_button_block(results: Sequence[dict]) -> Optional[dict]:
    """只认独立「立即签到」按钮，避免命中宣传语「快来签到领积分啦」。"""
    return find_block(results, CLAIM_BUTTON_TEXT, exact=True)


def points_entry_block(results: Sequence[dict]) -> Optional[dict]:
    """账户菜单里的「可用积分 1,100 >」入口，允许 OCR 并块。"""
    return find_block(results, POINTS_ENTRY_TEXT)


def claimed_text(results: Sequence[dict]) -> Optional[str]:
    """返回已签到证据文案；已签到判定必须先于可领取判定。"""
    for item in results or []:
        text = str((item or {}).get("text") or "").strip()
        normalized = _norm_text(text)
        if normalized and any(_norm_text(keyword) in normalized for keyword in CLAIMED_TEXTS):
            return text
    return None


def signin_page_visible(results: Sequence[dict]) -> bool:
    """积分页专属内容，用于区分“菜单还开着”与“已经进入积分页”。"""
    return any(find_block(results, text) is not None for text in SIGNIN_PAGE_TEXTS)


def claim_click_point(block: dict) -> Optional[Tuple[int, int]]:
    center = (block or {}).get("center")
    if not center or len(center) < 2:
        return None
    return int(center[0]), int(center[1])


def gift_local_point(rect) -> Optional[Tuple[int, int]]:
    """新版首页左下角礼盒的窗口局部坐标。

    1.0.97 实机截图 1936×1048：礼盒中心约 (295,1008)。左侧栏是固定宽度，
    横坐标不随窗口宽度缩放；纵坐标锚定底边。它是导航入口而非购买坐标，点完必须
    以「可用积分」菜单出现作为硬校验。
    """
    if not rect:
        return None
    _left, _top, _width, height = rect
    if height <= 0:
        return None
    return GIFT_X_LOCAL, max(1, int(height) - GIFT_BOTTOM_INSET)


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl

    return impl(hwnd)


def _click(hwnd: int, lx: int, ly: int) -> bool:
    from .foreground_flow_runner import _click_window_local

    return bool(_click_window_local(hwnd, int(lx), int(ly)))


def _ocr(logger: logging.Logger, hwnd: int) -> List[dict]:
    from .ocr_click_tester import ocr_window_content

    return ocr_window_content(logger, hwnd=hwnd) or []


def _ensure_visible(hwnd: int) -> Tuple[bool, str]:
    from .foreground_flow_runner import _focus_window
    from .ocr_click_tester import _window_obscured_ratio

    _focus_window(hwnd)
    time.sleep(0.8)
    try:
        ratio = _window_obscured_ratio(hwnd)
    except Exception:
        ratio = None
    if ratio is not None and ratio >= 0.3:
        _focus_window(hwnd)
        time.sleep(0.8)
        try:
            ratio = _window_obscured_ratio(hwnd)
        except Exception:
            ratio = None
    if ratio is not None and ratio >= 0.3:
        return False, f"搭子窗口被其他窗口遮挡约 {ratio:.0%}"
    return True, ""


def _to_local(rect, screen_xy) -> Tuple[int, int]:
    left, top, _width, _height = rect
    return int(screen_xy[0]) - left, int(screen_xy[1]) - top


def _wait_for(logger, hwnd, judge, timeout_sec, stop_check=None):
    from .foreground_flow_runner import _heartbeat_beat

    end = time.time() + max(1.0, float(timeout_sec))
    while time.time() < end:
        _heartbeat_beat()
        if stop_check and stop_check():
            return None
        results = _ocr(logger, hwnd)
        verdict = judge(results)
        if verdict is not None:
            return verdict
        time.sleep(POLL_SEC)
    return None


def _click_claim_and_verify(logger, hwnd, rect, deadline, stop_check=None) -> Tuple[bool, str]:
    results = _ocr(logger, hwnd)
    already = claimed_text(results)
    if already:
        return True, f"页面显示「{already}」"
    button = claim_button_block(results)
    point = claim_click_point(button or {})
    if not point:
        return False, "未找到独立的「立即签到」按钮"
    lx, ly = _to_local(rect, point)
    if not _click(hwnd, lx, ly):
        return False, "「立即签到」点击被前台防护闸拦截"
    logger.info("[DuMate] 已点击「立即签到」@ (%d, %d)", point[0], point[1])

    remaining = max(1.0, deadline - time.time())

    def verify_judge(ocr_results):
        return claimed_text(ocr_results)

    evidence = _wait_for(logger, hwnd, verify_judge, min(VERIFY_POLL_SEC, remaining), stop_check)
    if evidence:
        logger.info("[DuMate] 签到核验通过: %s", evidence)
        return True, f"签到成功（页面显示「{evidence}」）"
    if stop_check and stop_check():
        return False, "用户请求停止"
    return False, "点击「立即签到」后未出现已签到/签到成功文案"


def claim_daily(
    logger: logging.Logger,
    hwnd: int,
    scales=None,
    confidences=None,
    timeout_sec: int = 120,
    stop_check=None,
) -> Tuple[bool, str]:
    """旧首页卡片或新版礼盒→可用积分→积分页签到，返回 (ok, 说明)。"""
    logger = logger or LOGGER
    del scales, confidences
    rect = _window_rect(hwnd)
    if not rect:
        return False, "未找到百度搭子窗口"
    deadline = time.time() + max(30, int(timeout_sec))

    def remaining(sec: float) -> float:
        return max(1.0, min(sec, deadline - time.time()))

    visible, reason = _ensure_visible(hwnd)
    if not visible:
        return False, reason

    results = _ocr(logger, hwnd)
    already = claimed_text(results)
    if already:
        return True, f"今日已签到（页面显示「{already}」）"

    button = claim_button_block(results)
    if button is not None:
        return _click_claim_and_verify(logger, hwnd, rect, deadline, stop_check)

    points = points_entry_block(results)
    if points is None:
        gift = gift_local_point(rect)
        if not gift:
            return False, "无法计算新版首页礼盒入口坐标"
        if not _click(hwnd, gift[0], gift[1]):
            return False, "左下角礼盒点击被前台防护闸拦截"
        logger.info("[DuMate] 已点击左下角礼盒 @ 窗口局部 (%d, %d)", gift[0], gift[1])

        def menu_judge(ocr_results):
            block = points_entry_block(ocr_results)
            return block.get("center") if block else None

        points_center = _wait_for(logger, hwnd, menu_judge, remaining(MENU_WAIT_SEC), stop_check)
        if not points_center:
            if stop_check and stop_check():
                return False, "用户请求停止"
            return False, "点击礼盒后未出现「可用积分」菜单入口"
        points = {"center": points_center, "text": POINTS_ENTRY_TEXT}
    else:
        logger.info("[DuMate] 账户菜单已打开，跳过礼盒点击")

    points_point = claim_click_point(points)
    if not points_point:
        return False, "「可用积分」入口缺少 center 坐标"
    lx, ly = _to_local(rect, points_point)
    if not _click(hwnd, lx, ly):
        return False, "「可用积分」点击被前台防护闸拦截"
    logger.info("[DuMate] 已点击「可用积分」@ (%d, %d)", points_point[0], points_point[1])

    def page_judge(ocr_results):
        already = claimed_text(ocr_results)
        if already:
            return ("claimed", already)
        button = claim_button_block(ocr_results)
        if button is not None:
            return ("claimable", None)
        if signin_page_visible(ocr_results):
            return ("page_without_button", None)
        return None

    state = _wait_for(logger, hwnd, page_judge, remaining(POINTS_PAGE_WAIT_SEC), stop_check)
    if stop_check and stop_check():
        return False, "用户请求停止"
    if not state:
        return False, "点击「可用积分」后未进入积分页或未找到签到卡片"
    if state[0] == "claimed":
        return True, f"今日已签到（页面显示「{state[1]}」）"
    if state[0] == "page_without_button":
        return False, "积分页已打开，但既无「立即签到」也无已签到文案"
    return _click_claim_and_verify(logger, hwnd, rect, deadline, stop_check)
