# -*- coding: utf-8 -*-
"""白荆回廊·任务列表扫描与领取（2026-11-14 自 baijing_daily.py 拆出）。

职责：日常任务列表的打开/关闭/扫描/找行/领取——open_daily_task_list、
scan_daily_task_states（逐屏 OCR + 静态目录比对）、_find_row_button（行按钮
定位 + 滚动重试）、_claim_rewards_midway、_return_to_task_panel。

数据流：daily 的 process_task_list 按目录逐项调度；专项 handler
（baijing_task_handlers）从本模块取行按钮/列表守卫原语。依赖方向
tasklist → scene（recover_scene/_ensure_main_ui）→ common，禁反向。

不变量与坑：
- 静态目录 DAILY_TASK_CATALOG 只用于跑流程与兜底核算，**活跃度必须以
  面板大数字为准**（_read_activity_number），目录分值会随版本漂移。
- 「前往」类任务只记录不点击（跳转子活动需专门逻辑）。
- 列表特征词禁含「任务」二字（_ocr_best_match 反向子串规则会假阳性，
  2026-10-21 实锤）。
"""
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from ..settings.paths import TEMPLATES_DIR
from .baijing_common import (
    MAX_OPEN_ATTEMPTS,
    SCROLL_POST_DELAY,
    TASK_LIST_CORE_MARKERS,
    TASK_ROW_REGION,
    TEMPLATE_MATCH_THRESHOLD,
    TEMPLATE_REFERENCE_WIDTH,
    _abs_region,
    _capture_daily_list_snapshot,
    _dismiss_overlay,
    _ensure_window,
    _ocr_best_match,
    _pick_result,
    _ratio_point,
    _record_step_failure,
    _row_region_signature,
    _same_click_target,
    _scroll_list_at,
    _scroll_list_with_verify,
    _stopped,
    _template_center,
    _wheel_post,
    _wheel_real,
)
from .baijing_scene import _ensure_main_ui, recover_scene
from .baijing_flow import _focus_game_window, _window_region, detect_baijing_main_ui
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, ocr_window_content


# ── 「任务」按钮模板（1920x1080 参考分辨率裁剪，红色角标已抹除） ──
TASK_BUTTON_TEMPLATE = str(TEMPLATES_DIR / "白荆回廊-任务按钮.png")


# 「任务」按钮 OCR 搜索区域（窗口相对比例 fx0, fy0, fx1, fy1）：右侧竖排按钮区。
# 必须限定右侧——主界面中部「阶段任务154/200」也含「任务」二字。
TASK_BUTTON_REGION = (0.70, 0.45, 1.00, 0.92)


# OCR 命中文本黑名单：含这些字样的跳过（防误点「阶段任务」）
TASK_TEXT_EXCLUDE = ("阶段",)



# 日常任务列表打开后的特征文字（任一命中即认为列表已打开）。
# **只放面板独有强特征，且任何候选都不能含「任务」二字**——2026-10-21 实锤：
# 候选里放「日常任务/任务列表/距离任务重置」等含"任务"的词会被 _ocr_best_match
# 的反向子串规则击穿：主界面右下角「任务」按钮文字只有 2 字，规则
# "OCR 文本 ∈ 候选 且 ≥2 字符"让它命中任何含"任务"的长候选，
# _wait_task_list 假阳性判"列表已打开"→ 后续找行全部空转（当天 3/15 根因之一）。
# 「一键领取/全部领取/领取奖励」只在列表面板出现，主界面没有，保留安全。
TASK_LIST_MARKERS = (
    "累计活跃度", "主线指引", "一键领取", "全部领取", "领取奖励",
)


# 列表核心特征已上收 baijing_common（scene/handlers 也要用，2026-11-14 拆分；
# 本模块从 common import）。


# 列表内领取类按钮（先具体后泛化，防「一键领取」被当成单个「领取」）
CLAIM_ALL_TEXTS = ("一键领取", "全部领取", "一键全领")


CLAIM_TEXTS = ("领取",)


# 搜索单个「领取」时要排除的字样（避免点到「一键领取」上重复计数）。
# 「已领取」是行内**状态文字**不是按钮——2026-10-25 实锤：领取循环把 (750,840) 的
# 「已领取」标签当「领取」点了 3+ 轮，画面毫无变化，白烧轮次（一轮≈14s）。
# 真实按钮文字「领取」「领取奖励」都不含"已领取"子串，排除无副作用。
CLAIM_EXCLUDE = ("一键", "全部", "已领取")


# 「前往」类任务：只记录不点击（跳转子活动需专门逻辑，暂不自动执行）
GOTO_TEXTS = ("前往",)



