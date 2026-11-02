# -*- coding: utf-8 -*-
"""每月商店自动调度 + 鸣潮活跃度重查 回归（2026-11-01）。

背景（用户投诉）：
1. 崩铁/绝区零/原神的每月商店只是独立任务，每日一条龙不会顺带跑，
   月初不手动勾就一直"本月没跑"；白荆/暖暖却是内置在每日流程里的。
2. 鸣潮脚本刚退游戏就查库街区 API，活跃度常返回 0/100，落库标 stale，
   卡片显示"未刷新"；实测十几分钟后同一接口已是 100/100——查太快了。

本套件锁定：
1. 每日批次对 HSR/ZZZ/GENSHIN 会自动补跑月商店，未买才跑、买过零成本；
2. 月商店失败/异常不拖垮当天一条龙；
3. 鸣潮活跃度 0 时等待重查，拿到非 0 就采用；始终 0 才交给上层标 stale；
4. 重查失败不丢上一次可用结果。

跑法：.venv\\Scripts\\python.exe tests\\suite_monthly_auto_schedule_20261101.py
"""

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.clients.daily_checker as dc  # noqa: E402
import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.monthly_exchange as me  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("test.monthly.auto_schedule")
LOGGER.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeController:
    def __init__(self, stop=False):
        self.logger = LOGGER
        self.stop_current_task = stop


@contextmanager
def _no_maintenance():
    """月商店用例固定"无维护"，避免真实公告让补跑被跳过。"""
    with patch(
        "game_auto.services.game_update_calendar.get_maintenance_state",
        return_value=None,
    ):
        yield


def _wuwa_status(current, total=100, errors=None):
    return dc.GameDailyStatus(
        game_key="wuwa",
        game_name="鸣潮",
        uid="1",
        nickname="test",
        is_daily_completed=current >= total,
        tasks=[dc.DailyTaskItem(name="今日活跃", current=current, total=total, is_completed=current >= total)],
        errors=list(errors or []),
    )


# ---------------------------------------------------------------------------
# [1] 月商店自动调度
# ---------------------------------------------------------------------------


def test_monthly_mapping():
    print("[1] 游戏 → 每月流程映射")
    expect = {
        "HonkaiStarRail": "HSR_MONTHLY",
        "ZenlessZoneZero": "ZZZ_MONTHLY",
        "GenshinImpact": "GENSHIN_MONTHLY",
        "NevernessToEverness": "NTE_MONTHLY",
    }
    for app_key, flow_key in expect.items():
        entry = tbr._MONTHLY_FLOW_BY_APP.get(app_key)
        check(f"{app_key} 映射到 {flow_key}", entry is not None and entry[0] == flow_key, entry)
    check("覆盖四个游戏", len(tbr._MONTHLY_FLOW_BY_APP) == len(expect), tbr._MONTHLY_FLOW_BY_APP)
    check("每个流程都有执行入口",
          set(tbr._MONTHLY_RUNNERS) == {v[0] for v in tbr._MONTHLY_FLOW_BY_APP.values()},
          sorted(tbr._MONTHLY_RUNNERS))


def test_monthly_done_skip():
    print("[2] 本月已买 → 不重复启动")
    calls = []
    with patch.object(tbr, "is_monthly_flow_done", return_value=True), \
         patch.dict(tbr._MONTHLY_RUNNERS, {"HSR_MONTHLY": lambda *a, **k: calls.append(1) or (True, "x")}):
        out = tbr._run_pending_monthly_flow(FakeController(), "HonkaiStarRail")
    check("已完成返回 None", out is None, out)
    check("runner 未被调用", calls == [], calls)


def test_monthly_pending_runs():
    print("[3] 本月未买 → 自动补跑并对号入座")
    # 必须 patch _MONTHLY_RUNNERS 这个字典：执行入口是**模块加载时绑定的函数对象**，
    # patch 模块属性（patch.object(tbr, "run_xxx_background")）对字典里的引用无效，
    # 会真的去启动游戏、清理进程（2026-11-01 测试误触，别再犯）。
    cases = [
        ("HonkaiStarRail", "HSR_MONTHLY"),
        ("ZenlessZoneZero", "ZZZ_MONTHLY"),
        ("GenshinImpact", "GENSHIN_MONTHLY"),
        ("NevernessToEverness", "NTE_MONTHLY"),
    ]
    for app_key, flow_key in cases:
        calls = []

        def fake_runner(logger, _fk=flow_key):
            calls.append(_fk)
            return True, "已购买"

        with _no_maintenance(), \
             patch.object(tbr, "is_monthly_flow_done", return_value=False), \
             patch.dict(tbr._MONTHLY_RUNNERS, {flow_key: fake_runner}):
            out = tbr._run_pending_monthly_flow(FakeController(), app_key)
        check(f"{app_key} 调到 {flow_key}", calls == [flow_key], calls)
        check(f"{app_key} 报告 ✅", out is not None and out[1] and "✅" in out[0], out)


