"""白荆回廊 · 有猫零售「有猫兑换」每月购买（2026-10-25 新增）。

背景：用户 2026-10-25 指出「有猫零售 → 有猫兑换」页每月刷新，可以每月买点小东西；
但**具体买什么当月还没定**，所以购买清单走配置
（app_config.json → `baijing.cat_exchange.items`），清单为空时本流程只报「待配置」，
绝不乱买东西、绝不报假成功（对齐 AGENTS「optional/跳过不得报成功」铁律）。

几何定标（2026-10-25 用户截图实跑 RapidOCR，参考图
artifacts/ref_baijing_cat_exchange_20261025.png，1919x1079）：
  - 左列「有猫兑换」OCR 中心 frac (0.105, 0.489)，conf 0.77；
    上邻「特别供应」frac y 0.423、下邻「特级商店」frac y 0.559 → 取窄带隔离
  - 顶部页签行 frac y = 0.200：小猫礼品 0.272 / 大猫兑换 0.403 / 橘猫尊享 0.534 /
    活动拾遗 0.664 / 动态立绘 0.795（x frac）
  - 商品名行 frac y = 0.483（第一行）/ 0.795（第二行），
    五列 x frac = 0.280 / 0.433 / 0.587 / 0.741 / 0.895
  - 价格数字行在名下方 0.035 窗高，数字中心比名中心右移约 0.013 窗宽
    （实测：秘银拓片 12 @+26、无错回环 160 @+24、海隅点数 12 @+23、单向介点 15 @+26）
  - **价格数字 OCR 召回约 60%**（个位价 6/6/6/6 全部漏检）→ 定位主锚是「商品名」，
    价格只用于预算校验，取不到价格就不买（宁可漏买不可超支）

配置格式（app_config.json）：
    "baijing": {
      "cat_exchange": {
        "enabled": true,
        "tab": "小猫礼品",
        "items": [
          {"name": "心迹", "max_price": 160, "max_qty": false, "tab": "小猫礼品"}
        ]
      }
    }
  - items 支持字符串（"心迹"）或对象（上面那种）
  - max_price = 本次购买**总价**上限（有猫）；设了但读不到合计金额 → 不买（诚实失败）
  - max_qty = 是否点「最大」一次买满剩余库存（默认 false，买 1 件）
"""

import difflib
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import pyautogui

from .baijing_daily import (
    SHOP_BUY_FALLBACK,
    SHOP_DIALOG_BUY_REGION,
    SHOP_DIALOG_BUY_TEXTS,
    SHOP_DIALOG_MARKERS,
    SHOP_DIALOG_REGION,
    SHOP_MAX_QTY_FALLBACK,
    _abs_region,
    _capture_daily_list_snapshot,
    _click_ratio,
    _dismiss_overlay,
    _ensure_window,
    _leave_retail_shop,
    _ocr_best_match,
    _open_retail_shop,
    _stopped,
    _wait_page_markers,
    _wait_purchase_settled,
)
from .baijing_flow import _focus_game_window, _window_region, detect_baijing_main_ui
from .foreground_flow_runner import _pyautogui_click_guarded
from .ocr_click_tester import _normalize_text, ocr_window_content

# ── 左列入口 ────────────────────────────────────────────────────────
CAT_EXCHANGE_ENTRY_TEXTS = ("有猫兑换",)
# 左列第 6 项；上邻「特别供应」0.423、下邻「特级商店」0.559，取 0.44~0.53 窄带
CAT_EXCHANGE_ENTRY_REGION = (0.0, 0.44, 0.20, 0.53)
CAT_EXCHANGE_ENTRY_FALLBACK = (0.105, 0.489)   # 实测 OCR 中心 (201,528)/1919x1079

# ── 顶部页签 ────────────────────────────────────────────────────────
CAT_TAB_TEXTS = ("小猫礼品", "大猫兑换", "橘猫尊享", "活动拾遗", "动态立绘")
CAT_TAB_REGION = (0.20, 0.18, 0.90, 0.24)      # 页签行 frac y ≈ 0.200
# 页面标志：这两个页签只在「有猫兑换」页出现（别的零售页没有）
CAT_PAGE_MARKERS = ("大猫兑换", "橘猫尊享")

