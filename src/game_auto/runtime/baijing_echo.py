# -*- coding: utf-8 -*-
"""白荆回廊·俱乐部每日任务与回声猎区（2026-11-14 自 baijing_daily.py 拆出）。

职责：俱乐部页两大块——①俱乐部每日任务领取（claim_club_daily_reward，
每日刷新）；②「回声猎区」活动分支（2026-11-11 入口 / 11-13 节点链 /
11-14 boss 计分战+声量阶梯+分贝奖励固化）：A~E 节点顺序清（已清标记存
workflow.db meta）、boss 闸门（本期已打过只领奖）、战后结算页/战败页收口。

数据流：daily 的 run_baijing_daily_vision_flow 在日常段调用
claim_club_daily_reward；本模块对 daily **零依赖**（_ensure_main_ui 从
scene 取），否则会与 daily 成环。编队链路 lazy import baijing_fitting
（拟合回归同款，用户 2026-11-13 拍板复用）。

不变量与坑：
- 节点链是顺序链：点开面板无「开始收录」= 锁定，整链止步（正常收尾非失败）。
- 战败绝不写已清标记（写=下次跳过=永久漏奖励，2026-11-14 实锤）。
- 节点标签搜索区右界 0.70 必须含住节点 E（x≈0.667，2026-11-14 事故修复），
  且避开常驻会员排名面板（≈0.78）。
"""
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from ..settings.paths import TEMPLATES_DIR, WORKFLOW_DB_PATH
from .baijing_common import (
    MAX_CLOSE_ATTEMPTS,
    _abs_region,
    _capture_daily_list_snapshot,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _ocr_best_match,
    _pick_result,
    _ratio_point,
    _record_step_failure,
    _scene_results,
    _stopped,
    _template_center,
    _wait_page_markers,
)
from .baijing_scene import _ensure_main_ui
from .baijing_flow import _focus_game_window, detect_baijing_main_ui
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, grab_screen, ocr_window_content


# ── 俱乐部每日任务（2026-10-18 用户截图实测） ──
# 流程：主界面左上角第 4 个图标（俱乐部）→ 弹「新物品已存入S.E.E.D.仓库」点空白继续 →
#       俱乐部页面点「俱乐部每日任务」卡片领取 → 卡片变「今日任务已完成」→ 返回主界面。
# 下面比例全部来自 1919x1079 实测截图 + RapidOCR 探针复核：
#   入口图标 (0.183,0.047)（图标无文字，靠模板定位，比例兜底；红点已 inpaint 抹除，
#   整图只有一个峰 conf=0.924，不会误点旁边的邮件/好友/铃铛图标）
#   俱乐部页 返回 (0.102,0.049)｜俱乐部每日任务 卡片标题 (0.555,0.294)｜卡片中心 (0.573,0.381)
#   领完后卡片显示「今日任务已完成」(0.573,0.381)
#   页面特征：俱乐部等级 0.103,0.372｜俱乐部首席 0.102,0.578｜共响金 0.087,0.510｜
#             俱乐部活动 0.720,0.292｜俱乐部商店 0.898,0.931｜俱乐部成员 0.563,0.932
CLUB_ENTRY_TEMPLATE = str(TEMPLATES_DIR / "白荆回廊-俱乐部入口.png")


CLUB_ENTRY_REGION = (0.13, 0.00, 0.24, 0.10)   # 左上角图标区（只放第 4 个图标，排掉邻居）


CLUB_ENTRY_FALLBACK = (0.183, 0.047)


# 页面特征：**不要用底部「俱乐部成员/俱乐部商店/俱乐部支援」**——弹「新物品已存入
# 仓库」提示时这三行照样能被 OCR 读到，会把弹窗状态误判成已进页面，后面点卡片会被
# 弹窗挡住。用「弹窗期间认不出的那几个字」当特征（实测 210011 弹窗帧全都没认出来）。
CLUB_PAGE_MARKERS = ("俱乐部每日任务", "俱乐部活动", "俱乐部等级", "俱乐部首席", "共响金")


CLUB_DAILY_TASK_TEXTS = ("俱乐部每日任务",)


CLUB_DONE_TEXTS = ("今日任务已完成",)


CLUB_CLAIM_TEXTS = ("领取",)


CLUB_CARD_REGION = (0.45, 0.25, 0.75, 0.52)   # 每日任务卡片


CLUB_CARD_FALLBACK = (0.573, 0.381)


# 弹窗/加载中的空白推进点（顶部居中空白，避开返回按钮与货币栏）
CLUB_BLANK_CLICK = (0.50, 0.13)


CLUB_BACK_REGION = (0.0, 0.0, 0.20, 0.10)


CLUB_SNAPSHOT_PREFIX = "club_daily"



# ── 俱乐部活动「回声猎区」分支入口（2026-11-11 用户截图实测，活动重置剩余 12 天） ──
# 俱乐部页右侧「俱乐部活动」卡片整张可点：卡片边界 frac x 0.484~0.967 / y 0.264~0.497，
# 「回声猎区」大字标题中心 ≈ (0.819, 0.370)。分支只做"检测入口 → 点进活动页 →
# 快照取证 → 立即退出"，活动内容（刷本/领奖）未实现，等实机探明活动页布局再补。
CLUB_ACTIVITY_ECHO_TEXTS = ("回声猎区",)


# 检测区域限定在活动卡片内（顶部避开「今日俱乐部奖励已领取」横幅 y≈0.215~0.245）；
# 单词检测但带区域限定（页面判定铁律允许模式：标题+区域限定），且 OCR 未命中
# **不做比例盲点**——活动结束后卡片可能消失/换内容，瞎点会点进未知页面。
CLUB_ACTIVITY_DETECT_REGION = (0.50, 0.26, 1.0, 0.52)


CLUB_ACTIVITY_SNAPSHOT_PREFIX = "club_activity_echo"



# ── 回声猎区内容（2026-11-13 用户实机截图实测：A~E 顺序解锁，boss 暂不打） ──
# 用户拍板口径：「先打 abcde 按顺序打，点进去是编队选择页面，可以复用拟合回归」。
# 链路：地图点节点标签 → 右侧详情面板「开始收录」→ 编队页（拟合回归同款：
# 作战开始 → [布阵页] → 开始战斗 → 挂机 → 结算）→ 回地图 → 下一个节点。
ECHO_MAP_TITLE = ("回声猎区",)


# 地图页判定 = 标题 + ≥1 地图独有词（双证据，铁律禁单标题——俱乐部页入口卡片
# 也有「回声猎区」四个字；证据 = 2026-11-13 用户地图页截图）。
ECHO_MAP_BODY = ("纯净回声", "重置时间剩余", "猎区累计分贝奖励")


# 节点标签「纯净回声·X」定位：优先信 OCR 文本里的字母；字母认不全时按 x 从左
# 到右对应 A~E（两张截图 x 序严格递增）。**禁固定比例兜底**——选中节点后地图
# 会平移（A 标签 11-13 两张截图分别在 x≈0.107 / x≈0.046），比例必漂。
ECHO_NODE_LABEL = "纯净回声"


ECHO_NODE_LETTERS = ("A", "B", "C", "D", "E")


# 节点标签搜索区：地图中下部，右界 0.70 必须含住节点 E 标签中心 x≈0.667
# （2026-11-14 实拍；旧右界 0.66 把 E 滤掉 →「节点 E 标签未定位」），且避开
# 右侧常驻会员排名面板（左缘 ≈0.78）。点节点才开的详情面板（左缘 ≈0.68）
# 标题同名「纯净回声·X」，但标题在面板上部（y<0.35 区外），加上字母优先 +
# 5 标签 x 序兜底双防线，残余泄漏只会走向「宁可不点交证据」的安全侧。
ECHO_NODE_REGION = (0.0, 0.35, 0.70, 0.95)


# 详情面板「开始收录」按钮：OCR 优先，比例兜底 = 11-13 截图按钮中心 (1620,985)；
# 搜索区 y≥0.55 避开面板上部「关卡详情/推荐刻印评价」。
ECHO_CLAIM_TEXTS = ("开始收录",)


