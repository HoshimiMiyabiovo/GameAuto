"""AStudio 每日福利领取（2026-11-17 接入薅羊毛集合）。

页面模型（用户截图实锤，1358×843 窗口）：AStudio（AI 工作台，Electron）启动后
主界面 = 左侧栏（新建任务/搜索/插件/任务）+ 中央大 logo「AStudio 工作再多变
工具不用变」+ 输入框占位「把任务交给我」；**左下角**悬浮福利卡片：
  「限时每日福利」标题 + 「今日可领100积分」副标题 + 黑底白字 [领取] 按钮
  （按钮在卡片内右对齐，宽约 50px）+ 右上 × 关闭。
卡片钉在窗口左下角（与窗口大小/是否最大化无关，只随左下角走），所以区域
判定全部用**窗口相对比例**（x ≤ 0.40w、y ≥ 0.55h），OCR 只负责认字。

用户需求口径：「启动后直接领」——不做任何入口导航，主界面出来就点卡片按钮。
（对比 QODER/CODEARTS 需要点头像/悬停轨迹，这里是唯一零导航的羊毛流程。）

成败核验：AStudio 无本地领取日志可核验（对比 MINIMAX/TRAE/Qoder），OCR 是
唯一判据，fail-closed 三态（popup_state）：
  - claimable：左下区域有可点的「领取」块；
  - claimed：出现已领类文字（已领/明日再来/已签到——「已领取/今日已领」都含
    「已领」子串，一个词覆盖）；
  - absent：卡片不在。**已领态卡片长什么样没有截图**，候选词首跑取证校准。

幂等口径（重跑无害）：卡片等 POPUP_WAIT_SEC 仍不出现 → 视为今日已领或今日
无福利，按成功返回（对比 CODEARTS 用面板「待领取」副标题佐证，这里没有
佐证物，是**有依据的假设**：卡片可领时随主界面稳定出现——用户截图即启动
当刻。若首跑日志频繁走这条出口却没到账，先补一张已领态卡片截图再改判定）。

风险与未实机验证项（首跑按 ocr_debug_snapshot / flow_failures 取证校准）：
  - 点击后卡片行为未知：消失 / 文字变已领 / 弹积分 toast，三态核验都能接住；
  - 「领取」按钮可能与「今日可领100积分」被 OCR 并块 → claim_click_point
    按按钮在卡片内**右对齐**做右缘内缩换算（同 CODEARTS 并块兜底思路）；
  - OCR 坐标系坑与 CODEARTS 相同：box 是截图局部坐标、center 是屏幕绝对
    坐标（差区域原点），并块换算必须用相对块中心的偏移。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("GameAutomation")

# 卡片锚点词：确认左下角那块是福利卡片（而不是别的「领取」文字）。
# 「限时每日福利」是标题全串；「每日福利」「今日可领」是 OCR 漏字/并块时的容忍候选。
CARD_ANCHOR_TEXTS = ("限时每日福利", "每日福利", "今日可领")
CLAIM_BUTTON_TEXT = "领取"
# 点击后的"已领态"文字候选（未实锤，首跑取证校准）。「已领」覆盖 已领取/
# 今日已领/已领 N 天 等变体；「可领」不匹配（今日可领100积分 是未领态副标题）。
CLAIMED_TEXTS = ("已领", "明日再来", "已签到")

# 卡片搜索区：窗口左下角（x ≤ 0.40w、y ≥ 0.55h）。实测 1358×843 卡片在
# x 0.015-0.184、y 0.83-0.94；放大到最大化窗口（1920×1080）时卡片像素尺寸
# 不变、仍钉左下角，比例区间只会更宽松，不会漏。
CARD_REGION_X_MAX_FRAC = 0.40
CARD_REGION_Y_MIN_FRAC = 0.55

# OCR 把「今日可领100积分」和「领取」并成一块时，按钮在该块内**右对齐**
# （按钮右缘≈卡片内容右缘），按钮宽约 50px，取右缘内缩 28px ≈ 按钮中心。
MERGED_BUTTON_INSET = 28

POPUP_WAIT_SEC = 30.0     # 主界面出来后等福利卡片（Electron 首帧渲染可能滞后）
VERIFY_POLL_SEC = 20.0    # 点击后核验轮询时长


def _norm_text(text) -> str:
    """OCR 子串匹配前的归一化：去全部空白（onnxocr 会把英文标签稳定拆字，
    2026-11-08 XM 实锤）+ 去截断省略号 + 小写。"""
    return "".join(str(text or "").split()).replace("…", "").lower()


def card_region_blocks(results: Sequence[dict], rect) -> List[dict]:
    """过滤出窗口左下角福利卡片区域的 OCR 块（纯函数）。

    rect = (left, top, width, height)（_window_rect 屏幕坐标）；OCR 结果的
    center 是屏幕绝对坐标，换算成窗口相对比例后做区域闸。
    """
    if not rect:
        return []
    left, top, width, height = rect
    out: List[dict] = []
    for item in results or []:
        center = (item or {}).get("center")
        if not center or len(center) < 2:
            continue
        fx = (center[0] - left) / max(1, int(width))
        fy = (center[1] - top) / max(1, int(height))
        if fx <= CARD_REGION_X_MAX_FRAC and fy >= CARD_REGION_Y_MIN_FRAC:
            out.append(item)
    return out


def claim_button_block(results: Sequence[dict]) -> Optional[dict]:
    """在（已过区域闸的）OCR 块里选「领取」按钮块（纯函数）。

    优先级：
      1. 归一化后**恰为**「领取」的独立块——黑底白字按钮 OCR 几乎总是单独成块；
      2. 并块：含「领取」且长度明显超过按钮四个字符、且带卡片锚点词
        （「今日可领100积分领取」这类并块形态）——排除同区域其他含「领取」的
        长文本块误判。
    都不命中返回 None。
    """
    for item in results or []:
        if _norm_text((item or {}).get("text")) == CLAIM_BUTTON_TEXT:
            return item
    for item in results or []:
        text = _norm_text((item or {}).get("text"))
        if (
            CLAIM_BUTTON_TEXT in text
            and len(text) > len(CLAIM_BUTTON_TEXT) + 2
            and any(marker in text for marker in CARD_ANCHOR_TEXTS)
        ):
            return item
    return None


def claim_click_point(block: dict) -> Optional[Tuple[int, int]]:
    """「领取」点击点（纯函数）：精确块取中心；并块按按钮右对齐内缩换算。

    坐标系坑（同 CODEARTS）：box 是截图局部坐标、center 是屏幕绝对坐标
    （差一个区域原点，结果里不带原点）——并块兜底不能直接用 box 绝对值，
    改算「按钮点击位相对块中心的偏移」再叠加到 center 上，与原点无关：
        点击位 x = 块右缘 - MERGED_BUTTON_INSET
        相对偏移 = (x2 - inset) - (x1 + x2) / 2
    """
    center = (block or {}).get("center")
    if not center or len(center) < 2:
        return None
    cx, cy = int(center[0]), int(center[1])
    text = _norm_text((block or {}).get("text"))
    box = (block or {}).get("box")
    if text != CLAIM_BUTTON_TEXT and box and len(box) >= 4:
        x1, x2 = int(min(box[0], box[2])), int(max(box[0], box[2]))
        if (x2 - x1) > 3 * MERGED_BUTTON_INSET:
            cx = int(cx + (x2 - MERGED_BUTTON_INSET) - (x1 + x2) / 2)
    return cx, cy


def popup_state(results: Sequence[dict]) -> str:
    """福利卡片状态判定（纯函数）：claimable / claimed / absent。

    - 已领类文字在场（已领/明日再来/已签到）→ claimed；
      注意先于 claimable 判：已领态「已领取」也含「领取」子串，顺序反了会把
      已领态当可点态再点一次。
    - 有可点的「领取」块 → claimable；
    - 两者皆无 → absent（卡片不在/未渲染，由调用方结合等待预算裁决幂等出口）。
    """
    for item in results or []:
        text = _norm_text((item or {}).get("text"))
        if text and any(keyword in text for keyword in CLAIMED_TEXTS):
            return "claimed"
    if claim_button_block(results) is not None:
        return "claimable"
    return "absent"


def has_card_anchor(results: Sequence[dict]) -> bool:
    """左下区域里是否能看到福利卡片锚点词（诊断用：区分「卡片在但按钮没认出」）。"""
    for item in results or []:
        text = _norm_text((item or {}).get("text"))
        if text and any(marker in text for marker in CARD_ANCHOR_TEXTS):
            return True
    return False


# ── 真机交互层（pytest 全部打桩，绝不真动鼠标）────────────────────────


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl

    return impl(hwnd)


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


def _to_local(rect, screen_xy) -> Tuple[int, int]:
    left, top, _w, _h = rect
    return int(screen_xy[0]) - left, int(screen_xy[1]) - top


def _wait_for(
    logger: logging.Logger,
    hwnd: int,
    rect,
    judge,
    timeout_sec: float,
    interval: float,
    stop_check=None,
):
    """轮询 OCR 直到 judge(results) 返回非 None；超时返回 None。

    judge 收到的是**未过滤**的全窗 OCR 结果（区域闸在 judge 内部用 rect 做，
    便于纯函数直接测）；每轮先打任务心跳（新增等待循环必须续命，06 分篇
    2026-11-05 起）并尊重 stop_check。
    """
    from .foreground_flow_runner import _heartbeat_beat

    end = time.time() + max(1.0, float(timeout_sec))
    last = None
    while time.time() < end:
        _heartbeat_beat()
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
    """等左下角福利卡片 → 点「领取」→ OCR 三态核验。

    契约：成功 (True, 说明)；失败 (False, 原因)。全程尊重 stop_check，且共享
    timeout_sec 总预算（分相等待按剩余预算封顶，防 OCR 变慢时两段等待叠加
    穿透上层步骤超时）。幂等：卡片显示已领态 / 等不到卡片都按今日已领通过。
    scales/confidences 仅保留签名兼容（全 OCR 零模板，用不到）。
    """
    logger = logger or LOGGER
    del scales, confidences
    rect = _window_rect(hwnd)
    if not rect:
        return False, "未找到 AStudio 窗口"
    deadline = time.time() + max(30, int(timeout_sec))

    def budgeted(sec: float) -> float:
        """分相等待按剩余总预算封顶（最少 1s，让 judge 至少跑一轮再判超时）。"""
        return max(1.0, min(sec, deadline - time.time()))

    def stopped() -> bool:
        return bool(stop_check and stop_check())

    visible, reason = _ensure_visible(hwnd)
    if not visible:
        return False, reason

    def card_judge(ocr_results):
        blocks = card_region_blocks(ocr_results, rect)
        state = popup_state(blocks)
        if state == "claimed":
            return ("claimed", None)
        if state == "claimable":
            return ("claimable", claim_button_block(blocks))
        return None

    # ── Phase 1：等卡片出现（已领态直接幂等出口）──
    found = _wait_for(logger, hwnd, rect, card_judge, budgeted(POPUP_WAIT_SEC), 1.0, stop_check)
    if stopped():
        return False, "用户请求停止"
    if not found:
        # 卡片等不到：没有佐证物可区分「已领/无福利」与「渲染异常」，按幂等
        # 成功走（可领时卡片随主界面稳定出现）。首跑若频繁走这条出口却没到账，
        # 先补已领态截图再改判定。
        logger.warning("[AStudio] 福利卡片 %ds 未出现，视为今日已领/今日无福利（幂等通过，首跑注意核对）",
                       int(POPUP_WAIT_SEC))
        return True, f"福利卡片未出现（等了 {int(POPUP_WAIT_SEC)}s），视为今日已领/无福利"
    state, btn = found
    if state == "claimed":
        logger.info("[AStudio] 福利卡片已显示已领态文字，今日已领（幂等通过）")
        return True, "卡片显示已领态文字（视为今日已领）"

    # ── Phase 2：点击「领取」──
    point = claim_click_point(btn)
    if not point:
        return False, "「领取」块缺少 center 坐标，无法换算点击点"
    lx, ly = _to_local(rect, point)
    _click(hwnd, lx, ly)
    logger.info("[AStudio] 已点击「%s」 @ (%d, %d) [块: %s]",
                CLAIM_BUTTON_TEXT, point[0], point[1], str(btn.get("text"))[:40])

    # ── Phase 3：核验（fail-closed）：已领文字 或 卡片收起 才算成功 ──
    def verify_judge(ocr_results):
        if not ocr_results:
            # OCR 全空（窗口被挡/引擎故障）≠ 卡片已收起：继续轮询，否则会把
            # 一次 OCR 故障误判成「卡片消失」假成功。
            return None
        state = popup_state(card_region_blocks(ocr_results, rect))
        if state == "claimed":
            return "claimed"
        if state == "absent":
            return "closed"
        return None

    verdict = _wait_for(logger, hwnd, rect, verify_judge, budgeted(VERIFY_POLL_SEC), 1.5, stop_check)
    if verdict == "claimed":
        msg = "卡片出现已领态文字"
    elif verdict == "closed":
        msg = "点击后「领取」已消失（卡片收起）"
    else:
        extra = "" if has_card_anchor(_ocr(logger, hwnd)) else "（且未见卡片锚点词，可能点到别处）"
        return False, f"点击领取后卡片状态未变化（按钮仍在左下区域）{extra}"
    logger.info("[AStudio] 领取核验通过: %s", msg)
    return True, msg