# ── 商品网格 ────────────────────────────────────────────────────────
# 排除顶栏资源数字（y<0.16）、左列功能区（x<0.18）、页签行（y<0.24）
CAT_GRID_REGION = (0.18, 0.24, 1.00, 0.95)
CAT_PRICE_DY = 0.035        # 价格数字中心在商品名中心下方多少窗高
CAT_PRICE_DX = 0.013        # 价格数字中心比商品名中心右移多少窗宽
CAT_PRICE_BAND_DY = 0.050   # 价格数字搜索窗（名下方）
CAT_PRICE_BAND_DX = 0.070   # 价格数字搜索窗（同列）

CAT_SOLD_OUT_TEXTS = ("剩余0件", "已售罄", "已购买", "已领完")
CAT_NAME_MIN_CONF = 0.45    # 商品名 OCR 置信度下限（实测「卷礁」只有 0.51）
CAT_FUZZY_MIN_RATIO = 0.85  # 名称模糊匹配下限（exact 优先）
CAT_FUZZY_MARGIN = 0.05     # 与次优候选的最小差距（避免两个候选都像）

# 价格文本：纯数字/逗号（排除 "剩余5件"、"x4,000"、"2.5折" 这类）
_PRICE_TEXT_RE = re.compile(r"^[\d,]{1,9}$")
# 合计金额（弹窗里）：允许 "40[160]" 这种划线价写法，取第一个数字
_TOTAL_TEXT_RE = re.compile(r"(\d[\d,]{0,8})")


def _parse_price(text: str) -> Optional[int]:
    """'1,427' → 1427；'40[160]' → 40；非价格文本 → None。"""
    tnorm = _normalize_text(str(text or "")).replace(",", "")
    if not tnorm or not tnorm.isdigit():
        return None
    try:
        return int(tnorm)
    except ValueError:
        return None


def _parse_total(text: str) -> Optional[int]:
    """从 '合计' 附近的文本里解析金额（兼容 '40[160]' 划线价写法）。"""
    m = _TOTAL_TEXT_RE.search(_normalize_text(str(text or "")))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def load_cat_exchange_plan(logger: logging.Logger) -> Dict:
    """读 app_config.json → baijing.cat_exchange，返回规范化后的购买计划。

    返回 {"enabled": bool, "tab": str, "items": [{"name","max_price","max_qty","tab"}]}
    读不到配置时返回空清单（调用方据此跳过，绝不猜着买）。
    """
    plan: Dict = {"enabled": True, "tab": "", "items": []}
    try:
        from ..settings import manager as _cfg_manager

        cfg = _cfg_manager.load(force=True) or {}
        section = (cfg.get("baijing") or {}).get("cat_exchange") or {}
    except Exception as e:  # noqa: BLE001 - 配置读失败只降级，不炸一条龙
        logger.warning(f"[白荆回廊·有猫兑换] 读取配置失败（按空清单处理）: {e}")
        return plan

    plan["enabled"] = bool(section.get("enabled", True))
    plan["tab"] = str(section.get("tab") or "").strip()
    raw_items = section.get("items") or []
    if not isinstance(raw_items, list):
        logger.warning("[白荆回廊·有猫兑换] 配置 items 不是列表，按空清单处理")
        raw_items = []
    for raw in raw_items:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        max_price = raw.get("max_price")
        try:
            max_price = int(max_price) if max_price not in (None, "") else None
        except (TypeError, ValueError):
            max_price = None
        plan["items"].append(
            {
                "name": name,
                "max_price": max_price,
                "max_qty": bool(raw.get("max_qty", False)),
                "tab": str(raw.get("tab") or plan["tab"] or "").strip(),
            }
        )
    logger.info(
        f"[白荆回廊·有猫兑换] 购买清单载入 {len(plan['items'])} 项"
        + (f"：{'、'.join(i['name'] for i in plan['items'])}" if plan["items"] else "（空清单，待定）")
    )
    return plan