ECHO_CLAIM_FALLBACK = (0.844, 0.912)


ECHO_CLAIM_REGION = (0.60, 0.55, 1.0, 1.0)


# 「重置时间剩余N天」→ 已清标记过期日；读不到按 3 天保守失效（宁可重打不可漏打）
ECHO_RESET_RE = re.compile(r"重置时间剩余\s*(\d+)\s*天")


ECHO_RESET_FALLBACK_DAYS = 3


ECHO_META_KEY = "baijing_echo_zone:cleared"   # meta 存 JSON {"period_end","cleared"}


# 等待/超时（秒）
ECHO_WAIT_MAP = 25.0         # 俱乐部页 → 猎区地图（有过场加载）


ECHO_WAIT_PANEL = 6.0        # 点节点标签 → 详情面板出现（锁定节点无面板）


ECHO_HEARTBEAT_SEC = 20.0    # 战斗挂机心跳


ECHO_BATTLE_TIMEOUT = 480.0  # 单节点战斗上限（拟合 25 波 420s，boss 级留余量）



# ── 中央 boss「不协和音·鱼叉将军」+ 奖励领取（2026-11-14 提权会话实测） ──
# boss 是计分战不是胜负战：按钮叫「开始降噪」非「开始收录」，产出「有效分贝/
# 总分贝」计入个人声量；面板左侧「个人声量奖励」阶梯 1000~9000 按档领取。
ECHO_BOSS_TITLE = "不协和音"          # boss 名：地图标签与面板标题同名（禁单标题判页）


# boss 面板判定 = 标题 + ≥1 面板独有词。地图点 boss 标题只展开「已收录」条
# （无面板独有词，不会误判成面板）；战后总结页无 boss 标题，同样不匹配。
ECHO_BOSS_PANEL_MARKS = ("开始降噪", "重新降噪", "快速降噪", "本期最高分贝", "个人声量奖励")


ECHO_BOSS_FIGHT_TEXTS = ("开始降噪",)


ECHO_BOSS_FIGHT_DONE_MARKS = ("重新降噪", "快速降噪")  # 在场 = 本期已打过，跳过战斗


ECHO_BOSS_SCORE_TOP_TIER = 9000       # 声量阶梯顶档；本期最高分贝达标 → 战斗无收益跳过


# boss 图标比例（点标题只弹「已收录」条时点图标开面板；实测中心 (842,430)）
ECHO_BOSS_ICON_FALLBACK = (0.439, 0.398)


ECHO_BOSS_FIGHT_FALLBACK = (0.725, 0.876)   # 「开始降噪」按钮（实测 (1392,946)）


ECHO_BOSS_BACK_FALLBACK = (0.101, 0.047)    # boss 面板/总结页左上「返回」（实测 (193,51)）


# 声量阶梯金额列：纯数字 9000~1000 限 x<0.20（避开右侧本期/今日分贝与排名数值）
ECHO_BOSS_LADDER_REGION = (0.0, 0.28, 0.20, 0.88)


ECHO_BOSS_LADDER_TIERS = ("9000", "7000", "5000", "3000", "1000")


# 金额 → 奖励图标点击偏移（实测「7000」(145,477) → 图标 (263,497)）
ECHO_BOSS_LADDER_CLICK_OFFSET = (118, 20)


# OCR 认不出档位时的固定比例兜底（面板布局稳定，无地图平移问题）
ECHO_BOSS_LADDER_FALLBACK = ((0.137, 0.345), (0.137, 0.460), (0.136, 0.572),
                             (0.139, 0.688), (0.138, 0.802))


ECHO_BOSS_SETTLE_TIMEOUT = 720.0      # boss 计分战实测 ~4min（开场 CG ~90s），留足余量


# 「编队成员数量不足，是否自动编队？」弹窗（boss 编队页常为空，11-14 实锤）
ECHO_AUTOFORM_TEXTS = ("编队成员数量不足",)


ECHO_AUTOFORM_CONFIRM_FALLBACK = (0.623, 0.632)   # 弹窗「确认」（实测 (1196,683)）


# 地图左下「猎区累计分贝奖励」页：入口 OCR 定位（禁盲点），「一键领取」幂等
ECHO_MAP_REWARD_ENTRY = ("猎区累计分贝奖励",)


ECHO_MAP_REWARD_CLAIM_ALL = ("一键领取",)


ECHO_MAP_REWARD_CLAIM_ALL_FALLBACK = (0.801, 0.812)   # 实测 (1538,877)


ECHO_MAP_REWARD_CLOSE_FALLBACK = (0.832, 0.171)       # 奖励页右上 X（实测 (1597,185)）




def _enter_club_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """主界面左上第 4 个图标进俱乐部；中途弹「新物品已存入仓库」点空白继续。"""
    if _stopped(stop_check):
        return False
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return False
    _focus_game_window(logger, hwnd)
    _dismiss_overlay(logger, hwnd)
    center = _template_center(logger, hwnd, CLUB_ENTRY_TEMPLATE, frac_region=CLUB_ENTRY_REGION, threshold=0.85)
    if center:
        _pyautogui_click_guarded(*center)
        logger.info(f"[白荆回廊·俱乐部] 点击左上角俱乐部入口图标 @ {center}")
    else:
        _click_ratio(logger, hwnd, CLUB_ENTRY_FALLBACK, "「俱乐部」入口图标（比例兜底）")
    time.sleep(2.0)
    # 直接进页面就最好；没进去通常是弹了「新物品已存入S.E.E.D.仓库」这类提示
    if _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=6.0, stop_check=stop_check):
        logger.info("[白荆回廊·俱乐部] 已进入俱乐部页面")
        return True
    logger.info("[白荆回廊·俱乐部] 未见俱乐部页面特征，点击空白区域继续")
    _dismiss_overlay(logger, hwnd)
    _click_ratio(logger, hwnd, CLUB_BLANK_CLICK, "空白区域（继续进入）")
    time.sleep(1.5)
    if _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=8.0, stop_check=stop_check):
        logger.info("[白荆回廊·俱乐部] 点击空白区域后已进入俱乐部页面")
        return True
    # 2026-10-22：两次都没进页面，重试一次入口点击（弹窗/加载挡掉首次点击时），
    # 再次以页面特征文字确认——不凭"点了就算"
    logger.info("[白荆回廊·俱乐部] 仍未进页面，重试点击俱乐部入口")
    _dismiss_overlay(logger, hwnd)
    center = _template_center(logger, hwnd, CLUB_ENTRY_TEMPLATE, frac_region=CLUB_ENTRY_REGION, threshold=0.85)
    if center:
        _pyautogui_click_guarded(center[0], center[1])
        logger.info(f"[白荆回廊·俱乐部] 重试点击俱乐部入口图标 @ {center}")
    else:
        _click_ratio(logger, hwnd, CLUB_ENTRY_FALLBACK, "「俱乐部」入口图标（比例兜底）")
    time.sleep(2.0)
    if not _wait_page_markers(logger, hwnd, CLUB_PAGE_MARKERS, timeout=8.0, stop_check=stop_check):
        logger.warning("[白荆回廊·俱乐部] 未进入俱乐部页面")
        return False
    logger.info("[白荆回廊·俱乐部] 重试后已进入俱乐部页面")
    return True




