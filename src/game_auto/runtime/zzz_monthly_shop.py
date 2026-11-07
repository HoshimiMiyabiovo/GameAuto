"""绝区零后勤商店每月采购（2026-10-21）。

由 ZZZ_MONTHLY 流程的「后勤商店采购」步骤（action=zzz_shop_buy）驱动，
业务参数（页签/商品/数量）全在 flow_configs.json 的 step.shop_groups，
本模块只含交互逻辑，不含购买清单。

交互模型（按用户 2026-10-21 截图实测）：
- 打开商店默认停在「哨站后勤」页；「协会物资」需点左侧页签切换
- 点商品图标 → 弹「兑换确认」→ 点「确认」→ 弹窗关闭；弹窗默认兑换数量 ×1，
  因此买 N 个 = N 轮「点商品 → 确认」，不操作数量控件（最稳）
- 已达上限的商品点击后不弹窗（确认按钮不出现），此时跳过该商品剩余次数
- 商品定位：优先模板；模板缺失或未命中时用 item.ocr_text 做 OCR 文字定位兜底
  （点文字中心，商品卡整卡为热区），用于暂无模板图的新商品

模板路径统一经 _abs_template 锚定到项目根目录（与 _normalize_flow_paths 同规则），
flow 配置里写相对路径「assets/templates\\xxx.png」即可。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..settings.paths import BASE_DIR
from .monthly_shop_evidence import capture_shop_evidence
from .ui_coords import CONFIRM_MIN_Y_RATIO

LOGGER = logging.getLogger("GameAutomation")

ITEM_TIMEOUT_SEC = 20        # 商品图标匹配超时（轮询）
OCR_TIMEOUT_SEC = 25         # OCR 文字定位超时（轮询，单次识别 1-3s）
CONFIRM_TIMEOUT_SEC = 15     # 点商品后等确认按钮出现
CONFIRM_GONE_SEC = 10        # 点确认后等弹窗关闭
TAB_TIMEOUT_SEC = 20         # 页签匹配超时（切换用）
TAB_SELECTED_TIMEOUT_SEC = 10  # 目标页签选中态确认超时
SETTLE_SEC = 0.8             # 点击后画面稳定等待
TAB_SWITCH_SETTLE_SEC = 2.0  # 页签切换后等待
# 兑换成功后的「获得物品」浮层（原神/崩铁都有，中间偏下显示"纠缠之缘×1"）。
# 2026-11-03 实锤：第 1 个买完后浮层还挂着，第 2 次的 OCR 直接命中浮层上的商品名
# （原神日志：纠缠之缘 @ (960,607)，正是浮层位置，不是卡片），点过去没有弹窗 →
# 被 `_buy_once` 判成"已达上限"，于是纠缠之缘/相遇之缘各只买 1 个（采购前后截图
# 5→4），却记录 1/5 当成功。这里买完先等浮层消失再让下一次定位。
POST_BUY_SETTLE_SEC = 2.5
# 第一次点商品后没等到确认弹窗时的重试间隔：等「获得物品」浮层散掉再定位一次。
RETRY_SETTLE_SEC = 2.0
# 2026-11-05 异环实锤：它的「获得物品」浮层写着「点击空白区域关闭」，
# **不会自动散掉**（原神/崩铁的浮层会自动消失，所以 2.5s 的等待以前够用）。
# 浮层一直盖在商品卡上，下一轮定位/点击全落在浮层 → 质感骰子 1/5、
# 三重钥匙 0/20，还被当成「已达上限」写进成功报告与月度标记。
REWARD_POPUP_TEXTS = ("获得物品", "点击空白区域关闭")
# 浮层是居中大卡片，右下角是明确的空白区（避开左侧导航、顶部页签、商品卡）。
REWARD_POPUP_DISMISS_FRAC = (0.92, 0.93)
REWARD_POPUP_DISMISS_TRIES = 3
REWARD_POPUP_SETTLE_SEC = 0.8

# 2026-11-06 提权实机（data/artifacts/zzz_stage1.py）反复踩到：兑换确认弹窗的
# **标题**就叫「兑换确认」@y≈316，真正的确认按钮在弹窗**底部** @y≈763（1080p）。
# OCR 按「确认」找会先命中标题，点上去库存一点没扣，看起来却像"点了确认"。
# 正式代码以前只走 confirm_template，模板失配时既没有 OCR 兜底，也不校验命中点
# 是否在底部；11-06 上午 #201「加密母带 0/5（确认后弹窗未关）」即此通道。
# 口径与探针一致：只认窗口下部（ui_coords.CONFIRM_MIN_Y_RATIO = 700/1080）的按钮词，
# 模板命中也必须过同一道闸；比例按窗高换算，换 4K / 开 DSR 都不失效。
CONFIRM_BUTTON_TEXTS = ("确认", "购买", "确定")
CONFIRM_OCR_MIN_CONF = 0.4
CONFIRM_TEMPLATE_FIRST_SEC = 5                       # 模板先短轮询，失配立刻转 OCR
CONFIRM_OCR_RETRY_SEC = CONFIRM_TIMEOUT_SEC - CONFIRM_TEMPLATE_FIRST_SEC
CONFIRM_GONE_POLL_SEC = 0.5
CONFIRM_GONE_NEAR_FRAC = 0.04    # 「按钮还在原位」的容差（窗口宽/高比例）

EXIT_MAX_ESC = 3             # 采购完最多按几次 ESC 回主世界
EXIT_CHECK_SEC = 5           # 每次 ESC 后等主世界特征出现的超时

# 单次购买结果
BOUGHT = "bought"                # 买成功 1 个
NO_POPUP = "no_popup"            # 点商品后无弹窗（大概率已达上限）
NOT_FULL = "not_full"            # 点商品无弹窗，但页面还显示有剩余（中断，未买满）
ITEM_MISSING = "item_missing"    # 商品图标匹配不到
CONFIRM_GONE_FAIL = "confirm_gone_fail"  # 点了确认但弹窗不关（如余额不足换了提示）
REWARD_POPUP_STUCK = "reward_popup_stuck"  # 「获得物品」浮层关不掉，后续采购不可信
PURCHASE_UNVERIFIED = "purchase_unverified"  # 点完确认但余额/库存没变，疑似假成功
STOPPED = "stopped"              # 用户请求停止

# 「本月已经买过」的两种表现，都不计失败（用户 2026-11-01 拍板）：
#   1. 点了商品不出弹窗 —— 已达上限
#   2. 页面上根本看不到商品 —— 到上限后商店直接不展示该商品
# 买之前已确认进对页签，所以"看不到"按已购买处理，不是失败。
ALREADY_BOUGHT_REASONS = (
    "已达上限或无弹窗",
    "页面上没看到该商品，按已购买处理",
)


def _abs_template(path: str) -> str:
    """相对模板路径锚定到项目根目录；绝对路径原样返回。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    cand = os.path.join(str(BASE_DIR), path)
    return cand if os.path.exists(cand) else path