def _find_name_match(results: List[Dict], target: str, abs_grid) -> Optional[Dict]:
    """在商品网格里按名称找商品卡：精确优先，其次模糊（需唯一）。"""
    tnorm_target = _normalize_text(target)
    if not tnorm_target:
        return None
    exact: List[Dict] = []
    fuzzy: List[Tuple[float, Dict]] = []
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
            center = r.get("center")
        except Exception:
            continue
        if conf < CAT_NAME_MIN_CONF or not center:
            continue
        if abs_grid:
            ax0, ay0, aw, ah = abs_grid
            if not (ax0 <= center[0] <= ax0 + aw and ay0 <= center[1] <= ay0 + ah):
                continue
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not tnorm:
            continue
        if tnorm == tnorm_target:
            exact.append(r)
        else:
            ratio = difflib.SequenceMatcher(None, tnorm_target, tnorm).ratio()
            if ratio >= CAT_FUZZY_MIN_RATIO:
                fuzzy.append((ratio, r))
    if exact:
        return max(exact, key=lambda r: float(r.get("confidence") or 0.0))
    if not fuzzy:
        return None
    fuzzy.sort(key=lambda x: x[0], reverse=True)
    if len(fuzzy) > 1 and (fuzzy[0][0] - fuzzy[1][0]) < CAT_FUZZY_MARGIN:
        return None  # 两个候选都像 → 宁可不买
    return fuzzy[0][1]


def _find_price_hit(results: List[Dict], name_center: Tuple[int, int], win_region) -> Optional[Dict]:
    """在商品名下方找价格数字（读不到返回 None，调用方走比例兜底）。"""
    if not win_region:
        return None
    left, top, width, height = win_region
    nx, ny = name_center
    best = None
    for r in results:
        try:
            conf = float(r.get("confidence") or 0.0)
            center = r.get("center")
        except Exception:
            continue
        if conf < 0.5 or not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (left <= x <= left + width and top <= y <= top + height):
            continue
        if not _PRICE_TEXT_RE.match(_normalize_text(str(r.get("text") or ""))):
            continue
        dx = (x - nx) / float(width)
        dy = (y - ny) / float(height)
        if -CAT_PRICE_BAND_DX <= dx <= CAT_PRICE_BAND_DX and 0.005 <= dy <= CAT_PRICE_BAND_DY:
            if best is None or conf > float(best.get("confidence") or 0.0):
                best = r
    return best


def _find_sold_out_near(results: List[Dict], name_center: Tuple[int, int], win_region) -> Optional[str]:
    """商品名正上方（卡片图底部角标）若有「剩余0件/已售罄」→ 该商品已买完/不可买。"""
    if not win_region:
        return None
    left, top, width, height = win_region
    nx, ny = name_center
    for r in results:
        tnorm = _normalize_text(str(r.get("text") or ""))
        if not any(k in tnorm for k in CAT_SOLD_OUT_TEXTS):
            continue
        center = r.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (left <= x <= left + width and top <= y <= top + height):
            continue
        if abs(x - nx) <= CAT_PRICE_BAND_DX * width and -0.09 * height <= (y - ny) <= 0.02 * height:
            return str(r.get("text") or "")
    return None


def _read_dialog_total(logger: logging.Logger, hwnd) -> Optional[int]:
    """读购买弹窗里的「合计」金额（读不到返回 None）。"""
    try:
        results = ocr_window_content(logger, hwnd=hwnd)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[白荆回廊·有猫兑换] 读合计失败: {e}")
        return None
    if not results:
        return None
    win_region = _window_region(hwnd)
    if not win_region:
        return None
    left, top, width, height = win_region
    base = None
    for r in results:
        tnorm = _normalize_text(str(r.get("text") or ""))
        if "合计" in tnorm and r.get("center"):
            base = r
            break
    if base is None:
        return None
    bx, by = int(base["center"][0]), int(base["center"][1])
    best = None
    for r in results:
        if r is base:
            continue
        text = str(r.get("text") or "")
        amount = _parse_total(text)
        if amount is None:
            continue
        center = r.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (left <= x <= left + width and top <= y <= top + height):
            continue
        dx = (x - bx) / float(width)
        dy = (y - by) / float(height)
        # 合计金额在「合计」右侧同一行，或正下方一行
        if (0.0 <= dx <= 0.18 and -0.015 <= dy <= 0.02) or (-0.05 <= dx <= 0.05 and 0.02 <= dy <= 0.08):
            if best is None or float(r.get("confidence") or 0.0) > float(best.get("confidence") or 0.0):
                best = r
    if best is None:
        return None
    amount = _parse_total(str(best.get("text") or ""))
    if amount is not None:
        logger.info(f"[白荆回廊·有猫兑换] 弹窗合计 {amount}（原文 {best.get('text')!r}）")
    return amount


