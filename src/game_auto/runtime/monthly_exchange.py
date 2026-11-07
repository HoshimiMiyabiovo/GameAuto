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
from .monthly_shop_evidence import capture_shop_evidence
from .zzz_monthly_shop import (
    ALREADY_BOUGHT_REASONS,
    BOUGHT,
    CONFIRM_GONE_FAIL,
    ITEM_MISSING,
    NO_POPUP,
    NOT_FULL,
    PURCHASE_UNVERIFIED,
    REWARD_POPUP_STUCK,
    STOPPED,
    _buy_once,
    _frac_point,
    _locate,
    _locate_ocr,
    _wait_gone,
)

LOGGER = logging.getLogger("GameAutomation")

MENU_ATTEMPTS = 3            # ESC 打开主菜单的尝试次数（三次都见不到商店入口 = 界面不对）
MENU_WAIT_SEC = 10           # 单次找商店入口超时
ESC_HOLD_SEC = 0.15          # 扫描码 ESC 的按住时长（2026-11-05 绝区零实机值）
NAV_WAIT_SEC = 20            # 商店页里找左侧导航超时（页面加载有过渡动画）
TAB_WAIT_SEC = 15            # 找目标页签超时
TAB_SWITCH_SEC = 8           # 点页签后等「未选中态」消失的超时
TAB_ATTEMPTS = 2             # 页签点击重试次数
SHORT_WAIT_SEC = 8           # 第二次及以后的重试用短超时
PAGE_ATTEMPTS = 2            # 「左侧导航 → 顶部页签」组合重试次数
PAGE_SETTLE_SEC = 2.0        # 页面切换稳定等待
EXIT_MAX_ESC = 3             # 退出时最多按几次 ESC
EXIT_CHECK_SEC = 5           # 每次 ESC 后等主世界特征出现的超时

# ── 商品列表翻页（2026-11-02）────────────────────────────────────────────
# 兑换页的商品排成可滚动网格，同屏只有 2~3 行：崩铁余烬兑换实测可见「命运的足迹 /
# 冒险记录 / 信用点 / 无生残刃…」，清单里的星轨专票/星轨通票在折叠区以下。
# 一次整窗 OCR 看不到 → 被"看不到=已买过"吞掉，静默漏买。
# 改成：当前视图定位不到就往下翻着找，翻遍列表仍没有才算"已买过"。
# 2026-11-04 三次取证（三月七 `module/automation/local_input.py::mouse_scroll`）：
# 它是 `for _ in range(count): pyautogui.scroll(-1, _pause=True)` —— **一次只发一格、
# 每格之间带 pause**，日志里「滚轮滚动 -12 次」「-40 次」就是这个循环。旧实现
# `pyautogui.scroll(-5)` 一次性发 delta=-600，Unity 网格逐帧吃 wheel 事件时会被
# 吞掉，表现正是「8 屏滚完商品卡一行没动」。这里照抄逐格 + 间隔，并改成 -12。
SCROLL_STEP = -12                   # 每屏 12 格单格滚轮事件（三月七同款量级）
SCROLL_TICK_PAUSE_SEC = 0.08        # 单格之间的间隔，让游戏逐帧处理
SCROLL_SETTLE_SEC = 0.6             # 滚完等列表重绘
SCROLL_MAX_PAGES = 8                # 最多往下翻几屏
SCROLL_ANCHOR_DEFAULT = (0.60, 0.55)  # 滚轮落点（窗口比例）：商品网格中部
# 2026-11-03 修：原来用 32x18 缩略图的 MD5 判断"滚动了没有"，卡片角标有闪烁/
# 光效，签名每帧都不同 → 「列表没变化」的熔断一次都没触发，每件商品 8 轮 x 25s
# OCR 全跑满（3 件烧了 12 分钟）。改成灰度缩略图的平均像素差：真实滚动差 > 10，
# 角标闪烁的面积极小（差 < 1），连续两次低于阈值才判定滚轮无效。
SCROLL_DIFF_THRESHOLD = 2.0
SCROLL_STATIC_STREAK = 2
# 翻页时每屏只需一次整窗 OCR 就能判断有没有该商品，不再等满 25s
SCROLL_LOCATE_OCR_TIMEOUT = 6
# 2026-11-04 二次实锤：像素差也被背景动画骗了 —— 余烬兑换每屏差 30~40，
# 失败截图里商品卡一行没换，effective 却被记成 8、scroll_verified_dead=0，
# 上层因此把「看不到」当「已买过」（07:51:45 日志实锤的漏买通道）。
# 现在以「列表文字签名」为准判断内容是否真的换了一批；滚轮连续无效时
# 改用拖动右侧滚动条（截图右缘能看到滚动条），两种都无效才判 dead。
SCROLL_BAR_X_FRAC = 0.995        # 滚动条贴着窗口右缘
SCROLL_BAR_TOP_FRAC = 0.34       # 滑块上部（往下拖的起点）
SCROLL_BAR_BOTTOM_FRAC = 0.76    # 往下拖的终点 / 往上拖的起点
# 2026-11-04 五次取证：`tasks/daily/ember_exchange.py::_scroll_to_bottom_for_retry`
# 才是三月七真正的「翻页」—— 商品在列表最底下，它不逐屏翻，而是顶部整屏扫一遍
# 后把没命中的商品记进 unresolved_items，鼠标移到列表中心
# `RETRY_SCROLL_CENTER_CROP=(955/1920,535/1080,10/1920,10/1080)`，
# 一次性 `mouse_scroll(120,-1,False)` **滚到底**，再补扫一次。
SCROLL_TO_BOTTOM_TICKS = 120
SCROLL_TO_BOTTOM_ANCHOR = (955 / 1920, 535 / 1080)
SCROLL_BOTTOM_TICK_SEC = 0.01
SCROLL_BOTTOM_SETTLE_SEC = 1.0

