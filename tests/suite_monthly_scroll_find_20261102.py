"""月商店「长列表页」翻页找商品回归（2026-11-02，2026-11-03 修校验）。

背景：崩铁余烬兑换页同屏只有 2~3 行商品（证据图
`data/artifacts/monthly_shop/20261102_120641_余烬兑换_采购前.jpg` 只有
命运的足迹 / 冒险记录 / 煤油以太 / 遗失碎金 / 信用点 / 无生残刃…），
清单里的**星轨专票、星轨通票在折叠区下方**。旧实现只做一次整窗 OCR，
找不到就按"看不到=已买过"吞掉 → 静默漏买（2026-11-02 日志里两条 0/5）。

2026-11-03 用户复核：11-02 加的翻页**实测无效**——32x18 缩略图 MD5 被卡片
角标闪烁骗过，熔断一次没触发，每件商品 8 轮 x 25s 全跑满（3 件 12 分钟）。
现在改为像素平均差判滚动 + 连续两次不动熔断 + 每屏 OCR 6s + 失败落证据。

覆盖：
  1. 翻到第 N 屏后找到商品 → 返回坐标，且不滚回去
  2. 滚动后画面没变（像素差 < 阈值）→ 连续两次即熔断
  3. 翻遍仍找不到 → 滚回顶部 + 落失败证据
  4. _buy_items 把翻页兜底接进 _buy_once
  5. step.item_scroll_search=false 时不翻页
  6. 滚轮落点换算 / 越界保护
  7. diag 上报 effective_pages / scroll_verified_dead（2026-11-04）
  8. 一屏都没滚成功 → "看不到"判失败，不再按已购买处理（2026-11-04）

跑法：.venv\\Scripts\\python.exe tests\\suite_monthly_scroll_find_20261102.py
"""

import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.monthly_exchange as mx  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402

# 2026-11-04：新增的「一次滚到底 / 拖右侧滚动条」只在真机拿到 OCR 签名时启用。
# 回归子进程统一关掉这两条真机分支，绝不让测试去动用户鼠标；本套件里
# `_scroll_shop_list` 仍是逐屏逻辑的替身，断言语义不变。
mx._list_text_signature = lambda *a, **k: None
mx._scroll_list_to_edge = lambda *a, **k: False

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


def _thumb(value):
    """构造一张 32x18 的灰度缩略图（576 个相同像素）。"""
    return [value] * (32 * 18)


def test_find_after_scrolling():
    print("[1] 当前视图没有 → 往下翻页找到")
    scrolls = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    # 每次滚动后缩略图明显变化（差值远大于阈值），第 3 屏才找到
    values = iter(range(0, 200, 20))
    locate_calls = {"n": 0}

    def _fake_locate(logger, hwnd, item, scales, confidences, stop_check, label, ocr_timeout=None):
        locate_calls["n"] += 1
        return (300, 400) if locate_calls["n"] >= 3 else None

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", lambda hwnd, anchor: _thumb(next(values))), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", _fake_locate):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("翻页后找到商品", center == (300, 400), center)
    check("确实往下滚了", scrolls and scrolls[0] == mx.SCROLL_STEP, scrolls)
    check("找到后不往上滚回去（坐标要用来点击）", all(s < 0 for s in scrolls), scrolls)


def test_stop_when_list_unchanged():
    print("[2] 滚动后画面几乎没变（角标动画不该骗过熔断）→ 连续两次即停止")
    scrolls = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", lambda hwnd, anchor: _thumb(120)), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("没找到返回 None", center is None, center)
    down = [s for s in scrolls if s < 0]
    check(f"滚了 {mx.SCROLL_STATIC_STREAK} 次就熔断", len(down) == mx.SCROLL_STATIC_STREAK, scrolls)


def test_restore_top_when_not_found():
    print("[3] 翻遍仍找不到 → 滚回顶部 + 落失败证据")
    scrolls = []
    evidences = []

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        scrolls.append(amount)
        return True

    values = iter(range(0, 400, 40))

    def _thumb_next(hwnd, anchor):
        try:
            return _thumb(next(values))
        except StopIteration:
            return _thumb(399)

    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", _thumb_next), \
            mock.patch.object(mx, "_record_scroll_failure",
                              lambda logger, hwnd, label, name, scrolled, diff: evidences.append(scrolled)), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换"
        )
    check("没找到返回 None", center is None, center)
    down = [s for s in scrolls if s < 0]
    up = [s for s in scrolls if s > 0]
    check("往下翻了若干屏", len(down) >= 1, scrolls)
    check("最后滚回顶部", len(up) >= 1, scrolls)
    check("落了失败证据", len(evidences) == 1, evidences)


def test_thumb_diff():
    print("[3b] 像素差：动画噪声 < 阈值 < 真实滚动")
    check("同一帧差 0", mx._thumb_diff(_thumb(100), _thumb(100)) == 0)
    check("轻微动画差 < 阈值", mx._thumb_diff(_thumb(100), _thumb(101)) < mx.SCROLL_DIFF_THRESHOLD)
    check("真实滚动差 > 阈值", mx._thumb_diff(_thumb(20), _thumb(120)) > mx.SCROLL_DIFF_THRESHOLD)
    check("空输入返回大值", mx._thumb_diff(None, _thumb(1)) > 100)


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
    # 2026-11-04 起 _buy_items 传的是带共享 diag 的包装函数，不再是模块函数本身；
    # 是否真的转发到 _scroll_find_item 由 [7][8] 的行为用例验证。
    check("传了翻页兜底进去", calls and calls[0] is not None, calls)

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


