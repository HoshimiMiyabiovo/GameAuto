"""白荆回廊「有猫兑换」按清单逐件购买回归（2026-11-01）。

背景：用户 2026-11-01 确认「小猫礼品」每月只换两张抽卡券（同调线索 / 一刹闪回）
+ 心迹，两张券每月库存各 5，选择「买满库存」。实现上新增 items[].qty：逐件走
「点商品 → 购买 → 弹窗确认」，不依赖本页签尚未实测的「最大」按钮。

覆盖：
  1. qty 解析：缺省 1 / 字符串数字 / 非法值 / 0 与负数
  2. 逐件购买循环：全买到 / 部分买到（库存不足）/ 全部已买完 / 未找到 / 窗口丢失 / 用户停止
  3. 默认清单（settings/defaults.py）静态断言
  4. run 入口把清单（含 qty）原样交给购买循环
  5. 有新一期参考图时跑真实 OCR（缺图或缺引擎则 SKIP）

参考图：data/artifacts/ref_baijing_cat_exchange_20261101.png（用户 2026-11-01 截图，
仓库 .gitignore 忽略 data/，本机存在时才跑 OCR 用例）。

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_cat_exchange_qty_20261101.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_cat_exchange as ce  # noqa: E402
import game_auto.settings.manager as manager  # noqa: E402
from game_auto.settings.defaults import DEFAULT_BAIJING_CAT_EXCHANGE  # noqa: E402

REF_IMG = PROJECT_ROOT / "data" / "artifacts" / "ref_baijing_cat_exchange_20261101.png"

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    import logging

    logger = logging.getLogger("test_baijing_cat_exchange_qty")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def test_qty_parsing():
    print("[1] qty 解析")
    orig_load = manager.load
    try:
        manager.load = lambda force=False: {
            "baijing": {
                "cat_exchange": {
                    "tab": "小猫礼品",
                    "items": [
                        {"name": "A", "qty": 5},
                        {"name": "B", "qty": "3"},
                        {"name": "C"},
                        {"name": "D", "qty": "x"},
                        {"name": "E", "qty": 0},
                        {"name": "F", "qty": -2},
                        "G",
                    ],
                }
            }
        }
        plan = ce.load_cat_exchange_plan(_logger())
        qty = {i["name"]: i["qty"] for i in plan["items"]}
        check("整数 qty 直接采用", qty.get("A") == 5, qty)
        check("字符串 qty 转 int", qty.get("B") == 3, qty)
        check("缺省 qty = 1", qty.get("C") == 1 and qty.get("G") == 1, qty)
        check("非法 qty 降级为 1", qty.get("D") == 1, qty)
        check("0 / 负数 qty 兜到 1", qty.get("E") == 1 and qty.get("F") == 1, qty)
        check("页签继承到每一项", all(i["tab"] == "小猫礼品" for i in plan["items"]), plan["items"])
    finally:
        manager.load = orig_load


def _run(items, states, stop_check=None):
    """跑 buy_cat_exchange_items，mock 掉窗口/单件购买/页签切换。

    states 里的元素按调用顺序返回；用完后默认一直 purchased。
    """
    seq = list(states)
    bought_names = []
    switched = []

    def fake_buy(logger, hwnd, item, stop_check=None):
        state = seq.pop(0) if seq else "purchased"
        if state == "purchased":
            bought_names.append(item["name"])
        return {"state": state, "name": item["name"], "price": None, "detail": state}

    orig_buy = ce._buy_one_cat_item
    orig_ensure = ce._ensure_window
    orig_switch = ce.switch_cat_tab
    ce._buy_one_cat_item = fake_buy
    ce._ensure_window = lambda logger, hwnd: hwnd
    ce.switch_cat_tab = lambda logger, hwnd, tab, stop_check=None: switched.append(tab) or True
    try:
        res = ce.buy_cat_exchange_items(_logger(), 123, items, stop_check=stop_check)
        return res, bought_names, switched
    finally:
        ce._buy_one_cat_item = orig_buy
        ce._ensure_window = orig_ensure
        ce.switch_cat_tab = orig_switch


def _item(name, qty=1, tab=""):
    return {"name": name, "qty": qty, "max_price": None, "max_qty": False, "tab": tab}


def test_buy_loop():
    print("[2] 逐件购买循环")
    res, bought, _ = _run([_item("同调线索", 5), _item("心迹", 1)], [])
    check("全部买到 → success", res["status"] == "success", res)
    check("purchased 按件计数（5+1=6）", res["purchased"] == 6, res)
    check("qty>1 记 ×N", "同调线索×5" in res["items"] and "心迹" in res["items"], res["items"])
    check("detail 写明件数", "已购买 6 件" in res["detail"], res["detail"])
    check("实际下单次数 = qty 总和", bought == ["同调线索"] * 5 + ["心迹"], bought)

    res, bought, _ = _run(
        [_item("同调线索", 5)], ["purchased", "purchased", "purchased", "sold_out"]
    )
    check("部分买到 → success", res["status"] == "success", res)
    check("部分买到件数正确", res["purchased"] == 3 and res["items"] == ["同调线索×3"], res)
    check("差额进 shortfall", res["shortfall"] and "3/5" in res["shortfall"][0], res["shortfall"])
    check("detail 标明未买满", "未买满" in res["detail"], res["detail"])
    check("库存不足后停止下单", bought == ["同调线索"] * 3, bought)

    res, bought, _ = _run([_item("同调线索", 5)], ["sold_out"])
    check("全部已买完 → already", res["status"] == "already", res)
    check("已买完计入 missing", any("已买完" in m for m in res["missing"]), res["missing"])
    check("没有下单", bought == [], bought)

    res, _, _ = _run([_item("同调线索", 5)], ["missing"])
    check("未找到 → failed（不冒充 already）", res["status"] == "failed", res)
    check("未找到计入 missing", any("未找到" in m for m in res["missing"]), res["missing"])

    orig_ensure = ce._ensure_window
    ce._ensure_window = lambda logger, hwnd: None
    try:
        res = ce.buy_cat_exchange_items(_logger(), 123, [_item("同调线索", 5)])
        check("窗口丢失 → failed", res["status"] == "failed", res)
        check("窗口丢失写明原因", any("窗口丢失" in f for f in res["failed"]), res["failed"])
    finally:
        ce._ensure_window = orig_ensure

    calls = {"n": 0}

    def _stop_after_three():
        calls["n"] += 1
        return calls["n"] >= 5

    res, bought, _ = _run([_item("同调线索", 5)], [], stop_check=_stop_after_three)
    check("中途停止 → detail 用户停止", res["detail"] == "用户停止", res)
    check("停止前买到的件数保留", res["purchased"] == 3 and bought == ["同调线索"] * 3, res)
    check("停止不写 success（避免月度 meta 记完成）", res["status"] == "skipped", res)

    res, _, switched = _run(
        [_item("同调线索", 5, tab="小猫礼品"), _item("心迹", 1, tab="小猫礼品")], []
    )
    check("同页签只切一次", switched == ["小猫礼品"], switched)
    res, _, switched = _run(
        [_item("同调线索", 5, tab="小猫礼品"), _item("心迹", 1, tab="大猫兑换")], []
    )
    check("换页签会再切", switched == ["小猫礼品", "大猫兑换"], switched)

    res, _, _ = _run([], [])
    check("空清单 → skipped", res["status"] == "skipped", res)


def test_defaults():
    print("[3] 默认清单（2026-11-01 用户确认）")
    check("enabled=True", DEFAULT_BAIJING_CAT_EXCHANGE.get("enabled") is True)
    check("默认页签 小猫礼品", DEFAULT_BAIJING_CAT_EXCHANGE.get("tab") == "小猫礼品")
    items = DEFAULT_BAIJING_CAT_EXCHANGE.get("items") or []
    names = [i.get("name") for i in items]
    check("只列三件：同调线索 / 一刹闪回 / 心迹",
          names == ["同调线索", "一刹闪回", "心迹"], names)
    check("两张券买满 5 件、心迹 1 件",
          [i.get("qty") for i in items] == [5, 5, 1], items)
    check("没有多余商品（别的都不换）", len(items) == 3, items)


def test_run_wiring():
    print("[4] run 入口把清单（含 qty）交给购买循环")
    orig = {
        name: getattr(ce, name)
        for name in (
            "detect_baijing_main_ui",
            "open_cat_exchange",
            "switch_cat_tab",
            "_capture_daily_list_snapshot",
            "_leave_retail_shop",
            "_ensure_window",
            "buy_cat_exchange_items",
        )
    }
    orig_load = manager.load
    captured = {}
    try:
        manager.load = lambda force=False: {
            "baijing": {
                "cat_exchange": {
                    "enabled": True,
                    "tab": "小猫礼品",
                    "items": [
                        {"name": "同调线索", "qty": 5},
                        {"name": "一刹闪回", "qty": 5},
                        {"name": "心迹", "qty": 1},
                    ],
                }
            }
        }
        ce.detect_baijing_main_ui = lambda logger, hwnd, budget_sec=3.0: (True, "mock")
        ce.open_cat_exchange = lambda logger, hwnd, stop_check=None: True
        ce.switch_cat_tab = lambda logger, hwnd, tab, stop_check=None: True
        ce._capture_daily_list_snapshot = lambda logger, hwnd, prefix="": None
        ce._leave_retail_shop = lambda logger, hwnd, stop_check=None: True
        ce._ensure_window = lambda logger, hwnd: hwnd

        def fake_buy(logger, hwnd, items, stop_check=None):
            captured["items"] = items
            return {"status": "success", "purchased": 11, "items": ["mock"], "detail": "mock"}

        ce.buy_cat_exchange_items = fake_buy
        res = ce.run_cat_exchange_monthly(_logger(), hwnd=123)
        items = captured.get("items") or []
        check("进入购买循环", len(items) == 3, items)
        check("qty 原样透传", [i.get("qty") for i in items] == [5, 5, 1], items)
        check("页签继承 小猫礼品", all(i.get("tab") == "小猫礼品" for i in items), items)
        check("返回购买结果", res.get("status") == "success", res)
    finally:
        for name, fn in orig.items():
            setattr(ce, name, fn)
        manager.load = orig_load


def test_reference_ocr():
    print("[5] 2026-11-01 参考图 OCR")
    if not REF_IMG.exists():
        print(f"  SKIP 参考图不存在: {REF_IMG}")
        return
    try:
        import numpy as np
        from PIL import Image

        from game_auto.runtime.ocr_click_tester import _get_rapidocr_engine
    except Exception as e:  # noqa: BLE001 - 缺依赖就跳过，不误报失败
        print(f"  SKIP 依赖不可用: {e}")
        return
    engine = _get_rapidocr_engine()
    if not engine:
        print("  SKIP RapidOCR 不可用")
        return

    img = Image.open(REF_IMG).convert("RGB")
    w, h = img.size
    result, _ = engine(np.asarray(img))
    rows = []
    for box, text, confidence in result or []:
        x1 = min(p[0] for p in box)
        y1 = min(p[1] for p in box)
        x2 = max(p[0] for p in box)
        y2 = max(p[1] for p in box)
        rows.append(
            {
                "text": text,
                "confidence": float(confidence),
                "box": (int(x1), int(y1), int(x2), int(y2)),
                "center": (int((x1 + x2) // 2), int((y1 + y2) // 2)),
            }
        )
    fx0, fy0, fx1, fy1 = ce.CAT_GRID_REGION
    grid = (int(fx0 * w), int(fy0 * h), int((fx1 - fx0) * w), int((fy1 - fy0) * h))
    win = (0, 0, w, h)

    for name in ("同调线索", "一刹闪回", "心迹"):
        m = ce._find_name_match(rows, name, grid)
        check(f"商品名「{name}」命中", m is not None, name)
        if not m:
            continue
        hit = ce._find_price_hit(rows, m["center"], win)
        check(f"「{name}」价格数字可读", hit is not None, "")
        sold = ce._find_sold_out_near(rows, m["center"], win)
        check(f"「{name}」在售（不误判买完）", sold is None, sold)

    prices = {}
    for name in ("同调线索", "一刹闪回", "心迹"):
        m = ce._find_name_match(rows, name, grid)
        hit = ce._find_price_hit(rows, m["center"], win) if m else None
        prices[name] = ce._parse_price(hit["text"]) if hit else None
    check("价格 = 40 / 40 / 160",
          (prices["同调线索"], prices["一刹闪回"], prices["心迹"]) == (40, 40, 160), prices)
    check("页签区域常量覆盖页签行", ce.CAT_TAB_REGION[1] < 0.24 and ce.CAT_TAB_REGION[3] > 0.18,
          ce.CAT_TAB_REGION)


def main():
    test_qty_parsing()
    test_buy_loop()
    test_defaults()
    test_run_wiring()
    test_reference_ocr()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
