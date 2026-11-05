# -*- coding: utf-8 -*-
"""崩铁每月事项交接给三月七小助手的回归（2026-11-04 用户拍板）。

背景：三月七 `tasks/daily/{synthesis,ember_exchange}.py` 已有自塑尘脂与
余烬兑换（专票/通票/命运的足迹）的完整实现，本机 11-01 前三项已成功，
「命运的足迹」只是 `asset_ember_tracks_of_destiny_enable` 开关关着。
本项目 `HSR_MONTHLY` 属重复实现且 11-04 全天假失败，用户决定整体交给三月七。

覆盖：
  [1] 三月七本机配置：足迹开关已打开（文件不存在则 SKIP）
  [2] 本项目两份 flow_configs：HSR_MONTHLY 禁用、其它月度流程不受影响
  [3] 调度器遇到 enabled=false 安静跳过：不调 runner、不写失败报告
  [4] enabled=true 时仍按原逻辑触发 runner（防止把别游戏一起跳过）

跑法：.venv\\Scripts\\python.exe tests\\suite_hsr_monthly_handoff_20261104.py
"""

import datetime
import json
import logging
import re
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.task_batch_runner as tbr  # noqa: E402

FAILURES = []
M7A_CONFIG = Path(r"D:\down\March7thAssistant_v2.2.0.3_full\config.yaml")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


class FakeController:
    stop_current_task = False

    def __init__(self):
        self.logger = logging.getLogger("suite_hsr_handoff")
        self.logger.addHandler(logging.NullHandler())


def _hsr_app_key():
    return next(
        (key for key, value in tbr._MONTHLY_FLOW_BY_APP.items()
         if value and value[0] == "HSR_MONTHLY"),
        None,
    )


def test_m7a_switch():
    print("[1] 三月七：命运的足迹开关")
    if not M7A_CONFIG.is_file():
        print("  SKIP 本机未安装三月七，跳过外部配置断言")
        return
    text = M7A_CONFIG.read_text(encoding="utf-8")
    check("asset_ember_tracks_of_destiny_enable: true",
          bool(re.search(r"^asset_ember_tracks_of_destiny_enable:\s*true", text, re.M)))
    # 2026-11-05：开关打开后三月七下次运行就会消费它，把 timestamp 写成当月
    # 时间。0（待补买）与本月时间戳（已买）都是合法状态——这条是一次性交接
    # 动作，不能把「还没消费」写死成永久断言。
    m = re.search(r"^asset_ember_tracks_of_destiny_timestamp:\s*([\d.]+)", text, re.M)
    ts = float(m.group(1)) if m else None
    this_month = datetime.datetime.now().strftime("%Y-%m")
    valid = ts is not None and (
        ts == 0
        or datetime.datetime.fromtimestamp(ts).strftime("%Y-%m") == this_month
    )
    check("timestamp 为 0（待补买）或本月已消费（三月七跑过）", valid, ts)


def test_flow_configs():
    print("[2] 本项目配置：只停 HSR_MONTHLY")
    cfgs = [
        json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        for name in ("flow_configs.json", "flow_configs.example.json")
    ]
    for i, cfg in enumerate(cfgs):
        check(f"配置{i + 1}: HSR_MONTHLY.enabled=false",
              cfg["HSR_MONTHLY"]["enabled"] is False)
    for key in ("ZZZ_MONTHLY", "GENSHIN_MONTHLY", "NTE_MONTHLY"):
        check(f"{key} 仍启用（没被误伤）", all(c[key]["enabled"] is True for c in cfgs))
    check("步骤定义保留，随时可改回",
          all(len(c["HSR_MONTHLY"]["steps"]) >= 3 for c in cfgs))


def test_disabled_flow_skips_quietly():
    print("[3] 调度器：禁用流程安静跳过，不刷 ❌、不调 runner")
    app_key = _hsr_app_key()
    check("注册表里有 HSR_MONTHLY 映射", app_key is not None,
          dict(tbr._MONTHLY_FLOW_BY_APP))
    if app_key is None:
        return
    calls = []

    def _runner(logger, *a, **k):
        calls.append(1)
        return True, "不应该跑"

    with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                    return_value={"enabled": False}), \
            mock.patch.object(tbr, "is_monthly_flow_done", return_value=False), \
            mock.patch.dict(tbr._MONTHLY_RUNNERS, {"HSR_MONTHLY": _runner}):
        result = tbr._run_pending_monthly_flow(FakeController(), app_key)
    check("返回 None（不是失败报告行）", result is None, result)
    check("没有调用月度 runner", calls == [], calls)


def test_enabled_flow_still_runs():
    print("[4] 未禁用的流程仍按原逻辑触发")
    app_key = _hsr_app_key()
    if app_key is None:
        return
    calls = []

    def _runner(logger, *a, **k):
        calls.append(1)
        return True, "完成"

    with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                    return_value={"enabled": True}), \
            mock.patch.object(tbr, "is_monthly_flow_done", return_value=False), \
            mock.patch("game_auto.services.game_update_calendar.get_maintenance_state",
                       return_value=None), \
            mock.patch.dict(tbr._MONTHLY_RUNNERS, {"HSR_MONTHLY": _runner}):
        result = tbr._run_pending_monthly_flow(FakeController(), app_key)
    check("调用了 runner", calls == [1], calls)
    check("返回成功报告行", bool(result) and result[1] is True, result)


def main():
    test_m7a_switch()
    test_flow_configs()
    test_disabled_flow_skips_quietly()
    test_enabled_flow_still_runs()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
