"""白荆回廊有猫零售 · 每周「俱乐部商店」全量购买（2026-10-18 视觉版）。

替代旧的固定坐标序列回放（click_sequences/白荆回廊-每周商店购买.json：34 步，
录于 1.0.2.1082，坐标写死只买固定几件，UI 一改版就整段失效；商店每周刷新，
漏买要等下一周）。

流程（起点：主界面，与每日免费礼包共用同一套零售页导航）：
  1. 主界面底栏「有猫零售」（_open_retail_shop）
  2. 左侧功能列「俱乐部商店」
  3. 反复扫描商品网格，找出所有价格按钮（¥数字 / 免费 / 兑换）→ 逐个点开
     → 购买弹窗先点「最大」把数量拉满（+ 号右侧蓝色双箭头）
     → 点「购买」→ 等弹窗关闭 → 重新扫描
     （买过的卡片不再有价格按钮，扫不到新目标即视为全部买完）
  4. 返回主界面

识别策略：
  - 价格按钮不写死坐标，按「文本像价格」+「落在商品网格区域」筛当前屏可见卡片；
    卡片图里的数字（如道具数量 10/40/1000）同样像价格，点开也只是打开该商品的
    购买弹窗，方向上仍然是想买的东西，最坏是空点一次，不会误购。
  - 排除「已购买 / 已售罄 / 剩余0件」等已买标记。
  - 商品名取按钮正上方同列最近的一条非价格文本，仅用于日志与去重签名。
  - 全程不写死绝对坐标，点击坐标一律来自当前帧 OCR 结果。
  - 若商品网格需要翻页/滚动购买，本流程只覆盖当前可见区域，会在日志里提示
    可见目标数，便于判断是否需要补翻页逻辑。

进度记录：由 baijing_flow._maybe_run_weekly 统一写 daily_progress
（task_key=weekly，name=每周商店购买），extra 带 items/visible_targets。
"""

import logging
import re
import time
from typing import Dict, List, Tuple

import pyautogui

from .baijing_daily import (
    SHOP_BUY_FALLBACK,
    SHOP_DIALOG_BUY_REGION,
    SHOP_DIALOG_BUY_TEXTS,
    SHOP_DIALOG_MARKERS,
    SHOP_DIALOG_REGION,
    SHOP_MAX_QTY_FALLBACK,
    _abs_region,
    _capture_daily_list_snapshot,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _leave_retail_shop,
    _ocr_best_match,
    _open_retail_shop,
    _stopped,
    _wait_page_markers,
    _wait_purchase_settled,
)
from .baijing_flow import _focus_game_window, _window_region, detect_baijing_main_ui
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, ocr_window_content

# 左侧功能列「俱乐部商店」（实测左列自上而下：…家具兑换 0.766 / 俱乐部商店 0.834 /
# 自游盒子 0.902）。限定 fy 0.72~0.98 避开「家具兑换」。
CLUB_ENTRY_TEXTS = ("俱乐部商店", "俱乐部")
CLUB_ENTRY_REGION = (0.0, 0.72, 0.20, 0.98)
# 商品网格区域（排除顶栏资源数字与左侧功能列）
CLUB_GRID_REGION = (0.18, 0.18, 1.00, 0.95)
# 商品名相对价格按钮的位置：同列（±CLUB_NAME_DX 窗宽）、上方（CLUB_NAME_DY 窗高内）
CLUB_NAME_DX = 0.06
CLUB_NAME_DY = 0.22
# 名字候选要排除的文本（价格/已购/剩余/弹窗字样都不算商品名）
CLUB_NAME_EXCLUDE = ("已购买", "已售罄", "已领完", "剩余", "免费", "兑换", "购买", "合计", "内容")
# 已买/售罄标记
CLUB_SOLD_OUT_TEXTS = ("已购买", "已售罄", "已领完", "剩余0件", "售罄")
# 价格按钮文案（¥648 / ￥6 / 198 / 免费 / 兑换）
CLUB_FREE_TEXTS = ("免费", "兑换")
CLUB_PRICE_RE = re.compile(r"^[¥￥]?\d{1,6}$")
# 单轮最多尝试的商品数 / 最多重跑几轮（防止布局异常时无限点）
CLUB_MAX_TARGETS = 24
CLUB_MAX_ATTEMPTS = 2
# 商店页快照前缀（落 artifacts/baijing_verify/ 便于核对导航与商品数）
CLUB_SNAPSHOT_PREFIX = "club_store"


def _is_sold_out(tnorm: str) -> bool:
    return any(k in tnorm for k in CLUB_SOLD_OUT_TEXTS)


def _is_price_like(tnorm: str) -> bool:
    return tnorm in CLUB_FREE_TEXTS or bool(CLUB_PRICE_RE.match(tnorm))


