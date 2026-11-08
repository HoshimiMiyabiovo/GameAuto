# -*- coding: utf-8 -*-
"""商店采购禁盲点回归（2026-11-08）。

事故：2026-11-07 异环迷迭兑换月初布局变化——质感骰子旧坐标 (0.256,0.371) 落到
第一格捏造骰子上，5 连击买光捏造骰子（0/5）+ 缀缀棉花 3 个，报告却写
「质感骰子: 5/5」假成功；三重钥匙 0 次命中被写成「3/20 按已购买处理」。
迷迭是抽卡兑换来的货币（用户 11-07 拍板「这 tm 不能盲点啊」）。

修法：
  1. `_locate_item` 废除 point_frac 盲点购买——只有模板（彩色）/OCR 商品名
     命中才准点；
  2. 两处采购收口（monthly_exchange._buy_items / zzz_monthly_shop.buy_shop_groups）
     区分「买到一半后消失 = 买满隐藏，维持已购口径」与「一件都没买到就看不到 =
     布局变化，判失败不写月度标记」；
  3. 异环配置删 item point_frac、补 ocr_text、开翻页（找商品定位也走模板/OCR）。

覆盖：
  [1] 模板未命中 + 无 ocr_text → 返回 None，即便 item 配了 point_frac 也不盲点
  [2] 模板命中 → 用模板坐标（正路径不回归）
  [3] 模板未命中 + OCR 命中商品名 → 用 OCR 坐标
  [4] monthly_exchange._buy_items：bought==0 判失败 / bought>0 后消失按已购买
  [5] flow_configs.example.json 异环段镜像断言（本地份由 suite_nte_monthly_20261101 锁）

跑法：.venv\\Scripts\\python.exe tests\\suite_shop_no_blind_buy_20261108.py
"""

import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402
import game_auto.runtime.monthly_exchange as me  # noqa: E402

LOG = logging.getLogger("no-blind-buy-test")
LOG.addHandler(logging.NullHandler())
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def test_no_blind_point_even_with_point_frac():
    print("[1] 模板/OCR 都未命中：返回 None，配了 point_frac 也不盲点")
    item = {
        "name": "质感骰子",
        "template": "assets/templates/异环-质感骰子.png",
        "color_match": True,
        "point_frac": [0.256, 0.371],   # 11-07 事故坐标——必须被无视
        "count": 5,
    }
    frac_calls = []
    with mock.patch.object(shop, "os") as mock_os, \
         mock.patch.object(shop, "_locate", return_value=None) as m_locate, \
         mock.patch.object(shop, "_locate_ocr", return_value=None) as m_ocr, \
         mock.patch.object(shop, "_frac_point",
                           side_effect=lambda *a, **k: frac_calls.append(a) or (491, 400)):
        mock_os.path.isfile.return_value = True
        got = shop._locate_item(LOG, 1, item, [1.0], [0.8], None, "迷迭兑换")
    check("定位返回 None", got is None, got)
    check("没有按 point_frac 盲点", frac_calls == [], frac_calls)
    check("模板定位尝试过", m_locate.called, None)
    check("没有 OCR 文字可兜底时不调 OCR（无 ocr_text 配置）", not m_ocr.called, None)


def test_template_hit_still_works():
    print("[2] 模板命中：用模板坐标（正路径）")
    item = {"name": "质感骰子", "template": "assets/templates/异环-质感骰子.png",
            "color_match": True, "ocr_text": "质感骰子"}
    with mock.patch.object(shop, "os") as mock_os, \
         mock.patch.object(shop, "_locate", return_value=(491, 400)) as m_locate:
        mock_os.path.isfile.return_value = True
        got = shop._locate_item(LOG, 1, item, [1.0], [0.8], None, "迷迭兑换")
    check("返回模板坐标", got == (491, 400), got)
    check("彩色匹配透传", m_locate.call_args.kwargs.get("use_color") is True, m_locate.call_args)


def test_ocr_fallback_after_template_miss():
    print("[3] 模板未命中 + OCR 命中商品名：用 OCR 坐标（第二道定位）")
    item = {"name": "三重钥匙", "template": "assets/templates/异环-三重钥匙.png",
            "color_match": True, "ocr_text": "三重钥匙"}
    with mock.patch.object(shop, "os") as mock_os, \
         mock.patch.object(shop, "_locate", return_value=None), \
         mock.patch.object(shop, "_locate_ocr", return_value=(1100, 400)) as m_ocr:
        mock_os.path.isfile.return_value = True
        got = shop._locate_item(LOG, 1, item, [1.0], [0.8], None, "迷迭兑换")
    check("返回 OCR 坐标", got == (1100, 400), got)
    check("OCR 用的是配置的商品名", "三重钥匙" in str(m_ocr.call_args), m_ocr.call_args)


def test_buy_items_missing_semantics():
    print("[4] monthly_exchange._buy_items：bought==0 判失败，bought>0 后消失按已购买")
    items = [{"name": "质感骰子", "count": 5}]
    with mock.patch.object(me, "_buy_once", return_value=me.ITEM_MISSING):
        ok, summaries = me._buy_items(LOG, 0, items, "tpl.png", "迷迭兑换", [], [], None)
    check("一件没买到判失败", ok is False, summaries)
    check("摘要写明不能按已购买处理",
          any("不能按已购买处理" in s for s in summaries), summaries)

    state = {"n": 0}

    def fake_partial(logger, hwnd, item, confirm_template, scales, confidences, stop_check,
                     label="", scroll_finder=None):
        state["n"] += 1
        return me.BOUGHT if state["n"] <= 3 else me.ITEM_MISSING

    with mock.patch.object(me, "_buy_once", side_effect=fake_partial):
        ok, summaries = me._buy_items(LOG, 0, items, "tpl.png", "迷迭兑换", [], [], None)
    check("买到一半后消失按已购买（买满隐藏口径保留）", ok is True, summaries)
    check("摘要 3/5 + 按已购买处理",
          any("3/5" in s and "按已购买处理" in s for s in summaries), summaries)


def test_example_config_mirrors_local():
    print("[5] flow_configs.example.json 异环段与本地同口径")
    cfg = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    step = next(s for s in cfg["NTE_MONTHLY"]["steps"]
                if "monthly_exchange" in str(s.get("action", "")))
    items = step.get("items") or []
    check("商品无 point_frac", all("point_frac" not in it for it in items),
          [it.get("point_frac") for it in items])
    check("商品有 ocr_text", all(str(it.get("ocr_text") or "").strip() for it in items),
          [it.get("ocr_text") for it in items])
    check("翻页开启", step.get("item_scroll_search") is True, step.get("item_scroll_search"))
    check("清单不含捏造骰子", "捏造骰子" not in [it.get("name") for it in items],
          [it.get("name") for it in items])


def main():
    test_no_blind_point_even_with_point_frac()
    test_template_hit_still_works()
    test_ocr_fallback_after_template_miss()
    test_buy_items_missing_semantics()
    test_example_config_mirrors_local()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