def _locate(
    hwnd: int,
    template: str,
    scales,
    confidences,
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]] = None,
    interval: float = 0.5,
    use_color: bool = False,
) -> Optional[Tuple[int, int]]:
    """轮询匹配模板，命中返回窗口局部中心坐标，超时/停止返回 None。

    use_color=True 走三通道匹配：只对「灰度分不开、颜色是唯一判据」的商品开
    （异环红/蓝骰子灰度互匹配 0.976，彩色 0.689）。
    """
    from .foreground_flow_runner import _focus_window, _locate_template_in_window

    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None
        _focus_window(hwnd)
        center = _locate_template_in_window(
            hwnd, template, scales, confidences, use_color=use_color,
        )
        if center:
            return center
        time.sleep(interval)
    return None


def _wait_gone(
    hwnd: int,
    template: str,
    scales,
    confidences,
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]] = None,
    interval: float = 0.5,
) -> bool:
    """轮询等待模板消失（弹窗关闭）。超时返回 False。"""
    from .foreground_flow_runner import _locate_template_in_window

    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return False
        if _locate_template_in_window(hwnd, template, scales, confidences) is None:
            return True
        time.sleep(interval)
    return False


def _locate_ocr(
    logger: logging.Logger,
    hwnd: int,
    text: str,
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]] = None,
    interval: float = 0.5,
) -> Optional[Tuple[int, int]]:
    """轮询用 OCR 定位目标文字（无模板商品的兜底），命中返回窗口局部中心坐标。"""
    from .foreground_flow_runner import _focus_window, _ocr_locate_in_window

    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None
        _focus_window(hwnd)
        center = _ocr_locate_in_window(logger, hwnd, text)
        if center:
            return center
        time.sleep(interval)
    return None


def _confirm_min_local_y(hwnd: int, ratio=None) -> Optional[int]:
    """「底部确认按钮」的窗口局部 y 下界；窗口尺寸取不到返回 None（不猜测，放行）。

    `ui_coords.min_confirm_y` 吃 (l,t,r,b) 且返回**屏幕** y；这里 `_window_rect`
    返回 (l,t,w,h)，点击坐标又全是**窗口局部**，所以直接用比例乘窗高，不混两套口径。
    取不到 rect（窗口最小化/测试桩）时返回 None：宁可保持旧行为，也不凭空拦掉购买。
    """
    from .foreground_flow_runner import _window_rect

    rect = _window_rect(hwnd)
    if not rect:
        return None
    try:
        value = float(ratio if ratio is not None else CONFIRM_MIN_Y_RATIO)
    except (TypeError, ValueError):
        value = CONFIRM_MIN_Y_RATIO
    if not 0.0 < value < 1.0:
        value = CONFIRM_MIN_Y_RATIO
    return int(rect[3] * value)


def _confirm_bottom_region(hwnd: int, min_y: Optional[int]):
    """只扫弹窗底部的 OCR 区域（屏幕绝对 l,t,w,h）；标题「兑换确认」根本进不来。"""
    from .foreground_flow_runner import _window_rect

    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    cut = min_y if min_y is not None else 0
    return (left, top + cut, width, max(1, height - cut))