def _nearest_item_name(results: List[Dict], center: Tuple[int, int], win_region) -> str:
    """价格按钮正上方同列最近的非价格文本 = 商品名（仅用于日志/去重）。"""
    if not win_region:
        return ""
    left, top, width, height = win_region
    cx, cy = center
    best = ""
    best_y = -1
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
            c = r.get("center")
        except Exception:
            continue
        if conf < 0.5 or not c:
            continue
        x, y = int(c[0]), int(c[1])
        if not (left <= x <= left + width and top <= y <= top + height):
            continue
        if not (cx - CLUB_NAME_DX * width <= x <= cx + CLUB_NAME_DX * width):
            continue
        if not (cy - CLUB_NAME_DY * height <= y <= cy - 0.015 * height):
            continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm or _is_price_like(tnorm) or _is_sold_out(tnorm):
            continue
        if any(k in tnorm for k in CLUB_NAME_EXCLUDE):
            continue
        if y > best_y:
            best, best_y = tnorm, y
    return best


def _collect_buy_targets(logger: logging.Logger, hwnd) -> List[Dict]:
    """扫描当前帧，返回所有「像价格按钮」的商品卡目标（行优先排序）。"""
    results = ocr_window_content(logger, hwnd=hwnd)
    if not results:
        return []
    abs_region = _abs_region(hwnd, CLUB_GRID_REGION)
    win_region = _window_region(hwnd)
    targets: List[Dict] = []
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
            center = r.get("center")
        except Exception:
            continue
        if conf < 0.5 or not center:
            continue
        cx, cy = int(center[0]), int(center[1])
        if abs_region:
            ax0, ay0, aw, ah = abs_region
            if not (ax0 <= cx <= ax0 + aw and ay0 <= cy <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm or _is_sold_out(tnorm) or not _is_price_like(tnorm):
            continue
        targets.append({"text": tnorm, "center": (cx, cy), "conf": conf})
    for t in targets:
        t["name"] = _nearest_item_name(results, t["center"], win_region)
    targets.sort(key=lambda t: (t["center"][1], t["center"][0]))
    for t in targets:
        sig = t["name"] or f"{t['text']}@{t['center'][0] // 60},{t['center'][1] // 60}"
        t["sig"] = sig
        logger.info(
            f"[白荆回廊·俱乐部商店] 候选商品 {t['name'] or '(无名)'} 价格 {t['text']!r} @ {t['center']}"
        )
    return targets


def _buy_one_target(logger: logging.Logger, hwnd, target: Dict, stop_check=None) -> str:
    """点开一个商品 → 弹窗点「最大」拉满数量 → 点「购买」→ 确认到账。

    返回 "purchased"（确认买到）/ "no_dialog"（点到的不是可交互卡片，多半是卡面
    装饰数字）/ "unconfirmed"（弹窗出来了但没确认到账）。
    """
    _pyautogui_click_guarded(*target["center"])
    logger.info(
        f"[白荆回廊·俱乐部商店] 点击 {target['name'] or '(无名)'} 价格按钮 @ {target['center']}"
    )
    if not _wait_page_markers(
        logger,
        hwnd,
        SHOP_DIALOG_MARKERS,
        timeout=6.0,
        stop_check=stop_check,
        frac_region=SHOP_DIALOG_REGION,
    ):
        # 卡片图里的数字等点不中卡片时会走到这里：不算失败，跳过该目标继续
        logger.warning("[白荆回廊·俱乐部商店] 点击后未出现购买弹窗（可能点到卡片图内非交互元素）")
        _dismiss_overlay(logger, hwnd)
        return "no_dialog"
    # 弹窗已出现 → 先把购买数量设为「最大」（+ 号右侧蓝色双箭头图标，无文字可 OCR，
    # 按弹窗固定比例点）。不点这一步每次都只买 1 件（2026-10-20 用户反馈）。
    # 失败也无害：没点中就是数量保持 1，购买照常进行。
    _click_ratio(logger, hwnd, SHOP_MAX_QTY_FALLBACK, "「最大」数量按钮")
    time.sleep(0.4)
    hit = _ocr_best_match(
        logger, SHOP_DIALOG_BUY_TEXTS, hwnd, frac_region=SHOP_DIALOG_BUY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·俱乐部商店] 点击「购买」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_BUY_FALLBACK, "「购买」（比例兜底）")
    state = _wait_purchase_settled(logger, hwnd, timeout=12.0, stop_check=stop_check)
    _dismiss_overlay(logger, hwnd)
    return "purchased" if state == "purchased" else "unconfirmed"


