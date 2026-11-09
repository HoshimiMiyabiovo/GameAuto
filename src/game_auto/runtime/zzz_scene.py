# -*- coding: utf-8 -*-
"""ZZZ（绝区零）场景登记表：后台月/周任务运行前的状态校正（参照白荆 recover_scene）。

背景（2026-11-08 #219/#220）：OneDragon 异常收尾后画面停在功能菜单，后勤商店/
恶名狩猎两条流程的「点击进入游戏→等待主界面」进入段假设冷启动登录页，语义全面
失效——步骤 1 OCR 误点 (960,886)、步骤 2 干等 4 分钟回退、步骤 4 在功能菜单里
70s 找不到「后勤商店」入口（该菜单里根本没有这个字，OCR 把活动横幅误当入口），
月商店失败后 close_process_on_finish 杀游戏、69ms 后周常又冷启动。

本模块提供场景登记表 + ensure_main_world 状态校正循环：
- 每轮单次整窗 OCR（+电台模板命中检查），按 ZZZ 登记表顺序认领派发
- 终态 = 大世界（电台模板 / 「星期」OCR 任一）
- unknown 连续 2 轮 → 截图+当帧文字落 flow_failures，返回失败
- 冷启动 logo/加载过场只等待不点击；进程启动窗内 unknown 不速败（11-09 冷启动误判实锤）
- 绝不盲点：只认领有特征的场景，动作全部有实机依据（月卡 dismiss 11-07 实机、
  扫描码 ESC 11-05 实机、前台短按住点击 11-04 实机）

接线：foreground_flow_runner._BUILTIN_ACTIONS + _dispatch_step_action 的
`zzz_enter_main` action；两条 ZZZ 后台流程（LOGISTICS/NOTORIOUS）第一步调用。
"""
import logging
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .ocr_click_tester import _normalize_text, ocr_window_content

LOGGER = logging.getLogger("GameAutomation")
LOG_TAG = "[ZZZ场景]"

# ── 场景特征（全部有取证/实机出处，禁止单词裸判）────────────────────────────
# 大世界：电台模板（start_from_main 同款）+ OCR「星期」双通道任一
MAIN_WORLD_OCR = ("星期",)
# 功能菜单（ESC 后的主菜单页）：底部功能栏词 ≥2 才算——单词会在别处出现
# （2026-11-08 失败帧：一整排 更多/助战编队/邮件/…/丽都城募/调频）
MENU_MARKERS = ("丽都城募", "调频", "代理人", "助战编队", "绳网", "公告")
# 月卡/补给弹窗：11-07 dismiss 机制实机词表（命中即点掉，单词即可——弹窗独占）
POPUP_MARKERS = ("今日到账", "惊喜补给", "月卡剩余")
# 登录页按钮
LOGIN_TEXTS = ("点击进入游戏",)
# 冷启动 logo/加载过场（11-09 20:03 取证帧：zenLess zone zero / 绝·区·零，只等待不点击）
LOADING_MARKERS = ("zenlesszonezero", "绝·区·零")
# 丽都城募活动页（NEW ERIDU CITY FUND）：顶部页签组「选择奖励/兑换商店/等级回馈/
# 成长任务/城募赠礼」任 2 词即认领（11-09 22:28 取证帧）。注意与功能菜单区分：
# 功能菜单底部栏有「丽都城募/调频/代理人/助战编队/绳网/公告」；活动页只有顶部页签，
# 两者词表无交集，单词「丽都城募」在活动页 OCR 里不出现（页签是「城募赠礼」）。
CITY_FUND_MARKERS = ("选择奖励", "兑换商店", "等级回馈", "成长任务", "城募赠礼")
# 活动页左上角红色返回箭头：占窗口宽/高各 12% 的左上区（11-09 22:28 取证帧：
# 顶部横条最左端，红色描边斜切框内弯曲返回箭头；限定左上区域防误点页签/正文）
_CITY_FUND_BACK_RATIO = (0.12, 0.12)

MAIN_WORLD_RADIO_TPL = "assets/templates\\绝区零-主界面-电台.png"
SHOP_LOGISTICS_TAB_TPL = "assets/templates\\绝区零-商店-哨站后勤页签.png"

