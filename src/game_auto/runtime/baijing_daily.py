# -*- coding: utf-8 -*-
"""白荆回廊·日常主流程（零售礼包 + 任务调度 + 视觉版一条龙）。

职责：①有猫零售「每日外勤赠礼」免费礼包领取（含防重复标记）；②
process_task_list 按静态目录调度专项任务（handler 在 baijing_task_handlers）；
③ run_baijing_daily_vision_flow 主入口（主界面→日常→俱乐部→零售→回主界面），
baijing_flow.run_baijing_full_flow 的日常段就是它。

2026-11-14 拆分后本文件只留**主流程胶水**；基础设施/场景恢复/任务列表/
专项 handler/俱乐部与回声猎区分别在 baijing_common / baijing_scene /
baijing_tasklist / baijing_task_handlers / baijing_echo。文件尾部的
re-export 块是对外符号面的**过渡性 facade**（shop/cat_exchange 与测试套件
仍从 baijing_daily import；mock 类测试必须 patch 符号的**归属模块**，
patch daily 已不生效）。
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from ..settings.paths import BAIJING_VERIFY_DIR, TEMPLATES_DIR, WORKFLOW_DB_PATH
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, grab_screen, ocr_window_content
from .baijing_common import (
    MAX_CLOSE_ATTEMPTS,
    _abs_region,
    _capture_daily_list_snapshot,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _ocr_best_match,
    _pick_result,
    _record_step_failure,
    _scene_results,
    _stopped,
    _template_center,
    _wait_page_markers,
)
from .baijing_scene import _ensure_main_ui
from .baijing_tasklist import (
    DAILY_ACTIVITY_TARGET,
    _claim_rewards_midway,
    _ensure_task_list_open,
    _read_activity_number,
    _scroll_row_list_to_top,
    _return_to_task_panel,
    close_task_list,
    open_daily_task_list,
    scan_daily_task_states,
)
from .baijing_task_handlers import (
    _claim_leaf_vein_rewards,
    _handle_friend_medal_task,
    _handle_lab_harvest_task,
    _handle_memory_exp_task,
    _handle_stage_battle_task,
    _handle_tea_break_task,
)
from .baijing_echo import claim_club_daily_reward
from .baijing_flow import (
    BAIJING_GAME_KEY,
    _baijing_today_key,
    _focus_game_window,
    _record_baijing_task,
    _resolve_baijing_window,
    _window_region,
    detect_baijing_main_ui,
)

# ── 有猫零售「每日外勤赠礼」免费礼包（2026-10-18 用户截图实测） ──
# 流程：主界面底栏「有猫零售」→ 左侧「精选礼包」→ 顶部 tab「有猫精选」→
#       首张卡「每日外勤赠礼」点底部「免费」→ 购买弹窗点「购买」→ 关奖励弹窗 → 返回主界面。
# 下方比例/区域全部来自 1919x1079 实测截图 + RapidOCR 探针复核（字串与坐标都对得上）：
#   主界面底栏 有猫零售 (0.712,0.935)｜零售页 返回 (0.102,0.049)｜左列 精选礼包 (0.105,0.217)
#   顶部 tab 有猫精选 (0.670,0.203)（同排还有 活动限时 0.274 / 启航钜惠 0.406 / 里程碑 0.538）
#   首张卡 每日外勤赠礼 (0.269,0.734) / 剩余1件 (0.268,0.769) / 免费 (0.267,0.833)
#   购买弹窗 合计 (0.436,0.626) / 免费 (0.597,0.625) / 购买 (0.606,0.714)
# 注意「免费」在同页有两个：卡片顶部角标 (0.267,0.287，conf 仅 0.54) 与卡底价格按钮
# (0.267,0.833)。只认卡底那个 → 搜索区域限定 fy 0.70~0.90（角标与弹窗里的
# 「免费」(0.597,0.625) 都被排除在外）。
SHOP_ENTRY_TEXTS = ("有猫零售",)


# 主界面底栏区域：夹在「精神深潜」(0.595) 与「活动中心」(0.828) 之间
SHOP_ENTRY_REGION = (0.62, 0.88, 0.80, 1.00)


SHOP_ENTRY_FALLBACK = (0.712, 0.935)


# 零售页特征文字（不用「客服」：主界面左下角有「Q客服」，子串匹配会假命中）
SHOP_PAGE_MARKERS = ("有猫推荐", "精选礼包", "已购礼包自动收起", "X晶体验值")


SHOP_SIDE_TEXTS = ("精选礼包",)


SHOP_SIDE_REGION = (0.0, 0.10, 0.20, 0.95)   # 左侧功能列


SHOP_SIDE_FALLBACK = (0.105, 0.217)


SHOP_TAB_TEXTS = ("有猫精选",)


SHOP_TAB_REGION = (0.20, 0.15, 0.80, 0.26)   # 顶部 tab 行


SHOP_TAB_FALLBACK = (0.670, 0.203)


# 「有猫精选」页首张卡的名字（用它确认 tab 切对了：停在「活动限时」时首卡是「集结共鸣助力」）
SHOP_FREE_PACK_TEXTS = ("每日外勤赠礼",)


# 首张卡区域 + 「免费」按钮搜索区域（含礼包名/剩余/底部价格按钮，不含顶部角标）。
# 整排搜索而非只搜首列：礼包顺序若变化仍能点到卡底价格按钮，fy 与弹窗里的「免费」错开。
SHOP_FREE_BTN_REGION = (0.18, 0.70, 1.00, 0.90)


SHOP_FREE_BTN_TEXTS = ("免费",)


# 已买完时的字样（找不到「免费」时用来区分"已领取"与"没找到"）
SHOP_SOLD_OUT_TEXTS = ("剩余0件", "已售罄", "已购买", "已领完")


# 购买确认弹窗特征（点卡底「免费」后出现）。
# **不要把「购买」当弹窗特征**：买完后卡片按钮变「已购买」，子串匹配会永远命中，
# 弹窗判定永远为"仍在"→ 每次都判失败重试。用弹窗独有的「内容:」「合计」。
SHOP_DIALOG_MARKERS = ("内容", "合计")


SHOP_DIALOG_REGION = (0.15, 0.20, 0.92, 0.80)   # 弹窗覆盖范围


SHOP_DIALOG_BUY_TEXTS = ("购买",)


SHOP_DIALOG_BUY_REGION = (0.35, 0.55, 0.95, 0.85)


SHOP_BUY_FALLBACK = (0.606, 0.707)   # (1162,763)@1919x1079（弹窗截图底部少 11px，按窗口高折算）


# 「最大」购买数量按钮：弹窗内 + 号右侧的蓝色双箭头图标（无文字可 OCR）。
# 2026-10-20 用户反馈——不点它每次都只买 1 件。实测中心 (1368,566)@1919x1079。
# 仅俱乐部商店（baijing_shop）使用；每日免费礼包无数量选择，不要套用。
SHOP_MAX_QTY_FALLBACK = (0.713, 0.524)


# 零售页左上「返回」
SHOP_BACK_REGION = (0.0, 0.0, 0.20, 0.10)


# 打开零售页的最大尝试次数
SHOP_MAX_ATTEMPTS = 2



# 列表内操作最大轮数（每轮最多执行一个动作）
MAX_LIST_ROUNDS = 12




def _daily_gift_meta_key() -> str:
    return f"baijing_daily_gift:{_baijing_today_key()}"




def _daily_gift_marked_done() -> bool:
    """今日是否已经成功领过免费礼包（task_meta 标记，一天一次，后写不覆盖）。"""
    try:
        from ..storage.workflow_db import WorkflowDB

        return WorkflowDB(str(WORKFLOW_DB_PATH)).get_task_meta(_daily_gift_meta_key()) == "ok"
    except Exception:
        return False




def _mark_daily_gift_done(logger: logging.Logger) -> None:
    try:
        from ..storage.workflow_db import WorkflowDB

        WorkflowDB(str(WORKFLOW_DB_PATH)).set_task_meta(_daily_gift_meta_key(), "ok")
    except Exception as e:
        logger.debug(f"[白荆回廊·日常] 写入每日礼包 meta 失败（已忽略）: {e}")




def _open_retail_shop(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """主界面底栏点「有猫零售」进入零售页（OCR 优先，比例兜底）。"""
    _focus_game_window(logger, hwnd)
    hit = _ocr_best_match(
        logger, SHOP_ENTRY_TEXTS, hwnd, frac_region=SHOP_ENTRY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「有猫零售」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_ENTRY_FALLBACK, "「有猫零售」（比例兜底）")
    return _wait_page_markers(logger, hwnd, SHOP_PAGE_MARKERS, timeout=8.0, stop_check=stop_check)




def _select_featured_gift_tab(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """左侧「精选礼包」→ 顶部「有猫精选」tab，确认首张卡是「每日外勤赠礼」。"""
    side = _ocr_best_match(
        logger, SHOP_SIDE_TEXTS, hwnd, frac_region=SHOP_SIDE_REGION, min_conf=0.55
    )
    if side:
        _pyautogui_click_guarded(*side["center"])
        logger.info(f"[白荆回廊·日常] 点击左侧「精选礼包」@ {side['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_SIDE_FALLBACK, "左侧「精选礼包」（比例兜底）")
    time.sleep(1.5)
    tab = _ocr_best_match(
        logger, SHOP_TAB_TEXTS, hwnd, frac_region=SHOP_TAB_REGION, min_conf=0.55
    )
    if tab:
        _pyautogui_click_guarded(*tab["center"])
        logger.info(f"[白荆回廊·日常] 点击顶部「有猫精选」tab @ {tab['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_TAB_FALLBACK, "「有猫精选」tab（比例兜底）")
    time.sleep(1.2)
    # 确认切到了含「每日外勤赠礼」的那一页（停在「活动限时」时首卡是「集结共鸣助力」）
    if not _wait_page_markers(logger, hwnd, SHOP_FREE_PACK_TEXTS, timeout=6.0, stop_check=stop_check):
        logger.warning("[白荆回廊·日常] 有猫精选页未出现「每日外勤赠礼」")
        return False
    return True




def _buy_in_dialog(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """购买确认弹窗点「购买」（OCR 优先，比例兜底）。"""
    hit = _ocr_best_match(
        logger, SHOP_DIALOG_BUY_TEXTS, hwnd, frac_region=SHOP_DIALOG_BUY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·日常] 点击「购买」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_BUY_FALLBACK, "「购买」（比例兜底）")
    return True




def _wait_purchase_settled(logger: logging.Logger, hwnd, timeout: float = 12.0, stop_check=None) -> str:
    """点完「购买」后确认到账。

    先等购买弹窗消失（期间不做任何点击，避免误关弹窗把购买打断），
    弹窗消失后再关奖励弹窗并查卡片是否显示已买完。
    返回 "purchased" / "unknown"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return "unknown"
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return "unknown"
        if _ocr_best_match(
            logger, SHOP_DIALOG_MARKERS, hwnd, min_conf=0.55, frac_region=SHOP_DIALOG_REGION
        ):
            time.sleep(1.0)
            continue
        # 弹窗已消失 → 关掉可能弹出的奖励弹窗，再看卡片状态
        _dismiss_overlay(logger, hwnd)
        sold = _ocr_best_match(logger, SHOP_SOLD_OUT_TEXTS, hwnd, min_conf=0.5)
        if sold:
            logger.info(f"[白荆回廊·日常] 卡片显示 {sold['text']!r}，确认购买成功")
        else:
            logger.info("[白荆回廊·日常] 购买弹窗已关闭，确认购买完成")
        return "purchased"
    logger.warning("[白荆回廊·日常] 点「购买」后弹窗仍在，未确认到账")
    return "unknown"