# ── 日常任务静态目录（2026-10-18 从游戏内实测提取；任务项/分值会随版本漂移） ──
# 2026-10-27 实锤：游戏当天是「完成2次战斗」，目录写的是「完成3次战斗」。
# 目录只用于跑流程和兜底核算，**活跃度记录必须以面板大数字为准**（见
# _read_activity_number）；发现对不上时先看 daily_scan_*_日期.jpg 逐屏快照。
# 每项: key=内部标识, name=任务名, activity=活跃度分值, match=OCR 匹配关键词。
# OCR 实测坑：「午后茶歇」会被识别成「午后茶憩」、「记忆烙痕提升」会被识别成
# 「记忆熔痕提开」，关键词一律取稳定片段。
# 研发室任务的徽章是紫角无数字样式，分值 10 由用户 2026-10-18 确认。
DAILY_TASK_CATALOG: List[Dict] = [
    {"key": "login",              "name": "登录游戏",                              "activity": 5,   "match": ["登录游戏"]},
    # 2026-10-25 实跑截图：日常里同时存在「完成3次战斗」(3/3, 10) 和
    # 「完成5次战斗」(4/5, 20)。2026-10-27 用户指出战斗项每天可能是 2/3/4 次
    # 中的一种（不会同时显示）：当天列表实际是「完成2次战斗 (0/2)」，旧目录
    # 只认「完成3次战斗」→ 匹配不到/靠 OCR 误读成 3/3。这里把 2/3/4 归到
    # 同一个 10 分槽位（key 保留 battle_3 兼容历史数据），5 次战斗仍是独立 20 分。
    # 4 次战斗的分值尚未实测，暂按 10；真实活跃度以面板大数字为准。
    {"key": "battle_3",           "name": "完成2/3/4次战斗",                       "activity": 10,  "match": ["完成2次战斗", "完成3次战斗", "完成4次战斗"]},
    {"key": "battle_5",           "name": "完成5次战斗",                           "activity": 20,  "match": ["完成5次战斗"]},
    {"key": "friend_medal",       "name": "赠送1次友情勋章",                       "activity": 10,  "match": ["友情勋章", "勋章"]},
    {"key": "excellent_road",     "name": "完成1次「卓越道路」",                   "activity": 10,  "match": ["卓越道路"]},
    {"key": "kill_60",            "name": "累计击败60名敌方目标",                  "activity": 10,  "match": ["击败60", "击败"]},
    {"key": "lab_harvest",        "name": "在白荆穹顶的初级研发室或高级研发室收获1次产物", "activity": 10,  "match": ["研发室"]},
    {"key": "tea_break",          "name": "邀请任一同调者共享一次「午后茶歇」",      "activity": 15,  "match": ["午后茶"]},
    {"key": "gallery_tour",       "name": "完成1次回廊漫巡",                       "activity": 20,  "match": ["回廊漫巡", "漫巡"]},
    {"key": "imaging_action",     "name": "完成1次任意难度的「显影行动」",          "activity": 5,   "match": ["显影行动"]},
    {"key": "memory_exp",         "name": "为记忆烙痕提升1次经验",                  "activity": 15,  "match": ["经验"]},
    {"key": "skill_upgrade",      "name": "同调者的任意非异核技能升级1次",          "activity": 10,  "match": ["非异核", "技能升级"]},
    {"key": "free_trade",         "name": "完成1次任意难度的「自由贸易」",          "activity": 5,   "match": ["自由贸易"]},
    {"key": "material_guarantee", "name": "完成1次「定向物资保障」",                "activity": 5,   "match": ["物资保障"]},
    {"key": "litho_protocol",     "name": "完成1次「光刻协议」",                    "activity": 5,   "match": ["光刻协议"]},
    {"key": "restroom_mini_game", "name": "游玩一次休息室中任一小游戏",             "activity": 5,   "match": ["小游戏"]},
]


# 每日活跃度理论总分（2026-10-18 实测：研发室徽章无数字但分值 10，用户确认）。
# 实得可超过领满线（用户实测跑完日常到 130）。
DAILY_ACTIVITY_TOTAL = sum(t["activity"] for t in DAILY_TASK_CATALOG if t["activity"])


# 每日活跃度领满线：里程碑奖励为 20/40/60/80/100 五档，满 100 即领完全部
# 日常里程碑奖励（叶脉联结页「日常任务累计活跃度达到100」任务可印证，2026-10-18
# 用户确认）。进度百分比按 领满线/100 计算，实得超过 100 按 100 记。
DAILY_ACTIVITY_TARGET = 100


# task_key → activity 快速查找
DAILY_ACTIVITY_BY_KEY = {t["key"]: t["activity"] for t in DAILY_TASK_CATALOG}



# 任务行区域已上收 baijing_common（common 的滚动签名也要用，2026-11-14 拆分）。


