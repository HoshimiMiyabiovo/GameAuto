"""游戏内「兑换商店」每月采购的通用流程（2026-10-25）。

目前两个使用者（都由对应月度流程驱动）：
- 绝区零 ZZZ_MONTHLY：主界面 ESC → 商城(24HOUR) → 信源清单 → **信号残响**
- 崩铁 HSR_MONTHLY：主界面 ESC → 商店 → 兑换商店 → **余烬兑换**

两边的导航骨架完全一致（ESC 打开主菜单 → 点商店入口 → 点左侧导航 → 点顶部页签 →
按清单逐个兑换 → ESC 回主世界），差异全在 step 配置里：入口文字/模板、导航文字与
搜索区域、页签文字/模板、确认按钮模板、退出标志模板、商品清单。

**页签强校验（用户 2026-10-25 两次强调「一定要进入余烬兑换/信号残响」）**：
页签模板一律裁「未选中态」。点完页签后必须等到模板消失（= 页签变成选中态）才算
进入成功；点两次仍未切换就整步失败，绝不"点一下就当成功"往下买。如果进来时模板
本来就不命中（上次已停在该页签，或模板失效），按 OCR 找到页签文字为依据继续，并在
日志里写明判定依据。

购买清单在 flow_configs.json 的 step.items。用户 2026-10-25 明确「具体买什么下个月
告诉你」，两个流程当前都是空清单：空清单只记一条「待定」日志并直接返回，**不按 ESC、
不点开商店** —— 每月空跑一遍无关界面没有意义。

购买交互沿用后勤商店（runtime/zzz_monthly_shop.py::_buy_once）：点商品卡 → 弹确认
→ 点确认；弹窗默认数量 ×1，买 N 个 = N 轮。崩铁余烬兑换的弹窗形态尚未实跑，
下月补清单时先看日志，再按实际截图补 confirm_template，不要凭猜。

退出：买完按 ESC 逐层退，直到能看到 `exit_marker_template`（主世界特征：
绝区零「···」按钮 / 崩铁主界面图标）。先检查再按键，避免多按一次又把主世界切回菜单。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pyautogui

from ..settings.paths import BASE_DIR
from .zzz_monthly_shop import (
    BOUGHT,
    CONFIRM_GONE_FAIL,
    ITEM_MISSING,
    NO_POPUP,
    STOPPED,
    _buy_once,
    _locate,
    _locate_ocr,
    _wait_gone,
)

LOGGER = logging.getLogger("GameAutomation")

MENU_ATTEMPTS = 3            # ESC 打开主菜单的尝试次数（三次都见不到商店入口 = 界面不对）
MENU_WAIT_SEC = 10           # 单次找商店入口超时
NAV_WAIT_SEC = 20            # 商店页里找左侧导航超时（页面加载有过渡动画）
TAB_WAIT_SEC = 15            # 找目标页签超时
TAB_SWITCH_SEC = 8           # 点页签后等「未选中态」消失的超时
TAB_ATTEMPTS = 2             # 页签点击重试次数
SHORT_WAIT_SEC = 8           # 第二次及以后的重试用短超时
PAGE_ATTEMPTS = 2            # 「左侧导航 → 顶部页签」组合重试次数
PAGE_SETTLE_SEC = 2.0        # 页面切换稳定等待
EXIT_MAX_ESC = 3             # 退出时最多按几次 ESC
EXIT_CHECK_SEC = 5           # 每次 ESC 后等主世界特征出现的超时

# 已选中页签的亮度判据：选中态是亮色胶囊（绝区零黄底 / 崩铁白底），
# 未选中态是暗底白字。实测 1919x1079：绝区零 145 vs 78，崩铁 188 vs 53。
# 取区块尺寸按窗口比例换算，阈值可用 step.tab_selected_brightness 覆盖。
TAB_PATCH_WIDTH_FRAC = 0.05
TAB_PATCH_HEIGHT_FRAC = 0.035
TAB_SELECTED_BRIGHTNESS = 110.0
FORBIDDEN_MIN_CONF = 0.5    # 错页签拦截的 OCR 置信度下限


def _abs_template(path: str) -> str:
    """相对模板路径锚定到项目根目录；绝对路径原样返回。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    cand = os.path.join(str(BASE_DIR), path)
    return cand if os.path.exists(cand) else path