def _open_club_store(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """进零售页 → 左侧「俱乐部商店」。"""
    if not _open_retail_shop(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·俱乐部商店] 未打开零售页")
        return False
    entry = _ocr_best_match(
        logger, CLUB_ENTRY_TEXTS, hwnd, frac_region=CLUB_ENTRY_REGION, min_conf=0.55
    )
    if entry:
        _pyautogui_click_guarded(*entry["center"])
        logger.info(f"[白荆回廊·俱乐部商店] 点击「俱乐部商店」@ {entry['center']}")
    else:
        logger.warning("[白荆回廊·俱乐部商店] 左列未找到「俱乐部商店」")
        return False
    time.sleep(2.0)
    # 「俱乐部商店」这四个字在整个商店页都在（左侧功能列），只能当「已进商店页」的
    # 弱标志；真正确认靠后面能否扫到商品卡片。
    return True


def buy_club_store_all(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """俱乐部商店全量购买（每周刷新）。返回结果字典。

    {"status": success|already|failed|skipped, "purchased": n,
     "items": [商品名...], "visible_targets": 首轮可见目标数, "detail": str}
    """
    result: Dict = {
        "status": "skipped",
        "purchased": 0,
        "items": [],
        "visible_targets": 0,
        "detail": "",
        "attempts": 0,
    }
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=3.0)
    if not is_main and not _stopped(stop_check):
        # 用户叮嘱：不知道页面到哪了先 ESC 退一层再判
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        time.sleep(1.5)
        is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=3.0)
    if not is_main:
        result.update(status="skipped", detail=f"未处于主界面（证据: {evidence}）")
        logger.warning(f"[白荆回廊·俱乐部商店] {result['detail']}，本次跳过")
        return result

    tried = set()
    saw_dialog = False
    for attempt in range(1, CLUB_MAX_ATTEMPTS + 1):
        if _stopped(stop_check):
            result["detail"] = "用户停止"
            return result
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            result.update(status="failed", detail="游戏窗口丢失")
            return result
        result["attempts"] = attempt
        if not _open_club_store(logger, hwnd, stop_check=stop_check):
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            time.sleep(1.5)
            continue
        _capture_daily_list_snapshot(logger, hwnd, prefix=CLUB_SNAPSHOT_PREFIX)
        for step in range(1, CLUB_MAX_TARGETS + 1):
            if _stopped(stop_check):
                result["detail"] = "用户停止"
                return result
            hwnd = _ensure_window(logger, hwnd)
            if not hwnd:
                result.update(status="failed", detail="游戏窗口丢失")
                return result
            targets = _collect_buy_targets(logger, hwnd)
            if attempt == 1 and step == 1:
                result["visible_targets"] = len(targets)
            pending = [t for t in targets if t["sig"] not in tried]
            if not pending:
                break
            target = pending[0]
            tried.add(target["sig"])
            name = target["name"] or target["text"]
            state = _buy_one_target(logger, hwnd, target, stop_check=stop_check)
            if state == "purchased":
                result["purchased"] += 1
                result["items"].append(name)
                logger.info(
                    f"[白荆回廊·俱乐部商店] 已购买 {name}（{result['purchased']} 件）"
                )
            elif state == "no_dialog":
                logger.info(f"[白荆回廊·俱乐部商店] {name} 非可交互卡片，跳过")
            else:
                logger.warning(f"[白荆回廊·俱乐部商店] {name} 未能确认购买，跳过")
            saw_dialog = saw_dialog or state != "no_dialog"
        back_ok = _leave_retail_shop(logger, hwnd, stop_check=stop_check)
        logger.info(
            f"[白荆回廊·俱乐部商店] 第 {attempt}/{CLUB_MAX_ATTEMPTS} 轮结束："
            f"本轮后累计购买 {result['purchased']} 件，回主界面={back_ok}"
        )
        if result["purchased"] or back_ok:
            break
        time.sleep(1.5)

    if result["purchased"]:
        result.update(
            status="success",
            detail=(
                f"已购买 {result['purchased']} 件（首屏可见 {result['visible_targets']} 个目标）："
                + "、".join(result["items"][:12])
            ),
        )
    elif not saw_dialog:
        result.update(status="already", detail="未扫到可购买商品（本周应已买完或页面未加载）")
    else:
        result.update(status="failed", detail="扫到商品但均未确认购买")
    logger.info(f"[白荆回廊·俱乐部商店] 结果 {result['status']}：{result['detail']}")
    return result


def run_club_store_weekly(logger: logging.Logger, hwnd=None, stop_check=None) -> Dict:
    """周常入口包装：供 baijing_flow._maybe_run_weekly 调用（内部自兜异常）。"""
    try:
        return buy_club_store_all(logger, hwnd, stop_check=stop_check)
    except Exception as e:  # noqa: BLE001 - 周常兜底不能把一条龙炸掉
        logger.warning(f"[白荆回廊·俱乐部商店] 视觉购买异常: {e}", exc_info=True)
        return {"status": "failed", "purchased": 0, "items": [], "visible_targets": 0,
                "detail": f"异常: {e}", "attempts": 0}