def open_cat_exchange(logger: logging.Logger, hwnd, stop_check=None) -> bool:
    """进「有猫零售」→ 左列「有猫兑换」，并确认已切到该页（页签行出现）。"""
    if not _open_retail_shop(logger, hwnd, stop_check=stop_check):
        logger.warning("[白荆回廊·有猫兑换] 未打开零售页")
        return False
    entry = _ocr_best_match(
        logger,
        CAT_EXCHANGE_ENTRY_TEXTS,
        hwnd,
        frac_region=CAT_EXCHANGE_ENTRY_REGION,
        min_conf=0.55,
    )
    if entry:
        _pyautogui_click_guarded(*entry["center"])
        logger.info(f"[白荆回廊·有猫兑换] 点击左列「有猫兑换」@ {entry['center']}")
    else:
        logger.warning("[白荆回廊·有猫兑换] 左列未找到「有猫兑换」，用比例兜底点击")
        _click_ratio(logger, hwnd, CAT_EXCHANGE_ENTRY_FALLBACK, "「有猫兑换」（比例兜底）")
    time.sleep(1.8)
    if not _wait_page_markers(logger, hwnd, CAT_PAGE_MARKERS, timeout=8.0, stop_check=stop_check):
        logger.warning("[白荆回廊·有猫兑换] 未出现「有猫兑换」页签行，页面可能没切过去")
        return False
    logger.info("[白荆回廊·有猫兑换] 已进入有猫兑换页")
    return True


def switch_cat_tab(logger: logging.Logger, hwnd, tab_name: str, stop_check=None) -> bool:
    """切到指定页签（清单项可带 tab；不填就用配置里的默认页签）。"""
    if not tab_name:
        return True
    tab = _ocr_best_match(logger, (tab_name,), hwnd, frac_region=CAT_TAB_REGION, min_conf=0.55)
    if not tab:
        logger.warning(f"[白荆回廊·有猫兑换] 未找到页签 {tab_name!r}，留在当前页签")
        return False
    _pyautogui_click_guarded(*tab["center"])
    logger.info(f"[白荆回廊·有猫兑换] 切到页签 {tab_name!r} @ {tab['center']}")
    time.sleep(1.5)
    return True