def _sleep(seconds: float, stop_check: Optional[Callable[[], bool]] = None) -> None:
    """可中断等待：停止请求最迟 0.2s 生效。"""
    end = time.time() + max(0.0, float(seconds))
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        if stop_check is not None and stop_check():
            return
        time.sleep(min(0.2, remaining))


def _press_escape(hwnd: int) -> None:
    """在游戏窗口内按 ESC。pyautogui 的 fail-safe 与点击同样处理，转成可读异常。"""
    from .foreground_flow_runner import _focus_window, FailSafeAborted, _FAILSAFE_HINT

    _focus_window(hwnd)
    try:
        pyautogui.press("escape")
    except pyautogui.FailSafeException as exc:
        raise FailSafeAborted(_FAILSAFE_HINT) from exc


def _visible_texts(logger: logging.Logger, hwnd: int) -> List[Dict]:
    """当前窗口的 OCR 结果（单独抽出便于测试替换）。"""
    from .ocr_click_tester import ocr_window_content

    return ocr_window_content(logger, hwnd=hwnd) or []


def _check_forbidden_texts(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    label: str,
) -> Tuple[bool, str]:
    """错页签拦截：目标页签上绝不该出现的文字命中即失败，不进入购买。

    三个游戏的口径一致：
    - 绝区零：信号余波的硫磺石/燃狱齿轮/嵌合编译器/啜泣摇篮（RapidOCR 会把它认成
      「嚼泣摇篮」，配置里两个写法都列）
    - 崩铁：星芒兑换的五星光锥（银河铁道之夜等五件）
    - 原神：星辉兑换的宗室武器（长剑/大剑/猎枪/秘法录/长弓）
    OCR 失败只告警不拦截（页签本身已有模板/亮度校验，这里是第二道防线）。
    """
    forbidden = [str(t).strip() for t in (step.get("forbidden_texts") or []) if str(t).strip()]
    if not forbidden:
        return True, ""
    try:
        results = _visible_texts(logger, hwnd)
    except Exception as exc:
        logger.warning(f"[{label}] 错页签校验 OCR 失败，跳过这道额外校验: {exc}")
        return True, ""

    for item in results:
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < FORBIDDEN_MIN_CONF:
            continue
        text = str(item.get("text") or "")
        compact = text.replace(" ", "")
        for bad in forbidden:
            if bad in compact:
                logger.error(
                    f"[{label}] 检测到「{text}」({confidence:.2f})，当前不在目标页签，放弃购买"
                )
                return False, f"检测到「{text}」，当前不在目标页签"
    return True, ""


def _region_frac(step: Dict, key: str) -> Optional[Sequence[float]]:
    raw = step.get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        frac = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if not (0.0 <= frac[0] < frac[2] <= 1.0 and 0.0 <= frac[1] < frac[3] <= 1.0):
        return None
    return frac


def _locate_ocr_region(
    logger: logging.Logger,
    hwnd: int,
    text: str,
    timeout_sec: int,
    region_frac: Sequence[float],
    stop_check: Optional[Callable[[], bool]] = None,
    interval: float = 0.5,
) -> Optional[Tuple[int, int]]:
    """在窗口的相对区域内 OCR 定位（按窗口尺寸换算屏幕区域）。

    崩铁左侧导航有「商店 / 兑换商店」面包屑与导航行同文案，必须限定区域才能点对；
    绝区零不传 region_frac，走全窗定位。
    """
    from .foreground_flow_runner import _focus_window, _ocr_locate_in_window, _window_rect

    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return None
        rect = _window_rect(hwnd)
        if not rect:
            return None
        left, top, width, height = rect
        fx1, fy1, fx2, fy2 = region_frac
        search_region = (
            left + int(width * fx1),
            top + int(height * fy1),
            max(1, int(width * (fx2 - fx1))),
            max(1, int(height * (fy2 - fy1))),
        )
        _focus_window(hwnd)
        center = _ocr_locate_in_window(logger, hwnd, text, search_region=search_region)
        if center:
            return center
        time.sleep(interval)
    return None