def _leave_retail_shop(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """零售页返回主界面（左上「返回」→ ESC），返回是否已回主界面。"""
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 购买成功可能弹出奖励弹窗，先清一次再判断是否已回主界面
        _dismiss_overlay(logger, hwnd)
        ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
        if ok:
            logger.info("[白荆回廊·日常] 已从零售页回到主界面")
            return True
        back = _ocr_best_match(logger, ("返回",), hwnd, frac_region=SHOP_BACK_REGION, min_conf=0.55)
        if back:
            _pyautogui_click_guarded(*back["center"])
            logger.info(f"[白荆回廊·日常] 点击零售页「返回」@ {back['center']}")
        else:
            _focus_game_window(logger, hwnd)
            pyautogui.press("escape")
            logger.info(f"[白荆回廊·日常] 按 ESC 退出零售页 (#{attempt}/{MAX_CLOSE_ATTEMPTS})")
        time.sleep(1.5)
    ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
    if not ok:
        logger.warning("[白荆回廊·日常] 多次尝试后仍未从零售页回到主界面")
    return ok




def buy_daily_free_gift_pack(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """有猫零售领取每日免费礼包「每日外勤赠礼」（2026-10-18 用户实测流程）。

    主界面 → 底栏「有猫零售」→ 左侧「精选礼包」→ 顶部「有猫精选」→ 首张卡
    「每日外勤赠礼」卡底「免费」→ 购买弹窗「购买」→ 关奖励弹窗 → 返回主界面。

    返回 {"status", "purchased", "detail"}：
      purchased=True  当日买到（免费礼包每天刷新，错过要等 8 小时刷新）
      status="already" 今日已领（卡片已无「免费」按钮，显示已售罄/剩余0件）
      status="failed"  / "skipped" 见 detail
    """
    result = {"status": "skipped", "purchased": False, "detail": "", "attempts": 0}
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    # 每日礼包一天只能领一次：强制重跑/同一天第二次执行时，商品已从货架消失，
    # 继续导航只会得到"未找到免费按钮"并误报失败（2026-10-25 17:49 实锤）。
    # 以今日 daily_progress 的成功记录为准，直接记 already。
    prev = _baijing_task_status_today("daily_gift")
    if prev in ("success", "already") or _daily_gift_marked_done():
        result.update(status="already", detail="今日已领取（进度记录/meta）")
        logger.info(f"[白荆回廊·日常] {result['detail']}，跳过零售页")
        return result
    # 2026-10-22 修复：不再依赖主流程 close_task_list 的 back_ok 门禁
    # （10-21 实锤：close_task_list 被引导气泡带偏判失败，零售/俱乐部被整体跳过，
    # 连记录都没落）。这里自己确保回到主界面，失败才 skipped 且 detail 留痕。
    if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过零售")
        logger.info(f"[白荆回廊·日常] {result['detail']}")
        return result

    for attempt in range(1, SHOP_MAX_ATTEMPTS + 1):
        if _stopped(stop_check):
            result["detail"] = "用户停止"
            return result
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            result.update(status="failed", detail="游戏窗口丢失")
            return result
        result["attempts"] = attempt
        # 每轮开始都确保在主界面（上一轮 _leave_retail_shop 可能没退干净）
        if attempt > 1 and not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
            result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过零售")
            return result
        # 1) 进零售页
        if not _open_retail_shop(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次：未打开零售页")
            time.sleep(1.5)
            continue
        # 2) 精选礼包 → 有猫精选 tab
        if not _select_featured_gift_tab(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次：未切到有猫精选页")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            time.sleep(1.5)
            continue
        # 3) 点卡底「免费」（区域限定价格行，排掉卡片顶部角标与弹窗里的同名文字）
        btn = _ocr_best_match(
            logger, SHOP_FREE_BTN_TEXTS, hwnd, frac_region=SHOP_FREE_BTN_REGION, min_conf=0.5
        )
        if not btn:
            sold = _ocr_best_match(logger, SHOP_SOLD_OUT_TEXTS, hwnd, min_conf=0.5)
            if sold:
                result.update(status="already", detail=f"今日已领取（{sold['text']}）")
                _mark_daily_gift_done(logger)
            else:
                result.update(status="failed", detail="未找到「免费」按钮且无已领标记")
            logger.info(f"[白荆回廊·日常] 跳过购买：{result['detail']}")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            return result
        _pyautogui_click_guarded(*btn["center"])
        logger.info(f"[白荆回廊·日常] 点击「每日外勤赠礼」卡底「免费」@ {btn['center']}")
        # 4) 购买弹窗 → 点「购买」
        if not _wait_page_markers(
            logger,
            hwnd,
            SHOP_DIALOG_MARKERS,
            timeout=6.0,
            stop_check=stop_check,
            frac_region=SHOP_DIALOG_REGION,
        ):
            logger.warning("[白荆回廊·日常] 点「免费」后未出现购买弹窗")
            _dismiss_overlay(logger, hwnd)
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            time.sleep(1.5)
            continue
        _buy_in_dialog(logger, hwnd, stop_check=stop_check)
        state = _wait_purchase_settled(logger, hwnd, timeout=12.0, stop_check=stop_check)
        _dismiss_overlay(logger, hwnd)
        if state == "purchased":
            result.update(
                status="success", purchased=True, detail="已领取「每日外勤赠礼」免费礼包"
            )
            _mark_daily_gift_done(logger)
            logger.info(f"[白荆回廊·日常] {result['detail']}")
            _leave_retail_shop(logger, hwnd, stop_check=stop_check)
            return result
        logger.warning(f"[白荆回廊·日常] 第 {attempt}/{SHOP_MAX_ATTEMPTS} 次购买未确认到账，重试")
        _dismiss_overlay(logger, hwnd)
        _leave_retail_shop(logger, hwnd, stop_check=stop_check)
        time.sleep(1.5)
    result.update(status="failed", detail="多次尝试后仍未完成购买")
    return result




def process_task_list(
    logger: logging.Logger, hwnd, stop_check=None, max_rounds: int = MAX_LIST_ROUNDS
) -> Dict:
    """任务列表内操作：先滚动扫描全表拿任务状态，多个「前往」类任务自动执行，再一键/逐个领取。

    已实测的「前往」流程（2026-10-18 用户截图）：
    - 研发室：点行内「前往」→「白荆穹顶」弹窗 →「一键收取」→ 点 X/空白关闭回列表；
    - 记忆烙痕经验：点行内「前往」→ 烙痕列表页点首个卡片 → 详情页「强化烙痕」→
      重置 → +1 → 确认 → 动画点击屏幕 → 逐层返回任务面板；
    - 午后茶憩：点行内「前往」→「是否前往茶憩?」确定 → 角色选择页点首个角色 →
      场景内喝茶 → 等待后再来一杯 → 摘要随意点击 → 休息室返回 → 确认 → 主界面；
    - 关卡战斗类（显影行动/自由贸易/定向物资保障/光刻协议，操作相同）：
      点行内「前往」→ 关卡页「快速战斗」→ 设置弹窗再点「快速战斗」→ 结算页随意
      点击 → ESC 逐层返回。
    以上任务完成后对应「领取奖励」均由下方领取循环处理；每个任务完成后还会先做
    一次中途「一键领取」（固定底栏、无需滚动，用户 2026-10-18 建议：避免已完成
    奖励堆积把后面的任务行挤到可视区外）。
    其余「前往」类任务（回廊漫巡/战斗/友情勋章等）会跳转到不同玩法，流程未实测，
    v1 只记录不点击，待按活动类型分别实测后实现。
    """
    # 1) 滚动扫描全表（15 个任务的状态 + 活跃度核算基础）
    states = scan_daily_task_states(
        logger, hwnd, stop_check=stop_check, capture_prefix="daily_scan"
    )
    summary = {
        "claim_all": 0,
        "claim": 0,
        "goto_seen": 0,
        "rounds": 0,
        "scanned": len(states),
        "tasks_total": len(DAILY_TASK_CATALOG),
    }
    # 2) 研发室「前往」任务：流程已知（白荆穹顶弹窗一键收取），自动执行；
    #    收取后该任务应出现「领取奖励」，交给下面的领取循环
    lab_state = states.get("lab_harvest")
    if lab_state and not lab_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过研发室任务")
        elif _handle_lab_harvest_task(logger, hwnd, stop_check=stop_check):
            lab_state["done"] = True
            lab_state["progress"] = "1/1"
            lab_state["claimable"] = True
            logger.info("[白荆回廊·日常] 研发室任务已收取，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 3) 记忆烙痕经验「前往」任务：流程已知（任选烙痕→强化烙痕→重置→+1→确认），
    #    强化后该任务应出现「领取奖励」，交给下面的领取循环
    mem_state = states.get("memory_exp")
    if mem_state and not mem_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过记忆烙痕任务")
        elif _handle_memory_exp_task(logger, hwnd, stop_check=stop_check):
            mem_state["done"] = True
            mem_state["progress"] = "1/1"
            mem_state["claimable"] = True
            logger.info("[白荆回廊·日常] 记忆烙痕经验任务已强化，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 4) 午后茶憩「前往」任务：流程已知（选角色→喝茶→再来一杯→摘要→返回确定），
    #    茶憩后该任务应出现「领取奖励」，交给下面的领取循环
    tea_state = states.get("tea_break")
    if tea_state and not tea_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过午后茶憩任务")
        elif _handle_tea_break_task(logger, hwnd, stop_check=stop_check):
            # 不乐观写 done：茶憩返回 True 只代表流程走完（能回到面板），不代表
            # 游戏内任务真的 1/1（2026-10-25 19:06 假成功实锤：本地记成功、
            # 游戏内仍 0/1）。任务状态交给下面的最终重扫，重扫失败就保守判未完成。
            logger.info("[白荆回廊·日常] 午后茶憩流程已执行，任务状态以重扫为准")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 5) 友情勋章「前往」任务：优先点行内「前往」，未进好友页则回主界面点左上角
    #    第 2 个功能图标（好友）→「一键领取/赠送」→ 处理弹窗 → 重开任务列表
    friend_state = states.get("friend_medal")
    if friend_state and not friend_state["done"] and not _stopped(stop_check):
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 任务列表丢失且重开失败，跳过友情勋章任务")
        elif _handle_friend_medal_task(logger, hwnd, stop_check=stop_check):
            # 友情勋章的“成功”只能靠任务行 1/1 或「领取奖励」确认，弹窗可能是错误提示；
            # 这里不手动改 done，交给处理完的重扫判定，避免把失败提示当成功。
            logger.info("[白荆回廊·日常] 友情勋章流程已执行，任务状态以重扫为准")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 6) 关卡战斗类「前往」任务（显影行动/自由贸易/定向物资保障/光刻协议，
    #    操作相同：快速战斗×2 → 结算随意点 → ESC 返回），逐个处理；
    #    战斗后各任务应出现「领取奖励」，交给下面的领取循环
    # 「完成5次战斗」需要 5 场：四个关卡任务各 1 场只有 4 场，给本批第一个
    # 实际执行的关卡加一次指定次数（2），正好凑满 5（2026-10-25 用户指定）。
    double_battle_pending = True
    for task_key, row_keyword, task_label in STAGE_BATTLE_TASKS:
        if _stopped(stop_check):
            break
        st = states.get(task_key)
        if not st or st["done"]:
            continue
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning(f"[白荆回廊·日常] 任务列表丢失且重开失败，跳过{task_label}任务")
            break
        battle_count = 2 if double_battle_pending else 1
        if battle_count == 2:
            logger.info(f"[白荆回廊·日常] {task_label}使用指定次数=2（凑满完成5次战斗）")
        if _handle_stage_battle_task(
            logger, hwnd, row_keyword, task_label,
            battle_count=battle_count, stop_check=stop_check,
        ):
            double_battle_pending = False
            st["done"] = True
            st["progress"] = "1/1"
            st["claimable"] = True
            logger.info(f"[白荆回廊·日常] {task_label}任务已完成，等待领取奖励")
            summary["claim_all"] += _claim_rewards_midway(logger, hwnd, stop_check=stop_check)
    # 7) 领取（一键领取在固定底栏，一次点完所有已完成任务）
    for rnd in range(1, max_rounds + 1):
        if _stopped(stop_check):
            break
        summary["rounds"] = rnd
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            logger.error("[白荆回廊·日常] 游戏窗口丢失，中断列表操作")
            break
        # 0) 先关掉可能遮挡列表的弹窗（领取奖励后的弹窗等）
        _dismiss_overlay(logger, hwnd)
        # 0.5) 确认列表还在屏（不在则重开；10-21 空转根因修复）
        if not _ensure_task_list_open(logger, hwnd, stop_check=stop_check):
            logger.warning("[白荆回廊·日常] 领取阶段任务列表丢失且重开失败，结束列表操作")
            break
        # 1) 一键领取
        hit = _ocr_best_match(logger, CLAIM_ALL_TEXTS, hwnd, min_conf=0.6)
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            summary["claim_all"] += 1
            logger.info(f"[白荆回廊·日常] 点击「一键领取」@ {hit['center']}")
            time.sleep(1.5)
            continue
        # 2) 单个「领取」（排除「一键/全部」字样，避免重复计数）
        hit = _ocr_best_match(logger, CLAIM_TEXTS, hwnd, min_conf=0.6, exclude=CLAIM_EXCLUDE)
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            summary["claim"] += 1
            logger.info(f"[白荆回廊·日常] 点击「领取」@ {hit['center']}")
            time.sleep(1.2)
            continue
        # 3) 「前往」类未完成任务：只记录不点击（研发室/记忆烙痕/午后茶憩/显影行动已单独处理，
        #    其余前往流程未实测）
        goto_hit = _ocr_best_match(logger, GOTO_TEXTS, hwnd, min_conf=0.6)
        if goto_hit:
            summary["goto_seen"] += 1
            logger.info(
                f"[白荆回廊·日常] 检测到「前往」类未完成任务 @ {goto_hit['center']}，"
                f"暂不自动跳转（需按活动类型分别实现）"
            )
        break
    # 8) 处理完成后重新扫描一次，拿到战斗/茶憩等顺带推进的真实状态。
    #    原实现只用手动标记的 states 算活跃度，「完成2次战斗」「击败60」这类
    #    被战斗顺带推进的任务永远不会计入。重扫失败只告警，不影响主流程。
    initial_done = sum(1 for s in states.values() if s.get("done"))
    if not _stopped(stop_check) and initial_done < len(DAILY_TASK_CATALOG):
        hwnd = _ensure_window(logger, hwnd)
        if hwnd:
            # 顺序必须是 先关弹窗 → 再确认列表在屏：2026-10-25 实锤原顺序
            # （先 ensure 后 dismiss）下 dismiss 一旦动到面板，紧接着的重扫就
            # 对着非列表界面扫 6 屏全 0 行。先 dismiss 再 ensure 可以自愈。
            _dismiss_overlay(logger, hwnd)
            if _ensure_task_list_open(logger, hwnd, stop_check=stop_check) and _scroll_row_list_to_top(logger, hwnd):
                logger.info("[白荆回廊·日常] 处理完成后重新扫描任务状态…")
                try:
                    final_states = scan_daily_task_states(
                        logger,
                        hwnd,
                        stop_check=stop_check,
                        capture_prefix="daily_scan_final",
                    )
                except Exception as e:
                    final_states = {}
                    logger.warning(f"[白荆回廊·日常] 重新扫描任务状态失败（已忽略）: {e}")
                merged = 0
                for key, st in final_states.items():
                    old = states.get(key)
                    if old is None:
                        states[key] = st
                        merged += 1
                        continue
                    # 任一来源判定完成都保留，避免 OCR 波动抹掉处理器已确认的结果
                    old["done"] = bool(old.get("done") or st.get("done"))
                    old["claimable"] = bool(old.get("claimable") or st.get("claimable"))
                    if st.get("progress") and st["progress"] != "0/1":
                        old["progress"] = st["progress"]
                logger.info(
                    f"[白荆回廊·日常] 重新扫描完成: {len(final_states)}/{len(DAILY_TASK_CATALOG)} 项，"
                    f"补入新状态 {merged} 项"
                )
    # 9) 活跃度核算：优先读游戏面板顶部的「累计活跃度」大数字（权威值），
    #    读不到才退回静态目录核算。2026-10-27 用户实锤：目录算出 90，游戏面板
    #    105 且五档奖励全领；目录会随版本漂移（当天任务是「完成2次战斗」，
    #    目录写「完成3次战斗」），不能再用目录和当唯一口径。
    #    activity_earned=原始实得（可超 100）；activity_total=领满线 100；
    #    activity_max=理论总分（面板值更高时跟随面板）。
    done_keys = [k for k, s in states.items() if s.get("done")]
    catalog_earned = sum(
        DAILY_ACTIVITY_BY_KEY[k] for k in done_keys if DAILY_ACTIVITY_BY_KEY.get(k)
    )
    game_activity = _read_activity_number(logger, hwnd)
    if game_activity is not None:
        earned = game_activity
        summary["activity_source"] = "game_panel"
        logger.info(
            f"[白荆回廊·日常] 活跃度口径: 面板 {game_activity}，"
            f"目录核算 {catalog_earned} → 以面板为准"
        )
    else:
        earned = catalog_earned
        summary["activity_source"] = "catalog"
        logger.warning(
            f"[白荆回廊·日常] 面板活跃度读取失败，退回目录核算 {catalog_earned}"
        )
    summary["activity_catalog"] = catalog_earned
    summary["activity_earned"] = earned
    summary["activity_total"] = DAILY_ACTIVITY_TARGET
    summary["activity_max"] = max(DAILY_ACTIVITY_TOTAL, earned)
    summary["scanned"] = len(states)
    summary["done"] = len(done_keys)
    # states 一并带回调用方：入口要做活跃度记录（历史上入口直接引用 states
    # 会 NameError，把日常记录整段炸掉）
    summary["states"] = states
    logger.info(
        f"[白荆回廊·日常] 列表操作结束: 一键领取x{summary['claim_all']} "
        f"逐个领取x{summary['claim']} 前往类x{summary['goto_seen']} "
        f"任务完成 {summary['done']}/{summary['tasks_total']} "
        f"活跃度 {earned}/{summary['activity_max']}"
        f"（来源 {summary['activity_source']}，目录核算 {summary['activity_catalog']}，"
        f"领满线 {DAILY_ACTIVITY_TARGET}） (共 {summary['rounds']} 轮)"
    )
    return summary




def run_baijing_daily_vision_flow(
    logger: logging.Logger, hwnd=None, stop_check=None
) -> bool:
    """白荆回廊视觉版日常流程入口（由 run_baijing_full_flow 调用）。

    主界面 → 点击「任务」→ 日常任务列表 → 领取 → 关闭列表回主界面 →
    有猫零售领「每日外勤赠礼」免费礼包。
    返回 True 表示成功打开并处理了任务列表（领没领到奖励都算成功，
    具体数量见 daily_progress extra.summary，零售结果见 extra.free_gift_pack）。
    """
    logger.info(
        "=" * 50 + "\n[白荆回廊·日常] 视觉版日常流程开始（主界面 → 任务 → 日常任务列表）\n" + "=" * 50
    )

    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        logger.error("[白荆回廊·日常] 未找到游戏窗口")
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未找到游戏窗口")
        return False

    pyautogui.PAUSE = 0.1
    _focus_game_window(logger, hwnd)
    time.sleep(0.8)

    # 1) 确认主界面（未识别到则 ESC 恢复一次再判）
    is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=4.0)
    if not is_main and not _stopped(stop_check):
        logger.info("[白荆回廊·日常] 未识别到主界面，尝试 ESC 恢复后重判")
        pyautogui.press("escape")
        time.sleep(1.5)
        is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=4.0)
    if not is_main:
        logger.error(f"[白荆回廊·日常] 未处于主界面，放弃本次日常（证据: {evidence}）")
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未处于主界面")
        return False
    logger.info("[白荆回廊·日常] 已确认处于主界面")

    # 2) 打开日常任务列表
    if not open_daily_task_list(logger, hwnd, stop_check=stop_check):
        if _stopped(stop_check):
            _record_baijing_task(logger, "daily", "日常任务", "skipped", "用户停止")
            return False
        _record_baijing_task(logger, "daily", "日常任务", "failed", "未能打开日常任务列表")
        return False

    # 3) 列表内领取（日常 tab：扫描 + 前往类任务 + 一键/逐个领取）
    summary = process_task_list(logger, hwnd, stop_check=stop_check)

    # 3.5) 叶脉联结领取（日常完毕后：任务总览 → 全部领取；奖励与日常 tab 不互通）
    leaf_ok = False
    if not _stopped(stop_check):
        leaf_ok = _claim_leaf_vein_rewards(logger, hwnd, stop_check=stop_check)
        if not leaf_ok:
            logger.warning("[白荆回廊·日常] 叶脉联结领取未完成（不影响日常结果记录）")
            # 2026-10-25 用户拍板：叶脉失败此前只打 warning，flow_failures 查不到，
            # 漏领无迹可寻。补结构化埋点（截图+当帧文字），与俱乐部/礼包同口径。
            _record_step_failure(logger, hwnd, "叶脉联结领取", "未进入叶脉联结页或领取失败")

    # 4) 关闭列表，回主界面。back_ok 仅作记录，不再门禁后续步骤：
    #    2026-10-22 起俱乐部/零售各自 _ensure_main_ui 自保，close 失败不再连坐跳过
    #    （10-21 实锤：close 被引导气泡带偏 → 俱乐部+免费礼包整体跳过且无记录）。
    back_ok = close_task_list(logger, hwnd, stop_check=stop_check)
    if not back_ok and not _stopped(stop_check):
        logger.warning("[白荆回廊·日常] 关闭列表后未确认回主界面，后续步骤将自行恢复")

    # 4.5) 俱乐部每日任务奖励（活跃度 100 里程碑领完之后顺手领：主界面左上第 4 个
    #      图标进俱乐部 → 空白继续 → 点「俱乐部每日任务」卡片领取）
    club = {"status": "skipped", "detail": "未执行"}
    if not _stopped(stop_check):
        club = claim_club_daily_reward(logger, hwnd, stop_check=stop_check)
    else:
        club["detail"] = "用户停止"
    _record_baijing_task(
        logger,
        "club_daily",
        "俱乐部每日任务",
        club["status"],
        club["detail"],
        extra=dict(club),
    )
    if club["status"] == "failed":
        _record_step_failure(logger, hwnd, "俱乐部每日任务", club["detail"])

    # 5) 有猫零售每日免费礼包（主界面底栏「有猫零售」→ 精选礼包 → 有猫精选 →
    #    「每日外勤赠礼」卡底「免费」→ 购买弹窗「购买」）。免费礼包每日刷新、
    #    8 小时重置，错过要等刷新，所以放在日常尾部独立记录一项；
    #    skipped 也记录（10-21 跳过无记录，用户看不到漏买）。
    gift = {"status": "skipped", "purchased": False, "detail": "未执行", "attempts": 0}
    if not _stopped(stop_check):
        gift = buy_daily_free_gift_pack(logger, hwnd, stop_check=stop_check)
    else:
        gift["detail"] = "用户停止"
    if gift["status"] == "failed":
        _record_step_failure(logger, hwnd, "每日外勤赠礼", gift["detail"])
    _record_baijing_task(
        logger,
        "daily_gift",
        "每日外勤赠礼",
        gift["status"],
        gift["detail"],
        extra=gift,
    )

    if _stopped(stop_check):
        _record_baijing_task(logger, "daily", "日常任务", "skipped", "用户停止")
        return False

    states = summary.get("states") or {}
    # states 提到 extra 顶层，summary 里剔掉，避免同一份任务状态在 JSON 里存两遍
    summary_view = {k: v for k, v in summary.items() if k != "states"}
    extra = {
        "summary": summary_view,
        "states": states,
        "returned_main": back_ok,
        "leaf_vein_claimed": leaf_ok,
        "club_daily": club,
        "free_gift_pack": gift,
        "engine": "vision",
    }
    msg = (
        f"一键领取x{summary['claim_all']} 逐个领取x{summary['claim']} "
        f"前往类x{summary['goto_seen']}"
    )
    done = summary["done"]
    capped = min(summary["activity_earned"], DAILY_ACTIVITY_TARGET)
    # 状态仍保持 success（避免触发整条任务重跑），但消息必须如实反映完成度：
    # 以前只按“有没有点过领取”写“已完成”，出现 2/15、10/100 也报成功的情况。
    if done >= summary["tasks_total"] or capped >= DAILY_ACTIVITY_TARGET:
        status, message = "success", (
            f"已完成（活跃度 {capped}/{DAILY_ACTIVITY_TARGET}，"
            f"任务 {done}/{summary['tasks_total']}；{msg}）"
        )
    elif done > 0 or summary["activity_earned"] > 0:
        status, message = "success", (
            f"部分完成（活跃度 {capped}/{DAILY_ACTIVITY_TARGET}，"
            f"任务 {done}/{summary['tasks_total']}；{msg}）"
        )
    else:
        status, message = "success", f"未完成任何日常任务（活跃度 0/{DAILY_ACTIVITY_TARGET}；{msg}）"
    _record_baijing_task(logger, "daily", "日常任务", status, message, extra=extra)
    # 活跃度计数任务（与 wuwa「今日活跃」同口径：extra.current/total 供
    # daily_progress_tracker.real_progress_percent 计算真实进度百分比）。
    # current 按领满线 100 封顶（实得可超 100，原始值保留在 extra.earned）；
    # 满 100 即日常里程碑奖励全领完（用户 2026-10-18 确认）。
    if states:
        earned = summary["activity_earned"]
        capped = min(earned, DAILY_ACTIVITY_TARGET)
        _record_baijing_task(
            logger,
            "activity",
            "活跃度",
            "success",
            f"活跃度 {capped}/{DAILY_ACTIVITY_TARGET}"
            f"（实得 {earned}/{summary['activity_max']}），"
            f"任务完成 {summary['done']}/{summary['tasks_total']}",
            extra={
                "current": capped,
                "total": DAILY_ACTIVITY_TARGET,
                "earned": earned,
                "max": summary["activity_max"],
                "done": summary["done"],
                "tasks_total": summary["tasks_total"],
            },
        )
    logger.info(
        f"[白荆回廊·日常] 视觉版日常流程完成: {message} "
        f"活跃度 {summary['activity_earned']}/{summary['activity_max']}"
        f"（领满线 {DAILY_ACTIVITY_TARGET}） "
        f"叶脉联结领取={leaf_ok} 免费礼包={gift['status']}({gift['detail']}) "
        f"回主界面={back_ok}"
    )
    return True




# ══ 对外符号面 facade（过渡性） ══
# 2026-11-14 拆分：符号已迁往各专模块；这里全量 re-export 保持
# baijing_daily.X 引用面不变（shop/cat_exchange/多数测试套件零改动）。
# 注意：mock 必须打在符号归属模块上，patch baijing_daily 不再生效。

from .baijing_common import (  # noqa: F401
    DAILY_LIST_SNAPSHOT_MAX_WIDTH as DAILY_LIST_SNAPSHOT_MAX_WIDTH,
    DAILY_LIST_SNAPSHOT_QUALITY as DAILY_LIST_SNAPSHOT_QUALITY,
    MAX_OPEN_ATTEMPTS as MAX_OPEN_ATTEMPTS,
    SCROLL_CURSOR_DWELL as SCROLL_CURSOR_DWELL,
    SCROLL_NOTCH_PAUSE as SCROLL_NOTCH_PAUSE,
    SCROLL_POST_DELAY as SCROLL_POST_DELAY,
    WHEEL_NOTCH as WHEEL_NOTCH,
    _baijing_task_status_today as _baijing_task_status_today,
    _cursor_pos as _cursor_pos,
    _in_host_panel_close_zone as _in_host_panel_close_zone,
    _pixel_is_blue as _pixel_is_blue,
    _ratio_point as _ratio_point,
    _row_region_signature as _row_region_signature,
    _scroll_list_at as _scroll_list_at,
    _scroll_list_with_verify as _scroll_list_with_verify,
    _set_cursor_pos as _set_cursor_pos,
    _wheel_post as _wheel_post,
    _wheel_real as _wheel_real,
    _window_valid as _window_valid,
)

from .baijing_scene import (  # noqa: F401
    CAT_RETAIL_MARKERS as CAT_RETAIL_MARKERS,
    EVENT_ANNOUNCE_BLANK as EVENT_ANNOUNCE_BLANK,
    EVENT_ANNOUNCE_MARKERS as EVENT_ANNOUNCE_MARKERS,
    EVENT_DIALOG_CLAIM_MARKERS as EVENT_DIALOG_CLAIM_MARKERS,
    EVENT_DIALOG_CLOSE as EVENT_DIALOG_CLOSE,
    EVENT_DIALOG_MARKERS as EVENT_DIALOG_MARKERS,
    FOCUS_MODE_MARKERS as FOCUS_MODE_MARKERS,
    FOCUS_MODE_REGION as FOCUS_MODE_REGION,
    FRIEND_SCENE_MARKERS as FRIEND_SCENE_MARKERS,
    LEVEL_UP_MARKERS as LEVEL_UP_MARKERS,
    LOADING_SCREEN_MARKERS as LOADING_SCREEN_MARKERS,
    LOADING_WAIT_TIMEOUT_SEC as LOADING_WAIT_TIMEOUT_SEC,
    MEMORY_DETAIL_MARKERS as MEMORY_DETAIL_MARKERS,
    MEMORY_LIST_MARKERS as MEMORY_LIST_MARKERS,
    REST_ROOM_BACK as REST_ROOM_BACK,
    REST_ROOM_BACK_REGION as REST_ROOM_BACK_REGION,
    REST_ROOM_LEAVE_CONFIRM as REST_ROOM_LEAVE_CONFIRM,
    REST_ROOM_MARKERS as REST_ROOM_MARKERS,
    SCENE_OCR_MIN_CONF as SCENE_OCR_MIN_CONF,
    SCENE_RECOVERY_ROUNDS as SCENE_RECOVERY_ROUNDS,
    SCENE_RULES as SCENE_RULES,
    SCENE_UNKNOWN_ROUNDS as SCENE_UNKNOWN_ROUNDS,
    TEA_ROOM_REGISTRY_MARKERS as TEA_ROOM_REGISTRY_MARKERS,
    TEA_SUMMARY_MARKERS as TEA_SUMMARY_MARKERS,
    _SCENE_LOGGED as _SCENE_LOGGED,
    _SCENE_LOG_INTERVAL_SEC as _SCENE_LOG_INTERVAL_SEC,
    _exit_via_top_left_back as _exit_via_top_left_back,
    _friend_page_hit as _friend_page_hit,
    _in_rest_room as _in_rest_room,
    _loading_screen_hit as _loading_screen_hit,
    _match_scene_rule as _match_scene_rule,
    _on_memory_detail_page as _on_memory_detail_page,
    _record_unknown_scene as _record_unknown_scene,
    _scene_exit_cat_retail as _scene_exit_cat_retail,
    _scene_exit_event_announce as _scene_exit_event_announce,
    _scene_exit_event_dialog as _scene_exit_event_dialog,
    _scene_exit_focus as _scene_exit_focus,
    _scene_exit_friend as _scene_exit_friend,
    _scene_exit_level_up as _scene_exit_level_up,
    _scene_exit_loading as _scene_exit_loading,
    _scene_exit_memory_detail as _scene_exit_memory_detail,
    _scene_exit_memory_list as _scene_exit_memory_list,
    _scene_exit_overlay as _scene_exit_overlay,
    _scene_exit_rest_room as _scene_exit_rest_room,
    _scene_exit_tea_room as _scene_exit_tea_room,
    _scene_exit_tea_summary as _scene_exit_tea_summary,
    _scene_failure_detail as _scene_failure_detail,
    recover_scene as recover_scene,
)

from .baijing_tasklist import (  # noqa: F401
    ACTIVITY_NUMBER_ATTEMPTS as ACTIVITY_NUMBER_ATTEMPTS,
    ACTIVITY_NUMBER_INTERVAL as ACTIVITY_NUMBER_INTERVAL,
    ACTIVITY_NUMBER_MAX as ACTIVITY_NUMBER_MAX,
    ACTIVITY_NUMBER_REGION as ACTIVITY_NUMBER_REGION,
    ACTIVITY_PANEL_MARKERS as ACTIVITY_PANEL_MARKERS,
    CLAIM_ALL_TEXTS as CLAIM_ALL_TEXTS,
    CLAIM_EXCLUDE as CLAIM_EXCLUDE,
    CLAIM_TEXTS as CLAIM_TEXTS,
    DAILY_ACTIVITY_BY_KEY as DAILY_ACTIVITY_BY_KEY,
    DAILY_ACTIVITY_TOTAL as DAILY_ACTIVITY_TOTAL,
    DAILY_TASK_CATALOG as DAILY_TASK_CATALOG,
    GOTO_TEXTS as GOTO_TEXTS,
    TAB_COLUMN_REGION as TAB_COLUMN_REGION,
    TASK_BUTTON_REGION as TASK_BUTTON_REGION,
    TASK_BUTTON_TEMPLATE as TASK_BUTTON_TEMPLATE,
    TASK_LIST_CORE_MARKERS as TASK_LIST_CORE_MARKERS,
    TASK_LIST_MARKERS as TASK_LIST_MARKERS,
    TASK_ROW_REGION as TASK_ROW_REGION,
    TASK_SCROLL_MAX as TASK_SCROLL_MAX,
    TASK_SCROLL_STEPS as TASK_SCROLL_STEPS,
    TASK_TEXT_EXCLUDE as TASK_TEXT_EXCLUDE,
    TEMPLATE_MATCH_THRESHOLD as TEMPLATE_MATCH_THRESHOLD,
    TEMPLATE_REFERENCE_WIDTH as TEMPLATE_REFERENCE_WIDTH,
    _ensure_daily_tab as _ensure_daily_tab,
    _find_row_button as _find_row_button,
    _find_row_button_once as _find_row_button_once,
    _find_task_button as _find_task_button,
    _match_rows_to_catalog as _match_rows_to_catalog,
    _parse_task_rows as _parse_task_rows,
    _wait_task_list as _wait_task_list,
)

from .baijing_task_handlers import (  # noqa: F401
    FRIEND_ENTRY_FALLBACK as FRIEND_ENTRY_FALLBACK,
    FRIEND_GIFT_TEXTS as FRIEND_GIFT_TEXTS,
    FRIEND_PAGE_MARKERS as FRIEND_PAGE_MARKERS,
    FRIEND_POPUP_FAILURE_TEXTS as FRIEND_POPUP_FAILURE_TEXTS,
    FRIEND_POPUP_REGION as FRIEND_POPUP_REGION,
    FRIEND_POPUP_TEXTS as FRIEND_POPUP_TEXTS,
    FRIEND_POPUP_TEXT_REGION as FRIEND_POPUP_TEXT_REGION,
    IMAGING_ANYWHERE_CLICK as IMAGING_ANYWHERE_CLICK,
    IMAGING_DIALOG_COUNT_REGION as IMAGING_DIALOG_COUNT_REGION,
    IMAGING_DIALOG_MARKERS as IMAGING_DIALOG_MARKERS,
    IMAGING_DIALOG_OK_BTN as IMAGING_DIALOG_OK_BTN,
    IMAGING_DIALOG_PLUS_BTN as IMAGING_DIALOG_PLUS_BTN,
    IMAGING_QUICK_BATTLE_BTN as IMAGING_QUICK_BATTLE_BTN,
    IMAGING_QUICK_BATTLE_REGION as IMAGING_QUICK_BATTLE_REGION,
    IMAGING_QUICK_BATTLE_TEXTS as IMAGING_QUICK_BATTLE_TEXTS,
    IMAGING_SETTLEMENT_MARKERS as IMAGING_SETTLEMENT_MARKERS,
    IMAGING_STAGE_MARKERS as IMAGING_STAGE_MARKERS,
    LAB_DIALOG_HINT as LAB_DIALOG_HINT,
    LAB_DIALOG_MARKERS as LAB_DIALOG_MARKERS,
    LEAF_CLAIM_ALL_BTN as LEAF_CLAIM_ALL_BTN,
    LEAF_OVERVIEW_BTN as LEAF_OVERVIEW_BTN,
    LEAF_PAGE_MARKERS as LEAF_PAGE_MARKERS,
    LEAF_TAB_FALLBACK as LEAF_TAB_FALLBACK,
    LEAF_TAB_REGION as LEAF_TAB_REGION,
    LEAF_TAB_TEXTS as LEAF_TAB_TEXTS,
    MEMORY_BREAKTHROUGH_CANCEL as MEMORY_BREAKTHROUGH_CANCEL,
    MEMORY_BREAKTHROUGH_CONFIRM as MEMORY_BREAKTHROUGH_CONFIRM,
    MEMORY_BREAKTHROUGH_CONFIRM_SAMPLE as MEMORY_BREAKTHROUGH_CONFIRM_SAMPLE,
    MEMORY_BREAKTHROUGH_MARKERS as MEMORY_BREAKTHROUGH_MARKERS,
    MEMORY_DIALOG_MARKERS as MEMORY_DIALOG_MARKERS,
    MEMORY_ENHANCE_BTN_REGION as MEMORY_ENHANCE_BTN_REGION,
    MEMORY_ENHANCE_BUTTON_TPL as MEMORY_ENHANCE_BUTTON_TPL,
    MEMORY_ENHANCE_TEXTS as MEMORY_ENHANCE_TEXTS,
    MEMORY_LEVEL_TAB_FALLBACK as MEMORY_LEVEL_TAB_FALLBACK,
    MEMORY_LEVEL_TAB_MARKERS as MEMORY_LEVEL_TAB_MARKERS,
    MEMORY_LEVEL_TAB_REGION as MEMORY_LEVEL_TAB_REGION,
    MEMORY_LEVEL_TAB_TEXT as MEMORY_LEVEL_TAB_TEXT,
    MEMORY_LIST_CLICK_CANDIDATES as MEMORY_LIST_CLICK_CANDIDATES,
    MEMORY_RESET_REGION as MEMORY_RESET_REGION,
    PLUS_BUTTON_DX_RATIO as PLUS_BUTTON_DX_RATIO,
    REST_ROOM_TEA_ENTRY as REST_ROOM_TEA_ENTRY,
    STAGE_BATTLE_TASKS as STAGE_BATTLE_TASKS,
    TEA_CONFIRM_MARKERS as TEA_CONFIRM_MARKERS,
    TEA_CONFIRM_OK as TEA_CONFIRM_OK,
    TEA_DIALOGUE_CLICK as TEA_DIALOGUE_CLICK,
    TEA_DRINK_POLL_SEC as TEA_DRINK_POLL_SEC,
    TEA_DRINK_ROUNDS as TEA_DRINK_ROUNDS,
    TEA_DRINK_TEXTS as TEA_DRINK_TEXTS,
    TEA_DRINK_WAIT_SEC as TEA_DRINK_WAIT_SEC,
    TEA_FIRST_CHARACTER as TEA_FIRST_CHARACTER,
    TEA_FIRST_TOPIC as TEA_FIRST_TOPIC,
    TEA_INVITE_EXCLUDE_TEXTS as TEA_INVITE_EXCLUDE_TEXTS,
    TEA_INVITE_MAX_CHARS as TEA_INVITE_MAX_CHARS,
    TEA_INVITE_TEXTS as TEA_INVITE_TEXTS,
    TEA_SCENE_MARKERS as TEA_SCENE_MARKERS,
    TEA_SCENE_TIMEOUT_SEC as TEA_SCENE_TIMEOUT_SEC,
    TEA_SELECT_DRINK_RATIO as TEA_SELECT_DRINK_RATIO,
    TEA_SELECT_MARKERS as TEA_SELECT_MARKERS,
    TEA_SELECT_TIMEOUT_SEC as TEA_SELECT_TIMEOUT_SEC,
    TEA_SUMMARY_CLICK as TEA_SUMMARY_CLICK,
    TEA_TOPIC_MARKERS as TEA_TOPIC_MARKERS,
    _back_to_memory_list as _back_to_memory_list,
    _capture_stage_battle_dialog as _capture_stage_battle_dialog,
    _click_quick_battle_entry as _click_quick_battle_entry,
    _close_lab_dialog as _close_lab_dialog,
    _confirm_action_power_use as _confirm_action_power_use,
    _do_friend_medal_on_page as _do_friend_medal_on_page,
    _ensure_memory_level_tab as _ensure_memory_level_tab,
    _enter_friend_page_from_main as _enter_friend_page_from_main,
    _exit_focus_if_present as _exit_focus_if_present,
    _find_memory_enhance_button as _find_memory_enhance_button,
    _find_plus_button as _find_plus_button,
    _handle_friend_gift_popup as _handle_friend_gift_popup,
    _invite_hit_usable as _invite_hit_usable,
    _is_breakthrough_dialog as _is_breakthrough_dialog,
    _nudge_into_tea_scene as _nudge_into_tea_scene,
    _retry_tea_entry_after_focus as _retry_tea_entry_after_focus,
    _same_click_target as _same_click_target,
)

from .baijing_echo import (  # noqa: F401
    CLUB_ACTIVITY_DETECT_REGION as CLUB_ACTIVITY_DETECT_REGION,
    CLUB_ACTIVITY_ECHO_TEXTS as CLUB_ACTIVITY_ECHO_TEXTS,
    CLUB_ACTIVITY_SNAPSHOT_PREFIX as CLUB_ACTIVITY_SNAPSHOT_PREFIX,
    CLUB_BACK_REGION as CLUB_BACK_REGION,
    CLUB_BLANK_CLICK as CLUB_BLANK_CLICK,
    CLUB_CARD_FALLBACK as CLUB_CARD_FALLBACK,
    CLUB_CARD_REGION as CLUB_CARD_REGION,
    CLUB_CLAIM_TEXTS as CLUB_CLAIM_TEXTS,
    CLUB_DAILY_TASK_TEXTS as CLUB_DAILY_TASK_TEXTS,
    CLUB_DONE_TEXTS as CLUB_DONE_TEXTS,
    CLUB_ENTRY_FALLBACK as CLUB_ENTRY_FALLBACK,
    CLUB_ENTRY_REGION as CLUB_ENTRY_REGION,
    CLUB_ENTRY_TEMPLATE as CLUB_ENTRY_TEMPLATE,
    CLUB_PAGE_MARKERS as CLUB_PAGE_MARKERS,
    CLUB_SNAPSHOT_PREFIX as CLUB_SNAPSHOT_PREFIX,
    ECHO_AUTOFORM_CONFIRM_FALLBACK as ECHO_AUTOFORM_CONFIRM_FALLBACK,
    ECHO_AUTOFORM_TEXTS as ECHO_AUTOFORM_TEXTS,
    ECHO_BATTLE_TIMEOUT as ECHO_BATTLE_TIMEOUT,
    ECHO_BOSS_BACK_FALLBACK as ECHO_BOSS_BACK_FALLBACK,
    ECHO_BOSS_FIGHT_DONE_MARKS as ECHO_BOSS_FIGHT_DONE_MARKS,
    ECHO_BOSS_FIGHT_FALLBACK as ECHO_BOSS_FIGHT_FALLBACK,
    ECHO_BOSS_FIGHT_TEXTS as ECHO_BOSS_FIGHT_TEXTS,
    ECHO_BOSS_ICON_FALLBACK as ECHO_BOSS_ICON_FALLBACK,
    ECHO_BOSS_LADDER_CLICK_OFFSET as ECHO_BOSS_LADDER_CLICK_OFFSET,
    ECHO_BOSS_LADDER_FALLBACK as ECHO_BOSS_LADDER_FALLBACK,
    ECHO_BOSS_LADDER_REGION as ECHO_BOSS_LADDER_REGION,
    ECHO_BOSS_LADDER_TIERS as ECHO_BOSS_LADDER_TIERS,
    ECHO_BOSS_PANEL_MARKS as ECHO_BOSS_PANEL_MARKS,
    ECHO_BOSS_SCORE_TOP_TIER as ECHO_BOSS_SCORE_TOP_TIER,
    ECHO_BOSS_SETTLE_TIMEOUT as ECHO_BOSS_SETTLE_TIMEOUT,
    ECHO_BOSS_TITLE as ECHO_BOSS_TITLE,
    ECHO_CLAIM_FALLBACK as ECHO_CLAIM_FALLBACK,
    ECHO_CLAIM_REGION as ECHO_CLAIM_REGION,
    ECHO_CLAIM_TEXTS as ECHO_CLAIM_TEXTS,
    ECHO_HEARTBEAT_SEC as ECHO_HEARTBEAT_SEC,
    ECHO_MAP_BODY as ECHO_MAP_BODY,
    ECHO_MAP_REWARD_CLAIM_ALL as ECHO_MAP_REWARD_CLAIM_ALL,
    ECHO_MAP_REWARD_CLAIM_ALL_FALLBACK as ECHO_MAP_REWARD_CLAIM_ALL_FALLBACK,
    ECHO_MAP_REWARD_CLOSE_FALLBACK as ECHO_MAP_REWARD_CLOSE_FALLBACK,
    ECHO_MAP_REWARD_ENTRY as ECHO_MAP_REWARD_ENTRY,
    ECHO_MAP_TITLE as ECHO_MAP_TITLE,
    ECHO_META_KEY as ECHO_META_KEY,
    ECHO_NODE_LABEL as ECHO_NODE_LABEL,
    ECHO_NODE_LETTERS as ECHO_NODE_LETTERS,
    ECHO_NODE_REGION as ECHO_NODE_REGION,
    ECHO_RESET_FALLBACK_DAYS as ECHO_RESET_FALLBACK_DAYS,
    ECHO_RESET_RE as ECHO_RESET_RE,
    ECHO_WAIT_MAP as ECHO_WAIT_MAP,
    ECHO_WAIT_PANEL as ECHO_WAIT_PANEL,
    _claim_club_daily_task as _claim_club_daily_task,
    _echo_boss_and_map_rewards as _echo_boss_and_map_rewards,
    _echo_boss_claim_ladder as _echo_boss_claim_ladder,
    _echo_boss_fight as _echo_boss_fight,
    _echo_boss_open_panel as _echo_boss_open_panel,
    _echo_boss_panel_confirmed as _echo_boss_panel_confirmed,
    _echo_boss_wait_settled as _echo_boss_wait_settled,
    _echo_defeat_frame as _echo_defeat_frame,
    _echo_letter_of as _echo_letter_of,
    _echo_load_marks as _echo_load_marks,
    _echo_map_confirmed as _echo_map_confirmed,
    _echo_map_rewards_claim as _echo_map_rewards_claim,
    _echo_node_labels as _echo_node_labels,
    _echo_panel_claim as _echo_panel_claim,
    _echo_pick_label as _echo_pick_label,
    _echo_read_top_score as _echo_read_top_score,
    _echo_reset_expiry as _echo_reset_expiry,
    _echo_result_frame as _echo_result_frame,
    _echo_save_marks as _echo_save_marks,
    _echo_texts as _echo_texts,
    _echo_wait_battle_settled as _echo_wait_battle_settled,
    _echo_wait_map as _echo_wait_map,
    _echo_zone_fight_node as _echo_zone_fight_node,
    _echo_zone_run_content as _echo_zone_run_content,
    _enter_club_page as _enter_club_page,
    _leave_club_page as _leave_club_page,
    _run_club_activity_echo_zone as _run_club_activity_echo_zone,
)
