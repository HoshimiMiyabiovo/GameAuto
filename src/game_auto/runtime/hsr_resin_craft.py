"""崩铁「自塑尘脂」每月合成（2026-11-01）。

用户在实机里逐步给的路径（4 张截图确认）：

    主界面 B → 背包 → 顶部**倒数第二个**图标「贵重物」→ 第一行第 5 格「自塑尘脂」
    → 右侧「获得途径」条「万能合成机 - 使用遗器残骸合成」→ 合成界面（材料合成）

合成界面唯一可靠的判据是右侧那行「本月还可合成：N」：

- N == 0，或底部按钮变成「已达上限」/「材料不足」→ 本月已完成，一步都不点，正常返回；
- N > 0 → 点底部合成按钮，每点一次重读 N，直到 N 归零 / 按钮状态改变 / 达到 max_rounds。

为什么不按「点一次算一次成功」写：2026-11-01 首次取证时本月已经是
「本月还可合成 0 / 已达上限」，可合成态在实机上没法直接观察。用 N 的前后变化做
判定，无论界面是「点一次合一个」还是「先弹数量框」，都能自洽；读不到 N 时退化成
只看按钮状态，并靠 max_rounds 兜底，绝不无限点。

调用方：`foreground_flow_runner._dispatch_step_action` 的 `hsr_resin_craft` 分支。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pyautogui

from ..settings.paths import BASE_DIR
from .zzz_monthly_shop import _frac_point, _locate, _locate_ocr

LOGGER = logging.getLogger("GameAutomation")

LABEL = "自塑尘脂"

BAG_OPEN_ATTEMPTS = 3       # 按 B 开背包的尝试次数（界面卡在别处时先 ESC 清一层）
# 背包标题只认左上角区域：ESC 菜单中央也有「背包」按钮，不能当"背包已打开"
BAG_TITLE_REGION = (0.0, 0.0, 0.30, 0.18)
BAG_WAIT_SEC = 6            # 按 B 后等背包标题出现的超时
TAB_WAIT_SEC = 8            # 找「贵重物」页签超时
DETAIL_WAIT_SEC = 8         # 点完自塑尘脂后等右侧详情出现
SOURCE_WAIT_SEC = 6         # 找「万能合成机」条超时
SYNTH_WAIT_SEC = 15         # 点「万能合成机」后等合成界面出现
CLICK_SETTLE_SEC = 1.0      # 单次点击后的稳定等待
CRAFT_CLICK_WAIT_SEC = 1.2  # 点合成按钮后等界面刷新
EXIT_MAX_ESC = 3            # 退出时最多按几次 ESC
EXIT_CHECK_SEC = 5          # 每次 ESC 后等主世界特征的超时
MAX_ROUNDS_DEFAULT = 30     # 合成循环上限（每月上限远小于它，纯粹防死循环）
STUCK_LIMIT = 2             # 连续两次「点了但 N 没变」就判定卡住
OCR_MIN_CONF = 0.5          # 判据文字的最低置信度
GRID_SCAN_CLICK_SEC = 0.35  # 网格扫描每格点击后的稳定等待
# 2026-11-04 首跑：tab/item/source 三个 point_frac 是按 11-01 的 1091x613
# 缩略图推算的，真机上点了 (1353,61)/(769,207) 都没反应（OCR 始终是 32 个
# 文本区、找不到「贵重物」）。失败截图又是 ESC 回主界面之后拍的，量不到背包
# 网格。所以在写死坐标之后加一层网格扫描：逐格点选，直到右侧详情出现目标名。
GRID_SCAN_DEFAULT = {
    "x_start": 0.06, "x_end": 0.52,
    "y_start": 0.12, "y_end": 0.74,
    "cols": 8, "rows": 5,
}

REMAIN_KEYWORD = "本月还可合成"
LIMIT_TEXTS = ("已达上限",)
NO_MATERIAL_TEXTS = ("材料不足", "材料不够")
CRAFT_TEXTS = ("合成",)
CRAFT_BUTTON_EXCLUDE = ("材料合成", "合成机", "万能合成")  # 「合成」在这些词里不算按钮

# 底部按钮区域（窗口相对比例），把「合成/已达上限」限定在按钮条上，
# 避免把标题「材料合成」、顶部页签「合成」当成按钮。
CRAFT_REGION_FRAC = (0.25, 0.70, 0.76, 0.90)


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


def _press_key(hwnd: int, key: str) -> None:
    """在游戏窗口内按键。pyautogui 的 fail-safe 与点击同样处理，转成可读异常。"""
    from .foreground_flow_runner import _focus_window, FailSafeAborted, _FAILSAFE_HINT

    _focus_window(hwnd)
    try:
        pyautogui.press(key)
    except pyautogui.FailSafeException as exc:
        raise FailSafeAborted(_FAILSAFE_HINT) from exc


def _texts(logger: logging.Logger, hwnd: int) -> List[Dict]:
    """当前窗口的全窗 OCR 结果（单独抽出便于测试替换）。"""
    from .ocr_click_tester import ocr_window_content

    return ocr_window_content(logger, hwnd=hwnd) or []


def _local_frac(hwnd: int, screen_center: Optional[Sequence[float]]) -> Optional[Tuple[float, float]]:
    """OCR 的 center（屏幕绝对坐标）→ 窗口内相对比例 (0~1)。

    全窗 OCR 返回的 center 是屏幕坐标，而区域过滤和点击都按窗口算，
    必须先把窗口左上角减掉，否则窗口不在 (0,0) 时区域判断会整体偏掉。
    """
    from .foreground_flow_runner import _window_rect

    if not screen_center:
        return None
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    try:
        return (
            (float(screen_center[0]) - int(left)) / max(1, int(width)),
            (float(screen_center[1]) - int(top)) / max(1, int(height)),
        )
    except (TypeError, ValueError, IndexError):
        return None


def _to_local(hwnd: int, screen_center: Optional[Sequence[float]]) -> Optional[Tuple[int, int]]:
    """OCR 的 center（屏幕绝对坐标）→ 窗口局部像素坐标，供 _click_window_local 用。"""
    from .foreground_flow_runner import _window_rect

    if not screen_center:
        return None
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, _w, _h = rect
    try:
        return int(screen_center[0]) - int(left), int(screen_center[1]) - int(top)
    except (TypeError, ValueError, IndexError):
        return None


def _conf_of(item: Dict) -> float:
    try:
        return float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _find_text(
    items: Sequence[Dict],
    keywords: Sequence[str],
    *,
    hwnd: Optional[int] = None,
    region_frac: Optional[Sequence[float]] = None,
    min_conf: float = OCR_MIN_CONF,
    exclude: Sequence[str] = (),
) -> Optional[Dict]:
    """在 OCR 结果里找第一个命中关键字的条目；可限定窗口相对区域、排除干扰词。"""
    kws = [str(k) for k in keywords or () if str(k)]
    if not kws:
        return None
    for item in items or []:
        text = str(item.get("text") or "").replace(" ", "")
        if not text or _conf_of(item) < min_conf:
            continue
        if any(bad in text for bad in exclude):
            continue
        if not any(kw in text for kw in kws):
            continue
        if region_frac is not None:
            if hwnd is None:
                continue
            frac = _local_frac(hwnd, item.get("center"))
            if not frac:
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in region_frac]
            except (TypeError, ValueError):
                continue
            if not (x1 <= frac[0] <= x2 and y1 <= frac[1] <= y2):
                continue
        return item
    return None


def _read_remaining(logger: logging.Logger, hwnd: int) -> Tuple[Optional[int], str]:
    """读「本月还可合成：N」。返回 (N, 原始文本)；读不到返回 (None, 文本或空串)。

    OCR 可能把冒号和数字连成一行，也可能把数字切到同一行的另一块，
    两种都试：先在本行文本里取数字，取不到再找同一水平带的纯数字条目。
    """
    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败按「读不到」处理
        logger.debug(f"[{LABEL}] 读取「{REMAIN_KEYWORD}」OCR 失败: {exc}")
        return None, ""

    for item in items:
        text = str(item.get("text") or "").replace(" ", "")
        if REMAIN_KEYWORD not in text or _conf_of(item) < OCR_MIN_CONF:
            continue
        tail = text.split(REMAIN_KEYWORD, 1)[1]
        match = re.search(r"(\d+)", tail)
        if match:
            return int(match.group(1)), text

        center = item.get("center")
        if not center:
            return None, text
        for other in items:
            other_text = str(other.get("text") or "").strip()
            if not re.fullmatch(r"\d{1,4}", other_text):
                continue
            other_center = other.get("center")
            if not other_center:
                continue
            if (
                abs(float(other_center[1]) - float(center[1])) <= 26
                and float(other_center[0]) > float(center[0])
            ):
                return int(other_text), f"{text} {other_text}"
        return None, text
    return None, ""


def _button_state(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[str, Optional[Tuple[int, int]]]:
    """判定底部按钮状态。返回 (state, 窗口局部坐标)。

    state: ``limit``（已达上限）/ ``no_material``（材料不足）/ ``ready``（可合成）/
    ``unknown``（都没认出来，调用方按固定比例坐标兜底）。
    """
    # 1) 「已达上限」模板（2026-11-01 实机截图裁的红条按钮，最可靠）
    limit_template = _abs_template(str(step.get("limit_template") or ""))
    if limit_template and os.path.isfile(limit_template):
        center = _locate(hwnd, limit_template, scales, confidences, 2, stop_check)
        if center:
            return "limit", center

    # 2) 底部区域 OCR：按钮文案
    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败走 unknown 兜底
        logger.debug(f"[{LABEL}] 读取底部按钮 OCR 失败: {exc}")
        return "unknown", None

    region = step.get("craft_button_region_frac") or CRAFT_REGION_FRAC
    for state, words, exclude in (
        ("limit", LIMIT_TEXTS, ()),
        ("no_material", NO_MATERIAL_TEXTS, ()),
        ("ready", CRAFT_TEXTS, CRAFT_BUTTON_EXCLUDE),
    ):
        hit = _find_text(
            items, words,
            hwnd=hwnd,
            region_frac=region,
            exclude=exclude,
        )
        if hit:
            return state, _to_local(hwnd, hit.get("center"))
    return "unknown", None


def _bag_visible(logger: logging.Logger, hwnd: int, step: Dict, scales, confidences) -> bool:
    """背包是否已打开：优先模板，其次 OCR 左上标题区的「背包」。

    只认左上角标题区：ESC 菜单里也有「背包」按钮（屏幕中央），
    不限区域会把菜单误判成背包已打开（2026-11-04 页面判定排查）。
    """
    bag_template = _abs_template(str(step.get("bag_template") or ""))
    if bag_template and os.path.isfile(bag_template):
        if _locate(hwnd, bag_template, scales, confidences, 1, None):
            return True
    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败按未见处理
        logger.debug(f"[{LABEL}] 背包标题 OCR 失败: {exc}")
        return False
    return _find_text(items, ("背包",), hwnd=hwnd, region_frac=BAG_TITLE_REGION) is not None


def _capture_bag_evidence(logger: logging.Logger, hwnd: int, reason: str) -> None:
    """失败当刻（**还在背包/合成页**）先留一张带 OCR 标注的截图。

    `fail()` 会先 `_leave_to_main` 退回主界面再返回，上层统一的失败取证
    拍到的永远是主界面 —— 11-03、11-04 两次实跑的证据图里只有
    「观景车厢 / 信使 / Enter」，根本量不到背包网格和页签图标位置。
    这里只存图不写 flow_failures，避免和上层 dispatch 取证重复计数。
    """
    try:
        from .flow_diagnostics import capture_failure_image
        from .ocr_click_tester import ocr_window_content

        try:
            results = ocr_window_content(logger, hwnd=hwnd)
        except Exception as exc:  # noqa: BLE001 - OCR 失败也要保住原图
            logger.debug(f"[{LABEL}] 失败前画面 OCR 失败: {exc}")
            results = None
        raw, annotated = capture_failure_image(
            logger, hwnd=hwnd,
            tag="HSR_MONTHLY_自塑尘脂_失败前画面",
            ocr_results=results,
        )
        if raw:
            logger.warning(
                f"[{LABEL}] 失败前画面已存: {raw} / {annotated or '无标注图'}（原因: {reason}）"
            )
    except Exception as exc:  # noqa: BLE001 - 取证永远不能拖垮失败收尾
        logger.debug(f"[{LABEL}] 失败前画面取证异常（已忽略）: {exc}")


def _scan_resin_grid(
    logger: logging.Logger,
    hwnd: int,
    cfg: Dict,
    detail_visible: Callable[[], bool],
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str, Optional[Tuple[int, int]]]:
    """逐格点击背包网格，直到右侧详情出现目标物品；返回 (命中, 摘要, 命中点)。

    只做「选中」这一种安全动作（崩铁背包点格子不会使用/丢弃物品），
    每个点都靠 `detail_visible` 复核，绝不盲点确认按钮。
    """
    from .foreground_flow_runner import _click_window_local

    def _num(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    x1, x2 = _num("x_start", GRID_SCAN_DEFAULT["x_start"]), _num("x_end", GRID_SCAN_DEFAULT["x_end"])
    y1, y2 = _num("y_start", GRID_SCAN_DEFAULT["y_start"]), _num("y_end", GRID_SCAN_DEFAULT["y_end"])
    try:
        cols = min(12, max(1, int(cfg.get("cols", GRID_SCAN_DEFAULT["cols"]))))
        rows = min(10, max(1, int(cfg.get("rows", GRID_SCAN_DEFAULT["rows"]))))
    except (TypeError, ValueError):
        cols, rows = GRID_SCAN_DEFAULT["cols"], GRID_SCAN_DEFAULT["rows"]
    if x2 <= x1 or y2 <= y1:
        return False, "网格范围配置无效", None

    tried = 0
    for row in range(rows):
        for col in range(cols):
            if stop_check is not None and stop_check():
                return False, "用户停止", None
            fx = x1 + (x2 - x1) * (col + 0.5) / cols
            fy = y1 + (y2 - y1) * (row + 0.5) / rows
            point = _frac_point(hwnd, (fx, fy))
            if not point:
                continue
            _click_window_local(hwnd, point[0], point[1], clicks=1)
            _sleep(GRID_SCAN_CLICK_SEC, stop_check)
            tried += 1
            if detail_visible():
                logger.info(
                    f"[{LABEL}] 网格扫描第 {tried} 格命中 @ 比例({fx:.3f},{fy:.3f}) → {point}"
                )
                return True, f"网格扫描第 {tried} 格命中", point
    return False, f"网格扫描 {tried} 格仍未看到目标详情", None


def _open_bag(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """主界面按 B 打开背包；已经开着就直接过（避免 B 反而把背包关掉）。"""
    if _bag_visible(logger, hwnd, step, scales, confidences):
        logger.info(f"[{LABEL}] 背包已打开，跳过按 B")
        return True, "背包已打开"

    key = str(step.get("bag_open_key") or "b").strip() or "b"
    for attempt in range(1, BAG_OPEN_ATTEMPTS + 1):
        if stop_check is not None and stop_check():
            return False, "用户停止"
        _press_key(hwnd, key)
        logger.info(f"[{LABEL}] 已按 {key.upper()} 打开背包（第 {attempt} 次）")
        end = time.time() + BAG_WAIT_SEC
        while time.time() < end:
            if stop_check is not None and stop_check():
                return False, "用户停止"
            if _bag_visible(logger, hwnd, step, scales, confidences):
                _sleep(CLICK_SETTLE_SEC, stop_check)
                return True, "背包已打开"
            _sleep(0.5, stop_check)
        # 界面可能停在战斗/别的菜单，B 无效：ESC 清一层再试
        logger.warning(f"[{LABEL}] 按 {key.upper()} 后没看到背包标题，按 ESC 清一层后重试")
        _press_key(hwnd, "escape")
        _sleep(CLICK_SETTLE_SEC, stop_check)
    return False, "未能打开背包（按 B 后没出现背包标题）"


def _select_precious_tab(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """切到顶部「贵重物」分类（倒数第二个图标）。

    定位优先级：模板 → OCR「贵重物」→ 固定比例坐标；切完以左上标题「贵重物」
    为准做确认。
    """
    from .foreground_flow_runner import _click_window_local

    tab_text = str(step.get("tab_text") or "贵重物").strip() or "贵重物"
    tab_template = _abs_template(str(step.get("tab_template") or ""))
    point_frac = step.get("tab_point_frac")

    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001 - OCR 失败不影响后续定位
        logger.debug(f"[{LABEL}] 分类标题 OCR 失败: {exc}")
        items = []
    if _find_text(items, (tab_text,)) and _find_text(items, ("稀有度", "筛选")):
        logger.info(f"[{LABEL}] 已在「{tab_text}」分类")
        return True, f"已在「{tab_text}」"

    center = None
    if tab_template and os.path.isfile(tab_template):
        center = _locate(hwnd, tab_template, scales, confidences, TAB_WAIT_SEC, stop_check)
        if center:
            logger.info(f"[{LABEL}] 模板命中「{tab_text}」页签 @ {center}")
    if center is None:
        center = _locate_ocr(logger, hwnd, tab_text, TAB_WAIT_SEC, stop_check)
        if center:
            logger.info(f"[{LABEL}] OCR 命中「{tab_text}」页签 @ {center}")
    if center is None and isinstance(point_frac, (list, tuple)) and len(point_frac) == 2:
        center = _frac_point(hwnd, point_frac)
        if center:
            logger.info(f"[{LABEL}] 「{tab_text}」改用固定比例坐标 @ {center}")

    if center is None:
        return False, f"未找到「{tab_text}」分类图标"

    _click_window_local(hwnd, center[0], center[1], clicks=1)
    _sleep(CLICK_SETTLE_SEC, stop_check)
    if stop_check is not None and stop_check():
        return False, "用户停止"

    try:
        items = _texts(logger, hwnd)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{LABEL}] 切换后校验 OCR 失败: {exc}")
        items = []
    if _find_text(items, (tab_text,)):
        return True, f"已切到「{tab_text}」"
    logger.warning(
        f"[{LABEL}] 点完分类图标 @ {center} 后没看到「{tab_text}」标题，"
        f"继续按物料定位（比例坐标可能已失效，需按当前截图重新校准）"
    )
    return True, f"已点击「{tab_text}」（未确认标题）"


def _select_resin(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """点第一行第 5 格的「自塑尘脂」，并确认右侧详情已切换到它。

    详情校验用「自塑尘脂 + 获得途径/消耗品」两个条件：只看到名字可能是别的
    界面（搜索框、聊天），配上「获得途径」才是详情面板。
    """
    from .foreground_flow_runner import _click_window_local

    item_text = str(step.get("item_text") or LABEL).strip() or LABEL
    item_template = _abs_template(str(step.get("item_template") or ""))
    point_frac = step.get("item_point_frac")

    def detail_visible() -> bool:
        try:
            items = _texts(logger, hwnd)
        except Exception as exc:  # noqa: BLE001 - OCR 失败按未见处理
            logger.debug(f"[{LABEL}] 详情区 OCR 失败: {exc}")
            return False
        return _find_text(items, (item_text,)) is not None and _find_text(
            items, ("获得途径", "消耗品")
        ) is not None

    if detail_visible():
        logger.info(f"[{LABEL}] 详情区已显示「{item_text}」")
        return True, "已在详情页"

    center = None
    if item_template and os.path.isfile(item_template):
        center = _locate(hwnd, item_template, scales, confidences, DETAIL_WAIT_SEC, stop_check)
        if center:
            logger.info(f"[{LABEL}] 模板命中「{item_text}」格子 @ {center}")
    if center is None and isinstance(point_frac, (list, tuple)) and len(point_frac) == 2:
        center = _frac_point(hwnd, point_frac)
        if center:
            logger.info(f"[{LABEL}] 「{item_text}」改用固定比例坐标 @ {center}")
    if center is None:
        return False, f"未找到「{item_text}」格子（模板/比例坐标均不可用）"

    _click_window_local(hwnd, center[0], center[1], clicks=1)
    _sleep(CLICK_SETTLE_SEC, stop_check)
    if stop_check is not None and stop_check():
        return False, "用户停止"

    end = time.time() + DETAIL_WAIT_SEC
    while time.time() < end:
        if detail_visible():
            return True, f"已选中「{item_text}」"
        _sleep(0.5, stop_check)
    # 2026-11-03 / 11-04 两次首跑都卡在这里：比例坐标 (769,207) 是按 11-01
    # 缩略图推算的，真机没点中；而失败截图又是退回主界面后才拍的，量不到网格。
    # 不再赌一个坐标，改在背包网格里逐格点选 + 每格复核详情，命中就把实际
    # 比例写进日志，下次可直接把校准后的值填回 item_point_frac。
    logger.warning(
        f"[{LABEL}] 点击「{item_text}」格子 @ {center} 后 {DETAIL_WAIT_SEC}s 内"
        f"详情区仍未出现，改用网格扫描定位"
    )
    grid = step.get("item_grid_frac")
    if not isinstance(grid, dict):
        grid = GRID_SCAN_DEFAULT
    ok, scan_msg, _hit = _scan_resin_grid(
        logger, hwnd, grid, detail_visible, stop_check
    )
    if ok:
        return True, f"已选中「{item_text}」（{scan_msg}）"
    logger.warning(f"[{LABEL}] 固定比例坐标与网格扫描都没选中「{item_text}」：{scan_msg}")
    return False, f"点开后详情区没显示「{item_text}」（{scan_msg}）"


def _open_synthesizer(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """点「获得途径」里的「万能合成机」条目进合成界面。

    只有在看到「本月还可合成 / 所需材料」之后才算进入；看不到就失败返回，
    绝不在背包页上盲点底部合成按钮。
    """
    from .foreground_flow_runner import _click_window_local

    source_text = str(step.get("source_text") or "万能合成机").strip() or "万能合成机"
    source_template = _abs_template(str(step.get("source_template") or ""))
    point_frac = step.get("source_point_frac")

    def synth_visible() -> bool:
        try:
            items = _texts(logger, hwnd)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{LABEL}] 合成界面 OCR 失败: {exc}")
            return False
        return _find_text(items, (REMAIN_KEYWORD, "所需材料")) is not None

    if synth_visible():
        logger.info(f"[{LABEL}] 已在合成界面")
        return True, "已在合成界面"

    center = None
    if source_template and os.path.isfile(source_template):
        center = _locate(hwnd, source_template, scales, confidences, SOURCE_WAIT_SEC, stop_check)
        if center:
            logger.info(f"[{LABEL}] 模板命中「{source_text}」入口 @ {center}")
    if center is None:
        center = _locate_ocr(logger, hwnd, source_text, SOURCE_WAIT_SEC, stop_check)
        if center:
            logger.info(f"[{LABEL}] OCR 命中「{source_text}」入口 @ {center}")
    if center is None and isinstance(point_frac, (list, tuple)) and len(point_frac) == 2:
        center = _frac_point(hwnd, point_frac)
        if center:
            logger.info(f"[{LABEL}] 「{source_text}」改用固定比例坐标 @ {center}")
    if center is None:
        return False, f"未找到「{source_text}」入口（获得途径里那条）"

    _click_window_local(hwnd, center[0], center[1], clicks=1)
    _sleep(CLICK_SETTLE_SEC + 0.5, stop_check)

    end = time.time() + SYNTH_WAIT_SEC
    while time.time() < end:
        if stop_check is not None and stop_check():
            return False, "用户停止"
        if synth_visible():
            return True, "已进入合成界面"
        _sleep(0.5, stop_check)
    return False, "点「万能合成机」后没进入合成界面（没看到「本月还可合成/所需材料」）"


def _craft_loop(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """循环点合成按钮，直到本月上限 / 材料不足 / 读数不再变化。"""
    from .foreground_flow_runner import _click_window_local

    try:
        max_rounds = max(1, int(step.get("max_rounds", MAX_ROUNDS_DEFAULT) or MAX_ROUNDS_DEFAULT))
    except (TypeError, ValueError):
        max_rounds = MAX_ROUNDS_DEFAULT
    fallback_frac = step.get("craft_button_point_frac")

    before, raw = _read_remaining(logger, hwnd)
    if before == 0:
        logger.info(f"[{LABEL}] 本月还可合成 0（{raw or '已达上限'}），本月无需合成")
        return True, "本月已合成完（还可合成 0）"
    if before is None:
        logger.warning(f"[{LABEL}] 读不到「{REMAIN_KEYWORD}」数字，改由按钮状态驱动合成")

    last_known = before
    done = 0
    stuck = 0

    for round_no in range(1, max_rounds + 1):
        if stop_check is not None and stop_check():
            return False, f"用户停止（已合成 {done} 次）" if done else "用户停止"

        state, center = _button_state(logger, hwnd, step, scales, confidences, stop_check)
        if state == "limit":
            logger.info(f"[{LABEL}] 底部按钮已是「已达上限」，本月合成结束（本次 {done} 次）")
            return True, f"已达本月上限（本次合成 {done} 次）"
        if state == "no_material":
            logger.info(f"[{LABEL}] 底部按钮显示「材料不足」，合成结束（本次 {done} 次）")
            return True, f"遗器残骸不足（本次合成 {done} 次）"

        if center is None and isinstance(fallback_frac, (list, tuple)) and len(fallback_frac) == 2:
            center = _frac_point(hwnd, fallback_frac)
        if center is None:
            return False, "定位不到底部合成按钮（模板/OCR/比例坐标均失败）"

        _click_window_local(hwnd, center[0], center[1], clicks=1)
        _sleep(CRAFT_CLICK_WAIT_SEC, stop_check)
        if stop_check is not None and stop_check():
            return False, f"用户停止（已合成 {done} 次）" if done else "用户停止"

        after, _raw_after = _read_remaining(logger, hwnd)
        if after is None:
            # 读不到计数：可能被弹窗遮住，按钮状态就是唯一依据，交给下一轮
            done += 1
            logger.info(f"[{LABEL}] 点了一次合成（读不到计数，第 {round_no} 轮）")
            continue

        if last_known is None:
            # 点击前没读到、点击后读到了：以新读数为基准继续
            done += 1
            last_known = after
            logger.info(f"[{LABEL}] 已合成 1 次，本月还可合成 {after}")
            if after == 0:
                return True, f"已达本月上限（本次合成 {done} 次）"
            continue

        if after < last_known:
            done += 1
            stuck = 0
            logger.info(f"[{LABEL}] 已合成 1 次（本月还可合成 {last_known} → {after}）")
            last_known = after
            if after == 0:
                logger.info(f"[{LABEL}] 本月还可合成已归零，合成结束（本次 {done} 次）")
                return True, f"已达本月上限（本次合成 {done} 次）"
            continue

        stuck += 1
        logger.warning(
            f"[{LABEL}] 点击后「{REMAIN_KEYWORD}」仍是 {after}（第 {stuck} 次未变化）"
        )
        if stuck >= STUCK_LIMIT:
            return False, f"点了合成但计数一直停在 {after}，可能弹了确认框或按钮不可用"

    logger.warning(f"[{LABEL}] 达到循环上限 {max_rounds} 轮仍未结束，停止合成")
    return True, f"达到循环上限 {max_rounds} 轮（本次合成 {done} 次）"


def _leave_to_main(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]],
) -> bool:
    """合成完逐层 ESC 回主世界，以主世界特征模板为到达标志（先查再按）。"""
    marker = _abs_template(str(step.get("exit_marker_template") or ""))
    if not marker or not os.path.isfile(marker):
        logger.warning(f"[{LABEL}] 未配置 exit_marker_template，按 ESC 两次兜底返回主界面")
        for _ in range(2):
            if stop_check is not None and stop_check():
                return False
            _press_key(hwnd, "escape")
            _sleep(1.2, stop_check)
        return True

    for _ in range(EXIT_MAX_ESC):
        if stop_check is not None and stop_check():
            return False
        if _locate(hwnd, marker, scales, confidences, EXIT_CHECK_SEC, stop_check):
            return True
        _press_key(hwnd, "escape")
        _sleep(1.2, stop_check)

    if stop_check is not None and stop_check():
        return False
    if _locate(hwnd, marker, scales, confidences, EXIT_CHECK_SEC, stop_check):
        return True
    logger.warning(f"[{LABEL}] ESC 后仍未确认回到主界面（下一步会再校验主世界特征）")
    return False


def craft_self_modeling_resin(
    logger: logging.Logger,
    hwnd: int,
    step: Dict,
    *,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """执行「背包 → 贵重物 → 自塑尘脂 → 万能合成机 → 合成到本月上限」。

    step 字段（都有默认值，配置文件可覆盖）：
    - bag_open_key: 打开背包的按键，默认 ``b``
    - tab_text / tab_template / tab_point_frac: 顶部「贵重物」分类
    - item_text / item_template / item_point_frac: 包内「自塑尘脂」格子
    - source_text / source_template / source_point_frac: 获得途径里的「万能合成机」条
    - limit_template: 底部「已达上限」按钮模板（最强判据，缺省走 OCR）
    - craft_button_region_frac: 按钮 OCR 的限定区域，默认窗口下半部中间
    - craft_button_point_frac: 按钮定位全失败时的兜底坐标
    - max_rounds: 合成循环上限，默认 30
    - exit_marker_template: 主世界特征（崩铁主界面图标）

    返回 (是否成功, 摘要)。「本月已合成完」「材料不足」都算成功 —— 这是每月
    流程里正常结束的两种形态；只有界面进不去 / 按钮点不动才算失败。

    失败也要尽量退回主界面：中途停在背包/合成页会污染下一次运行
    （`starrail_enter_main` 靠主界面特征判断，停在背包页会判不出来）。
    """
    label = str(step.get("log_label") or LABEL).strip() or LABEL

    def fail(reason: str) -> Tuple[bool, str]:
        # 2026-11-03 实锤：以前这里只 return，日志里从"点格子"到"下一次流程启动"
        # 中间一片空白，flow_failures 也没记录。失败原因必须先落日志。
        logger.warning(f"[{label}] 失败: {reason}")
        # 2026-11-04：取证必须赶在 ESC 退回主界面之前，否则证据图里只有
        # 「观景车厢」，永远拿不到背包网格/页签坐标来校准 point_frac。
        _capture_bag_evidence(logger, hwnd, reason)
        exit_ok = _leave_to_main(logger, hwnd, step, scales, confidences, stop_check)
        return False, reason if exit_ok else f"{reason}（未确认返回主界面）"

    ok, msg = _open_bag(logger, hwnd, step, scales, confidences, stop_check)
    if not ok:
        return fail(msg)
    logger.info(f"[{label}] {msg}")

    ok, msg = _select_precious_tab(logger, hwnd, step, scales, confidences, stop_check)
    if not ok:
        return fail(msg)

    ok, msg = _select_resin(logger, hwnd, step, scales, confidences, stop_check)
    if not ok:
        return fail(msg)

    ok, msg = _open_synthesizer(logger, hwnd, step, scales, confidences, stop_check)
    if not ok:
        return fail(msg)

    craft_ok, craft_msg = _craft_loop(logger, hwnd, step, scales, confidences, stop_check)
    if stop_check is not None and stop_check():
        return False, f"用户停止 | {craft_msg}"

    exit_ok = _leave_to_main(logger, hwnd, step, scales, confidences, stop_check)
    detail = craft_msg if exit_ok else f"{craft_msg}（未确认返回主界面）"
    return craft_ok, detail