# 已选中页签的亮度判据：选中态是亮色胶囊（绝区零黄底 / 崩铁白底），
# 未选中态是暗底白字。实测 1919x1079：绝区零 145 vs 78，崩铁 188 vs 53。
# 取区块尺寸按窗口比例换算，阈值可用 step.tab_selected_brightness 覆盖。
TAB_PATCH_WIDTH_FRAC = 0.05
TAB_PATCH_HEIGHT_FRAC = 0.035
TAB_SELECTED_BRIGHTNESS = 110.0
FORBIDDEN_MIN_CONF = 0.5    # 错页签拦截的 OCR 置信度下限
CONTENT_MIN_CONF = 0.45     # 页签内容强校验（专属商品 OCR）置信度下限


def _content_markers_present(
    logger: logging.Logger,
    hwnd: int,
    markers: Sequence[str],
) -> Optional[str]:
    """全窗 OCR 找目标页签的专属商品（如原神星尘页的「摩拉」）。

    页签选中态的模板/亮度天然容易混淆（实测未选中态模板在已选中页仍 0.854），
    所以有专属商品可查时以页面内容为准。窗口不可用/OCR 失败返回 None，
    调用方必须按「未确认」处理，绝不放行购买。
    """
    markers = [str(m).strip() for m in markers or [] if str(m).strip()]
    if not markers:
        return None
    try:
        from .ocr_click_tester import ocr_window_content

        items = ocr_window_content(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败按未确认处理
        logger.debug(f"[每月兑换] 页签内容校验 OCR 失败: {exc}")
        return None
    for item in items or []:
        text = str(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < CONTENT_MIN_CONF:
            continue
        for marker in markers:
            if marker in text:
                return marker
    return None


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
    """在游戏窗口内按 ESC：必须发带硬件扫描码的按键，且按住一小会儿。

    2026-11-05 绝区零实机逐项对比（双方都提权、UIPI 无阻碍、前台窗口就是游戏）：
    - `pyautogui.press("escape")` / `keyDown+keyUp` / `press("f3")` —— 全无反应，
      主菜单根本打不开，`_open_exchange_page` 按 3 次 ESC 全是白按；
    - `win32api.keybd_event(vk, MapVirtualKey(vk), ...)` 按住 0.15s ——
      vk=114 立刻开出城募页面、vk=27 一次退回主世界、再一次开出主菜单。
    Unity 只认带扫描码的按键事件。这里统一改掉，崩铁/异环的 ESC 退出链路
    同样受益；用 finally 保证异常时也会松开键，不留下卡住的 ESC。
    """
    import win32api
    import win32con

    from .foreground_flow_runner import _focus_window

    _focus_window(hwnd)
    vk = win32con.VK_ESCAPE
    scan = win32api.MapVirtualKey(vk, 0)
    try:
        win32api.keybd_event(vk, scan, 0, 0)
        time.sleep(ESC_HOLD_SEC)
    finally:
        win32api.keybd_event(vk, scan, win32con.KEYEVENTF_KEYUP, 0)


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
    point_frac: Optional[Sequence[float]] = None,
) -> Optional[Tuple[int, int]]:
    """主菜单里的商店入口：模板 → OCR 文字 → 固定比例坐标。

    2026-11-05 绝区零实机：主菜单底部商城入口是「24 HOUR」纯图标，OCR 把
    它认成 `24 / HOUR`，中文「商城」标签太小也常常识别不到；模板在动态背景下
    也可能失配。`point_frac` 是最后一道兜底，坐标系与 `_locate` / `_locate_ocr`
    一致（窗口局部像素），调用方只在按过 ESC 之后才传，避免在主世界瞎点。
    """
    if template and os.path.isfile(template):
        center = _locate(hwnd, template, scales, confidences, timeout_sec, stop_check)
        if center:
            return center
    center = _locate_ocr(logger, hwnd, text, SHORT_WAIT_SEC, stop_check)
    if center:
        return center
    if point_frac:
        fixed = _frac_point(hwnd, point_frac)
        if fixed:
            logger.info("[每月兑换] 模板/OCR 都没命中商店入口，改用固定比例坐标 @ %s", fixed)
            return fixed
    return None


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
    content_markers = [
        str(t).strip() for t in (step.get("tab_content_markers") or []) if str(t).strip()
    ]
    tab_point_frac = step.get("tab_point_frac")

    # 内容优先：目标页签有专属商品（原神星尘页的摩拉/嬗变之尘等）时，以页面内容为准。
    # 页签选中态模板/亮度在视觉上天然容易混淆，不再作为该游戏的放行依据。
    if content_markers:
        hit = _content_markers_present(logger, hwnd, content_markers)
        if hit:
            logger.info(f"[{label}] 已在「{tab_text}」（页面出现专属商品「{hit}」）")
            return True, f"已在「{tab_text}」"
        # 页签文字被 OCR 认错（异环「迷迭兑换」→「迷送兑换」）时按固定比例坐标点，
        # 点完仍要用专属商品验证，坐标只负责"点哪里"。
        center = _frac_point(hwnd, tab_point_frac) if tab_point_frac else None
        if center:
            logger.info(f"[{label}] 按固定比例坐标定位「{tab_text}」页签 @ {center}")
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
        for attempt in range(1, TAB_ATTEMPTS + 1):
            if stop_check is not None and stop_check():
                return False, "用户停止"
            _click_window_local(hwnd, center[0], center[1], clicks=1)
            logger.info(f"[{label}] 已点击「{tab_text}」页签 @ {center}（内容校验，第 {attempt} 次）")
            _sleep(1.0, stop_check)
            hit = _content_markers_present(logger, hwnd, content_markers)
            if hit:
                logger.info(f"[{label}] 内容确认已在「{tab_text}」（看到专属商品「{hit}」）")
                return True, f"已切换到「{tab_text}」"
            logger.warning(f"[{label}] 点击后仍看不到「{tab_text}」专属商品，重试")
        return False, (
            f"未确认在「{tab_text}」"
            f"（未看到 {'/'.join(content_markers)} 等专属商品），未购买"
        )

    center = _frac_point(hwnd, tab_point_frac) if tab_point_frac else None
    if center:
        logger.info(f"[{label}] 按固定比例坐标定位「{tab_text}」页签 @ {center}")
    unselected = None
    if center is None:
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

    # 固定比例坐标只在按过 ESC（确认已进主菜单）之后才允许用；第一轮还在
    # 主世界时直接点会把点击浪费在大世界上。
    entry_point_frac = step.get("shop_entry_point_frac")
    entry = None
    for attempt in range(1, MENU_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"
        entry = _find_shop_entry(
            logger, hwnd, shop_template, shop_text, scales, confidences,
            MENU_WAIT_SEC, stop_check,
            point_frac=entry_point_frac if attempt > 1 else None,
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


def _scroll_anchor_point(hwnd: int, anchor_frac) -> Optional[Tuple[int, int]]:
    """滚轮落点的屏幕坐标（窗口相对比例 → 屏幕绝对坐标）。"""
    try:
        from .foreground_flow_runner import _window_rect

        rect = _window_rect(hwnd)
        if not rect:
            return None
        left, top, width, height = rect
        fx, fy = float(anchor_frac[0]), float(anchor_frac[1])
        if not (0.0 < fx < 1.0 and 0.0 < fy < 1.0):
            return None
        return (int(left + width * fx), int(top + height * fy))
    except Exception as exc:  # noqa: BLE001 - 换算失败就当不能滚
        LOGGER.debug("[每月兑换] 滚轮落点换算失败: %s", exc)
        return None


def _scroll_shop_list(
    logger: logging.Logger,
    hwnd: int,
    amount: int,
    anchor_frac,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """在商品网格上滚一屏：逐格发送单格滚轮事件，返回是否执行成功。

    不能写成 `pyautogui.scroll(amount)` 一次发完 —— 三月七的实机日志证明
    游戏列表只吃「一格一个事件、事件之间有间隔」的滚动。见 SCROLL_STEP 注释。
    """
    point = _scroll_anchor_point(hwnd, anchor_frac)
    if not point:
        return False
    try:
        from .foreground_flow_runner import _focus_window

        _focus_window(hwnd)
        pyautogui.moveTo(point[0], point[1])
        step = -1 if int(amount) < 0 else 1
        for _ in range(abs(int(amount))):
            if stop_check is not None and stop_check():
                return False
            pyautogui.scroll(step)
            _sleep(SCROLL_TICK_PAUSE_SEC, stop_check)
    except Exception as exc:  # noqa: BLE001 - 滚不动不影响主流程
        logger.debug(f"[每月兑换] 滚动商品列表失败: {exc}")
        return False
    _sleep(SCROLL_SETTLE_SEC, stop_check)
    return True


def _list_text_signature(logger: logging.Logger, hwnd: int):
    """当前商品列表的 OCR 文字集合签名，用来判列表是否真换了内容。

    2026-11-04 20:17 实跑教训：首版把 y//24 分桶塞进签名，卡片光效让 OCR
    框每帧抖 1~2px 就跨桶，8 屏全判「文字变化」，`effective=8`、一次都没
    切到拖动滚动条，严格语义形同虚设。现在**只收文字、不带坐标**，并且用
    Jaccard 差异率（见 `_signatures_differ`）容忍少量 OCR 抖动。
    取不到 OCR 时返回 None，调用方自动退回旧的像素差判据。
    """
    try:
        from .ocr_click_tester import ocr_window_content

        items = ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:  # noqa: BLE001 - OCR 不可用就退回像素差
        LOGGER.debug("[每月兑换] 列表文字签名 OCR 失败: %s", exc)
        return None
    parts = set()
    for item in items:
        text = str(item.get("text") or "").strip()
        if len(text) < 2:
            continue
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 0.55:
            continue
        parts.add(text)
    return frozenset(parts) if parts else None


def _signatures_differ(before, after, ratio: float = 0.12) -> bool:
    """两屏文字集合是否真的换了一批内容（差异率 > 12% 才算滚了）。

    游戏卡片光效会让单帧 OCR 少认/多认一两条，全等比较太严；用对称差集
    占并集的比例容忍这种噪声。任一为空时按"变了"处理，交回像素差兜底。
    """
    if before is None or after is None:
        return True
    a, b = frozenset(before), frozenset(after)
    if a == b:
        return False
    union = a | b
    return bool(union) and len(a ^ b) / len(union) > ratio


def _drag_shop_scrollbar(
    logger: logging.Logger,
    hwnd: int,
    downward: bool = True,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """滚轮对游戏内网格无效时的兜底：直接拖右侧滚动条滑块。

    2026-11-04 证据图右缘 x≈957/960、y≈150~430/540 有一条滚动条滑块，
    说明列表本身可滚，只是 Unity 网格不吃 WM_MOUSEWHEEL。
    """
    try:
        from .foreground_flow_runner import _focus_window, _window_rect

        rect = _window_rect(hwnd)
        if not rect:
            return False
        left, top, width, height = rect
        x = int(left + width * SCROLL_BAR_X_FRAC)
        y_from = SCROLL_BAR_TOP_FRAC if downward else SCROLL_BAR_BOTTOM_FRAC
        y_to = SCROLL_BAR_BOTTOM_FRAC if downward else SCROLL_BAR_TOP_FRAC
        _focus_window(hwnd)
        pyautogui.moveTo(x, int(top + height * y_from))
        pyautogui.mouseDown()
        pyautogui.moveTo(x, int(top + height * y_to), duration=0.35)
        pyautogui.mouseUp()
    except Exception as exc:  # noqa: BLE001 - 拖不动就如实返回 False
        logger.debug(f"[每月兑换] 拖动滚动条失败: {exc}")
        return False
    _sleep(SCROLL_SETTLE_SEC, stop_check)
    return True


def _scroll_list_to_edge(
    logger: logging.Logger,
    hwnd: int,
    down: bool,
    anchor_frac,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """三月七 `_scroll_to_bottom_for_retry` 同款：一次性逐格滚到列表底（或回顶）。

    落点用它的 `RETRY_SCROLL_CENTER_CROP`（列表正中央），120 个单格
    `pyautogui.scroll(±1, _pause=False)` 连续发出，中间只留 10ms 让游戏
    逐帧处理。不要把它拆成「每屏滚一点 + 每屏 OCR」—— 商品就在最底下，
    一屏一屏找永远碰不到，还会在每屏 8 秒的 OCR 里让惯性回弹。
    """
    point = _scroll_anchor_point(hwnd, anchor_frac)
    if not point:
        return False
    try:
        from .foreground_flow_runner import _focus_window

        _focus_window(hwnd)
        pyautogui.moveTo(point[0], point[1])
        _sleep(SCROLL_BOTTOM_SETTLE_SEC, stop_check)
        step = -1 if down else 1
        for _ in range(SCROLL_TO_BOTTOM_TICKS):
            if stop_check is not None and stop_check():
                return False
            pyautogui.scroll(step, _pause=False)
            _sleep(SCROLL_BOTTOM_TICK_SEC, stop_check)
    except Exception as exc:  # noqa: BLE001 - 滚不动就如实返回 False
        logger.debug(f"[每月兑换] 一次滚到列表边缘失败: {exc}")
        return False
    _sleep(SCROLL_SETTLE_SEC, stop_check)
    return True


def _list_thumb(hwnd: int, anchor_frac):
    """取商品区灰度缩略图（32x18 像素列表）；取不到返回 None。

    不再用 MD5：卡片角标闪烁/光效会导致每帧哈希都不同，`sig == before` 永远
    不成立，熔断形同虚设。像素列表可以算平均差，对小幅动画不敏感。
    """
    try:
        from .foreground_flow_runner import _window_rect

        rect = _window_rect(hwnd)
        if not rect:
            return None
        left, top, width, height = rect
        fx, fy = float(anchor_frac[0]), float(anchor_frac[1])
        box = (
            int(left + width * max(0.0, fx - 0.25)),
            int(top + height * max(0.0, fy - 0.25)),
            max(1, int(width * 0.5)),
            max(1, int(height * 0.5)),
        )
        img = pyautogui.screenshot(region=box).convert("L").resize((32, 18))
        return list(img.getdata())
    except Exception as exc:  # noqa: BLE001 - 取不到缩略图按"未知"处理
        LOGGER.debug("[每月兑换] 取列表缩略图失败: %s", exc)
        return None


def _thumb_diff(before, after) -> float:
    """两张缩略图的平均像素差（0~255）；任一为空返回一个大值。"""
    if not before or not after or len(before) != len(after):
        return 999.0
    return sum(abs(int(a) - int(b)) for a, b in zip(before, after)) / len(before)


def _scroll_find_item(
    logger: logging.Logger,
    hwnd: int,
    item: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    label: str,
    diag: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[int, int]]:
    """当前视图没有该商品时，往下翻着找（长列表页必备）。

    2026-11-02 实锤：崩铁余烬兑换页同屏只有 2~3 行商品，证据图里能看到的只有
    「命运的足迹 / 冒险记录 / 煤油以太 / 遗失碎金 / 信用点 / 无生残刃…」，
    清单里的**星轨专票、星轨通票在折叠区下方**，一次整窗 OCR 根本看不到 →
    被"看不到=已买过"吞掉，静默漏买。

    2026-11-03 二次实锤（用户复核）：这个修复**实测无效**——
      - 原先用 32x18 缩略图 MD5 判断"滚没滚"，卡片角标闪烁让签名每帧都变，
        「列表没变化」的熔断一次都没触发；
      - 滚轮对崩铁商城网格到底有没有效，从未被验证，校验失效后这个未知被完全掩盖；
      - 每屏 OCR 还等满 25s，8 屏 × 3 件 ≈ 12 分钟纯空转。
    现在：像素平均差判滚动 + 连续两次判定不动就熔断 + 每屏 OCR 缩短到 6s，
    并且翻页失败要落 flow_failures 证据（截图 + 当帧文字），不再静默"按已购买处理"。

    2026-11-04 三次实锤：像素差同样会被背景动画骗——余烬兑换每屏差 30~40，
    可商品卡一行没换，`effective` 被算成 8、`scroll_verified_dead=0`，上层又
    把「看不到」当「已买过」（07:51:45 日志：三项 0/5 按已购买处理）。
    现在每屏取一次 OCR 文字集合签名（带 12% 抖动容忍）：签名不变就是没滚；
    滚轮连续两次没滚就改用拖动右缘滚动条，两种都无效才让 diag 标 dead。
    20:17 实跑又补一层：滚动本身改成逐格单事件（见 SCROLL_STEP），否则
    签名判得再准也只是在原地判"没滚"。

    diag（2026-11-04）：调用方传入共享 dict，用于把"滚动到底有没有生效"暴露给
    `_buy_items` 的判定——`effective_pages` 是画面真的变过的页数；翻遍了仍没找到
    且 `scroll_verified_dead` 为 1（一屏都没滚成功）时，调用方**不得**把"看不到"
    当"已买过"（滚轮无效时折叠区下的商品永远看不到，那是漏买不是买完）。
    """
    from .zzz_monthly_shop import _locate_item

    anchor = SCROLL_ANCHOR_DEFAULT
    item_name = str(item.get("name") or "商品")
    last_diff = 999.0
    scrolled = 0
    effective = 0
    mode = "wheel"
    diag = diag if diag is not None else {}
    try:
        prev = _list_thumb(hwnd, anchor)
        prev_sig = _list_text_signature(logger, hwnd)
        # 只有真拿到 OCR 文字签名才启用「改拖滚动条」这条新路径；无 OCR 的
        # 测试/降级环境保持旧的像素差熔断行为，不会去动用户的鼠标。
        sig_enabled = prev_sig is not None
        static_streak = 0
        if sig_enabled:
            # 2026-11-04：三月七 `ember_exchange.py` 的做法是「顶部整屏扫一遍 →
            # 一次性滚 120 格到底 → 底部补扫」，没有命中才继续别的尝试。
            # 用户实锤清单商品就在最底下；我们先补这一刀，8 屏逐格滚动留作兜底。
            logger.info(f"[{label}] 先按三月七口径一次滚到列表底部补扫")
            if _scroll_list_to_edge(
                logger, hwnd, True, SCROLL_TO_BOTTOM_ANCHOR, stop_check
            ):
                bottom_center = _locate_item(
                    logger, hwnd, item, scales, confidences, stop_check, label,
                    ocr_timeout=SCROLL_LOCATE_OCR_TIMEOUT,
                )
                if bottom_center:
                    logger.info(f"[{label}] 列表底部补扫命中商品 @ {bottom_center}")
                    diag["effective_pages"] = max(1, effective)
                    return bottom_center
                # 底部也没有 → 滚回顶部，继续原来的逐屏查找
                _scroll_list_to_edge(
                    logger, hwnd, False, SCROLL_TO_BOTTOM_ANCHOR, stop_check
                )
        for page in range(1, SCROLL_MAX_PAGES + 1):
            if stop_check is not None and stop_check():
                return None
            if mode == "drag":
                advanced = _drag_shop_scrollbar(logger, hwnd, True, stop_check)
            else:
                advanced = _scroll_shop_list(logger, hwnd, SCROLL_STEP, anchor, stop_check)
            if not advanced:
                logger.warning(f"[{label}] 商品列表滚不动（{mode} 未执行），停止翻页")
                break
            cur = _list_thumb(hwnd, anchor)
            last_diff = _thumb_diff(prev, cur)
            cur_sig = _list_text_signature(logger, hwnd)
            if sig_enabled and cur_sig is not None:
                changed = _signatures_differ(prev_sig, cur_sig)
            else:
                # 降级：拿不到文字签名时仍按旧的像素差判断
                changed = cur is not None and last_diff >= SCROLL_DIFF_THRESHOLD
            logger.info(
                f"[{label}] 翻页第 {page} 屏（{mode}）：像素差 {last_diff:.1f}，"
                f"列表文字{'变化' if changed else '未变'}"
            )
            if not changed:
                static_streak += 1
                logger.warning(
                    f"[{label}] 第 {page} 次 {mode} 后列表内容没变（像素差 {last_diff:.1f}）"
                )
                if sig_enabled and mode == "wheel" and static_streak >= SCROLL_STATIC_STREAK:
                    # 像素差大但文字一行没换 = 背景动画，滚轮其实没滚列表。
                    # 截图右缘有滚动条 → 直接拖滑块再试，别再空烧 8 屏 OCR。
                    logger.warning(f"[{label}] 滚轮对该网格无效，改用拖动右侧滚动条")
                    mode = "drag"
                    static_streak = 0
                elif static_streak >= SCROLL_STATIC_STREAK:
                    logger.warning(
                        f"[{label}] 连续 {static_streak} 次翻不动，停止翻页（不再空等 OCR）"
                    )
                    break
            else:
                static_streak = 0
                effective += 1
            prev = cur if cur is not None else prev
            prev_sig = cur_sig if cur_sig is not None else prev_sig
            scrolled = page
            center = _locate_item(
                logger, hwnd, item, scales, confidences, stop_check, label,
                ocr_timeout=SCROLL_LOCATE_OCR_TIMEOUT,
            )
            if center:
                logger.info(f"[{label}] 翻到第 {page} 屏后找到商品 @ {center}")
                # 不滚回去：返回的坐标马上要用来点击，回滚会点错位置
                diag["effective_pages"] = effective
                return center
        # 没找到：滚回顶部，下一个商品从头找（滚轮无效时别白滚）
        if scrolled:
            for _ in range(SCROLL_MAX_PAGES):
                if not _scroll_shop_list(logger, hwnd, -SCROLL_STEP, anchor, stop_check):
                    break
            if mode == "drag":
                _drag_shop_scrollbar(logger, hwnd, False, stop_check)
        diag["effective_pages"] = effective
        diag["scroll_verified_dead"] = 1 if effective == 0 else 0
        _record_scroll_failure(logger, hwnd, label, item_name, scrolled, last_diff)
        return None
    except Exception as exc:  # noqa: BLE001 - 翻页兜底失败不能拖垮采购
        logger.warning(f"[{label}] 翻页查找异常（已忽略）: {exc}")
        return None


def _record_scroll_failure(
    logger: logging.Logger,
    hwnd: int,
    label: str,
    item_name: str,
    scrolled: int,
    last_diff: float,
) -> None:
    """翻页翻遍了仍没找到商品 → 落一条失败取证（截图 + 当帧 OCR）。

    用户 2026-11-03 要求：这种"按已购买处理"的静默路径必须留证据，否则
    滚轮是否真的滚了、页面停在哪一屏，事后完全查不到。
    """
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key=label,
            step=f"{item_name}·翻页查找",
            error=(
                f"翻页 {scrolled} 屏仍未找到（末次像素差 {last_diff:.1f}；"
                f"以列表文字签名为准，像素差大也可能是背景动画，"
                f"滚轮无效时会改拖右侧滚动条）"
            ),
            hwnd=hwnd,
            save_image=True,
            extra={"kind": "monthly_shop_scroll", "scrolled_pages": scrolled},
        )
    except Exception as exc:  # noqa: BLE001 - 取证失败不能影响采购
        logger.debug(f"[{label}] 翻页失败取证写入失败（已忽略）: {exc}")


def _buy_items(
    logger: logging.Logger,
    hwnd: int,
    items: List[Dict],
    confirm_template: str,
    label: str,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
    scroll_search: bool = True,
    step: Optional[Dict] = None,
) -> Tuple[bool, List[str]]:
    """按清单逐项兑换。返回 (是否全部成功, 每项摘要)。

    单项判定沿用后勤商店：NO_POPUP 视为已达上限/已购（正常业务状态，不计失败并
    中断该项剩余次数）；**模板与 OCR 都定位不到也按"已买过"处理**（用户 2026-11-01
    拍板：兑换页商品到月上限后就不展示了，看不到 ≠ 失败）；只有确认后弹窗不关
    才算真失败。

    scroll_search=True 时，当前视图定位不到会先往下翻页找（长列表页必备），
    翻遍列表仍没有才按"已买过"处理。**例外（2026-11-04）**：翻页跑完但一屏都没
    滚动成功（`scroll_verified_dead`，滚轮对该列表无效）时，"看不到"可能是折叠区
    下的商品根本没被扫到——这时判失败，不按已购买处理，下次批次会带着
    flow_failures 的证据重试。
    """
    summaries: List[str] = []
    all_ok = True
    missing_count = 0
    scroll_diag: Dict[str, int] = {}

    def _scroll_finder(*args, **kwargs):
        # 包一层把共享 diag 塞进去；模块级名字在调用时解析，测试 patch
        # mx._scroll_find_item 依然生效。
        return _scroll_find_item(*args, diag=scroll_diag, **kwargs)

    scroll_finder = _scroll_finder if scroll_search else None

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
            # step 只在调用方传了配置时才透传：旧回归套件直接打桩 `_buy_once`
            # 且签名不含新参数，无条件传 keyword 会把它们全部打挂。
            buy_kwargs = {"label": label, "scroll_finder": scroll_finder}
            if step is not None:
                buy_kwargs["step"] = step
            result = _buy_once(
                logger, hwnd, item, confirm_template,
                scales, confidences, stop_check, **buy_kwargs,
            )
            if result == BOUGHT:
                bought += 1
                continue
            if result == STOPPED:
                return False, summaries + [f"{name}: 用户停止"]
            if result == NO_POPUP:
                # 已在 `_buy_once` 里重试过一次（浮层遮挡会重试成功），重试仍无弹窗
                # 按「已达上线」处理（用户 2026-11-01 口径：到上限后商品不再可兑）
                reason = "已达上限或无弹窗"
                break
            if result == NOT_FULL:
                # 2026-11-03 用户口径：页面还显示剩余数量就不能算完成（原神 4/5 事故）
                reason = "页面仍有剩余，未买满"
                break
            if result == ITEM_MISSING:
                if scroll_finder is not None and scroll_diag.get("scroll_verified_dead"):
                    # 2026-11-04：翻页开着却一屏都没滚成功——"看不到"不能当"已买过"，
                    # 否则滚轮失效时折叠区下的商品永远漏买（11-03 中午崩铁 0/5 实锤）。
                    # 判失败 → 本步不成功 → 不写月度标记，下批带着翻页取证重试。
                    reason = "商品未找到且页面无法滚动（滚轮疑似无效），不能按已购买处理"
                    break
                missing_count += 1
                reason = "页面上没看到该商品，按已购买处理"
                break
            if result == CONFIRM_GONE_FAIL:
                reason = "确认后弹窗未关"
                break
            if result == REWARD_POPUP_STUCK:
                reason = "「获得物品」浮层未关闭，无法继续采购"
                break
            if result == PURCHASE_UNVERIFIED:
                reason = "点完确认但限购/库存数字未变，疑似假成功"
                break

        if bought >= count:
            logger.info(f"[{label}] {name} 完成 {bought}/{count}")
            summaries.append(f"{name}: {bought}/{count}")
        else:
            log_fn = logger.info if reason in ALREADY_BOUGHT_REASONS else logger.warning
            log_fn(f"[{label}] {name}: {bought}/{count}（{reason or '未知'}）")
            summaries.append(f"{name}: {bought}/{count}（{reason or '未知'}）")
            if reason not in ALREADY_BOUGHT_REASONS:
                all_ok = False

    if items and missing_count == len(items):
        # 整张清单一件都没看到：按用户口径算"本月已买完"，但要留一条可排查的日志
        logger.warning(
            f"[{label}] 清单 {len(items)} 项全部未在页面上出现，按本月已购买处理"
            "（如确认没买过，请检查商品模板 / OCR 文字）"
        )

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
    - item_scroll_search: 当前视图找不到商品时是否往下翻页找（默认开；关掉就是
      旧行为——一次整窗 OCR，看不到即按已买过）

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

    content_markers = [
        str(t).strip() for t in (step.get("tab_content_markers") or []) if str(t).strip()
    ]
    if content_markers:
        hit = _content_markers_present(logger, hwnd, content_markers)
        if not hit:
            return False, (
                f"未确认在目标页签（未看到 {'/'.join(content_markers)} 等专属商品），未购买"
            )
        logger.info(f"[{label}] 购买前内容复核通过（看到「{hit}」）")

    guard_ok, guard_msg = _check_forbidden_texts(logger, hwnd, step, label)
    if not guard_ok:
        return False, guard_msg

    # 采购前留一张货架截图：本次判定"看不到商品算已买过"，复核时要能看出
    # 当时页面上到底有没有这些东西（模板/OCR 失灵 vs 真买完）。
    capture_shop_evidence(logger, hwnd, flow_label=label, stage="采购前")

    buy_ok, summaries = _buy_items(
        logger, hwnd, items, confirm_template, label, scales, confidences, stop_check,
        scroll_search=bool(step.get("item_scroll_search", True)),
        step=step,
    )
    if not buy_ok and any("用户停止" in s for s in summaries):
        return False, "用户停止 | " + "，".join(summaries)

    # 买完仍停在兑换页，趁现在留"采购后"证据（离开后只剩主世界，没有复核价值）
    detail = "，".join(summaries) if summaries else "无商品"
    capture_shop_evidence(
        logger, hwnd, flow_label=label, stage="采购后",
        summary=detail + ("" if buy_ok else "（有未买够项，见日志）"),
        annotate=True,
    )

    exit_ok = _leave_to_main_world(logger, hwnd, step, label, scales, confidences, stop_check)
    if stop_check is not None and stop_check():
        return False, "用户停止 | " + "，".join(summaries)
    if not exit_ok:
        detail += "（未确认返回主界面）"
    return buy_ok, detail
