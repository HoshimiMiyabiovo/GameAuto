"""码道Agent Space（CodeArts Agent）每日积分领取（2026-11-16 接入薅羊毛集合）。

页面模型（用户三张截图实锤，1920 宽最大化窗口）：
  右上角标题栏图标条：限时福利 / 侧栏 / 刷新 / 设置 / 表情 / **个人图标**（从右往左
  数过 关闭/最大化/最小化 后的第 4 个，带红点=有权益待领取）→ 点它弹出**下拉面板**
  （账号/总已用积分/活动福利/码道控制台…，面板左缘 x≈1538）→ 鼠标**悬停**（不点击）
  「活动福利」行 → 左侧弹出**签到浮层**（每日签到领100积分 + [每日领取] 按钮，
  浮层右缘 x≈1537，与面板左缘**共享同一条边**）→ 从两四边形连接处把鼠标移进浮层 →
  点「每日领取」。

**鼠标轨迹铁律（用户实锤）**：签到浮层靠悬停保持，鼠标一旦从两四边形连接处以外的
路径接近就会整体消失。所以「活动福利」→ 浮层内 每日领取 必须走受控路径：
  ① 悬停「活动福利」（面板内，y≈160）；
  ② 从该点**水平左移** JUNCTION_ENTRY_DX 像素 —— 两条路径全程贴着 面板∪连接边∪浮层
     的并集走，绝不经过浮层以外的空白区；
  ③ 浮层内再移到「每日领取」并点击。
浮层与面板都是**右锚定**的固定尺寸卡片，以上全是相对像素偏移，与窗口大小/是否最大化
无关；OCR 只用来定位「活动福利」「每日领取」的文字块，不做区域猜测。

成败核验：CodeArts 无本地领取日志（对比 MINIMAX/TRAE/Qoder），OCR 是唯一判据，
fail-closed：点击后「每日领取」消失或出现已领类文字才算成功；浮层没弹出且面板
「待领取」副标题消失 → 视为今日已领（幂等重跑无害）。已领态 UI 长什么样还没有
截图，首跑失败看 flow_failures 取证图校准。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Tuple

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

AVATAR_TEMPLATE = str(TEMPLATES_DIR / "codearts-个人图标.png")

# 面板行与浮层按钮的 OCR 关键词。「每日签到领100积分」是浮层标签行，与按钮
# 分开两个文字块；若 OCR 把两者并成一块，claim_click_point 会按右对齐兜底。
BENEFIT_ROW_TEXT = "活动福利"
CLAIM_BUTTON_TEXT = "每日领取"
# 点击后浮层的"已领态"文字候选（未实锤，首跑取证校准）
CLAIMED_TEXTS = ("已领取", "今日已领", "已签到", "明日再来")
# 面板「活动福利」行的副标题：有权益时显示「您有权益待领取」，领完后消失
PENDING_SUBTITLE = "待领取"

# 从「活动福利」中心到浮层内的水平进层距离。实测（1920 截图）：活动福利文字
# 中心 x≈1578，浮层右缘 x≈1537（=面板左缘，共享边），左移 100px 落在浮层内
# 约 60px 处；偏移的是相对距离，窗口挪到哪都成立。
JUNCTION_ENTRY_DX = -100
# OCR 把「每日签到领100积分 ●」和「每日领取」并成一块时，按钮在该块内**右对齐**
# （按钮右缘≈文字块右缘），按钮宽约 72px，取右缘内缩 36px ≈ 按钮中心。
MERGED_BUTTON_INSET = 36

AVATAR_REGION_X0_FRAC = 0.75   # 头像搜索区：窗口顶部右侧条带（设置面板「账号」行
AVATAR_REGION_H_FRAC = 0.055   # 里有同款人形图标，全窗匹配 0.997 误命中，必须锁条带）
PANEL_WAIT_SEC = 8.0           # 点头像后等面板
POPUP_WAIT_SEC = 10.0          # 悬停后等签到浮层
VERIFY_POLL_SEC = 12.0         # 点击后核验轮询时长


def find_block(results: Sequence[dict], keyword: str) -> Optional[dict]:
    """在 OCR 结果里找第一个包含 keyword 的文字块（大小写不敏感）。"""
    kw = keyword.lower()
    for item in results or []:
        text = str((item or {}).get("text") or "").strip().lower()
        if text and kw in text:
            return item
    return None


def claim_click_point(block: dict) -> Optional[Tuple[int, int]]:
    """「每日领取」点击点（纯函数）：精确块取中心；与标签并块时按钮右对齐。

    坐标系坑：OCR 结果的 box 是截图局部坐标、center 是屏幕绝对坐标（差一个
    区域原点，而结果里不带原点）——所以并块兜底不能用 box 绝对值，改算
    「按钮点击位相对块中心的偏移」再叠加到 center 上，与原点无关：
        点击位 x = 块右缘 - MERGED_BUTTON_INSET（按钮右对齐，右缘≈块右缘）
        相对偏移 = (x2 - inset) - (x1 + x2) / 2
    对恰好只含「每日领取」的块，inset≈半宽，偏移≈0，公式退化为取中心。
    """
    center = (block or {}).get("center")
    if not center or len(center) < 2:
        return None
    cx, cy = int(center[0]), int(center[1])
    text = str((block or {}).get("text") or "").strip()
    box = (block or {}).get("box")
    # 并块判定：块文本明显比按钮四个字长（标签「每日签到领100积分」被并进来）
    if len(text) > len(CLAIM_BUTTON_TEXT) + 2 and box and len(box) >= 4:
        x1, x2 = int(min(box[0], box[2])), int(max(box[0], box[2]))
        width = x2 - x1
        if width > 3 * MERGED_BUTTON_INSET:
            cx = int(cx + (x2 - MERGED_BUTTON_INSET) - (x1 + x2) / 2)
    return cx, cy


def popup_state(results: Sequence[dict]) -> str:
    """浮层状态判定（纯函数）：claimable / claimed / absent。

    - 有「每日领取」→ claimable（可点）；
    - 无「每日领取」但出现已领类文字 → claimed；
    - 两者都无 → absent（浮层不在/未弹出，由调用方结合面板副标题裁决）。
    """
    if find_block(results, CLAIM_BUTTON_TEXT):
        return "claimable"
    texts = [str((r or {}).get("text") or "") for r in results or []]
    if any(any(k in t for k in CLAIMED_TEXTS) for t in texts):
        return "claimed"
    return "absent"


def pending_subtitle_visible(results: Sequence[dict]) -> bool:
    """面板「活动福利」行的「待领取」副标题是否还在（有权益没领的标志）。"""
    return find_block(results, PENDING_SUBTITLE) is not None


# ── 真机交互层（pytest 全部打桩，绝不真动鼠标）────────────────────────


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl
    return impl(hwnd)


def _hover(hwnd: int, lx: int, ly: int, duration: float = 0.25) -> None:
    from .foreground_flow_runner import _hover_window_local
    _hover_window_local(hwnd, int(lx), int(ly), duration=duration)


def _click(hwnd: int, lx: int, ly: int) -> None:
    from .foreground_flow_runner import _click_window_local
    _click_window_local(hwnd, int(lx), int(ly))


def _ocr(logger: logging.Logger, hwnd: int) -> List[dict]:
    from .ocr_click_tester import ocr_window_content
    return ocr_window_content(logger, hwnd=hwnd) or []


def _ensure_visible(hwnd: int) -> Tuple[bool, str]:
    """前台化 + 遮挡检查（≥30% 拒绝执行，Qoder 2026-10-24 假阳性同款防线）。"""
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
        return False, f"窗口被其他窗口遮挡约 {ratio:.0%}"
    return True, ""


def _locate_avatar(logger: logging.Logger, hwnd: int, scales, confidences) -> Optional[Tuple[int, int]]:
    """顶部右侧条带内找个人图标（屏幕绝对坐标）。找不到返回 None。"""
    import os

    from .foreground_flow_runner import _locate_template_on_screen

    if not os.path.isfile(AVATAR_TEMPLATE):
        LOGGER.error("[CodeArts] 个人图标模板不存在: %s", AVATAR_TEMPLATE)
        return None
    rect = _window_rect(hwnd)
    if not rect:
        return None
    l, t, w, h = rect
    x0 = l + int(w * AVATAR_REGION_X0_FRAC)
    region = (x0, t, max(1, l + w - x0), max(1, int(h * AVATAR_REGION_H_FRAC)))
    return _locate_template_on_screen(
        AVATAR_TEMPLATE, region, scales, confidences
    )


def _to_local(rect, screen_xy) -> Tuple[int, int]:
    l, t, _w, _h = rect
    return int(screen_xy[0]) - l, int(screen_xy[1]) - t


def _wait_for(
    logger: logging.Logger,
    hwnd: int,
    judge,
    timeout_sec: float,
    interval: float,
    stop_check=None,
):
    """轮询 OCR 直到 judge(results) 返回非 None；超时返回 None。

    judge: OCR 结果列表 -> 任意裁决值（None=继续等）。
    """
    end = time.time() + max(1.0, float(timeout_sec))
    last = None
    while time.time() < end:
        if stop_check and stop_check():
            return None
        results = _ocr(logger, hwnd)
        last = judge(results)
        if last is not None:
            return last
        time.sleep(interval)
    return None


def claim_daily(
    logger: logging.Logger,
    hwnd: int,
    scales=None,
    confidences=None,
    timeout_sec: int = 90,
    stop_check=None,
) -> Tuple[bool, str]:
    """点头像 → 悬停活动福利 → 连接处轨迹进浮层 → 点每日领取 → OCR 核验。

    契约：成功 (True, 说明)；失败 (False, 原因)。重试幂等——面板已开时不再
    点头像（再点一次会把面板收起，重试反而必败）。所有等待尊重 stop_check，
    且共享 timeout_sec 总预算（分相等待时长按剩余预算封顶，防 OCR 变慢时
    三段等待叠加超时穿透上层步骤超时）。
    """
    logger = logger or LOGGER
    scales = scales or [1.0, 1.25, 0.8]
    confidences = confidences or [0.92, 0.9]
    rect = _window_rect(hwnd)
    if not rect:
        return False, "未找到 CodeArts Agent 窗口"
    deadline = time.time() + max(30, int(timeout_sec))

    def budgeted(sec: float) -> float:
        """分相等待按剩余总预算封顶（最少 1s，让 judge 至少跑一轮再判超时）。"""
        return max(1.0, min(sec, deadline - time.time()))

    def stopped() -> bool:
        return bool(stop_check and stop_check())

    visible, reason = _ensure_visible(hwnd)
    if not visible:
        return False, reason

    # 面板可能已开（上一轮重试/用户手动）：先 OCR 一次，别急着点头像
    # （面板开着再点头像会把它收起，重试反而必败）。
    results = _ocr(logger, hwnd)
    benefit = find_block(results, BENEFIT_ROW_TEXT)
    avatar = None
    if benefit is None:
        avatar = _locate_avatar(logger, hwnd, scales, confidences)
        if not avatar:
            return False, "右上角未找到个人图标（模板不命中或窗口被遮挡）"
        ax_l, ay_l = _to_local(rect, avatar)
        _click(hwnd, ax_l, ay_l)
        logger.info("[CodeArts] 已点击右上角个人图标 @ (%d, %d)", avatar[0], avatar[1])

        def panel_judge(ocr_results):
            blk = find_block(ocr_results, BENEFIT_ROW_TEXT)
            return (blk.get("center"), blk.get("text")) if blk else None

        found = _wait_for(logger, hwnd, panel_judge, budgeted(PANEL_WAIT_SEC), 1.0, stop_check)
        if not found:
            return False, "点击个人图标后面板未出现（未找到「活动福利」行）"
        center, _text = found
        benefit = {"center": center, "text": _text}
    else:
        logger.info("[CodeArts] 面板已开（重试/手动场景），跳过点头像")
        avatar = _locate_avatar(logger, hwnd, scales, confidences)

    # ── 悬停路径 ①：头像锚点 → 正下方竖直进面板 → 面板内移到「活动福利」行 ──
    # 面板是头像的下拉层：从面板外直接斜穿可能路过面板上沿以外的标题条把面板
    # 晃没；先回到锚点（悬停锚点不关面板），再正下方进面板、面板内部移动。
    # 重试/手动场景头像定位不到时，退化为直接移到目标行（可能触发一次面板
    # 收起，由外层 retry_step 下一轮重新点头像恢复）。
    bx_l, by_l = _to_local(rect, benefit["center"])
    if avatar:
        ax_l, ay_l = _to_local(rect, avatar)
        _hover(hwnd, ax_l, ay_l, duration=0.2)
        _hover(hwnd, ax_l, ay_l + 60, duration=0.15)
    _hover(hwnd, bx_l, by_l, duration=0.3)
    logger.info("[CodeArts] 已悬停「%s」 @ (%d, %d)（不点击）", BENEFIT_ROW_TEXT, bx_l, by_l)

    # ── 等签到浮层 ──
    # 浮层标签与按钮可能被 OCR 并成一块：judge 命中块后用 claim_click_point
    # 复算点击点（并块时按钮右对齐），返回 None 表示浮层还没出现。
    def popup_judge_safe(ocr_results):
        blk = find_block(ocr_results, CLAIM_BUTTON_TEXT)
        if not blk:
            return None
        pt = claim_click_point(blk)
        return (pt, blk) if pt else None

    if stopped():
        return False, "用户请求停止"
    found = _wait_for(logger, hwnd, popup_judge_safe, budgeted(POPUP_WAIT_SEC), 1.0, stop_check)
    if not found:
        # 浮层没弹：看面板「待领取」副标题区分「今日已领」与「悬停失效」。
        results = _ocr(logger, hwnd)
        if pending_subtitle_visible(results):
            return False, "悬停「活动福利」后签到浮层未弹出（面板仍显示待领取）"
        return True, "未弹出签到浮层且面板无待领取权益（视为今日已领，幂等通过）"

    point, blk = found
    # ── 悬停路径 ②：从「活动福利」水平左移穿过两四边形连接边进浮层 ──
    _hover(hwnd, bx_l + JUNCTION_ENTRY_DX, by_l, duration=0.25)
    time.sleep(0.4)
    # ── 悬停路径 ③：浮层内移到「每日领取」再点击（全程不出浮层） ──
    results = _ocr(logger, hwnd)
    blk2 = find_block(results, CLAIM_BUTTON_TEXT) or blk
    pt2 = claim_click_point(blk2) or point
    cx_l, cy_l = _to_local(rect, pt2)
    _hover(hwnd, cx_l, cy_l, duration=0.25)
    time.sleep(0.2)
    _click(hwnd, cx_l, cy_l)
    logger.info("[CodeArts] 已点击「%s」 @ (%d, %d) [块: %s]",
                CLAIM_BUTTON_TEXT, pt2[0], pt2[1], str(blk2.get("text"))[:40])

    # ── 核验（fail-closed）：已领文字 或 按钮消失 才算成功 ──
    def verify_judge(ocr_results):
        if not ocr_results:
            # OCR 全空（窗口被挡/引擎故障）≠ 浮层已收起：继续轮询，否则会把
            # 一次 OCR 故障误判成「按钮已消失」假成功。
            return None
        state = popup_state(ocr_results)
        if state == "claimed":
            return "claimed"
        if state == "absent":
            return "closed"
        return None

    verdict = _wait_for(logger, hwnd, verify_judge, budgeted(VERIFY_POLL_SEC), 1.5, stop_check)
    if verdict == "claimed":
        msg = "浮层出现已领态文字"
    elif verdict == "closed":
        msg = "点击后「每日领取」已消失（浮层收起）"
    else:
        return False, "点击每日领取后浮层状态未变化（按钮仍在、无已领文字）"
    logger.info("[CodeArts] 领取核验通过: %s", msg)
    return True, msg