def _ocr_bottom_confirm(
    logger: logging.Logger,
    hwnd: int,
    min_y: Optional[int],
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """OCR 兜底找底部确认按钮，返回 (窗口局部坐标, 命中文本)；找不到返回 (None, None)。

    与 11-06 实机探针同口径：按钮词 + 置信度 + 底部区域。region 已把标题截掉，
    坐标仍按 `_ocr_scan` 契约减窗口原点换算回局部坐标再交给 `_click_window_local`。
    """
    from .foreground_flow_runner import _window_rect
    from .ocr_click_tester import ocr_window_content

    rect = _window_rect(hwnd)
    if not rect:
        return None, None
    left, top, _width, _height = rect
    region = _confirm_bottom_region(hwnd, min_y)
    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None, None
        try:
            items = ocr_window_content(logger, hwnd=hwnd, region=region) or []
        except Exception as exc:  # noqa: BLE001 - OCR 失败按未命中处理，下一轮重试
            LOGGER.debug(f"[商店采购] 底部确认按钮 OCR 失败: {exc}")
            items = []
        cands = []
        for item in items:
            text = str(item.get("text") or "")
            if not any(word in text for word in CONFIRM_BUTTON_TEXTS):
                continue
            if float(item.get("confidence") or 0) < CONFIRM_OCR_MIN_CONF:
                continue
            center = item.get("center") or (0, 0)
            local = (int(center[0]) - left, int(center[1]) - top)
            if min_y is not None and local[1] < min_y:
                continue
            cands.append((float(item.get("confidence") or 0), local, text))
        if cands:
            cands.sort(key=lambda c: (-c[0], c[1][1]))
            _conf, local, text = cands[0]
            return local, text
        time.sleep(CONFIRM_GONE_POLL_SEC)
    return None, None


def _locate_confirm_button(
    logger: logging.Logger,
    hwnd: int,
    template: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    min_y_ratio=None,
) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """定位确认按钮 → (窗口局部坐标, "template"/"ocr")；找不到 → (None, None)。

    模板命中**必须**在弹窗底部；命中标题区（y < 下界）或模板失配一律转 OCR 底部兜底。
    模板只先等 CONFIRM_TEMPLATE_FIRST_SEC，剩下的时间留给 OCR，总时长仍是
    CONFIRM_TIMEOUT_SEC，不拖慢「弹窗出得慢」的场景。
    """
    min_y = _confirm_min_local_y(hwnd, min_y_ratio)
    center = _locate(
        hwnd, template, scales, confidences, CONFIRM_TEMPLATE_FIRST_SEC, stop_check,
    )
    if center and (min_y is None or center[1] >= min_y):
        return center, "template"
    if center:
        logger.warning(
            f"[商店采购] 确认按钮模板命中 y={center[1]} < 底部下界 {min_y}，"
            "疑似弹窗标题「兑换确认」，不点击，改走 OCR 底部兜底"
        )
    else:
        logger.info("[商店采购] 确认按钮模板未命中，改走 OCR 底部兜底")
    local, text = _ocr_bottom_confirm(
        logger, hwnd, min_y, CONFIRM_OCR_RETRY_SEC, stop_check,
    )
    if local:
        logger.info(f"[商店采购] OCR 命中底部确认按钮「{text}」@ {local}")
        return local, "ocr"
    return None, None


def _confirm_button_still_at(
    logger: logging.Logger,
    hwnd: int,
    point: Tuple[int, int],
    min_y: Optional[int],
) -> bool:
    """点完确认后，原位置附近是否还看得到同一个底部按钮（= 弹窗没关）。"""
    from .foreground_flow_runner import _window_rect
    from .ocr_click_tester import ocr_window_content

    rect = _window_rect(hwnd)
    if not rect:
        return False
    left, top, width, height = rect
    tol = max(20, int(max(width, height) * CONFIRM_GONE_NEAR_FRAC))
    try:
        items = ocr_window_content(
            logger, hwnd=hwnd, region=_confirm_bottom_region(hwnd, min_y),
        ) or []
    except Exception as exc:  # noqa: BLE001 - 取不到证据就不拦，交给模板 _wait_gone
        LOGGER.debug(f"[商店采购] 确认按钮消失复核 OCR 失败: {exc}")
        return False
    for item in items:
        text = str(item.get("text") or "")
        if not any(word in text for word in CONFIRM_BUTTON_TEXTS):
            continue
        if float(item.get("confidence") or 0) < CONFIRM_OCR_MIN_CONF:
            continue
        center = item.get("center") or (0, 0)
        local = (int(center[0]) - left, int(center[1]) - top)
        if min_y is not None and local[1] < min_y:
            continue
        if abs(local[0] - point[0]) <= tol and abs(local[1] - point[1]) <= tol:
            return True
    return False


def _read_verify_signature(
    logger: logging.Logger,
    hwnd: int,
    verify: Optional[Dict],
):
    """读「月剩余/本月限购/本月库存」等数字当购买证据，返回排序后的数字元组。

    11-06 实机教训：只看「确认按钮点过了」会假成功（前两次库存 3/5、20/20 一点
    没变，靠截图对比才发现）。这里不依赖任何坐标——整窗 OCR 里凡是命中配置标签
    的数字全收进签名，真买了签名必变；**读不到就返回 None 保守放行**，避免弹窗
    遮罩 / 浮层遮挡 / 别的游戏没有这个标签时误判成失败。
    """
    if not isinstance(verify, dict) or not verify:
        return None
    labels = [str(t).strip() for t in (verify.get("labels") or []) if str(t).strip()]
    if not labels:
        return None
    from .ocr_click_tester import ocr_window_content

    try:
        items = ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:  # noqa: BLE001 - 取不到证据就不拦
        LOGGER.debug(f"[商店采购] 购买证据 OCR 失败: {exc}")
        return None
    numbers = []
    for item in items:
        text = str(item.get("text") or "")
        if not any(label in text for label in labels):
            continue
        numbers.extend(int(m.group()) for m in re.finditer(r"\d+", text))
    return tuple(sorted(numbers)) if numbers else None


def _wait_confirm_button_gone(
    logger: logging.Logger,
    hwnd: int,
    point: Tuple[int, int],
    min_y: Optional[int],
    stop_check: Optional[Callable[[], bool]],
    timeout_sec: int = CONFIRM_GONE_SEC,
) -> bool:
    """OCR 兜底路径专用：模板本就失配时 `_wait_gone` 恒真，必须自己复核按钮消失。"""
    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return False
        if not _confirm_button_still_at(logger, hwnd, point, min_y):
            return True
        time.sleep(CONFIRM_GONE_POLL_SEC)
    return False


def _frac_point(hwnd: int, frac) -> Optional[Tuple[int, int]]:
    """把 [fx, fy]（窗口相对比例）换算成窗口局部像素坐标。

    给「图标在灰度下无法区分」的商品兜底：异环迷迭兑换的质感骰子（红）和
    捏造骰子（蓝）转灰度后互相匹配 0.976，模板/OCR 都认不准，只能按固定布局点。
    比例坐标跟着窗口尺寸走，换分辨率不会失效，但游戏改版布局变了要重新量。
    """
    try:
        from .foreground_flow_runner import _window_rect

        rect = _window_rect(hwnd)
        if not rect:
            return None
        _, _, width, height = rect
        fx, fy = float(frac[0]), float(frac[1])
        if not (0.0 < fx < 1.0 and 0.0 < fy < 1.0):
            return None
        return (int(fx * width), int(fy * height))
    except Exception as exc:  # noqa: BLE001 - 取不到窗口就当定位失败
        LOGGER.debug(f"[商店采购] 比例坐标换算失败 {frac}: {exc}")
        return None


def _locate_item(
    logger: logging.Logger,
    hwnd: int,
    item: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    label: str,
    ocr_timeout: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """按 point_frac → 模板 → OCR 的优先级定位商品，返回窗口局部中心坐标。

    point_frac 是给「图标灰度相同、OCR 也认不准」的商品用的（异环两个骰子）。
    拆成独立函数是为了让「当前视图找不到 → 翻页再找」能复用同一套定位逻辑
    （2026-11-02：崩铁余烬兑换的商品网格同屏只有 2~3 行，票类商品在折叠区以下，
    一次整窗 OCR 根本看不到，会被"看不到=已买过"吞成静默漏买）。

    ocr_timeout：翻页场景专用。默认 `OCR_TIMEOUT_SEC=25s` 是给"首屏找商品"的，
    翻页时每屏都等满 25s 会变成 8 屏×25s 的空转（2026-11-03 实测 3 件商品烧了
    12 分钟），翻页只需单次整窗 OCR 就能判断当前屏有没有，给 6s 足够。
    """
    item_name = str(item.get("name", "商品") or "商品")
    item_template = _abs_template(str(item.get("template", "") or ""))
    ocr_text = str(item.get("ocr_text", "") or "").strip()
    has_template = bool(item_template) and os.path.isfile(item_template)
    point_frac = item.get("point_frac")
    use_color = bool(item.get("color_match"))

    center = None
    # 模板优先（配了 color_match 时走三通道，红/蓝骰子靠它分开）；
    # 模板没命中才退到固定比例坐标，最后才是 OCR。
    if has_template:
        center = _locate(
            hwnd, item_template, scales, confidences, ITEM_TIMEOUT_SEC, stop_check,
            use_color=use_color,
        )
        if center and use_color:
            logger.info(f"[{label}] {item_name} 彩色模板命中 @ {center}")
    if center is None and isinstance(point_frac, (list, tuple)) and len(point_frac) == 2:
        center = _frac_point(hwnd, point_frac)
        if center:
            logger.info(f"[{label}] {item_name} 模板未命中，改用固定比例坐标 @ {center}")
        else:
            logger.warning(f"[{label}] {item_name} 比例坐标无效: {point_frac}")
    if center is None and ocr_text:
        if stop_check is not None and stop_check():
            return None
        way = "OCR文字定位（无模板）" if not has_template else "OCR兜底（模板未命中）"
        logger.info(f"[{label}] {item_name} 改用{way}: {ocr_text}")
        center = _locate_ocr(
            logger, hwnd, ocr_text, int(ocr_timeout or OCR_TIMEOUT_SEC), stop_check
        )
    return center


# 「本月剩余数量:4」（原神）/「可兑换: 4/5」（崩铁）——都取第一个数字当剩余可兑换数。
# 2026-11-03 用户要求：纠缠之缘还写着 4/5 就必须算未完成，不能按"已达上限"静默成功。
_REMAIN_PATTERNS = (
    re.compile(r"剩余数量\s*[:：]?\s*(\d+)"),
    re.compile(r"可兑换\s*[:：]?\s*(\d+)\s*[／/]"),
)


def _read_item_remaining(
    logger: logging.Logger,
    hwnd: int,
    center: Tuple[int, int],
    radius: int = 320,
    item_text: str = "",
) -> Optional[int]:
    """读目标商品卡片附近的剩余可兑换数量，读不到返回 None。

    只在「点击无弹窗」时用来区分两种状态：
      - 剩余 > 0：根本没买满（浮层遮挡/点击落空），必须报未完成；
      - 剩余 = 0 或读不到：商品到月上限后不展示/变灰，维持旧的"已购"口径。
    radius 按卡片尺寸给的：商品名和「剩余数量」行通常在同一张卡里，320px 足够，
    又不会串到隔壁卡。

    item_text（2026-11-04 守卫）：商品自己的名字/ocr_text。崩铁的商品到月上限
    后**整卡从货架消失**（08:47 证据图：命运的足迹 2/2 后第一格直接空掉），
    这时点中的是隔壁卡——如果只看「附近有没有 可兑换: N/M」，邻卡的
    「可兑换: 80/80」会把真买完误判成"未买满"，从此每月都报失败。
    所以给了 item_text 时，必须商品名也出现在坐标附近（= 卡还是这张卡）
    才允许报剩余 > 0；名字不在 → 返回 None 走旧口径。
    """
    try:
        from .ocr_click_tester import ocr_window_content

        results = ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:  # noqa: BLE001 - 读不到就交给旧口径，不能拖垮采购
        LOGGER.debug(f"[商店采购] 读取商品剩余数量失败: {exc}")
        return None
    cx, cy = int(center[0]), int(center[1])
    own = str(item_text or "").replace(" ", "").strip()
    own_found = False
    best_val: Optional[int] = None
    best_dist: Optional[int] = None
    for r in results:
        try:
            c = r.get("center") or (0, 0)
            x, y = int(c[0]), int(c[1])
            if abs(x - cx) > radius or abs(y - cy) > radius:
                continue
            text = str(r.get("text") or "")
            compact = text.replace(" ", "")
            # 商品名只用来判定"这张卡还是不是目标卡"；「可兑换: N/M」徽标行
            # 本来就不含商品名，两件事不能混在同一个 continue 里（否则徽标
            # 永远读不到，剩余数量恒为 None，2026-11-04 首版踩坑）。
            if own and (own in compact or compact in own):
                own_found = True
            for pat in _REMAIN_PATTERNS:
                m = pat.search(text)
                if not m:
                    continue
                val = int(m.group(1))
                dist = abs(x - cx) + abs(y - cy)
                if best_dist is None or dist < best_dist:
                    best_val, best_dist = val, dist
                break
        except Exception:
            continue
    if own and not own_found:
        # 坐标附近没有商品自己的名字：点中的多半是别的东西（卡片已消失），
        # 邻卡的剩余数量不可信，按"读不到"处理。
        LOGGER.debug(
            f"[商店采购] 坐标 {center} 附近未找到商品名「{own}」，剩余数量不可信"
        )
        return None
    return best_val


def _dismiss_reward_popup(logger: logging.Logger, hwnd: int, label: str):
    """买成功后确保「获得物品」浮层消失。

    原神/崩铁的浮层会自动散掉（POST_BUY_SETTLE_SEC 够用），异环的浮层
    写着「点击空白区域关闭」、**不点就一直挂着**，把商品卡全遮住。这里
    OCR 检测浮层文字 → 点右下角空白 → 复查，最多 3 次。

    返回 None=本来就没有浮层（正常）、True=已关闭、False=关不掉（异常）。
    """
    from .foreground_flow_runner import _click_window_local, _window_rect
    from .ocr_click_tester import ocr_window_content

    rect = _window_rect(hwnd)
    if not rect:
        return None
    _, _, width, height = rect
    x = int(width * REWARD_POPUP_DISMISS_FRAC[0])
    y = int(height * REWARD_POPUP_DISMISS_FRAC[1])
    seen = False
    for attempt in range(1, REWARD_POPUP_DISMISS_TRIES + 1):
        try:
            texts = [
                str(r.get("text") or "")
                for r in (ocr_window_content(logger, hwnd=hwnd) or [])
            ]
        except Exception as exc:  # noqa: BLE001 - OCR 不可用就按"没有浮层"处理，不拖垮采购
            LOGGER.debug("[%s] 检测获得物品浮层失败: %s", label, exc)
            return None
        if not any(any(k in t for k in REWARD_POPUP_TEXTS) for t in texts):
            return True if seen else None
        seen = True
        logger.info(f"[{label}] 检测到「获得物品」浮层，点空白关闭（第 {attempt} 次）@ ({x},{y})")
        _click_window_local(hwnd, x, y, clicks=1)
        time.sleep(REWARD_POPUP_SETTLE_SEC)
    logger.error(f"[{label}] 「获得物品」浮层点了 {REWARD_POPUP_DISMISS_TRIES} 次仍在，停止本商品采购")
    return False


def _buy_once(
    logger: logging.Logger,
    hwnd: int,
    item: Dict,
    confirm_template: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    label: str = "商店采购",
    scroll_finder: Optional[Callable[..., Optional[Tuple[int, int]]]] = None,
    step: Optional[Dict] = None,
) -> str:
    """执行一次「点商品 → 确认」。返回 BOUGHT/NO_POPUP/.../PURCHASE_UNVERIFIED/STOPPED。

    商品定位优先级：point_frac（固定比例坐标）→ 模板 → OCR 文字兜底。
    point_frac 是给「图标灰度相同、OCR 也认不准」的商品用的（异环两个骰子），
    点完同样要等确认弹窗出现，点空/点错照样会暴露成 NO_POPUP。
    label 只影响日志前缀（信源残响兑换复用本函数，避免日志里出现"商店采购"字样）。
    scroll_finder：当前视图定位不到时的翻页兜底（见 monthly_exchange._scroll_find_item）；
    为 None 时保持旧行为（找不到 = 已买过，不做额外操作）。
    step：`confirm_min_y_ratio`（底部按钮下界比例）/ `purchase_verify`（购买证据
    标签词表）两个可选配置，缺省时只启用底部按钮过滤，不做数字核对。
    """
    from .foreground_flow_runner import _click_window_local

    if stop_check is not None and stop_check():
        return STOPPED

    step = step or {}
    min_y_ratio = step.get("confirm_min_y_ratio")
    verify = step.get("purchase_verify")
    # 点商品之前读一次证据：此时商店页干净，弹窗/浮层还没遮住限购数字。
    before = _read_verify_signature(logger, hwnd, verify) if verify else None

    item_name = str(item.get("name", "商品") or "商品")
    center = _locate_item(logger, hwnd, item, scales, confidences, stop_check, label)
    if center is None and scroll_finder is not None:
        if stop_check is not None and stop_check():
            return STOPPED
        logger.info(f"[{label}] {item_name} 当前视图没找到，翻页再找")
        center = scroll_finder(logger, hwnd, item, scales, confidences, stop_check, label)
    if center is None:
        if stop_check is not None and stop_check():
            return STOPPED
        logger.warning(f"[{label}] {item_name} 定位失败（模板/OCR 均未命中）")
        return ITEM_MISSING

    _click_window_local(hwnd, center[0], center[1], clicks=1)
    time.sleep(SETTLE_SEC)

    confirm_center, confirm_method = _locate_confirm_button(
        logger, hwnd, confirm_template, scales, confidences, stop_check,
        min_y_ratio=min_y_ratio,
    )
    if confirm_center is None:
        if stop_check is not None and stop_check():
            return STOPPED
        # 2026-11-03 实锤：上一次兑换的「获得物品」浮层可能还挂着（浮层上有商品名），
        # 这一击落在浮层上、等不到确认弹窗，就被判"已达上限"——原神纠缠之缘只买到
        # 1/5（采购前后截图 5→4）却记成功。等 2 秒重新定位再点一次，仍无弹窗才算真上限。
        time.sleep(RETRY_SETTLE_SEC)
        retry_center = _locate_item(logger, hwnd, item, scales, confidences, stop_check, label)
        if retry_center:
            logger.info(f"[{label}] {item_name} 首次点击未弹确认，重新定位再点一次 @ {retry_center}")
            _click_window_local(hwnd, retry_center[0], retry_center[1], clicks=1)
            time.sleep(SETTLE_SEC)
            confirm_center, confirm_method = _locate_confirm_button(
                logger, hwnd, confirm_template, scales, confidences, stop_check,
                min_y_ratio=min_y_ratio,
            )
    if confirm_center is None:
        if stop_check is not None and stop_check():
            return STOPPED
        # 2026-11-03 用户口径：页面还写着"剩余 4"就说明没买满，不能算已达上限。
        # item_text 守卫（2026-11-04）：卡片到上限会整卡消失，点中的可能是邻卡，
        # 邻卡的「可兑换: 80/80」不能拿来判未买满。
        own_text = str(item.get("ocr_text") or item_name or "").strip()
        remain = _read_item_remaining(logger, hwnd, center, item_text=own_text)
        if remain is not None and remain > 0:
            logger.warning(
                f"[{label}] {item_name} 点击无弹窗，但页面仍显示剩余 {remain} 个 → 判未买满"
            )
            return NOT_FULL
        logger.info(f"[{label}] 点击商品后未弹出兑换确认（可能已达上限），结束该商品")
        return NO_POPUP

    _click_window_local(hwnd, confirm_center[0], confirm_center[1], clicks=1)
    time.sleep(SETTLE_SEC)

    if confirm_method == "ocr":
        # 模板本来就没命中时 `_wait_gone` 会立刻返回 True，等于「点了标题也算买成功」。
        # 必须用同一套底部探测复核按钮真的从原位消失，否则 OCR 兜底反而开了假成功通道。
        gone = _wait_confirm_button_gone(
            logger, hwnd, confirm_center,
            _confirm_min_local_y(hwnd, min_y_ratio), stop_check,
        )
    else:
        gone = _wait_gone(hwnd, confirm_template, scales, confidences, CONFIRM_GONE_SEC, stop_check)
    if not gone:
        if stop_check is not None and stop_check():
            return STOPPED
        logger.warning(f"[{label}] 已点确认但弹窗未关闭（可能余额不足或界面异常）")
        return CONFIRM_GONE_FAIL
    # 等「获得物品」浮层散掉：不等的话下一次 OCR 会命中浮层上的商品名，
    # 点过去不是商品卡、等不到确认弹窗，就被误判成"已达上限"（2026-11-03 实锤）。
    time.sleep(POST_BUY_SETTLE_SEC)
    # 2026-11-05：异环的浮层不会自己散，必须点空白关掉；关不掉就返回失败码，
    # 让上层判失败、不写月度标记，避免「1/5 + 0/20 却算本月已买完」。
    popup = _dismiss_reward_popup(logger, hwnd, label)
    if popup is False:
        return REWARD_POPUP_STUCK

    # 防假成功：点了「确认」、弹窗也关了，但限购/库存数字一点没变 —— 11-06
    # 实机前两次就是这种假成功。两次都读到且签名完全一致才判失败，读不到就放行。
    if before is not None:
        after = _read_verify_signature(logger, hwnd, verify)
        if after is not None and after == before:
            logger.error(
                f"[{label}] {item_name} 点完确认但购买证据未变化（{before}），"
                "疑似点到弹窗标题的假成功，本次不计入已购买"
            )
            return PURCHASE_UNVERIFIED
    return BOUGHT


def _leave_shop_to_main(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    label: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> bool:
    """买完按 ESC 退回主世界（step 配了 exit_marker_template 才执行）。

    2026-11-01 用户要求「记得返回」：采购完别把游戏停在商店页，否则下次启动
    或下一个流程要人工退出。主世界特征用「绝区零-主界面-电台」那类模板，
    先确认再按键，避免多按一次又把主世界切回菜单。
    """
    marker = _abs_template(str(step.get("exit_marker_template") or ""))
    if not marker or not os.path.isfile(marker):
        return True   # 没配就当不需要退出，保持旧行为
    from .foreground_flow_runner import _press_escape

    for _ in range(EXIT_MAX_ESC):
        if stop_check is not None and stop_check():
            return False
        if _locate(hwnd, marker, scales, confidences, EXIT_CHECK_SEC, stop_check):
            logger.info(f"[{label}] 已退回主世界")
            return True
        _press_escape(hwnd)
        time.sleep(1.2)
    logger.warning(f"[{label}] 采购完成，但按 {EXIT_MAX_ESC} 次 ESC 没确认回到主世界")
    return False


def buy_shop_groups(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    *,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """按 shop_groups 配置执行后勤商店采购。

    step 需要的字段：
    - confirm_template: 兑换确认弹窗的「确认」按钮模板
    - log_label: 日志/证据截图前缀（默认「后勤商店」）
    - shop_groups: [{"name", "tab_template"?, "tab_selected_template"?,
                     "items": [{"name", "template", "count"}]}]

    返回 (是否全部买够, 摘要消息)。
    """
    log_label = str(step.get("log_label") or "后勤商店").strip() or "后勤商店"
    confirm_template = _abs_template(str(step.get("confirm_template", "") or ""))
    if not confirm_template or not os.path.isfile(confirm_template):
        return False, "确认按钮模板缺失，无法采购"

    groups = step.get("shop_groups", []) or []
    if not groups:
        return False, "shop_groups 配置为空"

    from .foreground_flow_runner import _click_window_local, _focus_window

    summaries: List[str] = []
    all_ok = True
    stopped = False
    missing_count = 0      # 页面上没看到的商品数（按"已买过"处理）
    total_items = 0        # 通过配置检查、真正参与采购的商品数

    # 采购前留一张货架截图：「看不到商品算已买过」的判定要靠它事后复核
    capture_shop_evidence(logger, hwnd, flow_label=log_label, stage="采购前")

    for group in groups:
        if stopped:
            break
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name", "未命名组") or "未命名组")
        tab_template = _abs_template(str(group.get("tab_template", "") or ""))
        tab_selected_template = _abs_template(str(group.get("tab_selected_template", "") or ""))

        # 1) 确认当前已在目标页签（哨站后勤：打开商店默认就在此页）
        if tab_selected_template and os.path.isfile(tab_selected_template):
            if _locate(hwnd, tab_selected_template, scales, confidences,
                       TAB_SELECTED_TIMEOUT_SEC, stop_check):
                logger.info(f"[商店采购] {group_name} 已在目标页签")
            else:
                logger.warning(f"[商店采购] {group_name} 未确认到选中态页签，继续尝试商品匹配")

        # 2) 需要时点击页签切换（协会物资）
        if tab_template and os.path.isfile(tab_template):
            center = _locate(hwnd, tab_template, scales, confidences, TAB_TIMEOUT_SEC, stop_check)
            if center is not None:
                _click_window_local(hwnd, center[0], center[1], clicks=1)
                logger.info(f"[商店采购] {group_name} 已点击页签切换")
                time.sleep(TAB_SWITCH_SETTLE_SEC)
            else:
                if stop_check is not None and stop_check():
                    stopped = True
                    break
                logger.warning(f"[商店采购] {group_name} 页签未匹配（可能已在目标页），继续")

        # 3) 逐商品购买
        for item in group.get("items", []) or []:
            if stopped:
                break
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name", "未命名商品") or "未命名商品")
            item_template = _abs_template(str(item.get("template", "") or ""))
            ocr_text = str(item.get("ocr_text", "") or "").strip()
            try:
                count = max(1, int(item.get("count", 1) or 1))
            except (TypeError, ValueError):
                count = 1

            has_template = bool(item_template) and os.path.isfile(item_template)
            if not has_template and not ocr_text:
                logger.error(f"[商店采购] {group_name}/{item_name} 模板缺失且无 OCR 兜底，跳过: {item_template}")
                summaries.append(f"{group_name}/{item_name}: 模板缺失跳过")
                all_ok = False
                continue
            if not has_template:
                logger.info(f"[商店采购] {group_name}/{item_name} 无模板，使用 OCR 文字定位: {ocr_text}")

            total_items += 1

            bought = 0
            reason = ""
            for _ in range(count):
                result = _buy_once(
                    logger, hwnd, item, confirm_template,
                    scales, confidences, stop_check,
                    step=step,
                )
                if result == BOUGHT:
                    bought += 1
                    continue
                if result == STOPPED:
                    stopped = True
                    reason = "用户停止"
                    break
                if result == NO_POPUP:
                    # `_buy_once` 内部已重试过一次（浮层遮挡可重试成功），仍无弹窗按上限处理
                    reason = "已达上限或无弹窗"
                    break
                if result == NOT_FULL:
                    reason = "页面仍有剩余，未买满"
                    break
                if result == ITEM_MISSING:
                    missing_count += 1
                    reason = "页面上没看到该商品，按已购买处理"
                    break
                if result == CONFIRM_GONE_FAIL:
                    reason = "确认后弹窗未关"
                    break
                if result == REWARD_POPUP_STUCK:
                    reason = "「获得物品」浮层未关闭，无法继续采购"
                    break
                if result == PURCHASE_UNVERIFIED:
                    reason = "点完确认但限购/库存数字未变，疑似假成功"
                    break

            if bought >= count:
                logger.info(f"[商店采购] {group_name}/{item_name} 完成 {bought}/{count}")
                summaries.append(f"{group_name}/{item_name}: {bought}/{count}")
            else:
                log_fn = logger.info if reason in ALREADY_BOUGHT_REASONS else logger.warning
                log_fn(
                    f"[商店采购] {group_name}/{item_name}: {bought}/{count}（{reason or '未知'}）"
                )
                summaries.append(f"{group_name}/{item_name}: {bought}/{count}（{reason or '未知'}）")
                if reason not in ALREADY_BOUGHT_REASONS:
                    # 已达上限 / 页面没看到商品 = 本月已买过，正常业务状态，不计失败
                    all_ok = False

    if total_items and missing_count == total_items:
        # 整张清单一件都没看到：按用户口径算"本月已买完"，但留一条可排查的日志
        logger.warning(
            f"[商店采购] 清单 {total_items} 项全部未在页面上出现，按本月已购买处理"
            "（如确认没买过，请检查商品模板 / OCR 文字）"
        )

    # 采购后证据：仍停在商店页，能看到剩余月上限；stopped 时也留，记下停在哪
    detail = "，".join(summaries) if summaries else "无商品"
    if stopped:
        detail += "（用户停止）"
    elif not all_ok:
        detail += "（有未买够项，见日志）"
    capture_shop_evidence(
        logger, hwnd, flow_label=log_label, stage="采购后", summary=detail, annotate=True
    )

    if stopped:
        return False, "用户停止 | " + "，".join(summaries)
    # 用户 2026-11-01 要求「记得返回」：买完退回主世界，别停在商店页
    _leave_shop_to_main(logger, hwnd, step, log_label, scales, confidences, stop_check)
    return all_ok, "，".join(summaries)
