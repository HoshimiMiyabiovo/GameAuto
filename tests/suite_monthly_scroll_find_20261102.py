"""月商店「长列表页」翻页找商品回归（2026-11-02）。

背景：崩铁余烬兑换页同屏只有 2~3 行商品（证据图
`data/artifacts/monthly_shop/20261102_120641_余烬兑换_采购前.jpg` 只有
命运的足迹 / 冒险记录 / 煤油以太 / 遗失碎金 / 信用点 / 无生残刃…），
清单里的**星轨专票、星轨通票在折叠区以下**。旧实现只做一次整窗 OCR，
找不到就按"看不到=已买过"吞掉 → 静默漏买（2026-11-02 日志里两条 0/5）。

修法：`monthly_exchange._scroll_find_item` —— 当前视图定位不到就往下翻着找，
用列表区域截图指纹判断"滚轮到底有没有生效"，翻遍列表仍没有才按已买过处理。

覆盖：
  1. 翻到第 N 屏后找到商品 → 返回坐标，且不滚回去（坐标马上要用来点击）
  2. 列表像素没变（滚不动）→ 立刻停止翻页
  3. 翻遍仍找不到 → 滚回顶部再返回 None
  4. _buy_items 把翻页兜底接进 _buy_once（当前视图没有 → 翻页找）
  5. step.item_scroll_search=false 时不翻页（保留旧行为）
  6. 滚轮落点换算 / 越界保护

跑法：.venv\\Scripts\\python.exe tests\\suite_monthly_scroll_find_20261102.py
"""

import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.monthly_exchange as mx  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    import logging

    logger = logging.getLogger("test_monthly_scroll_find")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


ITEM = {"name": "星轨专票", "ocr_text": "星轨专票"}


def test_find_after_scrolling():
    print("[1] 当前视图没有 → 往下翻页找到")
    scrolls = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    # 列表指纹每次都变（说明滚动真的生效），第 3 屏才找到
    sigs = iter(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
    locate_calls = {"n": 0}

    def _fake_locate(logger, hwnd, item, scales, confidences, stop_check, label):
        locate_calls["n"] += 1
        return (300, 400) if locate_calls["n"] >= 3 else None

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_signature", lambda hwnd, anchor: next(sigs)), \
            mock.patch.object(shop, "_locate_item", _fake_locate):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("翻页后找到商品", center == (300, 400), center)
    check("确实往下滚了", scrolls and scrolls[0] == mx.SCROLL_STEP, scrolls)
    check("找到后不往上滚回去（坐标要用来点击）", all(s < 0 for s in scrolls), scrolls)


def test_stop_when_list_unchanged():
    print("[2] 列表像素没变（滚不动）→ 立刻停止翻页")
    scrolls = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_signature", lambda hwnd, anchor: "same"), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("没找到返回 None", center is None, center)
    down = [s for s in scrolls if s < 0]
    check("只往下滚了一次就停", len(down) == 1, scrolls)


def test_restore_top_when_not_found():
    print("[3] 翻遍仍找不到 → 滚回顶部")
    scrolls = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    sigs = iter([f"s{i}" for i in range(40)])

    def _sig(hwnd, anchor):
        try:
            return next(sigs)
        except StopIteration:
            return "end"

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_signature", _sig), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("没找到返回 None", center is None, center)
    down = [s for s in scrolls if s < 0]
    up = [s for s in scrolls if s > 0]
    check("往下翻了若干屏", len(down) >= 1, scrolls)
    check("最后滚回顶部", len(up) >= 1, scrolls)


def test_buy_items_uses_scroll_finder():
    print("[4] _buy_items 把翻页兜底接进 _buy_once")
    calls = []

    def _fake_buy_once(logger, hwnd, item, confirm_template, scales, confidences,
                       stop_check, label="商店采购", scroll_finder=None):
        calls.append(scroll_finder)
        # 第一次（当前视图）没找到 → 用翻页兜底找到
        if scroll_finder is not None:
            return shop.BOUGHT
        return shop.ITEM_MISSING

    with mock.patch.object(mx, "_buy_once", _fake_buy_once):
        ok, summaries = mx._buy_items(
            _logger(), 1234, [dict(ITEM)], "confirm.png", "余烬兑换",
            [1.0], [0.8], None, scroll_search=True,
        )
    check("买了成功", ok is True and summaries == ["星轨专票: 1/1"], (ok, summaries))
    check("传了翻页兜底进去", calls and calls[0] is mx._scroll_find_item, calls)

    calls.clear()
    with mock.patch.object(mx, "_buy_once", _fake_buy_once):
        ok2, summaries2 = mx._buy_items(
            _logger(), 1234, [dict(ITEM)], "confirm.png", "余烬兑换",
            [1.0], [0.8], None, scroll_search=False,
        )
    check("关掉翻页时不传兜底", calls and calls[0] is None, calls)
    check("关掉翻页 → 旧行为（按已买过，不算失败）", ok2 is True, (ok2, summaries2))


def test_step_switch():
    print("[5] step.item_scroll_search 开关")
    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "monthly_exchange.py").read_text(encoding="utf-8")
    check("buy_monthly_exchange 读 step 开关",
          'step.get("item_scroll_search", True)' in src)
    check("_buy_items 默认开翻页",
          "scroll_search: bool = True" in src)


def test_anchor_math():
    print("[6] 滚轮落点换算")
    with mock.patch("game_auto.runtime.foreground_flow_runner._window_rect",
                    return_value=(100, 200, 800, 600)):
        pt = mx._scroll_anchor_point(1234, (0.5, 0.5))
        check("窗口中心换算正确", pt == (500, 500), pt)
        bad = mx._scroll_anchor_point(1234, (1.5, 0.5))
        check("越界比例返回 None", bad is None, bad)
    with mock.patch("game_auto.runtime.foreground_flow_runner._window_rect",
                    return_value=None):
        check("取不到窗口返回 None", mx._scroll_anchor_point(1234, (0.5, 0.5)) is None)


def main():
    test_find_after_scrolling()
    test_stop_when_list_unchanged()
    test_restore_top_when_not_found()
    test_buy_items_uses_scroll_finder()
    test_step_switch()
    test_anchor_math()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
