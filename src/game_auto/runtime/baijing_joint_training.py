# -*- coding: utf-8 -*-
"""白荆回廊「联合训练场」活动入口侦察（2026-11-15 用户实机截图落地入口层）。

调用方：`baijing_flow.run_baijing_full_flow` 第 4.6 步（拟合回归之后）。
**phase 1 范围（用户 2026-11-15 拍板「先实装入口层」）**：只读侦察——
主界面 → 外勤作战中心 → 点「联合训练场」→ 防区页识别（未完成防区数 /
总进度 / 结算与解锁文案）→ 快照取证 → 退回主界面；**不点击任何防区 /
队伍槽位**。摆队（约束：**队长不可重复**）与每周结算领取等实机取证
（摆队页/结算页截图）后再做 phase 2。

玩法口径（用户 2026-11-15）：
- 活动「联合训练·捷速难逃」2026-12-21 00:00 结束（页面上有「结束」字样）。
- 3 防区各 3 槽位，摆队后**纯挂机推演**涨进度（无战斗），总进度 0%/180%。
- **每周结算**：截图里「6小时54分后结算」即距周一 04:00 周重置的剩余时间；
  结算领奖与重新摆队属 phase 2。
- 防区分期解锁（截图右缘「将于2026年11月18日04:00解…」），OCR 认不到
  属正常（窗口边缘截断），解锁文案解析只为日志信息。

页面判定遵守 docs/conventions/04-game-flows.md「页面判定铁律」：
- 外勤作战中心 hub 页复用 `baijing_fitting` 的 field 判定（FIELD_MARKERS
  已含「联合训练场」，policy 登记表已有），不新增判定。
- 防区页判定 `_is_joint_zone_page`：**标题含「联合训练」AND body 独有词
  ≥1**（防区未完成/防区推演进度）。hub 页的「联合训练场」是「联合训练」
  的 OCR 超集、单标题必误判（铁律），但 hub 永无 body 词——证据已在
  tests/suite_page_judgement_policy.py REGISTRY 登记。证据帧：
  `data/artifacts/ref_baijing_joint_{center,zones}_20261115.png`（OCR 转录
  存 `ref_baijing_joint_ocr_20261115.json`）。

坑与设计：
- 入口是六边形图标 + 右侧文字标签；**先点 OCR 命中的标签中心，等不到防区页
  再按比例点图标兜底**（echo boss「点标题 → 没面板再点图标」同款，非盲点：
  每次点击后都有页面验证，两次都进不去才落取证）。
- 活动过期（EVENT_END 之后）直接 skip，不进外勤页——活动结束入口可能消失。
- 失败不连坐：本分支由调用方 try 包裹，内部失败只落取证与 warning。
- 坐标口径：全部窗口比例（1080p 基准 /1920 /1080，来源 ref 截图实测），
  点击走 `_click_ratio` / `_pyautogui_click_guarded`，换分辨率不失效。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .baijing_common import (
    _capture_daily_list_snapshot,
    _click_ratio,
    _ensure_window,
    _ocr_best_match,
    _ratio_point,
    _stopped,
    _template_center,
)
from .baijing_scene import _ensure_main_ui
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import ocr_window_content
from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")
LOG_TAG = "[白荆回廊·联合训练场]"

FLOW_KEY = "BaiJingJointTraining"
EVENT_END = "2026-12-21"          # 活动「联合训练·捷速难逃」结束日（页面「结束」字样佐证）
MIN_CONF = 0.5

# ── 外勤作战中心 hub 页 → 联合训练场入口（ref_baijing_joint_center_20261115）──
HUB_ENTRY_TEXTS = ("联合训练场",)
HUB_ENTRY_REGION = (0.50, 0.50, 0.78, 0.66)   # 含标签 (0.636,0.570)；排 天隙勘测(0.710)/分进合击战(0.452)
# 六边形图标模板（裁自 ref 实机帧 (1042,580)-(1150,690)，避开会消失的道具角标；
# 2026-11-16 事故：静态比例点 (1047,605) 与标签点 (1220,616) 两次点击都未导航
# （弧形节点热区未知，拟合回归那种"标签整行可点"在此不成立）——改为模板匹配
# 定位图标真中心为主，OCR 标签/静态比例降级为兜底。
JOINT_ENTRY_TEMPLATE = str(TEMPLATES_DIR / "白荆回廊-联合训练场入口.png")
P_JOINT_ENTRY_LABEL = (1220 / 1920, 616 / 1080)   # OCR 实测标签中心（兜底 2）
P_JOINT_ENTRY_ICON = (1070 / 1920, 630 / 1080)    # 网格实测六边形中心（兜底 3，2026-11-16 修正）

# ── 防区页（ref_baijing_joint_zones_20261115）──
ZONE_TITLE_MARKERS = ("联合训练",)                # 标题「联合训练·捷速难逃」OCR 保留「·」
ZONE_BODY_MARKERS = ("防区未完成", "防区推演进度")  # 防区页独占（hub 页永无）
ZONE_NAVIGATED_MARKERS = ("捷速难逃", "返回")      # 已进入联合训练相关页但 body 词缺失时的现场特征
P_ZONE_BACK = (193 / 1920, 52 / 1080)             # 左上「返回」实测 (193,52)

WAIT_FIELD = 8.0        # 主界面 → 外勤 hub（复用 fitting 的 field 判定）
WAIT_ZONE = 12.0        # hub → 防区页过场
POLL_INTERVAL_SEC = 0.8  # 2026-11-16：缩短轮询间隔——短命页面（点击后被弹回）也要抓得到

PROGRESS_RE = re.compile(r"(\d{1,3})\s*%\s*/\s*(\d{1,3})\s*%")
UNLOCK_RE = re.compile(r"将于(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})")
COUNTDOWN_RE = re.compile(r"(\d+)\s*小时\s*(\d+)\s*分后结算")
EVENT_END_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})-\d{1,2}:\d{2}结束")


def _texts(items: Sequence[Dict]) -> List[str]:
    """OCR 行 → 置信度达标的文本列表（与 fitting 同口径）。"""
    return [
        str(it.get("text") or "")
        for it in (items or [])
        if float(it.get("confidence") or 0) >= MIN_CONF
    ]


def _is_joint_zone_page(items: Sequence[Dict]) -> bool:
    """防区页判定：标题含「联合训练」AND 防区页独占 body 词 ≥1。

    证据（ref_baijing_joint_zones_20261115 实测）：标题「联合训练·捷速难逃」
    conf 1.00、「防区未完成」×3 conf 1.00、「防区推演进度」conf 1.00；hub 页
    （ref_baijing_joint_center_20261115）有「联合训练场」（标题超集）但**永不**
    出现防区 body 词——双证据互斥成立。所有防区摆满/已领取时「防区未完成」
    消失，「防区推演进度」仍在，判定不退化。
    """
    texts = _texts(items)
    if not any("联合训练" in t for t in texts):
        return False
    return any(any(marker in t for t in texts) for marker in ZONE_BODY_MARKERS)


def _frame_signature(items: Sequence[Dict], limit: int = 4) -> str:
    """OCR 帧 → 摘要签名（前 N 条达标文本），用于区分轮询期间见过的不同画面。"""
    texts = _texts(items)[:limit]
    return " | ".join(texts) if texts else "(空白帧)"


def _wait_zone_page(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]],
    timeout: float = WAIT_ZONE,
) -> Tuple[bool, List[Dict], str]:
    """轮询等防区页双证据出现。

    返回 (ok, 最后当帧, 现场证据摘要)——摘要记录轮询期间见过的**所有不同画面**
    （2026-11-16 事故教训：点击后页面短暂出现又被弹回的话，盲等只留最后一帧
    hub，什么都没看到；逐帧记签名才能把"到底点进过什么"带回来）。
    """
    deadline = time.time() + max(1.0, float(timeout))
    seen: List[str] = []
    last_items: List[Dict] = []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, last_items, _frames_evidence(seen)
        items = ocr_window_content(logger, hwnd=hwnd) or []
        last_items = items
        if _is_joint_zone_page(items):
            return True, items, _frames_evidence(seen)
        sig = _frame_signature(items)
        if sig not in seen:
            seen.append(sig)
        time.sleep(POLL_INTERVAL_SEC)
    logger.warning(f"{LOG_TAG} 等防区页超时 {timeout:.0f}s")
    return False, last_items, _frames_evidence(seen)


def _frames_evidence(seen: Sequence[str], limit: int = 3) -> str:
    if not seen:
        return ""
    return "；轮询期间见过：" + "／".join(f"「{s}」" for s in seen[:limit])


def _locate_joint_entry(logger: logging.Logger, hwnd) -> List[Tuple[str, Tuple[int, int]]]:
    """三级定位联合训练场入口（hub 页稳定，一次算好整条点击链）。

    返回 [(方式, 屏幕绝对坐标), ...]：模板命中 → OCR 标签命中 → 静态比例。
    **模板与 OCR 都未命中时返回空表**——入口不存在/改版，静态比例点绝不盲点
    （禁盲点铁律：layout 变了比例点可能落在别处）。
    """
    chain: List[Tuple[str, Tuple[int, int]]] = []
    try:
        pt = _template_center(logger, hwnd, JOINT_ENTRY_TEMPLATE, frac_region=HUB_ENTRY_REGION)
        if pt:
            chain.append(("模板", pt))
    except Exception as exc:  # noqa: BLE001 - 模板定位异常降级 OCR
        logger.debug(f"{LOG_TAG} 模板定位异常（降级 OCR）: {exc}")
    hit = _ocr_best_match(
        logger, HUB_ENTRY_TEXTS, hwnd, frac_region=HUB_ENTRY_REGION, min_conf=0.6,
    )
    if hit:
        chain.append(("OCR标签", hit["center"]))
    if chain:
        ratio_pt = _ratio_point(logger, hwnd, P_JOINT_ENTRY_ICON)
        if ratio_pt:
            chain.append(("静态比例", ratio_pt))
    return chain


def parse_zone_state(items: Sequence[Dict]) -> Dict:
    """从防区页 OCR 帧解析状态（纯函数，测试矩阵直接喂转录帧）。

    返回 {"incomplete_zones", "progress", "event_end", "unlocks", "labels"}；
    读不到的字段为 None/空表——识别是侦察，不做任何点击。
    """
    texts = _texts(items)
    incomplete = sum(1 for t in texts if "防区未完成" in t)
    progress: Optional[Dict[str, int]] = None
    for t in texts:
        m = PROGRESS_RE.search(t)
        if m:
            progress = {"current": int(m.group(1)), "cap": int(m.group(2))}
            break
    event_end: Optional[str] = None
    for t in texts:
        m = EVENT_END_RE.search(t)
        if m:
            event_end = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            break
    unlocks = []
    joined = " ".join(texts)
    for m in UNLOCK_RE.finditer(joined):
        unlocks.append(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {m.group(4)}")
    return {
        "incomplete_zones": incomplete,
        "progress": progress,
        "event_end": event_end,
        "unlocks": unlocks,
        "labels": [t for t in texts if "联合训练" in t],
    }


def parse_hub_countdown(items: Sequence[Dict]) -> Optional[str]:
    """从外勤 hub 帧读「N小时M分后结算」原文（phase 2 结算对账用）。"""
    for t in _texts(items):
        if COUNTDOWN_RE.search(t):
            return t
    return None


def _record_failure(logger: logging.Logger, hwnd, step: str, detail: str, items) -> None:
    """失败取证：截图 + 当帧 OCR（flow_diagnostics，独立 flow_key 便于聚合）。"""
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key=FLOW_KEY,
            step=step,
            error=detail,
            hwnd=hwnd,
            game_key="baijing",
            save_image=bool(hwnd),
            ocr_results=items,
            extra={"kind": "baijing_joint_training"},
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不能拖垮主流程
        logger.debug(f"{LOG_TAG} 失败取证写入失败（已忽略）: {exc}")


def _leave_zone_page(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """退出防区页：点左上「返回」（OCR 优先 + 比例兜底）→ 确认回主界面。

    返回 True = 已回主界面；False = 需人工恢复（调用方据此标 failed，
    同回声猎区 `_leave_club_page` 失败口径）。中间态（回外勤 hub）由
    `_ensure_main_ui` 的检测-关闭-ESC 循环继续收。
    """
    try:
        hit = _ocr_best_match(
            logger, ("返回",), hwnd, frac_region=(0.0, 0.0, 0.25, 0.12), min_conf=0.5,
        )
        if hit:
            _pyautogui_click_guarded(*hit["center"])
            logger.info(f"{LOG_TAG} 点左上「返回」@ {hit['center']}")
        else:
            _click_ratio(logger, hwnd, P_ZONE_BACK, "防区页左上「返回」（比例兜底）")
        time.sleep(1.5)
    except Exception as exc:  # noqa: BLE001 - 退出异常交给 _ensure_main_ui 兜底
        logger.debug(f"{LOG_TAG} 点返回异常（交给主界面恢复兜底）: {exc}")
    ok = _ensure_main_ui(logger, hwnd, stop_check=stop_check)
    if not ok:
        logger.warning(f"{LOG_TAG} 退出防区页后未回到主界面（需人工恢复）")
    return ok


def run_joint_training_recon(logger: logging.Logger, hwnd, stop_check=None) -> Dict:
    """联合训练场入口侦察（只读，不点击防区/槽位）。

    返回 {"status", "detail", "entered", "returned_main", "zones", "countdown"}：
    status = success / skipped / failed；zones 为 parse_zone_state 结果
    （未进入页面时 None）。失败不连坐调用方主状态。
    """
    result: Dict = {
        "status": "skipped", "detail": "", "entered": False,
        "returned_main": None, "zones": None, "countdown": None,
    }
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    # 活动过期直接 skip：入口可能已消失，进外勤页纯属浪费（2026-12-21 结束）
    today = time.strftime("%Y-%m-%d")
    if today > EVENT_END:
        result["detail"] = f"活动已结束（{EVENT_END}），跳过联合训练场侦察"
        logger.info(f"{LOG_TAG} {result['detail']}")
        return result
    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result
    if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
        result.update(status="skipped", detail="多次尝试仍未回到主界面，跳过联合训练场")
        logger.info(f"{LOG_TAG} {result['detail']}")
        return result

    # 主界面 → 外勤作战中心（复用拟合回归的入口坐标与 field 页判定）
    from .baijing_fitting import P_FIELD_ENTRY, WAIT_FIELD, _wait_page  # lazy：避免环

    _click_ratio(logger, hwnd, P_FIELD_ENTRY, "外勤")
    ok, page, hub_items = _wait_page(logger, hwnd, "field", WAIT_FIELD, stop_check)
    if not ok:
        result.update(status="failed", detail=f"外勤页未打开（停在 {page}）")
        _record_failure(logger, hwnd, "外勤页", result["detail"], hub_items)
        _ensure_main_ui(logger, hwnd, stop_check=stop_check)
        result["returned_main"] = True
        return result
    result["countdown"] = parse_hub_countdown(hub_items)

    # hub → 联合训练场入口：三级定位（模板 → OCR 标签 → 静态比例），逐点点击
    # 并验证；模板与 OCR 都未命中 = 入口不存在/改版，**skip 不点击**（禁盲点）。
    chain = _locate_joint_entry(logger, hwnd)
    if not chain:
        result["detail"] = "外勤页未见「联合训练场」入口（活动未开/已结束/版本改版）"
        logger.info(f"{LOG_TAG} {result['detail']}")
        return result
    result["entered"] = True
    zone_ok, items, evidence = False, [], ""
    for attempt, (how, pt) in enumerate(chain, 1):
        _pyautogui_click_guarded(*pt)
        logger.info(f"{LOG_TAG} 点击「联合训练场」入口（{how}）@ {pt} [{attempt}/{len(chain)}]")
        time.sleep(1.0)  # 过场起步，随即进入逐帧轮询（短命页面也抓得到）
        zone_ok, items, evidence = _wait_zone_page(logger, hwnd, stop_check)
        if zone_ok:
            break
        logger.info(f"{LOG_TAG} {how} 点击未见防区页，尝试下一个定位点")
    if not zone_ok:
        result.update(
            status="failed",
            detail=f"点击入口后未见防区页（防区未完成/推演进度均未出现，已试 "
                   f"{'/'.join(h for h, _ in chain)}）{evidence}",
        )
        _record_failure(logger, hwnd, "防区页", result["detail"], items)
        result["returned_main"] = _ensure_main_ui(logger, hwnd, stop_check=stop_check)
        if not result["returned_main"]:
            result["detail"] += "；且未退回主界面（需人工恢复）"
        return result

    _capture_daily_list_snapshot(logger, hwnd, prefix="joint_training")
    zones = parse_zone_state(items)
    result["zones"] = zones
    result["status"] = "success"
    result["detail"] = (
        f"未完成防区 {zones['incomplete_zones']} 个，"
        f"总进度 {zones['progress']['current']}%/{zones['progress']['cap']}%"
        if zones["progress"] else f"未完成防区 {zones['incomplete_zones']} 个"
    )
    if zones["event_end"]:
        result["detail"] += f"，活动 {zones['event_end']} 结束"
    logger.info(f"{LOG_TAG} 侦察完成：{result['detail']}")

    back_ok = _leave_zone_page(logger, hwnd, stop_check=stop_check)
    result["returned_main"] = back_ok
    if not back_ok:
        result["status"] = "failed"
        result["detail"] += "；分支结束但未退回主界面（需人工恢复）"
    return result