# 「累计活跃度」大数字区域（窗口相对比例）：每日任务面板顶部的圆形数字。
# 2026-10-27 实测 480x270 验证图中心 (148,66) → (0.308,0.244)，960x540 列表
# 快照同位置；区域内只有大数字，不会碰到左侧 tab / 下方里程碑奖励卡。
ACTIVITY_NUMBER_REGION = (0.22, 0.15, 0.42, 0.34)


# 面板特征：只有这些字样在屏上才认为大数字来自每日任务面板。
# 防止面板被关掉后，把主界面/其它页面同区域里的数字误当活跃度。
ACTIVITY_PANEL_MARKERS = ("累计活跃度", "主线指引", "距离任务重置", "一键领取")


# 活跃度合理上限：当前版本理论总分 160，留余量；防止把道具图标上的 1000
# 或其它界面数字当成活跃度。
ACTIVITY_NUMBER_MAX = 300


# 面板数字有入场动画（刚打开时会从 0 涨到真实值），多次读取取非零最大值。
ACTIVITY_NUMBER_ATTEMPTS = 3


ACTIVITY_NUMBER_INTERVAL = 0.8


# 左侧 tab 列区域（用于确保停在「日常」页）
TAB_COLUMN_REGION = (0.0, 0.15, 0.28, 0.85)


# 滚动扫描：鼠标悬停位置取列表区中心，每次滚动量与最大滚动次数
# TASK_SCROLL_STEPS 语义 = 滚轮格数（负数向下），经 _wheel_real 逐格发送 ±120
TASK_SCROLL_STEPS = -3


TASK_SCROLL_MAX = 5




def _read_activity_number(logger: logging.Logger, hwnd) -> Optional[int]:
    """读取每日任务面板顶部的「累计活跃度」大数字（游戏真实值）。

    数据流：全窗 OCR（遵守 ocr_click_tester「不裁剪识别」的约定）→ 按
    ACTIVITY_NUMBER_REGION 过滤 center → 只接受纯数字 token（被拆成多个
    数字块时按 x 拼接）→ 多次读取取非零最大值。返回 None 表示读不到，
    调用方退回静态目录核算，绝不写假值。

    2026-10-27 背景：静态目录把当天已完成任务算成 90，游戏面板实际 105
    （五档里程碑全部已领取）。目录会随版本漂移（当天游戏是「完成2次战斗」，
    目录写的是「完成3次战斗」），面板数字才是权威口径；480x270 的压缩
    验证图上 RapidOCR 已能读出 105（conf≈0.71），实时窗口更大更稳。
    """
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return None
    abs_region = _abs_region(hwnd, ACTIVITY_NUMBER_REGION)
    if not abs_region:
        return None
    ax0, ay0, aw, ah = abs_region
    readings: List[int] = []
    for attempt in range(ACTIVITY_NUMBER_ATTEMPTS):
        if attempt:
            time.sleep(ACTIVITY_NUMBER_INTERVAL)
        try:
            results = ocr_window_content(logger, hwnd=hwnd)
        except Exception as exc:
            logger.debug(f"[白荆回廊·日常] 面板活跃度 OCR 失败: {exc}")
            continue
        # 先确认当前屏确实是每日任务面板，避免面板被关掉后误读其它数字
        texts = [_normalize_text(str(r.get("text") or "")) for r in results]
        if not any(
            any(marker in text for marker in ACTIVITY_PANEL_MARKERS)
            for text in texts
        ):
            logger.debug("[白荆回廊·日常] 未识别到每日任务面板特征，跳过活跃度读取")
            continue
        tokens: List[Tuple[float, str, float]] = []
        for r in results:
            center = r.get("center")
            if not center:
                continue
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
            text = _normalize_text(str(r.get("text") or ""))
            if re.fullmatch(r"\d{1,3}", text):
                tokens.append((center[0], text, float(r.get("confidence") or 0)))
        if not tokens:
            continue
        # 优先用字符数最多的数字 token（105 优先于被截断的 10/05），同长度看置信度；
        # 没有多位 token 时兼容被拆成 1/0/5 三块的情况。
        multi = [t for t in tokens if len(t[1]) >= 2]
        if multi:
            multi.sort(key=lambda t: (-len(t[1]), -t[2], -int(t[1])))
            readings.append(int(multi[0][1]))
            continue
        joined = "".join(t[1] for t in sorted(tokens, key=lambda t: t[0]))
        if 1 <= len(joined) <= 3:
            value = int(joined)
            if 0 <= value <= ACTIVITY_NUMBER_MAX:
                readings.append(value)
    positives = [v for v in readings if v > 0]
    if not positives:
        if readings:
            logger.warning(
                f"[白荆回廊·日常] 面板活跃度只读到 0（可能仍在入场动画）{readings}，"
                f"退回目录核算"
            )
        return None
    chosen = max(positives)
    logger.info(f"[白荆回廊·日常] 面板累计活跃度读取: {readings} → 采用 {chosen}")
    return chosen