def _find_shop_entry(
    logger: logging.Logger,
    hwnd: int,
    template: str,
    text: str,
    scales,
    confidences,
    timeout_sec: int,
    stop_check: Optional[Callable[[], bool]],
) -> Optional[Tuple[int, int]]:
    """主菜单里的商店入口：模板优先，OCR 文字兜底。"""
    if template and os.path.isfile(template):
        center = _locate(hwnd, template, scales, confidences, timeout_sec, stop_check)
        if center:
            return center
    return _locate_ocr(logger, hwnd, text, SHORT_WAIT_SEC, stop_check)


def _tab_brightness(logger: logging.Logger, hwnd: int, center: Tuple[int, int]) -> Optional[float]:
    """采样页签中心小块的灰度均值，用于判定「是否选中态」。读不到返回 None。"""
    from PIL import ImageStat

    from .foreground_flow_runner import _window_rect

    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    pw = max(24, int(width * TAB_PATCH_WIDTH_FRAC))
    ph = max(16, int(height * TAB_PATCH_HEIGHT_FRAC))
    box = (
        left + int(center[0]) - pw // 2,
        top + int(center[1]) - ph // 2,
        pw,
        ph,
    )
    try:
        patch = pyautogui.screenshot(region=box).convert("L")
        return float(ImageStat.Stat(patch).mean[0])
    except Exception as exc:
        logger.debug(f"[每月兑换] 读取页签亮度失败: {exc}")
        return None