def test_diag_flags():
    print("[7] diag 上报：翻到找到 / 翻遍没找到 / 一屏都没滚成")
    # 7a. 第 2 屏找到：effective_pages=1，无 scroll_verified_dead
    values = iter(range(0, 200, 20))
    locate_calls = {"n": 0}

    def _fake_scroll(logger, hwnd, amount, anchor_frac, stop_check=None):
        return True

    def _fake_locate(logger, hwnd, item, scales, confidences, stop_check, label, ocr_timeout=None):
        locate_calls["n"] += 1
        return (300, 400) if locate_calls["n"] >= 2 else None

    diag = {}
    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", lambda hwnd, anchor: _thumb(next(values))), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", _fake_locate):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换", diag=diag
        )
    check("找到商品", center == (300, 400), center)
    # 初始快照吃掉一个值，第 2 屏才定位到 → 两屏都算有效滚动
    check("effective_pages=2", diag.get("effective_pages") == 2, diag)
    check("找到时不标 scroll_verified_dead", "scroll_verified_dead" not in diag, diag)

    # 7b. 每屏都滚动了但翻遍没找到：effective_pages>0，dead=0（可信的"已买过"）
    values = iter(range(0, 400, 40))

    def _thumb_next(hwnd, anchor):
        try:
            return _thumb(next(values))
        except StopIteration:
            return _thumb(399)

    diag = {}
    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", _thumb_next), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换", diag=diag
        )
    check("翻遍没找到返回 None", center is None, center)
    check("翻遍时 effective_pages>0", diag.get("effective_pages", 0) > 0, diag)
    check("翻遍时不算 scroll_verified_dead", diag.get("scroll_verified_dead") == 0, diag)

    # 7c. 画面一直没变（滚轮无效）：effective_pages=0，dead=1
    diag = {}
    with mock.patch.object(mx, "_scroll_shop_list", _fake_scroll), \
            mock.patch.object(mx, "_list_thumb", lambda hwnd, anchor: _thumb(120)), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换", diag=diag
        )
    check("滚轮无效返回 None", center is None, center)
    check("滚轮无效 effective_pages=0", diag.get("effective_pages") == 0, diag)
    check("滚轮无效标 scroll_verified_dead=1", diag.get("scroll_verified_dead") == 1, diag)


def test_strict_semantics_when_scroll_dead():
    print("[8] 翻页开但一屏没滚成 → '看不到'判失败；翻遍了仍按已买过")
    # 8a. scroll_verified_dead=1 → 失败
    def _dead_finder(logger, hwnd, item, scales, confidences, stop_check, label, diag=None):
        if diag is not None:
            diag["effective_pages"] = 0
            diag["scroll_verified_dead"] = 1
        return None

    def _missing(logger, hwnd, item, confirm_template, scales, confidences,
                 stop_check, label="商店采购", scroll_finder=None):
        if scroll_finder is not None:
            scroll_finder(logger, hwnd, item, scales, confidences, stop_check, label)
            return shop.ITEM_MISSING
        return shop.ITEM_MISSING

    with mock.patch.object(mx, "_buy_once", _missing), \
            mock.patch.object(mx, "_scroll_find_item", _dead_finder):
        ok, summaries = mx._buy_items(
            _logger(), 1234, [dict(ITEM)], "confirm.png", "余烬兑换",
            [1.0], [0.8], None, scroll_search=True,
        )
    check("滚轮无效判失败", ok is False, (ok, summaries))
    check("失败原因写明不能按已购买处理",
          any("不能按已购买处理" in s for s in summaries), summaries)

    # 8b. 翻遍了（有效滚动）没找到 → 维持"已买过"成功口径
    def _alive_finder(logger, hwnd, item, scales, confidences, stop_check, label, diag=None):
        if diag is not None:
            diag["effective_pages"] = 3
            diag["scroll_verified_dead"] = 0
        return None

    with mock.patch.object(mx, "_buy_once", _missing), \
            mock.patch.object(mx, "_scroll_find_item", _alive_finder):
        ok2, summaries2 = mx._buy_items(
            _logger(), 1234, [dict(ITEM)], "confirm.png", "余烬兑换",
            [1.0], [0.8], None, scroll_search=True,
        )
    check("翻遍没找到仍按已买过（成功）", ok2 is True, (ok2, summaries2))
    check("摘要带已购买说明",
          any("按已购买处理" in s for s in summaries2), summaries2)


def main():
    test_find_after_scrolling()
    test_stop_when_list_unchanged()
    test_restore_top_when_not_found()
    test_thumb_diff()
    test_buy_items_uses_scroll_finder()
    test_step_switch()
    test_anchor_math()
    test_diag_flags()
    test_strict_semantics_when_scroll_dead()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