def test_monthly_failure_not_fatal():
    print("[4] 月商店失败/异常不拖垮一条龙")
    with _no_maintenance(), \
         patch.object(tbr, "is_monthly_flow_done", return_value=False), \
         patch.dict(tbr._MONTHLY_RUNNERS, {"ZZZ_MONTHLY": lambda *a, **k: (False, "❌ 步骤失败")}):
        out = tbr._run_pending_monthly_flow(FakeController(), "ZenlessZoneZero")
    check("失败返回 ok=False", out is not None and out[1] is False, out)
    check("失败行带 ❌", out is not None and "❌" in out[0], out)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    with _no_maintenance(), \
         patch.object(tbr, "is_monthly_flow_done", return_value=False), \
         patch.dict(tbr._MONTHLY_RUNNERS, {"GENSHIN_MONTHLY": _boom}):
        out = tbr._run_pending_monthly_flow(FakeController(), "GenshinImpact")
    check("异常被吞成失败行", out is not None and out[1] is False and "boom" in out[0], out)


def test_monthly_ignores_other_apps_and_stop():
    print("[5] 非目标游戏 / 用户停止 不触发")
    for app_key in ("BaiJing", "WutheringWaves", "WoolFlow", "CJFlow", "HsrMonthlyFlow"):
        with patch.object(tbr, "is_monthly_flow_done", return_value=False):
            check(f"{app_key} 不触发月商店", tbr._run_pending_monthly_flow(FakeController(), app_key) is None)
    with patch.object(tbr, "is_monthly_flow_done", return_value=False):
        check(
            "已请求停止时不启动",
            tbr._run_pending_monthly_flow(FakeController(stop=True), "HonkaiStarRail") is None,
        )


def test_monthly_maintenance_skip():
    print("[5b] 维护窗口内顺延")
    with patch.object(tbr, "is_monthly_flow_done", return_value=False), \
         patch("game_auto.services.game_update_calendar.get_maintenance_state",
               return_value={"version": "9.9"}):
        out = tbr._run_pending_monthly_flow(FakeController(), "HonkaiStarRail")
    check("维护中不启动客户端", out is None, out)


def test_monthly_report_helper():
    print("[6] 报告/警告写入")
    with _no_maintenance(), \
         patch.object(tbr, "is_monthly_flow_done", return_value=False), \
         patch.dict(tbr._MONTHLY_RUNNERS, {"HSR_MONTHLY": lambda *a, **k: (False, "❌ 未找到商店")}):
        report, warnings = [], []
        tbr._append_monthly_report(FakeController(), "HonkaiStarRail", report, warnings)
    check("report 有行", len(report) == 1, report)
    check("失败进 warnings", len(warnings) == 1 and warnings[0] == report[0], warnings)


def test_batch_wiring():
    print("[7] 批次接线（静态）")
    src = Path(tbr.__file__).read_text(encoding="utf-8")
    call = "_append_monthly_report(controller, app_key, report, warnings)"
    # 2026-11-02 起有 3 处调用：跳过分支、正常分支、批次收尾补漏
    check("_append_monthly_report 调用点 3 处（跳过分支 + 正常分支 + 收尾补漏）",
          src.count(call) == 3,
          src.count(call))
    idx_skip = src.find("今日已完成，跳过")
    idx_calls = [i for i in range(len(src)) if src.startswith(call, i)]
    check("跳过分支与正常分支两处调用都在「今日已完成」之后",
          sum(1 for i in idx_calls if i > idx_skip) >= 2,
          (idx_skip, idx_calls))
    check("月商店排在日常执行之后",
          src.find("result = _run_single_task(") < src.rfind(call),
          True)
    check("批次收尾调用补漏", "_sweep_pending_monthly_flows(controller, report, warnings)" in src)


