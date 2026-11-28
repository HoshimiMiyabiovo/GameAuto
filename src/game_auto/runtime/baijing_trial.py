"""白荆回廊「角色试玩（试玩关卡）」周期活动（2026-11-19 用户截图接入）。

入口链（用户 11-19 截图 ref_baijing_trial_20261119.png 实证页面本体）：
  主界面「活动中心」→ 活动页左列「试玩关卡」→ 试玩页（右侧角色缩略图条，
  NEW 徽标 y≈0.233；页面双证据「开始试玩」+「体验角色完成关卡领取奖励」）。
战斗链复用拟合回归（用户口述「点进去就是正常的编队战斗页面」）：
  开始试玩 → 编队页「作战开始」→ 布阵页「开始战斗」→ 挂机 → 结算点空白
  → 回试玩页（结算签出现前可能有「新物品已存入」领奖弹窗）。
周期：用户 11-19 口述「每14天刷新」——由 `baijing_flow._maybe_run_trial_periodic`
按 meta 日期门控（TRIAL_CYCLE_DAYS）。

⚠ v1 边界（未实机验证，首次实跑后按取证帧校正）：
  - 槽位 5 个、坐标由 1919x1079 截图量取；多于 5 个角色需要滚动，v1 不处理。
  - 「已完成/已领取」状态 UI 未取证：页面不满足双证据（或槽位点开不在试玩页）
    一律跳过该槽位，绝不盲点；整链在意外页面停下并留取证图。
  - 「获取同调者」是抽卡入口，任何情况下都不点（FORBIDDEN_TEXTS 防呆）。
  - 结算页判据沿用拟合「作战完成 + 达成波次」；若试玩结算文案不同会落超时，
    失败帧会带当页 OCR 供校正。

页面判定遵守 04 分篇「页面判定铁律」：每页 ≥2 专属标记，登记表见
tests/suite_page_judgement_policy.py，文本快照+截图矩阵见
tests/suite_baijing_trial_20261119.py。
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .baijing_common import _ocr_best_match, _pyautogui_click_guarded, _window_region
from .baijing_fitting import (
    BATTLE_START_MAX_ATTEMPTS,
    BATTLE_TIMEOUT_SEC,
    BATTLE_VERIFY_DELAY_SEC,
    WAIT_TEAM,
    _click,
    _dismiss_item_popup,
    _has,
    _is_battle_page,
    _is_deploy_page,
    _is_item_popup_page,
    _is_main_page,
    _is_result_page,
    _is_reward_page,
    _is_team_page,
    _ocr,
    _page_now,
    _stopped,
    _wait_deploy_settled,
    _wait_page,
    P_BATTLE_START,
    P_RESULT_CONTINUE,
    P_REWARD_CONTINUE,
    P_TEAM_START,
)

LOGGER = logging.getLogger("GameAutomation")
LOG_TAG = "[白荆回廊·角色试玩]"

MIN_CONF = 0.5

# 活动页（活动中心 hub）左列菜单专属标记；「联合训练场」故意不收——外勤页
# 同词会串（04 分篇页面判定铁律）。trial 页也带这组左列，必须先判 trial。
HUB_MARKERS = ("荣耀巡航", "叶脉联结", "分进合击战", "成长投资", "新手教学", "烙痕限时满级")
TRIAL_MARKERS = ("开始试玩", "体验角色完成关卡领取奖励")
FORBIDDEN_TEXTS = ("获取同调者",)   # 抽卡入口，绝不点

# ── 1080p 基准坐标 → 窗口比例（2026-11-19 用户截图 1919x1079 量取）──────
P_MAIN_ACTIVITY = "活动中心"                     # 主界面底栏，OCR 定位
P_TRIAL_TAB = (0.092, 0.672)                     # 左列「试玩关卡」
P_BACK = (0.101, 0.047)                          # 左上角「返回」
P_START_TRIAL = (0.849, 0.788)                   # 「开始试玩」按钮（OCR 优先，比例兜底）
SLOT_POINTS = tuple(                            # 角色缩略图条 5 槽（NEW 徽标下方）
    (x, 0.310) for x in (0.674, 0.738, 0.798, 0.861, 0.924)
)
# OCR 区域（窗口比例，x0,y0,x1,y1）——全窗 OCR 后按区域过滤，不做裁剪再识别
# （04 分篇教训）。必须与 _abs_region 及其它白荆模块的约定一致：11-25 #505 /
# 11-27 / 11-28 #536 角色试玩「打开活动中心」三连挂，就是这里曾按 (x,y,宽,高)
# 写、被 _abs_region 当 (x0,y0,x1,y1) 解释算出零面积区域，把 1.00 置信度的
# 「活动中心」候选全滤掉（该步无比例兜底 → 每跑必失败）。
REGION_ACTIVITY_BTN = (0.55, 0.86, 1.00, 1.00)
REGION_TRIAL_TAB = (0.00, 0.55, 0.22, 0.72)
REGION_START_BTN = (0.72, 0.72, 1.00, 0.86)
REGION_CHAR_NAME = (0.60, 0.48, 0.95, 0.60)

WAIT_HUB = 10.0
WAIT_TRIAL = 12.0
WAIT_BACK_TRIAL = 25.0
SLOT_SETTLE_SEC = 0.8
BATTLE_HEARTBEAT_SEC = 20
CLEANUP_MAIN_ATTEMPTS = 2

TRIAL_CYCLE_DAYS = 14
TRIAL_META_KEY = "periodic_flow:BAIJING_TRIAL"


def _texts(items: Sequence[Dict]) -> List[str]:
    return [
        str(it.get("text") or "")
        for it in (items or [])
        if float(it.get("confidence") or 0) >= MIN_CONF
    ]


def _is_trial_page(items) -> bool:
    """试玩页：「开始试玩」按钮 AND「体验角色完成关卡领取奖励」正文（双证据）。"""
    texts = _texts(items)
    return _has(texts, TRIAL_MARKERS[0]) and _has(texts, TRIAL_MARKERS[1])


def _is_hub_page(items) -> bool:
    """活动中心 hub 页：左列菜单专属标记 ≥3，且不是试玩页本体。"""
    if _is_trial_page(items):
        return False
    texts = _texts(items)
    return sum(1 for m in HUB_MARKERS if _has(texts, m)) >= 3


TRIAL_PAGE_CHECKS = (
    ("item_popup", _is_item_popup_page),
    ("reward", _is_reward_page),
    ("result", _is_result_page),
    ("battle", _is_battle_page),
    ("deploy", _is_deploy_page),
    ("team", _is_team_page),
    ("trial", _is_trial_page),
    ("hub", _is_hub_page),
    ("main", _is_main_page),
)


def classify_trial_page(items) -> str:
    """互斥分类：试玩链各页越具体越先判，unknown 绝不盲点。"""
    for name, fn in TRIAL_PAGE_CHECKS:
        if fn(items):
            return name
    return "unknown"


def _trial_page_now(logger: logging.Logger, hwnd) -> Tuple[str, List[Dict]]:
    items = _ocr(logger, hwnd)
    return classify_trial_page(items), items


def _wait_trial_page(
    logger: logging.Logger,
    hwnd,
    want: str,
    timeout: float,
    stop_check: Optional[Callable[[], bool]],
    also_ok: Sequence[str] = (),
) -> Tuple[bool, str, List[Dict]]:
    deadline = time.time() + max(1.0, float(timeout))
    last_page, last_items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "stopped", last_items
        page, items = _trial_page_now(logger, hwnd)
        last_page, last_items = page, items
        if page == want or page in also_ok:
            return True, page, items
        time.sleep(1.0)
    logger.warning(f"{LOG_TAG} 等「{want}」超时 {timeout:.0f}s，停在「{last_page}」")
    return False, last_page, last_items


def _record_failure(logger: logging.Logger, hwnd, step: str, detail: str, items) -> None:
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="BaiJingTrial",
            step=step,
            error=detail,
            hwnd=hwnd,
            game_key="baijing",
            save_image=bool(hwnd),
            ocr_results=items,
            extra={"kind": "baijing_trial_periodic"},
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不能拖垮主流程
        logger.debug(f"{LOG_TAG} 失败取证写入失败（已忽略）: {exc}")


def _fail(logger, hwnd, step, detail, items, out):
    logger.error(f"{LOG_TAG} {step}: {detail}")
    _record_failure(logger, hwnd, step, detail, items)
    out.update(ok=False, detail=f"{step}: {detail}", step=step)
    return out


def _ocr_click(
    logger: logging.Logger,
    hwnd,
    keyword: str,
    label: str,
    frac_region: Tuple[float, float, float, float],
    fallback: Optional[Tuple[float, float]] = None,
) -> bool:
    """OCR 定位点击（区域限定），未命中走比例兜底；FORBIDDEN 入口直接拒点。"""
    if keyword in FORBIDDEN_TEXTS:
        logger.error(f"{LOG_TAG} 拒点抽卡入口「{keyword}」")
        return False
    hit = _ocr_best_match(logger, [keyword], hwnd, frac_region=frac_region,
                          min_conf=MIN_CONF, exclude=FORBIDDEN_TEXTS)
    if hit and hit.get("center"):
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"{LOG_TAG} 点击{label}「{hit.get('text')}」@ {hit['center']}")
        return True
    if fallback is not None:
        return bool(_click(logger, hwnd, fallback, label))
    logger.warning(f"{LOG_TAG} OCR 未命中{label}「{keyword}」（区域 {frac_region}），不盲点")
    return False


def _read_char_name(logger: logging.Logger, hwnd, items) -> str:
    """读试玩页右侧角色名（仅用于日志/汇总，读不到返回空串）。"""
    region = _window_region(hwnd)
    if not region:
        return ""
    left, top, width, height = region
    rx0, ry0, rx1, ry1 = REGION_CHAR_NAME
    best = ""
    best_conf = 0.0
    for it in items or []:
        conf = float(it.get("confidence") or 0)
        if conf < MIN_CONF or not it.get("center"):
            continue
        fx = (it["center"][0] - left) / width if width else -1.0
        fy = (it["center"][1] - top) / height if height else -1.0
        if rx0 <= fx <= rx1 and ry0 <= fy <= ry1 and conf > best_conf:
            best, best_conf = str(it.get("text") or ""), conf
    return best


def _start_battle_flexible(
    logger: logging.Logger, hwnd, stop_check: Optional[Callable[[], bool]]
) -> Tuple[bool, str, List[Dict], int]:
    """布阵页 → 开打：稳定等待 → 点击 → 延迟复核；判据「进战斗/结算或已离开布阵」。

    与拟合 `_start_battle_with_retry` 同源；额外接受「布阵页已离开」（11-13 #283
    口径：部分战斗页对 OCR 是盲页，不能只认 battle 页特征）。
    """
    if not _wait_deploy_settled(logger, hwnd, stop_check):
        return False, "stopped", [], 0
    last_page, last_items = "deploy", []
    for attempt in range(1, BATTLE_START_MAX_ATTEMPTS + 1):
        if _stopped(stop_check):
            return False, "stopped", last_items, attempt - 1
        if not _click(logger, hwnd, P_BATTLE_START, "开始战斗"):
            return False, last_page, last_items, attempt
        time.sleep(BATTLE_VERIFY_DELAY_SEC)
        page, items = _trial_page_now(logger, hwnd)
        last_page, last_items = page, items
        if page in ("battle", "result") or page not in ("team", "deploy"):
            logger.info(f"{LOG_TAG} 开始战斗第 {attempt} 次点击生效（页面={page}）")
            return True, page, items, attempt
        logger.warning(
            f"{LOG_TAG} 开始战斗第 {attempt}/{BATTLE_START_MAX_ATTEMPTS} 次点击后仍停在"
            f"「{page}」{'，准备补点' if attempt < BATTLE_START_MAX_ATTEMPTS else ''}"
        )
    return False, last_page, last_items, BATTLE_START_MAX_ATTEMPTS


def _wait_battle_settled(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]],
    timeout: float = BATTLE_TIMEOUT_SEC,
) -> Tuple[bool, str, List[Dict]]:
    """挂机等到结算/领奖弹窗；战斗页出现过之后再回试玩页也按已结算处理。"""
    deadline = time.time() + timeout
    seen_battle = False
    last_heartbeat = 0.0
    last_page, last_items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "stopped", last_items
        page, items = _trial_page_now(logger, hwnd)
        last_page, last_items = page, items
        if page in ("result", "reward", "item_popup"):
            return True, page, items
        if page == "battle":
            seen_battle = True
        elif page == "trial" and seen_battle:
            logger.info(f"{LOG_TAG} 战斗结束直接回到试玩页（未见结算页）")
            return True, "trial", items
        now = time.time()
        if now - last_heartbeat >= BATTLE_HEARTBEAT_SEC:
            logger.info(f"{LOG_TAG} 挂机中… 页面={page}")
            last_heartbeat = now
        time.sleep(1.0)
    return False, last_page, last_items


def _back_to_trial_page(
    logger: logging.Logger,
    hwnd,
    page: str,
    items,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str, List[Dict]]:
    """结算页/领奖弹窗 → 回到试玩页；可能有 结算→领奖→试玩 多段过场。"""
    if page == "trial":
        return True, page, items
    if page == "result":
        if not _click(logger, hwnd, P_RESULT_CONTINUE, "结算页点空白"):
            return False, page, items
    elif page == "reward":
        if not _click(logger, hwnd, P_REWARD_CONTINUE, "点击屏幕继续"):
            return False, page, items
    elif page == "item_popup":
        _dismiss_item_popup(logger, hwnd)
    deadline = time.time() + WAIT_BACK_TRIAL
    last_page, last_items = page, items
    while time.time() < deadline:
        if _stopped(stop_check):
            return False, "stopped", last_items
        page, items = _trial_page_now(logger, hwnd)
        last_page, last_items = page, items
        if page == "trial":
            return True, page, items
        if page == "reward":
            if not _click(logger, hwnd, P_REWARD_CONTINUE, "点击屏幕继续"):
                return False, page, items
        elif page == "item_popup":
            _dismiss_item_popup(logger, hwnd)
        elif page == "result":
            if not _click(logger, hwnd, P_RESULT_CONTINUE, "结算页点空白"):
                return False, page, items
        time.sleep(1.0)
    logger.warning(f"{LOG_TAG} 等回试玩页超时，停在「{last_page}」")
    return False, last_page, last_items


def _run_single_slot(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]],
    out: Dict,
) -> str:
    """当前试玩页槽位：开始试玩 → 编队/布阵 → 挂机 → 回页。

    返回 "done"（完成一场）/ "skipped"（不可打，原地不动）/ "failed"（硬失败，已取证）。
    """
    if not _ocr_click(logger, hwnd, "开始试玩", "开始试玩", REGION_START_BTN,
                      fallback=P_START_TRIAL):
        _fail(logger, hwnd, "开始试玩", "按钮定位失败", [], out)
        return "failed"
    ok, page, items = _wait_page(logger, hwnd, "team", WAIT_TEAM, stop_check)
    if not ok:
        if page == "trial":
            # 槽位不可打（未开放/已领完等）时点击可能原地不动——跳过该槽，不算失败
            logger.info(f"{LOG_TAG} 点「开始试玩」后仍在试玩页，跳过该槽位")
            return "skipped"
        _fail(logger, hwnd, "编队页", f"未进入编队页（{page}）", items, out)
        return "failed"
    if not _click(logger, hwnd, P_TEAM_START, "作战开始"):
        _fail(logger, hwnd, "作战开始", "点击失败", [], out)
        return "failed"
    # 作战开始后可能出现布阵页或直接进战斗（与拟合/回声同款兼容处理）
    deadline = time.time() + 20.0
    page, items = "unknown", []
    while time.time() < deadline:
        if _stopped(stop_check):
            out.update(ok=False, detail="用户停止", step="作战开始")
            return "failed"
        page, items = _trial_page_now(logger, hwnd)
        if page in ("deploy", "battle"):
            break
        time.sleep(1.0)
    if page not in ("deploy", "battle"):
        _fail(logger, hwnd, "作战开始", f"点「作战开始」后未进布阵/战斗（{page}）", items, out)
        return "failed"
    if page == "deploy":
        ok, page, items, attempts = _start_battle_flexible(logger, hwnd, stop_check)
        if not ok:
            _fail(logger, hwnd, "开始战斗",
                  f"战斗未开始（{page}，开始战斗已点 {attempts} 次）", items, out)
            return "failed"
    ok, page, items = _wait_battle_settled(logger, hwnd, stop_check)
    if not ok:
        _fail(logger, hwnd, "挂机结算", f"未结算（{page}）", items, out)
        return "failed"
    ok, page, items = _back_to_trial_page(logger, hwnd, page, items, stop_check)
    if not ok:
        _fail(logger, hwnd, "回试玩页", f"结算后未回试玩页（{page}）", items, out)
        return "failed"
    return "done"


def run_trial_activity(
    logger: logging.Logger,
    hwnd,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Dict:
    """主界面 → 活动中心 → 试玩关卡 → 逐槽打完 → 返回主界面。

    返回 {"ok", "detail", "fought", "slots", "step", "returned_main"}；
    ok=False 时调用方不写周期 meta，下一批按门控重试。
    """
    out: Dict = {"ok": False, "detail": "", "fought": [], "slots": [],
                 "step": "", "returned_main": None}
    fought: List[str] = out["fought"]
    slot_states: List[Dict] = out["slots"]
    # 2026-11-22 #447/#456 修：items 只在「起点恢复失败」分支赋值，正常进流程后
    # 「打开活动中心」失败分支还要拿它当 _fail 的参数 → UnboundLocalError，
    # _fail 没执行、异常直冲 finally（盲点 P_BACK 连开三天邮件面板）。给空兜底。
    items: List[Dict] = []
    try:
        if _stopped(stop_check):
            out.update(detail="用户停止", step="起点确认")
            return out
        # 本步排在拟合回归之后，上一段收尾可能停在外勤/活动页——先恢复主界面
        from .baijing_scene import _ensure_main_ui

        if not _ensure_main_ui(logger, hwnd, stop_check=stop_check):
            page, items = _trial_page_now(logger, hwnd)
            return _fail(logger, hwnd, "起点确认", f"未能回到主界面（{page}）", items, out)

        if not _ocr_click(logger, hwnd, "活动中心", "活动中心",
                          REGION_ACTIVITY_BTN):
            # 11-20/11-21/11-22 三次「主界面没见到入口」都没留下当帧文字，
            # 底栏改版/遮挡只能靠猜；这里补一次 OCR 交给 _record_failure 落证据。
            _, items = _trial_page_now(logger, hwnd)
            return _fail(logger, hwnd, "打开活动中心", "主界面未见「活动中心」入口", items, out)
        ok, page, items = _wait_trial_page(logger, hwnd, "hub", WAIT_HUB, stop_check,
                                           also_ok=("trial",))
        if not ok:
            return _fail(logger, hwnd, "打开活动中心", f"未进入活动页（{page}）", items, out)
        if page == "hub":
            if not _ocr_click(logger, hwnd, "试玩关卡", "试玩关卡",
                              REGION_TRIAL_TAB, fallback=P_TRIAL_TAB):
                return _fail(logger, hwnd, "打开试玩关卡", "左列入口定位失败", items, out)
            ok, page, items = _wait_trial_page(logger, hwnd, "trial", WAIT_TRIAL, stop_check)
            if not ok:
                return _fail(logger, hwnd, "打开试玩关卡", f"未进入试玩页（{page}）", items, out)

        logger.info(f"{LOG_TAG} 已进入试玩页，开始逐槽位处理 {len(SLOT_POINTS)} 个槽位")
        for idx, pt in enumerate(SLOT_POINTS, 1):
            if _stopped(stop_check):
                out.update(ok=False, detail="用户停止", step=f"槽位{idx}")
                return out
            name = _read_char_name(logger, hwnd, items)
            if not _click(logger, hwnd, pt, f"槽位{idx}"):
                return _fail(logger, hwnd, f"槽位{idx}", "点击失败", [], out)
            time.sleep(SLOT_SETTLE_SEC)
            page, items = _trial_page_now(logger, hwnd)
            if page != "trial":
                # 切槽后页面不再是试玩页 = 异常（不改版误判就是点了别处）；留证停止
                return _fail(logger, hwnd, f"槽位{idx}",
                             f"切槽后页面异常（{page}）", items, out)
            slot_name = _read_char_name(logger, hwnd, items) or name or f"槽位{idx}"
            state = _run_single_slot(logger, hwnd, stop_check, out)
            if state == "failed":
                return out
            slot_states.append({"slot": idx, "name": slot_name, "state": state})
            if state == "skipped":
                logger.info(f"{LOG_TAG} {slot_name} 跳过（未开战/不可打）")
                continue
            fought.append(slot_name)
            logger.info(f"{LOG_TAG} {slot_name} 完成（{len(fought)} 场）")

        out.update(ok=True, step="done",
                   detail=f"角色试玩完成 {len(fought)} 场（跳过 {len(slot_states) - len(fought)} 槽）")
        logger.info(f"{LOG_TAG} {out['detail']}")
        return out
    finally:
        # 2026-11-22 #447/#456：收尾「返回」只有确认还在试玩链上才点。主界面左上
        # (0.101,0.047) 那个坐标就是「邮件」图标 (0.095，差 11px)——盲点会自己
        # 打开邮件/公告面板，随后场景恢复认不出、白烧 6~8s 还落一条假失败。
        last_page = "unknown"
        try:
            last_page, _items = _trial_page_now(logger, hwnd)
        except Exception as exc:  # noqa: BLE001 - 判页失败按不在试玩链处理
            logger.debug(f"{LOG_TAG} 收尾判页异常（按不在试玩链处理）: {exc}")
        try:
            if last_page in ("trial", "hub"):
                _click(logger, hwnd, P_BACK, "试玩页返回")
            else:
                logger.info(
                    f"{LOG_TAG} 收尾页面「{last_page}」不在试玩链，跳过左上「返回」"
                    "（那个坐标在主界面是邮件图标）"
                )
            from .baijing_scene import _ensure_main_ui

            out["returned_main"] = _ensure_main_ui(
                logger, hwnd, stop_check=stop_check, attempts=CLEANUP_MAIN_ATTEMPTS
            )
        except Exception as exc:  # noqa: BLE001 - 收尾异常不改变本轮结论
            logger.debug(f"{LOG_TAG} 收尾返回异常（已忽略）: {exc}")


def trial_due(last_value: Optional[str], today: Optional[_dt.date] = None) -> bool:
    """周期门控：上次成功日期（ISO）距今 ≥ TRIAL_CYCLE_DAYS 天才到期。

    last_value 为空/不可解析 → 视为到期（首跑或脏数据不卡死流程）。
    """
    if not last_value:
        return True
    try:
        last = _dt.date.fromisoformat(str(last_value))
    except ValueError:
        return True
    today = today or _dt.date.today()
    return (today - last).days >= TRIAL_CYCLE_DAYS
