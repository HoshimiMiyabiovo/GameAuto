"""2026-11-15 绝区零邦布币商店月度自动化回归。

2026-11-15 用户拍板：信源清单→邦布币页签每月把邦布券买满（月剩余×5，50% 折扣
20 邦布币/张）；特邀帮手礼盒/浓缩型冷却液/邦布系统控件/玛瑟尔改装原件不买，
四件本页独占商品作页签特征指纹（tab_content_markers）。

为什么是独立的第三条月流程：邦布券买满后卡片整卡消失（同信号残响母带口径），
而 2026-11-08 起的采购语义是「一件没买到就看不到 = 布局变化，失败不写标记」。
若塞进 ZZZ_MONTHLY 共用流程+标记，任一步失败后的下批重跑会因先跑的那步商品
消失而永久失败，邦布币步永远轮不到——与 11-06 后勤拆分同因。故独立 flow_key、
独立 monthly_flow:ZZZ_BANGBOO_MONTHLY 门控、独立 runner（_EXTRA_MONTHLY_FLOWS
第 2 条），与主流程/后勤流程互不阻断。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_bangboo_monthly_20261115.py
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.monthly_exchange as mx  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402

FAILURES = []
LOG = logging.getLogger("zzz_bangboo_monthly_test")
LOG.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def cfgs():
    return [json.loads((PROJECT_ROOT / p).read_text(encoding="utf-8"))
            for p in ("flow_configs.json", "flow_configs.example.json")]


def bangboo_step():
    flow = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    return flow["ZZZ_BANGBOO_MONTHLY"]["steps"][1]


def test_config():
    print("[1] 配置：独立流程、只买邦布券、页签特征指纹")
    a, b = cfgs()
    flow = a["ZZZ_BANGBOO_MONTHLY"]
    check("两份配置 ZZZ_BANGBOO_MONTHLY 一致", flow == b["ZZZ_BANGBOO_MONTHLY"])
    check("enabled", flow.get("enabled") is True)
    steps = flow["steps"]
    check("两步：场景校正 + 邦布币兑换",
          len(steps) == 2
          and steps[0].get("action") == "zzz_enter_main"
          and steps[1].get("action") == "monthly_exchange",
          [s.get("name") for s in steps])
    step = bangboo_step()
    check("防假成功签名=月剩余",
          step.get("purchase_verify", {}).get("labels") == ["月剩余"],
          step.get("purchase_verify"))
    check("一屏清单不翻页（邦布券第一格）", step.get("item_scroll_search") is False)
    check("商城入口/左栏与 ZZZ_MONTHLY 同源",
          step.get("shop_entry_point_frac") == [0.7021, 0.8796]
          and step.get("nav_text") == "信源清单",
          (step.get("shop_entry_point_frac"), step.get("nav_text")))
    check("页签三重定位：文字 + 实测坐标 + 相邻中点兜底",
          step.get("tab_text") == "邦布币"
          and step.get("tab_point_frac") == [0.8153, 0.1483]
          and step.get("tab_between_texts") == ["信号残响", "母带兑换"],
          (step.get("tab_text"), step.get("tab_point_frac"), step.get("tab_between_texts")))
    markers = step.get("tab_content_markers")
    check("特征指纹=4 件本页独占商品（用户 11-15 拍板）",
          markers == ["特邀帮手", "浓缩型冷却液", "邦布系统控件", "玛瑟尔改装原件"],
          markers)
    check("特征词不能是整卡名（「特邀帮手」礼盒带书名号，OCR 子串会断开）",
          "特邀帮手礼盒" not in markers)
    forbidden = step.get("forbidden_texts")
    check("错页拦截=邻页独占商品（残响储值电卡 + 余波母带/S 引擎）",
          {"储值电卡", "原装母带", "加密母带", "硫磺石"} <= set(forbidden),
          forbidden)
    items = step.get("items")
    check("采购清单只有邦布券×5（礼盒/冷却液/控件/玛瑟尔不买）",
          len(items) == 1 and items[0]["name"] == "邦布券" and items[0]["count"] == 5,
          items)
    tpl = PROJECT_ROOT / "assets" / "templates" / "绝区零-商店-邦布币页-邦布券.png"
    old = PROJECT_ROOT / "assets" / "templates" / "绝区零-商店-邦布券.png"
    check("邦布币页模板入库，且与物流店协会物资页旧模板不同源",
          tpl.is_file() and old.is_file() and tpl.read_bytes() != old.read_bytes(),
          (tpl.is_file(), old.is_file()))
    check("确认/退出模板复用现有文件",
          (PROJECT_ROOT / "assets" / "templates" / "绝区零-商店-确认.png").is_file()
          and (PROJECT_ROOT / "assets" / "templates" / "绝区零-主菜单按钮.png").is_file())
    check("跑完关游戏（ZZZ 月流程槽最后一个任务）",
          flow.get("close_process_on_finish") is True)
    check("门控键独立（三条 ZZZ 月流程互不撞）",
          btr.monthly_flow_meta_key("ZZZ_BANGBOO_MONTHLY") == "monthly_flow:ZZZ_BANGBOO_MONTHLY"
          and len({btr.monthly_flow_meta_key(k) for k in
                   ("ZZZ_MONTHLY", "ZZZ_LOGISTICS_MONTHLY", "ZZZ_BANGBOO_MONTHLY")}) == 3)


def test_wiring():
    print("[2] 调度接线：附加流程第 2 条、独立 runner")
    extras = tbr._EXTRA_MONTHLY_FLOWS.get("ZenlessZoneZero", ())
    check("邦布币挂在绝区零附加流程第 2 条",
          len(extras) == 2 and extras[-1] == ("ZZZ_BANGBOO_MONTHLY", "绝区零邦布币商店"),
          extras)
    runner = tbr._EXTRA_MONTHLY_RUNNERS.get("ZZZ_BANGBOO_MONTHLY")
    check("runner 指向 btr.run_zzz_bangboo_monthly_background",
          runner is btr.run_zzz_bangboo_monthly_background, runner)
    with mock.patch.object(btr, "_run_monthly_flow", return_value=(True, "ok")) as run:
        ok, _ = btr.run_zzz_bangboo_monthly_background(LOG)
    check("runner 用独立 flow_key 调 _run_monthly_flow",
          ok and run.call_args[0][1] == "ZZZ_BANGBOO_MONTHLY", run.call_args)
    check("主映射不受影响（其它游戏零影响）",
          len(tbr._MONTHLY_FLOW_BY_APP) == 4
          and "ZZZ_BANGBOO_MONTHLY" not in tbr._MONTHLY_RUNNERS)


def test_rebuy_semantics():
    """邦布券月度重跑语义（桩 _buy_once，不碰真窗口）。

    - 点商品无弹窗 = 已达上限（月初重跑撞上月内已买满的主场景），不算失败；
    - 买到一半后商品消失 = 买满隐藏，按已购买处理，不算失败；
    - 一件没买到就看不到 = 布局变化/加载异常，必须失败（不写月度标记）。
    前两条是买满消失口径下的正常重跑路径，第三条保住诚实失败。
    """
    print("[3] 邦布券重跑语义（买满消失 / 部分购买 / 未找到）")
    items = [{"name": "邦布券", "count": 5, "ocr_text": "邦布券"}]

    def run_buy(results):
        calls = iter(results)

        def fake(logger, hwnd, item, confirm_template, scales, confidences, stop_check, **kw):
            return next(calls)

        with mock.patch.object(mx, "_buy_once", side_effect=fake):
            return mx._buy_items(
                LOG, 0, items, "unused_confirm.png", "邦布币兑换", [], [], None,
                scroll_search=False, step=None,
            )

    ok, summaries = run_buy([mx.NO_POPUP])
    check("点击无弹窗 → 已达上限口径，整步成功",
          ok is True and len(summaries) == 1 and "邦布券" in summaries[0] and "0/5" in summaries[0],
          summaries)

    ok, summaries = run_buy([mx.BOUGHT, mx.BOUGHT, mx.ITEM_MISSING])
    check("买到 2 张后卡片消失 → 按已购买处理，整步成功",
          ok is True and "2/5" in summaries[0], summaries)

    ok, summaries = run_buy([mx.ITEM_MISSING])
    check("一件没买到就看不到 → 失败（不写月度标记）",
          ok is False and "0/5" in summaries[0], summaries)

    ok, summaries = run_buy([mx.STOPPED])
    check("用户停止 → 整步失败", ok is False, summaries)


def main():
    test_config()
    test_wiring()
    test_rebuy_semantics()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