def _buy_one_cat_item(logger: logging.Logger, hwnd, item: Dict, stop_check=None) -> Dict:
    """买单个商品。返回 {"state", "name", "price", "detail"}。

    state: purchased / missing / sold_out / over_price / price_unknown /
           no_dialog / unconfirmed / failed
    """
    name = item["name"]
    out: Dict = {"state": "failed", "name": name, "price": None, "detail": ""}
    results = ocr_window_content(logger, hwnd=hwnd)
    if not results:
        out.update(state="failed", detail="OCR 无结果")
        return out
    abs_grid = _abs_region(hwnd, CAT_GRID_REGION)
    win_region = _window_region(hwnd)
    match = _find_name_match(results, name, abs_grid)
    if not match:
        out.update(state="missing", detail="当前页签商品网格里没找到该商品")
        logger.warning(f"[白荆回廊·有猫兑换] {name} 未在网格中找到（可能不在当前页签/需翻页）")
        return out
    name_center = (int(match["center"][0]), int(match["center"][1]))
    logger.info(
        f"[白荆回廊·有猫兑换] 命中商品 {name!r}（OCR {match.get('text')!r} "
        f"conf {float(match.get('confidence') or 0):.2f}）@ {name_center}"
    )

    sold = _find_sold_out_near(results, name_center, win_region)
    if sold:
        out.update(state="sold_out", detail=f"卡片标记 {sold!r}，不可购买")
        logger.info(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
        return out

    # 价格数字优先用 OCR 命中；读不到（个位价常漏检）走比例兜底
    price_hit = _find_price_hit(results, name_center, win_region)
    if price_hit:
        price = _parse_price(str(price_hit.get("text") or ""))
        click_point = (int(price_hit["center"][0]), int(price_hit["center"][1]))
        logger.info(f"[白荆回廊·有猫兑换] {name} 价格 {price}（OCR 命中）")
    else:
        price = None
        if win_region:
            left, top, width, height = win_region
            click_point = (
                int(name_center[0] + CAT_PRICE_DX * width),
                int(name_center[1] + CAT_PRICE_DY * height),
            )
            logger.info(
                f"[白荆回廊·有猫兑换] {name} 价格数字未识别（常见于个位价），"
                f"按比例兜底点击 {click_point}"
            )
        else:
            out.update(state="failed", detail="窗口区域不可用，无法定位价格按钮")
            return out
    out["price"] = price

    _pyautogui_click_guarded(*click_point)
    logger.info(f"[白荆回廊·有猫兑换] 点击 {name} 价格按钮 @ {click_point}")
    if not _wait_page_markers(
        logger,
        hwnd,
        SHOP_DIALOG_MARKERS,
        timeout=6.0,
        stop_check=stop_check,
        frac_region=SHOP_DIALOG_REGION,
    ):
        # 没弹窗有两种可能：① 商品已买完（卡片变灰/无价格）② 点歪了没点到价格按钮。
        # 重新扫一遍该列的售罄标记，把「已买完」和「没点到」区分开，别混报一个失败。
        _dismiss_overlay(logger, hwnd)
        sold_after = None
        try:
            fresh = ocr_window_content(logger, hwnd=hwnd)
            sold_after = _find_sold_out_near(fresh or [], name_center, win_region)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[白荆回廊·有猫兑换] 复查售罄标记失败: {e}")
        if sold_after:
            out.update(state="sold_out", detail=f"点击后卡片标记 {sold_after!r}，已买完")
            logger.info(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
            return out
        out.update(state="no_dialog", detail="点击后未出现购买弹窗（可能点到非交互元素）")
        logger.warning(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
        return out

    # 需要买满剩余库存时点「最大」（+ 号右侧双箭头，无文字，按比例点）
    if item.get("max_qty"):
        _click_ratio(logger, hwnd, SHOP_MAX_QTY_FALLBACK, "「最大」数量按钮")
        time.sleep(0.4)

    # 预算校验：合计金额超过上限 → 不买
    max_price = item.get("max_price")
    if max_price is not None:
        total = _read_dialog_total(logger, hwnd)
        if total is None:
            _dismiss_overlay(logger, hwnd)
            out.update(state="price_unknown", detail=f"读不到合计金额，无法校验上限 {max_price}，放弃购买")
            logger.warning(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
            return out
        if total > max_price:
            _dismiss_overlay(logger, hwnd)
            out.update(state="over_price", price=total, detail=f"合计 {total} > 上限 {max_price}，放弃购买")
            logger.warning(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
            return out

    hit = _ocr_best_match(
        logger, SHOP_DIALOG_BUY_TEXTS, hwnd, frac_region=SHOP_DIALOG_BUY_REGION, min_conf=0.55
    )
    if hit:
        _pyautogui_click_guarded(*hit["center"])
        logger.info(f"[白荆回廊·有猫兑换] 点击「购买」@ {hit['center']}")
    else:
        _click_ratio(logger, hwnd, SHOP_BUY_FALLBACK, "「购买」（比例兜底）")
    state = _wait_purchase_settled(logger, hwnd, timeout=12.0, stop_check=stop_check)
    _dismiss_overlay(logger, hwnd)
    if state == "purchased":
        out.update(state="purchased", detail="购买成功")
        logger.info(f"[白荆回廊·有猫兑换] {name} 购买成功")
    else:
        out.update(state="unconfirmed", detail="点了购买但没确认到账")
        logger.warning(f"[白荆回廊·有猫兑换] {name} {out['detail']}")
    return out


def buy_cat_exchange_items(
    logger: logging.Logger,
    hwnd,
    items: List[Dict],
    stop_check=None,
) -> Dict:
    """按清单逐项购买（页签按项切换）。返回汇总结果。"""
    result: Dict = {
        "status": "skipped",
        "purchased": 0,
        "items": [],
        "missing": [],
        "failed": [],
        "detail": "",
    }
    if not items:
        result.update(status="skipped", detail="购买清单为空（待定）")
        return result
    current_tab = ""
    for item in items:
        if _stopped(stop_check):
            result["detail"] = "用户停止"
            return result
        want_tab = item.get("tab") or ""
        if want_tab and want_tab != current_tab:
            switch_cat_tab(logger, hwnd, want_tab, stop_check=stop_check)
            current_tab = want_tab
        hwnd = _ensure_window(logger, hwnd)
        if not hwnd:
            result["failed"].append(f"{item['name']}(窗口丢失)")
            result["detail"] = "游戏窗口丢失"
            break
        out = _buy_one_cat_item(logger, hwnd, item, stop_check=stop_check)
        state = out.get("state")
        if state == "purchased":
            result["purchased"] += 1
            result["items"].append(item["name"])
        elif state == "sold_out":
            result["missing"].append(f"{item['name']}(已买完)")
        elif state == "missing":
            # 没找到 ≠ 已买完：可能是商品不在当前页签/需要翻页 → 老实记「未找到」
            result["missing"].append(f"{item['name']}(未找到)")
        else:
            result["failed"].append(f"{item['name']}({state})")
        logger.info(
            f"[白荆回廊·有猫兑换] {item['name']} → {state}（{out.get('detail') or ''}）"
        )

    if result["purchased"]:
        result["status"] = "success"
        result["detail"] = (
            f"已购买 {result['purchased']}/{len(items)} 项："
            + "、".join(result["items"])
            + (f"；未买到：{'、'.join(result['missing'])}" if result["missing"] else "")
            + (f"；失败：{'、'.join(result['failed'])}" if result["failed"] else "")
        )
    elif result["missing"] and not result["failed"] and all("已买完" in m for m in result["missing"]):
        result["status"] = "already"
        result["detail"] = "清单内商品均已买完/不可买：" + "、".join(result["missing"])
    else:
        result["status"] = "failed"
        result["detail"] = (
            "未买到任何商品"
            + (f"；未找到/已买完：{'、'.join(result['missing'])}" if result["missing"] else "")
            + (f"；失败：{'、'.join(result['failed'])}" if result["failed"] else "")
        )
    logger.info(f"[白荆回廊·有猫兑换] 结果 {result['status']}：{result['detail']}")
    return result


def run_cat_exchange_monthly(logger: logging.Logger, hwnd=None, stop_check=None) -> Dict:
    """每月入口：主界面 → 有猫兑换 → 按清单买 → 返回主界面。

    返回 {"status": success|already|failed|skipped, "purchased", "items", "detail"}。
    """
    result: Dict = {"status": "skipped", "purchased": 0, "items": [], "detail": ""}
    if _stopped(stop_check):
        result["detail"] = "用户停止"
        return result
    plan = load_cat_exchange_plan(logger)
    if not plan.get("enabled"):
        result["detail"] = "配置已关闭（baijing.cat_exchange.enabled=false）"
        logger.info(f"[白荆回廊·有猫兑换] {result['detail']}")
        return result
    if not plan["items"]:
        result["detail"] = "购买清单为空（待定，暂不购买）"
        logger.info(f"[白荆回廊·有猫兑换] {result['detail']}")
        return result

    hwnd = _ensure_window(logger, hwnd)
    if not hwnd:
        result.update(status="failed", detail="未找到游戏窗口")
        return result

    is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=3.0)
    if not is_main and not _stopped(stop_check):
        # 用户叮嘱：不知道页面到哪了先 ESC 退一层再判
        _focus_game_window(logger, hwnd)
        pyautogui.press("escape")
        time.sleep(1.5)
        is_main, evidence = detect_baijing_main_ui(logger, hwnd, budget_sec=3.0)
    if not is_main:
        result.update(status="skipped", detail=f"未处于主界面（证据: {evidence}）")
        logger.warning(f"[白荆回廊·有猫兑换] {result['detail']}，本次跳过")
        return result

    if not open_cat_exchange(logger, hwnd, stop_check=stop_check):
        _leave_retail_shop(logger, hwnd, stop_check=stop_check)
        result.update(status="failed", detail="未进入有猫兑换页")
        return result
    _capture_daily_list_snapshot(logger, hwnd, prefix="cat_exchange")
    if plan.get("tab"):
        switch_cat_tab(logger, hwnd, plan["tab"], stop_check=stop_check)

    result = buy_cat_exchange_items(logger, hwnd, plan["items"], stop_check=stop_check)
    back_ok = _leave_retail_shop(logger, hwnd, stop_check=stop_check)
    if not back_ok:
        logger.warning("[白荆回廊·有猫兑换] 未确认回到主界面")
    return result
