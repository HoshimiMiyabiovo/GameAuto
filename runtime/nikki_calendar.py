"""无限暖暖 · 奇想日历领奖 + 晶石兑换/共鸣商店（2026-11-01）。

由 NikkiLauncher 流程的 action=nikki_calendar 驱动，固定在 nikki_enter_game 之后：
主界面 → 按 L 打开奇想日历 → 奇迹之冠 → 奇迹之冠搭配赛 → 领取奖励（如有）
→「空白区域继续」收尾 → 晶石兑换（每月一次）→ 回主界面 → 按 H 打开商城 →
左列滚动找「共鸣商店」→ 静谧水滴兑换（每月一次）→ ESC 回主界面。

几何定标（2026-11-01 用户截图实跑 RapidOCR，参考图 data/artifacts/ref_nikki_*_20261101.png，
1919x1079）：
  - 奇想日历标题 (0.094,0.047)；日历页「奇迹之冠」区块 (0.506,0.556)，
    页面特征：每日灵感 / 幻境挑战 / 奇迹之冠 / 星光的追寻（命中 ≥2 个）
  - 奇迹之冠 hub：「奇迹之冠搭配赛」(0.277,0.618)、「奇迹之冠争锋赛」(0.789,0.363)
  - 搭配赛页：「领取奖励」(0.501,0.697)、左列「晶石兑换」(0.078,0.718)、「赛事嘉奖」(0.078,0.830)
  - 奖励页：「恭喜获得」(0.501,0.259)、「F/空白区域继续」(0.902,0.962) → 点下方空白 (0.50,0.82)
  - 晶石兑换：共鸣水晶 (0.221,0.353) / 启示水晶 (0.364,0.353)，价格 120；
    「本月限购 N/M」在商品名上方、中心右移约 0.026 窗宽（N 是剩余次数）。
    价格 OCR 会被图标污染（120 常识别成 4120），所以**购买只用「本月限购」N 递减作到账证据**，
    不解析价格。
  - 兑换确认弹窗（用户 2026-11-01 截图）：标题「兑换」(0.501,0.209)、数量默认 1、
    底部按钮「确认」(0.500,0.751)、「本月限购 3/3」(0.622,0.538)、「合计」(0.580,0.651)。
    **标题也叫「兑换」且 conf 常高于按钮（0.66 vs 0.64）**：确认词表必须去掉「兑换」，
    按「确认」找按钮，否则会点标题、弹窗不动却被当成已确认。
  - 商城 → 共鸣商店（用户 2026-11-01 截图）：主界面 H 进商城（商城 (0.077,0.047) /
    购物车 (0.168,0.046)）；左列「美鸭梨优选」初始 (0.115,0.830)，滚轮向下后
    (0.115,0.287)，「共鸣商店」出现在 (0.105,0.787)。共鸣商店页两页签
    澎湃浪花兑换 (0.313,0.119) / 静谧水滴兑换 (0.500,0.119)。
    **必须切到静谧水滴兑换**：用专属商品 万象星实/虹彩棱镜/流辉棱镜/璨光棱镜 确认页签
    （澎湃页没有），没确认到就**不买**，防止买错货币；静谧页 启示水晶 (0.292,0.383) /
    共鸣水晶 (0.436,0.384)，本月限购 5/5 在名上方 (0.320,0.204)/(0.465,0.204)，单价 40。

设计约束：
- 领奖/兑换是「尽力而为」：失败只记 ERROR + flow_failures，**不阻断**后续 whimbox 一条龙
  （prelaunch 步骤失败会让整个暖暖任务不跑，代价远大于漏领一次）。流程内部异常也吞成
  警告，但用户在日志/失败记录里能查到。
- 晶石兑换 / 共鸣商店都按月门控（WorkflowDB meta `monthly_sequence:nikki_crystal_exchange`
  / `monthly_sequence:nikki_resonance_shop`），逐件购买（点商品 → 确认 → 空白区域继续），
  库存不足/没点到就如实记 shortfall/failed。
- **交接规则（2026-11-01 用户叮嘱）**：后续接 whimbox 前必须保证游戏在主界面；确认不了
  就调用 `_close_game_for_whimbox_restart` 关闭 `X6Game-Win64-Shipping.exe`，让 whimbox
  自己重新启动进入。**不要**「日历没跑成但把游戏留在菜单里」直接交出去。
- 「空白区域继续」是暖暖奖励界面的通用收尾：识别到就点屏幕下方空白处，
  **不要**按 ESC 或乱点按钮。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..settings.paths import WORKFLOW_DB_PATH

# ── 页面文字特征 ────────────────────────────────────────────────────
CALENDAR_TITLE = "奇想日历"
CALENDAR_MARKERS = ("每日灵感", "幻境挑战", "奇迹之冠", "星光的追寻")
CROWN_ENTRY = "奇迹之冠"
CROWN_HUB_MARKERS = ("奇迹之冠搭配赛", "奇迹之冠争锋赛")
PAIR_ENTRY = "奇迹之冠搭配赛"
STAGE_MARKERS = ("赛事嘉奖", "晶石兑换", "第4场", "第5场", "第6场")
STAGE_CLAIM_MODAL = "巅峰赛速报"
CLAIM_TEXTS = ("领取奖励",)
CONTINUE_TEXTS = ("空白区域继续", "空白区域", "点击屏幕继续")
EXCHANGE_TITLE = "晶石兑换"
LIMIT_KEYWORD = "本月限购"
CONFIRM_TEXTS = ("确认兑换", "确认", "确定", "购买")
CANCEL_TEXTS = ("取消",)

# ── 区域（窗口比例）────────────────────────────────────────────────
CALENDAR_CROWN_REGION = (0.40, 0.45, 0.75, 0.63)
HUB_PAIR_REGION = (0.12, 0.52, 0.48, 0.72)
STAGE_MENU_REGION = (0.00, 0.55, 0.22, 0.92)
DIALOG_REGION = (0.20, 0.15, 0.80, 0.88)
EXCHANGE_GRID_REGION = (0.15, 0.10, 0.90, 0.78)
BLANK_CONTINUE_POINT = (0.50, 0.82)

# 商品名 → 本月限购的相对位置（实测：名中心上方 0.18 窗高、右移 0.026 窗宽）
LIMIT_DX = 0.026
LIMIT_BAND_DX = 0.10
LIMIT_DY_MIN = -0.26
LIMIT_DY_MAX = -0.10

# ── 商城 → 共鸣商店（2026-11-01 用户截图定标，1919x1079）────────────
# 商城左列：初始「美鸭梨优选」(0.115,0.830)，滚动后 (0.115,0.287)；
# 「共鸣商店」滚动后 (0.105,0.787)。页面标志用 商城+购物车（主界面只有商城，没有购物车）。
SHOP_MARKERS = ("商城", "购物车")
SHOP_MENU_ITEM = "美鸭梨优选"
RESONANCE_ENTRY = "共鸣商店"
RESONANCE_TABS = ("澎湃浪花兑换", "静谧水滴兑换")
RESONANCE_TAB_TARGET = "静谧水滴兑换"
# 静谧水滴页专属商品（澎湃页没有），用来强校验页签，避免买错货币
SERENE_MARKERS = ("万象星实", "璨光棱镜", "流辉棱镜", "虹彩棱镜")
SHOP_MENU_REGION = (0.00, 0.15, 0.22, 0.95)
RESONANCE_TAB_REGION = (0.22, 0.07, 0.78, 0.18)
SHOP_GRID_REGION = (0.15, 0.14, 0.92, 0.95)
SHOP_MENU_SCROLL_ANCHOR = (0.11, 0.55)

NAME_MIN_CONF = 0.45
FUZZY_MIN_RATIO = 0.85
FUZZY_MARGIN = 0.05

MONTHLY_META_KEY = "monthly_sequence:nikki_crystal_exchange"
RESONANCE_META_KEY = "monthly_sequence:nikki_resonance_shop"
_LIMIT_RE = re.compile(r"本月限购\s*(\d+)\s*/\s*(\d+)")


def _stop(stop_check: Optional[Callable[[], bool]]) -> bool:
    return bool(stop_check and stop_check())


def _norm(text: str) -> str:
    from .ocr_click_tester import _normalize_text

    return _normalize_text(str(text or ""))


def _ocr_full(logger: logging.Logger, hwnd: int, region=None) -> List[Dict]:
    try:
        from .ocr_click_tester import ocr_window_content

        return ocr_window_content(logger, hwnd=hwnd, region=region) or []
    except Exception:  # noqa: BLE001 - OCR 失败按空帧处理，由等待逻辑重试
        logger.warning("[无限暖暖·奇想日历] OCR 失败", exc_info=True)
        return []


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl

    return impl(hwnd)


def _focus(hwnd: int) -> None:
    from .foreground_flow_runner import _focus_window

    _focus_window(hwnd)


def _click(center) -> None:
    from .foreground_flow_runner import _click_screen

    _click_screen(int(center[0]), int(center[1]))


def _click_ratio(logger: logging.Logger, hwnd: int, frac: Tuple[float, float], label: str) -> bool:
    rect = _window_rect(hwnd)
    if not rect:
        logger.warning("[无限暖暖·奇想日历] 窗口区域不可用，无法点击%s", label)
        return False
    left, top, width, height = rect
    point = (left + int(width * frac[0]), top + int(height * frac[1]))
    _click(point)
    logger.info("[无限暖暖·奇想日历] 点击%s @ %s", label, point)
    return True


def _press_key(key: str) -> None:
    import pyautogui

    from .foreground_flow_runner import FailSafeAborted

    try:
        pyautogui.press(key)
    except pyautogui.FailSafeException as exc:
        raise FailSafeAborted(
            "PyAutoGUI 紧急停止已触发：鼠标位于屏幕左上角 (0,0)。"
        ) from exc


def _press_esc() -> None:
    from .nikki_launcher import _press_esc as impl

    impl()


def _has_main(hwnd: int, scales, confidences) -> bool:
    from .nikki_launcher import _has_main as impl

    try:
        return bool(impl(hwnd, scales, confidences))
    except Exception:  # noqa: BLE001 - 模板缺失/窗口失效按非主界面处理
        return False


def _abs_region(rect, frac) -> Tuple[int, int, int, int]:
    left, top, width, height = rect
    fx0, fy0, fx1, fy1 = frac
    return (
        left + int(width * fx0),
        top + int(height * fy0),
        int(width * (fx1 - fx0)),
        int(height * (fy1 - fy0)),
    )


def _in_abs(center, region) -> bool:
    if not center or not region:
        return False
    x, y = int(center[0]), int(center[1])
    left, top, width, height = region
    return left <= x <= left + width and top <= y <= top + height


def _pick(
    items: List[Dict],
    texts,
    min_conf: float = 0.5,
    region=None,
    exclude=(),
) -> Optional[Dict]:
    """在 OCR 结果里挑目标文字：精确优先，其次子串；排除黑名单（如「取消」）。"""
    exact = None
    partial = None
    for item in items or []:
        text = _norm(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        if any(_norm(x) in text for x in exclude if x):
            continue
        center = item.get("center")
        if region and not _in_abs(center, region):
            continue
        for cand in texts:
            cnorm = _norm(cand)
            if not cnorm:
                continue
            if text == cnorm:
                if exact is None or conf > exact[0]:
                    exact = (conf, item)
                break
            if cnorm in text:
                if partial is None or conf > partial[0]:
                    partial = (conf, item)
    best = exact or partial
    return best[1] if best else None


def _pick_confirm(items: List[Dict], region) -> Optional[Dict]:
    """兑换弹窗按钮：按词序找（确认兑换 > 确认 > 确定 > 购买）。

    弹窗标题也叫「兑换」且置信度常高于按钮（实测 0.66 vs 0.64），词表里不能含
    「兑换」，否则会点标题、弹窗不动却被当成已确认。
    """
    for cand in CONFIRM_TEXTS:
        hit = _pick(items, (cand,), min_conf=0.5, region=region, exclude=CANCEL_TEXTS)
        if hit:
            return hit
    return None


def _markers_present(items: List[Dict], markers, min_conf: float = 0.45) -> set:
    found = set()
    for item in items or []:
        text = _norm(item.get("text") or "")
        if not text:
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if conf < min_conf:
            continue
        for marker in markers:
            if _norm(marker) in text:
                found.add(marker)
    return found


def _is_calendar_page(items: List[Dict]) -> bool:
    return len(_markers_present(items, CALENDAR_MARKERS)) >= 2


def _is_stage_page(items: List[Dict]) -> bool:
    if _pick(items, CLAIM_TEXTS, 0.5):
        return True
    if _pick(items, (STAGE_CLAIM_MODAL,), 0.5):
        return True
    return bool(_markers_present(items, STAGE_MARKERS, 0.55))


def _is_exchange_page(items: List[Dict]) -> bool:
    return bool(_pick(items, (LIMIT_KEYWORD,), 0.5))


def _wait_for(
    logger: logging.Logger,
    hwnd: int,
    predicate: Callable[[List[Dict]], bool],
    timeout: float,
    stop_check=None,
    interval: float = 1.0,
) -> Optional[List[Dict]]:
    end = time.time() + max(0.5, float(timeout))
    while time.time() < end:
        if _stop(stop_check):
            return None
        items = _ocr_full(logger, hwnd)
        if predicate(items):
            return items
        time.sleep(interval)
    return None


def _dismiss_continue_screens(
    logger: logging.Logger,
    hwnd: int,
    stop_check=None,
    max_rounds: int = 4,
) -> int:
    """暖暖奖励界面通用收尾：识别「空白区域继续」→ 点屏幕下方空白处。

    多次出现（连续领奖）时循环处理；没有该提示就立即返回 0。
    """
    dismissed = 0
    for _ in range(max(1, int(max_rounds))):
        if _stop(stop_check):
            break
        items = _ocr_full(logger, hwnd)
        hit = _pick(items, CONTINUE_TEXTS, min_conf=0.5)
        if not hit:
            break
        if not _click_ratio(logger, hwnd, BLANK_CONTINUE_POINT, "「空白区域继续」空白处"):
            break
        dismissed += 1
        time.sleep(1.0)
    return dismissed


def _record_failure(
    logger: logging.Logger,
    step: str,
    message: str,
    hwnd,
    ocr_results: Optional[List[Dict]] = None,
) -> None:
    try:
        from .flow_diagnostics import record_failure

        record_failure(
            logger,
            flow_key="NikkiLauncher",
            step=step,
            error=message,
            hwnd=hwnd,
            ocr_results=ocr_results,
            extra={"kind": "nikki_calendar"},
        )
    except Exception as exc:  # noqa: BLE001 - 诊断自身失败不能影响主流程
        logger.debug("[无限暖暖·奇想日历] 记录失败诊断异常 %s: %s", step, exc)


def _load_nikki_shop_plan(logger: logging.Logger, section_name: str, label: str) -> Dict:
    """读 app_config.json → nikki.<section_name> 的通用清单解析。

    返回 {"enabled": bool, "items": [{"name","qty"}]}；qty 缺省/非法/<1 按 1，
    上限 99（防配置手滑把循环开太大）。读不到配置返回空清单（跳过兑换，不乱买东西）。
    """
    plan: Dict = {"enabled": True, "items": []}
    try:
        from ..settings import manager as cfg_manager

        cfg = cfg_manager.load(force=True) or {}
        section = (cfg.get("nikki") or {}).get(section_name) or {}
    except Exception as e:  # noqa: BLE001 - 配置读失败只降级，不炸一条龙
        logger.warning("[无限暖暖·%s] 读取配置失败（按空清单处理）: %s", label, e)
        return plan

    plan["enabled"] = bool(section.get("enabled", True))
    raw_items = section.get("items") or []
    if not isinstance(raw_items, list):
        logger.warning("[无限暖暖·%s] 配置 items 不是列表，按空清单处理", label)
        raw_items = []
    for raw in raw_items:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        try:
            qty = int(raw.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(99, qty))
        plan["items"].append({"name": name, "qty": qty})
    logger.info(
        "[无限暖暖·%s] 清单载入 %s 项%s",
        label,
        len(plan["items"]),
        "：" + "、".join(
            f"{i['name']}×{i['qty']}" if i["qty"] > 1 else i["name"] for i in plan["items"]
        )
        if plan["items"]
        else "（空清单，跳过兑换）",
    )
    return plan


def load_crystal_exchange_plan(logger: logging.Logger) -> Dict:
    """晶石兑换清单（app_config.json → nikki.crystal_exchange）。"""
    return _load_nikki_shop_plan(logger, "crystal_exchange", "晶石兑换")


def load_resonance_shop_plan(logger: logging.Logger) -> Dict:
    """共鸣商店清单（app_config.json → nikki.resonance_shop）。"""
    return _load_nikki_shop_plan(logger, "resonance_shop", "共鸣商店")


def _month_key() -> str:
    return time.strftime("%Y-%m")


def _monthly_done(logger: logging.Logger, meta_key: str = MONTHLY_META_KEY) -> bool:
    try:
        from ..storage.workflow_db import WorkflowDB

        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        return db.get_task_meta(meta_key) == _month_key()
    except Exception as exc:  # noqa: BLE001 - 读不到就当没做过
        logger.debug("[无限暖暖·月度标记] 读取失败 %s: %s", meta_key, exc)
        return False


def _mark_monthly_done(logger: logging.Logger, meta_key: str = MONTHLY_META_KEY) -> None:
    try:
        from ..storage.workflow_db import WorkflowDB

        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        db.set_task_meta(meta_key, _month_key())
        logger.info("[无限暖暖·月度标记] 已写入 %s=%s", meta_key, _month_key())
    except Exception as exc:  # noqa: BLE001 - 写标记失败下次会重试，不阻塞
        logger.warning("[无限暖暖·月度标记] 写入失败 %s: %s", meta_key, exc)


def _find_item_name(
    items: List[Dict],
    target: str,
    rect,
    grid_frac=EXCHANGE_GRID_REGION,
) -> Optional[Dict]:
    """在商店网格里按名称找商品卡（精确优先，其次唯一模糊匹配）。"""
    tnorm = _norm(target)
    if not tnorm:
        return None
    grid = _abs_region(rect, grid_frac)
    exact: List[Dict] = []
    fuzzy: List[Tuple[float, Dict]] = []
    for item in items or []:
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        center = item.get("center")
        if conf < NAME_MIN_CONF or not _in_abs(center, grid):
            continue
        text = _norm(item.get("text") or "")
        if not text:
            continue
        if text == tnorm:
            exact.append(item)
            continue
        ratio = difflib.SequenceMatcher(None, tnorm, text).ratio()
        if ratio >= FUZZY_MIN_RATIO:
            fuzzy.append((ratio, item))
    if exact:
        return max(exact, key=lambda r: float(r.get("confidence") or 0.0))
    if not fuzzy:
        return None
    fuzzy.sort(key=lambda x: x[0], reverse=True)
    if len(fuzzy) > 1 and (fuzzy[0][0] - fuzzy[1][0]) < FUZZY_MARGIN:
        return None  # 两个候选都像 → 宁可不买
    return fuzzy[0][1]


def _read_limit_near(items: List[Dict], name_center, rect) -> Optional[Tuple[int, int, str]]:
    """读商品卡上方的「本月限购 N/M」，返回 (剩余, 上限, 原文)。"""
    if not rect or not name_center:
        return None
    _, _, width, height = rect
    nx, ny = int(name_center[0]), int(name_center[1])
    for item in items or []:
        text = str(item.get("text") or "")
        match = _LIMIT_RE.search(text)
        if not match:
            continue
        center = item.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        dx = (x - nx) / float(width)
        dy = (y - ny) / float(height)
        if -LIMIT_BAND_DX <= dx <= LIMIT_BAND_DX and LIMIT_DY_MIN <= dy <= LIMIT_DY_MAX:
            try:
                return int(match.group(1)), int(match.group(2)), text.strip()
            except ValueError:
                continue
    return None


def _wait_confirm(logger: logging.Logger, hwnd: int, timeout: float, stop_check=None) -> Optional[Dict]:
    """等商品兑换弹窗的确认按钮（「确认兑换/兑换/购买/确认」）。"""
    rect = _window_rect(hwnd)
    if not rect:
        return None
    region = _abs_region(rect, DIALOG_REGION)
    end = time.time() + max(0.5, timeout)
    while time.time() < end:
        if _stop(stop_check):
            return None
        items = _ocr_full(logger, hwnd)
        hit = _pick_confirm(items, region)
        if hit:
            return hit
        time.sleep(0.8)
    return None


def _buy_one(
    logger: logging.Logger,
    hwnd: int,
    name: str,
    stop_check=None,
    grid_frac=EXCHANGE_GRID_REGION,
) -> Dict:
    """买一件商品。返回 {"state", "detail", "before", "after"}。

    state: purchased / missing / sold_out / no_dialog / unconfirmed / failed
    到账证据只认「本月限购 N」递减或购买后出现「空白区域继续」奖励页，二者都没有就
    unconfirmed（不报假成功）。
    """
    out: Dict = {"state": "failed", "detail": "", "before": None, "after": None}
    rect = _window_rect(hwnd)
    if not rect:
        out["detail"] = "窗口区域不可用"
        return out
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    items = _ocr_full(logger, hwnd)
    match = _find_item_name(items, name, rect, grid_frac)
    if not match:
        out.update(state="missing", detail="兑换页商品网格里没找到该商品")
        logger.warning("[无限暖暖·晶石兑换] %s 未在网格中找到（可能不在首屏/需翻页）", name)
        return out
    name_center = (int(match["center"][0]), int(match["center"][1]))
    limit = _read_limit_near(items, name_center, rect)
    before = limit[0] if limit else None
    out["before"] = before
    if before == 0:
        out.update(state="sold_out", detail=f"本月限购已为 0（{limit[2] if limit else ''}）")
        logger.info("[无限暖暖·晶石兑换] %s 本月已换完", name)
        return out
    logger.info(
        "[无限暖暖·晶石兑换] 点击商品 %r（OCR %r conf %.2f）@ %s%s",
        name,
        match.get("text"),
        float(match.get("confidence") or 0),
        name_center,
        f"，本月限购 {before}" if before is not None else "（本月限购未识别）",
    )
    _click(name_center)

    confirm = _wait_confirm(logger, hwnd, timeout=6.0, stop_check=stop_check)
    dismissed = 0
    if confirm:
        center = confirm.get("center")
        if center:
            _click(center)
            logger.info("[无限暖暖·晶石兑换] 点击「%s」@ %s", confirm.get("text"), center)
        time.sleep(1.0)
        dismissed = _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=3)
    else:
        logger.warning("[无限暖暖·晶石兑换] %s 点击后未出现确认弹窗", name)

    time.sleep(0.8)
    fresh = _ocr_full(logger, hwnd)
    after_limit = _read_limit_near(fresh, name_center, rect)
    after = after_limit[0] if after_limit else None
    out["after"] = after
    if before is not None and after is not None and after < before:
        out.update(state="purchased", detail=f"本月限购 {before}→{after}")
        logger.info("[无限暖暖·晶石兑换] %s 购买成功（本月限购 %s→%s）", name, before, after)
        return out
    if dismissed:
        out.update(state="purchased", detail="购买后出现「空白区域继续」奖励页")
        logger.info("[无限暖暖·晶石兑换] %s 购买成功（奖励页已确认）", name)
        return out
    if not confirm:
        out.update(state="no_dialog", detail="点击后未出现确认弹窗，且无到账证据")
        logger.warning("[无限暖暖·晶石兑换] %s %s", name, out["detail"])
        return out
    out.update(state="unconfirmed", detail=f"点了确认但本月限购未变化（{before}→{after}）")
    logger.warning("[无限暖暖·晶石兑换] %s %s", name, out["detail"])
    return out


def _open_calendar(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    for attempt in range(1, 4):
        if _stop(stop_check):
            return False
        _focus(hwnd)
        _press_key("l")
        logger.info("[无限暖暖·奇想日历] 按 L 打开奇想日历（第 %s/3 次）", attempt)
        items = _wait_for(logger, hwnd, _is_calendar_page, timeout=6.0, stop_check=stop_check)
        if items:
            logger.info("[无限暖暖·奇想日历] 已进入奇想日历")
            return True
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    return False


def _claim_pair_rewards(logger: logging.Logger, hwnd: int, stop_check=None) -> Tuple[int, str]:
    """奇想日历 → 奇迹之冠 → 搭配赛 → 有「领取奖励」就点掉并收尾奖励页。"""
    if _stop(stop_check):
        return 0, "用户停止"
    rect = _window_rect(hwnd)
    if not rect:
        return 0, "窗口不可用"
    items = _ocr_full(logger, hwnd)
    crown = _pick(items, (CROWN_ENTRY,), min_conf=0.5, region=_abs_region(rect, CALENDAR_CROWN_REGION))
    if not crown:
        _record_failure(logger, "奇迹之冠-入口", "奇想日历里未找到「奇迹之冠」", hwnd, items)
        return 0, "未找到「奇迹之冠」入口"
    _click(crown["center"])
    logger.info("[无限暖暖·奇想日历] 点击「奇迹之冠」@ %s", crown["center"])
    time.sleep(1.2)

    hub = _wait_for(
        logger,
        hwnd,
        lambda its: bool(_pick(its, CROWN_HUB_MARKERS, min_conf=0.5)),
        timeout=8.0,
        stop_check=stop_check,
    )
    if not hub:
        _record_failure(logger, "奇迹之冠-页面", "点击后未进入奇迹之冠", hwnd)
        return 0, "未进入奇迹之冠"
    pair = _pick(hub, (PAIR_ENTRY,), min_conf=0.5, region=_abs_region(rect, HUB_PAIR_REGION))
    if not pair:
        _record_failure(logger, "奇迹之冠搭配赛-入口", "奇迹之冠里未找到「奇迹之冠搭配赛」", hwnd, hub)
        return 0, "未找到「奇迹之冠搭配赛」"
    _click(pair["center"])
    logger.info("[无限暖暖·奇想日历] 点击「奇迹之冠搭配赛」@ %s", pair["center"])
    time.sleep(1.2)

    stage = _wait_for(logger, hwnd, _is_stage_page, timeout=10.0, stop_check=stop_check)
    if not stage:
        _record_failure(logger, "奇迹之冠搭配赛-页面", "搭配赛页面未就绪", hwnd)
        return 0, "搭配赛页面未就绪"

    claimed = 0
    for _ in range(3):
        if _stop(stop_check):
            break
        items = _ocr_full(logger, hwnd)
        hit = _pick(items, CLAIM_TEXTS, min_conf=0.5)
        if not hit:
            break
        _click(hit["center"])
        claimed += 1
        logger.info("[无限暖暖·奇想日历] 点击「领取奖励」@ %s", hit["center"])
        time.sleep(1.2)
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=4)

    leftover = _pick(_ocr_full(logger, hwnd), CLAIM_TEXTS, min_conf=0.5)
    if leftover:
        _record_failure(logger, "奇迹之冠-领取奖励", "点击后「领取奖励」仍存在", hwnd)
        return claimed, (
            f"领奖 {claimed} 次（仍有领取按钮，已记录）" if claimed else "领取奖励未能点掉（已记录）"
        )
    return claimed, (f"领奖 {claimed} 次" if claimed else "无可领奖励")


def _buy_plan_items(
    logger: logging.Logger,
    hwnd: int,
    plan: Dict,
    stop_check,
    grid_frac,
    meta_key: str,
    label: str,
) -> str:
    """在已打开的商店页按清单逐件购买（点商品→确认→空白区域继续），返回摘要。

    到账只认「本月限购 N/M」递减或奖励页出现；部分买到但有硬失败/没买满（非售罄）
    时不写月度标记，留给下次重试；售罄导致的差额视为正常完成。
    """
    purchased = 0
    bought_labels: List[str] = []
    missing: List[str] = []
    failed: List[str] = []
    shortfall: List[str] = []
    stopped = False
    for item in plan["items"]:
        name = item["name"]
        qty = int(item.get("qty") or 1)
        bought = 0
        last: Optional[Dict] = None
        for _ in range(qty):
            if _stop(stop_check):
                stopped = True
                break
            out = _buy_one(logger, hwnd, name, stop_check=stop_check, grid_frac=grid_frac)
            state = out.get("state")
            if state == "purchased":
                bought += 1
                continue
            last = out
            break
        if bought:
            purchased += bought
            bought_labels.append(f"{name}×{bought}" if qty > 1 else name)
            if bought < qty:
                shortfall.append(f"{name} {bought}/{qty}（{(last or {}).get('state') or 'stopped'}）")
        else:
            state = (last or {}).get("state") or "failed"
            if state == "sold_out":
                missing.append(f"{name}(已买完)")
            elif state == "missing":
                missing.append(f"{name}(未找到)")
            else:
                failed.append(f"{name}({state})")
        logger.info("[无限暖暖·%s] %s → 买到 %s/%s", label, name, bought, qty)
        if stopped:
            break

    if stopped:
        logger.info("[无限暖暖·%s] 用户停止（已买 %s 件，不写月度标记）", label, purchased)
        return "用户停止（兑换未完成）"

    detail = ""
    if purchased:
        detail = f"已兑换 {purchased} 件：" + "、".join(bought_labels)
        if shortfall:
            detail += f"；未买满：{'、'.join(shortfall)}"
        if missing:
            detail += f"；未买到：{'、'.join(missing)}"
        if failed:
            detail += f"；失败：{'、'.join(failed)}"
        # 只有「库存不足（sold_out）」可以接受为完成；点了没反应/没确认到账要下轮重试，
        # 否则本月剩下的库存会被月度标记永久锁死。
        retry_needed = bool(failed) or any("sold_out" not in s for s in shortfall)
        if retry_needed:
            detail += "；未写月度标记（下次运行重试）"
            logger.warning("[无限暖暖·%s] %s", label, detail)
        else:
            _mark_monthly_done(logger, meta_key)
            logger.info("[无限暖暖·%s] %s", label, detail)
        return detail
    if missing and not failed and all("已买完" in m for m in missing):
        detail = "清单内商品本月均已换完：" + "、".join(missing)
        _mark_monthly_done(logger, meta_key)
        logger.info("[无限暖暖·%s] %s", label, detail)
        return detail
    detail = "未兑换到任何商品"
    if missing:
        detail += f"；未找到/已买完：{'、'.join(missing)}"
    if failed:
        detail += f"；失败：{'、'.join(failed)}"
    _record_failure(logger, f"{label}-购买", detail, hwnd)
    logger.warning("[无限暖暖·%s] %s", label, detail)
    return detail


def _exchange_crystals(logger: logging.Logger, hwnd: int, stop_check=None) -> str:
    plan = load_crystal_exchange_plan(logger)
    if not plan.get("enabled"):
        return "晶石兑换已关闭"
    if not plan["items"]:
        return "晶石兑换清单为空（待定）"
    if _monthly_done(logger, MONTHLY_META_KEY):
        logger.info("[无限暖暖·晶石兑换] 本月已兑换，跳过")
        return "本月晶石兑换已完成"
    if _stop(stop_check):
        return "用户停止"

    rect = _window_rect(hwnd)
    if not rect:
        return "窗口不可用，跳过晶石兑换"
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    items = _ocr_full(logger, hwnd)
    entry = _pick(items, (EXCHANGE_TITLE,), min_conf=0.5, region=_abs_region(rect, STAGE_MENU_REGION))
    if not entry:
        _record_failure(logger, "晶石兑换-入口", "搭配赛页未找到左侧「晶石兑换」", hwnd, items)
        return "未找到「晶石兑换」入口"
    _click(entry["center"])
    logger.info("[无限暖暖·晶石兑换] 点击左侧「晶石兑换」@ %s", entry["center"])
    time.sleep(1.2)
    page = _wait_for(logger, hwnd, _is_exchange_page, timeout=8.0, stop_check=stop_check)
    if not page:
        _record_failure(logger, "晶石兑换-页面", "未进入晶石兑换页", hwnd)
        return "未进入晶石兑换页"
    return _buy_plan_items(
        logger, hwnd, plan, stop_check, EXCHANGE_GRID_REGION, MONTHLY_META_KEY, "晶石兑换"
    )


def _is_shop_page(items: List[Dict]) -> bool:
    # 主界面也有「商城」但没有「购物车」，两个都命中才算进了商城
    return len(_markers_present(items, SHOP_MARKERS, 0.45)) >= 2


def _is_resonance_page(items: List[Dict]) -> bool:
    return len(_markers_present(items, RESONANCE_TABS, 0.45)) >= 2


def _has_serene_markers(items: List[Dict]) -> bool:
    return bool(_markers_present(items, SERENE_MARKERS, 0.5))


def _scroll_at(logger: logging.Logger, hwnd: int, point, clicks: int) -> None:
    import pyautogui

    from .foreground_flow_runner import FailSafeAborted

    try:
        pyautogui.scroll(int(clicks), x=int(point[0]), y=int(point[1]))
        logger.debug("[无限暖暖·共鸣商店] 滚轮 %s @ %s", clicks, point)
    except pyautogui.FailSafeException as exc:
        raise FailSafeAborted(
            "PyAutoGUI 紧急停止已触发：鼠标位于屏幕左上角 (0,0)。"
        ) from exc


def _open_shop(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    for attempt in range(1, 4):
        if _stop(stop_check):
            return False
        _focus(hwnd)
        _press_key("h")
        logger.info("[无限暖暖·共鸣商店] 按 H 打开商城（第 %s/3 次）", attempt)
        items = _wait_for(logger, hwnd, _is_shop_page, timeout=6.0, stop_check=stop_check)
        if items:
            logger.info("[无限暖暖·共鸣商店] 已进入商城")
            return True
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    return False


def _find_resonance_entry(logger: logging.Logger, hwnd: int, stop_check=None) -> Optional[Dict]:
    """左列滚动找「共鸣商店」：当前屏 → 向下滚 → 向上滚，每滚一次都复扫。"""
    rect = _window_rect(hwnd)
    if not rect:
        return None
    region = _abs_region(rect, SHOP_MENU_REGION)
    items = _ocr_full(logger, hwnd)
    entry = _pick(items, (RESONANCE_ENTRY,), min_conf=0.5, region=region)
    if entry:
        return entry
    anchor_item = _pick(items, (SHOP_MENU_ITEM,), min_conf=0.45, region=region)
    anchor = (
        tuple(anchor_item["center"])
        if anchor_item
        else (
            rect[0] + int(rect[2] * SHOP_MENU_SCROLL_ANCHOR[0]),
            rect[1] + int(rect[3] * SHOP_MENU_SCROLL_ANCHOR[1]),
        )
    )
    _focus(hwnd)
    for direction in (-1, 1):
        for _ in range(10):
            if _stop(stop_check):
                return None
            _scroll_at(logger, hwnd, anchor, direction * 2)
            time.sleep(0.45)
            items = _ocr_full(logger, hwnd)
            entry = _pick(items, (RESONANCE_ENTRY,), min_conf=0.5, region=region)
            if entry:
                logger.info("[无限暖暖·共鸣商店] 滚动后找到「共鸣商店」@ %s", entry["center"])
                return entry
            anchor_item = _pick(items, (SHOP_MENU_ITEM,), min_conf=0.45, region=region)
            if anchor_item:
                anchor = tuple(anchor_item["center"])
    return None


def _select_serene_tab(logger: logging.Logger, hwnd: int, stop_check=None) -> bool:
    """必须切到「静谧水滴兑换」：用专属商品（万象星实/棱镜）确认，而不是只看页签名。"""
    if _stop(stop_check):
        return False
    if _has_serene_markers(_ocr_full(logger, hwnd)):
        logger.info("[无限暖暖·共鸣商店] 已在「静谧水滴兑换」")
        return True
    rect = _window_rect(hwnd)
    if not rect:
        return False
    region = _abs_region(rect, RESONANCE_TAB_REGION)
    for _ in range(2):
        if _stop(stop_check):
            return False
        items = _ocr_full(logger, hwnd)
        tab = _pick(items, (RESONANCE_TAB_TARGET,), min_conf=0.5, region=region)
        if not tab:
            _record_failure(logger, "共鸣商店-页签", "未找到「静谧水滴兑换」页签", hwnd, items)
            return False
        _click(tab["center"])
        logger.info("[无限暖暖·共鸣商店] 点击「静谧水滴兑换」@ %s", tab["center"])
        time.sleep(1.2)
        page = _wait_for(logger, hwnd, _has_serene_markers, timeout=5.0, stop_check=stop_check)
        if page:
            logger.info("[无限暖暖·共鸣商店] 已确认静谧水滴页（万象星实等专属商品出现）")
            return True
    _record_failure(logger, "共鸣商店-页签", "点击后仍未切到「静谧水滴兑换」", hwnd)
    return False


def _run_resonance_shop(logger: logging.Logger, hwnd: int, stop_check=None) -> str:
    """主界面 H → 商城 → 左列滚动找共鸣商店 → 静谧水滴兑换 → 换抽卡道具（每月）。"""
    plan = load_resonance_shop_plan(logger)
    if not plan.get("enabled"):
        return "共鸣商店已关闭"
    if not plan["items"]:
        return "共鸣商店清单为空（待定）"
    if _monthly_done(logger, RESONANCE_META_KEY):
        logger.info("[无限暖暖·共鸣商店] 本月已兑换，跳过")
        return "本月共鸣商店已完成"
    if _stop(stop_check):
        return "用户停止"

    if not _open_shop(logger, hwnd, stop_check=stop_check):
        _record_failure(logger, "共鸣商店-商城", "按 H 后未进入商城", hwnd)
        return "商城未打开（已记录）"
    entry = _find_resonance_entry(logger, hwnd, stop_check=stop_check)
    if not entry:
        _record_failure(logger, "共鸣商店-入口", "左列滚动后未找到「共鸣商店」", hwnd)
        return "未找到「共鸣商店」入口"
    _click(entry["center"])
    logger.info("[无限暖暖·共鸣商店] 点击「共鸣商店」@ %s", entry["center"])
    time.sleep(1.2)
    page = _wait_for(logger, hwnd, _is_resonance_page, timeout=8.0, stop_check=stop_check)
    if not page:
        _record_failure(logger, "共鸣商店-页面", "未进入共鸣商店", hwnd)
        return "未进入共鸣商店"
    if not _select_serene_tab(logger, hwnd, stop_check=stop_check):
        return "未确认「静谧水滴兑换」页签，未购买"
    return _buy_plan_items(
        logger, hwnd, plan, stop_check, SHOP_GRID_REGION, RESONANCE_META_KEY, "共鸣商店"
    )


def _return_to_main(logger: logging.Logger, hwnd: int, scales, confidences, stop_check=None) -> bool:
    for attempt in range(1, 6):
        if _stop(stop_check):
            return False
        _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
        if _has_main(hwnd, scales, confidences):
            logger.info("[无限暖暖·奇想日历] 已回到主界面")
            return True
        _focus(hwnd)
        _press_esc()
        logger.info("[无限暖暖·奇想日历] 按 ESC 返回主界面（第 %s/5 次）", attempt)
        time.sleep(1.2)
    return _has_main(hwnd, scales, confidences)


def _close_game_for_whimbox_restart(logger: logging.Logger) -> None:
    """交接兜底：关闭游戏，让 whimbox 重新启动并进入主界面。

    用户 2026-11-01 叮嘱：后续接奇想盒时必须保证游戏在主界面；保证不了就关掉游戏，
    让奇想盒按自己的启动流程重新进。只关游戏本体，不动启动器。
    """
    try:
        from .foreground_flow_runner import _find_process_by_exact_name, close_flow_processes
        from .nikki_launcher import GAME_PROCESS

        close_flow_processes(logger, "NikkiLauncher", None, [GAME_PROCESS])
        # 等进程真正退出，避免 whimbox 启动时撞上正在退出的旧窗口
        for _ in range(20):
            if not _find_process_by_exact_name(GAME_PROCESS):
                break
            time.sleep(0.5)
        logger.warning(
            "[无限暖暖·交接] 未能确认回到主界面，已关闭游戏 %s，交给 whimbox 重新进入",
            GAME_PROCESS,
        )
    except Exception as exc:  # noqa: BLE001 - 关游戏失败只能记录，后续仍交给 whimbox
        logger.warning("[无限暖暖·交接] 关闭游戏失败: %s", exc)


def _ensure_main_or_close(logger: logging.Logger, hwnd: int, scales, confidences, stop_check=None) -> str:
    """交接前检查：主界面 → main；回不去 → 关游戏 → closed；用户停止 → stopped。"""
    if _stop(stop_check):
        return "stopped"
    if _return_to_main(logger, hwnd, scales, confidences, stop_check=stop_check):
        return "main"
    _record_failure(logger, "奇想日历-交接", "未能确认回到主界面，已关闭游戏交给 whimbox 重新进入", hwnd)
    _close_game_for_whimbox_restart(logger)
    return "closed"


def _run_flow_inner(
    logger: logging.Logger,
    hwnd: int,
    scales,
    confidences,
    stop_check=None,
) -> Tuple[bool, str]:
    if _stop(stop_check):
        return False, "用户请求停止"
    if not hwnd or not _window_rect(hwnd):
        if _stop(stop_check):
            return False, "用户请求停止"
        _close_game_for_whimbox_restart(logger)
        logger.warning("[无限暖暖·奇想日历] 游戏窗口不可用，已关闭游戏交给 whimbox 重新进入")
        return True, "游戏窗口不可用，已关闭游戏交给 whimbox 重新进入"

    _focus(hwnd)
    _dismiss_continue_screens(logger, hwnd, stop_check, max_rounds=2)
    if not _has_main(hwnd, scales, confidences):
        time.sleep(1.5)
    if not _has_main(hwnd, scales, confidences):
        message = "未识别到暖暖主界面，跳过奇想日历"
        _record_failure(logger, "奇想日历-入口", message, hwnd)
        if _stop(stop_check):
            return False, "用户请求停止"
        # 交接规则：保证不了在主界面就关游戏，让 whimbox 自己重新进
        _close_game_for_whimbox_restart(logger)
        message += "（已关闭游戏交给 whimbox 重新进入）"
        logger.warning("[无限暖暖·奇想日历] %s", message)
        return True, message

    if not _open_calendar(logger, hwnd, stop_check=stop_check):
        _record_failure(logger, "奇想日历-打开", "按 L 后未出现奇想日历", hwnd)
        _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=stop_check)
        logger.warning("[无限暖暖·奇想日历] 奇想日历未打开（已记录，继续后续任务）")
        return True, "奇想日历未打开（已记录，继续后续任务）"

    notes: List[str] = []
    try:
        _, claim_note = _claim_pair_rewards(logger, hwnd, stop_check=stop_check)
        notes.append(claim_note)
        notes.append(_exchange_crystals(logger, hwnd, stop_check=stop_check))
        if _return_to_main(logger, hwnd, scales, confidences, stop_check=stop_check):
            notes.append(_run_resonance_shop(logger, hwnd, stop_check=stop_check))
        else:
            notes.append("未回到主界面，跳过共鸣商店")
    finally:
        handoff = _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=stop_check)
        if handoff == "closed":
            notes.append("未确认回到主界面，已关闭游戏交给 whimbox 重新进入")
        elif handoff == "stopped":
            notes.append("用户停止（未做交接检查）")
    return True, "；".join(n for n in notes if n)


def run_nikki_calendar_flow(
    logger: logging.Logger,
    hwnd: int,
    scales,
    confidences,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    """主入口（action=nikki_calendar）：返回 (是否继续, 摘要)。

    除用户停止/紧急停止外恒返回 True：领奖或兑换失败只记录，不阻断后续 whimbox。
    """
    from .foreground_flow_runner import FailSafeAborted

    try:
        return _run_flow_inner(logger, hwnd, scales, confidences, stop_check=stop_check)
    except FailSafeAborted:
        raise
    except Exception as exc:  # noqa: BLE001 - 流程异常不能让整个暖暖任务不跑
        logger.error("[无限暖暖·奇想日历] 流程异常（已记录，继续后续任务）: %s", exc, exc_info=True)
        _record_failure(logger, "奇想日历-流程", f"流程异常: {exc}", hwnd)
        try:
            _ensure_main_or_close(logger, hwnd, scales, confidences, stop_check=None)
        except FailSafeAborted:
            raise
        except Exception as back_exc:  # noqa: BLE001 - 收尾失败只记录
            logger.debug("[无限暖暖·奇想日历] 异常后回主界面失败: %s", back_exc)
        return True, f"奇想日历流程异常（已记录，继续后续任务）: {exc}"