def _select_tab(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    label: str,
    tab_text: str,
    tab_template: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """点顶部页签并验证真的进入目标页签。

    定位优先级：未选中态模板 → OCR 页签文字 → 相邻页签中点
    （崩铁「余烬兑换」会被 RapidOCR 认成「余煨兑换」，中点兜底实测必要）。

    验证分两条路：
    - 模板命中（= 当前未选中）：点完必须等到模板消失，点两次仍不消失就整步失败；
    - 模板不命中（= 已在目标页签 / 模板失效）：用页签中心亮度确认选中态，
      亮度不够就点一次再测，仍不够同样失败 —— 绝不在无法确认页签的情况下往下买。
    """
    from .foreground_flow_runner import _click_window_local

    has_template = bool(tab_template) and os.path.isfile(tab_template)
    try:
        threshold = float(step.get("tab_selected_brightness", TAB_SELECTED_BRIGHTNESS) or TAB_SELECTED_BRIGHTNESS)
    except (TypeError, ValueError):
        threshold = TAB_SELECTED_BRIGHTNESS
    between = [str(t).strip() for t in (step.get("tab_between_texts") or []) if str(t).strip()]

    unselected = _locate(hwnd, tab_template, scales, confidences, TAB_WAIT_SEC, stop_check) if has_template else None
    center = unselected
    if center is None:
        center = _locate_ocr(logger, hwnd, tab_text, SHORT_WAIT_SEC, stop_check)
    if center is None and len(between) >= 2:
        left = _locate_ocr(logger, hwnd, between[0], SHORT_WAIT_SEC, stop_check)
        right = _locate_ocr(logger, hwnd, between[1], SHORT_WAIT_SEC, stop_check)
        if left and right:
            center = ((left[0] + right[0]) // 2, (left[1] + right[1]) // 2)
            logger.info(
                f"[{label}] 页签文字未识别，按相邻「{between[0]}」「{between[1]}」中点定位 @ {center}"
            )
    if center is None:
        return False, f"未找到「{tab_text}」页签"

    if has_template and unselected is not None:
        for attempt in range(1, TAB_ATTEMPTS + 1):
            if stop_check is not None and stop_check():
                return False, "用户停止"
            _click_window_local(hwnd, center[0], center[1], clicks=1)
            logger.info(f"[{label}] 已点击「{tab_text}」页签 @ {center}（第 {attempt} 次）")
            if _wait_gone(hwnd, tab_template, scales, confidences, TAB_SWITCH_SEC, stop_check):
                logger.info(f"[{label}] 页签已切换（未选中态模板消失）")
                _sleep(0.5, stop_check)
                return True, f"已切换到「{tab_text}」"
            logger.warning(f"[{label}] 点击后「{tab_text}」仍是未选中态，重试")
            unselected = _locate(hwnd, tab_template, scales, confidences, SHORT_WAIT_SEC, stop_check)
            if unselected is None:
                return True, f"已切换到「{tab_text}」"
            center = unselected
        return False, f"未能切换到「{tab_text}」页签（仍停留在其它页签）"

    # 模板不命中：已在目标页签，或模板/分辨率导致失效 —— 用亮度确认
    brightness = _tab_brightness(logger, hwnd, center)
    if brightness is not None and brightness >= threshold:
        logger.info(f"[{label}] 「{tab_text}」已是选中态（亮度 {brightness:.0f} ≥ {threshold:.0f}）")
        return True, f"「{tab_text}」已处于选中态"

    for attempt in range(1, TAB_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"
        _click_window_local(hwnd, center[0], center[1], clicks=1)
        logger.info(f"[{label}] 已点击「{tab_text}」页签 @ {center}（亮度判定路径，第 {attempt} 次）")
        _sleep(0.8, stop_check)
        brightness = _tab_brightness(logger, hwnd, center)
        if brightness is not None and brightness >= threshold:
            logger.info(f"[{label}] 页签已选中（亮度 {brightness:.0f} ≥ {threshold:.0f}）")
            return True, f"已切换到「{tab_text}」"

    if brightness is None:
        return False, f"无法读取「{tab_text}」页签亮度，未确认已进入目标页签"
    return False, f"未能切换到「{tab_text}」页签（亮度 {brightness:.0f} < {threshold:.0f}）"


def _open_exchange_page(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    label: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """主界面 → 商店 → 左侧导航 → 目标页签。返回 (是否到达, 说明)。"""
    from .foreground_flow_runner import _click_window_local

    shop_template = _abs_template(str(step.get("shop_entry_template") or ""))
    shop_text = str(step.get("shop_entry_text") or "商店").strip() or "商店"
    nav_text = str(step.get("nav_text") or "").strip()
    nav_region = _region_frac(step, "nav_region_frac")
    tab_text = str(step.get("tab_text") or "").strip()
    tab_template = _abs_template(str(step.get("tab_template") or ""))

    entry = None
    for attempt in range(1, MENU_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"
        entry = _find_shop_entry(
            logger, hwnd, shop_template, shop_text, scales, confidences,
            MENU_WAIT_SEC, stop_check,
        )
        if entry:
            break
        # 看不到商店入口：多半还停在大世界（需要 ESC 打开菜单），逐次 ESC 退一层再找
        logger.info(f"[{label}] 未见「{shop_text}」入口，按 ESC 打开主菜单（第 {attempt} 次）")
        _press_escape(hwnd)
        _sleep(1.5, stop_check)
    if entry is None:
        return False, f"未找到主菜单「{shop_text}」入口"

    _click_window_local(hwnd, entry[0], entry[1], clicks=1)
    logger.info(f"[{label}] 已点击「{shop_text}」入口 @ {entry}")
    _sleep(PAGE_SETTLE_SEC, stop_check)

    last_reason = ""
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"

        nav = None
        if nav_text:
            if nav_region:
                nav = _locate_ocr_region(
                    logger, hwnd, nav_text,
                    NAV_WAIT_SEC if attempt == 1 else SHORT_WAIT_SEC,
                    nav_region, stop_check,
                )
            else:
                nav = _locate_ocr(
                    logger, hwnd, nav_text,
                    NAV_WAIT_SEC if attempt == 1 else SHORT_WAIT_SEC,
                    stop_check,
                )
            if nav:
                _click_window_local(hwnd, nav[0], nav[1], clicks=1)
                logger.info(f"[{label}] 已点击左侧「{nav_text}」@ {nav}")
                _sleep(PAGE_SETTLE_SEC, stop_check)
            else:
                logger.warning(f"[{label}] 商店页未识别到「{nav_text}」（第 {attempt} 次）")

        if tab_text:
            tab_ok, tab_msg = _select_tab(
                logger, hwnd, step, label, tab_text, tab_template,
                scales, confidences, stop_check,
            )
            if tab_ok:
                return True, tab_msg
            last_reason = tab_msg
            logger.warning(f"[{label}] {tab_msg}（第 {attempt} 次）")
        else:
            # 没有页签要求：确认导航点过即可
            if nav:
                return True, f"已进入「{nav_text}」页面"
            last_reason = f"未识别到「{nav_text}」"

        if nav is None:
            # 连导航文字都认不到，再重试也是同一画面，直接收工
            break

    return False, last_reason or "未能进入目标页面"


def _buy_items(
    logger: logging.Logger,
    hwnd: int,
    items: List[Dict],
    confirm_template: str,
    label: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, List[str]]:
    """按清单逐项兑换。返回 (是否全部成功, 每项摘要)。

    单项判定沿用后勤商店：NO_POPUP 视为已达上限/已购（正常业务状态，不计失败并
    中断该项剩余次数）；模板与 OCR 都定位不到、或确认后弹窗不关 → 该项失败。
    """
    summaries: List[str] = []
    all_ok = True

    for raw in items:
        if stop_check is not None and stop_check():
            return False, summaries + ["用户停止"]

        name = str(raw.get("name") or "未命名商品").strip() or "未命名商品"
        try:
            count = max(1, int(raw.get("count", 1) or 1))
        except (TypeError, ValueError):
            count = 1

        item = dict(raw)
        item["name"] = name
        # 未给模板也未给 ocr_text 时直接拿商品名做 OCR 定位：兑换页卡片上就是商品名
        # 文字（实测 conf 0.66-0.79），先跑起来，有稳定模板再补 template。
        if not str(item.get("template") or "").strip() and not str(item.get("ocr_text") or "").strip():
            item["ocr_text"] = name

        bought = 0
        reason = ""
        for _ in range(count):
            result = _buy_once(
                logger, hwnd, item, confirm_template,
                scales, confidences, stop_check,
                label=label,
            )
            if result == BOUGHT:
                bought += 1
                continue
            if result == STOPPED:
                return False, summaries + [f"{name}: 用户停止"]
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
            logger.info(f"[{label}] {name} 完成 {bought}/{count}")
            summaries.append(f"{name}: {bought}/{count}")
        else:
            logger.warning(f"[{label}] {name} 未买够: {bought}/{count}（{reason or '未知'}）")
            summaries.append(f"{name}: {bought}/{count}（{reason or '未知'}）")
            if reason != "已达上限或无弹窗":
                all_ok = False

    return all_ok, summaries


def _leave_to_main_world(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    label: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> bool:
    """买完逐层 ESC 回主世界，以主世界特征模板（exit_marker_template）为到达标志。

    先检查再按键：已经在主世界时不再按 ESC（否则会重新打开菜单，把下一步定位带偏）。
    """
    marker = _abs_template(str(step.get("exit_marker_template") or ""))
    if not marker or not os.path.isfile(marker):
        # 没配到达标志时按两次 ESC 兜底（商店 → 菜单 → 主世界）
        logger.warning(f"[{label}] 未配置 exit_marker_template，按 ESC 两次兜底返回主界面")
        for _ in range(2):
            if stop_check is not None and stop_check():
                return False
            _press_escape(hwnd)
            _sleep(1.5, stop_check)
        return True

    for _ in range(EXIT_MAX_ESC):
        if stop_check is not None and stop_check():
            return False
        if _locate(hwnd, marker, scales, confidences, EXIT_CHECK_SEC, stop_check):
            return True
        _press_escape(hwnd)
        _sleep(1.5, stop_check)

    if stop_check is not None and stop_check():
        return False
    if _locate(hwnd, marker, scales, confidences, EXIT_CHECK_SEC, stop_check):
        return True
    logger.warning(f"[{label}] ESC 后仍未确认回到主界面（下一步会再校验主世界特征）")
    return False


def buy_monthly_exchange(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    *,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """执行「商店 → 兑换商店 → 目标页签」每月兑换（绝区零 / 崩铁共用）。

    step 字段：
    - items: [{"name", "template"?, "ocr_text"?, "count"?}]，为空则整段跳过
    - log_label: 日志前缀（如「信源兑换」「余烬兑换」）
    - shop_entry_template / shop_entry_text: 主菜单商店入口（模板优先，OCR 兜底）
    - nav_text / nav_region_frac: 左侧导航文字；region 为 [fx1, fy1, fx2, fy2] 窗口相对区域
    - tab_text / tab_template: 顶部页签（模板必须是未选中态，用于验证切换成功）
    - tab_between_texts: 目标页签左右邻居文字，OCR 认错目标文字时按两者中点定位
    - tab_selected_brightness: 选中态亮度阈值（默认 110，实测绝区零 145/78、崩铁 188/53、原神 186/114）
    - forbidden_texts: 错页签拦截词（如原神星辉兑换的宗室武器），命中即失败、不购买
    - confirm_template: 兑换确认弹窗的「确认」按钮模板
    - exit_marker_template: 买完后用于确认已回主世界的模板（主世界特征）

    返回 (是否成功, 摘要)。空清单也算成功（本月无采购需求），摘要写明「待定」。
    """
    label = str(step.get("log_label") or "每月兑换").strip() or "每月兑换"

    items = [it for it in (step.get("items") or []) if isinstance(it, dict)]
    if not items:
        logger.info(f"[{label}] 兑换清单待定（items 为空），本月跳过兑换（不打开商店）")
        return True, "兑换清单待定（未配置商品），跳过本月兑换"

    if stop_check is not None and stop_check():
        return False, "用户停止"

    confirm_template = _abs_template(str(step.get("confirm_template") or ""))
    if not confirm_template or not os.path.isfile(confirm_template):
        return False, "确认按钮模板缺失，无法兑换"

    page_ok, page_msg = _open_exchange_page(
        logger, hwnd, step, label, scales, confidences, stop_check,
    )
    if not page_ok:
        return False, page_msg

    guard_ok, guard_msg = _check_forbidden_texts(logger, hwnd, step, label)
    if not guard_ok:
        return False, guard_msg

    buy_ok, summaries = _buy_items(
        logger, hwnd, items, confirm_template, label, scales, confidences, stop_check,
    )
    if not buy_ok and any("用户停止" in s for s in summaries):
        return False, "用户停止 | " + "，".join(summaries)

    exit_ok = _leave_to_main_world(logger, hwnd, step, label, scales, confidences, stop_check)
    if stop_check is not None and stop_check():
        return False, "用户停止 | " + "，".join(summaries)
    detail = "，".join(summaries) if summaries else "无商品"
    if not exit_ok:
        detail += "（未确认返回主界面）"
    return buy_ok, detail