def _claim_club_daily_task(logger: logging.Logger, hwnd, stop_check=None) -> str:
    """点「俱乐部每日任务」卡片领取，返回 purchased / already / no_claim / failed。

    领完后卡片会变成「今日任务已完成」（实测 2026-10-18 截图），这是唯一的成功凭据：
    页面顶栏那条「今日俱乐部奖励已领取」在进页面前就已存在，不能当领取结果。
    """
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 卡片已是「今日任务已完成」，无需重复领取")
        return "already"
    # 先点卡片标题；有些版本标题即领取热区
    card = _ocr_best_match(
        logger, CLUB_DAILY_TASK_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55
    )
    if card:
        _pyautogui_click_guarded(*card["center"])
        logger.info(f"[白荆回廊·俱乐部] 点击「俱乐部每日任务」卡片 @ {card['center']}")
    else:
        _click_ratio(logger, hwnd, CLUB_CARD_FALLBACK, "「俱乐部每日任务」卡片（比例兜底）")
    time.sleep(1.5)
    # 领完后可能弹奖励提示，先清一次
    _dismiss_overlay(logger, hwnd)
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 卡片已变为「今日任务已完成」")
        return "purchased"
    # 若弹出独立「领取」按钮则点掉（卡片点击不一定直接领）
    claim_clicked = False
    claim = _ocr_best_match(logger, CLUB_CLAIM_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.6)
    if claim:
        claim_clicked = True
        _pyautogui_click_guarded(*claim["center"])
        logger.info(f"[白荆回廊·俱乐部] 点击「领取」@ {claim['center']}")
        time.sleep(1.5)
        _dismiss_overlay(logger, hwnd)
        if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
            logger.info("[白荆回廊·俱乐部] 点击「领取」后卡片显示「今日任务已完成」")
            return "purchased"
    # 兜底：点卡片中心（标题不是热区时）
    _click_ratio(logger, hwnd, CLUB_CARD_FALLBACK, "卡片中心（领取热区兜底）")
    time.sleep(1.5)
    _dismiss_overlay(logger, hwnd)
    if _ocr_best_match(logger, CLUB_DONE_TEXTS, hwnd, frac_region=CLUB_CARD_REGION, min_conf=0.55):
        logger.info("[白荆回廊·俱乐部] 点击卡片中心后显示「今日任务已完成」")
        return "purchased"
    # 没有可点的「领取」按钮：卡片点击本身没生效更可能是“今日无奖励可领”
    # （任务未完成或奖励已领），不要记成点击失败。只有确实点过「领取」仍未变化才算 failed。
    return "failed" if claim_clicked else "no_claim"




def _leave_club_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """俱乐部页返回主界面（左上「返回」→ ESC）。"""
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        _dismiss_overlay(logger, hwnd)
        ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
        if ok:
            logger.info("[白荆回廊·俱乐部] 已回到主界面")
            return True
        back = _ocr_best_match(logger, ("返回",), hwnd, frac_region=CLUB_BACK_REGION, min_conf=0.55)
        if back:
            _pyautogui_click_guarded(*back["center"])
            logger.info(f"[白荆回廊·俱乐部] 点击「返回」@ {back['center']}")
        else:
            _focus_game_window(logger, hwnd)
            pyautogui.press("escape")
            logger.info(f"[白荆回廊·俱乐部] 按 ESC 返回主界面 (#{attempt}/{MAX_CLOSE_ATTEMPTS})")
        time.sleep(1.5)
    ok, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=2.0)
    if not ok:
        logger.warning("[白荆回廊·俱乐部] 多次尝试后仍未回到主界面")
    return ok




def _echo_texts(items: List[Dict], min_conf: float = 0.5) -> List[str]:
    """当帧 OCR 文本（置信度过滤）——回声猎区各判定共用。"""
    return [
        str(it.get("text") or "")
        for it in (items or [])
        if float(it.get("confidence") or 0) >= min_conf
    ]




def _echo_map_confirmed(items: List[Dict]) -> bool:
    """猎区地图 = 标题「回声猎区」+ ≥1 地图独有词（双证据，页面判定铁律）。

    俱乐部页入口卡片也有「回声猎区」四个字，单标题必误判；独有词取节点标签/
    重置倒计时/左下奖励入口（2026-11-13 用户地图页截图实测）。
    """
    texts = _echo_texts(items)
    if not any(m in t for t in texts for m in ECHO_MAP_TITLE):
        return False
    return any(m in t for t in texts for m in ECHO_MAP_BODY)




def _echo_reset_expiry(items: List[Dict]) -> str:
    """从「重置时间剩余N天」算已清标记过期日（ISO 日期）；读不到按保守值。

    读不到宁可短给（3 天后标记作废重打一遍）也不长给——标记错最多浪费一场，
    漏打是丢奖励。
    """
    for text in _echo_texts(items):
        m = ECHO_RESET_RE.search(text)
        if m:
            days = int(m.group(1))
            return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
    return time.strftime(
        "%Y-%m-%d", time.localtime(time.time() + ECHO_RESET_FALLBACK_DAYS * 86400)
    )




def _echo_load_marks(logger: logging.Logger) -> Dict:
    """读「已清节点」标记（workflow.db meta，JSON）；损坏/过期一律按无标记。

    标记只是跳过优化，不是真值闸：每次开打前还有详情面板「开始收录」在场与否
    这道实测判断（2026-11-13 口径）。用户手动打的节点标记不知道，最多重打一场。
    """
    try:
        from ..storage.workflow_db import WorkflowDB

        raw = WorkflowDB(str(WORKFLOW_DB_PATH)).get_task_meta(ECHO_META_KEY)
        marks = json.loads(raw) if raw else {}
        cleared = marks.get("cleared")
        period_end = str(marks.get("period_end") or "")
        if not isinstance(cleared, list) or not period_end:
            return {}
        if period_end < time.strftime("%Y-%m-%d"):  # 活动期已过 → 标记作废
            return {}
        return {"period_end": period_end, "cleared": [str(c) for c in cleared]}
    except Exception as exc:  # noqa: BLE001 - 标记读不到不影响作战，只当无标记
        logger.debug(f"[白荆回廊·俱乐部] 回声猎区已清标记读取失败（按无标记）: {exc}")
        return {}