def test_sweep_runs_monthly_for_apps_not_in_batch():
    print("[7b] 收尾补漏：当天没进批次的游戏也要补跑月商店")
    ran = []

    def _fake_runner(label):
        def _fn(*a, **k):
            ran.append(label)
            return True, "✅ 已买"
        return _fn

    fake_runners = {
        flow: _fake_runner(flow) for flow, _label in tbr._MONTHLY_FLOW_BY_APP.values()
    }
    saved = set(tbr._MONTHLY_CHECKED)
    try:
        tbr._MONTHLY_CHECKED.clear()
        with _no_maintenance(), \
             patch.object(tbr, "is_monthly_flow_done", return_value=False), \
             patch.dict(tbr._MONTHLY_RUNNERS, fake_runners):
            report, warnings = [], []
            tbr._sweep_pending_monthly_flows(FakeController(), report, warnings)
        check("四个游戏的月商店都补跑了", len(ran) == 4, ran)
        check("报告里有补跑行", len(report) == 4, report)
        check("全部成功不进 warnings", not warnings, warnings)

        # 已经检查过的游戏不重复跑（避免同一批次重复启动客户端）
        ran.clear()
        tbr._MONTHLY_CHECKED.clear()
        tbr._MONTHLY_CHECKED.add("GenshinImpact")
        with _no_maintenance(), \
             patch.object(tbr, "is_monthly_flow_done", return_value=False), \
             patch.dict(tbr._MONTHLY_RUNNERS, fake_runners):
            report, warnings = [], []
            tbr._sweep_pending_monthly_flows(FakeController(), report, warnings)
        check("已检查过的游戏不重复跑", "GENSHIN_MONTHLY" not in ran, ran)
        check("其余游戏照常补跑", len(ran) == 3, ran)

        # 本月已买过 → 不启动客户端
        ran.clear()
        tbr._MONTHLY_CHECKED.clear()
        with _no_maintenance(), \
             patch.object(tbr, "is_monthly_flow_done", return_value=True), \
             patch.dict(tbr._MONTHLY_RUNNERS, fake_runners):
            report, warnings = [], []
            tbr._sweep_pending_monthly_flows(FakeController(), report, warnings)
        check("本月已买过 → 一个都不启动", not ran, ran)
        check("已买过不写报告", not report, report)
    finally:
        tbr._MONTHLY_CHECKED.clear()
        tbr._MONTHLY_CHECKED.update(saved)


def test_monthly_meta_key_shared():
    print("[8] 月度标记口径唯一")
    check("meta key 格式", btr.monthly_flow_meta_key("HSR_MONTHLY") == "monthly_flow:HSR_MONTHLY")
    src = Path(btr.__file__).read_text(encoding="utf-8")
    check("_run_monthly_flow 复用同一 key 函数", src.count("monthly_flow_meta_key(flow_key)") >= 1)
    # 硬编码拼接只允许留在 monthly_flow_meta_key 内部这一处，别处一律走函数
    check("硬编码拼 key 仅剩定义处一处", src.count('f"monthly_flow:{flow_key}"') == 1,
          src.count('f"monthly_flow:{flow_key}"'))


def test_standalone_tasks_removed():
    print("[8b] 每月独立任务已删除")
    removed = ("HsrMonthlyFlow", "ZzzMonthlyFlow", "GenshinMonthlyFlow")
    check("catalog 不再定义", not any(k in catalog.STANDALONE_TASK_APPS for k in removed),
          list(catalog.STANDALONE_TASK_APPS))
    check("STANDALONE_TASK_ORDER 不再含", not any(k in catalog.STANDALONE_TASK_ORDER for k in removed),
          catalog.STANDALONE_TASK_ORDER)
    check("REMOVED_TASK_KEYS 记录三个 key", set(removed) == set(catalog.REMOVED_TASK_KEYS),
          catalog.REMOVED_TASK_KEYS)
    order, apps = catalog.merge_launch_apps(["GenshinImpact", "HsrMonthlyFlow", "ZzzMonthlyFlow"], {})
    check("老配置残留 key 被过滤",
          "HsrMonthlyFlow" not in order and "ZzzMonthlyFlow" not in order and order[0] == "GenshinImpact",
          order)
    check("独立任务仍按 catalog 追加", len(order) == 1 + len(catalog.STANDALONE_TASK_APPS), order)
    check("不再注入幽灵任务", not any(k in apps for k in removed), list(apps))
    check("is_standalone_task 对已删任务返回 False",
          not any(catalog.is_standalone_task(k) for k in removed))
    batch_src = Path(tbr.__file__).read_text(encoding="utf-8")
    check("批次不再按 app_key 分派每月任务",
          not any(f'app_key == "{k}"' in batch_src for k in removed))