SCENE_RECOVERY_ROUNDS = 16
SCENE_UNKNOWN_ROUNDS = 2
_ACTION_COOLDOWN_SEC = 1.5
_LOADING_CAP_SEC = 90
# 冷启动容忍窗：游戏进程龄小于此值时 unknown 只等待不速败（logo 前后的黑屏/提示页）
_STARTUP_GRACE_SEC = 240


def _stopped(stop_check: Optional[Callable[[], bool]]) -> bool:
    try:
        return bool(stop_check and stop_check())
    except Exception:
        return False


def _texts(results: Sequence[Dict]) -> List[str]:
    out = []
    for r in results or []:
        try:
            conf = float(r.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.5:
            continue
        t = _normalize_text(str(r.get("text") or ""))
        if t:
            out.append(t)
    return out


def _first_hit(results: Sequence[Dict], words: Sequence[str]) -> Optional[Dict]:
    """返回第一个命中词表的 OCR 行（按置信度优先），无命中 None。"""
    best = None
    best_conf = -1.0
    for r in results or []:
        try:
            conf = float(r.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        t = _normalize_text(str(r.get("text") or ""))
        if conf < 0.5 or not t:
            continue
        if any(w in t for w in words):
            if conf > best_conf:
                best, best_conf = r, conf
    return best


def _count_markers(texts: Sequence[str], words: Sequence[str]) -> int:
    return sum(1 for w in words if any(w in t for t in texts))


def _radio_hit(logger: logging.Logger, hwnd, scales, confidences) -> bool:
    """大世界电台模板单次命中检查（复用 runner 的窗口内模板匹配）。"""
    try:
        from .foreground_flow_runner import _locate_template_in_window

        return _locate_template_in_window(
            hwnd, MAIN_WORLD_RADIO_TPL, scales, confidences) is not None
    except Exception as exc:  # noqa: BLE001 - 模板通道失败交给 OCR「星期」兜底
        LOGGER.debug("%s 电台模板检查失败: %s", LOG_TAG, exc)
        return False


def _shop_page_hit(logger: logging.Logger, hwnd, scales, confidences) -> bool:
    try:
        from .foreground_flow_runner import _locate_template_in_window

        return _locate_template_in_window(
            hwnd, SHOP_LOGISTICS_TAB_TPL, scales, confidences) is not None
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("%s 商店页签模板检查失败: %s", LOG_TAG, exc)
        return False


def _press_escape(logger: logging.Logger) -> None:
    from .foreground_flow_runner import press_key_scancode

    press_key_scancode("escape")


def _click_center(logger: logging.Logger, center) -> None:
    from .foreground_flow_runner import _pyautogui_click_guarded

    _pyautogui_click_guarded(int(center[0]), int(center[1]))


def _exit_city_fund_page(logger: logging.Logger, hwnd, stop_check) -> str:
    """丽都城募活动页 → 点左上返回图标回大世界；图标未命中按 ESC 兜底。

    返回 "gone"（页签词已消失）/"stopped"/"still"（动作后仍在活动页）。
    返回图标是图标不是文字，OCR 认不出 → 用左上 12%×12% 区域中心点按
    （11-09 22:28 取证帧：红色返回箭头在顶部横条最左端）；点完一轮 OCR
    复核页签词是否消失，消失才算退出，绝不假成功。
    """
    from .ocr_click_tester import _ensure_window_region

    region = _ensure_window_region(hwnd)
    if region is None:
        logger.warning("%s 窗口区域取不到，返回图标点击改按 ESC 兜底", LOG_TAG)
        _press_escape(logger)
        return "esc"
    left, top, w, h = region
    rx = left + int(w * _CITY_FUND_BACK_RATIO[0] / 2)
    ry = top + int(h * _CITY_FUND_BACK_RATIO[1] / 2)
    _click_center(logger, (rx, ry))
    logger.info("%s 点活动页左上返回图标 @ (%d, %d)", LOG_TAG, rx, ry)
    time.sleep(_ACTION_COOLDOWN_SEC)
    if _stopped(stop_check):
        return "stopped"
    results = ocr_window_content(logger, hwnd=hwnd) or []
    texts = _texts(results)
    if _count_markers(texts, CITY_FUND_MARKERS) < 2:
        return "gone"
    # 返回图标没点掉（图标 OCR 不可认、坐标偏移），ESC 兜底再复核一轮
    _press_escape(logger)
    logger.info("%s 返回图标点击后仍在活动页，按 ESC 兜底", LOG_TAG)
    time.sleep(_ACTION_COOLDOWN_SEC)
    if _stopped(stop_check):
        return "stopped"
    results = ocr_window_content(logger, hwnd=hwnd) or []
    texts = _texts(results)
    return "gone" if _count_markers(texts, CITY_FUND_MARKERS) < 2 else "still"


def _record_unknown(logger: logging.Logger, hwnd, flow_key: str,
                    results: Sequence[Dict], detail: str) -> None:
    """unknown 场景兜底：截图 + 当帧文字落 flow_failures（白荆同款语义）。"""
    try:
        from .flow_diagnostics import record_failure

        sample = [str(r.get("text") or "") for r in (results or [])][:20]
        record_failure(
            logger, flow_key or "ZZZ_SCENE", "场景校正", detail,
            hwnd=hwnd, ocr_results=list(results or []), ocr_sample=sample,
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不改变校正结论
        LOGGER.debug("%s 落取证失败: %s", LOG_TAG, exc)


def _process_age_sec(hwnd: int) -> Optional[float]:
    """游戏进程龄（秒）；拿不到返回 None（不启用冷启动容忍窗）。"""
    try:
        import psutil
        import win32process
        pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
        if pid <= 0:
            return None
        return max(0.0, time.time() - psutil.Process(pid).create_time())
    except Exception:
        return None


def _wait_loading_scene(logger: logging.Logger, hwnd, stop_check) -> str:
    """logo/加载过场：轮询到可认领场景（大世界/登录/弹窗/菜单）为止，上限 _LOADING_CAP_SEC。"""
    end = time.time() + _LOADING_CAP_SEC
    while time.time() < end:
        if _stopped(stop_check):
            return "stopped"
        time.sleep(3.0)
        results = ocr_window_content(logger, hwnd=hwnd) or []
        texts = _texts(results)
        if (_has_word(texts, MAIN_WORLD_OCR) or _first_hit(results, LOGIN_TEXTS)
                or _first_hit(results, POPUP_MARKERS)
                or _count_markers(texts, MENU_MARKERS) >= 2
                or _count_markers(texts, CITY_FUND_MARKERS) >= 2):
            return "gone"
    return "cap"


def ensure_main_world(
    logger: logging.Logger,
    hwnd: int,
    stop_check: Optional[Callable[[], bool]] = None,
    scales=None,
    confidences=None,
    flow_key: str = "",
    context: str = "ZZZ场景校正",
) -> Tuple[bool, str]:
    """把游戏画面校正到**大世界**：场景登记表循环，终态或失败返回。

    返回 (是否到达大世界, 说明)。每轮单次 OCR 共享给所有判定（白荆同款，
    避免一轮恢复跑多次识别）；规则按序认领：月卡弹窗点掉 → 功能菜单 ESC →
    登录页点进入 → 商店页 ESC → unknown 计数落证据。电台模板与「星期」任一
    命中即终态。动作后短暂等待画面稳定再进下一轮。
    """
    unknown_streak = 0
    age = _process_age_sec(hwnd)
    startup_deadline = (time.time() + max(0.0, _STARTUP_GRACE_SEC - age)
                        if age is not None else 0.0)
    for round_no in range(1, SCENE_RECOVERY_ROUNDS + 1):
        if _stopped(stop_check):
            return False, "用户停止"
        results = ocr_window_content(logger, hwnd=hwnd) or []
        texts = _texts(results)
        # 终态：大世界（模板/OCR 双通道任一）
        if _has_word(texts, MAIN_WORLD_OCR) or _radio_hit(logger, hwnd, scales, confidences):
            logger.info("%s 第 %d 轮已在大世界", LOG_TAG, round_no)
            return True, "大世界"
        # 规则 1：月卡/补给弹窗 → 点命中词关掉（11-07 dismiss 实机同款）
        popup = _first_hit(results, POPUP_MARKERS)
        if popup is not None:
            _click_center(logger, popup["center"])
            logger.info("%s 第 %d 轮点掉弹窗「%s」", LOG_TAG, round_no,
                        popup.get("text"))
            time.sleep(_ACTION_COOLDOWN_SEC)
            unknown_streak = 0
            continue
        # 规则 2：功能菜单 → 扫描码 ESC 关回大世界（11-05 实机：只吃扫描码）
        if _count_markers(texts, MENU_MARKERS) >= 2:
            _press_escape(logger)
            logger.info("%s 第 %d 轮功能菜单按 ESC 关闭", LOG_TAG, round_no)
            time.sleep(_ACTION_COOLDOWN_SEC)
            unknown_streak = 0
            continue
        # 规则 2.5：丽都城募活动页残留（11-09 22:28 实锤：旧链路 ESC→点丽都城募
        # 停在这页）→ 点左上返回图标退出，ESC 兜底，页签词消失才算数
        if _count_markers(texts, CITY_FUND_MARKERS) >= 2:
            state = _exit_city_fund_page(logger, hwnd, stop_check)
            if state == "stopped":
                return False, "用户停止"
            if state == "still":
                detail = f"{context}: 丽都城募活动页返回图标/ESC 均未退出"
                _record_unknown(logger, hwnd, flow_key, results, detail)
                return False, detail
            logger.info("%s 第 %d 轮丽都城募活动页已退出（%s）", LOG_TAG, round_no,
                        "返回图标" if state == "gone" else "ESC")
            unknown_streak = 0
            continue
        # 规则 3：登录页 → 前台短按住点「点击进入游戏」
        login = _first_hit(results, LOGIN_TEXTS)
        if login is not None:
            _click_center(logger, login["center"])
            logger.info("%s 第 %d 轮登录页点「点击进入游戏」", LOG_TAG, round_no)
            time.sleep(3.0)  # 进游戏有加载过场，多等一会
            unknown_streak = 0
            continue
        # 规则 4：商店页残留（上次任务失败遗留）→ ESC 退出
        if _shop_page_hit(logger, hwnd, scales, confidences):
            _press_escape(logger)
            logger.info("%s 第 %d 轮商店页按 ESC 退出", LOG_TAG, round_no)
            time.sleep(_ACTION_COOLDOWN_SEC)
            unknown_streak = 0
            continue
        # 规则 5：冷启动 logo/加载过场 → 只等待到可认领场景，绝不盲点
        # （11-09 20:03 实锤：启动 23s 还在 ZZ logo 页，旧逻辑当 unknown 两轮速败）
        if _first_hit(results, LOADING_MARKERS) is not None:
            state = _wait_loading_scene(logger, hwnd, stop_check)
            if state == "stopped":
                return False, "用户停止"
            if state == "cap":
                detail = f"{context}: 加载过场超 {_LOADING_CAP_SEC}s 未结束"
                _record_unknown(logger, hwnd, flow_key, results, detail)
                return False, detail
            logger.info("%s 第 %d 轮加载过场结束，继续校正", LOG_TAG, round_no)
            unknown_streak = 0
            continue
        # unknown：启动容忍窗内只等待（冷启动黑屏/提示页）；窗外连续 2 轮认不出 → 落证据失败
        if time.time() < startup_deadline:
            logger.info("%s 第 %d 轮未知但游戏进程尚在启动窗，等待加载", LOG_TAG, round_no)
            time.sleep(3.0)
            continue
        unknown_streak += 1
        logger.warning("%s 第 %d 轮未知场景（连续 %d 轮）", LOG_TAG, round_no,
                       unknown_streak)
        if unknown_streak >= SCENE_UNKNOWN_ROUNDS:
            detail = f"{context}: 连续 {unknown_streak} 轮未知场景，未到达大世界"
            _record_unknown(logger, hwnd, flow_key, results, detail)
            return False, detail
        time.sleep(1.5)
    detail = f"{context}: {SCENE_RECOVERY_ROUNDS} 轮未到达大世界"
    _record_unknown(logger, hwnd, flow_key, [], detail)
    return False, detail


def _has_word(texts: Sequence[str], words: Sequence[str]) -> bool:
    return any(w in t for t in texts for w in words)


def enter_main_interface(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
    flow_key: str = "",
) -> Tuple[bool, str, int, Optional[int]]:
    """builtin action `zzz_enter_main`：把画面校正到大世界（签名对齐 enter_main 家族）。

    flow_key 仅用于失败取证归类；step 保留以对齐家族签名（当前无步骤级配置项）。
    """
    ok, msg = ensure_main_world(
        logger, hwnd, stop_check=stop_check, scales=scales,
        confidences=confidences, flow_key=flow_key,
        context=str(step.get("name") or "ZZZ场景校正"),
    )
    if not ok:
        return False, msg, hwnd, None
    return True, msg, hwnd, None
