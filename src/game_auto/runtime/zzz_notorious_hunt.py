"""绝区零「恶名狩猎」每周三次自动挑战（2026-11-01）。

用户给的实机路径（9 张截图逐步确认）：

    主界面 F2 → 快捷手册「训练」页签 → 左侧「恶名狩猎」→ 「库萨里库」行「前往」
    → 「是否立即传送？」确认 → 详情页「下一步」→ 配队页「出战」（配队不改）
    → 等右上角开始计时 → 向前 W 走两秒 → 自动战斗
    → 战斗结束（照抄 OneDragon 的副本结算流程）→ 重复到本周 3 次
    → 左下「奖励入口」→ 周期挑战「全部领取」→ 返回

自动战斗本体在 ``runtime/zzz_battle_assist.py``（从 OneDragon 移植）。
本模块只负责导航、节奏（每周三次）和领奖。

定位策略：**比例坐标点击 + OCR 校验**。每个坐标都来自用户截图，换分辨率按窗口
比例跟随；点完必须看到预期文字才算这一步成立，绝不"点一下就当成功"往下走。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pyautogui

from ..settings.paths import BASE_DIR

LOGGER = logging.getLogger("GameAutomation")

LABEL = "恶名狩猎"

# ---------------------------------------------------------------------------
# 步骤坐标（窗口相对比例，来自 2026-11-01 用户截图）
# ---------------------------------------------------------------------------
P_TRAINING_TAB = (0.589, 0.163)     # 快捷手册顶部「训练」页签
P_HUNT_ENTRY = (0.172, 0.685)       # 训练页左侧「恶名狩猎」
P_GO = (0.764, 0.607)               # 「库萨里库」那一行的「前往」
P_CONFIRM = (0.552, 0.576)          # 「是否立即传送？」→ 确认
P_NEXT = (0.910, 0.935)             # 详情页右下「下一步」
P_START = (0.913, 0.953)            # 配队页右下「出战」
P_REWARD_ENTRY = (0.063, 0.935)     # 恶名狩猎列表左下「奖励入口」
P_CLAIM_ALL = (0.709, 0.333)        # 周期挑战弹窗「全部领取」
P_RESULT_FINISH = (0.870, 0.950)    # 结算页「完成」兜底坐标

# 右上角战斗计时器（00:12.34）。用户口径：看到它开始跳才是真正进战斗，这时才走 W。
TIMER_REGION_FRAC = (0.84, 0.03, 0.97, 0.11)
TIMER_RE = re.compile(r"\d{1,2}[:：]\d{2}")

# 剩余次数（本周核心材料掉落次数 N/3）
TIMES_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

MENU_OPEN_ATTEMPTS = 3
MENU_WAIT_SEC = 6
TAB_WAIT_SEC = 6
PAGE_WAIT_SEC = 15
BATTLE_ENTER_SEC = 90        # 出战到进战斗（含加载动画）
MOVE_W_SEC = 2.0             # 用户口径：向前 W 走两秒
BATTLE_TIMEOUT_SEC = 300     # 单场战斗上限
RESULT_WAIT_SEC = 25         # 战斗结束到结算页
CLAIM_WAIT_SEC = 8
SHORT_WAIT_SEC = 5
CLICK_SETTLE_SEC = 1.0


def _abs(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(str(BASE_DIR), path)


def _sleep(seconds: float, stop_check: Optional[Callable[[], bool]] = None) -> None:
    end = time.time() + max(0.0, float(seconds))
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        if stop_check is not None and stop_check():
            return
        time.sleep(min(0.2, remaining))


def _press_key(hwnd: int, key: str) -> None:
    """游戏内按键一律发扫描码（2026-11-07 从 pyautogui.press 换过来）。

    11-05 绝区零实机：瞬时虚拟键事件被 Unity 整口吞掉；11-07 12:15 再实锤——
    pyautogui F2 按三次快捷手册都没开（#210），只有 keybd_event+MapVirtualKey
    按住 0.15s 的扫描码才被接收（press_key_scancode，同 monthly_exchange 的 ESC）。
    """
    from .foreground_flow_runner import _focus_window, press_key_scancode

    _focus_window(hwnd)
    press_key_scancode(key)


def _hold_key(hwnd: int, key: str, seconds: float) -> None:
    # 战斗里的 W/闪避/切人同是游戏内按键：pyautogui 的虚拟键事件同样会被吞
    # （11-05 实机 ESC/F3 全无效），统一走扫描码按住
    from .foreground_flow_runner import _focus_window, press_key_scancode

    _focus_window(hwnd)
    press_key_scancode(key, hold_sec=max(0.05, float(seconds)))


def _texts(logger: logging.Logger, hwnd: int) -> List[Dict]:
    from .ocr_click_tester import ocr_window_content

    return ocr_window_content(logger, hwnd=hwnd) or []


def _find_text(
    items: Sequence[Dict],
    keywords: Sequence[str],
    min_conf: float = 0.5,
    region_frac: Optional[Sequence[float]] = None,
    hwnd: Optional[int] = None,
) -> Optional[Dict]:
    """在 OCR 结果里找命中关键字的条目，可限定窗口相对区域。"""
    from .foreground_flow_runner import _window_rect

    rect = _window_rect(hwnd) if (hwnd and region_frac) else None
    for item in items or []:
        text = str(item.get("text") or "").replace(" ", "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        if not any(kw in text for kw in keywords):
            continue
        if region_frac and rect:
            center = item.get("center")
            if not center:
                continue
            fx = (float(center[0]) - rect[0]) / max(1, rect[2])
            fy = (float(center[1]) - rect[1]) / max(1, rect[3])
            x1, y1, x2, y2 = [float(v) for v in region_frac]
            if not (x1 <= fx <= x2 and y1 <= fy <= y2):
                continue
        return item
    return None


def _wait_text(
    logger: logging.Logger,
    hwnd: int,
    keywords: Sequence[str],
    timeout_sec: float,
    stop_check: Optional[Callable[[], bool]] = None,
    min_conf: float = 0.5,
    region_frac: Optional[Sequence[float]] = None,
) -> Optional[Dict]:
    """轮询等到某段文字出现（用于"点完确认真的到了下一屏"）。"""
    end = time.time() + max(1.0, float(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None
        try:
            items = _texts(logger, hwnd)
        except Exception as exc:  # noqa: BLE001 - OCR 失败按未出现处理，继续等
            logger.debug(f"[{LABEL}] OCR 失败: {exc}")
            items = []
        hit = _find_text(items, keywords, min_conf=min_conf, region_frac=region_frac, hwnd=hwnd)
        if hit:
            return hit
        _sleep(0.5, stop_check)
    return None


def _click_frac(hwnd: int, frac: Sequence[float], logger: logging.Logger, note: str = "") -> None:
    from .foreground_flow_runner import _click_window_local
    from .zzz_monthly_shop import _frac_point

    point = _frac_point(hwnd, frac)
    if not point:
        raise RuntimeError(f"比例坐标换算失败: {frac}")
    _click_window_local(hwnd, point[0], point[1], clicks=1)
    if note:
        logger.info(f"[{LABEL}] 点击{note} @ {point}")


def read_remaining_times(logger: logging.Logger, hwnd: int) -> Optional[Tuple[int, int]]:
    """读「本周核心材料掉落次数 N/3」。返回 (已用, 上限)；读不到返回 None。"""
    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败按"不知道"处理
        logger.debug(f"[{LABEL}] 读取掉落次数失败: {exc}")
        return None
    for item in items:
        text = str(item.get("text") or "").replace(" ", "")
        if "核心材料" not in text and "掉落次数" not in text:
            continue
        match = TIMES_RE.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
    # 次数和标签可能被 OCR 拆开：找形如 N/3 的独立文本
    for item in items:
        text = str(item.get("text") or "").replace(" ", "")
        match = re.fullmatch(r"(\d+)/(\d+)", text)
        if match and int(match.group(2)) in (3, 6):
            return int(match.group(1)), int(match.group(2))
    return None


def _open_quick_menu(logger: logging.Logger, hwnd: int, step: Dict,
                     stop_check: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """主界面 F2 打开快捷手册；已经开着就跳过。"""
    if _wait_text(logger, hwnd, ("训练", "作战", "战术"), 1, stop_check):
        logger.info(f"[{LABEL}] 快捷手册已打开")
        return True, "快捷手册已打开"

    key = str(step.get("menu_key") or "f2").strip() or "f2"
    for attempt in range(1, MENU_OPEN_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"
        _press_key(hwnd, key)
        logger.info(f"[{LABEL}] 已按 {key.upper()} 打开快捷手册（第 {attempt} 次）")
        hit = _wait_text(logger, hwnd, ("训练", "作战", "战术"), MENU_WAIT_SEC, stop_check)
        if hit:
            _sleep(CLICK_SETTLE_SEC, stop_check)
            return True, "快捷手册已打开"
        logger.warning(f"[{LABEL}] 按 {key.upper()} 后没看到快捷手册页签，按 ESC 清一层重试")
        _press_key(hwnd, "escape")
        _sleep(CLICK_SETTLE_SEC, stop_check)
    return False, "未能打开快捷手册（F2 后没看到训练/作战/战术）"


def _select_training(logger: logging.Logger, hwnd: int, step: Dict,
                     stop_check: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """切到「训练」页签，并确认左侧出现训练下的子入口。"""
    frac = step.get("training_tab_frac") or P_TRAINING_TAB
    _click_frac(hwnd, frac, logger, "「训练」页签")
    hit = _wait_text(logger, hwnd, ("恶名狩猎", "实战模拟室", "专业挑战室"), TAB_WAIT_SEC, stop_check)
    if hit:
        return True, f"已进入训练页（看到「{hit.get('text')}」）"
    return False, "点「训练」后没看到训练子入口（恶名狩猎/实战模拟室）"


def _select_hunt(logger: logging.Logger, hwnd: int, step: Dict,
                 stop_check: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """点左侧「恶名狩猎」，等 boss 列表出现。"""
    frac = step.get("hunt_entry_frac") or P_HUNT_ENTRY
    _click_frac(hwnd, frac, logger, "左侧「恶名狩猎」")
    hit = _wait_text(logger, hwnd, ("库萨里库", "基塔布鲁", "核心材料"), PAGE_WAIT_SEC, stop_check)
    if hit:
        return True, f"已进入恶名狩猎（看到「{hit.get('text')}」）"
    return False, "点「恶名狩猎」后没看到 boss 列表"


def _enter_battle(logger: logging.Logger, hwnd: int, step: Dict,
                  stop_check: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """从 boss 列表走到战斗加载完成：前往 → 传送确认 → 下一步 → 出战 → 等计时器。"""
    _click_frac(hwnd, step.get("go_frac") or P_GO, logger, "「库萨里库」前往")

    confirm = _wait_text(logger, hwnd, ("是否立即传送", "立即传送", "取消"), SHORT_WAIT_SEC + 3, stop_check)
    if confirm:
        _click_frac(hwnd, step.get("confirm_frac") or P_CONFIRM, logger, "传送确认")
        _sleep(CLICK_SETTLE_SEC, stop_check)
    else:
        logger.info(f"[{LABEL}] 没有出现传送确认框（可能已直接进入），继续")

    # 详情页 → 下一步
    if not _wait_text(logger, hwnd, ("下一步", "挑战奖励"), PAGE_WAIT_SEC, stop_check):
        return False, "没看到库萨里库详情页（下一步）"
    _click_frac(hwnd, step.get("next_frac") or P_NEXT, logger, "「下一步」")

    # 配队页 → 出战（用户：配队不用改）
    if not _wait_text(logger, hwnd, ("出战", "预备编队", "战斗设置"), PAGE_WAIT_SEC, stop_check):
        return False, "没看到配队页（出战）"
    _click_frac(hwnd, step.get("start_frac") or P_START, logger, "「出战」")

    # 等右上角计时器开始跳（= 真正进战斗，这时才能走位）
    end = time.time() + BATTLE_ENTER_SEC
    while time.time() < end:
        if stop_check is not None and stop_check():
            return False, "用户停止"
        try:
            items = _texts(logger, hwnd)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{LABEL}] 计时器 OCR 失败: {exc}")
            items = []
        for item in items:
            text = str(item.get("text") or "")
            if TIMER_RE.search(text):
                logger.info(f"[{LABEL}] 战斗计时已开始（{text.strip()}）")
                return True, "已进入战斗"
        _sleep(0.6, stop_check)
    return False, f"出战 {BATTLE_ENTER_SEC}s 后仍没看到右上角计时器"


def _wait_result(logger: logging.Logger, hwnd: int,
                 stop_check: Optional[Callable[[], bool]]) -> bool:
    """等结算页并点「完成」（照抄 OneDragon 的副本战斗结束流程）。"""
    hit = _wait_text(logger, hwnd, ("完成", "点击继续", "挑战成功", "挑战失败"), RESULT_WAIT_SEC, stop_check)
    if hit:
        logger.info(f"[{LABEL}] 战斗结束（看到「{hit.get('text')}」）")
    else:
        logger.warning(f"[{LABEL}] 没识别到结算页文字，按兜底坐标点「完成」")
    # 结算页的「完成」在右下，先按 OCR 定位，再退到固定比例坐标
    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{LABEL}] 结算页 OCR 失败: {exc}")
        items = []
    finish = _find_text(items, ("完成", "点击继续"))
    from .foreground_flow_runner import _click_window_local

    if finish and finish.get("center"):
        from .foreground_flow_runner import _window_rect

        rect = _window_rect(hwnd)
        if rect:
            local = (int(finish["center"][0]) - rect[0], int(finish["center"][1]) - rect[1])
            _click_window_local(hwnd, local[0], local[1], clicks=1)
            logger.info(f"[{LABEL}] 已点结算页「{finish.get('text')}」@ {local}")
            _sleep(2.0, stop_check)
            return True
    _click_frac(hwnd, P_RESULT_FINISH, logger, "结算页「完成」(兜底坐标)")
    _sleep(2.0, stop_check)
    return True


def _claim_cycle_rewards(logger: logging.Logger, hwnd: int, step: Dict,
                         stop_check: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """左下「奖励入口」→ 周期挑战「全部领取」。"""
    _click_frac(hwnd, step.get("reward_entry_frac") or P_REWARD_ENTRY, logger, "「奖励入口」")
    if not _wait_text(logger, hwnd, ("周期挑战", "全部领取", "已领取"), SHORT_WAIT_SEC + 5, stop_check):
        return False, "点奖励入口后没看到周期挑战弹窗"

    if _find_text(_texts(logger, hwnd), ("已领取",)) and not _find_text(
        _texts(logger, hwnd), ("全部领取",)
    ):
        logger.info(f"[{LABEL}] 周期挑战奖励已领取过")
        return True, "本周奖励已领取"

    _click_frac(hwnd, step.get("claim_all_frac") or P_CLAIM_ALL, logger, "「全部领取」")
    _sleep(1.5, stop_check)
    if _wait_text(logger, hwnd, ("已领取",), CLAIM_WAIT_SEC, stop_check):
        logger.info(f"[{LABEL}] 周期挑战奖励已领取")
        return True, "已全部领取"
    return False, "点了「全部领取」但没看到「已领取」"


def _leave_to_world(logger: logging.Logger, hwnd: int, max_esc: int = 3) -> None:
    """尽量退回大世界（失败/结束时调用，别把游戏留在菜单里）。"""
    for _ in range(max(1, max_esc)):
        try:
            _press_key(hwnd, "escape")
        except Exception as exc:  # noqa: BLE001 - 收尾按键失败不该抛给上层
            logger.debug(f"[{LABEL}] 返回按键失败: {exc}")
            return
        _sleep(1.2)


def run_notorious_hunt(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    *,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """执行恶名狩猎（每周三次）。返回 (是否成功, 摘要)。

    step 字段：
    - menu_key: 打开快捷手册的键，默认 ``f2``
    - weekly_times: 每周挑战次数，默认 3
    - move_w_sec: 进战斗后向前走的秒数，默认 2.0
    - battle_timeout_sec: 单场战斗上限，默认 300
    - 各 ``*_frac``: 覆盖内置比例坐标（换分辨率/改版时用）
    """
    label = str(step.get("log_label") or LABEL).strip() or LABEL
    try:
        weekly_times = max(1, int(step.get("weekly_times", 3) or 3))
    except (TypeError, ValueError):
        weekly_times = 3
    try:
        move_sec = float(step.get("move_w_sec", MOVE_W_SEC) or MOVE_W_SEC)
    except (TypeError, ValueError):
        move_sec = MOVE_W_SEC
    try:
        battle_timeout = float(step.get("battle_timeout_sec", BATTLE_TIMEOUT_SEC) or BATTLE_TIMEOUT_SEC)
    except (TypeError, ValueError):
        battle_timeout = BATTLE_TIMEOUT_SEC

    ok, msg = _open_quick_menu(logger, hwnd, step, stop_check)
    if not ok:
        _leave_to_world(logger, hwnd)
        return False, msg

    ok, msg = _select_training(logger, hwnd, step, stop_check)
    if not ok:
        _leave_to_world(logger, hwnd)
        return False, msg

    ok, msg = _select_hunt(logger, hwnd, step, stop_check)
    if not ok:
        _leave_to_world(logger, hwnd)
        return False, msg

    times = read_remaining_times(logger, hwnd)
    if times is None:
        logger.warning(f"[{label}] 读不到「本周核心材料掉落次数」，按 {weekly_times} 次执行")
        done_target = weekly_times
    else:
        used, total = times
        done_target = max(0, min(weekly_times, total - used))
        logger.info(f"[{label}] 本周掉落次数 {used}/{total}，本次计划打 {done_target} 次")
        if done_target == 0:
            _leave_to_world(logger, hwnd)
            return True, f"本周 {total} 次已打完，无需挑战"

    done = 0
    fail_reason = ""
    for round_no in range(1, done_target + 1):
        if stop_check is not None and stop_check():
            return False, f"用户停止（已完成 {done} 次）"

        ok, msg = _enter_battle(logger, hwnd, step, stop_check)
        if not ok:
            fail_reason = f"第 {round_no} 次进战斗失败: {msg}"
            logger.warning(f"[{label}] {fail_reason}")
            break

        # 用户口径：计时开始后向前 W 走两秒，再交给自动战斗
        _hold_key(hwnd, "w", move_sec)
        logger.info(f"[{label}] 已向前走 {move_sec:.1f}s，开始自动战斗")

        from .zzz_battle_assist import ZzzBattleAssist, END_STOPPED

        # 模型缺失时闪光闪避会自动降级（dodge_flash 返回 -1），普攻/技能照常跑
        model_path = str(step.get("flash_model_path") or "").strip() or None
        try:
            dwell_sec = float(step.get("dwell_sec", 6.0) or 6.0)
        except (TypeError, ValueError):
            dwell_sec = 6.0
        assist = ZzzBattleAssist(
            hwnd,
            logger=logger,
            model_path=model_path,
            dodge_key=str(step.get("dodge_key") or "shift"),
            switch_key=str(step.get("switch_key") or "space"),
            dwell_sec=dwell_sec,
            enable_rotation=bool(step.get("enable_rotation", True)),
        )
        result, detail = assist.run(stop_check=stop_check, timeout_sec=battle_timeout)
        logger.info(f"[{label}] 第 {round_no} 次战斗结束: {result} | {detail}")
        if result == END_STOPPED:
            return False, f"用户停止 | {detail}"

        _wait_result(logger, hwnd, stop_check)
        done += 1

        if round_no < done_target:
            # 结算后回到 boss 列表，重新点「前往」
            if not _wait_text(logger, hwnd, ("库萨里库", "核心材料", "恶名狩猎"), PAGE_WAIT_SEC, stop_check):
                fail_reason = f"第 {round_no} 次战斗后没有回到恶名狩猎列表"
                break

    claim_ok, claim_msg = _claim_cycle_rewards(logger, hwnd, step, stop_check)
    _leave_to_world(logger, hwnd)

    detail = f"完成 {done}/{done_target} 次，{claim_msg}"
    if fail_reason:
        return False, f"{fail_reason} | {detail}"
    return claim_ok or done > 0, detail