def test_missing_item_treated_as_bought():
    print("[8c] 商店里看不到商品 → 视为本月已买过")
    items = [{"name": "星轨专票", "count": 5}, {"name": "星轨通票", "count": 5}]

    def fake_buy(logger, hwnd, item, confirm_template, scales, confidences, stop_check,
                 label="", scroll_finder=None):
        if item["name"] == "星轨专票":
            return me.ITEM_MISSING
        return me.BOUGHT

    with patch.object(me, "_buy_once", side_effect=fake_buy):
        ok, summaries = me._buy_items(LOGGER, 0, items, "tpl.png", "余烬兑换", [], [], None)
    check("部分看不到不计失败", ok is True, summaries)
    check("摘要写明按已购买处理", any("按已购买处理" in s for s in summaries), summaries)
    check("其余商品照常买满", any("星轨通票: 5/5" in s for s in summaries), summaries)

    with patch.object(me, "_buy_once", return_value=me.ITEM_MISSING):
        ok, summaries = me._buy_items(LOGGER, 0, items, "tpl.png", "余烬兑换", [], [], None)
    check("整张清单都看不到也按已买处理", ok is True, summaries)

    with patch.object(me, "_buy_once", return_value=me.CONFIRM_GONE_FAIL):
        ok, _ = me._buy_items(LOGGER, 0, items, "tpl.png", "余烬兑换", [], [], None)
    check("确认后弹窗不关仍算失败", ok is False)


def test_shop_shared_already_reasons():
    print("[8d] 两处采购共用「已买过」口径")
    import game_auto.runtime.zzz_monthly_shop as shop

    check("常量在后勤商店定义", hasattr(shop, "ALREADY_BOUGHT_REASONS"))
    check("monthly_exchange 复用同一常量", me.ALREADY_BOUGHT_REASONS is shop.ALREADY_BOUGHT_REASONS)
    check("含「看不到商品」口径",
          any("没看到" in r for r in shop.ALREADY_BOUGHT_REASONS), shop.ALREADY_BOUGHT_REASONS)
    check("含「已达上限」口径",
          any("已达上限" in r for r in shop.ALREADY_BOUGHT_REASONS), shop.ALREADY_BOUGHT_REASONS)


def test_shop_evidence_capture():
    print("[8e] 采购截图证据（复核用）")
    import json
    import shutil
    import tempfile

    from PIL import Image

    import game_auto.runtime.monthly_shop_evidence as ev

    tmp = Path(tempfile.mkdtemp(prefix="shop_ev_"))
    try:
        with patch.object(ev, "BASE_DIR", tmp), \
             patch.object(ev, "MONTHLY_SHOP_EVIDENCE_DIR", tmp), \
             patch.object(ev, "_window_region", return_value=(0, 0, 1920, 1080)), \
             patch("game_auto.runtime.ocr_click_tester.grab_screen",
                   return_value=Image.new("RGB", (1920, 1080), (30, 30, 30))):
            rel = ev.capture_shop_evidence(
                LOGGER, 12345, flow_label="余烬兑换", stage="采购前",
                summary="星轨专票: 0/5（页面上没看到该商品，按已购买处理）",
            )
        check("返回相对路径", bool(rel) and "余烬兑换" in rel and "采购前" in rel, rel)
        saved = tmp / rel
        check("文件已落盘", saved.is_file(), saved)
        with Image.open(saved) as img:
            check("压到 960 宽", img.width == 960, img.width)
            check("是 JPEG", img.format == "JPEG", img.format)

        index = tmp / ev.INDEX_FILENAME
        check("index.jsonl 已写", index.is_file(), index)
        if index.is_file():
            row = json.loads(index.read_text(encoding="utf-8").strip().splitlines()[-1])
            check("索引含流程", row.get("flow") == "余烬兑换", row)
            check("索引含阶段", row.get("stage") == "采购前", row)
            check("索引含摘要", "按已购买处理" in (row.get("summary") or ""), row)

        with patch.object(ev, "BASE_DIR", tmp), \
             patch.object(ev, "MONTHLY_SHOP_EVIDENCE_DIR", tmp), \
             patch.object(ev, "_window_region", return_value=None):
            empty = ev.capture_shop_evidence(LOGGER, 0, flow_label="后勤商店", stage="采购后")
        check("窗口取不到时跳过（不截桌面）", empty == "", empty)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_evidence_wiring():
    print("[8f] 两个采购入口都接入了截图")
    shop_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "zzz_monthly_shop.py").read_text(encoding="utf-8")
    me_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "monthly_exchange.py").read_text(encoding="utf-8")
    check("后勤商店有采购前/后两张", shop_src.count('stage="采购前"') == 1 and shop_src.count('stage="采购后"') == 1,
          (shop_src.count('stage="采购前"'), shop_src.count('stage="采购后"')))
    check("兑换流程有采购前/后两张", me_src.count('stage="采购前"') == 1 and me_src.count('stage="采购后"') == 1,
          (me_src.count('stage="采购前"'), me_src.count('stage="采购后"')))
    check("后勤商店支持 log_label 覆盖", 'step.get("log_label") or "后勤商店"' in shop_src)


