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
flow 配置里写相对路径「模板\\xxx.png」即可。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..settings.paths import BASE_DIR

LOGGER = logging.getLogger("GameAutomation")

ITEM_TIMEOUT_SEC = 20        # 商品图标匹配超时（轮询）
OCR_TIMEOUT_SEC = 25         # OCR 文字定位超时（轮询，单次识别 1-3s）
CONFIRM_TIMEOUT_SEC = 15     # 点商品后等确认按钮出现
CONFIRM_GONE_SEC = 10        # 点确认后等弹窗关闭
TAB_TIMEOUT_SEC = 20         # 页签匹配超时（切换用）
TAB_SELECTED_TIMEOUT_SEC = 10  # 目标页签选中态确认超时
SETTLE_SEC = 0.8             # 点击后画面稳定等待
TAB_SWITCH_SETTLE_SEC = 2.0  # 页签切换后等待

# 单次购买结果
BOUGHT = "bought"                # 买成功 1 个
NO_POPUP = "no_popup"            # 点商品后无弹窗（大概率已达上限）
ITEM_MISSING = "item_missing"    # 商品图标匹配不到
CONFIRM_GONE_FAIL = "confirm_gone_fail"  # 点了确认但弹窗不关（如余额不足换了提示）
STOPPED = "stopped"              # 用户请求停止


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
) -> Optional[Tuple[int, int]]:
    """轮询匹配模板，命中返回窗口局部中心坐标，超时/停止返回 None。"""
    from .foreground_flow_runner import _focus_window, _locate_template_in_window

    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None
        _focus_window(hwnd)
        center = _locate_template_in_window(hwnd, template, scales, confidences)
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


def _buy_once(
    logger: logging.Logger,
    hwnd: int,
    item: Dict,
    confirm_template: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    label: str = "商店采购",
) -> str:
    """执行一次「点商品 → 确认」。返回 BOUGHT/NO_POPUP/ITEM_MISSING/CONFIRM_GONE_FAIL/STOPPED。

    商品定位优先模板；模板缺失或未命中时，若 item 配了 ocr_text 则用 OCR
    文字定位兜底（点文字中心，商品卡整卡为热区）。两者都无 → ITEM_MISSING。
    label 只影响日志前缀（信源残响兑换复用本函数，避免日志里出现"商店采购"字样）。
    """
    from .foreground_flow_runner import _click_window_local

    if stop_check is not None and stop_check():
        return STOPPED

    item_name = str(item.get("name", "商品") or "商品")
    item_template = _abs_template(str(item.get("template", "") or ""))
    ocr_text = str(item.get("ocr_text", "") or "").strip()
    has_template = bool(item_template) and os.path.isfile(item_template)

    center = None
    if has_template:
        center = _locate(hwnd, item_template, scales, confidences, ITEM_TIMEOUT_SEC, stop_check)
    if center is None and ocr_text:
        if stop_check is not None and stop_check():
            return STOPPED
        way = "OCR文字定位（无模板）" if not has_template else "OCR兜底（模板未命中）"
        logger.info(f"[{label}] {item_name} 改用{way}: {ocr_text}")
        center = _locate_ocr(logger, hwnd, ocr_text, OCR_TIMEOUT_SEC, stop_check)
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
    - shop_groups: [{"name", "tab_template"?, "tab_selected_template"?,
                     "items": [{"name", "template", "count"}]}]

    返回 (是否全部买够, 摘要消息)。
    """
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
                    reason = "商品定位失败（模板/OCR 均未命中）"
                    break
                if result == CONFIRM_GONE_FAIL:
                    reason = "确认后弹窗未关"
                    break

            if bought >= count:
                logger.info(f"[商店采购] {group_name}/{item_name} 完成 {bought}/{count}")
                summaries.append(f"{group_name}/{item_name}: {bought}/{count}")
            else:
                logger.warning(
                    f"[商店采购] {group_name}/{item_name} 未买够: {bought}/{count}（{reason or '未知'}）"
                )
                summaries.append(f"{group_name}/{item_name}: {bought}/{count}（{reason or '未知'}）")
                if reason not in ("已达上限或无弹窗",):
                    # 已达上限是正常业务状态（之前买过），不计失败
                    all_ok = False

    if stopped:
        return False, "用户停止 | " + "，".join(summaries)
    return all_ok, "，".join(summaries)