def _find_task_button(logger: logging.Logger, hwnd, stop_check=None):
    """定位「任务」按钮：OCR 优先（结果限定右侧区域），模板兜底。返回 ((x, y), 方式) 或 None。"""
    hit = _ocr_best_match(
        logger, ("任务",), hwnd, frac_region=TASK_BUTTON_REGION, min_conf=0.55, exclude=TASK_TEXT_EXCLUDE
    )
    if hit:
        center = hit.get("center")
        if center:
            return (int(center[0]), int(center[1])), "ocr"
    center = _template_center(logger, hwnd, TASK_BUTTON_TEMPLATE, frac_region=TASK_BUTTON_REGION)
    if center:
        return (int(center[0]), int(center[1])), "template"
    return None




def _wait_task_list(
    logger: logging.Logger,
    hwnd,
    timeout: float = 8.0,
    stop_check=None,
    snapshot: bool = True,
) -> bool:
    """等待日常任务列表打开（OCR 特征文字）。

    snapshot=False 时跳过快照落盘（process_task_list 的每任务快检用，
    避免每轮覆盖 daily_list 快照）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        hit = _ocr_best_match(logger, TASK_LIST_MARKERS, hwnd, min_conf=0.55)
        if hit:
            logger.info(
                f"[白荆回廊·日常] 任务列表已打开（特征文字 {hit['text']!r} @ {hit['center']}）"
            )
            if snapshot:
                _capture_daily_list_snapshot(logger, hwnd)
            return True
        time.sleep(1.0)
    return False




def _ensure_task_list_open(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    timeout: float = 3.0,
) -> bool:
    """确认任务列表在屏，不在则重开（2026-10-22，治 10-21 空转根因）。

    10-21 实锤：研发室收取后面板被关回主界面，但 _wait_task_list 曾被主界面
    「任务」按钮文字骗过（假阳性），后续每个任务的找行都在错误界面上滚动
    6 次 ×3.5s 空转。现在每个任务处理前都强特征快检，丢了就重开列表。
    """
    if _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False):
        return True
    logger.warning("[白荆回廊·日常] 任务列表不在屏，尝试重新打开")
    if open_daily_task_list(logger, hwnd, stop_check=stop_check):
        return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    # 重开失败通常不是"列表按钮找不到"，而是界面卡在某个子页面（2026-10-24 烙痕详情页）：
    # 先 ESC/关闭恢复到主界面，再从主界面重开，别让后续任务一路空转跳过。
    logger.warning("[白荆回廊·日常] 重开任务列表失败，先恢复主界面再重试")
    # 茶憩残留的休息室/关注特写里根本没有「任务」按钮，先按场景登记表离开再重开
    # （2026-10-26 12:51 实锤：不处理则每个任务都"重开失败→跳过"，整轮只剩 2/16）
    state, acted = recover_scene(logger, hwnd, stop_check=stop_check, context="重开任务列表")
    if acted or state in ("main", "panel"):
        if open_daily_task_list(logger, hwnd, stop_check=stop_check):
            return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    if _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        if open_daily_task_list(logger, hwnd, stop_check=stop_check):
            return _wait_task_list(logger, hwnd, timeout=timeout, stop_check=stop_check, snapshot=False)
    return False




def _parse_task_rows(logger: logging.Logger, results: List[Dict]) -> List[Dict]:
    """    把全窗 OCR 结果按行聚类成任务行（行间距约 108px，±40px 聚类安全）。
    区域过滤由调用方（scan_daily_task_states）做；行内文本按 x 排序拼接——
    实测「完成1次「卓越道路」」的进度「(1/1)」常被 OCR 拆成独立文本框。
    """
    rows: List[Dict] = []
    for r in results:
        try:
            cx, cy = float(r["center"][0]), float(r["center"][1])
        except Exception:
            continue
        placed = False
        for row in rows:
            if abs(row["y"] - cy) <= 40:
                row["items"].append(r)
                row["n"] += 1
                row["y"] = (row["y"] * (row["n"] - 1) + cy) / row["n"]
                placed = True
                break
        if not placed:
            rows.append({"y": cy, "n": 1, "items": [r]})
    parsed = []
    for row in rows:
        items = sorted(row["items"], key=lambda i: i["center"][0])
        parsed.append({
            "y": row["y"],
            "text": "".join(str(i.get("text") or "") for i in items),
            "items": items,
        })
    return parsed




def _match_rows_to_catalog(logger: logging.Logger, rows: List[Dict]) -> Dict[str, Dict]:
    """把任务行匹配到静态目录，解析 (X/Y) 进度与按钮状态。

    done 判定：进度 X>=Y，或行内有「领取奖励」按钮（完成待领）。
    """
    states: Dict[str, Dict] = {}
    for row in rows:
        tnorm = _normalize_text(row["text"])
        if not tnorm:
            continue
        for task in DAILY_TASK_CATALOG:
            if not any(_normalize_text(k) in tnorm for k in task["match"]):
                continue
            m = re.search(r"(\d+)\s*/\s*(\d+)", tnorm)
            cur, need = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
            has_claim = any(
                "领取" in _normalize_text(str(i.get("text") or ""))
                and "一键" not in _normalize_text(str(i.get("text") or ""))
                for i in row["items"]
            )
            states[task["key"]] = {
                "done": bool(cur >= need or has_claim),
                "progress": f"{cur}/{need}",
                "claimable": has_claim,
                "activity": task["activity"],
            }
            break
    return states




def scan_daily_task_states(
    logger: logging.Logger,
    hwnd,
    stop_check=None,
    max_scrolls: int = TASK_SCROLL_MAX,
    capture_prefix: Optional[str] = None,
) -> Dict[str, Dict]:
    """滚动任务列表并逐屏 OCR，聚合日常任务的状态。

    每屏识别后与静态目录匹配，直到找齐或滚动耗尽；每次滚动后用 OCR 签名
    验证列表内容真的变了，无效即止损（见 _scroll_list_with_verify）。
    capture_prefix 非空时每屏额外存一张快照（daily_scan_1_日期.jpg …），
    用于事后核对目录与游戏实际任务/分值的差异（2026-10-27 本地 90 vs 游戏 105）。
    返回 {task_key: {"done", "progress", "claimable", "activity"}}。
    """
    states: Dict[str, Dict] = {}
    for i in range(max_scrolls + 1):
        if _stopped(stop_check):
            break
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            break
        results_all = ocr_window_content(logger, hwnd=hwnd)
        results = results_all
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            ax0, ay0, aw, ah = region
            results = [
                r for r in results
                if r.get("center")
                and ax0 <= r["center"][0] <= ax0 + aw
                and ay0 <= r["center"][1] <= ay0 + ah
            ]
        rows = _parse_task_rows(logger, results)
        found = _match_rows_to_catalog(logger, rows)
        new_keys = [k for k in found if k not in states]
        states.update(found)
        if capture_prefix:
            _capture_daily_list_snapshot(
                logger, hwnd, prefix=f"{capture_prefix}_{i + 1}"
            )
        logger.info(
            f"[白荆回廊·日常] 任务扫描第 {i + 1} 屏: 识别 {len(found)} 行, "
            f"新增 {len(new_keys)} 个任务, 累计 {len(states)}/{len(DAILY_TASK_CATALOG)}"
        )
        # 首屏零行 + 全窗 OCR 连列表强特征都没有 = 扫的根本不是任务列表
        # （2026-10-25 事故：面板被误关后重扫对着非列表页滚了 6 屏全 0 行、
        # ~25s 纯浪费）。复用本屏已有的全窗 OCR 结果判断，零额外识别开销；
        # 立即止损并落 flow_failures，别再往下滚。
        if i == 0 and not found:
            all_texts = [str(r.get("text") or "") for r in results_all]
            if not any(
                any(m in t for m in TASK_LIST_CORE_MARKERS) for t in all_texts
            ):
                logger.error(
                    "[白荆回廊·日常] 首屏 0 行且全窗无列表强特征（累计活跃度/主线指引），"
                    "当前界面不是任务列表，中止扫描"
                )
                _record_step_failure(
                    logger, hwnd, "日常任务扫描",
                    "首屏 0 行且无列表强特征：扫描时界面不是任务列表",
                )
                break
        if len(states) >= len(DAILY_TASK_CATALOG):
            break
        if i >= max_scrolls:
            break
        # 向下滚动一屏并验证内容真的变了（悬停列表区中心，每轮现算）。
        # 滚动无效时不再烧后续屏——2026-10-18/19 的教训：pyautogui.scroll(-3)
        # 的 dwData 不足一格滚轮，6 屏 OCR 完全相同纯属浪费。
        region = _abs_region(hwnd, TASK_ROW_REGION)
        if region:
            scroll_pos = (region[0] + region[2] // 2, region[1] + region[3] // 2)
        else:
            scroll_pos = (960, 540)
        logger.info(
            f"[白荆回廊·日常] 列表下滚 {abs(TASK_SCROLL_STEPS)} 格 @ {scroll_pos}"
        )
        if not _scroll_list_with_verify(logger, hwnd, TASK_SCROLL_STEPS):
            logger.warning(
                "[白荆回廊·日常] 滚动后列表内容无变化（真实滚轮 + PostMessage 均试过），"
                "已到底部或滚动未生效，停止继续扫描"
            )
            break
    if len(states) < len(DAILY_TASK_CATALOG):
        missing = [t["name"] for t in DAILY_TASK_CATALOG if t["key"] not in states]
        logger.warning(
            f"[白荆回廊·日常] 扫描覆盖不全 ({len(states)}/{len(DAILY_TASK_CATALOG)}): "
            f"未识别 {missing}（列表滚动可能无效）"
        )
    return states




def _ensure_daily_tab(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """确保任务面板停在「日常」页：看「累计活跃度」在不在，不在则点左侧「日常」tab。"""
    if _stopped(stop_check):
        return False
    if _ocr_best_match(logger, ("累计活跃度",), hwnd, min_conf=0.55):
        return True
    tab = _ocr_best_match(
        logger, ("日常",), hwnd, frac_region=TAB_COLUMN_REGION, min_conf=0.5, exclude=("周常",)
    )
    if not tab:
        logger.warning("[白荆回廊·日常] 未找到「日常」tab，可能面板未打开")
        return False
    _pyautogui_click_guarded(*tab["center"])
    logger.info(f"[白荆回廊·日常] 点击左侧「日常」tab @ {tab['center']}")
    # tab 切换有 1-2s 过渡；原实现固定等 1.2s 只查一次，2026-10-20 实操中
    # 任务面板默认停在「叶脉联结」，点日常后检查失败但页面随后已切到日常。
    for _ in range(3):
        time.sleep(1.2)
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        if _ocr_best_match(logger, ("累计活跃度",), hwnd, min_conf=0.55):
            return True
    return False




def _find_row_button_once(
    logger: logging.Logger, hwnd, row_keyword: str, button_text: str
) -> Optional[Tuple[int, int]]:
    """当前屏内定位任务行按钮：先找含 row_keyword 的行，再取同行(±45px)的 button_text。"""
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
    row_y = None
    for r in results:
        if _normalize_text(row_keyword) in _normalize_text(str(r.get("text") or "")):
            row_y = r["center"][1]
            break
    if row_y is None:
        return None
    btn = _normalize_text(button_text)
    for r in results:
        text = _normalize_text(str(r.get("text") or ""))
        if btn in text and abs(r["center"][1] - row_y) <= 45:
            logger.info(
                f"[白荆回廊·日常] 定位到 {row_keyword!r} 行的 {button_text!r} 按钮 @ {r['center']}"
            )
            return (int(r["center"][0]), int(r["center"][1]))
    return None




def _scroll_row_list_to_top(logger: logging.Logger, hwnd) -> bool:
    """把任务列表滚到顶部（发送足够多的向上滚轮，顶部会 clamp）。"""
    region = _abs_region(hwnd, TASK_ROW_REGION)
    if not region:
        return False
    sx, sy = region[0] + region[2] // 2, region[1] + region[3] // 2
    # 扫描最多下滚 TASK_SCROLL_MAX 次；回顶按“满量 + 2 屏”发，确保从底部也能 clamp 到顶。
    notches = abs(TASK_SCROLL_STEPS) * (TASK_SCROLL_MAX + 2)
    logger.info(f"[白荆回廊·日常] 回滚任务列表到顶部（向上 {notches} 格）")
    ok = _wheel_real(logger, hwnd, sx, sy, notches)
    if not ok:
        ok = _wheel_post(logger, hwnd, sx, sy, notches)
    if ok:
        time.sleep(SCROLL_POST_DELAY)
    return ok




def _find_row_button(
    logger: logging.Logger, hwnd, row_keyword: str, button_text: str, stop_check=None
) -> Optional[Tuple[int, int]]:
    """定位任务行内的按钮：先看当前屏，否则回顶部并逐屏校验扫描整个列表。

    扫描阶段会把列表滚到底部，而每个「前往」处理器返回面板时的滚动位置不确定。
    2026-10-20 的教训：回顶部只发 6 格（扫描累计下滚了 12 格），第 3 行的
    研发室永远不在视野里；下层战斗行也只试 3 屏 + 不校验的 PostMessage。
    现在改为：足量回顶 → 逐屏下滚并用 OCR 校验内容真的变化 → 到底后再回顶重扫一次。
    """
    if _stopped(stop_check):
        return None
    pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
    if pos:
        return pos
    if not _abs_region(hwnd, TASK_ROW_REGION):
        logger.warning(f"[白荆回廊·日常] 未找到含 {row_keyword!r} 的任务行")
        return None
    # 1) 足量回顶（顶部 clamp，滚过头无害）
    if not _scroll_row_list_to_top(logger, hwnd):
        logger.warning(f"[白荆回廊·日常] 无法回滚任务列表顶部，放弃定位 {row_keyword!r}")
        return None
    pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
    if pos:
        return pos
    # 2) 从顶部逐屏下滚；每屏用 OCR 校验列表真的移动，滚到底再回顶重扫一次
    retried_from_top = False
    moved_screens = 0
    for attempt in range(1, TASK_SCROLL_MAX + 2):
        if _stopped(stop_check):
            return None
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return None
        moved = _scroll_list_with_verify(logger, hwnd, TASK_SCROLL_STEPS)
        if not moved:
            # 已到底（或滚动未生效）。若还没重扫过，回顶再完整扫一轮。
            if retried_from_top or moved_screens >= TASK_SCROLL_MAX:
                break
            retried_from_top = True
            logger.info(
                f"[白荆回廊·日常] 已扫到列表底部仍未找到 {row_keyword!r}，回顶重扫一轮"
            )
            if not _scroll_row_list_to_top(logger, hwnd):
                break
            pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
            if pos:
                return pos
            continue
        moved_screens += 1
        logger.info(
            f"[白荆回廊·日常] 未找到 {row_keyword!r} 行，列表下滚重试 ({attempt}/{TASK_SCROLL_MAX + 1})"
        )
        pos = _find_row_button_once(logger, hwnd, row_keyword, button_text)
        if pos:
            return pos
    logger.warning(f"[白荆回廊·日常] 滚动后仍未找到 {row_keyword!r} 行的 {button_text!r} 按钮")
    return None




def _return_to_task_panel(logger: logging.Logger, hwnd, stop_check=None, max_attempts: int = 5) -> bool:
    """从子页面逐层返回日常任务面板：已在面板 → 主界面则重开「任务」→ 否则 ESC 退一层。

    「确认」必须防原地死循环：2026-10-24 记忆烙痕强化后返回时，页面上有个不生效的
    「确认」文字，原实现每轮都点它、从不按 ESC，界面卡在烙痕页 2 分钟，后续任务全跳过。
    同一坐标连续命中说明上次点击没改变界面，本轮改走 ESC。
    """
    last_confirm: Optional[Tuple[int, int]] = None
    last_back: Optional[Tuple[int, int]] = None
    last_cont: Optional[Tuple[int, int]] = None
    for attempt in range(1, max_attempts + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            return False
        # 1) 已在任务面板？
        if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
            return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
        # 2) 在主界面 → 重开任务面板
        is_main, _ = detect_baijing_main_ui(logger, hwnd, budget_sec=1.5)
        if is_main:
            _focus_game_window(logger, hwnd)
            found = _find_task_button(logger, hwnd, stop_check)
            if found:
                (x, y), method = found
                _pyautogui_click_guarded(x, y)
                logger.info(f"[白荆回廊·日常] 重新点击「任务」按钮 [{method}] @ ({x},{y})")
                time.sleep(2.0)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 2.4) 场景登记表：认出是哪个界面就跑它的专属退出流程
        # （休息室/关注特写这类界面吃不到 ESC，2026-10-26 实锤：连按 5 次空转）
        state, acted = recover_scene(logger, hwnd, stop_check=stop_check, context="返回任务面板")
        if state == "panel":
            return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
        if state == "main" or acted:
            continue
        # 3) 过场动画遮罩：先点掉「点击屏幕继续」，否则底部按钮都点不到
        cont = _ocr_best_match(logger, ("点击屏幕继续",), hwnd, min_conf=0.5)
        if cont:
            pos = (int(cont["center"][0]), int(cont["center"][1]))
            if not _same_click_target(pos, last_cont):
                last_cont = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 返回途中点击「点击屏幕继续」@ {pos}")
                time.sleep(1.5)
                continue
            # 同一坐标反复出现 = 点不动的静态文字，落到下面走 ESC
        # 4) 退出确认类弹窗（如「将离开休息室，是否确认退出?」）→ 点「确认」
        confirm = _ocr_best_match(logger, ("确认",), hwnd, min_conf=0.6)
        if confirm:
            pos = (int(confirm["center"][0]), int(confirm["center"][1]))
            if _same_click_target(pos, last_confirm):
                logger.warning(
                    "[白荆回廊·日常] 「确认」点击后界面未变化（疑似静态文字），本轮改按 ESC"
                )
            else:
                last_confirm = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 点击退出确认框「确认」@ {pos}")
                time.sleep(1.5)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 5) 页面左上角「返回」按钮（烙痕列表页/休息室等 ESC 不灵时用）
        back = _ocr_best_match(
            logger, ("返回",), hwnd, frac_region=(0.0, 0.0, 0.20, 0.15), min_conf=0.55
        )
        if back:
            pos = (int(back["center"][0]), int(back["center"][1]))
            if _same_click_target(pos, last_back):
                logger.warning("[白荆回廊·日常] 「返回」点击后界面未变化，本轮改按 ESC")
            else:
                last_back = pos
                _pyautogui_click_guarded(*pos)
                logger.info(f"[白荆回廊·日常] 点击页面左上角「返回」@ {pos}")
                time.sleep(1.5)
                if _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55):
                    return _ensure_daily_tab(logger, hwnd, stop_check=stop_check)
                continue
        # 6) ESC 退一层（不清 last_confirm：同一坐标的「确认」本次返回不再重复点）
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        logger.info(f"[白荆回廊·日常] 按 ESC 返回上一层 (#{attempt}/{max_attempts})")
        time.sleep(1.5)
    return _ocr_best_match(logger, TASK_LIST_CORE_MARKERS, hwnd, min_conf=0.55) is not None




def open_daily_task_list(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """从主界面点击「任务」并确认日常任务列表打开（含切到日常页）。"""
    for attempt in range(1, MAX_OPEN_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            logger.error("[白荆回廊·日常] 游戏窗口丢失，无法打开任务列表")
            return False
        _focus_game_window(logger, hwnd)
        found = _find_task_button(logger, hwnd, stop_check)
        if not found:
            logger.warning(f"[白荆回廊·日常] 第 {attempt}/{MAX_OPEN_ATTEMPTS} 次：未找到「任务」按钮")
            time.sleep(1.5)
            continue
        (x, y), method = found
        _pyautogui_click_guarded(x, y)
        logger.info(f"[白荆回廊·日常] 已点击「任务」按钮 [{method}] @ ({x},{y})")
        if _wait_task_list(logger, hwnd, timeout=8.0, stop_check=stop_check):
            # 面板可能停在其它 tab，确保切到「日常」
            if _ensure_daily_tab(logger, hwnd, stop_check=stop_check):
                _capture_daily_list_snapshot(logger, hwnd)
                return True
            logger.warning("[白荆回廊·日常] 任务面板已打开但未能切到日常页")
            return False
        logger.warning(
            f"[白荆回廊·日常] 第 {attempt}/{MAX_OPEN_ATTEMPTS} 次点击后未确认任务列表打开，重试"
        )
        time.sleep(1.5)
    return False




def _claim_rewards_midway(logger: logging.Logger, hwnd, stop_check=None) -> int:
    """中途领取：点固定底栏的「一键领取」并处理领取后的奖励弹窗。

    用户 2026-10-18 建议：每个「前往」类任务完成后顺手领一次，避免已完成奖励
    堆积把后面的任务行挤到可视区外。底栏「一键领取」是固定件（任何滚动位置都
    可见），所以中途领取不需要滚动页面；弹窗用模板关 + 屏幕中心随意点双保险
    （无弹窗时中心点落在任务行文字上，无副作用）。
    返回点击次数（0=当前无可领）。
    """
    if _stopped(stop_check):
        return 0
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        return 0
    hit = _ocr_best_match(logger, CLAIM_ALL_TEXTS, hwnd, min_conf=0.6)
    if not hit:
        return 0
    _pyautogui_click_guarded(*hit["center"])
    logger.info(f"[白荆回廊·日常] 中途「一键领取」@ {hit['center']}")
    time.sleep(1.5)
    # 奖励弹窗处理：先试模板（点击屏幕继续/关闭），再屏幕中心随意点
    _dismiss_overlay(logger, hwnd)
    time.sleep(0.5)
    region = _window_region(hwnd)
    if region:
        cx, cy = region[0] + region[2] // 2, region[1] + region[3] // 2
        _pyautogui_click_guarded(cx, cy)
        logger.info(f"[白荆回廊·日常] 领取后屏幕中心随意点（关可能存在的奖励弹窗）@ ({cx},{cy})")
        time.sleep(0.8)
    return 1




def close_task_list(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """关闭任务列表并确认回到主界面。

    先走 _ensure_main_ui（检测→关闭→ESC）；3 轮没回主界面的中间态，
    2026-10-22 起再试一次「重开任务列表 → 关闭」——列表面板有可靠的关闭
    按钮模板，重开刷新状态比继续盲按 ESC 更有效（10-21 实锤 ESC/点关闭
    对中间态无效，close 判失败连带跳过俱乐部/礼包）。
    """
    if _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        return True
    if _stopped(stop_check):
        return False
    logger.info("[白荆回廊·日常] 直接关闭失败，尝试重开任务列表再关闭")
    if open_daily_task_list(logger, hwnd, stop_check=stop_check):
        return _ensure_main_ui(logger, hwnd, stop_check=stop_check, attempts=2)
    return False