# ---------------------------------------------------------------------------
# [2] 鸣潮活跃度重查
# ---------------------------------------------------------------------------


def test_wuwa_retry_success():
    print("[9] 活跃度 0 → 等结算重查拿到 100")
    seq = [_wuwa_status(0), _wuwa_status(100)]
    sleeps = []
    with patch.object(dc, "check_wuwa_daily", side_effect=lambda p: seq.pop(0)), \
         patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        status = dc.check_wuwa_daily_with_retry(object())
    liveness = status.tasks[0]
    check("最终拿到 100", liveness.current == 100, liveness)
    check("只 sleep 一次", sleeps == [dc.WUWA_STALE_RETRY_DELAYS[0]], sleeps)


def test_wuwa_retry_exhausted():
    print("[10] 一直是 0 → 保留 0 交给上层标 stale")
    seq = [_wuwa_status(0), _wuwa_status(0), _wuwa_status(0)]
    sleeps = []
    with patch.object(dc, "check_wuwa_daily", side_effect=lambda p: seq.pop(0)), \
         patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        status = dc.check_wuwa_daily_with_retry(object())
    check("返回仍是 0", status.tasks[0].current == 0, status.tasks[0])
    check("两次重查都等过", sleeps == list(dc.WUWA_STALE_RETRY_DELAYS), sleeps)


def test_wuwa_retry_error_keeps_last():
    print("[11] 重查异常不丢上次结果")
    with patch.object(dc, "check_wuwa_daily", side_effect=[_wuwa_status(0), RuntimeError("net down")]), \
         patch.object(dc.time, "sleep", return_value=None):
        status = dc.check_wuwa_daily_with_retry(object())
    check("保留第一次结果", status.tasks[0].current == 0, status.tasks[0])
    check("没抛异常", isinstance(status, dc.GameDailyStatus))


def test_wuwa_no_retry_when_ok():
    print("[12] 活跃度正常 → 不等待")
    sleeps = []
    with patch.object(dc, "check_wuwa_daily", return_value=_wuwa_status(100)), \
         patch.object(dc.time, "sleep", side_effect=lambda s: sleeps.append(s)):
        status = dc.check_wuwa_daily_with_retry(object())
    check("直接返回 100", status.tasks[0].current == 100, status.tasks[0])
    check("零 sleep", sleeps == [], sleeps)


def test_wuwa_stale_predicate():
    print("[13] stale 判定只认「今日活跃 0/total」")
    check("0/100 算 stale", dc._wuwa_liveness_is_stale(_wuwa_status(0)) is True)
    check("100/100 不算 stale", dc._wuwa_liveness_is_stale(_wuwa_status(100)) is False)
    no_liveness = dc.GameDailyStatus("wuwa", "鸣潮", "1", "t", False,
                                     [dc.DailyTaskItem("结晶波片", 240, 240, True)], [])
    check("没有今日活跃任务不算 stale", dc._wuwa_liveness_is_stale(no_liveness) is False)


def test_check_games_uses_retry():
    print("[14] 统一入口已改走重查版本")
    src = Path(dc.__file__).read_text(encoding="utf-8")
    check("check_games_progress 调 check_wuwa_daily_with_retry",
          "wuwa_status = check_wuwa_daily_with_retry(profile)" in src)


def main():
    test_monthly_mapping()
    test_monthly_done_skip()
    test_monthly_pending_runs()
    test_monthly_failure_not_fatal()
    test_monthly_ignores_other_apps_and_stop()
    test_monthly_maintenance_skip()
    test_monthly_report_helper()
    test_batch_wiring()
    test_sweep_runs_monthly_for_apps_not_in_batch()
    test_monthly_meta_key_shared()
    test_standalone_tasks_removed()
    test_missing_item_treated_as_bought()
    test_shop_shared_already_reasons()
    test_shop_evidence_capture()
    test_evidence_wiring()
    test_wuwa_retry_success()
    test_wuwa_retry_exhausted()
    test_wuwa_retry_error_keeps_last()
    test_wuwa_no_retry_when_ok()
    test_wuwa_stale_predicate()
    test_check_games_uses_retry()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 → {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
