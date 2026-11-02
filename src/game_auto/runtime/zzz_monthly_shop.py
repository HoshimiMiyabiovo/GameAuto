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
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..settings.paths import BASE_DIR
from .monthly_shop_evidence import capture_shop_evidence

LOGGER = logging.getLogger("GameAutomation")

ITEM_TIMEOUT_SEC = 20        # 商品图标匹配超时（轮询）
OCR_TIMEOUT_SEC = 25         # OCR 文字定位超时（轮询，单次识别 1-3s）
CONFIRM_TIMEOUT_SEC = 15     # 点商品后等确认按钮出现
CONFIRM_GONE_SEC = 10        # 点确认后等弹窗关闭
TAB_TIMEOUT_SEC = 20         # 页签匹配超时（切换用）
TAB_SELECTED_TIMEOUT_SEC = 10  # 目标页签选中态确认超时
SETTLE_SEC = 0.8             # 点击后画面稳定等待
TAB_SWITCH_SETTLE_SEC = 2.0  # 页签切换后等待
EXIT_MAX_ESC = 3             # 采购完最多按几次 ESC 回主世界
EXIT_CHECK_SEC = 5           # 每次 ESC 后等主世界特征出现的超时

# 单次购买结果
BOUGHT = "bought"                # 买成功 1 个
NO_POPUP = "no_popup"            # 点商品后无弹窗（大概率已达上限）
ITEM_MISSING = "item_missing"    # 商品图标匹配不到
CONFIRM_GONE_FAIL = "confirm_gone_fail"  # 点了确认但弹窗不关（如余额不足换了提示）
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
) -> Optional[Tuple[int, int]]:
    """按 point_frac → 模板 → OCR 的优先级定位商品，返回窗口局部中心坐标。

    point_frac 是给「图标灰度相同、OCR 也认不准」的商品用的（异环两个骰子）。
    拆成独立函数是为了让「当前视图找不到 → 翻页再找」能复用同一套定位逻辑
    （2026-11-02：崩铁余烬兑换的商品网格同屏只有 2~3 行，票类商品在折叠区以下，
    一次整窗 OCR 根本看不到，会被"看不到=已买过"吞成静默漏买）。
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
        center = _locate_ocr(logger, hwnd, ocr_text, OCR_TIMEOUT_SEC, stop_check)
    return center


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
) -> str:
    """执行一次「点商品 → 确认」。返回 BOUGHT/NO_POPUP/ITEM_MISSING/CONFIRM_GONE_FAIL/STOPPED。

    商品定位优先级：point_frac（固定比例坐标）→ 模板 → OCR 文字兜底。
    point_frac 是给「图标灰度相同、OCR 也认不准」的商品用的（异环两个骰子），
    点完同样要等确认弹窗出现，点空/点错照样会暴露成 NO_POPUP。
    label 只影响日志前缀（信源残响兑换复用本函数，避免日志里出现"商店采购"字样）。
    scroll_finder：当前视图定位不到时的翻页兜底（见 monthly_exchange._scroll_find_item）；
    为 None 时保持旧行为（找不到 = 已买过，不做额外操作）。
    """
    from .foreground_flow_runner import _click_window_local

    if stop_check is not None and stop_check():
        return STOPPED

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

    confirm_center = _locate(hwnd, confirm_template, scales, confidences, CONFIRM_TIMEOUT_SEC, stop_check)
    if confirm_center is None:
        if stop_check is not None and stop_check():
            return STOPPED
        logger.info(f"[{label}] 点击商品后未弹出兑换确认（可能已达上限），结束该商品")
        return NO_POPUP

    _click_window_local(hwnd, confirm_center[0], confirm_center[1], clicks=1)
    time.sleep(SETTLE_SEC)

    if not _wait_gone(hwnd, confirm_template, scales, confidences, CONFIRM_GONE_SEC, stop_check):
        if stop_check is not None and stop_check():
            return STOPPED
        logger.warning(f"[{label}] 已点确认但弹窗未关闭（可能余额不足或界面异常）")
        return CONFIRM_GONE_FAIL
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
                )
                if result == BOUGHT:
                    bought += 1
                    continue
                if result == STOPPED:
                    stopped = True
                    reason = "用户停止"
                    break
                if result == NO_POPUP:
                    reason = "已达上限或无弹窗"
                    break
                if result == ITEM_MISSING:
                    missing_count += 1
                    reason = "页面上没看到该商品，按已购买处理"
                    break
                if result == CONFIRM_GONE_FAIL:
                    reason = "确认后弹窗未关"
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
    capture_shop_evidence(logger, hwnd, flow_label=log_label, stage="采购后", summary=detail)

    if stopped:
        return False, "用户停止 | " + "，".join(summaries)
    # 用户 2026-11-01 要求「记得返回」：买完退回主世界，别停在商店页
    _leave_shop_to_main(logger, hwnd, step, log_label, scales, confidences, stop_check)
    return all_ok, "，".join(summaries)