def _echo_save_marks(logger: logging.Logger, period_end: str, cleared: List[str]) -> None:
    """写「已清节点」标记；失败只告警——下次最多重打已清节点，无害。"""
    try:
        from ..storage.workflow_db import WorkflowDB

        WorkflowDB(str(WORKFLOW_DB_PATH)).set_task_meta(
            ECHO_META_KEY,
            json.dumps(
                {"period_end": period_end, "cleared": sorted(set(cleared))},
                ensure_ascii=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[白荆回廊·俱乐部] 回声猎区已清标记写入失败: {exc}")




def _echo_node_labels(hwnd, items: List[Dict]) -> List[Dict]:
    """当前帧地图区内的节点标签（含「纯净回声」），按 x 从左到右排序。

    右界 0.70 含住节点 E 标签（x≈0.667，2026-11-14 实拍）并避开常驻会员排名
    面板（≈0.78）；详情面板标题同名「纯净回声·X」，靠 y≥0.35 与字母优先/5 标签
    x 序兜底双防线防混入（见 ECHO_NODE_REGION 注释）。窗口取不到（失效/测试桩）
    时退化为只按文本匹配不过滤区域。
    """
    region = _abs_region(hwnd, ECHO_NODE_REGION)
    out: List[Dict] = []
    for it in items or []:
        text = str(it.get("text") or "")
        if float(it.get("confidence") or 0) < 0.5 or ECHO_NODE_LABEL not in text:
            continue
        if region:
            ax, ay, aw, ah = region
            cx, cy = it.get("center") or (0, 0)
            if not (ax <= cx <= ax + aw and ay <= cy <= ay + ah):
                continue
        out.append(it)
    out.sort(key=lambda it: (it.get("center") or (0, 0))[0])
    return out




def _echo_letter_of(text: str) -> Optional[str]:
    """从「纯净回声·A」式文本提节点字母；认不出返回 None。

    分隔符放行 ≤2 个非字母数字字符（·/./-），且字母后不能再跟字母——防 OCR
    把两个标签并成一条时取错字母。
    """
    m = re.search(r"纯净回声[^\dA-Za-z]{0,2}([A-Ea-e])(?![A-Za-z])", text)
    return m.group(1).upper() if m else None




def _echo_pick_label(labels: List[Dict], letter: str) -> Optional[Dict]:
    """定位目标节点标签：OCR 文本里的字母优先，x 序兜底（仅当 5 个标签全在）。

    x 序兜底的依据：11-13 两张实机截图里 A~E 标签 x 严格递增；地图平移不影响
    相对顺序。凑不齐 5 个且字母认不出时宁可不点（交证据），绝不猜。
    """
    for it in labels:
        if _echo_letter_of(str(it.get("text") or "")) == letter:
            return it
    if len(labels) == len(ECHO_NODE_LETTERS):
        return labels[ECHO_NODE_LETTERS.index(letter)]
    return None




def _echo_wait_map(
    logger: logging.Logger, hwnd, stop_check, timeout: float = ECHO_WAIT_MAP
) -> Tuple[bool, List[Dict], str]:
    """轮询等猎区地图双证据确认；返回 (ok, 确认帧 items, 重置过期日)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, [], ""
        items = _scene_results(logger, hwnd)
        if _echo_map_confirmed(items):
            return True, items, _echo_reset_expiry(items)
        time.sleep(1.0)
    return False, [], ""




def _echo_panel_claim(
    logger: logging.Logger, hwnd, stop_check, timeout: float = ECHO_WAIT_PANEL
) -> Optional[Dict]:
    """点节点标签后等详情面板「开始收录」；没有 = 锁定/不可打（顺序链终止）。

    锁定节点在地图上照样有标签（11-13 截图 B~E 带锁），点开没有「开始收录」
    才是不可打的实锤，所以这道闸不能省。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return None
        hit = _ocr_best_match(
            logger, ECHO_CLAIM_TEXTS, hwnd,
            frac_region=ECHO_CLAIM_REGION, min_conf=0.55,
        )
        if hit:
            return hit
        time.sleep(1.0)
    return None




def _echo_result_frame(items: List[Dict]) -> bool:
    """回声猎区胜利结算页（2026-11-14 实机）：「任务完成」+「关卡目标」双词同帧。

    文案与拟合回归的「作战完成/达成波次」完全不同——fitting 的 result 分类
    不认这页，首次实机打完 A 后分类恒 unknown，收敛循环干等 480s 报败。
    """
    texts = [_normalize_text(str(it.get("text") or "")) for it in items or []]
    return (
        any("任务完成" in t for t in texts if t)
        and any("关卡目标" in t for t in texts if t)
    )




def _echo_defeat_frame(items: List[Dict]) -> bool:
    """回声猎区战败页（2026-11-14 实机）：「任务失败」与「推荐」双词同帧。

    战斗目标横幅也含「任务失败」字样，单词命中是误报陷阱（会话两次实锤：
    误点把开场过场点没了、假回图还假写已清标记）；真战败页独有「推荐您：
    提升刻印/培养同调者」卡片，必须与「推荐」同帧才算战败。
    """
    texts = [_normalize_text(str(it.get("text") or "")) for it in items or []]
    return (
        any("任务失败" in t for t in texts if t)
        and any("推荐" in t for t in texts if t)
    )




def _echo_wait_battle_settled(
    logger: logging.Logger, hwnd, stop_check, deadline: float
) -> Tuple[bool, str, List[Dict]]:
    """战斗开始后自适应等收敛：结算/战败/奖励/浮层逐个点掉，回猎区地图算完成。

    2026-11-14 实机收口（A~D + boss 全打过），战后落点全套实锤：
    - 胜利结算页「任务完成/关卡目标」→ 点空白推进（`_echo_result_frame`）；
    - 战败页「任务失败+推荐您」不自动消失（实机挂 8 分钟）→ 点空白推进，
      点完回图但节点没打下来——返回 map_defeated 让调用方不写已清标记；
    - 战斗目标横幅含「任务失败」，单词误报会把开场过场点没，战败判定必须
      与「推荐」同帧（`_echo_defeat_frame`）；
    - 奖励页点「点击屏幕继续」、物品浮层按 ESC、地图双证据先到先收（原口径）。
    全程心跳日志，超时交调用方落证据。
    """
    from .baijing_fitting import (  # 延迟导入：fitting 懒引用本模块，防环
        _dismiss_item_popup,
        _page_now,
        P_RESULT_CONTINUE,
        P_REWARD_CONTINUE,
    )

    last_heartbeat = 0.0
    last_page, last_items = "unknown", []
    defeated = False  # 本场点过战败页 → 回图也不算收录（防假清标记）
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "用户停止", last_items
        page, items = _page_now(logger, hwnd)
        last_page, last_items = page, items
        if _echo_map_confirmed(items):
            return True, ("map_defeated" if defeated else "map"), items
        if _echo_result_frame(items):
            _click_ratio(logger, hwnd, P_RESULT_CONTINUE, "回声猎区结算页点空白")
            time.sleep(2.0)
            continue
        if _echo_defeat_frame(items):
            # 点位取屏幕中心，避开左侧「前往提升刻印/培养同调者」按钮（点了
            # 会跳养成页）；战败流程：点空白 → 回猎区地图（节点未清）。
            _click_ratio(logger, hwnd, P_RESULT_CONTINUE, "回声猎区战败页点空白")
            defeated = True
            time.sleep(2.5)
            continue
        if page == "result":
            _click_ratio(logger, hwnd, P_RESULT_CONTINUE, "回声猎区结算页点空白")
            time.sleep(2.0)
            continue
        if page == "reward":
            _click_ratio(logger, hwnd, P_REWARD_CONTINUE, "回声猎区奖励页继续")
            time.sleep(2.0)
            continue
        if page == "item_popup":
            _dismiss_item_popup(logger, hwnd)
            continue
        cont = next(
            (it for it in items or [] if "点击屏幕继续" in str(it.get("text") or "")),
            None,
        )
        if cont and page == "unknown":
            _pyautogui_click_guarded(*cont["center"])
            logger.info("[白荆回廊·俱乐部] 回声猎区：点「点击屏幕继续」推进过场")
            time.sleep(1.5)
            continue
        now = time.time()
        if now - last_heartbeat >= ECHO_HEARTBEAT_SEC:
            texts = _echo_texts(items)
            extra = next((t for t in texts if "波次" in t or "%" in t), "")
            logger.info(f"[白荆回廊·俱乐部] 回声猎区战斗挂机中… 页面={page} {extra}")
            last_heartbeat = now
        time.sleep(1.0)
    return False, f"战斗未收敛（{last_page}）", last_items




def _echo_boss_panel_confirmed(items: List[Dict]) -> bool:
    """boss 面板 = 标题「不协和音」+ ≥1 面板独有词（双证据，铁律禁单标题）。

    地图 boss 标签与面板标题同名；地图上点 boss 标题只展开「已收录」条（无
    面板独有词，不会误判）；战后总结页（出战队伍/重新降噪）无 boss 标题，
    同样不匹配——总结页要用「返回」退回面板。
    """
    texts = _echo_texts(items)
    if not any(ECHO_BOSS_TITLE in t for t in texts):
        return False
    return any(m in t for t in texts for m in ECHO_BOSS_PANEL_MARKS)




def _echo_read_top_score(items: List[Dict]) -> Optional[int]:
    """读 boss 面板「本期最高分贝」数值；读不到返回 None（调用方按开打处理）。

    实测 label 与数字常被 OCR 拆成两条（label (1559,580) / 数字 (1561,618)，
    同列 dy≈38）：先试 label 自带数字，再找 label 正下方 20~90px 的纯数字条目。
    """
    for it in items or []:
        text = str(it.get("text") or "")
        if "本期最高分贝" not in text:
            continue
        m = re.search(r"本期最高分贝\D*(\d+)", text)
        if m:
            return int(m.group(1))
        tx, ty = it.get("center") or (0, 0)
        for it2 in items or []:
            t2 = _normalize_text(str(it2.get("text") or ""))
            if not t2.isdigit():
                continue
            cx, cy = it2.get("center") or (0, 0)
            if abs(cx - tx) <= 80 and 20 <= cy - ty <= 90:
                return int(t2)
        return None
    return None




def _echo_boss_open_panel(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """从猎区地图打开 boss 面板：点 OCR 标签 → 面板双证据；未开再点图标兜底。

    11-14 实测：点标题只展开「已收录」条，完整面板要点 boss 图标——顺序固定
    「标题 → 图标」，图标比例只在标题点完仍无面板时才用。
    """
    hit = _ocr_best_match(logger, (ECHO_BOSS_TITLE,), hwnd, min_conf=0.55)
    if not hit:
        logger.warning("[白荆回廊·俱乐部] 回声猎区 boss：地图未定位「不协和音」标签")
        return False
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·俱乐部] 回声猎区 boss：点击 boss 标签 @ {hit['center']}")
    time.sleep(2.0)
    if _echo_boss_panel_confirmed(_scene_results(logger, hwnd)):
        return True
    _click_ratio(logger, hwnd, ECHO_BOSS_ICON_FALLBACK, "回声猎区 boss 图标")
    time.sleep(2.0)
    return _echo_boss_panel_confirmed(_scene_results(logger, hwnd))




def _echo_boss_wait_settled(
    logger: logging.Logger, hwnd, stop_check, deadline: float
) -> Tuple[bool, str, List[Dict]]:
    """boss 计分战收敛：作战完成点空白 → 总结页点「返回」→ 回 boss 面板算完成。

    boss 战后落点（11-14 实机）：「作战完成/有效分贝/总分贝」计分结算页（无
    「达成波次」，fitting result 分类不认）点空白 → 总结页「出战队伍/本轮分贝/
    重新降噪」（无 boss 标题，面板判定不认，必须单独分支）点左上「返回」→
    boss 面板。地图先到先收；战败页/奖励页/物品浮层按节点同款防御性覆盖
    （计分战理论不败，实测未覆盖败态）。返回 (ok, "boss_panel"|"map", items)。
    """
    from .baijing_fitting import (  # 延迟导入：fitting 懒引用本模块，防环
        _dismiss_item_popup,
        _page_now,
        P_RESULT_CONTINUE,
        P_REWARD_CONTINUE,
    )

    last_heartbeat = 0.0
    last_page, last_items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "用户停止", last_items
        page, items = _page_now(logger, hwnd)
        last_page, last_items = page, items
        texts = [_normalize_text(str(it.get("text") or "")) for it in items or []]
        if _echo_map_confirmed(items):
            return True, "map", items
        if _echo_boss_panel_confirmed(items):
            return True, "boss_panel", items
        if _echo_result_frame(items) or _echo_defeat_frame(items) or any(
            "作战完成" in t for t in texts if t
        ):
            _click_ratio(logger, hwnd, P_RESULT_CONTINUE, "回声猎区 boss 结算页点空白")
            time.sleep(2.0)
            continue
        if any("出战队伍" in t for t in texts if t):
            _click_ratio(logger, hwnd, ECHO_BOSS_BACK_FALLBACK, "回声猎区 boss 总结页点返回")
            time.sleep(2.0)
            continue
        if page == "reward":
            _click_ratio(logger, hwnd, P_REWARD_CONTINUE, "回声猎区 boss 奖励页继续")
            time.sleep(2.0)
            continue
        if page == "item_popup":
            _dismiss_item_popup(logger, hwnd)
            continue
        cont = next(
            (it for it in items or [] if "点击屏幕继续" in str(it.get("text") or "")),
            None,
        )
        if cont:
            _pyautogui_click_guarded(*cont["center"])
            time.sleep(1.5)
            continue
        now = time.time()
        if now - last_heartbeat >= ECHO_HEARTBEAT_SEC:
            logger.info(f"[白荆回廊·俱乐部] 回声猎区 boss 挂机中… 页面={page}")
            last_heartbeat = now
        time.sleep(1.0)
    return False, f"boss 战未收敛（{last_page}）", last_items




def _echo_boss_fight(logger: logging.Logger, hwnd, stop_check=None) -> Tuple[str, str]:
    """中央 boss 计分战：地图 → boss 面板 → 战斗闸门 → 开始降噪 → 收敛。

    战斗闸门（计分战无重复收益，别天天白打）：面板出现「重新降噪/快速降噪」
    （本期已打过）或「本期最高分贝」≥ 顶档 9000 → 跳过战斗只领奖励；重置期
    后分数归零闸门自动重开。编队页可能为空 → 「编队成员数量不足，是否自动
    编队？」弹窗点「确认」（OCR 精确匹配 + 比例兜底）后重等编队页。
    返回 (status, detail)，status ∈ fought / skipped / failed。
    """
    from .baijing_fitting import (  # 延迟导入：fitting 懒引用本模块，防环
        _click,
        _page_now,
        _wait_deploy_settled,
        _wait_page,
        BATTLE_START_MAX_ATTEMPTS,
        BATTLE_VERIFY_DELAY_SEC,
        P_BATTLE_START,
        P_TEAM_START,
        WAIT_DEPLOY,
        WAIT_TEAM,
    )

    ok, _items, _expiry = _echo_wait_map(logger, hwnd, stop_check)
    if not ok:
        return "failed", "boss：进页后未见猎区地图"
    if not _echo_boss_open_panel(logger, hwnd, stop_check):
        _record_step_failure(
            logger, hwnd, "俱乐部回声猎区:boss面板", "点标题/图标都未开出 boss 面板")
        return "failed", "boss：面板未打开"
    items = _scene_results(logger, hwnd)
    texts = [_normalize_text(str(it.get("text") or "")) for it in items]
    if any(m in t for t in texts for m in ECHO_BOSS_FIGHT_DONE_MARKS):
        logger.info("[白荆回廊·俱乐部] 回声猎区 boss：本期已打过（重新降噪/快速降噪在场）")
        return "skipped", "boss：本期已打过，只领奖励"
    top = _echo_read_top_score(items)
    if top is not None and top >= ECHO_BOSS_SCORE_TOP_TIER:
        logger.info(
            f"[白荆回廊·俱乐部] 回声猎区 boss：本期最高分贝 {top} 已达顶档，跳过战斗")
        return "skipped", f"boss：本期最高分贝 {top} 已达顶档，只领奖励"
    fight = _ocr_best_match(
        logger, ECHO_BOSS_FIGHT_TEXTS, hwnd,
        frac_region=(0.55, 0.80, 1.0, 1.0), min_conf=0.55,
    )
    if fight:
        _pyautogui_click_guarded(*fight["center"])
    else:
        _click_ratio(logger, hwnd, ECHO_BOSS_FIGHT_FALLBACK, "回声猎区 boss 开始降噪(比例兜底)")
    logger.info("[白荆回廊·俱乐部] 回声猎区 boss：已点「开始降噪」，等编队页")
    time.sleep(2.0)
    ok, page, _items = _wait_page(logger, hwnd, "team", WAIT_TEAM, stop_check)
    if not ok:
        # boss 编队页常为空：压着「编队成员数量不足」弹窗时点「确认」自动编队
        items = _scene_results(logger, hwnd)
        texts = [_normalize_text(str(it.get("text") or "")) for it in items]
        if not any(ECHO_AUTOFORM_TEXTS[0] in t for t in texts if t):
            return "failed", f"boss：编队页未就绪（{page}）"
        confirm = _pick_result(
            logger, hwnd, items, ("确认",),
            frac_region=(0.45, 0.55, 0.90, 0.80), min_conf=0.6, exact_only=True,
        )
        if confirm:
            _pyautogui_click_guarded(*confirm["center"])
        else:
            _click_ratio(
                logger, hwnd, ECHO_AUTOFORM_CONFIRM_FALLBACK, "回声猎区 boss 自动编队确认")
        logger.info("[白荆回廊·俱乐部] 回声猎区 boss：编队成员不足，已确认自动编队")
        time.sleep(2.5)
        ok, page, _items = _wait_page(logger, hwnd, "team", WAIT_TEAM, stop_check)
        if not ok:
            return "failed", f"boss：自动编队后编队页仍未就绪（{page}）"
    if not _click(logger, hwnd, P_TEAM_START, "作战开始"):
        return "failed", "boss：「作战开始」点击失败"
    deadline = time.time() + max(WAIT_DEPLOY, 10.0)
    entered, page = False, "unknown"
    while time.time() < deadline:
        if _stopped(stop_check):
            return "failed", "boss：用户停止"
        page, _items = _page_now(logger, hwnd)
        if page in ("deploy", "battle"):
            entered = True
            break
        time.sleep(1.0)
    if not entered:
        return "failed", f"boss：作战开始后未进布阵/战斗（{page}）"
    if page == "deploy":
        # 布阵稳定等待 + 「开始战斗」补点，与节点链同款（#283：点完当帧离开
        # 布阵页即算开打，战斗过程本身不检测）
        if not _wait_deploy_settled(logger, hwnd, stop_check):
            return "failed", "boss：用户停止"
        ok_started, last_page = False, "deploy"
        for attempt in range(1, BATTLE_START_MAX_ATTEMPTS + 1):
            if _stopped(stop_check):
                return "failed", "boss：用户停止"
            if not _click(logger, hwnd, P_BATTLE_START, "开始战斗"):
                return "failed", "boss：「开始战斗」点击失败"
            time.sleep(BATTLE_VERIFY_DELAY_SEC)
            last_page, _items = _page_now(logger, hwnd)
            if last_page in ("battle", "result") or last_page not in ("team", "deploy"):
                ok_started = True
                break
        if not ok_started:
            return "failed", f"boss：布阵页未离开（{last_page}）"
    ok, reason, _items = _echo_boss_wait_settled(
        logger, hwnd, stop_check, time.time() + ECHO_BOSS_SETTLE_TIMEOUT
    )
    if not ok:
        _record_step_failure(logger, hwnd, "俱乐部回声猎区:boss战斗", reason)
        return "failed", f"boss：{reason}"
    logger.info(f"[白荆回廊·俱乐部] 回声猎区 boss：计分战完成（{reason}）")
    return "fought", "boss：计分战完成"




def _echo_boss_claim_ladder(logger: logging.Logger, hwnd, stop_check=None) -> Tuple[int, str]:
    """领 boss 面板左侧「个人声量奖励」阶梯：OCR 档位金额 → 点右侧奖励图标。

    金额→图标实测偏移 (+118,+20)（「7000」(145,477) → 图标 (263,497)）；金额列
    限 x<0.20 避开右侧分贝/排名数值。OCR 认不出档位（布局改版）→ 固定比例
    兜底逐档点。已领档图标置灰，点击为无操作（幂等）。点击间隙处理「点击
    屏幕继续/新物品已存入」弹窗。返回 (点击档位数, detail)。
    """
    from .baijing_fitting import P_RESULT_CONTINUE  # 延迟导入，防环

    items = _scene_results(logger, hwnd)
    if not _echo_boss_panel_confirmed(items):
        return 0, "阶梯领取：不在 boss 面板"
    region = _abs_region(hwnd, ECHO_BOSS_LADDER_REGION)
    targets: List[Tuple[int, int]] = []
    for it in items or []:
        t = _normalize_text(str(it.get("text") or ""))
        if t not in ECHO_BOSS_LADDER_TIERS:
            continue
        cx, cy = it.get("center") or (0, 0)
        if region:
            ax, ay, aw, ah = region
            if not (ax <= cx <= ax + aw and ay <= cy <= ay + ah):
                continue
        targets.append((int(cx), int(cy)))
    targets.sort(key=lambda p: p[1])
    clicked = 0
    if targets:
        for tx, ty in targets:
            if _stopped(stop_check):
                break
            _pyautogui_click_guarded(
                tx + ECHO_BOSS_LADDER_CLICK_OFFSET[0], ty + ECHO_BOSS_LADDER_CLICK_OFFSET[1])
            clicked += 1
            logger.info(f"[白荆回廊·俱乐部] 回声猎区 boss：点击声量阶梯档位 @ ({tx},{ty})")
            time.sleep(1.5)
            for _ in range(3):  # 奖励弹窗收尾（新物品已存入 + 点击屏幕继续）
                items = _scene_results(logger, hwnd)
                cont = next(
                    (it for it in items
                     if "点击屏幕继续" in str(it.get("text") or "")), None)
                if cont:
                    _pyautogui_click_guarded(*cont["center"])
                    time.sleep(1.2)
                    continue
                texts = [_normalize_text(str(it.get("text") or "")) for it in items]
                if any("新物品已存入" in t for t in texts if t):
                    _click_ratio(logger, hwnd, P_RESULT_CONTINUE, "奖励浮层点空白")
                    time.sleep(1.2)
                    continue
                break
    else:
        logger.warning("[白荆回廊·俱乐部] 回声猎区 boss：阶梯金额未 OCR 命中，比例兜底逐档点")
        for fy in ECHO_BOSS_LADDER_FALLBACK:
            if _stopped(stop_check):
                break
            _click_ratio(logger, hwnd, fy, "回声猎区 boss 声量阶梯(比例兜底)")
            clicked += 1
            time.sleep(1.5)
    return clicked, f"声量阶梯点击 {clicked} 档"




def _echo_map_rewards_claim(logger: logging.Logger, hwnd, stop_check=None) -> Tuple[bool, str]:
    """领地图左下「猎区累计分贝奖励」：入口 OCR 定位 → 一键领取 → 关 X 回地图。

    入口禁比例盲点（活动改版入口可能消失）；奖励页双证据 = 标题「猎区累计
    分贝奖励」+「一键领取」（地图本体也有同名入口字样，单标题必误判）；
    「一键领取」幂等（已领齐时无操作）。返回 (是否完成, detail)。
    """
    hit = _ocr_best_match(logger, ECHO_MAP_REWARD_ENTRY, hwnd, min_conf=0.55)
    if not hit:
        return False, "地图未找到「猎区累计分贝奖励」入口（可能已改版）"
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·俱乐部] 回声猎区：点击分贝奖励入口 @ {hit['center']}")
    time.sleep(2.0)
    items = _scene_results(logger, hwnd)
    texts = [_normalize_text(str(it.get("text") or "")) for it in items]
    if not (
        any("猎区累计分贝奖励" in t for t in texts if t)
        and any("一键领取" in t for t in texts if t)
    ):
        _record_step_failure(
            logger, hwnd, "俱乐部回声猎区:分贝奖励页", "点入口后未见奖励页（标题+一键领取双证据不满足）")
        return False, "分贝奖励页未确认，不盲点"
    claim = _ocr_best_match(
        logger, ECHO_MAP_REWARD_CLAIM_ALL, hwnd,
        frac_region=(0.5, 0.75, 1.0, 1.0), min_conf=0.55,
    )
    if claim:
        _pyautogui_click_guarded(*claim["center"])
    else:
        _click_ratio(logger, hwnd, ECHO_MAP_REWARD_CLAIM_ALL_FALLBACK, "猎区分贝奖励一键领取(比例兜底)")
    logger.info("[白荆回廊·俱乐部] 回声猎区：已点「一键领取」")
    time.sleep(2.0)
    for _ in range(3):  # 领取弹窗收尾
        items = _scene_results(logger, hwnd)
        cont = next(
            (it for it in items if "点击屏幕继续" in str(it.get("text") or "")), None)
        if not cont:
            break
        _pyautogui_click_guarded(*cont["center"])
        time.sleep(1.2)
    _click_ratio(logger, hwnd, ECHO_MAP_REWARD_CLOSE_FALLBACK, "猎区分贝奖励页关闭")
    time.sleep(1.5)
    ok, _items, _exp = _echo_wait_map(logger, hwnd, stop_check, timeout=10.0)
    if not ok:
        return False, "关闭奖励页后未回猎区地图"
    return True, "猎区累计分贝奖励已一键领取"




def _echo_boss_and_map_rewards(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """boss 计分战（闸门跳过）→ 个人声量阶梯领取 → 地图左下分贝奖励。

    节点链之后执行（用户口径：abcde → boss → boss 页左侧奖励 → 左下角奖励）。
    boss/领奖失败不连坐节点链结果，分支层挂 result["boss"] 单独呈现；闸门
    决定打不打（本期分数达标/已打过 → 只领奖励），重置期后自动重开战斗。
    """
    out: Dict = {"status": "skipped", "detail": "", "fought": False, "claims": ""}
    if _stopped(stop_check):
        out["detail"] = "用户停止"
        return out
    parts: List[str] = []
    fight_status, fight_detail = _echo_boss_fight(logger, hwnd, stop_check)
    out["fought"] = fight_status == "fought"
    parts.append(fight_detail)
    if fight_status == "failed":
        out["status"] = "failed"
        # 失败恢复：尝试一次「返回」回地图（boss 面板/总结页的退出路径），
        # 让后面的地图奖励领取还有机会执行
        if not _echo_map_confirmed(_scene_results(logger, hwnd)):
            _click_ratio(logger, hwnd, ECHO_BOSS_BACK_FALLBACK, "boss 失败后尝试返回")
            time.sleep(2.0)
    # 阶梯领取：须在 boss 面板；不在（如战斗直接回图）则重开一次面板
    claimed, claim_detail = _echo_boss_claim_ladder(logger, hwnd, stop_check)
    if claimed == 0 and "不在 boss 面板" in claim_detail and fight_status != "failed":
        if _echo_boss_open_panel(logger, hwnd, stop_check):
            claimed, claim_detail = _echo_boss_claim_ladder(logger, hwnd, stop_check)
    parts.append(claim_detail)
    out["claims"] = claim_detail
    # 回地图领左下「猎区累计分贝奖励」
    map_ok = False
    if not _echo_map_confirmed(_scene_results(logger, hwnd)):
        _click_ratio(logger, hwnd, ECHO_BOSS_BACK_FALLBACK, "boss 面板返回猎区地图")
        time.sleep(2.0)
    if _echo_map_confirmed(_scene_results(logger, hwnd)):
        map_ok, map_detail = _echo_map_rewards_claim(logger, hwnd, stop_check)
        parts.append(map_detail)
    else:
        parts.append("未能回到猎区地图，跳过左下奖励领取")
    if fight_status != "failed" and not map_ok:
        out["status"] = "failed"
    if out["status"] != "failed":
        out["status"] = "success" if (out["fought"] or claimed or map_ok) else "skipped"
    out["detail"] = "；".join(p for p in parts if p)
    return out




def _echo_zone_fight_node(
    logger: logging.Logger, hwnd, letter: str, claim_xy, stop_check=None
) -> Tuple[bool, str]:
    """从点「开始收录」起打完一个节点：编队页 → 开战 → 挂机 → 回猎区地图。

    编队段复用拟合回归（用户 2026-11-13 拍板「编队选择页面可以复用拟合回归」）：
    同款编队页判定、「作战开始」、布阵稳定等待+补点。回声猎区可能没有布阵页
    （未实机探明），所以「作战开始」后同时等 deploy/battle 谁先出现。
    返回 (是否完成, 失败原因)。
    """
    from .baijing_fitting import (
        _click,
        _page_now,
        _wait_deploy_settled,
        _wait_page,
        BATTLE_START_MAX_ATTEMPTS,
        BATTLE_VERIFY_DELAY_SEC,
        P_BATTLE_START,
        P_TEAM_START,
        WAIT_DEPLOY,
        WAIT_TEAM,
    )

    _pyautogui_click_guarded(*claim_xy)
    logger.info(f"[白荆回廊·俱乐部] 回声猎区节点 {letter}：已点「开始收录」，等编队页")
    ok, page, _items = _wait_page(logger, hwnd, "team", WAIT_TEAM, stop_check)
    if not ok:
        return False, f"节点 {letter} 未进入编队页（{page}）"
    if not _click(logger, hwnd, P_TEAM_START, "作战开始"):
        return False, f"节点 {letter} 「作战开始」点击失败"
    deadline = time.time() + max(WAIT_DEPLOY, 10.0)
    page = "unknown"
    entered = False
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "用户停止"
        page, _items = _page_now(logger, hwnd)
        if page in ("deploy", "battle"):
            entered = True
            break
        time.sleep(1.0)
    if not entered:
        return False, f"节点 {letter} 点「作战开始」后未进布阵/战斗（{page}）"
    if page == "deploy":
        # 回声猎区战斗页对 OCR 是盲页（2026-11-13 #283 取证帧只有
        # 「01/02/uid/关卡号」4 个文本，拟合的「当前波次/战术终端」特征全无），
        # 「开始战斗」点完不能靠页面分类判进战斗——改判「布阵页已离开」：
        # 当帧不再是 team/deploy 即算开打（战斗自动挂机，收敛交给
        # _echo_wait_battle_settled 等回地图）。稳定等待沿用拟合的
        # _wait_deploy_settled（11-09 布阵按钮未稳点击被吞的教训）。
        if not _wait_deploy_settled(logger, hwnd, stop_check):
            return False, "用户停止"
        ok_started = False
        last_page = "deploy"
        for attempt in range(1, BATTLE_START_MAX_ATTEMPTS + 1):
            if _stopped(stop_check):
                return False, "用户停止"
            if not _click(logger, hwnd, P_BATTLE_START, "开始战斗"):
                return False, f"节点 {letter} 「开始战斗」点击失败"
            time.sleep(BATTLE_VERIFY_DELAY_SEC)
            last_page, _items = _page_now(logger, hwnd)
            if last_page in ("battle", "result") or last_page not in ("team", "deploy"):
                ok_started = True
                logger.info(
                    f"[白荆回廊·俱乐部] 回声猎区节点 {letter}：开始战斗第 {attempt} 次"
                    f"点击后布阵页已离开（页面={last_page}），按已进战斗处理"
                )
                break
            logger.warning(
                f"[白荆回廊·俱乐部] 回声猎区节点 {letter}：开始战斗第 "
                f"{attempt}/{BATTLE_START_MAX_ATTEMPTS} 次点击后仍停在「{last_page}」"
                f"{'，准备补点' if attempt < BATTLE_START_MAX_ATTEMPTS else ''}"
            )
        if not ok_started:
            return False, (
                f"节点 {letter} 布阵页未离开（{last_page}，"
                f"开始战斗已点 {BATTLE_START_MAX_ATTEMPTS} 次）"
            )
    ok, reason, _items = _echo_wait_battle_settled(
        logger, hwnd, stop_check, time.time() + ECHO_BATTLE_TIMEOUT
    )
    if not ok:
        return False, f"节点 {letter} {reason}"
    if reason == "map_defeated":
        # 战败回图（11-14 实机：失败页点空白 → 回猎区地图，节点未清）：绝不写
        # 已清标记——写了下次自动化直接跳过=永久漏奖励；顺序链在战败节点止步，
        # 报败交人工调整编队/刻印（E 节点实锤：编队缺【轻卫/铁御】时只部署
        # 1 人，战斗内 toast 提示出战后备同调者，几十秒即败）。
        logger.warning(f"[白荆回廊·俱乐部] 回声猎区节点 {letter} 战败，未收录")
        return False, f"节点 {letter} 战败（任务失败），未收录——需调整编队/刻印后重试"
    logger.info(f"[白荆回廊·俱乐部] 回声猎区节点 {letter} 战斗完成，已回猎区地图")
    return True, ""




def _echo_zone_run_content(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """回声猎区内容主体：确认地图 → 已清标记跳过 → A→E 逐节点打 → 回地图。

    顺序链口径（用户 2026-11-13「先打abcde按顺序打」）：节点解锁是链式的
    （11-13 截图：A 可打、B~E 带锁），某节点点开面板没有「开始收录」= 锁定/
    不可打，后面节点必然也没解锁，整链到此为止——这是正常收尾不是失败。
    每打完一个节点把字母写进已清标记（过期日取地图「重置时间剩余N天」）。
    中央 boss「不协和音·鱼叉将军」用户没让打，本轮不动。

    返回 {"status": success/skipped/failed, "detail", "fought", "skipped"}。
    """
    out: Dict = {"status": "failed", "detail": "", "fought": [], "skipped": []}
    ok, items, expiry = _echo_wait_map(logger, hwnd, stop_check)
    if not ok:
        out["detail"] = "进页后未见猎区地图（标题+独有词双证据不满足）"
        _record_step_failure(logger, hwnd, "俱乐部回声猎区:地图确认", out["detail"])
        return out
    marks = _echo_load_marks(logger)
    cleared = list(marks.get("cleared") or [])
    out["skipped"] = [c for c in ECHO_NODE_LETTERS if c in cleared]
    if out["skipped"]:
        logger.info(
            f"[白荆回廊·俱乐部] 回声猎区：已清标记跳过 {out['skipped']}"
            f"（period_end={marks.get('period_end')}）"
        )
    fought: List[str] = []
    chain_end = ""       # 非失败的顺序链终止原因
    failed = False
    for letter in ECHO_NODE_LETTERS:
        if _stopped(stop_check):
            chain_end = "用户停止"
            break
        if letter in cleared:
            continue
        items = _scene_results(logger, hwnd)
        if not _echo_map_confirmed(items):
            # 上一场打完可能停在过渡页/结算收尾，宽限一次重识别
            time.sleep(2.0)
            items = _scene_results(logger, hwnd)
            if not _echo_map_confirmed(items):
                failed = True
                out["detail"] = f"打节点 {letter} 前未回到猎区地图，节点链中止"
                _record_step_failure(
                    logger, hwnd, f"俱乐部回声猎区:节点{letter}前地图确认", out["detail"]
                )
                break
        labels = _echo_node_labels(hwnd, items)
        label = _echo_pick_label(labels, letter)
        if label is None:
            failed = True
            out["detail"] = f"节点 {letter} 标签未定位（地图布局可能变化，需补证据）"
            _record_step_failure(
                logger, hwnd, f"俱乐部回声猎区:节点{letter}定位", out["detail"]
            )
            break
        _pyautogui_click_guarded(*label["center"])
        logger.info(f"[白荆回廊·俱乐部] 回声猎区：点击节点 {letter} 标签 @ {label['center']}")
        claim = _echo_panel_claim(logger, hwnd, stop_check)
        if claim is None:
            chain_end = f"节点 {letter} 无「开始收录」（锁定或不可打），顺序链到此为止"
            logger.info(f"[白荆回廊·俱乐部] 回声猎区：{chain_end}")
            break
        ok, reason = _echo_zone_fight_node(
            logger, hwnd, letter, claim["center"], stop_check=stop_check
        )
        if not ok:
            failed = True
            out["detail"] = reason
            _record_step_failure(
                logger, hwnd, f"俱乐部回声猎区:节点{letter}战斗", reason
            )
            break
        fought.append(letter)
        cleared = sorted(set(cleared) | {letter})
        _echo_save_marks(logger, expiry, cleared)
    out["fought"] = fought
    if failed:
        out["status"] = "failed"          # detail 已在 break 处写好失败原因
    elif chain_end:
        out["status"] = "success" if fought else "skipped"
        out["detail"] = chain_end
    elif fought:
        out["status"] = "success"
        out["detail"] = f"节点链走完：本场打 {fought}，标记跳过 {out['skipped']}"
    else:
        out["status"] = "skipped"
        out["detail"] = "无可打节点（全部已清或未解锁）"
    return out




def _run_club_activity_echo_zone(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """俱乐部活动「回声猎区」分支（2026-11-11 入口，2026-11-13 内容落地）。

    在俱乐部页上检测右侧「俱乐部活动」卡片的「回声猎区」标题：命中 → 点击进
    猎区地图 → 按 A→E 顺序清节点（`_echo_zone_run_content`，编队链路复用拟合
    回归）→ 中央 boss 计分战 + 个人声量阶梯 + 地图左下分贝奖励
    （`_echo_boss_and_map_rewards`，2026-11-14 固化，用户口径 abcde → boss →
    boss 页左侧奖励 → 左下角奖励）→ 退出回主界面；未命中（活动结束/换期）→
    不点击直接跳过。节点链结果挂 result["echo"]、boss/领奖挂 result["boss"]，
    失败不连坐每日任务主状态（10-22 起口径）。
    """
    result: Dict = {
        "status": "skipped", "detail": "", "entered": False,
        "returned_main": None, "echo": None, "boss": None,
    }
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hit = _ocr_best_match(
        logger, CLUB_ACTIVITY_ECHO_TEXTS, hwnd,
        frac_region=CLUB_ACTIVITY_DETECT_REGION, min_conf=0.55,
    )
    if not hit:
        result["detail"] = "俱乐部活动未见「回声猎区」入口（活动未开或已结束）"
        logger.info(f"[白荆回廊·俱乐部] {result['detail']}")
        return result
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·俱乐部] 点击「回声猎区」活动入口 @ {hit['center']}")
    result["entered"] = True
    time.sleep(2.5)  # 俱乐部页 → 猎区地图过场（地图确认有自己的轮询等待）
    _capture_daily_list_snapshot(logger, hwnd, prefix=CLUB_ACTIVITY_SNAPSHOT_PREFIX)
    content = _echo_zone_run_content(logger, hwnd, stop_check=stop_check)
    result["echo"] = content
    result["status"] = content["status"]
    result["detail"] = f"回声猎区：{content['detail']}"
    if content["status"] == "failed":
        result["detail"] += "（分支失败不影响每日任务主状态）"
        logger.warning(f"[白荆回廊·俱乐部] {result['detail']}")
    else:
        logger.info(f"[白荆回廊·俱乐部] {result['detail']}")
    # boss + 奖励领取：闸门内只领奖励（本期分数达标/已打过），重置期自动重开
    # 战斗；失败只追加 detail 不改节点链状态（不连坐口径）。
    boss = _echo_boss_and_map_rewards(logger, hwnd, stop_check=stop_check)
    result["boss"] = boss
    result["detail"] += f"；boss：{boss['status']}（{boss['detail']}）"
    if boss["status"] == "failed":
        logger.warning(f"[白荆回廊·俱乐部] 回声猎区 boss：{boss['detail']}（不影响节点链结果）")
    if content["status"] == "failed":
        result["detail"] += "（分支失败不影响每日任务主状态）"
        logger.warning(f"[白荆回廊·俱乐部] {result['detail']}")
    else:
        logger.info(f"[白荆回廊·俱乐部] {result['detail']}")
    # 退出：猎区地图与俱乐部页共用左上「返回」，_leave_club_page 页无关
    # （主界面检测 + 返回/ESC 循环），可直接复用；失败时后续步骤各自
    # _ensure_main_ui 自保兜底（10-22 起的口径）。
    back_ok = _leave_club_page(logger, hwnd, stop_check=stop_check)
    result["returned_main"] = back_ok
    if not back_ok:
        result["status"] = "failed"
        result["detail"] = "回声猎区分支结束但未能退回主界面（需人工恢复）"
        logger.warning(f"[白荆回廊·俱乐部] {result['detail']}")
    return result




def claim_club_daily_reward(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """领俱乐部每日任务奖励（每日刷新，2026-10-18 用户截图实测）。

    返回 {"status", "detail"}：status = success / already / failed / skipped。
    """
    result = {"status": "skipped", "detail": ""}
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    # 2026-10-22 修复：同零售礼包，自己确保回到主界面，不再被 back_ok 连坐跳过
    if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过俱乐部")
        logger.info(f"[白荆回廊·俱乐部] {result['detail']}")
        return result
    if not _enter_club_page(logger, hwnd, stop_check=stop_check):
        result.update(status="failed", detail="未进入俱乐部页面")
        _leave_club_page(logger, hwnd, stop_check=stop_check)
        return result
    _capture_daily_list_snapshot(logger, hwnd, prefix=CLUB_SNAPSHOT_PREFIX)
    state = _claim_club_daily_task(logger, hwnd, stop_check=stop_check)
    if state == "purchased":
        result.update(status="success", detail="已领取俱乐部每日任务奖励")
    elif state == "already":
        result.update(status="already", detail="俱乐部每日任务今日已完成")
    elif state == "no_claim":
        result.update(
            status="skipped",
            detail="卡片无「领取」/「今日任务已完成」，今日可能无奖励可领（任务未完成或已领取）",
        )
    else:
        result.update(status="failed", detail="点击「领取」后卡片未变为「今日任务已完成」")
    # 俱乐部活动分支（回声猎区，2026-11-11）：仍在俱乐部页时顺手检测活动入口。
    # 只挂 result["activity"]，不改动每日任务主状态——活动分支失败不连坐领奖。
    if not _stopped(stop_check):
        result["activity"] = _run_club_activity_echo_zone(logger, hwnd, stop_check=stop_check)
    back_ok = _leave_club_page(logger, hwnd, stop_check=stop_check)
    result["returned_main"] = back_ok
    logger.info(f"[白荆回廊·俱乐部] 结果 {result['status']}：{result['detail']}，回主界面={back_ok}")
    return result


