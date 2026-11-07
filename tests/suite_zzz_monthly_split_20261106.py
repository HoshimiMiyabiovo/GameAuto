"""2026-11-06 绝区零两条月商店拆成独立任务回归。

事故：ZZZ_MONTHLY 一条 run_flow 串 7 步、只用一个 monthly_flow:ZZZ_MONTHLY
标记。步骤3「信号残响」失败时 foreground_flow_runner 直接 return False，
步骤4-7「后勤商店」连跑都跑不到（11-01～11-06 日志里 0 条）；整条失败还不写
标记，下次批次整条重来。两个都是每月刷新的商店，本该各自独立。

改法（不动其它三个游戏）：
- flow_configs: ZZZ_MONTHLY 只留信号残响 3 步；新增 ZZZ_LOGISTICS_MONTHLY
  6 步（启动→主界面→功能菜单→后勤商店→商店页→采购）。
- background_template_runner: 新增 run_zzz_logistics_monthly_background，
  独立写 monthly_flow:ZZZ_LOGISTICS_MONTHLY。
- task_batch_runner: _EXTRA_MONTHLY_FLOWS / _EXTRA_MONTHLY_RUNNERS；
  _append_monthly_report 无论主流程成功/失败/已完成都跑附加流程。
- 主映射 _MONTHLY_FLOW_BY_APP / _MONTHLY_RUNNERS 与 _run_pending_monthly_flow
  的默认返回值保持原样，其它游戏零影响。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_monthly_split_20261106.py
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
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402

FAILURES = []
LOG = logging.getLogger("zzz_monthly_split_test")
LOG.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


class FakeController:
    stop_current_task = False
    logger = LOG


def cfgs():
    return [json.loads((PROJECT_ROOT / p).read_text(encoding="utf-8"))
            for p in ("flow_configs.json", "flow_configs.example.json")]


def test_config_split():
    print("[1] 配置：两个独立流程、各自月度标记")
    a, b = cfgs()
    for key in ("ZZZ_MONTHLY", "ZZZ_LOGISTICS_MONTHLY"):
        check(f"两份配置 {key} 一致", a[key] == b[key], key)
        check(f"{key} enabled", a[key].get("enabled") is True)
    signal, logistics = a["ZZZ_MONTHLY"], a["ZZZ_LOGISTICS_MONTHLY"]
    check("信号残响流程只剩 3 步", len(signal["steps"]) == 3,
          [s["name"] for s in signal["steps"]])
    check("信号残响流程不含 zzz_shop_buy",
          all(s.get("action") != "zzz_shop_buy" for s in signal["steps"]))
    check("后勤流程 6 步且末步是 zzz_shop_buy",
          len(logistics["steps"]) == 6
          and logistics["steps"][-1]["action"] == "zzz_shop_buy",
          [s["name"] for s in logistics["steps"]])
    check("后勤流程 start_from_main 跳到「打开功能菜单」(第3步)",
          logistics["start_from_main"]["skip_to_step"] == 3
          and logistics["steps"][2]["name"] == "打开功能菜单")
    groups = logistics["steps"][-1]["shop_groups"]
    check("两组页签与清单完整",
          [g["name"] for g in groups] == ["哨站后勤", "协会物资"]
          and sum(len(g["items"]) for g in groups) == 6,
          [(g["name"], [i["name"] for i in g["items"]]) for g in groups])
    check("两条都 close_process_on_finish（状态隔离）",
          signal.get("close_process_on_finish") is True
          and logistics.get("close_process_on_finish") is True)
    k1 = btr.monthly_flow_meta_key("ZZZ_MONTHLY")
    k2 = btr.monthly_flow_meta_key("ZZZ_LOGISTICS_MONTHLY")
    check("月度门控键互相独立", k1 != k2 and k2 == "monthly_flow:ZZZ_LOGISTICS_MONTHLY",
          (k1, k2))


def test_wiring():
    print("[2] 调度接线：主映射不变，附加流程独立")
    check("_MONTHLY_FLOW_BY_APP 仍是 4 个游戏", len(tbr._MONTHLY_FLOW_BY_APP) == 4,
          dict(tbr._MONTHLY_FLOW_BY_APP))
    check("绝区零主流程仍是 ZZZ_MONTHLY",
          tbr._MONTHLY_FLOW_BY_APP["ZenlessZoneZero"][0] == "ZZZ_MONTHLY")
    check("_MONTHLY_RUNNERS 仍是 4 条（其它游戏零影响）",
          set(tbr._MONTHLY_RUNNERS) == {v[0] for v in tbr._MONTHLY_FLOW_BY_APP.values()},
          sorted(tbr._MONTHLY_RUNNERS))
    check("附加流程挂到绝区零",
          tbr._EXTRA_MONTHLY_FLOWS.get("ZenlessZoneZero")
          == (("ZZZ_LOGISTICS_MONTHLY", "绝区零后勤商店"),))
    check("附加流程有独立 runner",
          set(tbr._EXTRA_MONTHLY_RUNNERS) == {"ZZZ_LOGISTICS_MONTHLY"}
          and callable(tbr._EXTRA_MONTHLY_RUNNERS["ZZZ_LOGISTICS_MONTHLY"]))
    with mock.patch.object(btr, "_run_monthly_flow", return_value=(True, "ok")) as run:
        ok, msg = btr.run_zzz_logistics_monthly_background(LOG)
    check("runner 用独立 flow_key 调 _run_monthly_flow",
          ok and run.call_args[0][1] == "ZZZ_LOGISTICS_MONTHLY", run.call_args)


def test_failure_isolation():
    print("[3] 主流程失败 / 已完成，附加流程照样跑")
    for label, main_result, expect_lines, expect_warn in (
        ("主流程失败", ("信号残响 ❌", False), 2, 1),
        ("主流程本月已完成", None, 1, 0),
    ):
        calls = []

        def fake(controller, app_key, *, _override=None):
            calls.append(_override)
            return main_result if _override is None else ("后勤商店 ✅", True)

        report, warnings = [], []
        with mock.patch.object(tbr, "_run_pending_monthly_flow", side_effect=fake):
            tbr._append_monthly_report(FakeController(), "ZenlessZoneZero", report, warnings)
        check(f"{label} → 附加流程仍执行", len(calls) == 2 and calls[0] is None, calls)
        check(f"{label} → 报告 {expect_lines} 行、警告 {expect_warn} 行",
              len(report) == expect_lines and len(warnings) == expect_warn,
              (report, warnings))

    # 其它游戏不应触发附加流程
    calls = []
    with mock.patch.object(tbr, "_run_pending_monthly_flow",
                           side_effect=lambda *a, **k: calls.append(k) or None):
        tbr._append_monthly_report(FakeController(), "GenshinImpact", [], [])
    check("原神不触发附加流程", calls and all(not c.get("_override") for c in calls), calls)


def main():
    test_config_split()
    test_wiring()
    test_failure_isolation()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
